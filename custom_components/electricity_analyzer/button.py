"""Buttons. The only way a meter reading ever leaves Home Assistant.

* Submit meter reading to SSE: sends the two "reading to submit" boxes. In dry-run
  mode nothing is sent and the exact request is shown instead. SSE accepts one
  reading per 24 hours; the button refuses inside that window rather than trying.
* Check SSE account: read-only. Signs in and reads the meter details and SSE's last
  reading, so you can confirm the connection without submitting anything.
* Use estimated reading: hands the reading boxes back to the running estimate (clears
  anything typed into them). Contacts nobody.
* Refresh ESB now: one ESB fetch, ignoring the schedule. It reuses the saved session;
  a fresh sign-in still counts against the two allowed in any 24 hours, past which ESB
  answers with a CAPTCHA.

After a failed ESB or SSE attempt the buttons that would contact it again are
unavailable for twelve hours: the integration retries by itself then (and every twelve
hours after that if it fails again), so there is nothing to press and nothing to hammer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import AnalyzerEntity
from .runtime import Analyzer


@dataclass(frozen=True, kw_only=True)
class AnalyzerButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[Analyzer], Awaitable[object]]
    # False while pressing would contact a portal that failed less than 12 hours ago.
    available_fn: Callable[[Analyzer], bool] = lambda a: True


async def _refresh_esb(analyzer: Analyzer) -> None:
    if (wait := analyzer.esb_wait_message()) is not None:
        raise HomeAssistantError(wait)
    await analyzer.async_esb_tick(force=True)


async def _use_estimate(analyzer: Analyzer) -> None:
    analyzer.override_day = analyzer.override_night = None
    analyzer._push_live_now()


BUTTONS = (
    AnalyzerButtonDescription(
        key="submit_reading",
        name="Submit meter reading to SSE",
        icon="mdi:send",
        press_fn=lambda a: a.async_submit_reading(),
        available_fn=lambda a: a.sse_mode != "live" or a.waiting_until("sse") is None,
    ),
    AnalyzerButtonDescription(
        key="check_sse",
        name="Check SSE account",
        icon="mdi:account-check",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda a: a.async_check_sse(),
        available_fn=lambda a: a.waiting_until("sse") is None,
    ),
    AnalyzerButtonDescription(
        key="use_estimate",
        name="Use estimated reading",
        icon="mdi:restore",
        press_fn=_use_estimate,
    ),
    AnalyzerButtonDescription(
        key="refresh_esb",
        name="Refresh ESB now",
        icon="mdi:transmission-tower-import",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=_refresh_esb,
        available_fn=lambda a: a.waiting_until("esb") is None,
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    analyzer: Analyzer = entry.runtime_data
    async_add_entities([AnalyzerButton(analyzer, d) for d in BUTTONS])


class AnalyzerButton(AnalyzerEntity, ButtonEntity):
    entity_description: AnalyzerButtonDescription

    def __init__(self, analyzer: Analyzer, description: AnalyzerButtonDescription) -> None:
        super().__init__(analyzer, analyzer.insights_coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return self.entity_description.available_fn(self.analyzer)

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.analyzer)
        await self.analyzer.insights_coordinator.async_request_refresh()
