"""Serve the two custom cards the dashboard uses, and register them as resources.

* `apexcharts-card` (vendored, MIT, v2.2.3): the fixed-window charts.
* `electricity-analyzer-data-card` (this integration): the table view of the database.

A custom card that is not a registered Lovelace resource renders as a bare
"Configuration error", so both are served straight out of the integration directory
and registered. An apexcharts-card registered by someone else (HACS) always wins:
two modules both defining the element would make the second throw.

A browser tab that was already open when a resource was first registered does not
know about it until it reloads (the frontend reads the resource list once per page
load), so the very first install asks for one reload. After that, never again.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant

from ..const import APEX_FILE, APEX_MARKER, APEX_VERSION, DATA_CARD_FILE, DATA_CARD_VERSION, DOMAIN, STATIC_BASE

LOG = logging.getLogger(__name__)
_STATIC_READY = f"{DOMAIN}_static_path"

APEX_URL = f"{STATIC_BASE}/{APEX_FILE}"
DATA_URL = f"{STATIC_BASE}/{DATA_CARD_FILE}"


@dataclass
class CardSupport:
    apex: bool = False
    data_card: bool = False
    newly_registered: bool = False


async def async_ensure_cards(hass: HomeAssistant) -> CardSupport:
    support = CardSupport()
    if not await _serve(hass):
        return support
    resources = _collection(hass)
    items = await _items(hass)

    apex_existing = next((i for i in items if APEX_MARKER in str(i.get("url") or "")), None)
    if apex_existing is not None:
        support.apex = True
    else:
        support.apex = await _register(hass, resources, f"{APEX_URL}?v={APEX_VERSION}")
        support.newly_registered |= support.apex

    data_existing = next((i for i in items if DATA_CARD_FILE in str(i.get("url") or "")), None)
    wanted = f"{DATA_URL}?v={DATA_CARD_VERSION}"
    if data_existing is not None:
        support.data_card = True
        if str(data_existing.get("url")) != wanted and isinstance(resources, ResourceStorageCollection):
            # New card version: point the resource at it so browsers fetch the new file.
            try:
                await resources.async_update_item(data_existing["id"], {"res_type": "module", "url": wanted})
            except Exception as exc:  # noqa: BLE001
                LOG.debug("could not update the data card resource: %s", exc)
    else:
        support.data_card = await _register(hass, resources, wanted)
        support.newly_registered |= support.data_card
    return support


async def _serve(hass: HomeAssistant) -> bool:
    if hass.data.get(_STATIC_READY):
        return True
    directory = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
    try:
        await hass.http.async_register_static_paths([StaticPathConfig(STATIC_BASE, directory, cache_headers=True)])
    except RuntimeError:
        pass  # already registered by a previous setup in this process
    except Exception as exc:  # noqa: BLE001 - never break setup over a card
        LOG.warning("could not serve the dashboard cards: %s", exc)
        return False
    hass.data[_STATIC_READY] = True
    return True


def _collection(hass: HomeAssistant):
    lovelace = hass.data.get(LOVELACE_DATA)
    return None if lovelace is None else lovelace.resources


async def _items(hass: HomeAssistant) -> list[dict]:
    resources = _collection(hass)
    if resources is None:
        return []
    try:
        await resources.async_get_info()
        return list(resources.async_items() or [])
    except Exception as exc:  # noqa: BLE001
        LOG.debug("could not read Lovelace resources: %s", exc)
        return []


async def _register(hass: HomeAssistant, resources, url: str) -> bool:
    if not isinstance(resources, ResourceStorageCollection):
        LOG.warning(
            "Lovelace resources are in YAML mode; add this under lovelace: resources: - url: %s  type: module",
            url,
        )
        return False
    try:
        await resources.async_create_item({"res_type": "module", "url": url})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not register %s: %s", url, exc)
        return False
    LOG.info("registered dashboard card %s", url)
    return True


async def async_remove_resources(hass: HomeAssistant) -> None:
    resources = _collection(hass)
    if not isinstance(resources, ResourceStorageCollection):
        return
    for item in await _items(hass):
        url = str(item.get("url") or "")
        if url.startswith(STATIC_BASE):
            try:
                await resources.async_delete_item(item["id"])
            except Exception as exc:  # noqa: BLE001
                LOG.debug("could not remove %s: %s", url, exc)
