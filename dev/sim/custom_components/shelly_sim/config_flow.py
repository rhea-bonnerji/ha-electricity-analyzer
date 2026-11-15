"""Import-only config flow.

There is no UI flow: the simulator is switched on from configuration.yaml. A
config entry is still needed because Home Assistant only creates DEVICES for
entities that belong to one, and the real hardware shows up as a parent
device with two sub-devices - entity_ids such as
`sensor.full_house_energy_monitor_power` come from those device names.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN, PARENT_NAME


class ShellySimConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create (or update) the single entry from YAML."""

    VERSION = 1

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        # Changed YAML options update the entry and reload it.
        self._abort_if_unique_id_configured(updates=import_data)
        return self.async_create_entry(title=f"{PARENT_NAME} (simulated)", data=import_data)
