"""Persistent fitness history — the one thing Garmin's point-in-time APIs can't give us.

`get_race_predictions` only returns "today" and VO2max only comes back on the odd day
Garmin computes it, so a *trajectory* has to be accumulated locally. We snapshot both
into SQLite (alongside the response cache) each time the dashboard loads, and backfill
VO2max historically via per-day `get_max_metrics` (which does return past values).
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "history.sqlite"

# Re-check the most recent few days on every backfill: VO2max for "today" often
# lands a day or two late, so a date isn't truly settled until it's a bit old.
_RECHECK_DAYS = 3


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fitness_snapshot ("
        " date TEXT PRIMARY KEY,"
        " vo2max REAL,"
        " vo2_checked INTEGER NOT NULL DEFAULT 0,"  # 1 = we've queried Garmin for this date
        " p5k INTEGER, p10k INTEGER, phm INTEGER, pm INTEGER)"
    )
    # Added after the table shipped, so existing databases need the column bolted on.
    have = {r[1] for r in conn.execute("PRAGMA table_info(fitness_snapshot)")}
    if "fscore" not in have:
        conn.execute("ALTER TABLE fitness_snapshot ADD COLUMN fscore REAL")
    return conn


def _upsert(conn: sqlite3.Connection, d: str, **cols) -> None:
    """Insert a row for date `d` or update only the given columns (nulls preserved)."""
    keys = list(cols)
    conn.execute(
        f"INSERT INTO fitness_snapshot (date, {', '.join(keys)}) "
        f"VALUES (?, {', '.join('?' for _ in keys)}) "
        f"ON CONFLICT(date) DO UPDATE SET {', '.join(f'{k}=excluded.{k}' for k in keys)}",
        (d, *[cols[k] for k in keys]),
    )


def record_today(today: date, vo2max: float | None, predictions: dict | None,
                 fitness_score: float | None = None) -> None:
    """Snapshot today's VO2max + race predictions + fitness level. Called on each load."""
    d = today.isoformat()
    with _conn() as conn:
        if vo2max is not None:
            _upsert(conn, d, vo2max=float(vo2max), vo2_checked=1)
        p = _pred_seconds(predictions)
        if p:
            _upsert(conn, d, p5k=p["5K"], p10k=p["10K"], phm=p["half_marathon"], pm=p["marathon"])
        if fitness_score is not None:
            _upsert(conn, d, fscore=float(fitness_score))
        conn.commit()


def _pred_seconds(predictions: dict | None) -> dict | None:
    if not isinstance(predictions, dict):
        return None
    preds = predictions.get("predictions") if "predictions" in predictions else predictions
    if not isinstance(preds, dict):
        return None
    out = {}
    for k in ("5K", "10K", "half_marathon", "marathon"):
        v = preds.get(k)
        out[k] = int(v["time_seconds"]) if isinstance(v, dict) and v.get("time_seconds") else None
    return out if any(out.values()) else None


def missing_vo2_dates(today: date, days: int) -> list[date]:
    """Dates in the window still worth querying Garmin for.

    A date is *settled* (skip it) once we've queried it AND either it's older than the
    recent-recheck window or we already found a value. That means: past no-data days are
    never re-fetched, but the last few days keep getting checked until a value lands.
    """
    start = today - timedelta(days=days)
    cutoff = (today - timedelta(days=_RECHECK_DAYS)).isoformat()
    with _conn() as conn:
        settled = {
            r[0] for r in conn.execute(
                "SELECT date FROM fitness_snapshot "
                "WHERE vo2_checked = 1 AND (date < ? OR vo2max IS NOT NULL)",
                (cutoff,),
            )
        }
    out, d = [], start
    while d <= today:
        if d.isoformat() not in settled:
            out.append(d)
        d += timedelta(days=1)
    return out


def store_vo2(d: date, value: float | None) -> None:
    """Mark a date as queried and store its VO2max (may be None if Garmin had none)."""
    with _conn() as conn:
        _upsert(conn, d.isoformat(), vo2max=(float(value) if value else None), vo2_checked=1)
        conn.commit()


def series(today: date, days: int) -> dict:
    """The stored trajectory for the chart: sparse VO2max points + prediction points."""
    start = (today - timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT date, vo2max, p5k, p10k, phm, pm, fscore FROM fitness_snapshot "
            "WHERE date >= ? ORDER BY date",
            (start,),
        ).fetchall()
    vo2 = [{"date": r[0], "value": r[1]} for r in rows if r[1] is not None]
    preds = [
        {"date": r[0], "p5k": r[2], "p10k": r[3], "phm": r[4], "pm": r[5]}
        for r in rows if any(r[2:6])
    ]
    score = [{"date": r[0], "value": r[6]} for r in rows if r[6] is not None]
    return {"vo2max": vo2, "predictions": preds, "fitness_score": score}


def earlier_score(today: date, days_back: int = 30) -> dict | None:
    """Nearest stored fitness level at or before `days_back` ago, for a trend arrow.

    The trajectory only exists once the dashboard has been loaded a few times -- there is
    no way to backfill it, since the score depends on data Garmin only serves for today.
    """
    target = (today - timedelta(days=days_back)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT date, fscore FROM fitness_snapshot "
            "WHERE fscore IS NOT NULL AND date <= ? ORDER BY date DESC LIMIT 1",
            (target,),
        ).fetchone()
    return {"date": row[0], "score": row[1]} if row else None
