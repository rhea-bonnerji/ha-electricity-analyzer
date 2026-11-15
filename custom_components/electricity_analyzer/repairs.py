"""Repairs: the ESB CAPTCHA hand-over.

When ESB Networks shows a CAPTCHA the integration stops: no retry loop, no attempt to
solve or route around it. The person signs in on ESB's own site in their own
browser, and hands the resulting session over by pasting the Cookie header. That is
the only mechanism available to a server-side integration (the session cookie is
HttpOnly and on another origin), and it is the same thing the browser itself does on
its next request.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .engine.session import parse_cookie_header


class EsbSessionRepairFlow(RepairsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cookies = parse_cookie_header(user_input.get("cookie", ""))
            if cookies:
                for entry in self.hass.config_entries.async_loaded_entries(DOMAIN):
                    await entry.runtime_data.async_supply_esb_session(cookies)
                return self.async_create_entry(data={})
            errors["base"] = "no_cookies"
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema({vol.Required("cookie"): str}), errors=errors
        )


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    return EsbSessionRepairFlow()
