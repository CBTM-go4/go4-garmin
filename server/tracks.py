"""Local GPS track store — the route heatmap's source.

Garmin's activity list carries no coordinates: a track only comes from downloading
that activity's file, one HTTP round trip each, several MB of 17-decimal-place GPX
per run. Far too heavy to do on demand, so tracks are backfilled once in the
background and simplified hard on the way in:

  * points thinned to ~15 m spacing (a route is drawn a few hundred pixels wide;
    per-second fidelity is invisible and costs 10x the bytes), and
  * stored as an encoded polyline (~5 bytes/point vs ~40 for JSON floats),
    handed to the browser verbatim and decoded there.

A 14 km run lands at ~5 KB, so a decade of running is a few MB — small enough to
ship the whole heatmap in one request and render it client-side.

Runs without GPS (treadmill) are recorded with an empty polyline so the backfill
knows not to ask for them again. Distinct from store.py (activity metadata) and
cache.py (short-lived API responses); this is the durable geometry.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tracks.sqlite"

# Thin to this spacing, in metres. Below ~10 m the extra points are sub-pixel at any
# zoom the heatmap offers; above ~25 m corners start to visibly cut.
SIMPLIFY_M = 15.0


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tracks ("
        " activity_id INTEGER PRIMARY KEY,"
        " start_time TEXT,"
        " points INTEGER NOT NULL DEFAULT 0,"   # 0 = fetched, but no GPS in the file
        " polyline TEXT NOT NULL DEFAULT '',"
        " min_lat REAL, max_lat REAL, min_lon REAL, max_lon REAL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_start ON tracks(start_time)")
    return conn


def store(activity_id: int, start_time: str | None, coords: list[tuple[float, float]]) -> None:
    """Record one activity's simplified track (an empty list marks 'no GPS')."""
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tracks"
            " (activity_id, start_time, points, polyline, min_lat, max_lat, min_lon, max_lon)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(activity_id), start_time, len(coords), encode_polyline(coords),
             min(lats) if lats else None, max(lats) if lats else None,
             min(lons) if lons else None, max(lons) if lons else None),
        )
        conn.commit()


def have_ids() -> set[int]:
    """Activity ids already fetched — including the no-GPS ones, so they aren't retried."""
    with _conn() as conn:
        return {r[0] for r in conn.execute("SELECT activity_id FROM tracks")}


def counts() -> dict:
    """Backfill progress: how many activities are fetched, and how many had a track."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(points > 0), 0), COALESCE(SUM(points), 0) FROM tracks"
        ).fetchone()
    return {"fetched": row[0], "with_gps": row[1], "points": row[2]}


def all_tracks() -> list[dict]:
    """Every stored track with geometry, oldest first (draw order doesn't matter for
    additive compositing, but a stable order keeps the payload diff-friendly)."""
    with _conn() as conn:
        cur = conn.execute(
            "SELECT activity_id, start_time, points, polyline FROM tracks "
            "WHERE points > 0 ORDER BY start_time"
        )
        return [{"id": r[0], "date": (r[1] or "")[:10], "n": r[2], "p": r[3]} for r in cur]


def bounds() -> dict | None:
    """Bounding box over every stored track — the map's initial view."""
    with _conn() as conn:
        r = conn.execute(
            "SELECT MIN(min_lat), MAX(max_lat), MIN(min_lon), MAX(max_lon) FROM tracks "
            "WHERE points > 0"
        ).fetchone()
    if not r or r[0] is None:
        return None
    return {"min_lat": r[0], "max_lat": r[1], "min_lon": r[2], "max_lon": r[3]}


# ---- geometry ---------------------------------------------------------------
def simplify(coords: list[tuple[float, float]], spacing_m: float = SIMPLIFY_M) -> list[tuple[float, float]]:
    """Keep the first point, then only points at least `spacing_m` from the last kept
    one (plus the final point, so the route closes where it really ended).

    Distance-based rather than Douglas-Peucker: it also flattens the dense clump a
    watch records while you stand still at a trailhead, which DP would preserve.
    """
    out: list[tuple[float, float]] = []
    for c in coords:
        if not out or _rough_m(out[-1], c) >= spacing_m:
            out.append(c)
    if coords and len(out) > 1 and out[-1] != coords[-1]:
        out.append(coords[-1])
    return out


def _rough_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Equirectangular metres — exact enough at run scale, and far cheaper than
    haversine when it runs over millions of points."""
    import math
    dlat = (b[0] - a[0]) * 111_320.0
    dlon = (b[1] - a[1]) * 111_320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dlat, dlon)


def encode_polyline(coords: list[tuple[float, float]], precision: int = 5) -> str:
    """Google's encoded-polyline format: 1e5-degree deltas in base64-ish chunks.

    Deltas between adjacent points are tiny, so most take 2-3 chars instead of the
    ~20 a JSON float pair costs. The browser decodes it in a few lines.
    """
    factor = 10 ** precision
    out: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in coords:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            v = ~(delta << 1) if delta < 0 else (delta << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def parse_gpx(xml: str) -> list[tuple[float, float]]:
    """Pull track points out of a GPX file.

    A regex rather than an XML parse: Garmin's GPX carries one <trkpt> per second
    with 17 significant digits, so a 14 km run is a 3 MB document — walking a DOM
    of it costs far more than scanning for the attributes we want.
    """
    import re
    return [
        (float(lat), float(lon))
        for lat, lon in re.findall(r'<trkpt[^>]*?lat="([-0-9.]+)"[^>]*?lon="([-0-9.]+)"', xml)
    ]
