"""Live run bridge for phone/GPS telemetry.

This module keeps a single active run session in memory and evaluates incoming
HR/pace samples against the athlete's long-run guardrails. It is deliberately
simple and stateless enough to be driven from a phone browser, a tiny native app,
or a custom companion script.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import time
import uuid
from typing import Any

DEFAULT_TARGET_HR_MIN = 131
DEFAULT_TARGET_HR_MAX = 145
DEFAULT_HARD_HR_CAP = 145
DEFAULT_ALERT_GAP_SECONDS = 30
DEFAULT_HISTORY_SIZE = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utcnow()).isoformat()


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _fmt_pace(seconds_per_km: float | None) -> str:
    if not seconds_per_km or seconds_per_km <= 0:
        return "–"
    m = int(seconds_per_km // 60)
    s = int(round(seconds_per_km % 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}/km"


def _fmt_distance(km: float | None) -> str:
    if km is None:
        return "–"
    return f"{km:.2f} km"


def _fmt_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    secs = max(0, int(round(seconds)))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class LiveSession:
    session_id: str
    started_at: datetime
    label: str
    target_hr_min: int = DEFAULT_TARGET_HR_MIN
    target_hr_max: int = DEFAULT_TARGET_HR_MAX
    hard_hr_cap: int = DEFAULT_HARD_HR_CAP
    alert_gap_seconds: int = DEFAULT_ALERT_GAP_SECONDS
    samples: deque = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_SIZE))
    alerts: deque = field(default_factory=lambda: deque(maxlen=15))
    finished_at: datetime | None = None

    def active(self) -> bool:
        return self.finished_at is None

    def last_sample(self) -> dict[str, Any] | None:
        return self.samples[-1] if self.samples else None


class LiveBridge:
    """In-memory live telemetry bridge.

    The bridge accepts telemetry from a phone/browser or companion app and returns
    coaching feedback based on the athlete's easy-long-run rules: stay in Z2, avoid
    surges, and back off once HR crosses the cap.
    """

    def __init__(self) -> None:
        self._session: LiveSession | None = None
        self._last_alert_signature: tuple[str, ...] | None = None

    def defaults(self) -> dict[str, Any]:
        return {
            "target_hr_min": DEFAULT_TARGET_HR_MIN,
            "target_hr_max": DEFAULT_TARGET_HR_MAX,
            "hard_hr_cap": DEFAULT_HARD_HR_CAP,
            "alert_gap_seconds": DEFAULT_ALERT_GAP_SECONDS,
        }

    def snapshot(self) -> dict[str, Any]:
        session = self._session
        if not session:
            return {"active": False, "session": None, "latest": None, "status": None}
        latest = session.last_sample()
        status = self._evaluate(session, latest) if latest else {
            "level": "info",
            "headline": "Waiting for the first sample",
            "message": "Open the phone bridge and start the run to begin live monitoring.",
        }
        return {
            "active": session.active(),
            "session": self._session_payload(session),
            "latest": latest,
            "status": status,
            "alerts": list(session.alerts),
        }

    def start(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        session = LiveSession(
            session_id=str(payload.get("session_id") or uuid.uuid4().hex[:12]),
            started_at=_utcnow(),
            label=str(payload.get("label") or "Long run"),
            target_hr_min=_coerce_int(payload.get("target_hr_min")) or DEFAULT_TARGET_HR_MIN,
            target_hr_max=_coerce_int(payload.get("target_hr_max")) or DEFAULT_TARGET_HR_MAX,
            hard_hr_cap=_coerce_int(payload.get("hard_hr_cap")) or DEFAULT_HARD_HR_CAP,
            alert_gap_seconds=_coerce_int(payload.get("alert_gap_seconds")) or DEFAULT_ALERT_GAP_SECONDS,
        )
        self._session = session
        self._last_alert_signature = None
        return {
            "ok": True,
            "session": self._session_payload(session),
            "status": {
                "level": "info",
                "headline": "Live run started",
                "message": "Waiting for the first telemetry sample from the phone bridge.",
            },
        }

    def stop(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._require_session()
        session.finished_at = _utcnow()
        return {
            "ok": True,
            "session": self._session_payload(session),
            "status": {
                "level": "info",
                "headline": "Live run stopped",
                "message": "Session closed.",
            },
        }

    def update(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._require_session()
        payload = payload or {}
        sample = self._normalize_sample(payload)
        sample["ts"] = _iso()
        session.samples.append(sample)
        status = self._evaluate(session, sample)
        if status.get("alert"):
            signature = tuple(status["alert"].get(k, "") for k in ("level", "headline", "message"))
            if signature != self._last_alert_signature:
                session.alerts.appendleft({
                    "ts": sample["ts"],
                    **status["alert"],
                })
                self._last_alert_signature = signature
        return self.snapshot()

    def _require_session(self) -> LiveSession:
        if not self._session:
            raise ValueError("No live session started yet")
        return self._session

    def _session_payload(self, session: LiveSession) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "label": session.label,
            "started_at": _iso(session.started_at),
            "finished_at": _iso(session.finished_at) if session.finished_at else None,
            "target_hr_min": session.target_hr_min,
            "target_hr_max": session.target_hr_max,
            "hard_hr_cap": session.hard_hr_cap,
            "alert_gap_seconds": session.alert_gap_seconds,
            "sample_count": len(session.samples),
        }

    def _normalize_sample(self, payload: dict[str, Any]) -> dict[str, Any]:
        hr = _coerce_int(payload.get("hr"))
        pace_s_per_km = _coerce_float(payload.get("pace_s_per_km"))
        if pace_s_per_km is None:
            pace_min = _coerce_float(payload.get("pace_min_per_km"))
            if pace_min is not None:
                pace_s_per_km = pace_min * 60.0
        distance_km = _coerce_float(payload.get("distance_km"))
        elapsed_s = _coerce_float(payload.get("elapsed_s"))
        speed_mps = _coerce_float(payload.get("speed_mps"))
        lat = _coerce_float(payload.get("lat"))
        lon = _coerce_float(payload.get("lon"))
        return {
            "hr": hr,
            "pace_s_per_km": pace_s_per_km,
            "pace": _fmt_pace(pace_s_per_km),
            "distance_km": distance_km,
            "distance": _fmt_distance(distance_km),
            "elapsed_s": elapsed_s,
            "elapsed": _fmt_elapsed(elapsed_s),
            "speed_mps": speed_mps,
            "lat": lat,
            "lon": lon,
        }

    def _evaluate(self, session: LiveSession, sample: dict[str, Any]) -> dict[str, Any]:
        hr = sample.get("hr")
        pace_s = sample.get("pace_s_per_km")
        recent = list(session.samples)
        recent_hr = [s["hr"] for s in recent[-4:] if s.get("hr") is not None]
        recent_pace = [s["pace_s_per_km"] for s in recent[-4:] if s.get("pace_s_per_km")]

        if hr is None and pace_s is None:
            return {
                "level": "warning",
                "headline": "Waiting for live data",
                "message": "No HR or pace data yet. Check the phone bridge and sensor pairing.",
            }

        if hr is None:
            return {
                "level": "warning",
                "headline": "Heart rate missing",
                "message": "Pace is coming through, but HR is not. Pair the chest strap or watch broadcast.",
            }

        if hr >= session.hard_hr_cap + 3:
            return self._with_alert(
                session,
                "red",
                "Back off now",
                f"HR is {hr} bpm, above the {session.hard_hr_cap} cap. Ease off or walk until it comes back down.",
                hr=hr,
                pace_s=pace_s,
                recent_hr=recent_hr,
                recent_pace=recent_pace,
            )

        if hr > session.hard_hr_cap:
            return self._with_alert(
                session,
                "amber",
                "HR is creeping up",
                f"HR is {hr} bpm. Stay smooth and back the pace off a touch so it settles under {session.hard_hr_cap}.",
                hr=hr,
                pace_s=pace_s,
                recent_hr=recent_hr,
                recent_pace=recent_pace,
            )

        if hr < session.target_hr_min and sample.get("elapsed_s") and sample["elapsed_s"] > 900:
            return {
                "level": "good",
                "headline": "Still easy",
                "message": f"HR is {hr} bpm, below the Z2 band. You can let the pace come up gently if the effort stays relaxed.",
            }

        if pace_s and recent_pace:
            avg_pace = sum(recent_pace) / len(recent_pace)
            if pace_s < avg_pace - 15 and hr >= session.target_hr_max - 2:
                return self._with_alert(
                    session,
                    "amber",
                    "Surge detected",
                    f"Pace jumped to {_fmt_pace(pace_s)} while HR is {hr} bpm. Ease back to the steady long-run rhythm.",
                    hr=hr,
                    pace_s=pace_s,
                    recent_hr=recent_hr,
                    recent_pace=recent_pace,
                )

        if hr <= session.target_hr_max:
            return {
                "level": "good",
                "headline": "On target",
                "message": f"HR {hr} bpm and pace {sample.get('pace', '–')} are in the right lane. Hold this effort.",
            }

        return {
            "level": "info",
            "headline": "Keep monitoring",
            "message": f"HR {hr} bpm, pace {sample.get('pace', '–')}. Stay smooth and keep an eye on drift.",
        }

    def _with_alert(
        self,
        session: LiveSession,
        level: str,
        headline: str,
        message: str,
        *,
        hr: int | None,
        pace_s: float | None,
        recent_hr: list[int],
        recent_pace: list[float],
    ) -> dict[str, Any]:
        alert = {
            "level": level,
            "headline": headline,
            "message": message,
        }
        return {
            "level": level,
            "headline": headline,
            "message": message,
            "alert": alert,
            "diagnostics": {
                "hr": hr,
                "pace_s_per_km": pace_s,
                "recent_hr_avg": round(sum(recent_hr) / len(recent_hr), 1) if recent_hr else None,
                "recent_pace_avg": round(sum(recent_pace) / len(recent_pace), 1) if recent_pace else None,
            },
        }


def distance_from_positions(points: list[dict[str, Any]]) -> float:
    """Return cumulative distance in metres for geolocation points.

    Each point is expected to have lat/lon keys. Missing or degenerate points are
    ignored. Useful for companion clients that only have GPS positions and need to
    derive pace before posting to the bridge.
    """
    total = 0.0
    last = None
    for pt in points:
        lat = _coerce_float(pt.get("lat"))
        lon = _coerce_float(pt.get("lon"))
        if lat is None or lon is None:
            continue
        if last is not None:
            total += _haversine_m(last[0], last[1], lat, lon)
        last = (lat, lon)
    return total
