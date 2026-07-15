"""FastAPI backend for Garmin Coach.

Serves a JSON API (consumed by the static dashboard) and the dashboard itself.
All Garmin data flows through the MCP client in garmin.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import cache, coach, demo, history, metrics, store
from .garmin import GarminData, is_demo
from .mcp_client import GarminMCP

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("garmin_coach")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp = None
    if not is_demo():
        mcp = GarminMCP()
        try:
            await mcp.start()
        except Exception as e:  # noqa: BLE001
            log.warning("MCP failed to start: %s", e)
    app.state.gd = GarminData(mcp)
    if is_demo():
        log.info("Running in DEMO mode (synthetic data)")
    elif mcp:
        # Populate/refresh the local activity store in the background so the first-ever
        # load backfills without blocking startup; subsequent reads are served locally.
        asyncio.create_task(_sync_store(app))
    yield
    if mcp:
        await mcp.stop()


app = FastAPI(title="Garmin Coach", lifespan=lifespan)


def _norm_hrv(h: Any) -> dict:
    """Garmin's HRV fields vary (real: *_hrv_ms; demo: short names). Normalize."""
    if not isinstance(h, dict):
        return {}
    return {
        "last_night_avg": h.get("last_night_avg") or h.get("last_night_avg_hrv_ms"),
        "weekly_avg": h.get("weekly_avg") or h.get("weekly_avg_hrv_ms"),
        "baseline_low": h.get("baseline_balanced_low_ms"),
        "baseline_high": h.get("baseline_balanced_upper_ms"),
        "status": h.get("status"),
    }


async def _daily_with_fallback(fn, today: date, back: int = 2):
    """Garmin often lacks 'today' until a sync; walk back a couple days."""
    for i in range(back + 1):
        d = today - timedelta(days=i)
        try:
            data = await fn(d)
        except Exception:  # noqa: BLE001
            data = None
        if data:
            return data
    return None


async def _sync_store(app: FastAPI) -> None:
    """Background wrapper around the activity sync; logs the outcome, swallows errors so a
    Garmin hiccup never takes the server down (reads fall back to live fetches)."""
    try:
        n = await app.state.gd.sync_activities()
        log.info("Activity store synced: %d activities written, %d total", n, store.count())
    except Exception as e:  # noqa: BLE001
        log.warning("Activity store sync failed: %s", e)


@app.get("/api/status")
async def status():
    g: GarminData = app.state.gd
    return {
        "available": g.available,
        "demo": is_demo(),
        "error": g.status_error,
        "tools": g.mcp.tool_names if g.mcp else [],
        "activities_stored": store.count() if not is_demo() else 0,
    }


# Days of training history to fetch *before* a window's start so the CTL (42d) and
# ATL (7d) EMAs are warmed up rather than cold-started. ~120d decays the zero-seed
# influence on CTL to <1%, so the fitness on the window's first day is accurate.
CTL_WARMUP_DAYS = 120


def _resolve_window(days: int, start: str | None, end: str | None) -> tuple[date, date, int]:
    """Resolve (start_date, anchor, span_days) from either a days preset or an
    explicit [start, end] range. A custom range overrides `days`; without one we keep
    the days-from-today behaviour. Raises ValueError on unparseable dates."""
    real_today = date.today()
    if start:
        start_d = date.fromisoformat(start)
        anchor = date.fromisoformat(end) if end else real_today
        if anchor < start_d:
            start_d, anchor = anchor, start_d
        days = max((anchor - start_d).days, 1)
    else:
        anchor = real_today
        start_d = anchor - timedelta(days=days)
    return start_d, anchor, days


@app.get("/api/overview")
async def overview(days: int = 90, start: str | None = None, end: str | None = None):
    g: GarminData = app.state.gd
    if not g.available:
        return JSONResponse({"available": False, "demo": is_demo(),
                             "error": g.status_error}, status_code=200)
    real_today = date.today()
    try:
        start_d, anchor, days = _resolve_window(days, start, end)
    except ValueError:
        return JSONResponse({"available": True, "demo": is_demo(),
                             "error": "Invalid start/end date."}, status_code=400)
    # Only snapshot today's fitness into history when viewing the live window; a
    # historical range must not write past-dated points.
    is_current = anchor >= real_today
    today = anchor  # local alias: the rest of this handler works off the window end
    # CTL/ATL are exponential moving averages (42d / 7d) and ACWR needs a 28-day
    # chronic base, so the fitness/fatigue on any given day depends on training
    # BEFORE the visible window. Fetch a warm-up lead-in and compute load off the
    # full history — otherwise the EMA cold-starts at the window's left edge and the
    # same date reads differently depending on how far back the range begins.
    hist_runs = await g.runs_between(start_d - timedelta(days=CTL_WARMUP_DAYS), today)
    runs = [r for r in hist_runs if (rd := metrics._run_date(r)) and rd >= start_d]
    hrmax = metrics.hr_max(hist_runs)

    ts = await _daily_with_fallback(g.training_status, today)
    ts = ts if isinstance(ts, dict) else {}
    hrv = await _daily_with_fallback(g.hrv, today)

    # VO2max isn't exposed by the MCP and its training_status can be null — backfill
    # it from Garmin's max-metrics (reusing the token) if missing.
    if not ts.get("vo2_max"):
        vo2 = await g.vo2max_recent(today)
        if vo2 and vo2.get("vo2_max"):
            ts = {**ts, "vo2_max": vo2["vo2_max"]}

    # Training Readiness is device-dependent and absent on this account — omitted.

    recovery = await _recovery(g, today)
    race = await g.race_predictions()

    # Model race times from the athlete's own runs over a stable window (not the
    # selected chart range), so predictions don't vanish on a 7-day view.
    pred_runs = runs if days >= 120 else await g.runs_between(today - timedelta(days=120), today)
    my_predictions = metrics.predict_races(pred_runs, today)
    potential_predictions = metrics.predict_races_threshold(pred_runs, today, hrmax)

    # The fitness trend tracks the *realistic* (your-runs) prediction, not Garmin's
    # optimistic number — so the trajectory matches the Race Predictions table.
    fitness_trend = _fitness_trend(app, g, today, days, my_predictions, record=is_current)

    ov: dict[str, Any] = {
        "available": True,
        "demo": is_demo(),
        "generated_for": today.isoformat(),
        "hr_max": hrmax,
        "hr_rest": metrics.HR_REST,
        "runs": runs,
        "summary": metrics.summarize_runs(runs),
        "load_series": metrics.load_series(hist_runs, hrmax, today, days=days),
        "acwr": metrics.acwr(hist_runs, hrmax, today),
        "weekly": metrics.weekly_mileage(runs, today),   # recent 12wk, for coach ramp check
        "volume": metrics.volume_series(runs, today, days),  # adaptive, for the chart
        "zones": metrics.approx_weekly_zones(runs, hrmax),
        "efficiency": metrics.aerobic_efficiency(runs, today),
        "training_status": ts,
        "readiness": {},
        "hrv": _norm_hrv(hrv),
        "sleep_last": recovery["sleep_last"],
        "sleep_trend": recovery["sleep_trend"],
        "body_battery": recovery["body_battery"],
        "race_predictions": race if isinstance(race, dict) else None,
        "my_predictions": my_predictions,
        "potential_predictions": potential_predictions,
        "fitness_trend": fitness_trend,
    }
    ov["coach"] = coach.build(ov)
    return ov


# Garmin Connect launched in 2007; no account predates this. A fixed floor lets one
# range query sweep the athlete's entire record — the pagination in runs_between walks
# however many activities exist, and only the years with runs come back.
HISTORY_START = date(2007, 1, 1)


@app.get("/api/history")
async def history_years():
    """Per-year running totals across the athlete's whole Garmin history."""
    g: GarminData = app.state.gd
    if not g.available:
        return JSONResponse({"available": False, "demo": is_demo(),
                             "error": g.status_error}, status_code=200)
    # Prior calendar years are immutable — fetch them once and cache hard. Only the
    # current year is refetched at the normal cadence, so the History view is fast
    # (only the first-ever load pays the full-sweep cost).
    today = date.today()
    past = await g.runs_between(HISTORY_START, date(today.year - 1, 12, 31), stable=True)
    current = await g.runs_between(date(today.year, 1, 1), today)
    years = metrics.yearly_history(past + current)
    return {
        "available": True,
        "demo": is_demo(),
        "years": years,
        "totals": {
            "years": len(years),
            "runs": sum(y["runs"] for y in years),
            "km": round(sum(y["km"] for y in years), 1),
        },
    }


@app.get("/api/compare")
async def compare(days: int = 90, start: str | None = None, end: str | None = None):
    """Block-vs-block summary: the selected window against the equal-length window
    immediately before it. Window-local metrics only (no EMA warm-up needed)."""
    g: GarminData = app.state.gd
    if not g.available:
        return JSONResponse({"available": False, "demo": is_demo(),
                             "error": g.status_error}, status_code=200)
    try:
        start_d, anchor, span = _resolve_window(days, start, end)
    except ValueError:
        return JSONResponse({"available": True, "demo": is_demo(),
                             "error": "Invalid start/end date."}, status_code=400)
    # Previous block: same length, ending the day before the current one starts.
    prev_end = start_d - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span)
    cur_runs = await g.runs_between(start_d, anchor)
    prev_runs = await g.runs_between(prev_start, prev_end)
    hrmax = metrics.hr_max(cur_runs + prev_runs)
    return {
        "available": True,
        "demo": is_demo(),
        "span_days": span,
        "current": {"start": start_d.isoformat(), "end": anchor.isoformat(),
                    **metrics.period_summary(cur_runs, hrmax)},
        "previous": {"start": prev_start.isoformat(), "end": prev_end.isoformat(),
                     **metrics.period_summary(prev_runs, hrmax)},
    }


def _fitness_trend(app: FastAPI, g: GarminData, today: date, days: int, predictions: Any,
                   record: bool = True) -> dict:
    """Accumulated VO2max + race-prediction trajectory (see history.py).

    In demo mode we synthesize it; with real data we snapshot today, read the stored
    series, and kick a one-time background backfill of historical VO2max.
    """
    if is_demo():
        return demo.fitness_trend(today, days)
    if record:
        try:
            history.record_today(today, None, predictions if isinstance(predictions, dict) else None)
        except Exception:  # noqa: BLE001
            pass
    missing = history.missing_vo2_dates(today, min(days, 120))
    if missing and not getattr(app.state, "backfilling", False):
        app.state.backfilling = True
        asyncio.create_task(_backfill_vo2(app, g, missing))
    trend = history.series(today, days)
    # Only advertise "still building" for a real backlog — not the routine 1–3 recent
    # rechecks, which shouldn't make the UI show a populating banner.
    trend["backfilling"] = len(missing) > 5
    return trend


async def _backfill_vo2(app: FastAPI, g: GarminData, dates: list[date]) -> None:
    """Fetch VO2max for each missing date, newest first, gently (rate-limit friendly)."""
    try:
        for d in sorted(dates, reverse=True):
            try:
                history.store_vo2(d, await g.vo2max_on(d))
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.25)
    finally:
        app.state.backfilling = False


async def _recovery(g: GarminData, today: date, window: int = 14) -> dict:
    """Sleep trend (last `window` days) + body-battery series for the recovery panels."""
    dates = [today - timedelta(days=i) for i in range(window)]
    raw = await asyncio.gather(*[g.sleep_summary(d) for d in dates])
    trend = []
    for d, s in zip(dates, raw):
        n = metrics.sleep_norm(s)
        if n:
            trend.append({"date": d.isoformat(), **n})
    trend.sort(key=lambda x: x["date"])

    bb = await g.body_battery(today - timedelta(days=window), today)
    return {
        "sleep_last": trend[-1] if trend else None,
        "sleep_trend": trend,
        "body_battery": metrics.body_battery_norm(bb),
    }


@app.get("/api/run/{activity_id}")
async def run_detail(activity_id: str):
    g: GarminData = app.state.gd
    if not g.available:
        raise HTTPException(503, "Garmin data unavailable")
    activity = await g.activity(activity_id)
    if not activity:
        raise HTTPException(404, f"No activity {activity_id}")
    splits = await g.activity_splits(activity_id)
    hr_zones = await g.activity_hr_zones(activity_id)
    weather = metrics.weather_norm(await g.activity_weather(activity_id))
    return {
        "activity": activity,
        "splits": metrics._laps(splits),
        "decoupling": metrics.decoupling(splits),
        "zone_distribution": metrics.run_zone_distribution(hr_zones),
        "weather": weather,
    }


@app.get("/api/run/{activity_id}/summary")
async def run_summary(activity_id: str):
    """Lightweight per-run extras for the runs table: aerobic decoupling + temperature.

    Kept separate from /api/run/{id} so the table can fill these in lazily. Both splits
    and weather are cached hard, so repeat views and range switches are instant.
    """
    g: GarminData = app.state.gd
    if not g.available:
        raise HTTPException(503, "Garmin data unavailable")
    dc = metrics.decoupling(await g.activity_splits(activity_id))
    w = metrics.weather_norm(await g.activity_weather(activity_id))
    return {
        "id": activity_id,
        "decoupling_pct": dc["decoupling_pct"] if dc else None,
        "temp_c": w["temp_c"] if w else None,
    }


@app.post("/api/refresh")
async def refresh():
    cache.clear()
    # Pull any activities recorded (or edited) since the last sync, so Refresh surfaces
    # new runs — not just re-warms the cleared response cache.
    written = 0
    try:
        written = await app.state.gd.sync_activities()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "synced": written}


# Static dashboard (mounted last so /api/* wins).
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn
    host = os.environ.get("GARMIN_COACH_HOST", "127.0.0.1")
    port = int(os.environ.get("GARMIN_COACH_PORT", "8765"))
    log.info("Garmin Coach on http://%s:%s  (demo=%s)", host, port, is_demo())
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
