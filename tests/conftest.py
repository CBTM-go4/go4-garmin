"""Shared fixtures/helpers for the test suite."""
from __future__ import annotations

from datetime import date, timedelta


def run(day: date, *, km: float = 10.0, minutes: float = 60.0,
        avg_hr: int | None = 150, max_hr: int | None = 175,
        name: str = "Run", event_type: str | None = None) -> dict:
    """Build a curated activity dict shaped like the Garmin MCP output.

    Only the fields metrics.py reads are populated; everything is keyword-driven so
    each test states just what it cares about.
    """
    r: dict = {
        "start_time": f"{day.isoformat()}T07:00:00",
        "distance_meters": km * 1000,
        "duration_seconds": minutes * 60,
        "name": name,
    }
    if avg_hr is not None:
        r["avg_hr_bpm"] = avg_hr
    if max_hr is not None:
        r["max_hr_bpm"] = max_hr
    if event_type is not None:
        r["event_type"] = event_type
    return r


def daily_runs(end: date, ndays: int, **kw) -> list[dict]:
    """One identical run on each of the `ndays` days ending at `end`."""
    return [run(end - timedelta(days=i), **kw) for i in range(ndays)]
