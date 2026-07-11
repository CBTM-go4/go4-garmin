"""Unit tests for server/metrics.py — the training-science math.

These pin the numeric behaviour (TRIMP → CTL/ATL/TSB, ACWR, VDOT/Riegel, HR zones,
decoupling) so the formulas can't silently drift. Golden values were computed from
the functions themselves; the invariant tests (window-independent CTL, VDOT round-trip,
ACWR ratios) are the ones that would have caught the cold-start bug we just fixed.

Assumes the default athlete constant HR_REST=48 (guarded below).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from server import metrics as m
from conftest import daily_runs, run

TODAY = date(2026, 7, 11)


def test_default_hr_rest():
    # The TRIMP golden values below assume the default resting HR.
    assert m.HR_REST == 48, "set GARMIN_COACH_HR_REST unset for these golden values"


# ---- TRIMP ---------------------------------------------------------------
def test_trimp_golden():
    assert m.trimp(150, 3600, 190) == 109.5


def test_trimp_zero_when_hr_at_or_below_rest():
    assert m.trimp(48, 3600, 190) == 0.0
    assert m.trimp(40, 3600, 190) == 0.0  # HRR clamps to 0, never negative


def test_trimp_clamps_hr_above_max():
    # HRR saturates at 1.0 — a spuriously high HR can't produce runaway load.
    assert m.trimp(250, 600, 190) == 43.7
    assert m.trimp(190, 600, 190) == m.trimp(250, 600, 190)


def test_trimp_zero_without_hr_or_duration():
    assert m.trimp(0, 3600, 190) == 0.0
    assert m.trimp(150, 0, 190) == 0.0


# ---- daily_load ----------------------------------------------------------
def test_daily_load_sums_same_day_runs():
    d = date(2026, 6, 1)
    runs = [run(d, minutes=30, avg_hr=150), run(d, minutes=30, avg_hr=150)]
    loads = m.daily_load(runs, 190)
    assert set(loads) == {d}
    assert loads[d] == pytest.approx(2 * m.trimp(150, 1800, 190))


# ---- CTL / ATL / TSB -----------------------------------------------------
def test_load_series_converges_to_constant_load():
    # A constant daily load L drives both EMAs to L, so form (TSB) settles at ~0.
    runs = daily_runs(TODAY, 200, minutes=60, avg_hr=150)
    series = m.load_series(runs, 190, TODAY, days=90)
    last = series[-1]
    L = m.trimp(150, 3600, 190)
    assert last["ctl"] == pytest.approx(L, abs=0.5)
    assert last["atl"] == pytest.approx(L, abs=0.5)
    assert last["tsb"] == pytest.approx(0.0, abs=0.5)


def test_load_series_window_independent_ctl():
    """CTL on a given day must not depend on how far back the window starts.

    This is the regression guard for the cold-start bug: as long as the run history
    is present, a 30-day and a 90-day view must agree on every shared date.
    """
    runs = daily_runs(TODAY, 200, minutes=60, avg_hr=150)
    s30 = {p["date"]: p for p in m.load_series(runs, 190, TODAY, days=30)}
    s90 = {p["date"]: p for p in m.load_series(runs, 190, TODAY, days=90)}
    shared = set(s30) & set(s90)
    assert len(shared) >= 30
    for d in shared:
        assert s30[d]["ctl"] == pytest.approx(s90[d]["ctl"], abs=0.05)
        assert s30[d]["atl"] == pytest.approx(s90[d]["atl"], abs=0.05)


def test_load_series_atl_leads_ctl_after_a_spike():
    # One big day on top of nothing: fatigue (7d) rises faster than fitness (42d).
    runs = [run(TODAY, minutes=120, avg_hr=170)]
    last = m.load_series(runs, 190, TODAY, days=30)[-1]
    assert last["atl"] > last["ctl"] > 0
    assert last["tsb"] < 0


def test_load_series_empty():
    assert m.load_series([], 190, TODAY, days=90) == []


# ---- ACWR ----------------------------------------------------------------
def test_acwr_balanced_on_constant_load():
    # 7-day acute vs 28-day/4 chronic are equal under steady training → ratio 1.0.
    runs = daily_runs(TODAY, 40, minutes=60, avg_hr=150)
    a = m.acwr(runs, 190, TODAY)
    assert a["ratio"] == pytest.approx(1.0, abs=0.01)
    assert a["zone"] == "optimal"


def test_acwr_spike_is_high_risk():
    # A hard week with no chronic base → acute:chronic 4.0.
    runs = daily_runs(TODAY, 7, minutes=60, avg_hr=150)
    a = m.acwr(runs, 190, TODAY)
    assert a["ratio"] == pytest.approx(4.0, abs=0.01)
    assert a["zone"] == "high-risk"


def test_acwr_no_data():
    a = m.acwr([], 190, TODAY)
    assert a["ratio"] == 0.0
    assert a["zone"] == "no-data"


# ---- HR zones ------------------------------------------------------------
@pytest.mark.parametrize("frac,zone", [
    (0.50, 1), (0.59, 1), (0.60, 2), (0.69, 2), (0.70, 3),
    (0.80, 4), (0.89, 4), (0.90, 5), (1.10, 5),
])
def test_hr_fraction_to_zone_boundaries(frac, zone):
    assert m.hr_fraction_to_zone(frac) == zone


def test_approx_weekly_zones_split():
    # Two easy runs (Z2/Z3) and one hard (Z5); check easy/hard accounting.
    runs = [
        run(TODAY, minutes=60, avg_hr=125, max_hr=190),  # 0.66 -> Z2
        run(TODAY, minutes=60, avg_hr=150, max_hr=190),  # 0.79 -> Z3
        run(TODAY, minutes=20, avg_hr=180, max_hr=190),  # 0.95 -> Z5
    ]
    z = m.approx_weekly_zones(runs, 190)
    secs = {row["zone"]: row["seconds"] for row in z["zones"]}
    assert secs[2] == 3600 and secs[3] == 3600 and secs[5] == 1200
    assert z["easy_pct"] == pytest.approx(100 * 3600 / 8400, abs=0.1)
    assert z["hard_pct"] == pytest.approx(100 * 1200 / 8400, abs=0.1)


# ---- Riegel / VDOT -------------------------------------------------------
def test_riegel_doubling_distance():
    assert m._riegel(1800, 5000, 10000) == pytest.approx(1800 * 2 ** 1.06)


def test_vdot_time_round_trip():
    # _time_for inverts _vdot: recovering the time from a distance+VDOT is stable.
    for dist, t in [(5000, 1200), (10000, 2500), (21097.5, 5400)]:
        v = m._vdot(dist, t)
        assert m._time_for(dist, v) == pytest.approx(t, abs=2)


def test_predict_races_anchors_on_actual_and_extrapolates_longer():
    runs = [run(TODAY - timedelta(days=3), km=5.0, minutes=20, avg_hr=175,
                name="parkrun", event_type="race")]
    p = m.predict_races(runs, TODAY)
    assert p is not None
    assert p["vdot"] == pytest.approx(49.8, abs=0.2)
    # 5K is anchored on the real 20:00 effort, not extrapolated.
    assert p["predictions"]["5K"]["extrapolated"] is False
    assert p["predictions"]["5K"]["time_seconds"] == pytest.approx(1200, abs=3)
    # Nothing run near 10K/half/marathon → flagged extrapolated and slower.
    assert p["predictions"]["10K"]["extrapolated"] is True
    assert p["predictions"]["10K"]["time_seconds"] > 2 * 1200


def test_predict_races_none_without_qualifying_efforts():
    # Too short / too brief to be a sustained effort.
    assert m.predict_races([run(TODAY, km=1.0, minutes=5)], TODAY) is None
    assert m.predict_races([], TODAY) is None


# ---- decoupling ----------------------------------------------------------
def _laps(pairs):
    return {"laps": [{"avg_speed_mps": s, "avg_hr_bpm": h, "duration_seconds": 600}
                     for s, h in pairs]}


def test_decoupling_positive_when_second_half_slower():
    d = m.decoupling(_laps([(3.0, 150), (3.0, 150), (2.7, 150), (2.7, 150)]))
    assert d["decoupling_pct"] == pytest.approx(10.0, abs=0.1)
    assert "decoupled" in d["verdict"]


def test_decoupling_zero_for_steady_effort():
    d = m.decoupling(_laps([(3.0, 150)] * 4))
    assert d["decoupling_pct"] == pytest.approx(0.0, abs=0.01)
    assert d["verdict"] == "aerobically coupled"


def test_decoupling_needs_four_laps():
    assert m.decoupling(_laps([(3.0, 150)] * 3)) is None
    assert m.decoupling(None) is None


# ---- normalisers ---------------------------------------------------------
def test_weather_fahrenheit_to_celsius():
    assert m.weather_norm({"temperature_celsius": 50})["temp_c"] == 10
    assert m.weather_norm({"temperature_celsius": 32})["temp_c"] == 0
    assert m.weather_norm({}) is None


def test_sleep_norm():
    s = m.sleep_norm({"sleep_seconds": 27000, "sleep_score": 82,
                      "sleep_score_qualifier": "very_good", "deep_sleep_seconds": 3600})
    assert s["hours"] == 7.5
    assert s["score"] == 82
    assert s["qualifier"] == "Very Good"
    assert m.sleep_norm({"sleep_seconds": 0}) is None
    assert m.sleep_norm(None) is None


def test_body_battery_norm_sorts_by_date():
    rows = m.body_battery_norm([
        {"date": "2026-07-02", "charged": 60, "drained": 40, "body_battery_level": 70},
        {"date": "2026-07-01", "charged": 55, "drained": 45, "body_battery_level": 65},
    ])
    assert [r["date"] for r in rows] == ["2026-07-01", "2026-07-02"]
    assert rows[0]["charged"] == 55
    assert m.body_battery_norm(None) == []


def test_period_summary():
    runs = [run(TODAY, km=10, minutes=50, avg_hr=150, max_hr=190),
            run(TODAY - timedelta(days=1), km=5, minutes=30, avg_hr=140, max_hr=190)]
    p = m.period_summary(runs, 190)
    assert p["runs"] == 2
    assert p["km"] == 15.0
    assert p["hours"] == 1.3  # 80 min, rounded to 1 dp
    assert p["avg_pace_s_per_km"] == round((50 + 30) * 60 / 15)  # total_s / total_km
    assert p["load"] == pytest.approx(sum(m.run_load(r, 190) for r in runs), abs=0.1)
    assert 0 <= p["easy_pct"] <= 100 and 0 <= p["hard_pct"] <= 100


def test_period_summary_empty():
    p = m.period_summary([], 190)
    assert p == {"runs": 0, "km": 0.0, "hours": 0.0, "load": 0.0,
                 "avg_pace_s_per_km": None, "easy_pct": 0.0, "hard_pct": 0.0}


def test_summarize_runs():
    runs = [run(TODAY, km=10, minutes=60), run(TODAY, km=5, minutes=30)]
    s = m.summarize_runs(runs)
    assert s["count"] == 2
    assert s["total_km"] == 15.0
    assert s["total_hours"] == 1.5


# ---- hr_max --------------------------------------------------------------
def test_hr_max_prefers_env(monkeypatch):
    monkeypatch.setenv("GARMIN_COACH_HR_MAX", "201")
    assert m.hr_max([run(TODAY, max_hr=180)]) == 201


def test_hr_max_from_observed(monkeypatch):
    monkeypatch.delenv("GARMIN_COACH_HR_MAX", raising=False)
    assert m.hr_max([run(TODAY, max_hr=185), run(TODAY, max_hr=192)]) == 192


def test_hr_max_default(monkeypatch):
    monkeypatch.delenv("GARMIN_COACH_HR_MAX", raising=False)
    assert m.hr_max([run(TODAY, max_hr=None)]) == 190
