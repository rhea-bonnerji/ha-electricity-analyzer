"""Own a Lovelace dashboard outright, without touching anyone else's.

WHAT THIS DELIBERATELY DOES NOT DO

- It never writes the default dashboard (`url_path=None`), and it never writes
  the `lovelace` dashboard. On a modern Home Assistant the sidebar "Overview" is
  a built-in `home` panel; writing Lovelace config at `url_path=None` updates a
  dashboard nobody sees, and writing to `lovelace` would replace whatever the
  user has there. Both were how the old generator put a card on the front door,
  and both are gone. Everything this integration produces lives on its own panel.
- It never sets `defaultPanel`, so the page you land on after signing in is
  unchanged.
- It never adds an item to Home Assistant's own dashboards collection. That
  collection is created inside `lovelace.async_setup` and is not published in
  `hass.data`, so a second integration can only reach it by building a rival
  instance over the same Store — and two live collections writing the same file
  silently drop each other's dashboards. Registering our own panel avoids the
  whole hazard.

WHAT IT DOES INSTEAD

A Lovelace panel is two things: a `frontend` panel record pointing at the
`lovelace` component, and an entry in `hass.data[LOVELACE_DATA].dashboards` that
the `lovelace/config` and `lovelace/config/save` websocket commands look up by
url path. Both are created here, which is the same pair `lovelace` itself creates
for a storage dashboard. The result is an ordinary, fully functional dashboard —
editable in the UI, backed by `.storage/lovelace.<id>` — that this integration
adds on setup and removes on unload.

The one consequence worth knowing: because the dashboard is not in Home
Assistant's collection, its *row* in Settings → Dashboards cannot be renamed or
deleted there. The title, icon and sidebar visibility are integration options
instead, which is where they belong for something the integration maintains.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import frontend
from homeassistant.components.lovelace import dashboard as lovelace_dashboard
from homeassistant.components.lovelace.const import (
    CONF_ICON,
    CONF_REQUIRE_ADMIN,
    CONF_SHOW_IN_SIDEBAR,
    CONF_TITLE,
    CONF_URL_PATH,
    LOVELACE_DATA,
    MODE_STORAGE,
    ConfigNotFound,
)
from homeassistant.components.lovelace.const import (
    DOMAIN as LOVELACE_DOMAIN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from ..const import DOMAIN

LOG = logging.getLogger(__name__)

# Url paths this integration must never claim. "lovelace" is the user's own main
# dashboard after Home Assistant's 2026.x migration, and the built-in panels are
# not ours to replace.
RESERVED_URL_PATHS = frozenset(
    {
        "lovelace",
        "home",
        "energy",
        "map",
        "history",
        "logbook",
        "developer-tools",
        "config",
        "profile",
        "todo",
        "calendar",
        "media-browser",
    }
)


class PanelUnavailable(HomeAssistantError):
    """The url path cannot be used, and the reason is worth telling the user."""


def storage_id(entry_id: str, slug: str = "") -> str:
    """Stable id for a dashboard's own config store.

    Keyed on the config entry rather than the url path so that changing the url
    path in options moves the dashboard without losing its contents.

    `slug` distinguishes the panels one entry owns. It is deliberately EMPTY for the
    electricity dashboard: this string is part of the Lovelace storage key, so giving
    it a name now would orphan the dashboard of anyone who already has one.
    """
    return f"{DOMAIN}_{entry_id}" if not slug else f"{DOMAIN}_{entry_id}_{slug}"


@callback
def async_panel_owner(hass: HomeAssistant, url_path: str) -> str | None:
    """Which component owns the panel at `url_path`, if any."""
    panels = hass.data.get(frontend.DATA_PANELS) or {}
    if (panel := panels.get(url_path)) is None:
        return None
    return panel.component_name


@callback
def async_check_url_path(
    hass: HomeAssistant,
    url_path: str,
    *,
    entry_id: str | None = None,
    slug: str = "",
) -> None:
    """Raise if `url_path` is not ours to use.

    Called from the config flow so the problem is a readable form error rather
    than a failed setup, and again at setup time because the frontend can have
    gained a panel in between.
    """
    if not url_path or url_path.strip("-") != url_path or " " in url_path:
        raise PanelUnavailable("invalid_url_path")
    if url_path in RESERVED_URL_PATHS:
        raise PanelUnavailable("reserved_url_path")

    owner = async_panel_owner(hass, url_path)
    if owner is None:
        return
    # Already ours, from a previous setup of this same entry: that is a
    # re-register, not a collision.
    if entry_id is not None and _is_our_dashboard(hass, url_path, entry_id, slug):
        return
    raise PanelUnavailable("url_path_taken")


@callback
def _is_our_dashboard(
    hass: HomeAssistant, url_path: str, entry_id: str, slug: str = ""
) -> bool:
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        return False
    existing = lovelace.dashboards.get(url_path)
    config = getattr(existing, "config", None) or {}
    return config.get("id") == storage_id(entry_id, slug)


async def async_register(
    hass: HomeAssistant,
    *,
    entry_id: str,
    url_path: str,
    title: str,
    icon: str,
    show_in_sidebar: bool,
    require_admin: bool,
    slug: str = "",
) -> lovelace_dashboard.LovelaceStorage:
    """Create or update one of our panels, and return the store that backs it."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        # `lovelace` is a manifest dependency, so this means it failed to set up.
        raise PanelUnavailable("lovelace_unavailable")

    async_check_url_path(hass, url_path, entry_id=entry_id, slug=slug)

    config: dict[str, Any] = {
        "id": storage_id(entry_id, slug),
        CONF_URL_PATH: url_path,
        CONF_TITLE: title,
        CONF_ICON: icon,
        CONF_SHOW_IN_SIDEBAR: show_in_sidebar,
        CONF_REQUIRE_ADMIN: require_admin,
        "mode": MODE_STORAGE,
    }

    existing = lovelace.dashboards.get(url_path)
    if isinstance(existing, lovelace_dashboard.LovelaceStorage) and _is_our_dashboard(
        hass, url_path, entry_id, slug
    ):
        # Reuse the live store. Rebuilding it would drop its cached data and its
        # loaded flag, and there is no reason to re-read the file.
        existing.config = {**config}
        store = existing
    else:
        store = lovelace_dashboard.LovelaceStorage(hass, config)
        lovelace.dashboards[url_path] = store

    frontend.async_register_built_in_panel(
        hass,
        LOVELACE_DOMAIN,
        sidebar_title=title,
        sidebar_icon=icon,
        frontend_url_path=url_path,
        config={"mode": MODE_STORAGE},
        require_admin=require_admin,
        show_in_sidebar=show_in_sidebar,
        # Setting up again after a reload or an options change must update the
        # existing panel, not trip the "overwriting panel" guard.
        update=True,
    )
    LOG.debug("registered the %s dashboard panel at /%s", title, url_path)
    return store


@callback
def async_unregister(
    hass: HomeAssistant, url_path: str, entry_id: str, slug: str = ""
) -> None:
    """Take the panel down, leaving its stored config in place.

    The config survives an unload on purpose: reloading the entry, or restarting
    Home Assistant, should bring the same dashboard back rather than regenerate it
    from nothing. It is only deleted when the config entry is removed outright
    (see async_delete_config).
    """
    if not _is_our_dashboard(hass, url_path, entry_id, slug):
        # Something else is there now. Removing it would be worse than leaving it.
        LOG.debug("not removing /%s: it is no longer ours", url_path)
        return

    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is not None:
        lovelace.dashboards.pop(url_path, None)
    if frontend.async_panel_exists(hass, url_path):
        frontend.async_remove_panel(hass, url_path)
    LOG.debug("removed the dashboard panel at /%s", url_path)


async def async_delete_config(
    hass: HomeAssistant, entry_id: str, slug: str = ""
) -> None:
    """Delete a dashboard's stored config. Only on config entry removal."""
    from homeassistant.helpers.storage import Store

    store = Store[dict[str, Any]](
        hass,
        lovelace_dashboard.CONFIG_STORAGE_VERSION,
        lovelace_dashboard.CONFIG_STORAGE_KEY.format(storage_id(entry_id, slug)),
    )
    await store.async_remove()
    LOG.debug("deleted the stored dashboard config for %s", entry_id)


async def async_current_config(
    store: lovelace_dashboard.LovelaceStorage,
) -> dict[str, Any] | None:
    """What the dashboard currently holds, or None if it has never been written."""
    try:
        return await store.async_load(False)
    except ConfigNotFound:
        return None
    except HomeAssistantError as exc:
        LOG.debug("could not read the current dashboard config: %s", exc)
        return None
