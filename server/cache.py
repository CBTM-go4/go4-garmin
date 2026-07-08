"""Tiny SQLite TTL cache for MCP responses.

Garmin rate-limits aggressively (HTTP 429), so we cache every tool response.
Past-dated data is effectively immutable, so it gets a long TTL; today's data
gets a short TTL so the dashboard stays fresh.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cache.sqlite"


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache ("
        " key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)"
    )
    return conn


def make_key(tool: str, args: dict[str, Any]) -> str:
    return tool + ":" + json.dumps(args, sort_keys=True, default=str)


def get(key: str) -> Any | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT value, expires FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    value, expires = row
    if expires < time.time():
        return None
    return json.loads(value)


def put(key: str, value: Any, ttl_seconds: float) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time() + ttl_seconds),
        )
        conn.commit()


def clear() -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM cache")
        conn.commit()
