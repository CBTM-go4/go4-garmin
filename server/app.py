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

from . import cache, coach, demo, history, metrics
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


@app.get("/api/status")
async def status():
    g: GarminData = app.state.gd
    return {
        "available": g.available,
        "demo": is_demo(),
        "error": g.status_error,
        "tools": g.mcp.tool_names if g.mcp else [],
    }


@app.get("/api/overview")
async def overview(days: int = 90, start: str | None = None, end: str | None = None):
    g: GarminData = app.state.gd
    if not g.available:
        return JSONResponse({"available": False, "demo": is_demo(),
                             "error": g.status_error}, status_code=200)
    real_today = date.today()
    # A custom [start, end] range overrides the `days` preset. Everything downstream
    # is expressed as (anchor, days-back-from-anchor), so we anchor on `end` and derive
    # the span. Without an explicit start we keep the original days-from-today behaviour.
    try:
        if start:
            start_d = date.fromisoformat(start)
            anchor = date.fromisoformat(end) if end else real_today
            if anchor < start_d:
                start_d, anchor = anchor, start_d
            days = max((anchor - start_d).days, 1)
        else:
            anchor = real_today
            start_d = anchor - timedelta(days=days)
    except ValueError:
        return JSONResponse({"available": True, "demo": is_demo(),
                             "error": "Invalid start/end date."}, status_code=400)
    # Only snapshot today's fitness into history when viewing the live window; a
    # historical range must not write past-dated points.
    is_current = anchor >= real_today
    today = anchor  # local alias: the rest of this handler works off the window end
    runs = await g.runs_between(start_d, today)
    hrmax = metrics.hr_max(runs)

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
    fitness_trend = _fitness_trend(app, g, today, days, race, record=is_current)

    # Model race times from the athlete's own runs over a stable window (not the
    # selected chart range), so predictions don't vanish on a 7-day view.
    pred_runs = runs if days >= 120 else await g.runs_between(today - timedelta(days=120), today)
    my_predictions = metrics.predict_races(pred_runs, today)
    potential_predictions = metrics.predict_races_threshold(pred_runs, today, hrmax)

    ov: dict[str, Any] = {
        "available": True,
        "demo": is_demo(),
        "generated_for": today.isoformat(),
        "hr_max": hrmax,
        "hr_rest": metrics.HR_REST,
        "runs": runs,
        "summary": metrics.summarize_runs(runs),
        "load_series": metrics.load_series(runs, hrmax, today, days=days),
        "acwr": metrics.acwr(runs, hrmax, today),
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


def _fitness_trend(app: FastAPI, g: GarminData, today: date, days: int, race: Any,
                   record: bool = True) -> dict:
    """Accumulated VO2max + race-prediction trajectory (see history.py).

    In demo mode we synthesize it; with real data we snapshot today, read the stored
    series, and kick a one-time background backfill of historical VO2max.
    """
    if is_demo():
        return demo.fitness_trend(today, days)
    if record:
        try:
            history.record_today(today, None, race if isinstance(race, dict) else None)
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
    return {"ok": True}


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
