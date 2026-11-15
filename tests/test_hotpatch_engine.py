"""1.0.2 hotpatch, pure engine: tariff dates and bands, fixed charges, unpriced hours,
statistics bucketing, and database safety. Every test here pins a fixed bug or a rule
the brief states (dates: data from 27 September, tariff from 6 October 2026)."""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.electricity_analyzer.engine import analytics, bill, durability
from custom_components.electricity_analyzer.engine.hourly import build_hour, reprice
from custom_components.electricity_analyzer.engine.store import Store
from custom_components.electricity_analyzer.engine.tariff import (
    DEFAULT_TARIFF,
    IRELAND,
    LEGACY_DEFAULT_PSO,
    LEGACY_DEFAULT_VALID_FROM,
    Tariff,
    TariffSchedule,
    cost_hour,
    hours_in_local_day,
    local_midnight,
)

UTC = timezone.utc
NEW = DEFAULT_TARIFF  # SSE 30% Day Night, from 6 October 2026, PSO €19.10/year
# A previous tariff, as a user would add it with electricity_analyzer.add_tariff.
OLD = Tariff(date(2026, 9, 1), "SSE Airtricity", "previous", 0.2917, 0.1866, 338.98, 19.10)


def local(y, m, d, h, mi=0, fold=0):
    return datetime(y, m, d, h, mi, tzinfo=IRELAND, fold=fold).astimezone(UTC)


def stats(kwh=1.0):
    return {"energy": {"change": kwh}, "power": {"mean": kwh * 1000}}


# ------------------------------------------------------------ defaults and dates


def test_defaults_tariff_from_6_october_with_pso_19_10():
    assert NEW.valid_from == date(2026, 10, 6)
    assert NEW.pso_per_year == pytest.approx(19.10)
    # Unit rates unchanged.
    assert (NEW.day_rate, NEW.night_rate, NEW.standing_per_year) == (0.3238, 0.1960, 359.71)
    assert (LEGACY_DEFAULT_VALID_FROM, LEGACY_DEFAULT_PSO) == (date(2026, 10, 5), 6.67)


@pytest.mark.parametrize(
    ("when", "priced_by"),
    [
        (local(2026, 10, 5, 23), None),          # 5 Oct 23:00 IST (22:00 UTC): before the tariff
        (local(2026, 10, 6, 0), "new"),          # 6 Oct 00:00 IST = 5 Oct 23:00 UTC: the tariff's first hour
        (local(2026, 10, 6, 1), "new"),          # 6 Oct 01:00 IST
    ],
)
def test_effective_date_boundary_is_local_midnight(when, priced_by):
    alone = cost_hour(TariffSchedule([NEW]), when, 1.0)
    assert (alone.tariff is not None) == (priced_by == "new")
    with_old = cost_hour(TariffSchedule([OLD, NEW]), when, 1.0)
    assert with_old.tariff is (NEW if priced_by == "new" else OLD)


def test_new_tariff_is_never_applied_retroactively():
    """27 September (monitoring starts) is before 6 October: kWh kept, no price."""
    row = build_hour(local(2026, 9, 27, 14), stats(0.8), stats(0.2), TariffSchedule([NEW]))
    assert row["house_kwh"] == pytest.approx(0.8)
    assert row["band"] == "day"
    for key in ("unit_rate_eur", "house_cost_eur", "hall_cost_eur", "rest_cost_eur", "fixed_cost_eur", "tariff_from"):
        assert row[key] is None, key
    # Adding the previous tariff prices it, at the previous tariff.
    priced = reprice(row, TariffSchedule([OLD, NEW]))
    assert priced["house_cost_eur"] == pytest.approx(0.8 * OLD.day_rate)
    assert priced["tariff_from"] == "2026-09-01"


def test_current_is_the_period_in_force_not_a_future_one():
    future = replace(NEW, valid_from=date(2099, 1, 1), plan="future")
    assert TariffSchedule([NEW, future]).current.plan == NEW.plan


# ----------------------------------------------------------- bands on the meter clock


@pytest.mark.parametrize(
    ("when", "band"),
    [
        # Irish summer time (IST, UTC+1), e.g. 7/8 October 2026: night 00:00-09:00 local.
        (local(2026, 10, 7, 23, 0), "day"),     # 22:00 UTC
        (local(2026, 10, 8, 0, 0), "night"),    # local midnight = 23:00 GMT
        (local(2026, 10, 8, 1, 0), "night"),
        (local(2026, 10, 8, 8, 59), "night"),
        (local(2026, 10, 8, 9, 0), "day"),      # 08:00 GMT
        # Irish winter (GMT): night 23:00-08:00 local.
        (local(2026, 12, 1, 22, 59), "day"),
        (local(2026, 12, 1, 23, 0), "night"),   # 23:00 GMT
        (local(2026, 12, 2, 0, 0), "night"),    # winter midnight
        (local(2026, 12, 2, 1, 0), "night"),
        (local(2026, 12, 2, 8, 0), "day"),
    ],
)
def test_bands_follow_the_gmt_meter_clock(when, band):
    assert cost_hour(TariffSchedule([NEW]), when, 1.0).band == band


def test_summer_night_starts_at_local_midnight_not_one():
    """The 23:00 UTC hour (00:00-01:00 IST) is night and is costed as night."""
    sched = TariffSchedule([NEW])
    rows = [build_hour(local(2026, 10, 7, 22) + timedelta(hours=i), stats(1.0), None, sched) for i in range(4)]
    labels = [(r["hour_utc"].astimezone(IRELAND).strftime("%H:%M"), r["band"]) for r in rows]
    assert labels == [("22:00", "day"), ("23:00", "day"), ("00:00", "night"), ("01:00", "night")]
    from custom_components.electricity_analyzer.longterm import build_series

    series = build_series(rows, [], [])
    night = dict(series["cost_night"])
    day = dict(series["cost_day"])
    midnight = local(2026, 10, 8, 0)
    assert midnight == datetime(2026, 10, 7, 23, tzinfo=UTC)
    assert night[midnight] == pytest.approx(NEW.night_rate) and day[midnight] == 0.0
    assert night[midnight - timedelta(hours=1)] == 0.0


def test_dst_spring_forward_day_has_23_hours_and_one_days_fixed_charge():
    day = date(2026, 3, 29)
    sched = TariffSchedule([replace(NEW, valid_from=date(2026, 1, 1))])
    assert hours_in_local_day(day) == 23
    start = local_midnight(day)
    rows = [build_hour(start + timedelta(hours=i), stats(), None, sched) for i in range(23)]
    assert sum(r["fixed_cost_eur"] for r in rows) == pytest.approx(NEW.fixed_per_day, abs=1e-4)
    # 01:00 local does not exist; 00:00 GMT and 01:00 GMT are both night, 08:00 GMT day.
    assert [r["band"] for r in rows[:3]] == ["night", "night", "night"]
    assert rows[8]["band"] == "day" and rows[8]["hour_utc"].hour == 8


def test_dst_autumn_back_day_has_25_hours_both_repeats_counted():
    day = date(2026, 10, 25)
    sched = TariffSchedule([NEW])
    assert hours_in_local_day(day) == 25
    start = local_midnight(day)
    rows = [build_hour(start + timedelta(hours=i), stats(), None, sched) for i in range(25)]
    assert sum(r["fixed_cost_eur"] for r in rows) == pytest.approx(NEW.fixed_per_day, abs=1e-4)
    first, second = local(2026, 10, 25, 1, fold=0), local(2026, 10, 25, 1, fold=1)
    assert second - first == timedelta(hours=1)
    by_hour = {r["hour_utc"]: r for r in rows}
    assert by_hour[first]["band"] == by_hour[second]["band"] == "night"


def test_dst_autumn_repeat_hour_round_trips_through_the_store(tmp_path):
    store = Store(tmp_path / "e.db")
    sched = TariffSchedule([NEW])
    first, second = local(2026, 10, 25, 1, fold=0), local(2026, 10, 25, 1, fold=1)
    store.upsert_hours([build_hour(first, stats(0.3), None, sched), build_hour(second, stats(0.4), None, sched)])
    labels = [r["local_hour"] for r in store.hours()]
    assert labels == ["2026-10-25 01:00", "2026-10-25 01:00 (2)"]
    assert sum(r["house_kwh"] for r in store.daily()) == pytest.approx(0.7)
    store.close()


# ---------------------------------------------------------------- fixed charges


def test_pso_and_standing_split_and_never_in_the_unit_rate():
    sched = TariffSchedule([NEW])
    day = date(2026, 10, 8)
    start = local_midnight(day)
    rows = [build_hour(start + timedelta(hours=i), stats(0.5), None, sched) for i in range(24)]
    assert all(r["unit_rate_eur"] in (NEW.day_rate, NEW.night_rate) for r in rows)  # pure energy price
    t = analytics.totals(rows, schedule=sched)
    assert t.fixed_cost == pytest.approx((359.71 + 19.10) / 365, abs=1e-5)
    assert t.standing_cost == pytest.approx(359.71 / 365, abs=1e-5)
    assert t.pso_cost == pytest.approx(19.10 / 365, abs=1e-5)
    assert t.total_cost == pytest.approx(t.energy_cost + t.standing_cost + t.pso_cost)
    d = t.as_dict()
    assert d["standing_cost"] + d["pso_cost"] == pytest.approx(d["fixed_cost"], abs=0.011)


def test_a_day_starts_at_its_full_fixed_charge_even_when_data_starts_late():
    """Monitoring starts mid-afternoon: the day still costs its fixed charge, booked on its
    first hour, not 10/24 of it."""
    from custom_components.electricity_analyzer.engine.hourly import book_fixed

    sched = TariffSchedule([NEW])
    start = local(2026, 10, 8, 14)
    rows = [build_hour(start + timedelta(hours=i), stats(0.5), None, sched) for i in range(10)]  # 14:00-24:00
    for r in rows:
        r["local_date"] = "2026-10-08"
    assert sum(r["fixed_cost_eur"] for r in rows) == 0.0  # no midnight hour among them
    fixed = {r["hour_utc"]: r for r in book_fixed(rows, sched)}
    rows = [fixed.get(r["hour_utc"], r) for r in rows]
    assert rows[0]["fixed_cost_eur"] == pytest.approx(NEW.fixed_for_day(date(2026, 10, 8)), abs=1e-6)
    assert all(r["fixed_cost_eur"] == 0.0 for r in rows[1:])
    assert book_fixed(rows, sched) == []  # nothing more to move


def test_the_fixed_charge_is_due_from_midnight_not_spread_over_the_hours():
    sched = TariffSchedule([NEW])
    midnight = local_midnight(date(2026, 10, 8))
    rows = [build_hour(midnight + timedelta(hours=i), stats(0.5), None, sched) for i in range(24)]
    assert rows[0]["fixed_cost_eur"] == pytest.approx((359.71 + 19.10) / 365, abs=1e-6)
    assert all(r["fixed_cost_eur"] == 0.0 for r in rows[1:])
    assert rows[0]["unit_rate_eur"] == NEW.night_rate  # the unit rate never carries it


def test_leap_years_divide_by_366():
    assert NEW.fixed_for_day(date(2028, 2, 29)) == pytest.approx((359.71 + 19.10) / 366)
    assert NEW.fixed_for_day(date(2027, 3, 1)) == pytest.approx((359.71 + 19.10) / 365)
    assert NEW.pso_for_day(date(2028, 6, 1)) == pytest.approx(19.10 / 366)
    assert NEW.standing_for_day(date(2026, 10, 8)) == pytest.approx(359.71 / 365)


def test_totals_report_unpriced_hours_and_rates_over_priced_kwh():
    sched = TariffSchedule([NEW])
    rows = [
        build_hour(local(2026, 10, 5, 12), stats(2.0), None, sched),   # before the tariff
        build_hour(local(2026, 10, 6, 12), stats(1.0), None, sched),   # priced, day
    ]
    t = analytics.totals(rows, schedule=sched)
    assert t.unpriced_hours == 1
    assert t.house_kwh == pytest.approx(3.0)
    assert t.energy_cost == pytest.approx(NEW.day_rate)
    assert t.effective_rate == pytest.approx(NEW.day_rate)  # not diluted by the unpriced 2 kWh
    assert t.as_dict()["unpriced_hours"] == 1


def test_live_hours_with_no_tariff_count_kwh_not_money():
    t = analytics.Totals(schedule=TariffSchedule([NEW]))
    t.add_live(1.0, None, "day", None, None)
    t.add_live(1.0, 0.25, "night", NEW.night_rate, NEW.fixed_for_hour(local(2026, 10, 8, 2)), tariff=NEW)
    assert t.house_kwh == 2.0 and t.unpriced_hours == 1
    assert t.energy_cost == pytest.approx(NEW.night_rate)
    assert t.standing_cost + t.pso_cost == pytest.approx(t.fixed_cost)


def test_bill_does_not_charge_fixed_before_the_tariff():
    est = bill.estimate(
        start=date(2026, 9, 27), end=date(2026, 10, 26), today=date(2026, 10, 8), rows=[], live_today={},
        schedule=TariffSchedule([NEW]), night_share={}, factors={}, next_days=[], today_remaining_kwh=0.0,
        source="test",
    )
    assert est.unpriced_days == 9  # 27 Sep - 5 Oct
    assert est.standing == pytest.approx(359.71 / 365 * 21, abs=1e-6)
    assert est.pso == pytest.approx(19.10 / 365 * 21, abs=1e-6)


# ------------------------------------------------------------- stored tariffs


def test_replace_tariff_moves_a_period_atomically_and_never_overwrites(tmp_path):
    store = Store(tmp_path / "e.db")
    legacy = replace(NEW, valid_from=LEGACY_DEFAULT_VALID_FROM, pso_per_year=LEGACY_DEFAULT_PSO)
    store.save_tariff(OLD)
    store.save_tariff(legacy)
    assert store.replace_tariff(legacy.valid_from, NEW)
    assert [t.valid_from for t in store.tariffs()] == [OLD.valid_from, NEW.valid_from]
    assert store.tariffs()[1].pso_per_year == pytest.approx(19.10)
    # Never onto a date another period already has, never from nothing.
    assert not store.replace_tariff(NEW.valid_from, replace(NEW, valid_from=OLD.valid_from))
    assert not store.replace_tariff(date(2000, 1, 1), NEW)
    assert len(store.tariffs()) == 2
    store.close()


def test_legacy_default_row_is_matched_only_in_the_exact_case():
    from custom_components.electricity_analyzer.runtime import _legacy_default_row

    legacy = replace(NEW, valid_from=LEGACY_DEFAULT_VALID_FROM, pso_per_year=LEGACY_DEFAULT_PSO)
    assert _legacy_default_row([OLD, legacy], NEW) is legacy
    assert _legacy_default_row([replace(legacy, day_rate=0.30)], NEW) is None          # someone's real tariff
    assert _legacy_default_row([legacy], replace(NEW, valid_from=date(2026, 11, 1))) is None
    assert _legacy_default_row([OLD], NEW) is None


# ---------------------------------------------------------------- database safety


def test_integrity_check_never_calls_an_unreadable_database_damaged(tmp_path):
    """A hot WAL in a folder the check cannot write to is 'could not tell', not damage."""
    folder = tmp_path / "ro"
    folder.mkdir()
    db = folder / "electricity.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
    conn.commit()
    hot = tmp_path / "hot"
    hot.mkdir()
    shutil.copy(db, hot / "electricity.db")
    shutil.copy(str(db) + "-wal", hot / "electricity.db-wal")
    conn.close()
    os.chmod(hot, 0o555)
    try:
        assert durability.check_file(hot / "electricity.db") is None
        outcome, _ = durability.recover(hot / "electricity.db")
        assert outcome in ("ok", "unchecked")
        assert sorted(p.name for p in hot.iterdir()) == ["electricity.db", "electricity.db-wal"]  # nothing moved
    finally:
        os.chmod(hot, 0o755)


def test_a_really_damaged_database_is_still_restored(tmp_path):
    db = tmp_path / "electricity.db"
    good = Store(db)
    good.set_meta("k", "v")
    good.backup_to(tmp_path / "backups" / "daily-2026-10-07.db")
    good.close()
    db.write_bytes(b"this is not a database" * 100)
    outcome, detail = durability.recover(db)
    assert outcome == "restored" and "daily-2026-10-07" in detail
    assert Store(db).get_meta("k") == "v"


# ------------------------------------------------- downtime filled from ESB's data


def _clamp_rows(sched, hours: dict[datetime, float]) -> list[dict]:
    return [build_hour(h, {"energy": {"change": kwh}}, None, sched) for h, kwh in sorted(hours.items())]


def _esb_rows(start: datetime, n_hours: int, kwh_per_hour: float = 0.5) -> list[tuple[datetime, float, str]]:
    out = []
    for i in range(n_hours * 2):
        t = start + timedelta(minutes=30 * i)
        out.append((t, kwh_per_hour / 2, NEW.window.band_at(t)))
    return out


def test_downtime_hole_and_restart_lump_are_rebuilt_from_esb():
    """HA down 22:00-09:00 local: no hours, then 09:00 holds all 11 hours' energy (at day rate)."""
    from custom_components.electricity_analyzer.engine import downtime

    sched = TariffSchedule([NEW])
    start = local(2026, 10, 10, 20)
    clamp = {start: 0.5, start + timedelta(hours=1): 0.5}           # 20:00, 21:00 seen
    restart = start + timedelta(hours=13)                            # 09:00 next day
    clamp[restart] = 0.5 * 12                                        # the lump: 22:00-09:59
    clamp[restart + timedelta(hours=1)] = 0.5
    stored = _clamp_rows(sched, clamp)
    esb = _esb_rows(start, 16)
    plan = dict(downtime.plan(stored, esb, ratio=1.0))
    assert sorted(plan) == [start + timedelta(hours=i) for i in range(2, 14)]  # 11-hour hole + the lump hour
    assert all(v == pytest.approx(0.5) for v in plan.values())
    rows = {r["hour_utc"]: r for r in stored}
    rows.update({h: build_hour(h, {"energy": {"change": k}}, None, sched, source="esb") for h, k in plan.items()})
    night = sum(r["house_kwh"] for r in rows.values() if r["band"] == "night")
    assert night == pytest.approx(0.5 * 9)  # 00:00-09:00 IST is back in the night band
    assert downtime.plan(list(rows.values()), esb, ratio=1.0) == []  # nothing more to do


def test_downtime_without_a_lump_only_fills_the_hole():
    from custom_components.electricity_analyzer.engine import downtime

    sched = TariffSchedule([NEW])
    start = local(2026, 10, 10, 12)
    stored = _clamp_rows(sched, {start: 0.5, start + timedelta(hours=4): 0.5})  # e.g. the Shelly was off
    plan = downtime.plan(stored, _esb_rows(start, 5), ratio=1.0)
    assert [h for h, _ in plan] == [start + timedelta(hours=i) for i in (1, 2, 3)]


def test_downtime_waits_for_complete_esb_data_and_takes_esb_history_from_before_the_clamp():
    from custom_components.electricity_analyzer.engine import downtime

    sched = TariffSchedule([NEW])
    start = local(2026, 10, 10, 12)
    stored = _clamp_rows(sched, {start: 0.5, start + timedelta(hours=4): 2.5})
    partial = _esb_rows(start, 5)[:-3]  # ESB not yet published for the last hours
    assert downtime.plan(stored, partial, ratio=1.0) == []  # nothing guessed for a half-covered hole
    earlier = _esb_rows(start - timedelta(days=3), 24 * 3 + 5)  # ESB also has the days before the clamp
    planned = [h for h, _ in downtime.plan(stored, earlier, ratio=1.0)]
    assert min(planned) == start - timedelta(days=3)  # the meter's history before the clamp, as ESB hours
    assert {start + timedelta(hours=i) for i in (1, 2, 3)} <= set(planned)  # and the hole
    assert start not in planned  # never a clamp hour that is not a restart lump


def test_downtime_fill_is_in_clamp_terms():
    from custom_components.electricity_analyzer.engine import downtime

    sched = TariffSchedule([NEW])
    start = local(2026, 10, 10, 12)
    stored = _clamp_rows(sched, {start: 0.5, start + timedelta(hours=2): 0.5})
    plan = dict(downtime.plan(stored, _esb_rows(start, 3, kwh_per_hour=1.02), ratio=1.02))
    assert plan[start + timedelta(hours=1)] == pytest.approx(1.0)



def test_esb_history_before_the_clamp_and_hall_share_over_measured_hours():
    from custom_components.electricity_analyzer.engine import downtime
    from custom_components.electricity_analyzer.longterm import build_series

    sched = TariffSchedule([NEW])
    start = local(2026, 10, 10, 12)
    stored = _clamp_rows(sched, {start: 0.5, start + timedelta(hours=1): 0.5})
    plan = downtime.plan(stored, _esb_rows(start - timedelta(hours=3), 3), ratio=1.0)
    assert [h for h, _ in plan] == [start - timedelta(hours=i) for i in (3, 2, 1)]
    esb_rows = [build_hour(h, {"energy": {"change": k}}, None, sched, source="esb") for h, k in plan]
    clamp_rows = [build_hour(start, {"energy": {"change": 1.0}}, {"energy": {"change": 0.25}}, sched)]
    t = analytics.totals(esb_rows + clamp_rows)
    assert t.hall_share == pytest.approx(0.25)  # not diluted by the ESB hours, which have no hall
    series = build_series(esb_rows + clamp_rows, [], [])
    assert len(series["house_energy_day"]) == 4 and len(series["hall_energy"]) == 1



def test_settle_and_registers_use_esb_figures():
    from custom_components.electricity_analyzer.engine import downtime, registers

    sched = TariffSchedule([NEW])
    start = local(2026, 10, 10, 12)
    rows = _clamp_rows(sched, {start + timedelta(hours=i): 1.0 for i in range(3)})
    esb = [(start + timedelta(minutes=30 * i), 0.55, "day") for i in range(4)]  # first two hours
    settled = dict(downtime.settle(rows, esb))
    assert settled == {start: pytest.approx(1.1), start + timedelta(hours=1): pytest.approx(1.1)}
    final = {h: reprice(r, sched, meter_kwh=settled.get(h)) if h in settled else r for h, r in ((r["hour_utc"], r) for r in rows)}
    assert final[start]["meter_source"] == "esb" and final[start]["meter_factor"] == pytest.approx(1.1)
    assert downtime.settle(list(final.values()), esb) == []
    again = reprice(final[start], TariffSchedule([replace(NEW, day_rate=0.5)]), lambda h, b: 0.9)
    assert again["meter_kwh"] == pytest.approx(1.1) and again["house_cost_eur"] == pytest.approx(0.55)  # stays ESB
    anchor = registers.Anchor(start, 1000.0, 500.0)
    est = registers.estimate(anchor, list(final.values()), as_of=start + timedelta(hours=3), calibration=1.02)
    assert est.day == pytest.approx(1000 + 1.1 + 1.1 + 1.0 * 1.02)  # ESB's figures, then the clamp x factor
