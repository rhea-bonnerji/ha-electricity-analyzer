"""The reading that the submit button would send, one box per register.

Each box FOLLOWS the running estimate (the dial value: whole units, never rounded
up) until you type into it, at which point it holds what you typed. That is how a
reading read off the physical meter replaces the estimate. Submitting, or pressing
"Use estimated reading", hands the boxes back to the estimate.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import AnalyzerEntity
from .runtime import Analyzer


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    analyzer: Analyzer = entry.runtime_data
    async_add_entities([ReadingNumber(analyzer, "day"), ReadingNumber(analyzer, "night")])


class ReadingNumber(AnalyzerEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 999999
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:counter"

    def __init__(self, analyzer: Analyzer, kind: str) -> None:
        super().__init__(analyzer, analyzer.live_coordinator, f"reading_to_submit_{kind}")
        self.kind = kind
        self._attr_name = f"{kind.title()} reading to submit"

    @property
    def native_value(self) -> float | None:
        submit = self.analyzer.reading_to_submit()
        value = submit.get(self.kind)
        return None if value is None else float(value)

    @property
    def extra_state_attributes(self) -> dict:
        submit = self.analyzer.reading_to_submit()
        manual = (self.analyzer.override_day if self.kind == "day" else self.analyzer.override_night) is not None
        return {"source": "typed in" if manual else "running estimate", "origin": submit.get("origin")}

    async def async_set_native_value(self, value: float) -> None:
        if self.kind == "day":
            self.analyzer.override_day = int(value)
        else:
            self.analyzer.override_night = int(value)
        self.analyzer._push_live_now()
