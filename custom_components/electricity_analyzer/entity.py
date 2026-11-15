"""Shared entity plumbing: one device, two data feeds."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import CONF_DASHBOARD_PATH, DEFAULT_DASHBOARD_PATH, DOMAIN, NAME, VERSION
from .runtime import Analyzer


def device_info(analyzer: Analyzer) -> DeviceInfo:
    path = str(analyzer.cfg.get(CONF_DASHBOARD_PATH) or DEFAULT_DASHBOARD_PATH).strip("/")
    return DeviceInfo(
        identifiers={(DOMAIN, analyzer.entry.entry_id)},
        name=NAME,
        manufacturer="Electricity Analyzer",
        model="Shelly EM Gen4 · ESB Networks · SSE Airtricity",
        sw_version=VERSION,
        entry_type=DeviceEntryType.SERVICE,
        # "Visit" on the device page opens the dashboard.
        configuration_url=f"homeassistant://{path}",
    )


class AnalyzerEntity(CoordinatorEntity[DataUpdateCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, analyzer: Analyzer, coordinator: DataUpdateCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self.analyzer = analyzer
        self._attr_unique_id = f"{analyzer.entry.entry_id}_{key}"
        self._attr_device_info = device_info(analyzer)
