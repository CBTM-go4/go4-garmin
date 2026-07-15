"""The athlete's goal races — the season's targets, in one place.

Not Garmin data: these are chosen objectives that drive the training plan, so they
live as explicit config rather than anything derived. Ordered by date; the one marked
priority "A" is the season's A-race that everything builds toward.
"""
from __future__ import annotations

from datetime import date

GOAL_RACES: list[dict] = [
    {
        "name": "Nelson Mandela Marathon",
        "date": "2026-10-18",
        "distance_km": 42.2,
        "surface": "road",
        "location": "Cape Town",
        "role": "Qualifier",
        "priority": "prep",
        "focus": "First marathon back — run/walk to finish. Doubles as the Comrades qualifier.",
    },
    {
        "name": "TM35 · Ultra-Trail Cape Town",
        "date": "2026-11-21",
        "distance_km": 35,
        "surface": "trail",
        "location": "Table Mountain NP",
        "role": "Prep",
        "priority": "prep",
        "focus": "Steep, technical descents — ideal down-run conditioning for the quads.",
    },
    {
        "name": "Comrades Marathon",
        "date": "2027-06-13",
        "distance_km": 89,
        "surface": "road",
        "location": "PMB → Durban",
        "role": "Goal",
        "priority": "A",
        "note": "100th · Down run",
        "focus": "The 100th running — a down run into Durban. The whole season builds to this.",
    },
    {
        # Tentative — date, distance and event all still open. Kept on the board so it
        # stays in view while it firms up. date=None ⇒ no countdown, sorts last.
        "name": "Ultra in Europe",
        "date": None,
        "distance_km": None,
        "surface": None,
        "location": "Europe",
        "role": "Maybe",
        "priority": "tentative",
        "focus": "A possible overseas ultra — event, distance and date still to be decided.",
    },
]


def with_countdown(today: date) -> list[dict]:
    """Goal races annotated with days remaining, soonest first; undated (tentative)
    races carry days_to=None and sort to the end."""
    out = []
    for r in GOAL_RACES:
        days = (date.fromisoformat(r["date"]) - today).days if r.get("date") else None
        out.append({**r, "days_to": days})
    out.sort(key=lambda r: (r.get("date") is None, r.get("date") or ""))
    return out
