"""The athlete's goal races — the season's targets.

Personal race goals are NOT committed to the repo. They live in ``data/races.json``
(gitignored) and are loaded at request time. This module ships an empty placeholder so
a fresh clone runs cleanly with no personal data; drop in a ``data/races.json`` to
populate the Goal Races view.

Each race is a dict:
    {
      "name": str,
      "date": "YYYY-MM-DD" | null,   # null = tentative (no countdown, sorts last)
      "distance_km": number | null,
      "surface": "road" | "trail" | null,
      "location": str,
      "role": str,                    # short label, e.g. "Qualifier", "Goal"
      "priority": "A" | "prep" | "tentative",   # "A" = the season's A-race
      "note": str,                    # optional badge, e.g. "100th · Down run"
      "focus": str,
    }
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_RACES_FILE = Path(__file__).resolve().parent.parent / "data" / "races.json"

# Placeholder — no personal data. Real races load from data/races.json (gitignored).
_PLACEHOLDER: list[dict] = []


def _load() -> list[dict]:
    """Load goal races from the local (gitignored) config, or the placeholder."""
    try:
        if _RACES_FILE.exists():
            data = json.loads(_RACES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:  # noqa: BLE001 — a bad/edited config must never crash the API
        pass
    return _PLACEHOLDER


def with_countdown(today: date) -> list[dict]:
    """Goal races annotated with days remaining, soonest first; undated (tentative)
    races carry days_to=None and sort to the end."""
    out = []
    for r in _load():
        days = (date.fromisoformat(r["date"]) - today).days if r.get("date") else None
        out.append({**r, "days_to": days})
    out.sort(key=lambda r: (r.get("date") is None, r.get("date") or ""))
    return out
