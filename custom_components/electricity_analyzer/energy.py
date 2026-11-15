"""Home Assistant's Energy dashboard: the meter's energy and its cost, revised like ours.

THE GRID CONNECTION

* energy: `electricity_analyzer:meter_energy`, what the METER counted: ESB's own figure once
  ESB has published the hour, the full-house clamp x the learned meter factor until then, and
  ESB's history from before the clamp. (The hall clamp is inside the total and never added.)
* cost: `electricity_analyzer:cost_energy`, those kWh at the tariff period in force: day and
  night units only. Standing charge and PSO are per day, not per kWh; they stay on this
  integration's own dashboard (energy cost + standing charge + PSO = total).

Both are this integration's own long-term statistics, rewritten whenever a tariff period is
set or ESB revises an hour, so the Energy dashboard shows every revision, for every period,
instead of the price that happened to be showing at the time. (A price ENTITY cannot do that:
Home Assistant multiplies each moment by the price shown then and never revisits it.)

THREE MODES (a setup choice, changeable in the options)

* replace (default): this is THE grid connection. Any other grid connection (a previous
  integration's, a hand-made one) is removed, and a copy of what was removed is handed back
  so the caller can keep it. Only grid connections: solar, battery, gas, water and
  individual devices are never touched.
* add: the connection is added only when there is no grid connection at all; anything else
  is left exactly as it is.
* off: the Energy dashboard is not touched.

A grid connection this integration made earlier (on the clamp's own sensor, priced by the
unit-rate entity) is upgraded in place in replace and add modes. Every mode is idempotent.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from .const import stat_id

LOG = logging.getLogger(__name__)

MODE_REPLACE = "replace"
MODE_ADD = "add"
MODE_OFF = "off"
MODES = (MODE_REPLACE, MODE_ADD, MODE_OFF)

ENERGY_STAT = stat_id("meter_energy")
COST_STAT = stat_id("cost_energy")
SOURCE_NAME = "Electricity meter (ESB, clamp until ESB has it)"


@dataclass
class EnergyResult:
    """outcome: configured | replaced | upgraded | already | other | off | unavailable.

    `removed`: the grid connections taken out in replace mode, exactly as they were.
    """

    outcome: str
    removed: list[dict[str, Any]] = field(default_factory=list)


def grid_source(name: str = SOURCE_NAME) -> dict[str, Any]:
    return {
        "type": "grid",
        "stat_energy_from": ENERGY_STAT,
        "stat_energy_to": None,
        "stat_cost": COST_STAT,
        "entity_energy_price": None,
        "number_energy_price": None,
        "stat_compensation": None,
        "entity_energy_price_export": None,
        "number_energy_price_export": None,
        "cost_adjustment_day": 0.0,
        "name": name,
    }


def _current(source: dict[str, Any]) -> bool:
    return (
        source.get("stat_energy_from") == ENERGY_STAT
        and source.get("stat_cost") == COST_STAT
        and source.get("entity_energy_price") is None
        and source.get("number_energy_price") is None
    )


async def async_configure(hass: HomeAssistant, house_energy: str, mode: str = MODE_REPLACE) -> EnergyResult:
    if mode == MODE_OFF:
        return EnergyResult("off")
    try:
        from homeassistant.components.energy import data as energy_data
    except ImportError:
        return EnergyResult("unavailable")
    try:
        manager = await energy_data.async_get_manager(hass)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("energy preferences unavailable: %s", exc)
        return EnergyResult("unavailable")

    prefs = manager.data or {}
    sources = list(prefs.get("energy_sources") or [])
    grids = [s for s in sources if s.get("type") == "grid"]
    # Ours: the meter statistics, or the clamp's sensor as 1.0.x configured it.
    ours = next((s for s in grids if s.get("stat_energy_from") == ENERGY_STAT), None) or next(
        (s for s in grids if s.get("stat_energy_from") == house_energy), None
    )
    others = [s for s in grids if s is not ours]

    if ours is None and others and mode != MODE_REPLACE:
        LOG.info("the Energy dashboard already has a grid connection; leaving it alone (mode: add)")
        return EnergyResult("other")
    removed = others if mode == MODE_REPLACE else []
    if ours is not None and _current(ours) and not removed:
        return EnergyResult("already")

    mine = grid_source() if ours is None else {**ours, **grid_source(ours.get("name") or SOURCE_NAME)}
    updated: list[dict[str, Any]] = []
    placed = False
    for s in sources:
        if s.get("type") != "grid":
            updated.append(s)
        elif s is ours or (ours is None and not placed and removed):
            updated.append(mine)  # in the place of the connection it upgrades or replaces
            placed = True
        elif any(s is r for r in removed):
            continue
        else:
            updated.append(s)
    if not placed:
        updated.append(mine)

    try:
        await manager.async_update({"energy_sources": updated})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not configure the Energy dashboard: %s", exc)
        return EnergyResult("unavailable")
    if removed:
        LOG.info(
            "Energy dashboard: grid connection(s) %s replaced by the meter's energy and cost statistics",
            [s.get("stat_energy_from") or s.get("name") for s in removed],
        )
        return EnergyResult("replaced", copy.deepcopy(removed))
    if ours is not None:
        LOG.info("Energy dashboard: grid connection now on the meter's energy and cost statistics")
        return EnergyResult("upgraded")
    LOG.info("Energy dashboard: grid connection on the meter's energy and cost statistics")
    return EnergyResult("configured")
