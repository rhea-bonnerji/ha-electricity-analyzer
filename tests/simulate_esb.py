"""What ESB would publish for a house, made from the clamp's own hours. TESTS ONLY.

* half-hourly intervals, each hour split unevenly in two, with the utility meter
  reading a steady ~1.2% more than the clamp plus a little per-interval noise
  (a typical CT-versus-meter relationship);
* published with ESB's real lag: a local day appears the morning after next, so at
  any moment the newest data is two to three days old;
* scheduled "billed" register reads every two months, on the meter's own registers.

Everything is seeded from the interval's own timestamp, so re-publishing the history
gives identical numbers, exactly as re-downloading ESB's HDF does.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from custom_components.electricity_analyzer.engine.registers import Anchor, register_series
from custom_components.electricity_analyzer.engine.tariff import IRELAND, NightWindow

UTC = timezone.utc
METER_BIAS = 1.012
NOISE = 0.015
PUBLISH_HOUR_LOCAL = 10   # a day's data appears at about 10:00, two days later


def published_through(now: datetime) -> datetime:
    """End of the newest day ESB would have published by `now` (a local midnight)."""
    local = now.astimezone(IRELAND)
    lag_days = 2 if local.hour >= PUBLISH_HOUR_LOCAL else 3
    last_day = local.date() - timedelta(days=lag_days)
    return datetime.combine(last_day + timedelta(days=1), time(0), IRELAND).astimezone(UTC)


def intervals(
    hours: list[dict[str, Any]], window: NightWindow, now: datetime
) -> list[tuple[datetime, float, str]]:
    """(start_utc, kWh, band) half-hours, up to what ESB would have published."""
    through = published_through(now)
    out: list[tuple[datetime, float, str]] = []
    for row in hours:
        start: datetime = row["hour_utc"]
        if start + timedelta(hours=1) > through or row.get("house_kwh") is None:
            continue
        rng = random.Random(int(start.timestamp()))
        kwh = float(row["house_kwh"]) * METER_BIAS
        split = rng.uniform(0.42, 0.58)
        for i, share in enumerate((split, 1.0 - split)):
            half = start + timedelta(minutes=30 * i)
            value = max(kwh * share * (1.0 + rng.gauss(0.0, NOISE)), 0.0)
            out.append((half, round(value, 3), window.band_at(half)))
    return out


def billed_reads(
    hours: list[dict[str, Any]], anchor: Anchor | None, now: datetime
) -> list[tuple[date, str, float, str]]:
    """Scheduled reads on the 14th of every other month, as ESB lists them."""
    if anchor is None or not hours:
        return []
    series = register_series(anchor, hours, calibration=METER_BIAS)
    by_end = {end: (d, n) for end, d, n in series}
    through = published_through(now)
    first = hours[0]["hour_utc"].astimezone(IRELAND).date()
    out = []
    cursor = date(first.year, first.month, 14)
    while True:
        instant = datetime.combine(cursor, time(0), IRELAND).astimezone(UTC)
        if instant > through:
            break
        if instant in by_end and cursor.month % 2 == 1:
            d, n = by_end[instant]
            out.append((cursor, "day", float(int(d)), "Scheduled Reading (Actual) [simulated]"))
            out.append((cursor, "night", float(int(n)), "Scheduled Reading (Actual) [simulated]"))
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 14)
    return out
