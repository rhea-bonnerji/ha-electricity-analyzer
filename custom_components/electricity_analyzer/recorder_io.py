"""Read the Shelly entities' statistics out of Home Assistant's recorder.

The recorder compiles every `measurement` and `total_increasing` sensor into hourly
long-term statistics (kept forever) and 5-minute short-term statistics (kept ten
days by default). That is the source for the hourly store: it already survives
restarts and outages, and it is what the Energy dashboard itself reads, so the two
can never disagree about an hour.

Units are normalised on the way out (`units=`), so an energy sensor reporting Wh or
a power sensor in kW is read in kWh and W like everything else.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant

from .const import (
    M_APPARENT,
    M_CURRENT,
    M_ENERGY,
    M_FREQUENCY,
    M_PF,
    M_POWER,
    M_RETURNED,
    M_VOLTAGE,
)

LOG = logging.getLogger(__name__)
UTC = timezone.utc

UNITS = {
    "energy": "kWh",
    "power": "W",
    "apparent_power": "VA",
    "voltage": "V",
    "electric_current": "A",
    "frequency": "Hz",
}

# Which statistic columns each measurement contributes to an hourly row.
TYPES: dict[str, set[str]] = {
    M_ENERGY: {"change", "state"},
    M_RETURNED: {"change"},
    M_POWER: {"mean", "min", "max"},
    M_APPARENT: {"mean"},
    M_PF: {"mean", "min"},
    M_VOLTAGE: {"mean", "min", "max"},
    M_CURRENT: {"mean", "max"},
    M_FREQUENCY: {"mean"},
}

CHUNK = timedelta(days=31)


async def _during(
    hass: HomeAssistant,
    start: datetime,
    end: datetime,
    ids: set[str],
    period: str,
    types: set[str],
) -> dict[str, list[dict[str, Any]]]:
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, ids, period, UNITS, types
    )


async def hourly(
    hass: HomeAssistant,
    entities: dict[str, dict[str, str]],
    start: datetime,
    end: datetime,
) -> dict[datetime, dict[str, dict[str, dict[str, float]]]]:
    """{hour_start: {channel: {measurement: {type: value}}}} for hours in [start, end).

    Read in month-sized chunks so a first backfill of a year of history does not ask
    the recorder for one enormous result set.
    """
    lookup: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for channel, mapping in entities.items():
        for measurement, entity_id in mapping.items():
            if entity_id and measurement in TYPES:
                lookup[entity_id].append((channel, measurement))
    if not lookup:
        return {}
    types = set().union(*TYPES.values())
    out: dict[datetime, dict[str, dict[str, dict[str, float]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + CHUNK, end)
        result = await _during(hass, cursor, chunk_end, set(lookup), "hour", types)
        for entity_id, rows in result.items():
            for row in rows:
                hour = datetime.fromtimestamp(row["start"], UTC)
                for channel, measurement in lookup.get(entity_id, []):
                    out[hour][channel][measurement] = {
                        t: row.get(t) for t in TYPES[measurement] if row.get(t) is not None
                    }
        cursor = chunk_end
    return out


async def first_hour(hass: HomeAssistant, entity_id: str) -> datetime | None:
    """Start of the oldest hourly statistic the recorder holds for an entity."""
    rows = await _during(
        hass,
        datetime(2000, 1, 1, tzinfo=UTC),
        datetime.now(UTC),
        {entity_id},
        "month",
        {"change"},
    )
    series = rows.get(entity_id) or []
    if not series:
        return None
    month_start = datetime.fromtimestamp(series[0]["start"], UTC)
    # Narrow down from the month to the hour.
    detail = await _during(hass, month_start, month_start + timedelta(days=32), {entity_id}, "hour", {"change"})
    hours = detail.get(entity_id) or []
    return datetime.fromtimestamp(hours[0]["start"], UTC) if hours else month_start


async def last_hour(hass: HomeAssistant, entity_id: str) -> datetime | None:
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, entity_id, True, {"state"}
    )
    series = rows.get(entity_id) or []
    return datetime.fromtimestamp(series[0]["start"], UTC) if series else None


async def five_minute_change(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> float | None:
    """kWh between two instants from 5-minute statistics, or None if not retained.

    Used for the meter-reading anchor's own hour: a reading taken at 18:50:34 should
    count only what was used after it, and 5-minute resolution gets within a few
    watt-hours of that instead of assuming the hour was used evenly.
    """
    aligned = start.replace(second=0, microsecond=0) - timedelta(minutes=start.minute % 5)
    rows = (await _during(hass, aligned, end, {entity_id}, "5minute", {"change"})).get(entity_id) or []
    if not rows:
        return None
    total = 0.0
    for row in rows:
        s = datetime.fromtimestamp(row["start"], UTC)
        e = s + timedelta(minutes=5)
        change = row.get("change")
        if change is None:
            continue
        overlap = (min(e, end) - max(s, start)).total_seconds()
        if overlap > 0:
            total += change * overlap / 300
    return total


async def counter_at(hass: HomeAssistant, entity_id: str, instant: datetime) -> float | None:
    """The counter's value at `instant` (the end of the 5-minute bucket before it)."""
    rows = (
        await _during(hass, instant - timedelta(hours=3), instant, {entity_id}, "5minute", {"state"})
    ).get(entity_id) or []
    for row in reversed(rows):
        end = datetime.fromtimestamp(row["start"], UTC) + timedelta(minutes=5)
        if end <= instant and row.get("state") is not None:
            return float(row["state"])
    rows = (
        await _during(hass, instant - timedelta(days=2), instant, {entity_id}, "hour", {"state"})
    ).get(entity_id) or []
    for row in reversed(rows):
        end = datetime.fromtimestamp(row["start"], UTC) + timedelta(hours=1)
        if end <= instant and row.get("state") is not None:
            return float(row["state"])
    return None
