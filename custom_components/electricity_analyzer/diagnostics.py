"""Diagnostics, with every credential and account identifier removed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ESB_PASSWORD,
    CONF_ESB_USERNAME,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_MPRN,
    CONF_SSE_ACCOUNT,
    CONF_SSE_PASSWORD,
    CONF_SSE_USERNAME,
)

REDACT = {
    CONF_ESB_PASSWORD,
    CONF_ESB_USERNAME,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_MPRN,
    CONF_SSE_ACCOUNT,
    CONF_SSE_PASSWORD,
    CONF_SSE_USERNAME,
    "mprn",
    "meter_number",
    "premises_id",
    "payload",
    "response",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    analyzer = entry.runtime_data
    insights = analyzer.insights_coordinator.data or {}
    live = analyzer.live_coordinator.data or {}
    return async_redact_data(
        {
            "config": dict(entry.data),
            "entities": analyzer.entities,
            "store": insights.get("store"),
            "tariffs": insights.get("tariffs"),
            "esb": insights.get("esb"),
            "sse": insights.get("sse"),
            "reconcile": {k: v for k, v in (insights.get("reconcile") or {}).items() if k != "per_day"},
            "forecast": insights["forecast"].summary() if insights.get("forecast") else None,
            "live": {k: v for k, v in live.items() if k not in ("tariff",)},
            "collection": insights.get("collection"),
            "energy_dashboard": analyzer.energy_status,
            "initial_load": analyzer.initial_load,
            "weather": analyzer.weather_status,
        },
        REDACT,
    )
