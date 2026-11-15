"""Constants for the DEV-ONLY Shelly EM Gen4 simulator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)

DOMAIN: Final = "shelly_sim"

CONF_HISTORY_DAYS: Final = "history_days"
CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_SEED: Final = "seed"

DEFAULT_HISTORY_DAYS: Final = 400
DEFAULT_UPDATE_INTERVAL: Final = 5  # seconds
DEFAULT_SEED: Final = 42

# A fake MAC so identifiers look like the real integration's ("<mac>-em1:0").
# Deliberately in the locally-administered range so it can never collide with
# a real Shelly on the same Home Assistant.
SIM_MAC: Final = "02a5e1500001"

PARENT_NAME: Final = "Shelly EM Gen 4"
MANUFACTURER: Final = "Shelly"
MODEL: Final = "Shelly EM Gen4"
MODEL_ID: Final = "S4EM-002CXCEU"
SW_VERSION: Final = "1.7.1-sim"

# Storage key for the "history already imported" marker (.storage/shelly_sim.history).
STORE_KEY: Final = f"{DOMAIN}.history"
STORE_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class Channel:
    """One CT clamp, which Home Assistant shows as its own sub-device."""

    key: str  # model channel name
    rpc_key: str  # the Shelly RPC component it stands in for
    name: str  # device name; entity_ids derive from it


CHANNELS: Final = (
    Channel("house", "em1:0", "Full House Energy Monitor"),
    Channel("hall", "em1:1", "Hall Energy Monitor"),
)


@dataclass(frozen=True, slots=True)
class SensorSpec:
    """One of the eight sensors every channel exposes.

    Shared by the live entities and the statistics backfill, so the imported
    metadata can never disagree with what the entity itself reports.
    """

    key: str  # model quantity, or "energy" / "energy_returned"
    name: str  # entity name; with has_entity_name the id is <device>_<name>
    device_class: SensorDeviceClass
    state_class: SensorStateClass
    unit: str | None
    decimals: int  # native values are rounded to this many places


SENSOR_SPECS: Final = (
    SensorSpec("power", "Power", SensorDeviceClass.POWER,
               SensorStateClass.MEASUREMENT, UnitOfPower.WATT, 1),
    SensorSpec("apparent_power", "Apparent power", SensorDeviceClass.APPARENT_POWER,
               SensorStateClass.MEASUREMENT, UnitOfApparentPower.VOLT_AMPERE, 1),
    SensorSpec("power_factor", "Power factor", SensorDeviceClass.POWER_FACTOR,
               SensorStateClass.MEASUREMENT, None, 2),
    SensorSpec("voltage", "Voltage", SensorDeviceClass.VOLTAGE,
               SensorStateClass.MEASUREMENT, UnitOfElectricPotential.VOLT, 1),
    SensorSpec("current", "Current", SensorDeviceClass.CURRENT,
               SensorStateClass.MEASUREMENT, UnitOfElectricCurrent.AMPERE, 3),
    SensorSpec("energy", "Energy", SensorDeviceClass.ENERGY,
               SensorStateClass.TOTAL_INCREASING, UnitOfEnergy.KILO_WATT_HOUR, 3),
    SensorSpec("energy_returned", "Energy returned", SensorDeviceClass.ENERGY,
               SensorStateClass.TOTAL_INCREASING, UnitOfEnergy.KILO_WATT_HOUR, 3),
    SensorSpec("frequency", "Frequency", SensorDeviceClass.FREQUENCY,
               SensorStateClass.MEASUREMENT, UnitOfFrequency.HERTZ, 2),
)  # fmt: skip


def unique_id(channel: Channel, spec: SensorSpec) -> str:
    """Stable unique_id, shaped like the real integration's (<mac>-em1:0-power)."""
    return f"{SIM_MAC}-{channel.rpc_key}-{spec.key}"
