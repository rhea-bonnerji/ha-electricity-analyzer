"""The 16 simulated sensors and the runtime that drives them.

One `SimRuntime` per config entry owns the model, the latest reading and the
two energy counters; the entities are thin views onto it. Every
`update_interval` seconds the runtime samples the model, advances the counters
and writes all entity states in one go - the same cadence a Shelly pushes
status updates at.

ENERGY COUNTERS

Both channels' Energy sensors are counters the model defines as a pure
function of time: counter(t) = counter(t0) + energy_kwh(t0, t). That is what
makes every start-up path below land on the same, never-decreasing value:

* first start   - the history backfill imports the counter up to the last
                  complete hour; live continues from that anchor.
* restart       - RestoreSensor gives back (counter, timestamp); the counter is
                  fast-forwarded over the downtime, because a real Shelly keeps
                  counting while Home Assistant is down. The energy used while
                  HA was down therefore lands in the first hour after it comes
                  back, exactly as it would with the real device.
* restore lost  - fall back to the anchor stored in the marker file.

Until one of those has run the Energy sensors report `unknown` (never 0, which
the recorder would read as a meter reset).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorEntity,
    SensorExtraStoredData,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import history
from .const import (
    CHANNELS,
    CONF_HISTORY_DAYS,
    CONF_SEED,
    CONF_UPDATE_INTERVAL,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SEED,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    MODEL_ID,
    SENSOR_SPECS,
    SIM_MAC,
    STORE_KEY,
    STORE_VERSION,
    Channel,
    SensorSpec,
    unique_id,
)
from .model import ShellySimModel

_LOGGER = logging.getLogger(__package__)

# Long enough for a slow first start; if the Energy entities never appear they
# have been disabled, and the measurement sensors keep running regardless.
_REGISTRATION_TIMEOUT_S = 120

type ShellySimConfigEntry = ConfigEntry[SimRuntime]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ShellySimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the runtime and its 16 sensors."""
    runtime = SimRuntime(hass, entry)
    entry.runtime_data = runtime
    # The channel sub-devices hang off the parent __init__ created, linked by
    # device id - the way the real Shelly integration links em1 channels
    # (`via_device` by identifier is deprecated as of 2026.x).
    parent_id = dr.async_get_device_id_by_identifier(
        hass, (DOMAIN, SIM_MAC), config_entry_id=entry.entry_id
    )
    async_add_entities(
        (SimEnergySensor if spec.key == "energy" else SimSensor)(
            runtime, channel, spec, parent_id
        )
        for channel in CHANNELS
        for spec in SENSOR_SPECS
    )
    runtime.async_start()


class SimRuntime:
    """Model, readings, counters and the update timer for one entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        opts = entry.data
        self.history_days = int(opts.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS))
        self.interval = timedelta(
            seconds=float(opts.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        )
        self.model = ShellySimModel(int(opts.get(CONF_SEED, DEFAULT_SEED)))
        self.reading = self.model.sample(dt_util.utcnow())
        # None until a start-up path has placed them (see module docstring).
        self.counters: dict[str, float] | None = None
        self.counter_ts: datetime | None = None
        self._entities: set[SimSensor] = set()
        self._restored: dict[str, tuple[float, datetime] | None] = {}
        self._energy_registered = asyncio.Event()
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)

    # ------------------------------------------------------------- lifecycle

    @callback
    def async_start(self) -> None:
        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._async_tick, self.interval, name=f"{DOMAIN} tick"
            )
        )
        # Background: a 400-day backfill takes a while and must not hold up
        # Home Assistant's start-up. Cancelled automatically on unload.
        self.entry.async_create_background_task(
            self.hass, self._async_startup(), f"{DOMAIN} startup"
        )

    @callback
    def register(self, entity: SimSensor) -> None:
        self._entities.add(entity)

    @callback
    def unregister(self, entity: SimSensor) -> None:
        self._entities.discard(entity)

    @callback
    def register_restore(self, channel: str, restored: tuple[float, datetime] | None) -> None:
        """Called by each Energy sensor once it has read its restore data."""
        self._restored[channel] = restored
        if len(self._restored) == len(CHANNELS):
            self._energy_registered.set()

    # ---------------------------------------------------------------- ticks

    @callback
    def _async_tick(self, _now: datetime | None = None) -> None:
        now = dt_util.utcnow()
        self.reading = self.model.sample(now)
        if self.counters is not None and self.counter_ts is not None:
            delta = self.model.energy_kwh(self.counter_ts, now)
            for key in self.counters:
                self.counters[key] += delta[key]
            self.counter_ts = now
        for entity in self._entities:
            entity.async_write_ha_state()

    @callback
    def _set_counters(self, at: datetime, counters: dict[str, float]) -> None:
        """Place the counters at `at` and catch them up to now."""
        now = dt_util.utcnow()
        delta = self.model.energy_kwh(at, now) if now > at else {k: 0.0 for k in counters}
        self.counters = {k: v + delta[k] for k, v in counters.items()}
        self.counter_ts = max(now, at)
        _LOGGER.debug(
            "Energy counters live: %s",
            {k: round(v, 3) for k, v in self.counters.items()},
        )
        for entity in self._entities:
            if isinstance(entity, SimEnergySensor):
                entity.async_write_ha_state()

    # ------------------------------------------------------------- start-up

    def _statistic_ids(self) -> history.StatisticIds:
        """entity_ids by (channel, sensor), via the registry (users can rename)."""
        registry = er.async_get(self.hass)
        ids: history.StatisticIds = {}
        for channel in CHANNELS:
            for spec in SENSOR_SPECS:
                if entity_id := registry.async_get_entity_id(
                    "sensor", DOMAIN, unique_id(channel, spec)
                ):
                    ids[(channel.key, spec.key)] = entity_id
        return ids

    async def _async_startup(self) -> None:
        try:
            async with asyncio.timeout(_REGISTRATION_TIMEOUT_S):
                await self._energy_registered.wait()
        except TimeoutError:
            _LOGGER.warning(
                "Energy sensors did not come up (disabled?); energy stays unknown"
            )
            return

        marker = await self._store.async_load() or {}
        stat_ids = self._statistic_ids()
        # Only the import counters: they are `unknown` until placed, so they
        # cannot have statistics of their own yet on a fresh start. Energy
        # returned is a live 0 from the first second and may already have
        # been compiled by the time this runs.
        energy_ids = [sid for (_, key), sid in stat_ids.items() if key == "energy"]
        state = marker.get("state")

        if state == "importing":
            # A previous backfill was interrupted. Imports are upserts, so
            # simply run it again over the same window.
            _LOGGER.warning("Previous history import was interrupted; redoing it")
            start = dt_util.parse_datetime(marker["history_start"])
            assert start is not None
            await self._async_backfill(stat_ids, start)
            return

        if state is None and self.history_days > 0:
            if not await get_recorder_ready(self.hass):
                _LOGGER.error("Recorder not available; cannot import history")
            elif await history.async_has_statistics(self.hass, energy_ids):
                _LOGGER.info(
                    "Energy statistics already exist; not importing history. To"
                    " rebuild, delete the statistics and .storage/%s", STORE_KEY
                )
                marker = await self._async_marker_from_statistics(stat_ids)
            else:
                start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
                await self._async_backfill(stat_ids, start - timedelta(days=self.history_days))
                return
        elif state is None:
            marker = {"state": "skipped", "reason": "history_days is 0"}

        await self._async_resume(marker)

    async def _async_backfill(self, stat_ids: history.StatisticIds, start: datetime) -> None:
        if not await get_recorder_ready(self.hass):
            _LOGGER.error("Recorder not available; cannot import history")
            return
        await self._store.async_save(
            {"state": "importing", "history_start": start.isoformat(), "seed": self.model.seed}
        )
        _LOGGER.info(
            "Importing hourly statistics for %d sensors from %s (%d days)",
            len(stat_ids), start, self.history_days,
        )
        result = await history.async_backfill(
            self.hass, self.model, stat_ids, start, self._set_counters
        )
        summary = history.describe(result)
        await self._store.async_save({"state": "done", "seed": self.model.seed, **summary})
        _LOGGER.info("History import complete: %s", summary)

    async def _async_marker_from_statistics(
        self, stat_ids: history.StatisticIds
    ) -> dict[str, Any]:
        """Record where existing statistics leave the counters, as a fallback."""
        anchor: dict[str, float] = {}
        anchor_ts: datetime | None = None
        for channel in CHANNELS:
            if sid := stat_ids.get((channel.key, "energy")):
                if last := await history.async_last_energy_state(self.hass, sid):
                    anchor[channel.key], anchor_ts = last
        marker: dict[str, Any] = {"state": "skipped", "reason": "statistics existed"}
        if anchor_ts is not None and len(anchor) == len(CHANNELS):
            marker |= {"anchor_ts": anchor_ts.isoformat(), "anchor": anchor}
        await self._store.async_save(marker)
        return marker

    async def _async_resume(self, marker: dict[str, Any]) -> None:
        """Normal start: restore and fast-forward over the downtime."""
        now = dt_util.utcnow()
        anchor_ts = dt_util.parse_datetime(marker.get("anchor_ts") or "")
        bases: dict[str, tuple[float, datetime]] = {}
        for channel in CHANNELS:
            if restored := self._restored.get(channel.key):
                bases[channel.key] = restored
            elif anchor_ts is not None and channel.key in marker.get("anchor", {}):
                bases[channel.key] = (float(marker["anchor"][channel.key]), anchor_ts)
            else:
                bases[channel.key] = (0.0, now)

        def _fast_forward() -> dict[str, float]:
            # Long downtimes cost ~30 us per minute of model time: executor.
            # One integration per distinct start (normally both channels share it).
            spans = {ts: self.model.energy_kwh(ts, now) for _, ts in bases.values()}
            return {key: value + spans[ts][key] for key, (value, ts) in bases.items()}

        counters = await self.hass.async_add_executor_job(_fast_forward)
        oldest = min(ts for _, ts in bases.values())
        if now - oldest > 2 * self.interval:
            _LOGGER.info(
                "Fast-forwarded energy counters over the %s since they were last saved: %s",
                now - oldest, {k: round(counters[k] - bases[k][0], 3) for k in counters},
            )
        self._set_counters(now, counters)
        # Counters are a pure function of time from any (time, value) pair, so
        # today's is as valid an anchor as the import's - and a lost restore
        # file then only has to fast-forward from the last start.
        await self._store.async_save(
            marker | {"anchor_ts": now.isoformat(), "anchor": counters}
        )


async def get_recorder_ready(hass: HomeAssistant) -> bool:
    """Wait for the recorder's database to be usable."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415

    return await get_instance(hass).async_db_ready


# ----------------------------------------------------------------- entities


@dataclass
class _EnergyStoredData(SensorExtraStoredData):
    """SensorExtraStoredData plus the exact counter and when it was valid.

    The state itself is rounded to 3 decimals and its last_updated only moves
    when that rounded value changes, so neither is precise enough to resume an
    exact counter from.
    """

    counter_kwh: float | None = None
    counter_ts: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return super().as_dict() | {
            "counter_kwh": self.counter_kwh,
            "counter_ts": self.counter_ts,
        }


class SimSensor(SensorEntity):
    """One reading of one channel. State comes from the runtime, never polled."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, runtime: SimRuntime, channel: Channel, spec: SensorSpec, parent_id: str
    ) -> None:
        self._runtime = runtime
        self._channel = channel
        self._spec = spec
        self._attr_unique_id = unique_id(channel, spec)
        self._attr_name = spec.name
        self._attr_device_class = spec.device_class
        self._attr_state_class = spec.state_class
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_suggested_display_precision = min(spec.decimals, 2)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{SIM_MAC}-{channel.rpc_key}")},
            name=channel.name,
            manufacturer=MANUFACTURER,
            model=MODEL,
            model_id=MODEL_ID,
            via_device_id=parent_id,
        )

    @property
    def native_value(self) -> float | None:
        if self._spec.key == "energy_returned":
            return 0.0  # nothing is ever exported
        return round(self._runtime.reading[self._channel.key][self._spec.key], self._spec.decimals)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.register(self)

    async def async_will_remove_from_hass(self) -> None:
        self._runtime.unregister(self)
        await super().async_will_remove_from_hass()


class SimEnergySensor(SimSensor, RestoreSensor):
    """The channel's import counter (kWh), restored across restarts."""

    @property
    def native_value(self) -> float | None:
        counters = self._runtime.counters
        if counters is None:
            return None
        return round(counters[self._channel.key], self._spec.decimals)

    @property
    def extra_restore_state_data(self) -> _EnergyStoredData:
        counters, ts = self._runtime.counters, self._runtime.counter_ts
        return _EnergyStoredData(
            self.native_value,
            self.native_unit_of_measurement,
            counters[self._channel.key] if counters is not None else None,
            ts.isoformat() if ts is not None else None,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored: tuple[float, datetime] | None = None
        if (extra := await self.async_get_last_extra_data()) is not None:
            data = extra.as_dict()
            ts = dt_util.parse_datetime(data.get("counter_ts") or "")
            if data.get("counter_kwh") is not None and ts is not None:
                restored = (float(data["counter_kwh"]), ts)
            elif (last := await self.async_get_last_state()) is not None:
                # Restore data from something other than this class: best effort.
                try:
                    restored = (float(last.state), last.last_updated)
                except ValueError:
                    restored = None
        self._runtime.register_restore(self._channel.key, restored)
