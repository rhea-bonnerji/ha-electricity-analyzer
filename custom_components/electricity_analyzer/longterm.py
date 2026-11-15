"""Publish the hourly store as external long-term statistics.

The Shelly entities already have statistics of their own. What they cannot have is
anything that needs the tariff, both clamps, or ESB: the day/night split, money, the
rest of the house, ESB's figures, the estimated meter registers. Those are published
here, as `electricity_analyzer:<key>`, so Home Assistant's own statistics-graph card
can chart them over ANY date range the dashboard's date picker selects, and so they
outlive the integration's own database if it is ever deleted.

SUMS ARE CUMULATIVE

Energy and money are published as running sums from the first stored hour, which is
what the recorder expects (`change` per bucket is computed from it). So a revision to
any hour changes every later sum, and the publisher checks the newest published sum
against its own before deciding whether appending is enough or the series has to be
rewritten from the start. That makes a late-compiled hour, a re-priced tariff or an
ESB revision self-healing at any depth.

LEVELS ARE NOT

The registers are published as hourly mean/min/max of the register value at the end
of each hour (one value, so all three agree), and the rest-of-house power as its
hourly mean.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN, STATISTICS, stat_id

LOG = logging.getLogger(__name__)
UTC = timezone.utc
HOUR = timedelta(hours=1)
TOLERANCE = 1e-4
OVERLAP = timedelta(hours=48)

_UNIT_CLASS = {"kWh": "energy", "W": "power", "EUR": None, "°C": "temperature", "km/h": "speed", "hPa": "pressure"}


def _meta(key: str) -> StatisticMetaData:
    name, kind, unit = STATISTICS[key]
    return StatisticMetaData(
        mean_type=StatisticMeanType.ARITHMETIC if kind == "level" else StatisticMeanType.NONE,
        has_sum=kind != "level",
        name=name,
        source=DOMAIN,
        statistic_id=stat_id(key),
        unit_class=_UNIT_CLASS.get(unit),
        unit_of_measurement=unit,
    )


def build_series(
    hours: list[dict[str, Any]],
    esb: list[tuple[datetime, float, str]],
    registers: list[tuple[datetime, float, float]],
) -> dict[str, list[tuple[datetime, float]]]:
    """key -> [(hour_start, value for that hour)] (per-hour amounts, or levels)."""
    s: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for r in hours:
        h = r["hour_utc"]
        kwh = float(r.get("house_kwh") or 0.0)
        hall = float(r.get("hall_kwh") or 0.0) * float(r.get("hall_factor") or 1.0)
        night = r.get("band") == "night"
        cost = float(r.get("house_cost_eur") or 0.0)
        fixed = float(r.get("fixed_cost_eur") or 0.0)
        s["house_energy_day"].append((h, 0.0 if night else kwh))
        s["house_energy_night"].append((h, kwh if night else 0.0))
        s["cost_day"].append((h, 0.0 if night else cost))
        s["cost_night"].append((h, cost if night else 0.0))
        s["cost_fixed"].append((h, fixed))
        s["cost_total"].append((h, cost + fixed))
        meter = r.get("meter_kwh")
        s["meter_energy"].append((h, float(meter) if meter is not None else kwh))
        s["cost_energy"].append((h, cost))
        if r.get("hall_kwh") is not None:
            # Only hours the hall clamp measured: an hour filled from ESB's meter data has
            # no hall/rest split, which is "no data", not "the hall used nothing".
            s["hall_energy"].append((h, hall))
            s["rest_energy"].append((h, float(r.get("rest_kwh") or 0.0)))
            s["hall_energy_day"].append((h, 0.0 if night else hall))
            s["hall_energy_night"].append((h, hall if night else 0.0))
            s["hall_cost"].append((h, float(r.get("hall_cost_eur") or 0.0)))
            s["rest_cost"].append((h, float(r.get("rest_cost_eur") or 0.0)))
        if r.get("house_avg_w") is not None and r.get("hall_avg_w") is not None:
            s["rest_power"].append((h, max(float(r["house_avg_w"]) - float(r["hall_avg_w"]), 0.0)))

    by_hour: dict[datetime, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for start, kwh, band in esb:
        acc = by_hour[start.replace(minute=0, second=0, microsecond=0)]
        acc[0] += kwh
        acc[1 if band != "night" else 2] += kwh
    for h in sorted(by_hour):
        total, day, night = by_hour[h]
        s["esb_energy"].append((h, total))
        s["esb_energy_day"].append((h, day))
        s["esb_energy_night"].append((h, night))

    for end, day, night in registers:
        s["register_day"].append((end - HOUR, day))
        s["register_night"].append((end - HOUR, night))
    return s


# weather column -> statistic key, for the levels
_WEATHER_LEVELS = {
    "temperature": "outdoor_temperature",
    "apparent_temperature": "apparent_temperature",
    "humidity": "humidity",
    "wind_speed": "wind_speed",
    "wind_gusts": "wind_gusts",
    "cloud_cover": "cloud_cover",
    "solar_radiation": "solar_radiation",
    "pressure": "pressure",
}


def build_weather_series(rows: list[dict[str, Any]], now: datetime, base: float) -> dict[str, list[tuple[datetime, float]]]:
    """Weather statistics for hours that have happened (forecast hours are never published)."""
    s: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for r in rows:
        h = r["hour_utc"]
        if h + HOUR > now:
            continue
        for column, key in _WEATHER_LEVELS.items():
            if r.get(column) is not None:
                s[key].append((h, float(r[column])))
        if r.get("precipitation") is not None:
            s["precipitation"].append((h, float(r["precipitation"])))
        if r.get("temperature") is not None:
            s["heating_degree_days"].append((h, max(0.0, base - float(r["temperature"])) / 24))
        if r.get("apparent_temperature") is not None:
            s["heating_degree_days_apparent"].append((h, max(0.0, base - float(r["apparent_temperature"])) / 24))
    return s


async def _last(hass: HomeAssistant, key: str) -> dict[str, Any] | None:
    sid = stat_id(key)
    kind = STATISTICS[key][1]
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, sid, False, {"sum"} if kind != "level" else {"mean"}
    )
    series = rows.get(sid) or []
    return series[0] if series else None


async def async_publish(
    hass: HomeAssistant,
    series: dict[str, list[tuple[datetime, float]]],
    *,
    full: bool = False,
    keys: set[str] | None = None,
) -> dict[str, int]:
    """Publish what changed. Returns rows written per key."""
    written: dict[str, int] = {}
    for key in STATISTICS:
        if keys is not None and key not in keys:
            continue
        points = series.get(key) or []
        if not points:
            continue
        kind = STATISTICS[key][1]
        rows: list[StatisticData] = []
        if kind == "level":
            start_from = None
            if not full:
                last = await _last(hass, key)
                if last is not None:
                    start_from = datetime.fromtimestamp(last["start"], UTC) - OVERLAP
            for h, v in points:
                if start_from is None or h >= start_from:
                    rows.append(StatisticData(start=h, mean=v, min=v, max=v))
        else:
            cumulative = []
            total = 0.0
            for h, v in points:
                total += v
                cumulative.append((h, v, total))
            start_from = None
            if not full:
                last = await _last(hass, key)
                if last is not None:
                    last_start = datetime.fromtimestamp(last["start"], UTC)
                    ours = next((c for h, _, c in cumulative if h == last_start), None)
                    if ours is not None and abs(ours - float(last.get("sum") or 0.0)) <= TOLERANCE * max(1.0, abs(ours)):
                        start_from = last_start - OVERLAP
                    else:
                        LOG.debug("%s: published sum diverged, rewriting the whole series", key)
            for h, v, c in cumulative:
                if start_from is None or h >= start_from:
                    rows.append(StatisticData(start=h, state=v, sum=c))
        if rows:
            async_add_external_statistics(hass, _meta(key), rows)
        written[key] = len(rows)
    return written
