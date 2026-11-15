"""Websocket commands behind the dashboard's data card.

    electricity_analyzer/table     {period, start?, end?}  -> columns, rows, totals
    electricity_analyzer/readings  {}                      -> meter reading log
    electricity_analyzer/summary   {}                      -> headline figures

Read-only, and only for signed-in users (the websocket API is authenticated).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .engine.tables import PERIODS


def _analyzer(hass: HomeAssistant):
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data
    return None


def _date(value: Any) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_table)
    websocket_api.async_register_command(hass, ws_readings)
    websocket_api.async_register_command(hass, ws_summary)
    websocket_api.async_register_command(hass, ws_minutes)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/table",
        vol.Optional("period", default="day"): vol.In(PERIODS),
        vol.Optional("start"): vol.Any(None, str),
        vol.Optional("end"): vol.Any(None, str),
    }
)
@websocket_api.async_response
async def ws_table(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    analyzer = _analyzer(hass)
    if analyzer is None:
        connection.send_error(msg["id"], "not_loaded", "Electricity Analyzer is not loaded")
        return
    try:
        start, end = _date(msg.get("start")), _date(msg.get("end"))
    except ValueError as exc:
        connection.send_error(msg["id"], "invalid_format", str(exc))
        return
    if start and end and start > end:
        start, end = end, start
    result = await analyzer.async_table(msg["period"], start, end)
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/readings"})
@websocket_api.async_response
async def ws_readings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    analyzer = _analyzer(hass)
    if analyzer is None:
        connection.send_error(msg["id"], "not_loaded", "Electricity Analyzer is not loaded")
        return
    connection.send_result(msg["id"], await analyzer.async_submissions())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/minutes",
        vol.Required("start"): str,
        vol.Optional("end"): str,
    }
)
@websocket_api.async_response
async def ws_minutes(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Minute rows between two instants (at most three days at a time)."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    analyzer = _analyzer(hass)
    if analyzer is None:
        connection.send_error(msg["id"], "not_loaded", "Electricity Analyzer is not loaded")
        return
    start = dt_util.parse_datetime(msg["start"])
    end = dt_util.parse_datetime(msg["end"]) if msg.get("end") else dt_util.utcnow()
    if start is None or end is None:
        connection.send_error(msg["id"], "invalid_format", "start/end must be ISO date-times")
        return
    if end - start > timedelta(days=3):
        start = end - timedelta(days=3)
    connection.send_result(msg["id"], {"rows": await analyzer.async_minutes(start, end)})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/summary"})
@websocket_api.async_response
async def ws_summary(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    analyzer = _analyzer(hass)
    if analyzer is None:
        connection.send_error(msg["id"], "not_loaded", "Electricity Analyzer is not loaded")
        return
    live = analyzer.live_coordinator.data or {}
    insights = analyzer.insights_coordinator.data or {}
    connection.send_result(
        msg["id"],
        {
            "today": live.get("today"),
            "band": live.get("band"),
            "registers": live.get("registers"),
            "summaries": insights.get("summaries"),
            "store": insights.get("store"),
        },
    )
