"""Training-science computations from Garmin activity data.

All inputs are the *curated* activity dicts from the Garmin MCP server. The load
model is Banister TRIMP (from duration + average HR), which lets us derive the
whole training-load picture from a single get_activities_by_date call.
"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any, Iterable

# ---- athlete constants (override via env) --------------------------------
HR_REST = int(os.environ.get("GARMIN_COACH_HR_REST", "48"))


def hr_max(runs: list[dict]) -> int:
    env = os.environ.get("GARMIN_COACH_HR_MAX")
    if env:
        return int(env)
    observed = [r.get("max_hr_bpm") for r in runs if r.get("max_hr_bpm")]
    return int(max(observed)) if observed else 190


# ---- basic helpers --------------------------------------------------------
def _run_date(r: dict) -> date | None:
    s = r.get("start_time") or r.get("start_time_local")
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "")).date()
    except ValueError:
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def trimp(avg_hr: float, duration_s: float, hrmax: int) -> float:
    if not avg_hr or not duration_s:
        return 0.0
    hrr = (avg_hr - HR_REST) / max(1, (hrmax - HR_REST))
    hrr = max(0.0, min(1.0, hrr))
    minutes = duration_s / 60.0
    return round(minutes * hrr * 0.64 * math.exp(1.92 * hrr), 1)


def run_load(r: dict, hrmax: int) -> float:
    return trimp(r.get("avg_hr_bpm", 0), r.get("duration_seconds", 0), hrmax)


def daily_load(runs: list[dict], hrmax: int) -> dict[date, float]:
    out: dict[date, float] = defaultdict(float)
    for r in runs:
        d = _run_date(r)
        if d:
            out[d] += run_load(r, hrmax)
    return out


# ---- CTL / ATL / TSB (fitness / fatigue / form) ---------------------------
def load_series(runs: list[dict], hrmax: int, today: date, days: int = 90) -> list[dict]:
    """Per-day EWMA training load: CTL (42d), ATL (7d), TSB (CTL-ATL)."""
    loads = daily_load(runs, hrmax)
    if not loads:
        return []
    first = min(loads)
    # don't show empty time before the first run (matters for the All range)
    start = max(today - timedelta(days=days), first)
    ctl = atl = 0.0
    k_ctl, k_atl = 2 / (42 + 1), 2 / (7 + 1)
    # warm up from earliest data so the visible window isn't cold-started
    d = min(first, start)
    series: list[dict] = []
    while d <= today:
        todays = loads.get(d, 0.0)
        ctl = ctl + k_ctl * (todays - ctl)
        atl = atl + k_atl * (todays - atl)
        if d >= start:
            series.append({
                "date": d.isoformat(),
                "load": round(todays, 1),
                "ctl": round(ctl, 1),   # fitness
                "atl": round(atl, 1),   # fatigue
                "tsb": round(ctl - atl, 1),  # form / freshness
            })
        d += timedelta(days=1)
    return series


def acwr(runs: list[dict], hrmax: int, today: date) -> dict:
    """Acute:Chronic Workload Ratio. Sweet spot ~0.8-1.3; >1.5 = injury risk."""
    loads = daily_load(runs, hrmax)
    acute = sum(v for d, v in loads.items() if today - timedelta(days=7) < d <= today)
    chronic28 = sum(v for d, v in loads.items() if today - timedelta(days=28) < d <= today)
    chronic = chronic28 / 4.0
    ratio = (acute / chronic) if chronic > 0 else 0.0
    if ratio == 0:
        zone = "no-data"
    elif ratio < 0.8:
        zone = "detraining"
    elif ratio <= 1.3:
        zone = "optimal"
    elif ratio <= 1.5:
        zone = "caution"
    else:
        zone = "high-risk"
    return {"acute": round(acute, 1), "chronic": round(chronic, 1),
            "ratio": round(ratio, 2), "zone": zone}


# ---- volume & efficiency --------------------------------------------------
def weekly_mileage(runs: list[dict], today: date, weeks: int = 12) -> list[dict]:
    out: dict[date, dict] = {}
    monday_today = today - timedelta(days=today.weekday())
    for w in range(weeks):
        wk = monday_today - timedelta(weeks=w)
        out[wk] = {"week_start": wk.isoformat(), "km": 0.0, "runs": 0, "load": 0.0}
    hrmax = hr_max(runs)
    for r in runs:
        d = _run_date(r)
        if not d:
            continue
        wk = d - timedelta(days=d.weekday())
        if wk in out:
            out[wk]["km"] += (r.get("distance_meters") or 0) / 1000
            out[wk]["runs"] += 1
            out[wk]["load"] += run_load(r, hrmax)
    rows = sorted(out.values(), key=lambda x: x["week_start"])
    for x in rows:
        x["km"] = round(x["km"], 1)
        x["load"] = round(x["load"], 1)
    return rows


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def volume_series(runs: list[dict], today: date, days: int) -> dict:
    """Distance buckets for the volume chart. Weekly for <=~30wk ranges,
    monthly beyond that so long histories stay readable."""
    monthly = days > 210
    buckets: dict[date, dict] = {}
    for r in runs:
        d = _run_date(r)
        if not d:
            continue
        if monthly:
            key = date(d.year, d.month, 1)
            short = f"{_MONTHS[d.month - 1]} {str(d.year)[2:]}"
            label = f"{_MONTHS[d.month - 1]} {d.year}"
        else:
            key = d - timedelta(days=d.weekday())
            short = f"{key.day} {_MONTHS[key.month - 1]}"
            label = f"Week of {key.isoformat()}"
        b = buckets.setdefault(key, {"km": 0.0, "runs": 0, "short": short, "label": label})
        b["km"] += (r.get("distance_meters") or 0) / 1000
        b["runs"] += 1
    rows = [{"key": k.isoformat(), "label": v["label"], "short": v["short"],
             "km": round(v["km"], 1), "runs": v["runs"]}
            for k, v in sorted(buckets.items())]
    return {"granularity": "month" if monthly else "week", "rows": rows}


def aerobic_efficiency(runs: list[dict], today: date) -> list[dict]:
    """Easy-run efficiency: speed per beat (m/s per bpm). Rising = aerobic base up."""
    out = []
    hrmax = hr_max(runs)
    for r in runs:
        d = _run_date(r)
        dur = r.get("duration_seconds") or 0
        dist = r.get("distance_meters") or 0
        hr = r.get("avg_hr_bpm") or 0
        if not (d and dur and dist and hr):
            continue
        # only aerobic/easy efforts (< 82% HRmax) to compare like-for-like
        if hr > hrmax * 0.82:
            continue
        speed = dist / dur
        out.append({"date": d.isoformat(), "efficiency": round(speed / hr * 1000, 3),
                    "pace_s_per_km": round(dur / (dist / 1000), 0)})
    out.sort(key=lambda x: x["date"])
    return out


# ---- HR zones -------------------------------------------------------------
_ZONE_EDGES = [0.60, 0.70, 0.80, 0.90]  # fractions of HRmax => Z1..Z5


def hr_fraction_to_zone(frac: float) -> int:
    for i, edge in enumerate(_ZONE_EDGES):
        if frac < edge:
            return i + 1
    return 5


def approx_weekly_zones(runs: list[dict], hrmax: int) -> dict:
    """Approximate time-in-zone across all runs from each run's average HR.

    Cheap (no per-activity calls). Good enough for polarization at a glance.
    """
    secs = [0.0] * 5
    for r in runs:
        hr = r.get("avg_hr_bpm")
        dur = r.get("duration_seconds")
        if not (hr and dur):
            continue
        z = hr_fraction_to_zone(hr / hrmax)
        secs[z - 1] += dur
    total = sum(secs) or 1
    zones = [{"zone": i + 1, "seconds": round(s), "pct": round(100 * s / total, 1)}
             for i, s in enumerate(secs)]
    easy = zones[0]["pct"] + zones[1]["pct"]
    hard = zones[3]["pct"] + zones[4]["pct"]
    return {"zones": zones, "easy_pct": round(easy, 1), "hard_pct": round(hard, 1),
            "polarized": easy >= 75 and hard >= 10}


def run_zone_distribution(hr_zones: Any) -> dict | None:
    """Parse get_activity_hr_in_timezones output into a clean distribution."""
    if not isinstance(hr_zones, list) or not hr_zones:
        return None
    zones = []
    total = 0.0
    for z in hr_zones:
        secs = float(z.get("secsInZone") or 0)
        total += secs
        zones.append({"zone": int(z.get("zoneNumber") or 0), "seconds": round(secs),
                      "low_bpm": z.get("zoneLowBoundary")})
    for z in zones:
        z["pct"] = round(100 * z["seconds"] / total, 1) if total else 0.0
    return {"zones": zones, "total_seconds": round(total)}


# ---- aerobic decoupling (from splits) -------------------------------------
def decoupling(splits: Any) -> dict | None:
    """Pa:HR aerobic decoupling — compares speed/HR in the 1st vs 2nd half.

    >5% suggests aerobic fatigue / under-fuelling / heat. Standard endurance metric.
    """
    laps = _laps(splits)
    valid = [(l.get("avg_speed_mps"), l.get("avg_hr_bpm"), l.get("duration_seconds"))
             for l in laps]
    valid = [(s, h, d) for s, h, d in valid if s and h and d]
    if len(valid) < 4:
        return None
    half = len(valid) // 2
    first, second = valid[:half], valid[half:]

    def ratio(group):
        # duration-weighted mean of speed/HR
        num = sum((s / h) * d for s, h, d in group)
        den = sum(d for _, _, d in group)
        return num / den if den else 0.0

    r1, r2 = ratio(first), ratio(second)
    if not r1:
        return None
    pct = (r1 - r2) / r1 * 100
    return {"first_half_ratio": round(r1, 5), "second_half_ratio": round(r2, 5),
            "decoupling_pct": round(pct, 1),
            "verdict": "aerobically coupled" if pct < 5 else "decoupled (fatigue/heat/fuel)"}


def _laps(splits: Any) -> list[dict]:
    if isinstance(splits, dict):
        return splits.get("laps", []) or []
    if isinstance(splits, list):
        return splits
    return []


# ---- race prediction from your own runs (Daniels VDOT) --------------------
# Jack Daniels' running-formula model: derive a VDOT (effective VO2max) from any
# distance+time, then read equivalent race times off the same VDOT. Far more grounded
# in *your* performances than a device VO2max estimate.
_RACE_DISTANCES = {"5K": 5000.0, "10K": 10000.0, "half_marathon": 21097.5, "marathon": 42195.0}


def _vdot(dist_m: float, time_s: float) -> float | None:
    if not dist_m or not time_s:
        return None
    t = time_s / 60.0                       # minutes
    v = dist_m / t                          # metres per minute
    pct = 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    return vo2 / pct if pct > 0 else None


def _time_for(dist_m: float, vdot: float) -> int:
    """Invert VDOT: the time at `dist_m` that yields this VDOT (bisection)."""
    lo, hi = 30.0, 6 * 3600.0               # seconds
    for _ in range(60):
        mid = (lo + hi) / 2
        val = _vdot(dist_m, mid)
        if val is None:
            break
        if val > vdot:                      # too fast (VDOT too high) → need more time
            lo = mid
        else:
            hi = mid
    return int(round((lo + hi) / 2))


def _fmt_hms(secs: float) -> str:
    secs = int(round(secs))
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_RIEGEL_EXP = 1.06   # Peter Riegel endurance/fatigue exponent


def _riegel(t_s: float, d_from: float, d_to: float) -> float:
    return t_s * (d_to / d_from) ** _RIEGEL_EXP


def predict_races(runs: list[dict], today: date, window_days: int = 120) -> dict | None:
    """Predict each race distance from your best real effort *at a similar distance*.

    Anchoring on nearby efforts (Riegel-adjusted) avoids the trap of stretching one
    short run across a huge multiplier — a fast 6 km shouldn't dictate your 10K if
    you've actually run 10 km. Distances longer than anything you've run fall back to
    a VDOT extrapolation from your best effort and are flagged `extrapolated`.
    """
    cutoff = today - timedelta(days=window_days)
    efforts = []
    for r in runs:
        d = _run_date(r)
        dist = r.get("distance_meters") or 0
        dur = r.get("duration_seconds") or 0
        if not d or d < cutoff or dist < 2400 or dur < 600:   # sustained efforts only
            continue
        efforts.append({"date": d.isoformat(), "name": r.get("name") or "Run",
                        "distance_m": dist, "duration_s": dur,
                        "is_race": r.get("event_type") == "race"})
    if not efforts:
        return None

    best = max(efforts, key=lambda e: _vdot(e["distance_m"], e["duration_s"]) or 0)
    best_vdot = _vdot(best["distance_m"], best["duration_s"]) or 0

    preds, anchors = {}, {}
    for k, dm in _RACE_DISTANCES.items():
        near = [e for e in efforts if e["distance_m"] >= 0.85 * dm]   # actually ran ~this far
        if near:
            secs, e = min(((_riegel(e["duration_s"], e["distance_m"], dm), e) for e in near),
                          key=lambda x: x[0])
            preds[k] = {"time_seconds": int(round(secs)), "time": _fmt_hms(secs), "extrapolated": False}
            anchors[k] = e
        else:
            secs = _time_for(dm, best_vdot)
            preds[k] = {"time_seconds": secs, "time": _fmt_hms(secs), "extrapolated": True}
            anchors[k] = best

    longest = max(efforts, key=lambda e: e["distance_m"])
    return {"vdot": round(best_vdot, 1),
            "source": {**best, "vdot": round(best_vdot, 1)},
            "longest_km": round(longest["distance_m"] / 1000, 1),
            "predictions": preds}


def _predict_from_vdot(vdot: float) -> dict:
    return {k: {"time_seconds": (t := _time_for(dm, vdot)), "time": _fmt_hms(t)}
            for k, dm in _RACE_DISTANCES.items()}


def predict_races_threshold(runs: list[dict], today: date, hrmax: int,
                            window_days: int = 120) -> dict | None:
    """Predict race times from your pace-vs-HR profile at threshold HR.

    Fits speed = a + b·HR across all qualifying runs and reads off the speed you'd
    hold at threshold (~88% HRmax). That threshold effort (~1-hour pace) anchors a
    VDOT — so easy runs still inform the estimate via their aerobic signal, unlike
    the best-effort model which needs an actual hard run.
    """
    cutoff = today - timedelta(days=window_days)
    pts = []
    for r in runs:
        d = _run_date(r)
        dist = r.get("distance_meters") or 0
        dur = r.get("duration_seconds") or 0
        hr = r.get("avg_hr_bpm") or 0
        if not d or d < cutoff or dist < 2400 or dur < 600 or not hr:
            continue
        pts.append((float(hr), dist / dur))          # (HR, speed m/s)
    if len(pts) < 4:
        return None
    n = len(pts)
    sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
    sxx = sum(p[0] ** 2 for p in pts); sxy = sum(p[0] * p[1] for p in pts)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    if b <= 0:
        return None                                  # speed must rise with HR to be meaningful
    thr_hr = round(0.88 * hrmax)
    thr_speed = a + b * thr_hr
    if thr_speed <= 0:
        return None
    vdot = _vdot(thr_speed * 3600, 3600)             # threshold ≈ a 1-hour effort
    if vdot is None:
        return None
    return {"vdot": round(vdot, 1), "threshold_hr": thr_hr, "n": n,
            "threshold_pace_s_per_km": round(1000 / thr_speed),
            "predictions": _predict_from_vdot(vdot)}


# ---- sleep & recovery -----------------------------------------------------
def sleep_norm(s: Any) -> dict | None:
    """Normalize a get_sleep_summary dict into the fields the dashboard uses."""
    if not isinstance(s, dict):
        return None
    secs = s.get("sleep_seconds") or 0
    if not secs:
        return None
    qual = s.get("sleep_score_qualifier")
    return {
        "hours": round(secs / 3600, 1),
        "score": s.get("sleep_score"),
        "qualifier": qual.replace("_", " ").title() if isinstance(qual, str) else None,
        "deep_s": int(s.get("deep_sleep_seconds") or 0),
        "light_s": int(s.get("light_sleep_seconds") or 0),
        "rem_s": int(s.get("rem_sleep_seconds") or 0),
        "awake_s": int(s.get("awake_seconds") or 0),
        "overnight_hrv": s.get("avg_overnight_hrv"),
    }


def weather_norm(w: Any) -> dict | None:
    """Normalize get_activity_weather. Garmin returns temps in °F despite the field
    name ('..._celsius'), so convert to °C for this metric account."""
    if not isinstance(w, dict):
        return None

    def f2c(v):
        return round((v - 32) * 5 / 9) if isinstance(v, (int, float)) else None

    t = f2c(w.get("temperature_celsius"))
    if t is None:
        return None
    return {
        "temp_c": t,
        "feels_c": f2c(w.get("apparent_temperature_celsius")),
        "humidity": w.get("humidity_percent"),
    }


def body_battery_norm(bb: Any) -> list[dict]:
    """Normalize get_body_battery's list into per-day charged/drained/level rows."""
    if not isinstance(bb, list):
        return []
    rows = []
    for d in bb:
        if not isinstance(d, dict) or not d.get("date"):
            continue
        rows.append({
            "date": d.get("date"),
            "charged": d.get("charged"),
            "drained": d.get("drained"),
            "level": d.get("body_battery_level"),
        })
    rows.sort(key=lambda x: x["date"])
    return rows


# ---- misc -----------------------------------------------------------------
def summarize_runs(runs: list[dict]) -> dict:
    total_km = sum((r.get("distance_meters") or 0) for r in runs) / 1000
    total_s = sum((r.get("duration_seconds") or 0) for r in runs)
    return {"count": len(runs), "total_km": round(total_km, 1),
            "total_hours": round(total_s / 3600, 1)}


def yearly_history(runs: list[dict]) -> list[dict]:
    """Per-calendar-year running totals: run count, distance, and that year's single
    longest run. Ordered oldest year first. Used by the History view, which spans the
    athlete's whole Garmin record rather than a rolling window."""
    buckets: dict[int, dict] = {}
    for r in runs:
        d = _run_date(r)
        if not d:
            continue
        meters = r.get("distance_meters") or 0
        if meters < 500:   # drop GPS-glitch fragments (accidental starts, lost signal)
            continue
        km = meters / 1000
        b = buckets.setdefault(d.year, {"year": d.year, "runs": 0, "km": 0.0, "longest": None})
        b["runs"] += 1
        b["km"] += km
        lg = b["longest"]
        if lg is None or km > lg["km"]:
            b["longest"] = {"km": round(km, 1), "date": d.isoformat(), "name": r.get("name") or "Run"}
    out = sorted(buckets.values(), key=lambda x: x["year"])
    for b in out:
        b["km"] = round(b["km"], 1)
    return out


def period_summary(runs: list[dict], hrmax: int) -> dict:
    """Window-local block summary for period-vs-period comparison.

    Only quantities computable from the window's own runs (no EMA warm-up), so a
    previous block can be summarised in isolation and diffed against the current one.
    """
    total_km = sum((r.get("distance_meters") or 0) for r in runs) / 1000
    total_s = sum((r.get("duration_seconds") or 0) for r in runs)
    load = sum(run_load(r, hrmax) for r in runs)
    zones = approx_weekly_zones(runs, hrmax)
    avg_pace = round(total_s / total_km) if total_km else None  # seconds per km
    return {
        "runs": len(runs),
        "km": round(total_km, 1),
        "hours": round(total_s / 3600, 1),
        "load": round(load, 1),
        "avg_pace_s_per_km": avg_pace,
        "easy_pct": zones["easy_pct"],
        "hard_pct": zones["hard_pct"],
    }
