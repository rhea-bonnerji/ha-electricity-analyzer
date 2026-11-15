"""The learning pieces: weather fit, drivers, ensemble, bill, hall correction, minutes, durability."""

from __future__ import annotations

import math
import random
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.electricity_analyzer.engine import bill, durability, ensemble as ens, learner
from custom_components.electricity_analyzer.engine import weather as wx
from custom_components.electricity_analyzer.engine.forecast import Forecaster
from custom_components.electricity_analyzer.engine.hallfix import HallCorrection, hour_factor, phase_factor
from custom_components.electricity_analyzer.engine.minutes import MinuteBuffer
from custom_components.electricity_analyzer.engine.store import Store
from custom_components.electricity_analyzer.engine.tariff import DEFAULT_TARIFF, TariffSchedule

UTC = timezone.utc
TODAY = date(2026, 10, 7)


def _world(days: int = 400, seed: int = 7):
    """A house that uses 18 kWh + 1.5 kWh per degree day, +2 on Mondays, -0.8 per 100 W/m² sun."""
    rng = random.Random(seed)
    kwh, weather = {}, {}
    for i in range(days, 0, -1):
        d = TODAY - timedelta(days=i)
        doy = d.timetuple().tm_yday
        temp = 10 - 6 * math.cos(2 * math.pi * (doy - 15) / 365) + rng.gauss(0, 2)
        hdd = max(0.0, 15.5 - temp)
        sun = max(0.0, 120 + 100 * math.sin(2 * math.pi * (doy - 80) / 365) + rng.gauss(0, 30))
        w = learner.DayWeather(hdd=hdd, hdd_app=hdd + rng.uniform(0, 1.5), rad=sun, wind=rng.uniform(5, 30), rain=rng.uniform(0, 8))
        weather[d] = w
        kwh[d] = 18 + 1.5 * hdd + (2.0 if d.weekday() == 0 else 0.0) - 0.8 * sun / 100 + rng.gauss(0, 0.8)
    return kwh, weather


def test_drivers_recover_the_true_weights():
    kwh, weather = _world()
    reg = learner.fit(kwh, weather, today=TODAY)
    assert reg.ready
    w = reg.weights
    assert w["cold"] == pytest.approx(1.5, abs=0.15)
    assert w["mon"] == pytest.approx(2.0, abs=0.4)
    assert w["sun"] == pytest.approx(-0.8, abs=0.3)
    assert reg.r2 > 0.9
    assert {x["effect"] for x in reg.as_dict()["weights"] if x["feature"] == "sun"} == {"subtracts"}


def test_weather_fit_and_normals():
    kwh, weather = _world()
    hdd = {d: w.hdd for d, w in weather.items()}
    fit = wx.best_fit(kwh, hdd, {d: w.hdd_app for d, w in weather.items()}, 15.5)
    assert fit is not None and fit.reliable
    assert fit.slope == pytest.approx(1.5, abs=0.25)
    normal = wx.normal_hdd(hdd)
    assert normal[(1, 15)] > normal[(7, 15)]


def test_ensemble_bootstraps_tunes_and_logs():
    kwh, weather = _world()
    ctx = ens.Context(
        daily=kwh, hdd={d: w.hdd for d, w in weather.items()}, fit=None,
        regression=learner.fit(kwh, weather, today=TODAY), weather_days=weather,
        weather_normals=learner.normals(weather), seasonal=Forecaster(kwh),
    )
    log = ens.bootstrap(ctx, TODAY)
    assert {r["model"] for r in log} >= {"seasonal", "drivers", "recent", "last_week", "ensemble"}
    weights = ens.tune(log, kwh, today=TODAY)
    assert sum(weights.weights.values()) == pytest.approx(1.0)
    # The model that knows the drivers should earn the largest weight here.
    assert max(weights.weights, key=weights.weights.get) == "drivers"
    assert 0.85 <= weights.bias <= 1.15
    rows = ens.log_rows(ctx, weights, TODAY)
    assert any(r["model"] == "ensemble" and r["target"] == TODAY for r in rows)
    days = ens.forecast_days(ctx, weights, TODAY)
    assert len(days) >= 365


def test_bill_is_priced_like_a_bill():
    sched = TariffSchedule([DEFAULT_TARIFF])
    report = Forecaster({TODAY - timedelta(days=i): 25.0 for i in range(1, 40)}).forecast(today=TODAY, schedule=sched)
    est = bill.estimate(
        start=date(2026, 9, 27), end=date(2026, 11, 7), today=TODAY, rows=[], live_today={},
        schedule=sched, night_share={m: 0.3 for m in range(1, 13)}, factors={"day": 1.0, "night": 1.0},
        next_days=report.next_days, today_remaining_kwh=0.0, source="test",
    )
    d = est.as_dict()
    assert d["days"] == 42
    # Supply from 27 September, tariff from 6 October: the nine days before it began are
    # not charged its standing charge or PSO (no retroactive pricing), and say so.
    assert d["standing"] == pytest.approx(359.71 / 365 * 33, abs=0.01)
    assert d["pso"] == pytest.approx(19.10 / 365 * 33, abs=0.01)
    assert d["unpriced_days"] == 9 and "before the first tariff" in d["note"]
    assert d["vat"] == pytest.approx(d["total"] * 0.09 / 1.09, abs=0.01)
    assert d["low"] <= d["total"] <= d["high"]
    period = bill.period_from_sse({"next_bill_date": "2026-11-08", "supply_start": "2026-09-27"}, TODAY)
    assert period[:2] == (date(2026, 9, 27), date(2026, 11, 7))


def test_sse_issued_bill_rolls_period_forward_and_summarizes_last_bill():
    sched = TariffSchedule([DEFAULT_TARIFF])
    # Exact pre-1.0.4 shape cached in electricity.db after the first SSE bill is issued.
    cached_billing = {
        "next_bill_date": "2027-01-08",
        "supply_start": "2026-09-27",
        "contract_end": "2027-09-26",
        "latest_bill": {
            "billDate": "2026-11-09",
            "totalBillAmount": "€7.16",
            "billAmountStatus": "CREDIT",
            "previousBalance": "€316.22",
            "previousBalanceStatus": "CREDIT",
            "premisesBillSummary": [
                {"totalElectricityInvoice": "€309.06", "totalAllInvoices": "€309.06"}
            ],
        },
        "transactions": [
            {
                "transactionDate": "2026-11-09",
                "transactionDescription": "Bill",
                "utilityType": "E",
                "transactionAmount": "€309.06",
                "transactionStatus": "DEBIT",
                "reversed": False,
            }
        ],
    }
    history = [
        {"date": "2026-11-12", "register": "day", "status": "Accepted", "units_used": 173.0},
        {"date": "2026-11-12", "register": "night", "status": "Accepted", "units_used": 79.0},
        {"date": "2026-11-04", "register": "day", "status": "Accepted", "units_used": 656.0},
        {"date": "2026-11-04", "register": "night", "status": "Accepted", "units_used": 315.0},
        {"date": "2026-09-27", "register": "day", "status": "Accepted", "units_used": 0.0},
        {"date": "2026-09-27", "register": "night", "status": "Accepted", "units_used": 0.0},
    ]
    now_day = date(2026, 11, 15)
    # With reading history: bill closed on 2026-11-04 reading, next cycle is 2026-11-05 .. 2027-01-07.
    period = bill.period_from_sse(cached_billing, now_day, history=history)
    assert period[:2] == (date(2026, 11, 5), date(2027, 1, 7))

    # Without reading history: bill closes on billDate - 1 day (2026-11-08), next starts 2026-11-09.
    period_no_hist = bill.period_from_sse(cached_billing, now_day)
    assert period_no_hist[:2] == (date(2026, 11, 9), date(2027, 1, 7))

    summary = bill.summarize_last_bill(
        billing=cached_billing,
        history=history,
        anchor_prev=None,
        today=now_day,
        rows=[],
        schedule=sched,
        factors={"day": 1.0, "night": 1.0},
    )
    assert summary is not None
    assert summary["total"] == 309.06 and summary["billed_eur"] == 309.06
    assert summary["start"] == "2026-09-27" and summary["end"] == "2026-11-04" and summary["days"] == 39
    assert summary["day_kwh"] == 656 and summary["night_kwh"] == 315 and summary["kwh"] == 971
    assert summary["balance_after_eur"] == 7.16 and summary["previous_balance_eur"] == 316.22

    # Second bill rollover: transactions carry both bills, so 2nd bill starts after 1st bill's close.
    second_billing = {
        **cached_billing,
        "next_bill_date": "2027-03-09",
        "latest_bill": {"billDate": "2027-01-08", "premisesBillSummary": [{"totalElectricityInvoice": "€410.00"}]},
        "transactions": [
            {"transactionDate": "2027-01-08", "transactionDescription": "Bill", "utilityType": "E", "transactionAmount": "€410.00"},
            *cached_billing["transactions"],
        ],
    }
    resolved_second = bill.resolve_last_bill(second_billing, history=history)
    assert resolved_second["start"] == date(2026, 11, 5)
    assert resolved_second["end"] == date(2027, 1, 7)
    assert bill.period_from_sse(second_billing, date(2027, 1, 10), history=history)[:2] == (
        date(2027, 1, 8),
        date(2027, 3, 8),
    )


def test_hall_phase_correction_is_pf_dependent_and_minimal():
    assert phase_factor(1.0, 2.0) == pytest.approx(math.cos(math.radians(2.0)))
    assert phase_factor(0.6, 2.0) < phase_factor(0.9, 2.0) < 1.0
    fix = HallCorrection()
    rng = random.Random(1)
    t = 0.0
    for _ in range(4000):
        t += 11
        rest = rng.uniform(0, 120)                   # the rest of the house nearly idle
        hall_true, pf = 400.0, 0.65
        va = hall_true / pf
        hall_read = va * math.cos(math.acos(pf) - math.radians(3))   # 3° phase error reads high
        fix.observe(hall_true + rest, hall_read, va, "2026-10-07", t)
    delta = fix.delta()
    assert 0 < delta <= 3.2
    model = fix.model()
    assert model["kind"] == "phase"
    assert hour_factor(model, 400, 400 / 0.65) < 1.0
    assert hour_factor(model, 400, 400) == pytest.approx(math.cos(math.radians(delta)))


def test_no_correction_when_the_hall_behaves():
    fix = HallCorrection()
    for i in range(1000):
        fix.observe(1000.0, 400.0, 520.0, "2026-10-07", i * 11.0)
    assert fix.delta() == 0.0 and fix.gain() == 1.0


def test_minute_buffer_aggregates_and_drains():
    buf = MinuteBuffer()
    t0 = datetime(2026, 10, 7, 18, 0, tzinfo=UTC)
    for s in range(60):
        buf.add("house_w", 1000 + s, t0 + timedelta(seconds=s))
        buf.add("house_va", 1100, t0 + timedelta(seconds=s))
    buf.add_energy("house", 0.0167, t0 + timedelta(seconds=30))
    assert buf.drain(t0 + timedelta(seconds=30)) == []          # minute still open
    rows = buf.drain(t0 + timedelta(minutes=1, seconds=3))
    assert len(rows) == 1
    r = rows[0]
    assert r["house_w_min"] == 1000 and r["house_w_max"] == 1059
    assert r["house_pf"] == pytest.approx(1029.5 / 1100, abs=1e-3)
    assert r["house_wh"] == pytest.approx(16.7)


def test_database_backup_and_recovery(tmp_path: Path):
    path = tmp_path / "electricity.db"
    store = Store(path)
    store.set_meta("hello", "world")
    target = durability.rotate(path, date(2026, 10, 4))  # a Sunday
    assert [p.name for p in target] == ["daily-2026-10-04.db", "weekly-2026-10-04.db"]
    for t in target:
        store.backup_to(t)
    store.close()
    assert durability.recover(path)[0] == "ok"
    path.write_bytes(b"this is not a database" * 100)      # simulate corruption
    outcome, detail = durability.recover(path)
    assert outcome == "restored" and "damaged" in detail
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='hello'").fetchone()[0] == "world"
    assert any(p.name.startswith("electricity.damaged-") for p in tmp_path.iterdir())


def test_csv_export(tmp_path: Path):
    store = Store(tmp_path / "e.db")
    store.add_anchor(datetime(2026, 10, 7, 17, 50, 34, tzinfo=UTC), 13876, 5660, "setup")
    counts = store.export_csv(tmp_path / "out", ["meter_anchors", "hourly"])
    assert counts == {"meter_anchors": 1, "hourly": 0}
    assert "13876" in (tmp_path / "out" / "meter_anchors.csv").read_text()
