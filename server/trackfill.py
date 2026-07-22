"""Background backfill of GPS tracks.

One activity file per run, downloaded serially — a decade of running is ~800 files
and several GB over the wire, so this is a slow, resumable, one-time sweep that
runs behind the dashboard rather than blocking it.

Newest runs are fetched first (RECENT_YEARS), which is what makes the map usable in
a couple of minutes instead of half an hour: the years you're actually training in
appear immediately and the older ones fill in underneath while you look at it.
Progress is published for the UI, and every fetched activity is recorded — tracks
and treadmill runs alike — so a restart resumes where it stopped.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import date, timedelta
from pathlib import Path

from . import store, tracks
from .garmin import RUN_TYPES, GarminData, is_demo

log = logging.getLogger("garmin_coach.tracks")

# Fetch runs from the last two years before older history.
RECENT_YEARS = 2
# Pause between downloads. Garmin throttles bulk file access, and this sweep is
# explicitly not in a hurry — the UI is usable throughout.
_DELAY_S = 0.4
# Give up on a single activity after this long rather than stalling the whole sweep.
_TIMEOUT_S = 120


class Backfill:
    """Owns the sweep and the progress the API reports."""

    def __init__(self) -> None:
        self.running = False
        self.done = 0            # activities fetched this run
        self.total = 0           # activities queued this run
        self.failed = 0
        self.task: asyncio.Task | None = None

    def status(self) -> dict:
        return {
            "running": self.running,
            "done": self.done,
            "total": self.total,
            "failed": self.failed,
            **tracks.counts(),
        }

    def start(self, gd: GarminData) -> None:
        if self.running or is_demo() or not gd.available:
            return
        self.task = asyncio.create_task(self._run(gd), name="track-backfill")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self, gd: GarminData) -> None:
        self.running = True
        try:
            pending = _pending_runs()
            self.total, self.done, self.failed = len(pending), 0, 0
            if pending:
                log.info("Track backfill: %d runs to fetch", len(pending))
            with tempfile.TemporaryDirectory(prefix="garmin-gpx-") as tmp:
                for act in pending:
                    await self._one(gd, act, Path(tmp))
                    await asyncio.sleep(_DELAY_S)
            if self.total:
                log.info("Track backfill finished: %d fetched, %d failed, %s",
                         self.done, self.failed, tracks.counts())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Track backfill aborted: %s", e)
        finally:
            self.running = False

    async def _one(self, gd: GarminData, act: dict, tmp: Path) -> None:
        """Fetch, simplify and store one activity's track. A failure here is never
        fatal — it's left unrecorded so the next sweep retries it."""
        aid = act["id"]
        try:
            xml = await asyncio.wait_for(gd.gpx(aid, tmp), timeout=_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            self.failed += 1
            log.debug("track %s failed: %s", aid, e)
            return
        coords = tracks.simplify(tracks.parse_gpx(xml)) if xml else []
        tracks.store(aid, act.get("start_time"), coords)
        self.done += 1


def _pending_runs() -> list[dict]:
    """Runs with no track row yet, newest years first.

    Reads the activity store rather than Garmin: it already mirrors the whole history,
    so the work list costs nothing and the sweep needs no pagination of its own.
    """
    have = tracks.have_ids()
    today = date.today()
    recent_from = today - timedelta(days=365 * RECENT_YEARS)
    runs = [
        a for a in store.activities_between(date(2007, 1, 1), today)
        if (a.get("type") or "") in RUN_TYPES and a.get("id") not in have
    ]
    runs.sort(key=lambda a: a.get("start_time") or "", reverse=True)   # newest first
    recent = [a for a in runs if (a.get("start_time") or "")[:10] >= recent_from.isoformat()]
    older = [a for a in runs if (a.get("start_time") or "")[:10] < recent_from.isoformat()]
    return recent + older
