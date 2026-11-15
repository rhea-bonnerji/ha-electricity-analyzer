"""The learner: every real meter reading makes the estimate (and the money) better."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.electricity_analyzer.engine import calibration as calib
from custom_components.electricity_analyzer.engine.tariff import DEFAULT_TARIFF

UTC = timezone.utc
START = datetime(2026, 6, 1, tzinfo=UTC)
DAY_TRUE, NIGHT_TRUE = 1.02, 1.05   # the meter counts 2% / 5% more than the clamp


def _hours(days: int):
    rows = []
    for i in range(days * 24):
        h = START + timedelta(hours=i)
        band = DEFAULT_TARIFF.window.band_at(h)
        rows.append({"hour_utc": h, "band": band, "house_kwh": 1.0 if band == "day" else 0.5})
    return rows


def _true_reading(hours, at: datetime, origin: str = "manual") -> calib.Reading:
    """What the real meter would show: 10000/4000 at START plus the clamp x the true ratios."""
    clamp, _ = calib.clamp_between(hours, START, at)
    return calib.Reading(at, 10000 + clamp["day"] * DAY_TRUE, 4000 + clamp["night"] * NIGHT_TRUE, origin)


def test_no_readings_means_factor_one():
    cal = calib.learn([], _hours(5), now=START + timedelta(days=5))
    assert (cal.day, cal.night, cal.source) == (1.0, 1.0, "default")


def test_each_reading_improves_the_estimate():
    hours = _hours(120)
    readings = [_true_reading(hours, START + timedelta(days=d, hours=13, minutes=7)) for d in (0, 14, 45, 75, 110)]
    cal = calib.learn(readings, hours, now=START + timedelta(days=120))
    assert cal.source == "readings"
    assert cal.day == pytest.approx(DAY_TRUE, abs=0.002)
    assert cal.night == pytest.approx(NIGHT_TRUE, abs=0.003)
    errors = [abs(l.error("night")) for l in cal.lessons]
    assert errors[-1] < errors[0]              # later predictions are closer to the meter
    assert all(l.used["day"] and l.used["night"] for l in cal.lessons)


def test_esb_prior_is_used_until_readings_outweigh_it():
    cal = calib.learn([], _hours(5), now=START, prior=1.012, prior_source="esb")
    assert cal.day == pytest.approx(1.012) and cal.source == "esb"


def test_typo_is_reported_not_learned():
    hours = _hours(60)
    good = [_true_reading(hours, START + timedelta(days=d)) for d in (0, 30)]
    typo = calib.Reading(START + timedelta(days=59), good[-1].day + 9000, good[-1].night + 300, "manual")
    cal = calib.learn([*good, typo], hours, now=START + timedelta(days=60))
    last = cal.lessons[-1]
    assert not last.used["day"] and "outside" in last.note
    assert cal.day == pytest.approx(DAY_TRUE, abs=0.01)


def test_register_backwards_is_rejected():
    hours = _hours(40)
    a = _true_reading(hours, START + timedelta(days=1))
    b = calib.Reading(START + timedelta(days=30), a.day - 5, a.night + 100, "manual")
    cal = calib.learn([a, b], hours, now=START + timedelta(days=40))
    assert not cal.lessons[0].used["day"] and "backwards" in cal.lessons[0].note


def test_register_history_passes_through_every_reading():
    hours = _hours(90)
    times = [START + timedelta(days=d, hours=1) for d in (10, 40, 70)]  # whole hours
    readings = [_true_reading(hours, t) for t in times]
    cal = calib.learn(readings, hours, now=START + timedelta(days=90))
    series = {end: (d, n) for end, d, n in calib.register_series(readings, hours, cal)}
    for r in readings:
        d, n = series[r.taken_at]
        assert d == pytest.approx(r.day, abs=1e-6) and n == pytest.approx(r.night, abs=1e-6)
    # Before the first reading it runs backwards with the learned factor.
    d0, _ = series[START + timedelta(hours=1)]
    assert d0 < readings[0].day


def test_segment_factor_prices_between_readings_exactly():
    hours = _hours(60)
    readings = [_true_reading(hours, START + timedelta(days=d)) for d in (5, 50)]
    cal = calib.learn(readings, hours, now=START + timedelta(days=60))
    inside = START + timedelta(days=20, hours=12)
    assert cal.factor_at(inside, "day") == pytest.approx(DAY_TRUE, abs=1e-4)
    assert cal.factor_at(START + timedelta(days=55), "night") == cal.night
