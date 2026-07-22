"""Local activity store — a queryable mirror of the athlete's Garmin activities.

Activities are immutable and append-only (you can't add a run to a past day), so we
sync them into SQLite once and then only pull what's new. Every view that needs runs
reads from here — instant, offline-capable, and immune to Garmin's rate limiting —
instead of re-paging the Garmin API on each date-range change.

Distinct from cache.py (a short-lived TTL cache of raw API responses) and history.py
(derived fitness snapshots): this is the durable source of truth for activities.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "activities.sqlite"

# Columns mirrored from the Garmin MCP activity list-item shape, so rows read back out
# as the same dicts the rest of the app already consumes.
_COLS = (
    "id", "start_time", "type", "name", "event_type", "distance_meters",
    "duration_seconds", "avg_hr_bpm", "max_hr_bpm", "elevation_gain_meters",
    "elevation_loss_meters", "steps", "calories",
)


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS activities ("
        " id INTEGER PRIMARY KEY,"
        " start_time TEXT, type TEXT, name TEXT, event_type TEXT,"
        " distance_meters REAL, duration_seconds REAL,"
        " avg_hr_bpm REAL, max_hr_bpm REAL,"
        " elevation_gain_meters REAL, elevation_loss_meters REAL,"
        " steps INTEGER, calories REAL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_time)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def upsert(activities: list[dict]) -> int:
    """Insert or replace activities by id (so a re-synced recent window picks up edits).
    Returns the number written."""
    rows = [tuple(a.get(c) for c in _COLS) for a in activities if a.get("id") is not None]
    if not rows:
        return 0
    with _conn() as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO activities ({', '.join(_COLS)}) "
            f"VALUES ({', '.join('?' for _ in _COLS)})",
            rows,
        )
        conn.commit()
    return len(rows)


def count() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]


def latest_start() -> str | None:
    """The most recent stored start_time (used to bound an incremental sync)."""
    with _conn() as conn:
        r = conn.execute("SELECT MAX(start_time) FROM activities").fetchone()
    return r[0] if r and r[0] else None


def is_synced() -> bool:
    """True once an initial full backfill has completed — after which the store is
    authoritative and an empty date range genuinely means 'no activities', not 'unfetched'."""
    with _conn() as conn:
        r = conn.execute("SELECT value FROM meta WHERE key = 'initial_synced'").fetchone()
    return bool(r and r[0] == "1")


def mark_synced() -> None:
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('initial_synced', '1')")
        conn.commit()


def names_by_id(ids: list[int]) -> dict[int, str]:
    """Activity names for the given ids. Garmin names runs after their location, so the
    route map uses these to label the places it finds."""
    if not ids:
        return {}
    with _conn() as conn:
        out = {}
        for chunk in (ids[i:i + 400] for i in range(0, len(ids), 400)):   # stay under SQLite's var limit
            q = f"SELECT id, name FROM activities WHERE id IN ({', '.join('?' for _ in chunk)})"
            out.update({r[0]: r[1] or "" for r in conn.execute(q, chunk)})
        return out


def activities_between(start: date, end: date) -> list[dict]:
    """All stored activities whose calendar date falls in [start, end], newest first —
    matching the ordering the Garmin MCP returns."""
    with _conn() as conn:
        cur = conn.execute(
            f"SELECT {', '.join(_COLS)} FROM activities "
            "WHERE substr(start_time, 1, 10) BETWEEN ? AND ? "
            "ORDER BY start_time DESC",
            (start.isoformat(), end.isoformat()),
        )
        return [dict(zip(_COLS, row)) for row in cur.fetchall()]
