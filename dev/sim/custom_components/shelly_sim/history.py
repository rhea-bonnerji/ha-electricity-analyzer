"""One-off backfill of hourly long-term statistics for the simulated sensors.

THE PROBLEM THIS SOLVES

A fresh sandbox has no history, and the analyzer being demoed needs months of
it. So on first start this imports `history_days` of hourly long-term
statistics for all 16 sensors - exactly what the recorder would have compiled
had the sensors been running all along - and then hands the energy counters'
final value to the live sensors so the Energy dashboard sees one continuous
counter.

HOW THE RECORDER CONTINUES A `sum` (and why hourly rows alone are not enough)

For a total_increasing sensor the recorder never looks at the hourly table when
it compiles new statistics. Every 5 minutes it takes the latest SHORT-TERM row
for the entity, and computes ``sum = last_sum + (state - last_state)``; each
hourly row is then copied from the last short-term row in that hour. With only
hourly rows imported there is no short-term row, the first live compile starts
a fresh ``sum`` at 0, and the dashboard shows a negative step of the whole
history at the join.

So besides the hourly rows this writes ONE short-term row per energy sensor -
the "anchor" - in the last 5-minute slot of the last imported hour, with
``state == sum ==`` the counter at the end of that hour, and only then lets the
live energy sensors report a number, starting from that same counter plus
whatever accrued since. The first live compile then continues the sum exactly.
Short-term rows can only be written through the recorder instance
(`async_import_statistics` always targets the hourly table), which is why that
one call reaches past the public helper.

ORDERING, BECAUSE THE RECORDER IS A QUEUE

Imports and compiles are jobs on the same FIFO queue, so correctness reduces to
queue order:

* The energy sensors stay `unknown` until the anchor is queued, so no compile
  can see a live energy number before the anchor exists.
* An imported hour must not reach the database before the recorder's own
  compile of that hour: the compile INSERTs (no upsert) and a duplicate would
  fail the whole 5-minute run. The compile of hour H is queued at H+1h+10s, so
  the last hour is never queued until 40 s after it ended. Imports are upserts,
  so arriving after a compile (which may hold a partial hour of live
  measurements) simply replaces it with the full modelled hour.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_short_term_statistics,
    get_last_statistics,
)
from homeassistant.components.sensor import SensorStateClass
from homeassistant.components.sensor.const import UNIT_CONVERTERS
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CHANNELS, SENSOR_SPECS, SensorSpec
from .model import ShellySimModel

_LOGGER = logging.getLogger(__name__)

# The recorder's own source name: these are entity statistics, not external
# ones, so they live under the entity_id and the recorder keeps compiling them.
RECORDER_SOURCE = "recorder"

# Generate and import a month at a time: bounded memory, progress in the log,
# and the recorder can be writing month N while the executor computes N+1.
CHUNK = timedelta(days=30)

# The recorder compiles hour H at H+1h+10s (5-minute task, second=10).
_COMPILE_CLEARANCE = timedelta(seconds=40)

# (channel key, sensor key) -> entity_id
StatisticIds = dict[tuple[str, str], str]


@dataclass(slots=True)
class BackfillResult:
    """What was imported, and the point the live counters continue from."""

    history_start: datetime
    anchor_ts: datetime  # end of the last imported hour
    anchor: dict[str, float]  # channel -> exact counter (kWh) at anchor_ts
    hours: int
    rows: int
    seconds: float


def _floor_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def metadata(spec: SensorSpec, statistic_id: str) -> StatisticMetaData:
    """Metadata identical to what the sensor recorder writes for this entity.

    unit_class is looked up the way homeassistant.components.sensor.recorder
    does it (device-class converter first). That matters for power_factor: with
    no unit it still gets unit_class "unitless" (UnitlessRatioConverter accepts
    None), and importing None instead would just be overwritten - with a
    metadata-changed round trip - at the first live compile.
    """
    converter = UNIT_CONVERTERS.get(spec.device_class)
    unit_class = (
        converter.UNIT_CLASS
        if converter is not None and spec.unit in converter.VALID_UNITS
        else None
    )
    has_sum = spec.state_class is SensorStateClass.TOTAL_INCREASING
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE if has_sum else StatisticMeanType.ARITHMETIC,
        has_sum=has_sum,
        name=None,
        source=RECORDER_SOURCE,
        statistic_id=statistic_id,
        unit_class=unit_class,
        unit_of_measurement=spec.unit,
    )


def _generate(
    model: ShellySimModel,
    stat_ids: StatisticIds,
    start: datetime,
    end: datetime,
    counters: dict[str, float],
) -> tuple[dict[str, list[StatisticData]], dict[str, float]]:
    """Hourly rows for [start, end). Runs in an executor thread.

    `counters` are the exact (unrounded) energy counters at `start`; the
    returned copy is at `end`. Rows carry them rounded to the sensor's 3
    decimals - the same rounding the live state uses, so the live value can
    never be below the last imported state (which would be logged as a dip).
    """
    counters = dict(counters)
    rows: dict[str, list[StatisticData]] = {sid: [] for sid in stat_ids.values()}
    t = start
    while t < end:
        hour = model.hourly(t)
        for channel in CHANNELS:
            stats = hour[channel.key]
            counters[channel.key] += stats["energy_kwh"]
            for spec in SENSOR_SPECS:
                if (sid := stat_ids.get((channel.key, spec.key))) is None:
                    continue
                if spec.key == "energy":
                    value = round(counters[channel.key], spec.decimals)
                    rows[sid].append({"start": t, "state": value, "sum": value})
                elif spec.key == "energy_returned":
                    rows[sid].append({"start": t, "state": 0.0, "sum": 0.0})
                else:
                    q = stats[spec.key]
                    rows[sid].append(
                        {
                            "start": t,
                            "mean": round(q["mean"], 4),
                            "min": round(q["min"], 4),
                            "max": round(q["max"], 4),
                        }
                    )
        t += timedelta(hours=1)
    return rows, counters


async def async_has_statistics(hass: HomeAssistant, statistic_ids: list[str]) -> bool:
    """True if any of these already has short- or long-term statistics.

    Short-term counts too: a sensor that has run for less than an hour has no
    hourly rows yet but does have a running sum the import would collide with.
    """

    def _check() -> bool:
        for sid in statistic_ids:
            for getter in (get_last_statistics, get_last_short_term_statistics):
                if getter(hass, 1, sid, False, {"sum"}):
                    return True
        return False

    return await get_instance(hass).async_add_executor_job(_check)


async def async_last_energy_state(
    hass: HomeAssistant, statistic_id: str
) -> tuple[float, datetime] | None:
    """Most recent recorded counter value and the end of its period, if any."""

    def _last() -> tuple[float, datetime] | None:
        best: tuple[float, datetime] | None = None
        for getter in (get_last_statistics, get_last_short_term_statistics):
            found = getter(hass, 1, statistic_id, False, {"state"})
            for row in found.get(statistic_id, []):
                end = row["end"]
                end_dt = dt_util.utc_from_timestamp(end) if isinstance(end, float) else end
                if row.get("state") is not None and (best is None or end_dt > best[1]):
                    best = (float(row["state"]), end_dt)
        return best

    return await get_instance(hass).async_add_executor_job(_last)


async def async_backfill(
    hass: HomeAssistant,
    model: ShellySimModel,
    stat_ids: StatisticIds,
    history_start: datetime,
    on_anchor: Callable[[datetime, dict[str, float]], None],
) -> BackfillResult:
    """Import hourly statistics from `history_start` to the last complete hour.

    `on_anchor(anchor_ts, counters)` is called synchronously, right after the
    anchor rows are queued, so the caller can start the live energy counters
    from exactly there (see the module docstring for why that order matters).
    Returns once the recorder has committed everything.
    """
    instance = get_instance(hass)
    began = time.monotonic()
    specs = {spec.key: spec for spec in SENSOR_SPECS}
    meta = {
        sid: metadata(specs[spec_key], sid) for (_, spec_key), sid in stat_ids.items()
    }
    counters = {channel.key: 0.0 for channel in CHANNELS}
    cursor = history_start
    hours = rows_total = 0

    while True:
        last_complete = _floor_hour(dt_util.utcnow())
        if cursor >= last_complete:
            break
        chunk_end = min(cursor + CHUNK, last_complete)
        rows, counters = await hass.async_add_executor_job(
            _generate, model, stat_ids, cursor, chunk_end, counters
        )
        # Never queue an hour before the recorder has queued its own compile
        # of it. Only ever bites if the final chunk lands just after the hour.
        if (wait := chunk_end + _COMPILE_CLEARANCE - dt_util.utcnow()).total_seconds() > 0:
            _LOGGER.debug("Waiting %.0fs for the recorder to compile the last hour", wait.total_seconds())
            await asyncio.sleep(wait.total_seconds())
        for sid, sid_rows in rows.items():
            if sid_rows:
                async_import_statistics(hass, meta[sid], sid_rows)
                rows_total += len(sid_rows)
        hours += int((chunk_end - cursor).total_seconds() // 3600)
        _LOGGER.debug(
            "Queued %s .. %s (%d hours so far, house %.1f kWh, hall %.1f kWh)",
            cursor, chunk_end, hours, counters["house"], counters["hall"],
        )
        cursor = chunk_end
        # Hours may have completed while this chunk was generated; the loop
        # picks them up until nothing complete is left.

    anchor_ts = cursor
    anchor_start = anchor_ts - StatisticsShortTerm.duration
    for channel in CHANNELS:
        for spec_key, value in (("energy", counters[channel.key]), ("energy_returned", 0.0)):
            if (sid := stat_ids.get((channel.key, spec_key))) is None:
                continue
            state = round(value, specs[spec_key].decimals)
            anchor_row: StatisticData = {"start": anchor_start, "state": state, "sum": state}
            instance.async_import_statistics(meta[sid], [anchor_row], StatisticsShortTerm)
    on_anchor(anchor_ts, dict(counters))

    await instance.async_block_till_done()
    return BackfillResult(
        history_start=history_start,
        anchor_ts=anchor_ts,
        anchor=counters,
        hours=hours,
        rows=rows_total,
        seconds=time.monotonic() - began,
    )


def describe(result: BackfillResult) -> dict[str, Any]:
    """JSON-safe summary for the storage marker and the log."""
    return {
        "history_start": result.history_start.isoformat(),
        "anchor_ts": result.anchor_ts.isoformat(),
        "anchor": result.anchor,
        "hours": result.hours,
        "rows": result.rows,
        "seconds": round(result.seconds, 1),
    }
