"""Build the sidebar dashboard and keep it correct.

* Registered as this integration's own Lovelace panel (see panel.py): it never
  touches Overview, the default dashboard, or the page you land on.
* Every entity is resolved through the entity registry by unique id, so renaming an
  entity id in the UI does not break the dashboard; it is regenerated instead.
* Rebuilt on setup and whenever this integration's entities change (debounced). A
  dashboard edited by hand is left alone; `electricity_analyzer.rebuild` with
  `dashboard: true` regenerates it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from homeassistant.components.persistent_notification import async_create as notify
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.storage import Store

from ..const import (
    CONF_DASHBOARD_PATH,
    CONF_DASHBOARD_TITLE,
    CONF_HALL,
    CONF_HOUSE,
    DEFAULT_DASHBOARD_ICON,
    DEFAULT_DASHBOARD_PATH,
    DEFAULT_DASHBOARD_TITLE,
    DOMAIN,
    MEASUREMENTS,
    M_PF,
    conf_entity,
)
from . import panel
from .cards import Cards, Source
from .frontend_assets import async_ensure_cards, async_remove_resources
from .views import build_views

LOG = logging.getLogger(__name__)
STATE_VERSION = 1


def fingerprint(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()


class DashboardManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STATE_VERSION, f"{DOMAIN}.dashboard_{entry.entry_id}")
        self._state: dict[str, Any] = {}
        self._lovelace = None
        self._unsub = None
        self._debouncer: Debouncer | None = None
        self.status: dict[str, Any] = {"written": False, "reason": "not built yet"}

    @property
    def cfg(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def url_path(self) -> str:
        return str(self.cfg.get(CONF_DASHBOARD_PATH) or DEFAULT_DASHBOARD_PATH).strip("/").lower()

    async def async_setup(self) -> None:
        self._state = await self._store.async_load() or {}
        try:
            self._lovelace = await panel.async_register(
                self.hass,
                entry_id=self.entry.entry_id,
                url_path=self.url_path,
                title=str(self.cfg.get(CONF_DASHBOARD_TITLE) or DEFAULT_DASHBOARD_TITLE),
                icon=DEFAULT_DASHBOARD_ICON,
                show_in_sidebar=True,
                require_admin=False,
            )
        except panel.PanelUnavailable as exc:
            self.status = {"written": False, "reason": f"cannot use /{self.url_path}: {exc}"}
            LOG.warning("dashboard not created: %s", exc)
            return

        self._debouncer = Debouncer(
            self.hass, LOG, cooldown=10, immediate=False, function=self._async_debounced
        )

        @callback
        def registry_changed(event: Event) -> None:
            entity_id = event.data.get("entity_id", "")
            if entity_id.startswith(("sensor.", "number.", "button.")) and self._debouncer:
                self.hass.async_create_task(self._debouncer.async_call())

        self._unsub = self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_changed)
        await self.async_rebuild()

    async def _async_debounced(self) -> None:
        await self.async_rebuild()

    @callback
    def async_unload(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._debouncer:
            self._debouncer.async_cancel()
        panel.async_unregister(self.hass, self.url_path, self.entry.entry_id)

    async def async_remove(self) -> None:
        await panel.async_delete_config(self.hass, self.entry.entry_id)
        await self._store.async_remove()
        if not self.hass.config_entries.async_entries(DOMAIN):
            await async_remove_resources(self.hass)

    # ------------------------------------------------------------- resolution

    def resolve(self) -> Source:
        registry = er.async_get(self.hass)
        prefix = f"{self.entry.entry_id}_"
        ours = {
            e.unique_id[len(prefix):]: e.entity_id
            for e in er.async_entries_for_config_entry(registry, self.entry.entry_id)
            if e.unique_id.startswith(prefix) and not e.disabled
        }
        shelly: dict[str, str] = {}
        for channel in (CONF_HOUSE, CONF_HALL):
            for m in MEASUREMENTS:
                entity_id = self.cfg.get(conf_entity(channel, m))
                if not entity_id:
                    continue
                key = f"{channel}_{m}"
                if m == M_PF:
                    key = f"{channel}_power_factor_live"  # our own sensor owns the plain name
                shelly[key] = entity_id
        return Source(ours=ours, shelly=shelly)

    # ---------------------------------------------------------------- writing

    async def async_rebuild(self, *, force: bool = False) -> dict[str, Any]:
        if self._lovelace is None:
            return self.status
        support = await async_ensure_cards(self.hass)
        cards = Cards(self.resolve(), apex=support.apex, data_card=support.data_card)
        views = build_views(cards)
        config = {"title": str(self.cfg.get(CONF_DASHBOARD_TITLE) or DEFAULT_DASHBOARD_TITLE), "views": views}
        built = fingerprint(config)

        current = await panel.async_current_config(self._lovelace)
        remembered = self._state.get("fingerprint")
        if current is not None and not force:
            live = fingerprint(current)
            if live == built:
                self.status = {"written": False, "reason": "already up to date", "views": len(views)}
                return self.status
            if remembered is not None and live != remembered:
                self.status = {
                    "written": False,
                    "reason": "edited by hand; left alone (call electricity_analyzer.rebuild with dashboard: true to regenerate)",
                    "views": len(views),
                }
                LOG.warning("the /%s dashboard was edited by hand; not overwriting it", self.url_path)
                return self.status

        await self._lovelace.async_save(config)
        self._state["fingerprint"] = built
        await self._store.async_save(self._state)
        self.status = {"written": True, "reason": "written", "views": len(views)}
        LOG.info("wrote the /%s dashboard: %d views", self.url_path, len(views))

        if support.newly_registered and not self._state.get("reload_asked"):
            notify(
                self.hass,
                "Your **Electricity** dashboard is in the sidebar.\n\n**Reload this browser tab once** "
                "(Ctrl-R / Cmd-R). Until you do, charts and the data table show *Configuration error*: "
                "Home Assistant only discovers new cards when a page loads. After one reload it works "
                "everywhere, for good.",
                title="Electricity Analyzer: reload once",
                notification_id=f"{DOMAIN}_reload",
            )
            self._state["reload_asked"] = True
            await self._store.async_save(self._state)
        return self.status
