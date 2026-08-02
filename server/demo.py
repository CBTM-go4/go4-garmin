"""Deterministic synthetic Garmin data for demo / offline development.

Enabled with GARMIN_COACH_DEMO=1. Returns the exact curated shapes the Garmin
MCP server produces, so the whole app (metrics, coach, dashboard) runs end-to-end
without Garmin credentials.
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

_TODAY = date.today()
_WEEKS = 14
_HR_MAX = 188
_HR_REST = 46

# (weekday-plan) name, type, km, rel_intensity(avgHR fraction of max), is_quality
_PLAN = {
    1: ("Interval session", "running", 9.0, 0.86, True),    # Tue
    2: ("Easy run", "running", 8.0, 0.70, False),           # Wed
    3: ("Tempo run", "running", 11.0, 0.82, True),          # Thu
    5: ("Long run", "running", 19.0, 0.74, False),          # Sat
    6: ("Recovery run", "running", 6.0, 0.66, False),       # Sun
}


@lru_cache(maxsize=1)
def _runs() -> list[dict]:
    """Generate a stable list of runs over the last _WEEKS weeks."""
    rng = random.Random(42)
    runs: list[dict] = []
    start = _TODAY - timedelta(weeks=_WEEKS)
    vo2_base = 49.5
    d = start
    while d <= _TODAY:
        plan = _PLAN.get(d.weekday())
        if plan and rng.random() > 0.12:  # ~88% adherence
            name, atype, km, intensity, quality = plan
            km = round(km * rng.uniform(0.9, 1.12), 1)
            # progressive fitness: pace improves slightly over the block
            weeks_in = (d - start).days / 7
            fitness = 1 + 0.012 * weeks_in
            avg_hr = int(_HR_MAX * intensity * rng.uniform(0.98, 1.02))
            # base pace (s/km) by intensity, improving with fitness
            base_pace = {0.66: 372, 0.70: 340, 0.74: 322, 0.82: 285, 0.86: 258}
            pace = min(base_pace.items(), key=lambda kv: abs(kv[0] - intensity))[1]
            pace = pace / fitness * rng.uniform(0.98, 1.02)
            dur = int(km * pace)
            max_hr = min(_HR_MAX, avg_hr + rng.randint(6, 16))
            elev = int(km * rng.uniform(4, 12))
            # heat-driven decoupling on long/hot runs
            decouple = 0.03 + (0.05 if km > 16 else 0.0) + rng.uniform(0, 0.03)
            runs.append({
                "id": int(datetime.combine(d, datetime.min.time()).timestamp()),
                "date": d,
                "name": name,
                "type": atype,
                "quality": quality,
                "km": km,
                "duration": dur,
                "avg_hr": avg_hr,
                "max_hr": max_hr,
                "pace": pace,
                "elev": elev,
                "decouple": decouple,
                "cadence": rng.randint(168, 182),
                "rpe": rng.randint(2, 5) + (2 if quality else 0),
                "vo2": round(vo2_base + 0.09 * weeks_in, 1),
            })
        d += timedelta(days=1)
    # Insert a 10K race 24 days ago
    race_day = _TODAY - timedelta(days=24)
    runs.append({
        "id": int(datetime.combine(race_day, datetime.min.time()).timestamp()) + 7,
        "date": race_day, "name": "City 10K Race", "type": "running", "event_type": "race",
        "quality": True, "km": 10.0, "duration": 2418, "avg_hr": 179, "max_hr": 190,
        "pace": 241.8, "elev": 42, "decouple": 0.06, "cadence": 186, "rpe": 9,
        "vo2": 52.0,
    })
    runs.sort(key=lambda r: r["date"])
    return runs


def _by_id(aid: int) -> dict | None:
    for r in _runs():
        if r["id"] == int(aid):
            return r
    return None


def _activity_list_item(r: dict) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "type": r["type"],
        "event_type": r.get("event_type", "training"),
        "start_time": datetime.combine(r["date"], datetime.min.time()).replace(hour=7).isoformat(sep=" "),
        "distance_meters": round(r["km"] * 1000, 1),
        "duration_seconds": float(r["duration"]),
        "calories": int(r["km"] * 65),
        "avg_hr_bpm": r["avg_hr"],
        "max_hr_bpm": r["max_hr"],
        "steps": int(r["km"] * 1000 / 1.15),
        "elevation_gain_meters": float(r["elev"]),
        "elevation_loss_meters": float(r["elev"]),
    }


def call(tool: str, args: dict[str, Any]) -> Any:
    if tool == "get_activities_by_date":
        s = date.fromisoformat(args["start_date"])
        e = date.fromisoformat(args["end_date"])
        acts = [_activity_list_item(r) for r in _runs() if s <= r["date"] <= e]
        acts.sort(key=lambda a: a["start_time"], reverse=True)
        return {"count": len(acts), "date_range": {"start": args["start_date"], "end": args["end_date"]},
                "activities": acts}

    if tool == "get_activity":
        r = _by_id(args["activity_id"])
        if not r:
            return None
        return {
            "id": r["id"], "name": r["name"], "type": r["type"],
            "event_type": r.get("event_type", "training"),
            "start_time_local": datetime.combine(r["date"], datetime.min.time()).replace(hour=7).isoformat(sep=" "),
            "duration_seconds": float(r["duration"]),
            "distance_meters": round(r["km"] * 1000, 1),
            "avg_speed_mps": round(r["km"] * 1000 / r["duration"], 3),
            "avg_hr_bpm": r["avg_hr"], "max_hr_bpm": r["max_hr"],
            "avg_cadence": r["cadence"], "calories": int(r["km"] * 65),
            "elevation_gain_meters": float(r["elev"]),
            "training_effect": round(min(5.0, 2.0 + r["rpe"] * 0.35), 1),
            "training_load": round(_trimp(r), 1),
            "workout_rpe": r["rpe"], "workout_feel": 50 + (r["rpe"] - 5) * 5,
        }

    if tool == "get_activity_splits":
        r = _by_id(args["activity_id"])
        if not r:
            return None
        return {"activity_id": r["id"], "lap_count": max(1, int(r["km"])),
                "laps": _splits(r)}

    if tool == "get_activity_hr_in_timezones":
        r = _by_id(args["activity_id"])
        if not r:
            return None
        return _hr_zones(r)

    if tool == "get_activity_weather":
        r = _by_id(args["activity_id"])
        if not r:
            return None
        # Match garmin_mcp's current shape: 'temperature' in the account's display
        # unit, tagged by 'temperature_unit'. Warmer on the hotter/higher-decoupling
        # long runs.
        rng = random.Random(r["id"])
        c = 9 + rng.randint(0, 12) + (8 if r["km"] > 16 else 0)
        return {"activity_id": r["id"], "temperature": c, "temperature_unit": "C",
                "apparent_temperature": c + rng.randint(-1, 2),
                "humidity_percent": rng.randint(40, 80), "wind_speed": rng.randint(0, 18),
                "wind_speed_unit": "km/h"}

    if tool == "get_activity_fit_data":
        r = _by_id(args["activity_id"])
        if not r:
            return None
        # Mirror the real payload's session.temperature_stats. Morning runs heat up
        # under you, so the long ones span the widest range — the same driver as
        # r["decouple"], which is how the demo's hot long runs drift.
        rng = random.Random(r["id"] + 1)
        start_c = 9 + rng.randint(0, 8)
        rise = round(2 + r["duration"] / 1500 + (5 if r["km"] > 16 else 0))
        speed = 1000 / r["pace"]                       # m/s
        pw_cool = round(speed * 62, 1)                 # rough running power
        return {
            "activity_id": r["id"],
            "session": {
                "sport": r["type"],
                "total_distance_m": round(r["km"] * 1000, 1),
                "avg_heart_rate_bpm": r["avg_hr"],
                "temperature_stats": {
                    "avg_temp_c": round(start_c + rise / 2, 1),
                    "min_temp_c": start_c,
                    "max_temp_c": start_c + rise,
                    "temp_range_c": rise,
                    "avg_hr_coolest_third_bpm": round(r["avg_hr"] * 0.97, 1),
                    "avg_hr_hottest_third_bpm": round(r["avg_hr"] * (0.97 + r["decouple"] * 1.4), 1),
                    "avg_power_coolest_third_w": pw_cool,
                    "avg_power_hottest_third_w": round(pw_cool * 0.98, 1),
                },
            },
            "laps": [],
        }

    if tool == "get_training_status":
        d = date.fromisoformat(args["date"])
        return _training_status(d)

    if tool == "get_training_readiness":
        d = date.fromisoformat(args["date"])
        return _readiness(d)

    if tool == "get_hrv_data":
        d = date.fromisoformat(args["date"])
        return _hrv(d)

    if tool == "get_sleep_data":
        return {"date": args["date"], "sleep_seconds": 7 * 3600 + 1200, "sleep_score": 78}

    if tool == "get_sleep_summary":
        return _sleep_summary(date.fromisoformat(args["date"]))

    if tool == "get_body_battery":
        return _body_battery(date.fromisoformat(args["start_date"]),
                             date.fromisoformat(args["end_date"]))

    if tool == "get_race_predictions":
        return _race_predictions()
    return None


def _trimp(r: dict) -> float:
    """Banister TRIMP training load from duration + avg HR."""
    hrr = (r["avg_hr"] - _HR_REST) / (_HR_MAX - _HR_REST)
    hrr = max(0.0, min(1.0, hrr))
    minutes = r["duration"] / 60
    return minutes * hrr * 0.64 * math.exp(1.92 * hrr)


def _splits(r: dict) -> list[dict]:
    laps = max(1, int(round(r["km"])))
    out = []
    for i in range(laps):
        frac = i / max(1, laps - 1)
        # HR drifts up over the run (decoupling), pace roughly steady
        hr = int(r["avg_hr"] * (1 - r["decouple"] / 2 + r["decouple"] * frac))
        pace = r["pace"] * (1 + 0.01 * math.sin(i))
        out.append({
            "lap_number": i + 1,
            "distance_meters": 1000.0,
            "duration_seconds": round(pace, 1),
            "avg_speed_mps": round(1000 / pace, 3),
            "avg_hr_bpm": hr,
            "max_hr_bpm": min(r["max_hr"], hr + 6),
            "avg_cadence": r["cadence"],
            "elevation_gain_meters": round(r["elev"] / laps, 1),
        })
    return out


def _hr_zones(r: dict) -> list[dict]:
    dur = r["duration"]
    # distribution by run character
    if r["avg_hr"] >= _HR_MAX * 0.85:
        dist = [0.05, 0.15, 0.20, 0.35, 0.25]
    elif r["avg_hr"] >= _HR_MAX * 0.80:
        dist = [0.05, 0.20, 0.45, 0.25, 0.05]
    elif r["avg_hr"] >= _HR_MAX * 0.72:
        dist = [0.10, 0.55, 0.28, 0.06, 0.01]
    else:
        dist = [0.25, 0.60, 0.13, 0.02, 0.0]
    bounds = [0, 113, 132, 151, 170]
    return [{"zoneNumber": i + 1, "secsInZone": round(dur * f, 1), "zoneLowBoundary": bounds[i]}
            for i, f in enumerate(dist)]


def _training_status(d: date) -> dict:
    recent = [r for r in _runs() if d - timedelta(days=42) <= r["date"] <= d]
    vo2 = recent[-1]["vo2"] if recent else 50.0
    atl = _ewma([r for r in recent], d, 7)
    ctl = _ewma([r for r in recent], d, 42)
    return {
        "date": d.isoformat(),
        "vo2_max": vo2,
        "training_status": "productive" if atl >= ctl else "maintaining",
        "acute_training_load": round(atl * 7, 1),
        "load_balance": {"acute": round(atl * 7, 1), "chronic": round(ctl * 7, 1)},
        "recovery_time_hours": 18 if atl > ctl else 8,
    }


def _ewma(runs: list[dict], ref: date, tau: int) -> float:
    total = 0.0
    for r in runs:
        age = (ref - r["date"]).days
        if age < 0:
            continue
        total += _trimp(r) * math.exp(-age / tau)
    return total / tau


def _readiness(d: date) -> dict:
    rng = random.Random(d.toordinal())
    score = 55 + rng.randint(-15, 30)
    return {"date": d.isoformat(), "score": max(1, min(100, score)),
            "level": "READY" if score > 65 else "MODERATE" if score > 40 else "LOW"}


def _hrv(d: date) -> dict:
    rng = random.Random(d.toordinal() * 3)
    return {"date": d.isoformat(), "last_night_avg": 58 + rng.randint(-10, 12),
            "status": "BALANCED", "weekly_avg": 60,
            "baseline_balanced_low_ms": 52, "baseline_balanced_upper_ms": 70}


def _sleep_summary(d: date) -> dict:
    rng = random.Random(d.toordinal() * 7)
    hrs = 6.6 + rng.uniform(0, 1.5)
    secs = int(hrs * 3600)
    deep = int(secs * rng.uniform(0.12, 0.20))
    rem = int(secs * rng.uniform(0.18, 0.26))
    awake = int(secs * rng.uniform(0, 0.03))
    light = secs - deep - rem - awake
    score = max(45, min(96, int(58 + hrs * 4 + rng.randint(-7, 7))))
    qual = ("EXCELLENT" if score >= 90 else "GOOD" if score >= 80
            else "FAIR" if score >= 60 else "POOR")
    return {
        "sleep_seconds": secs, "sleep_score": score, "sleep_score_qualifier": qual,
        "deep_sleep_seconds": deep, "light_sleep_seconds": light,
        "rem_sleep_seconds": rem, "awake_seconds": awake,
        "avg_overnight_hrv": 55 + rng.randint(-8, 12), "sleep_hours": round(hrs, 1),
    }


def _body_battery(start: date, end: date) -> list[dict]:
    out = []
    d = start
    while d <= end:
        rng = random.Random(d.toordinal() * 11)
        charged = rng.randint(45, 70)
        drained = rng.randint(40, 72)
        net = charged - drained
        lvl = "HIGH" if net > 0 else "MODERATE" if net > -12 else "LOW"
        out.append({"date": d.isoformat(), "charged": charged, "drained": drained,
                    "body_battery_level": lvl, "events": []})
        d += timedelta(days=1)
    return out


def fitness_trend(today: date, days: int) -> dict:
    """Synthetic VO2max + race-prediction trajectory for demo mode.

    Mirrors the shape server.history.series() returns: sparse weekly VO2max points
    and weekly prediction snapshots, both improving gently over the block.
    """
    start = today - timedelta(days=days)
    base = _race_predictions()["predictions"]
    vo2, preds = [], []
    weeks = max(1, days // 7)
    for w in range(weeks + 1):
        d = today - timedelta(weeks=(weeks - w))
        if d < start or d > today:
            continue
        # fitness ramps ~ +1.4% per week end-to-end; VO2max 49.5 -> ~51.5
        prog = w / max(1, weeks)
        vo2.append({"date": d.isoformat(), "value": round(49.6 + 2.0 * prog, 1)})
        # predictions get faster over time (times shrink), so scale up early times
        scale = 1 + 0.05 * (1 - prog)
        preds.append({
            "date": d.isoformat(),
            "p5k": int(base["5K"]["time_seconds"] * scale),
            "p10k": int(base["10K"]["time_seconds"] * scale),
            "phm": int(base["half_marathon"]["time_seconds"] * scale),
            "pm": int(base["marathon"]["time_seconds"] * scale),
        })
    return {"vo2max": vo2, "predictions": preds, "backfilling": False}


def _race_predictions() -> dict:
    return {"prediction_date": _TODAY.isoformat(), "predictions": {
        "5K": {"time": "24:10", "time_seconds": 1450},
        "10K": {"time": "50:15", "time_seconds": 3015},
        "half_marathon": {"time": "1:52:30", "time_seconds": 6750},
        "marathon": {"time": "3:58:40", "time_seconds": 14320},
    }}
