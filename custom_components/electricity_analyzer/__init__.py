"""Electricity Analyzer: Shelly EM Gen4 clamps, ESB Networks, SSE Airtricity, one dashboard.

* The full-house clamp (120 A) is the total and the Energy dashboard's grid source.
* The hall clamp (50 A) is a circuit inside that total, tracked separately.
* Every hour is stored in <config>/electricity_analyzer/electricity.db, forever, with
  day/night split, cost at the tariff in force, power, power factor and voltage, and
  is shown on the dashboard's Data tab.
* ESB's own meter data (two to three days late) is compared with the clamp.
* Meter readings go to SSE Airtricity only when you press the button.
"""

from __future__ import annotations

import logging
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import websocket_api
from .const import (
    CONF_ENERGY_MODE,
    CONF_ENERGY_PREFS,
    CONF_HOUSE,
    CONF_PSO,
    CONF_VALID_FROM,
    DOMAIN,
    M_ENERGY,
    SERVICE_ADD_TARIFF,
    SERVICE_EXPORT,
    SERVICE_REBUILD,
    SERVICE_REFRESH_ESB,
    SERVICE_SET_READING,
    SERVICE_SUBMIT_READING,
    SERVICE_SUPPLY_SESSION,
)
from .dashboard.manager import DashboardManager
from .energy import MODE_OFF, MODE_REPLACE, MODES, EnergyResult
from .energy import async_configure as async_configure_energy
from .engine.session import parse_cookie_header
from .engine.tariff import (
    DEFAULT_TARIFF,
    LEGACY_DEFAULT_PSO,
    LEGACY_DEFAULT_VALID_FROM,
    NightWindow,
    Tariff,
)
from .runtime import Analyzer

LOG = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.NUMBER, Platform.BUTTON]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AnalyzerEntry = ConfigEntry[Analyzer]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    websocket_api.async_register(hass)
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AnalyzerEntry) -> bool:
    analyzer = Analyzer(hass, entry)
    await analyzer.async_setup()
    entry.runtime_data = analyzer
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    dashboard = DashboardManager(hass, entry)
    analyzer.dashboard = dashboard
    await dashboard.async_setup()

    if energy_mode(analyzer.cfg) != MODE_OFF and CONF_HOUSE in analyzer.entities:
        await _async_setup_energy(hass, entry, analyzer)

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


def energy_mode(cfg: dict) -> str:
    """replace | add | off. Entries from before the choice existed follow their on/off."""
    mode = cfg.get(CONF_ENERGY_MODE)
    if mode in MODES:
        return mode
    return MODE_REPLACE if cfg.get(CONF_ENERGY_PREFS, True) else MODE_OFF


async def _async_setup_energy(hass: HomeAssistant, entry: AnalyzerEntry, analyzer: Analyzer) -> None:
    """The Energy dashboard's grid connection: the meter's energy and its energy cost, as this
    integration's own statistics (energy.py). Idempotent: runs on every setup."""
    result = await async_configure_energy(hass, analyzer.entities[CONF_HOUSE][M_ENERGY], energy_mode(analyzer.cfg))
    analyzer.energy_status = result.outcome
    if result.removed:
        await _keep_removed(hass, analyzer, result)


async def _keep_removed(hass: HomeAssistant, analyzer: Analyzer, result: EnergyResult) -> None:
    """A copy of every grid connection replace mode took out, and one notice saying so."""
    from homeassistant.components import persistent_notification

    stamp = dt_util.utcnow().replace(microsecond=0).isoformat()
    if analyzer.store is not None:
        kept = await hass.async_add_executor_job(analyzer.store.get_json, "energy_grid_replaced", [])
        kept = [*(kept or []), *({"removed_at": stamp, "source": s} for s in result.removed)]
        await hass.async_add_executor_job(analyzer.store.set_json, "energy_grid_replaced", kept)
    names = ", ".join(f"`{s.get('stat_energy_from') or s.get('name') or 'unnamed'}`" for s in result.removed)
    persistent_notification.async_create(
        hass,
        f"The Energy dashboard's grid connection is now the meter's energy (ESB's figures, the clamp "
        f"until ESB has them) and its energy cost at your tariff periods, both revised as prices "
        f"change and ESB publishes (standing charge and PSO stay on the Electricity dashboard). "
        f"Replaced: {names}. A copy of what was removed is kept in the integration's database "
        f"(meta key `energy_grid_replaced`). To keep other grid connections instead, choose "
        f"\"Add only\" under the integration's options > Dashboard.",
        title="Electricity Analyzer: Energy dashboard",
        notification_id=f"{DOMAIN}_energy_replaced",
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.1 -> 1.2: the default tariff starts on 6 October and its PSO is €19.10 a year.

    Only values that are EXACTLY what 1.0.x pre-filled (5 October 2026, €6.67) are
    changed; anything typed in by hand is kept. Credentials, entities, the reading and
    every other setting are untouched, so nothing has to be set up again.
    """
    if entry.version > 1:
        return False  # written by a newer version; never guess at downgrading it
    if entry.version == 1 and entry.minor_version < 2:
        data, options = dict(entry.data), dict(entry.options)
        for store in (data, options):
            if str(store.get(CONF_VALID_FROM) or "")[:10] == LEGACY_DEFAULT_VALID_FROM.isoformat():
                store[CONF_VALID_FROM] = DEFAULT_TARIFF.valid_from.isoformat()
            try:
                legacy_pso = store.get(CONF_PSO) is not None and abs(float(store[CONF_PSO]) - LEGACY_DEFAULT_PSO) < 1e-9
            except (TypeError, ValueError):
                legacy_pso = False
            if legacy_pso:
                store[CONF_PSO] = DEFAULT_TARIFF.pso_per_year
        if CONF_ENERGY_MODE not in data and CONF_ENERGY_MODE not in options:
            data[CONF_ENERGY_MODE] = energy_mode({**data, **options})
        hass.config_entries.async_update_entry(entry, data=data, options=options, minor_version=2)
        LOG.info("Electricity Analyzer settings migrated to 1.2 (tariff start, PSO default, Energy dashboard choice)")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AnalyzerEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        analyzer = entry.runtime_data
        if getattr(analyzer, "dashboard", None) is not None:
            analyzer.dashboard.async_unload()
        await analyzer.async_unload()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: AnalyzerEntry) -> None:
    """The dashboard goes with the integration. The database is KEPT on purpose:
    it is the only copy of hours older than the recorder retains, and deleting a
    user's history is never something to do implicitly."""
    await DashboardManager(hass, entry).async_remove()


async def _async_reload(hass: HomeAssistant, entry: AnalyzerEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _analyzer(hass: HomeAssistant) -> Analyzer:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data
    raise HomeAssistantError("Electricity Analyzer is not loaded")


@callback
def _register_services(hass: HomeAssistant) -> None:
    async def refresh_esb(call: ServiceCall) -> None:
        analyzer = _analyzer(hass)
        if (wait := analyzer.esb_wait_message()) is not None:
            raise HomeAssistantError(wait)  # after a failure only the 12-hour retry tries
        await analyzer.async_esb_tick(force=True)

    async def rebuild(call: ServiceCall) -> ServiceResponse:
        analyzer = _analyzer(hass)
        rows = await analyzer.async_rollup(rebuild=True)
        await analyzer.async_apply_esb()  # back to ESB's figures where it has them, lumps undone
        written = await analyzer.async_publish(full=True)
        await analyzer.insights_coordinator.async_request_refresh()
        if analyzer.dashboard is not None:
            await analyzer.dashboard.async_rebuild(force=bool(call.data.get("dashboard")))
        return {"hours_read": rows, "statistics_rows": sum(written.values())}

    async def set_reading(call: ServiceCall) -> None:
        when = call.data.get("taken_at") or dt_util.now()
        if isinstance(when, str):
            when = dt_util.parse_datetime(when)
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt_util.get_default_time_zone())
        await _analyzer(hass).async_set_anchor(float(call.data["day"]), float(call.data["night"]), when, "manual")

    async def submit_reading(call: ServiceCall) -> ServiceResponse:
        if not call.data.get("confirm"):
            raise HomeAssistantError("set confirm: true to send a meter reading; nothing was sent")
        return await _analyzer(hass).async_submit_reading(
            call.data.get("day"), call.data.get("night"), force=bool(call.data.get("force"))
        )

    async def add_tariff(call: ServiceCall) -> ServiceResponse:
        analyzer = _analyzer(hass)
        # Anything not given (standing charge, PSO, night hours, names) is what applied on
        # that date, so a period is always the same four figures over the same meter clock.
        current = analyzer.schedule.for_date(call.data["valid_from"])
        tariff = Tariff(
            valid_from=call.data["valid_from"],
            supplier=call.data.get("supplier", current.supplier),
            plan=call.data.get("plan", current.plan),
            day_rate=float(call.data["day_rate"]) / 100,
            night_rate=float(call.data["night_rate"]) / 100,
            standing_per_year=float(call.data.get("standing_charge", current.standing_per_year)),
            pso_per_year=float(call.data.get("pso_levy", current.pso_per_year)),
            window=NightWindow(
                int(call.data.get("night_start", current.window.start_hour)),
                int(call.data.get("night_end", current.window.end_hour)),
                call.data.get("night_clock", current.window.clock),
            ),
        )
        repriced = await analyzer.async_set_tariff_period(tariff, call.data.get("valid_to"))
        return {"repriced_hours": repriced}

    async def export(call: ServiceCall) -> ServiceResponse:
        analyzer = _analyzer(hass)
        result = await analyzer.async_export()
        if call.data.get("backup"):
            result["backup"] = await analyzer.async_backup()
        return result

    async def supply_session(call: ServiceCall) -> None:
        cookies = parse_cookie_header(call.data["cookie"])
        if not cookies:
            raise HomeAssistantError("no cookies found in what was pasted")
        await _analyzer(hass).async_supply_esb_session(cookies)

    hass.services.async_register(DOMAIN, SERVICE_REFRESH_ESB, refresh_esb)
    hass.services.async_register(
        DOMAIN,
        SERVICE_REBUILD,
        rebuild,
        schema=vol.Schema({vol.Optional("dashboard", default=False): cv.boolean}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_READING,
        set_reading,
        schema=vol.Schema(
            {
                vol.Required("day"): vol.All(vol.Coerce(float), vol.Range(min=0, max=999999)),
                vol.Required("night"): vol.All(vol.Coerce(float), vol.Range(min=0, max=999999)),
                vol.Optional("taken_at"): cv.datetime,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SUBMIT_READING,
        submit_reading,
        schema=vol.Schema(
            {
                vol.Optional("day"): vol.All(vol.Coerce(int), vol.Range(min=0, max=999999)),
                vol.Optional("night"): vol.All(vol.Coerce(int), vol.Range(min=0, max=999999)),
                vol.Required("confirm"): cv.boolean,
                vol.Optional("force", default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_TARIFF,
        add_tariff,
        schema=vol.Schema(
            {
                vol.Required("valid_from"): cv.date,
                vol.Optional("valid_to"): cv.date,
                vol.Required("day_rate"): vol.All(vol.Coerce(float), vol.Range(min=0, max=200)),
                vol.Required("night_rate"): vol.All(vol.Coerce(float), vol.Range(min=0, max=200)),
                vol.Optional("standing_charge"): vol.All(vol.Coerce(float), vol.Range(min=0, max=2000)),
                vol.Optional("pso_levy"): vol.All(vol.Coerce(float), vol.Range(min=-500, max=500)),
                vol.Optional("night_start"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                vol.Optional("night_end"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                vol.Optional("night_clock"): vol.In(["gmt", "local"]),
                vol.Optional("supplier"): cv.string,
                vol.Optional("plan"): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT,
        export,
        schema=vol.Schema({vol.Optional("backup", default=False): cv.boolean}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SUPPLY_SESSION, supply_session, schema=vol.Schema({vol.Required("cookie"): cv.string})
    )
