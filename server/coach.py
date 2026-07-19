"""Rule-based running coach.

Turns the computed metrics into plain-language insights and a next-session
recommendation. Deliberately transparent (no LLM needed) so advice is
explainable; the same data is also exposed to Claude via the MCP for
free-form conversational coaching.
"""
from __future__ import annotations

from typing import Any


def build(ov: dict[str, Any]) -> dict:
    insights: list[dict] = []
    acwr = ov.get("acwr") or {}
    series = ov.get("load_series") or []
    zones = ov.get("zones") or {}
    weekly = ov.get("weekly") or []
    ts = ov.get("training_status") or {}
    readiness = ov.get("readiness") or {}
    eff = ov.get("efficiency") or []

    tsb = series[-1]["tsb"] if series else None
    ctl = series[-1]["ctl"] if series else None
    rec_state = _recovery_state(ov)

    # ---- Injury risk: ACWR ------------------------------------------------
    zone = acwr.get("zone")
    if zone == "high-risk":
        insights.append(_i("warning", "Training load spike",
            f"Your acute:chronic workload ratio is {acwr.get('ratio')} (high-risk zone). "
            "Acute load has outrun what your body is adapted to — the range most "
            "associated with injury. Hold volume flat or drop ~20% for a week."))
    elif zone == "caution":
        insights.append(_i("caution", "Load climbing fast",
            f"ACWR is {acwr.get('ratio')} — slightly hot. Fine short-term, but don't "
            "stack another big week on top. Keep the next week steady."))
    elif zone == "optimal":
        insights.append(_i("good", "Load well balanced",
            f"ACWR {acwr.get('ratio')} sits in the sweet spot (0.8–1.3). You're "
            "absorbing training and have room to progress gradually."))
    elif zone == "detraining":
        insights.append(_i("info", "Room to build",
            f"ACWR {acwr.get('ratio')} is below 0.8 — you're fresh and slightly "
            "undertrained. A controlled build (~10%/week) is safe here."))

    # ---- Form / freshness: TSB -------------------------------------------
    if tsb is not None:
        if tsb < -20:
            insights.append(_i("caution", "Deep fatigue",
                f"Form (TSB) is {tsb} — you're carrying heavy fatigue. Productive for "
                "adaptation, but schedule a recovery day or two soon and protect sleep."))
        elif tsb > 8:
            insights.append(_i("good", "Fresh and sharp",
                f"Form (TSB) is +{tsb}. You're rested — a good window for a hard "
                "quality session, a time trial, or a race."))
        else:
            insights.append(_i("info", "Productive training zone",
                f"Form (TSB) is {tsb} — the mild-fatigue band where fitness is built. "
                "Keep the easy/hard balance and let it ride."))

    # ---- Recovery: sleep + HRV + body battery ----------------------------
    if rec_state["level"] == "red":
        insights.append(_i("warning", "Recovery is compromised",
            f"Recovery signals are down: {rec_state['summary']}. Prioritise an easy day or "
            "rest — hard training on a depleted system mostly adds fatigue, not fitness."))
    elif rec_state["level"] == "amber":
        insights.append(_i("caution", "Recovery is mixed",
            f"Some recovery signals are soft: {rec_state['summary']}. Fine to train, but "
            "favour easy volume over intensity today and re-check tomorrow."))
    elif rec_state["signals"]:
        insights.append(_i("good", "Recovery looks solid",
            f"Sleep, HRV and Body Battery are all in a good place ({rec_state['signals']}). "
            "Your body is primed to absorb a harder session if the plan calls for one."))

    # ---- Intensity distribution (time in zone, not run-level avg HR) ------
    # Keyed "intensity" so the frontend can swap in the *measured* zone split once
    # per-second HR data loads (this build uses the fast avg-HR estimate).
    ez, hz = zones.get("easy_pct"), zones.get("hard_pct")
    z3 = next((z["pct"] for z in (zones.get("zones") or []) if z.get("zone") == 3), 0)
    if ez is not None and hz is not None and len(ov.get("runs") or []) >= 5:
        if ez >= 75 and hz >= 10:
            insights.append(_i("good", "Well-polarized training",
                f"About {ez}% of your running time is easy and {hz}% hard — the ~80/20 "
                "shape that builds aerobic fitness with low injury cost.", key="intensity"))
        elif z3 >= 35:
            insights.append(_i("caution", "Too much “grey zone”",
                f"Around {z3}% of your running time sits in Zone 3 — moderate effort that's "
                "too hard to build your aerobic base yet too easy to count as a real workout. "
                "Slow your easy runs down into Z2; aim for roughly 80% easy.", key="intensity"))
        elif hz >= 30:
            insights.append(_i("caution", "Too many hard efforts",
                f"About {hz}% of your running time is hard (Z4–5) vs a target near 20%. Make "
                "easy days genuinely easy so quality sessions land on fresh legs.", key="intensity"))
        else:
            insights.append(_i("info", "Intensity balance",
                f"About {ez}% of your time is easy, {hz}% hard. Aim for roughly 80% easy.",
                key="intensity"))

    # ---- Weekly ramp (10% rule) ------------------------------------------
    complete = [w for w in weekly if w["km"] > 0]
    if len(complete) >= 2:
        this_wk, last_wk = complete[-1], complete[-2]
        if last_wk["km"] > 0 and this_wk["km"] > last_wk["km"] * 1.12:
            jump = round((this_wk["km"] / last_wk["km"] - 1) * 100)
            insights.append(_i("caution", "Mileage jumped",
                f"This week is {this_wk['km']} km vs {last_wk['km']} km last week "
                f"(+{jump}%). Above ~10% week-on-week raises injury risk — consider "
                "capping the jump."))

    # ---- VO2max ----------------------------------------------------------
    vo2 = ts.get("vo2_max")
    if vo2:
        insights.append(_i("info", "Aerobic capacity",
            f"Current VO₂max estimate is {vo2}. Track the trend, not the day-to-day "
            "number — consistent easy volume plus weekly quality nudges it up."))

    # ---- Aerobic efficiency trend ----------------------------------------
    if len(eff) >= 6:
        first = sum(e["efficiency"] for e in eff[:3]) / 3
        last = sum(e["efficiency"] for e in eff[-3:]) / 3
        if last > first * 1.02:
            insights.append(_i("good", "Aerobic base improving",
                "At the same heart rate you're running measurably faster than a few "
                "weeks ago — your aerobic engine is getting more efficient."))

    recommendation = _recommend(tsb, acwr.get("zone"), readiness, ov, rec_state)
    headline = _headline(tsb, acwr.get("zone"), ctl, rec_state)
    return {"headline": headline, "recommendation": recommendation, "insights": insights}


def _recovery_state(ov: dict[str, Any]) -> dict:
    """Fuse HRV, last night's sleep and body battery into a green/amber/red verdict.

    Each signal contributes a flag with severity 2 (strong) or 1 (mild). Any strong
    flag → red; any mild flag → amber; otherwise green. `signals` is a short readout
    of the good values (only populated when we actually have recovery data).
    """
    hrv = ov.get("hrv") or {}
    sleep = ov.get("sleep_last") or {}
    bb_list = ov.get("body_battery") or []
    bb = bb_list[-1] if bb_list else {}

    flags: list[tuple[int, str]] = []
    signals: list[str] = []

    # HRV — status vs baseline
    status = (hrv.get("status") or "").upper()
    ln, lo = hrv.get("last_night_avg"), hrv.get("baseline_low")
    if status and status not in ("BALANCED", "GOOD"):
        flags.append((2 if status in ("UNBALANCED", "LOW", "POOR") else 1, f"HRV {status.lower()}"))
    elif ln and lo and ln < lo:
        flags.append((1, "HRV below baseline"))
    if ln:
        signals.append(f"HRV {ln} ms")

    # Sleep — last night's score (fall back to duration)
    sc, hrs = sleep.get("score"), sleep.get("hours")
    if sc is not None:
        if sc < 50:
            flags.append((2, f"poor sleep ({sc})"))
        elif sc < 65:
            flags.append((1, f"fair sleep ({sc})"))
        signals.append(f"sleep {sc}")
    elif hrs is not None and hrs < 6:
        flags.append((1, f"short sleep ({hrs} h)"))

    # Body battery — latest level / net change
    lvl = (bb.get("level") or "").upper()
    net = None
    if bb.get("charged") is not None and bb.get("drained") is not None:
        net = bb["charged"] - bb["drained"]
    if lvl == "LOW":
        flags.append((2, "body battery low"))
    elif net is not None and net <= -15:
        flags.append((1, "body battery draining"))
    if lvl:
        signals.append(f"battery {lvl.lower()}")

    strong = any(sev == 2 for sev, _ in flags)
    mild = any(sev == 1 for sev, _ in flags)
    level = "red" if strong else "amber" if mild else "green"
    summary = ", ".join(t for _, t in sorted(flags, key=lambda x: -x[0]))
    return {"level": level, "summary": summary, "signals": ", ".join(signals)}


def _recommend(tsb, acwr_zone, readiness, ov, rec_state=None) -> dict:
    rec_state = rec_state or {"level": "green", "summary": "", "signals": ""}
    level = rec_state["level"]
    score = (readiness or {}).get("score")
    runs = ov.get("runs") or []
    last_quality = _days_since_quality(runs)

    # 1. Hard load / fatigue guardrail — highest priority.
    if acwr_zone == "high-risk" or (tsb is not None and tsb < -25):
        return _rec("Recovery / rest", "Easy 30–40 min in Z1–2, or take the day off. "
                    "You've earned recovery and it's where adaptation happens.")

    # 2. Recovery is compromised — override even if form looks fresh.
    if level == "red":
        return _rec("Easy / recovery day", f"Recovery signals are down ({rec_state['summary']}). "
                    "Keep it short and conversational in Z1–2, or rest — training a depleted "
                    "system adds fatigue without much adaptation.")

    # Legacy readiness score (kept for devices that report it).
    if score is not None and score < 40:
        return _rec("Easy day", f"Readiness is low ({score}/100). Keep it conversational "
                    "and short; don't chase pace today.")

    # 3. Fresh — but temper the quality push if recovery is mixed.
    if tsb is not None and tsb > 8:
        if level == "amber":
            return _rec("Quality — but listen to your body", "Form says you're fresh, yet "
                        f"recovery is mixed ({rec_state['summary']}). Start the session and "
                        "back off if it feels flat — a moderate tempo beats max intervals today.")
        return _rec("Quality session", "You're fresh — make it count: intervals "
                    "(e.g. 5×1000 m @ 5K effort) or a tempo of 20–30 min at threshold.")

    # 4. Been cruising easy for a while — add a stimulus, unless recovery is soft.
    if last_quality is not None and last_quality >= 3 and level != "amber":
        return _rec("Add some quality", "It's been a few easy days — a controlled tempo "
                    "or short intervals will provide a stimulus without deep fatigue.")
    return _rec("Easy aerobic run", "Steady conversational miles in Z2. Build the base "
                "that everything else is stacked on.")


def _headline(tsb, acwr_zone, ctl, rec_state=None) -> str:
    level = (rec_state or {}).get("level")
    if acwr_zone == "high-risk":
        return "Back off — load has spiked into the injury-risk zone."
    if level == "red":
        return "Recovery is down — keep today easy."
    if tsb is not None and tsb > 8:
        recovered = level == "green" and (rec_state or {}).get("signals")
        return ("Fresh and well-recovered — ready for a hard effort." if recovered
                else "You're fresh and ready for a hard effort.")
    if tsb is not None and tsb < -20:
        return "Fatigue is high — prioritize recovery this week."
    if ctl:
        return "Training is progressing — stay consistent."
    return "Let's build a consistent base."


def _days_since_quality(runs: list[dict]) -> int | None:
    from datetime import date, datetime
    best = None
    for r in runs:
        rpe = r.get("workout_rpe")
        hr = r.get("avg_hr_bpm") or 0
        mx = r.get("max_hr_bpm") or 0
        hard = (rpe and rpe >= 7) or (mx and hr > mx * 0.9) or (r.get("event_type") == "race")
        if not hard:
            continue
        s = r.get("start_time") or r.get("start_time_local")
        try:
            d = datetime.fromisoformat(str(s).replace("Z", "")).date()
        except (ValueError, TypeError):
            continue
        age = (date.today() - d).days
        best = age if best is None else min(best, age)
    return best


def _i(severity: str, title: str, text: str, key: str | None = None) -> dict:
    d = {"severity": severity, "title": title, "text": text}
    if key:
        d["key"] = key
    return d


def _rec(kind: str, detail: str) -> dict:
    return {"session": kind, "detail": detail}
