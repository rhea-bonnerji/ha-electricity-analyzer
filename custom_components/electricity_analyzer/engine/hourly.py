"""Turn one hour of raw clamp statistics into one stored row.

NO HOME ASSISTANT IMPORTS. The Home Assistant side reads the recorder's compiled
hourly statistics for each Shelly entity and hands them here as plain dicts:

    {"energy": {"change": 1.23}, "power": {"mean": 512.0, "min": 380.0, "max": 910.0},
     "apparent_power": {"mean": 650.0}, "power_factor": {"mean": 0.79, "min": 0.71},
     "voltage": {"mean": 230.1, "min": 227.9, "max": 233.0}, "current": {"max": 4.1},
     "frequency": {"mean": 50.0}, "energy_returned": {"change": 0.0}}

THE TWO CLAMPS ARE NOT ADDED

The full-house clamp is the total. The hall clamp measures one circuit inside that
total. So `rest = house - hall`, never `house + hall`.

The hall clamp can read slightly MORE than the house clamp: two independent CTs each
carry a percent or two of error, and the hall circuit's loads (pumps, ballasts) have
a poor power factor that stresses that error most when the rest of the house is
nearly idle. That is a measurement artefact, not negative consumption, so:

* `rest_kwh` is clamped at zero;
* `hall_over_house` records that it happened, so it is visible rather than hidden;
* cost is ALLOCATED with the hall capped at the house figure, so hall cost + rest
  cost always equals the house's energy cost exactly. The raw hall kWh is kept as
  measured.

NO TARIFF, NO PRICE

An hour before the earliest tariff period keeps its kWh and its band (the meter's
clock does not depend on the price) but its money columns are left empty: a tariff is
never applied to hours before it began. Adding the earlier tariff re-prices them.

THE BILL IS THE METER'S

Money is computed on `meter_kwh`, because the supplier bills what the meter counted, not
what the clamp saw. Until ESB publishes the hour, that is the clamp's kWh times the meter
factor learned from real readings and from ESB (calibration.py): PROVISIONAL. Once ESB has
the hour, `meter_kwh` IS ESB's figure and the hour is re-priced on it: FINAL
(`meter_source` "esb"). The clamp's own kWh are always kept as measured.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from .hallfix import hour_factor
from .tariff import TariffSchedule, cost_hour, local_date

# Below this, two clamps disagreeing is just rounding.
_OVER_TOLERANCE_KWH = 0.001


def _get(stats: dict[str, dict[str, Any]] | None, measurement: str, key: str) -> float | None:
    if not stats:
        return None
    value = (stats.get(measurement) or {}).get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _energy(stats: dict[str, dict[str, Any]] | None) -> float | None:
    """kWh for the hour: the counter's change, else the mean power times one hour.

    The fallback covers an hour where the energy counter has no compiled statistic
    (the device was briefly unavailable, or the counter reset mid-hour and the
    recorder could not attribute it) but the power sensor did report.
    """
    change = _get(stats, "energy", "change")
    if change is not None and change >= 0:
        return change
    mean_w = _get(stats, "power", "mean")
    if mean_w is not None:
        return max(mean_w, 0.0) / 1000.0
    return None


def _round(value: float | None, places: int) -> float | None:
    return None if value is None else round(value, places)


def build_hour(
    hour_start: datetime,
    house: dict[str, dict[str, Any]] | None,
    hall: dict[str, dict[str, Any]] | None,
    schedule: TariffSchedule,
    *,
    factor: Callable[[datetime, str], float] | None = None,
    hall_model: dict[str, Any] | None = None,
    source: str = "shelly",
    meter_kwh: float | None = None,
) -> dict[str, Any] | None:
    """One row for `store.upsert_hours`, or None when the house clamp has nothing.

    `meter_kwh`: ESB's own figure for the hour, when it has it (final); otherwise the
    clamp times the learned factor (provisional).
    """
    house_kwh = _energy(house)
    if house_kwh is None:
        return None
    hall_kwh = _energy(hall)

    band = schedule.for_instant(hour_start).window.band_at(hour_start)
    f, meter, meter_source = _meter(house_kwh, meter_kwh, factor(hour_start, band) if factor else 1.0)
    cost = cost_hour(schedule, hour_start, meter)
    rate = cost.rate  # None before the first tariff period: such an hour is not priced

    rest_kwh = None
    hall_alloc = None
    over = False
    hall_factor = hour_factor(
        hall_model or {}, _get(hall, "power", "mean"), _get(hall, "apparent_power", "mean")
    )
    if hall_kwh is not None:
        # The flag records what the clamps MEASURED; the split uses the corrected hall,
        # corrected for THIS hour's power factor (hallfix.py).
        over = hall_kwh > house_kwh + _OVER_TOLERANCE_KWH
        hall_alloc = min(hall_kwh * hall_factor, house_kwh)
        rest_kwh = max(house_kwh - hall_kwh * hall_factor, 0.0)

    return {
        "hour_utc": hour_start,
        "band": cost.band,
        "house_kwh": _round(house_kwh, 4),
        "meter_kwh": _round(meter, 4),
        "meter_factor": round(f, 5),
        "meter_source": meter_source,
        "hall_kwh": _round(hall_kwh, 4),
        "hall_factor": round(hall_factor, 4),
        "rest_kwh": _round(rest_kwh, 4),
        "unit_rate_eur": cost.rate,
        "house_cost_eur": _round(cost.energy_cost, 6),
        "hall_cost_eur": _round(hall_alloc * f * rate if hall_alloc is not None and rate is not None else None, 6),
        "rest_cost_eur": _round(rest_kwh * f * rate if rest_kwh is not None and rate is not None else None, 6),
        "fixed_cost_eur": _round(cost.fixed_cost, 6),
        "house_avg_w": _round(_get(house, "power", "mean"), 1),
        "house_min_w": _round(_get(house, "power", "min"), 1),
        "house_max_w": _round(_get(house, "power", "max"), 1),
        "hall_avg_w": _round(_get(hall, "power", "mean"), 1),
        "hall_min_w": _round(_get(hall, "power", "min"), 1),
        "hall_max_w": _round(_get(hall, "power", "max"), 1),
        "house_avg_va": _round(_get(house, "apparent_power", "mean"), 1),
        "hall_avg_va": _round(_get(hall, "apparent_power", "mean"), 1),
        "house_pf": _round(_get(house, "power_factor", "mean"), 3),
        "house_pf_min": _round(_get(house, "power_factor", "min"), 3),
        "hall_pf": _round(_get(hall, "power_factor", "mean"), 3),
        "hall_pf_min": _round(_get(hall, "power_factor", "min"), 3),
        "voltage_avg": _round(_get(house, "voltage", "mean"), 2),
        "voltage_min": _round(_get(house, "voltage", "min"), 2),
        "voltage_max": _round(_get(house, "voltage", "max"), 2),
        "hall_voltage_avg": _round(_get(hall, "voltage", "mean"), 2),
        "hall_voltage_min": _round(_get(hall, "voltage", "min"), 2),
        "hall_voltage_max": _round(_get(hall, "voltage", "max"), 2),
        "house_current_avg": _round(_get(house, "current", "mean"), 2),
        "house_current_max": _round(_get(house, "current", "max"), 2),
        "hall_current_avg": _round(_get(hall, "current", "mean"), 2),
        "hall_current_max": _round(_get(hall, "current", "max"), 2),
        "frequency_avg": _round(_get(house, "frequency", "mean"), 3),
        "hall_frequency_avg": _round(_get(hall, "frequency", "mean"), 3),
        "house_returned_kwh": _round(_get(house, "energy_returned", "change"), 4),
        "hall_over_house": 1 if over else 0,
        "tariff_from": cost.tariff.valid_from.isoformat() if cost.tariff else None,
        "source": source,
    }


def _meter(house_kwh: float, meter_kwh: float | None, factor: float) -> tuple[float, float, str]:
    """(meter / clamp factor, meter kWh, 'esb' | 'clamp') for one hour."""
    if meter_kwh is None:
        return factor, house_kwh * factor, "clamp"
    ratio = meter_kwh / house_kwh if house_kwh > 1e-9 else 1.0
    return ratio, meter_kwh, "esb"


def book_fixed(rows: list[dict[str, Any]], schedule: TariffSchedule) -> list[dict[str, Any]]:
    """Each local day's fixed charge on its FIRST stored hour, nothing on the others.

    Normally that is the midnight hour, which `cost_hour` already books. When a day's
    data starts later (the first day of monitoring, Home Assistant down over midnight)
    the day still costs its full fixed charge: it goes on the earliest hour there is.
    Returns only the rows whose fixed cost changed.
    """
    by_day: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(row["local_date"], []).append(row)
    changed = []
    for _day, members in by_day.items():
        members.sort(key=lambda r: r["hour_utc"])
        first = members[0]["hour_utc"]
        tariff = schedule.in_force(local_date(first))
        for i, row in enumerate(members):
            if tariff is None:
                want = None
            else:
                want = round(tariff.fixed_for_day(local_date(first)), 6) if i == 0 else 0.0
            have = row.get("fixed_cost_eur")
            if (have is None) != (want is None) or (want is not None and abs(float(have) - want) > 1e-9):
                changed.append({**row, "fixed_cost_eur": want})
    return changed


def reprice(
    row: dict[str, Any],
    schedule: TariffSchedule,
    factor: Callable[[datetime, str], float] | None = None,
    hall_model: dict[str, Any] | None = None,
    meter_kwh: float | None = None,
) -> dict[str, Any]:
    """Recompute the money columns of a stored row under a new schedule, a new meter factor,
    or ESB's own figure for the hour (`meter_kwh`). An hour already final on ESB stays on
    ESB's figure when only the schedule or the factor changed."""
    start: datetime = row["hour_utc"]
    house_kwh = float(row.get("house_kwh") or 0.0)
    band = schedule.for_instant(start).window.band_at(start)
    if meter_kwh is None and row.get("meter_source") == "esb" and row.get("meter_kwh") is not None:
        meter_kwh = float(row["meter_kwh"])
    f, meter, meter_source = _meter(house_kwh, meter_kwh, factor(start, band) if factor else 1.0)
    cost = cost_hour(schedule, start, meter)
    rate = cost.rate
    hall_kwh = row.get("hall_kwh")
    out = dict(row)
    out["band"] = cost.band
    out["meter_kwh"] = round(meter, 4)
    out["meter_factor"] = round(f, 5)
    out["meter_source"] = meter_source
    out["unit_rate_eur"] = rate
    out["house_cost_eur"] = _round(cost.energy_cost, 6)
    out["fixed_cost_eur"] = _round(cost.fixed_cost, 6)
    if hall_kwh is not None:
        hall_factor = hour_factor(hall_model or {}, row.get("hall_avg_w"), row.get("hall_avg_va"))
        hall = float(hall_kwh) * hall_factor
        out["hall_factor"] = round(hall_factor, 4)
        out["rest_kwh"] = round(max(house_kwh - hall, 0.0), 4)
        out["hall_cost_eur"] = None if rate is None else round(min(hall, house_kwh) * f * rate, 6)
        out["rest_cost_eur"] = None if rate is None else round(max(house_kwh - hall, 0.0) * f * rate, 6)
    out["tariff_from"] = cost.tariff.valid_from.isoformat() if cost.tariff else None
    return out
