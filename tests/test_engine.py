"""The pure engine: tariff bands across Irish clock changes, money, the clamps,
the meter registers, live accounting, forecasting, tables, ESB comparison."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.electricity_analyzer.engine import analytics, reconcile, registers
from custom_components.electricity_analyzer.engine.esb.hdf import parse_hdf
from custom_components.electricity_analyzer.engine.esb.reads import parse_billed_reads
from custom_components.electricity_analyzer.engine.forecast import Forecaster, bill_period_for
from custom_components.electricity_analyzer.engine.hourly import build_hour, reprice
from custom_components.electricity_analyzer.engine.live import Counter, LiveTracker
from custom_components.electricity_analyzer.engine.store import Store
from custom_components.electricity_analyzer.engine.tables import TableBuilder
from custom_components.electricity_analyzer.engine.tariff import (
    CLOCK_LOCAL,
    DEFAULT_TARIFF,
    IRELAND,
    NightWindow,
    Tariff,
    TariffSchedule,
    hours_in_local_day,
    local_midnight,
)

import simulate_esb  # tests/simulate_esb.py

UTC = timezone.utc
# The same plan, but in force over every date these tests use (the shipped default
# only starts on 6 October 2026, and nothing before a tariff's start is priced at it).
SCHED = TariffSchedule([replace(DEFAULT_TARIFF, valid_from=date(2024, 1, 1))])


def local(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=IRELAND).astimezone(UTC)


# ------------------------------------------------------------------ tariff


@pytest.mark.parametrize(
    ("when", "band"),
    [
        (local(2026, 1, 15, 22, 59), "day"),
        (local(2026, 1, 15, 23, 0), "night"),   # winter: 23:00 local = 23:00 GMT
        (local(2026, 1, 16, 7, 59), "night"),
        (local(2026, 1, 16, 8, 0), "day"),
        (local(2026, 7, 1, 23, 30), "day"),     # summer: 23:30 IST is 22:30 GMT
        (local(2026, 7, 2, 0, 0), "night"),
        (local(2026, 7, 2, 8, 30), "night"),    # 08:30 IST is 07:30 GMT
        (local(2026, 7, 2, 9, 0), "day"),
    ],
)
def test_gmt_night_window_moves_with_summer_time(when, band):
    assert DEFAULT_TARIFF.window.band_at(when) == band


def test_local_window_follows_wall_clock():
    w = NightWindow(23, 8, CLOCK_LOCAL)
    assert w.band_at(local(2026, 7, 1, 23, 30)) == "night"
    assert w.band_at(local(2026, 7, 2, 8, 30)) == "day"


def test_window_description_and_next_change():
    assert DEFAULT_TARIFF.window.describe(date(2026, 7, 1)) == "00:00–09:00 (summer time, meter on GMT)"
    assert DEFAULT_TARIFF.window.describe(date(2026, 1, 15)) == "23:00–08:00 (winter time, meter on GMT)"
    assert "summer time" in DEFAULT_TARIFF.window.describe(date(2026, 10, 7))  # clocks go back on 25 Oct
    nxt = DEFAULT_TARIFF.window.next_change(local(2026, 10, 7, 19, 30))
    assert nxt.astimezone(IRELAND).strftime("%H:%M") == "00:00"  # still summer time on 7 Oct


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25), date(2026, 7, 1)])
def test_fixed_charge_is_one_day_on_every_day(day):
    start = local_midnight(day)
    total = sum(DEFAULT_TARIFF.fixed_for_hour(start + timedelta(hours=i)) for i in range(hours_in_local_day(day)))
    assert total == pytest.approx((359.71 + 19.10) / 365)


def test_defaults_are_sse_30_day_night_incl_vat():
    assert DEFAULT_TARIFF.day_rate == pytest.approx(0.3238)
    assert DEFAULT_TARIFF.night_rate == pytest.approx(0.1960)
    assert DEFAULT_TARIFF.standing_per_year == pytest.approx(359.71)


def test_schedule_picks_period_by_date():
    older = Tariff(date(2025, 10, 20), "SSE", "old", 0.2917, 0.1866, 338.98, 19.08)
    sched = TariffSchedule([DEFAULT_TARIFF, older])
    assert sched.for_date(date(2026, 1, 1)).plan == "old"
    assert sched.for_date(date(2026, 10, 5)).plan == "old"  # the new plan starts on 6 October
    assert sched.for_date(date(2026, 10, 6)) is DEFAULT_TARIFF
    assert sched.is_estimated(date(2025, 1, 1))


# ------------------------------------------------------------------ hours


def _hour(start, house, hall=None):
    return build_hour(
        start,
        {"energy": {"change": house}, "power": {"mean": house * 1000}, "apparent_power": {"mean": house * 1050}},
        None if hall is None else {"energy": {"change": hall}, "power": {"mean": hall * 1000}},
        SCHED,
    )


def test_hall_is_inside_house_not_added():
    row = _hour(local(2026, 1, 15, 12), 2.0, 0.5)
    assert row["rest_kwh"] == pytest.approx(1.5)
    assert row["house_cost_eur"] == pytest.approx(2.0 * 0.3238)
    assert row["hall_cost_eur"] + row["rest_cost_eur"] == pytest.approx(row["house_cost_eur"])


def test_hall_reading_above_house_is_clamped_and_flagged():
    row = _hour(local(2026, 1, 15, 3), 0.40, 0.42)
    assert row["band"] == "night"
    assert row["rest_kwh"] == 0
    assert row["hall_over_house"] == 1
    assert row["hall_kwh"] == pytest.approx(0.42)  # kept as measured
    assert row["hall_cost_eur"] == pytest.approx(row["house_cost_eur"])


def test_reprice_uses_new_schedule():
    row = _hour(local(2026, 10, 6, 12), 1.0)
    cheaper = DEFAULT_TARIFF.__class__(**{**DEFAULT_TARIFF.__dict__, "valid_from": date(2026, 10, 6), "day_rate": 0.10})
    out = reprice(row, SCHED.with_period(cheaper))
    assert out["house_cost_eur"] == pytest.approx(0.10)


# --------------------------------------------------------------- registers


ANCHOR = registers.Anchor(datetime(2026, 10, 7, 17, 50, 34, tzinfo=UTC), 13876, 5660)


def test_register_estimate_from_the_real_anchor():
    hours = [
        {"hour_utc": datetime(2026, 10, 7, 17, tzinfo=UTC), "band": "day", "house_kwh": 1.2},
        {"hour_utc": datetime(2026, 10, 7, 18, tzinfo=UTC), "band": "day", "house_kwh": 1.0},
    ]
    est = registers.estimate(ANCHOR, hours, as_of=datetime(2026, 10, 7, 23, 30, tzinfo=UTC), tail_kwh=0.3, tail_band="night")
    # 9m26s of the anchor hour counts: 1.2 * 566/3600
    assert est.day == pytest.approx(13876 + 1.2 * 566 / 3600 + 1.0)
    assert est.night == pytest.approx(5660.3)
    assert est.day_dial == 13877
    assert est.hours_missing == 4  # 19:00-23:00 UTC had no stored hours


def test_register_series_runs_both_ways_from_anchor():
    hours = [
        {"hour_utc": datetime(2026, 10, 7, h, tzinfo=UTC), "band": "night" if h < 8 else "day", "house_kwh": 1.0}
        for h in range(0, 20)
    ]
    series = {end: (d, n) for end, d, n in registers.register_series(ANCHOR, hours)}
    at_anchor_hour_end = series[datetime(2026, 10, 7, 18, tzinfo=UTC)]
    assert at_anchor_hour_end[0] == pytest.approx(13876 + 566 / 3600)
    assert series[datetime(2026, 10, 7, 8, tzinfo=UTC)][1] == pytest.approx(5660)  # night done by 08:00 UTC
    assert series[datetime(2026, 10, 7, 1, tzinfo=UTC)][1] == pytest.approx(5660 - 7)


def test_dial_never_rounds_up():
    est = registers.RegisterEstimate(datetime.now(UTC), 13876.999, 5660.0, ANCHOR, 0, 0, 1, 0, 0)
    assert est.day_dial == 13876


# -------------------------------------------------------------------- live


def test_counter_splits_delta_across_hour_boundary():
    c = Counter()
    c.feed(100.0, datetime(2026, 10, 7, 10, 59, 30, tzinfo=UTC))
    c.feed(100.1, datetime(2026, 10, 7, 11, 0, 30, tzinfo=UTC))  # 6 kW for a minute
    assert c.hours[datetime(2026, 10, 7, 10, tzinfo=UTC)] == pytest.approx(0.05)
    assert c.hours[datetime(2026, 10, 7, 11, tzinfo=UTC)] == pytest.approx(0.05)


def test_counter_reset_counts_new_value():
    c = Counter()
    t = datetime(2026, 10, 7, 10, tzinfo=UTC)
    c.feed(5.0, t)
    assert c.feed(0.2, t + timedelta(minutes=1)) == pytest.approx(0.2)
    assert c.resets == 1


def test_counter_ignores_implausible_jump():
    c = Counter()
    t = datetime(2026, 10, 7, 10, tzinfo=UTC)
    c.feed(5.0, t)
    assert c.feed(5000.0, t + timedelta(seconds=10)) == 0.0
    assert c.rejected == 1


def test_tracker_rest_power_and_hall_over_time():
    tr = LiveTracker()
    t = datetime(2026, 10, 7, 3, tzinfo=UTC)
    tr.feed_power("house", 400, t, "2026-10-07")
    tr.feed_power("hall", 420, t, "2026-10-07")
    tr.feed_power("house", 410, t + timedelta(seconds=30), "2026-10-07")
    assert tr.rest_power == 0
    assert tr.hall_over_seconds == pytest.approx(30)


# ---------------------------------------------------------------- forecast


def _daily(days=420, start=date(2025, 8, 1)):
    shape = {1: 44, 2: 36.4, 3: 31.3, 4: 24.7, 5: 19.7, 6: 20.4, 7: 18.8, 8: 23.7, 9: 22.4, 10: 26.6, 11: 26.1, 12: 35.6}
    rng = random.Random(3)
    return {start + timedelta(days=i): shape[(start + timedelta(days=i)).month] * rng.uniform(0.9, 1.1) for i in range(days)}


def test_forecast_learns_season_and_is_backtested():
    f = Forecaster(_daily()).forecast(today=date(2026, 9, 24), schedule=SCHED)
    assert f.available
    assert f.mape is not None and f.mape < 0.15
    jan = next(p for p in f.months if p.start == date(2027, 1, 1))
    jul = next(p for p in f.months if p.start == date(2027, 7, 1))
    assert jan.total_kwh > 1.8 * jul.total_kwh  # winter much higher, as in the house's data
    assert sum(p.total_kwh for p in f.seasons) == pytest.approx(f.year_kwh, rel=1e-6)


def test_forecast_with_three_weeks_uses_prior_shape():
    short = {d: v for d, v in _daily().items() if date(2026, 9, 3) <= d < date(2026, 9, 24)}
    f = Forecaster(short).forecast(today=date(2026, 9, 24), schedule=SCHED)
    jan = next(p for p in f.months if p.start == date(2027, 1, 1))
    sep_level = sum(short.values()) / len(short)
    assert jan.total_kwh / 31 > 1.5 * sep_level


def test_bill_period():
    assert bill_period_for(date(2026, 10, 7), date(2026, 9, 27), 2) == (date(2026, 9, 27), date(2026, 11, 26))
    assert bill_period_for(date(2026, 12, 1), date(2026, 9, 27), 2) == (date(2026, 11, 27), date(2027, 1, 26))


# -------------------------------------------------------- store and tables


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "e.db")
    rng = random.Random(5)
    rows = []
    start = local_midnight(date(2026, 8, 1))
    for i in range(24 * 68):
        h = start + timedelta(hours=i)
        rows.append(_hour(h, rng.uniform(0.3, 2.5), rng.uniform(0.35, 0.55)))
    s.upsert_hours(rows)
    yield s
    s.close()


def test_daily_view_and_tables_agree(store):
    hours = store.hours()
    tb = TableBuilder(hours, now=datetime(2026, 10, 7, 12, tzinfo=UTC))
    day = tb.build("day", date(2026, 9, 1), date(2026, 9, 30))
    month = tb.build("month", date(2026, 9, 1), date(2026, 9, 30))
    assert len(day["rows"]) == 30
    assert sum(r["house_kwh"] for r in day["rows"]) == pytest.approx(month["rows"][0]["house_kwh"], abs=0.05)
    assert day["totals"]["total_cost"] == pytest.approx(month["totals"]["total_cost"], abs=0.01)
    view = {r["local_date"]: r for r in store.daily(date(2026, 9, 1), date(2026, 9, 30))}
    assert view["2026-09-15"]["house_kwh"] == pytest.approx(
        next(r for r in day["rows"] if r["key"] == "2026-09-15")["house_kwh"], abs=0.001
    )
    everything = tb.build("year")
    assert everything["start"] == "2026-08-01"


def test_simulated_esb_lag_and_reconcile(store):
    hours = store.hours()
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    esb = simulate_esb.intervals(hours, DEFAULT_TARIFF.window, now)
    assert max(e[0] for e in esb) < local_midnight(date(2026, 10, 6))
    assert simulate_esb.intervals(hours, DEFAULT_TARIFF.window, now) == esb  # deterministic
    rec = reconcile.reconcile(hours, esb)
    assert rec.calibration == pytest.approx(simulate_esb.METER_BIAS, abs=0.003)


def test_store_round_trips_tariff_anchor_submission(store):
    store.save_tariff(DEFAULT_TARIFF)
    assert store.tariffs()[0] == DEFAULT_TARIFF
    store.add_anchor(ANCHOR.taken_at, 13876, 5660, "setup")
    assert registers.Anchor.from_row(store.latest_anchor()).day == 13876
    store.log_submission({"read_at": "x", "day_kwh": 1, "night_kwh": 2, "mode": "dry_run", "status": "dry_run"})
    assert store.last_submission()["status"] == "dry_run"
    assert store.last_submission(status="submitted") is None


# --------------------------------------------------------------------- ESB


def test_hdf_kwh_export_is_parsed_newest_first():
    text = (
        "MPRN,Meter Serial Number,Read Value,Read Type,Read Date and End Time\n"
        "10000000001,1,0.3230,Active Import Interval (kWh),16-04-2026 03:00\n"
        "10000000001,1,0.3200,Active Import Interval (kWh),16-04-2026 02:30\n"
        "10000000001,1,0.3170,Active Import Interval (kWh),16-04-2026 02:00\n"
    )
    ds = parse_hdf(text)
    assert len(ds.readings) == 3
    first = min(ds.readings, key=lambda r: r.start_utc)
    assert first.kwh == pytest.approx(0.317)
    assert first.start_utc == datetime(2026, 4, 16, 0, 30, tzinfo=UTC)  # 01:30 IST start


def test_billed_reads():
    reads = parse_billed_reads(
        {"readings": [
            {"date": "14-Sep-26", "registerNumber": "002", "reading": "5494.0", "registerType": "Night", "details": "Scheduled Reading (Actual)"},
            {"date": "14-Sep-26", "registerNumber": "001", "reading": "13530.0", "registerType": "Day"},
        ]}
    )
    assert (date(2026, 9, 14), "night", 5494.0, "Scheduled Reading (Actual)") in reads
    assert any(r[1] == "day" and r[2] == 13530.0 for r in reads)


def test_analytics_profiles(store):
    hours = store.hours()
    profile = analytics.hour_profile(hours)
    assert set(profile) == set(range(24))
    shares = analytics.hour_shares(profile)
    assert sum(shares) == pytest.approx(1.0)
    assert analytics.baseline_w(hours) is not None
