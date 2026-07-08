"""High-level Garmin data access.

Wraps the MCP client with caching and demo-mode fallback, and exposes clean
functions the rest of the app uses. Every function returns the *curated* shapes
the Garmin MCP server produces (see garmin_mcp source).
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta
from typing import Any

from . import cache, demo
from .mcp_client import GarminMCP, GarminMCPUnavailable

# Activity type keys (garminconnect typeKey) that count as a run.
RUN_TYPES = {
    "running", "trail_running", "treadmill_running", "track_running",
    "indoor_running", "obstacle_run", "ultra_run", "virtual_run",
}

_VOLATILE_TTL = 15 * 60          # today's data — refresh often
_STABLE_TTL = 30 * 24 * 3600     # past, immutable data
_NEG_TTL = 20 * 60               # cache "no data" results this long
_NONE = "__NONE__"               # sentinel for a cached None


def is_demo() -> bool:
    return os.environ.get("GARMIN_COACH_DEMO", "").lower() in ("1", "true", "yes")


def _ttl_for(d: str | None) -> float:
    """Long TTL for past dates, short for today/unknown."""
    if not d:
        return _VOLATILE_TTL
    try:
        return _STABLE_TTL if datetime.fromisoformat(d[:10]).date() < date.today() else _VOLATILE_TTL
    except ValueError:
        return _VOLATILE_TTL


class GarminData:
    def __init__(self, mcp: GarminMCP | None):
        self.mcp = mcp
        self._direct = None                       # lazily-logged-in garminconnect client
        self._direct_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return is_demo() or (self.mcp is not None and self.mcp.connected)

    @property
    def status_error(self) -> str | None:
        if is_demo():
            return None
        return self.mcp.error if self.mcp else "MCP not initialized"

    async def _call(self, tool: str, ttl: float, **args: Any) -> Any:
        """Cached MCP call. In demo mode, serve synthetic fixtures instead.

        Negative results (None) are cached too (short TTL) so metrics that this
        account doesn't record — e.g. training status — aren't re-fetched on
        every page load.
        """
        if is_demo():
            return demo.call(tool, args)
        key = cache.make_key(tool, args)
        hit = cache.get(key)
        if hit is not None:
            return None if hit == _NONE else hit
        if self.mcp is None:
            raise GarminMCPUnavailable("Garmin MCP not initialized")
        value = await self.mcp.call(tool, **args)
        cache.put(key, value if value is not None else _NONE,
                  ttl if value is not None else _NEG_TTL)
        return value

    # ---- activities -------------------------------------------------------
    async def activities_between(self, start: date, end: date) -> list[dict]:
        """All activities in the range, following the MCP tool's pagination."""
        acts: list[dict] = []
        for page in range(60):  # safety cap (60 * 200 = 12k activities)
            data = await self._call(
                "get_activities_by_date",
                ttl=_VOLATILE_TTL,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                page=page,
                page_size=200,
            )
            if not isinstance(data, dict):
                break
            acts.extend(data.get("activities", []))
            if not data.get("has_more"):
                break
        return acts

    async def runs_between(self, start: date, end: date) -> list[dict]:
        acts = await self.activities_between(start, end)
        return [a for a in acts if (a.get("type") or "") in RUN_TYPES]

    async def activity(self, activity_id: int | str) -> dict | None:
        return await self._call("get_activity", ttl=_STABLE_TTL, activity_id=activity_id)

    async def activity_splits(self, activity_id: int | str) -> Any:
        return await self._call("get_activity_splits", ttl=_STABLE_TTL, activity_id=activity_id)

    async def activity_hr_zones(self, activity_id: int | str) -> Any:
        return await self._call("get_activity_hr_in_timezones", ttl=_STABLE_TTL, activity_id=activity_id)

    async def activity_weather(self, activity_id: int | str) -> Any:
        return await self._call("get_activity_weather", ttl=_STABLE_TTL, activity_id=activity_id)

    # ---- daily health / training -----------------------------------------
    async def training_status(self, d: date) -> dict | None:
        return await self._call("get_training_status", ttl=_ttl_for(d.isoformat()), date=d.isoformat())

    async def training_readiness(self, d: date) -> Any:
        return await self._call("get_training_readiness", ttl=_ttl_for(d.isoformat()), date=d.isoformat())

    async def hrv(self, d: date) -> Any:
        return await self._call("get_hrv_data", ttl=_ttl_for(d.isoformat()), date=d.isoformat())

    async def sleep(self, d: date) -> Any:
        return await self._call("get_sleep_data", ttl=_ttl_for(d.isoformat()), date=d.isoformat())

    async def sleep_summary(self, d: date) -> Any:
        """Compact sleep summary (score, stages, overnight HRV) — ~1KB vs ~50KB."""
        return await self._call("get_sleep_summary", ttl=_ttl_for(d.isoformat()), date=d.isoformat())

    async def body_battery(self, start: date, end: date) -> Any:
        return await self._call(
            "get_body_battery", ttl=_VOLATILE_TTL,
            start_date=start.isoformat(), end_date=end.isoformat(),
        )

    async def race_predictions(self) -> Any:
        """Garmin's predicted 5K/10K/HM/marathon times from current fitness."""
        return await self._call("get_race_predictions", ttl=_VOLATILE_TTL)

    # ---- VO2max (not exposed by the MCP; fetched directly, reusing the token) --
    async def _direct_client(self):
        if is_demo():
            return None
        if self._direct is None:
            async with self._direct_lock:
                if self._direct is None:
                    def _login():
                        from garminconnect import Garmin
                        g = Garmin()
                        g.login("~/.garminconnect")
                        return g
                    try:
                        self._direct = await asyncio.to_thread(_login)
                    except Exception:  # noqa: BLE001
                        self._direct = None
        return self._direct

    async def vo2max_recent(self, today: date, back: int = 14) -> dict | None:
        """Most recent VO2max. Garmin only computes it on run days, so walk back."""
        if is_demo():
            return None
        key = cache.make_key("vo2max_recent", {"d": today.isoformat()})
        hit = cache.get(key)
        if hit is not None:
            return hit or None
        g = await self._direct_client()
        if not g:
            return None

        def _fetch():
            for i in range(back):
                d = (today - timedelta(days=i)).isoformat()
                try:
                    r = g.get_max_metrics(d)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(r, list) and r and isinstance(r[0].get("generic"), dict):
                    gen = r[0]["generic"]
                    v = gen.get("vo2MaxValue") or gen.get("vo2MaxPreciseValue")
                    if v:
                        return {"vo2_max": v, "date": d,
                                "vo2_max_precise": gen.get("vo2MaxPreciseValue")}
            return None

        res = await asyncio.to_thread(_fetch)
        cache.put(key, res or {}, _VOLATILE_TTL * 6)
        return res

    async def vo2max_on(self, d: date) -> float | None:
        """VO2max for a single date (or None if Garmin didn't compute one that day).

        Used to backfill the fitness-history trend. Reuses the direct client/token.
        """
        if is_demo():
            return None
        g = await self._direct_client()
        if not g:
            return None

        def _fetch():
            try:
                r = g.get_max_metrics(d.isoformat())
            except Exception:  # noqa: BLE001
                return None
            if isinstance(r, list) and r and isinstance(r[0].get("generic"), dict):
                gen = r[0]["generic"]
                return gen.get("vo2MaxValue") or gen.get("vo2MaxPreciseValue")
            return None

        return await asyncio.to_thread(_fetch)
