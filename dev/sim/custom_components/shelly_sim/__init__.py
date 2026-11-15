"""DEV-ONLY simulator of a Shelly EM Gen4 with two CT clamps.

Exists to demo the electricity_analyzer integration in a sandbox before real
hardware data is wired in. It creates the same devices and sensors, with the
same names, device classes and units, as the real Shelly integration does for
the real installation, drives them from a calibrated household load model, and
on first start backfills hourly long-term statistics so there is history to
analyse. Never install it on a real Home Assistant: the backfill writes
statistics under ordinary sensor entity_ids.

Enabled from configuration.yaml:

    shelly_sim:
      history_days: 400    # optional, 0 disables the backfill
      update_interval: 5   # optional, seconds
      seed: 42             # optional

Removing the block removes the simulated devices again (statistics are kept).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import (
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
    PARENT_NAME,
    SIM_MAC,
    SW_VERSION,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

_OPTIONS = vol.Schema(
    {
        vol.Optional(CONF_HISTORY_DAYS, default=DEFAULT_HISTORY_DAYS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=3650)
        ),
        vol.Optional(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=300)
        ),
        vol.Optional(CONF_SEED, default=DEFAULT_SEED): vol.Coerce(int),
    }
)

# `shelly_sim:` with nothing under it parses as None; treat it as all defaults.
CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.All(lambda value: value or {}, _OPTIONS)}, extra=vol.ALLOW_EXTRA
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Turn the YAML block into (or out of) the config entry."""
    if DOMAIN not in config:
        # YAML is the on/off switch for a dev tool: no block, no simulator.
        for entry in hass.config_entries.async_entries(DOMAIN):
            _LOGGER.info("shelly_sim removed from configuration.yaml; removing its entry")
            hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
        return True

    options: dict[str, Any] = dict(config[DOMAIN])
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=options
        )
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create the parent device, then the sensor platform."""
    _LOGGER.warning(
        "Shelly EM Gen4 SIMULATOR is running - every reading it produces is fake"
    )
    # The parent must exist before the channel sub-devices point at it with
    # via_device, so create it here rather than leave it to entity order.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, SIM_MAC)},
        name=PARENT_NAME,
        manufacturer=MANUFACTURER,
        model=MODEL,
        model_id=MODEL_ID,
        sw_version=SW_VERSION,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload; the runtime's timer and start-up task are tied to the entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
