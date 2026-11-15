"""Setup and options.

Setup, in order:

  1. meters     which Shelly energy sensors are the full house (120 A) and the hall (50 A)
  2. channels   the companion sensors (power, PF, voltage...), found automatically
  3. tariff     the plan's rates, standing charge and PSO, all INCLUDING VAT
  4. sse        SSE Airtricity sign-in for meter readings: off, dry run, or live
  5. esb        ESB Networks sign-in: off, or the account
  6. reading    the meter's current day and night reading and when it was taken
  7. weather    where the house is (picked on a map), and an optional AccuWeather entity
  8. dashboard  the sidebar dashboard, and the Energy dashboard

Nothing in setup sends a meter reading. With SSE in live mode, setup signs in once,
read-only, to confirm the credentials and to offer SSE's last accepted reading as the
starting point; it never submits.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector as sel
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.util.location import async_detect_location_info

from .const import (
    CONF_BILL_ANCHOR,
    CONF_CLOCK,
    CONF_DASHBOARD_PATH,
    CONF_DASHBOARD_TITLE,
    CONF_DAY_RATE,
    CONF_ENERGY_MODE,
    CONF_ENERGY_PREFS,
    CONF_ESB_MODE,
    CONF_ESB_PASSWORD,
    CONF_ESB_PER_DAY,
    CONF_ESB_USERNAME,
    CONF_HALL,
    CONF_HOUSE,
    CONF_LATITUDE,
    CONF_LOCATION,
    CONF_LONGITUDE,
    CONF_MPRN,
    CONF_NIGHT_END,
    CONF_NIGHT_RATE,
    CONF_NIGHT_START,
    CONF_PLAN,
    CONF_PSO,
    CONF_READING_AT,
    CONF_READING_DAY,
    CONF_READING_NIGHT,
    CONF_SSE_ACCOUNT,
    CONF_SSE_MODE,
    CONF_SSE_PASSWORD,
    CONF_SSE_SYNC,
    CONF_SSE_USERNAME,
    CONF_STANDING,
    CONF_SUPPLIER,
    CONF_VALID_FROM,
    CONF_WEATHER_ENTITY,
    DATA_ESB_HANDOFF,
    DEFAULT_DASHBOARD_PATH,
    DEFAULT_DASHBOARD_TITLE,
    DEFAULT_ESB_PER_DAY,
    DEFAULT_READING_AT,
    DEFAULT_READING_DAY,
    DEFAULT_READING_NIGHT,
    DOMAIN,
    ESB_ACCOUNT,
    ESB_OFF,
    MAX_ESB_PER_DAY,
    M_APPARENT,
    M_CURRENT,
    M_ENERGY,
    M_FREQUENCY,
    M_PF,
    M_POWER,
    M_RETURNED,
    M_VOLTAGE,
    MEASUREMENTS,
    NAME,
    SSE_DRY_RUN,
    SSE_LIVE,
    SSE_OFF,
    conf_entity,
)
from .engine.errors import AuthFailed, CaptchaEncountered, PortalError
from .engine.esb.client import EsbClient, new_cookie_jar
from .engine.sse.client import MeterInfo, SseClient
from .engine.tariff import IRELAND
from .runtime import TariffPeriodError
from .tariff_config import tariff_defaults

LOG = logging.getLogger(__name__)

BILL_MONTHS = "bill_months"

# The entity ids a Shelly EM Gen4 gets when its two channels are named as in this
# house. Used only as the first guess; anything can be picked instead.
DEFAULT_GUESS = {
    CONF_HOUSE: "sensor.full_house_energy_monitor_energy",
    CONF_HALL: "sensor.hall_energy_monitor_energy",
}

_DEVICE_CLASS = {
    M_POWER: "power",
    M_APPARENT: "apparent_power",
    M_PF: "power_factor",
    M_VOLTAGE: "voltage",
    M_CURRENT: "current",
    M_FREQUENCY: "frequency",
}
_SUFFIX = {
    M_POWER: "_power",
    M_APPARENT: "_apparent_power",
    M_PF: "_power_factor",
    M_VOLTAGE: "_voltage",
    M_CURRENT: "_current",
    M_FREQUENCY: "_frequency",
    M_RETURNED: "_energy_returned",
}
_LABEL = {
    M_POWER: "power",
    M_APPARENT: "apparent power",
    M_PF: "power factor",
    M_VOLTAGE: "voltage",
    M_CURRENT: "current",
    M_FREQUENCY: "frequency",
    M_RETURNED: "energy returned",
}


def _energy_selector() -> sel.EntitySelector:
    return sel.EntitySelector(sel.EntitySelectorConfig(domain="sensor", device_class="energy"))


def _entity_selector(measurement: str) -> sel.EntitySelector:
    if measurement == M_RETURNED:
        return sel.EntitySelector(sel.EntitySelectorConfig(domain="sensor", device_class="energy"))
    return sel.EntitySelector(sel.EntitySelectorConfig(domain="sensor", device_class=_DEVICE_CLASS[measurement]))


def _guess_energy(hass: HomeAssistant, channel: str) -> str | None:
    guess = DEFAULT_GUESS[channel]
    if hass.states.get(guess) is not None:
        return guess
    words = ("full house", "house", "mains", "total") if channel == CONF_HOUSE else ("hall",)
    for state in hass.states.async_all("sensor"):
        if state.attributes.get("device_class") != "energy" or "returned" in state.entity_id:
            continue
        name = str(state.attributes.get("friendly_name", "")).lower()
        if any(w in name for w in words) and not (channel == CONF_HOUSE and "hall" in name):
            return state.entity_id
    return None


def detect_companions(hass: HomeAssistant, energy_entity: str) -> dict[str, str]:
    """The other sensors on the same Shelly channel as `energy_entity`."""
    found: dict[str, str] = {}
    registry = er.async_get(hass)
    entry = registry.async_get(energy_entity)
    if entry is not None and entry.device_id:
        for other in er.async_entries_for_device(registry, entry.device_id):
            if other.domain != "sensor" or other.entity_id == energy_entity:
                continue
            state = hass.states.get(other.entity_id)
            dc = other.device_class or other.original_device_class or (
                state.attributes.get("device_class") if state else None
            )
            for m, wanted in _DEVICE_CLASS.items():
                if dc == wanted and m not in found:
                    found[m] = other.entity_id
            if dc == "energy" and "return" in other.entity_id and M_RETURNED not in found:
                found[M_RETURNED] = other.entity_id
    prefix = energy_entity.removesuffix("_energy")
    for m, suffix in _SUFFIX.items():
        if m not in found and hass.states.get(prefix + suffix) is not None:
            found[m] = prefix + suffix
    return found


def _tariff_schema(defaults: dict[str, Any]) -> vol.Schema:
    money = lambda step, unit: sel.NumberSelector(  # noqa: E731
        sel.NumberSelectorConfig(min=0, max=100000, step=step, mode=sel.NumberSelectorMode.BOX, unit_of_measurement=unit)
    )
    hour = sel.NumberSelector(sel.NumberSelectorConfig(min=0, max=23, step=1, mode=sel.NumberSelectorMode.BOX))
    return vol.Schema(
        {
            vol.Required(CONF_SUPPLIER, default=defaults[CONF_SUPPLIER]): sel.TextSelector(),
            vol.Required(CONF_PLAN, default=defaults[CONF_PLAN]): sel.TextSelector(),
            vol.Required(CONF_DAY_RATE, default=defaults[CONF_DAY_RATE]): money("any", "c/kWh"),
            vol.Required(CONF_NIGHT_RATE, default=defaults[CONF_NIGHT_RATE]): money("any", "c/kWh"),
            vol.Required(CONF_STANDING, default=defaults[CONF_STANDING]): money(0.01, "€/year"),
            vol.Required(CONF_PSO, default=defaults[CONF_PSO]): money(0.01, "€/year"),
            vol.Required(CONF_NIGHT_START, default=defaults[CONF_NIGHT_START]): hour,
            vol.Required(CONF_NIGHT_END, default=defaults[CONF_NIGHT_END]): hour,
            vol.Required(CONF_CLOCK, default=defaults[CONF_CLOCK]): sel.SelectSelector(
                sel.SelectSelectorConfig(
                    options=[
                        sel.SelectOptionDict(
                            value="gmt",
                            label="Day/night meter, Irish summer time (DST) taken into account automatically: "
                            "night 23:00–08:00 in winter, 00:00–09:00 in summer",
                        ),
                        sel.SelectOptionDict(
                            value="local",
                            label="Smart tariff: night 23:00–08:00 local time all year",
                        ),
                    ],
                    mode=sel.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(CONF_VALID_FROM, default=defaults[CONF_VALID_FROM]): sel.DateSelector(),
            vol.Optional(CONF_BILL_ANCHOR, description={"suggested_value": defaults.get(CONF_BILL_ANCHOR)}): sel.DateSelector(),
            vol.Required(BILL_MONTHS, default=str(defaults.get(BILL_MONTHS, "2"))): sel.SelectSelector(
                sel.SelectSelectorConfig(
                    options=[
                        sel.SelectOptionDict(value="1", label="Monthly"),
                        sel.SelectOptionDict(value="2", label="Every two months"),
                    ]
                )
            ),
        }
    )


def _as_date(value: Any):
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _period_label(t) -> str:
    return (
        f"From {t.valid_from:%-d %b %Y}: day {t.day_rate * 100:.2f}c, night {t.night_rate * 100:.2f}c, "
        f"standing €{t.standing_per_year:.2f}/yr, PSO €{t.pso_per_year:.2f}/yr"
    )


def _periods_table(schedule) -> str:
    rows = [
        f"| {t.valid_from:%-d %b %Y} | {t.day_rate * 100:.2f} | {t.night_rate * 100:.2f} | "
        f"{t.standing_per_year:.2f} | {t.pso_per_year:.2f} |"
        for t in schedule.periods
    ]
    first = schedule.periods[0].valid_from
    return (
        "| From | Day c/kWh | Night c/kWh | Standing €/year | PSO €/year |\n|:--|--:|--:|--:|--:|\n"
        + "\n".join(rows)
        + f"\n\nBefore {first:%-d %b %Y}: no prices, so those hours are kept but not priced."
    )


def _clean_tariff(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    out[CONF_NIGHT_START] = int(out[CONF_NIGHT_START])
    out[CONF_NIGHT_END] = int(out[CONF_NIGHT_END])
    out[BILL_MONTHS] = int(out.get(BILL_MONTHS, 2))
    out[CONF_VALID_FROM] = str(out[CONF_VALID_FROM])[:10]
    if not out.get(CONF_BILL_ANCHOR):
        out[CONF_BILL_ANCHOR] = None
    return out


def _tariff_errors(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    for key in (CONF_DAY_RATE, CONF_NIGHT_RATE):
        value = float(user_input[key])
        if value <= 0 or value > 200:
            errors[key] = "rate_range"  # c/kWh: anything above €2/kWh is a typo
    if float(user_input[CONF_STANDING]) > 2000:
        errors[CONF_STANDING] = "standing_range"
    return errors


def _sse_schema(defaults: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_SSE_MODE, default=defaults.get(CONF_SSE_MODE, SSE_DRY_RUN)): sel.SelectSelector(
            sel.SelectSelectorConfig(
                options=[
                    sel.SelectOptionDict(value=SSE_OFF, label="Off"),
                    sel.SelectOptionDict(value=SSE_DRY_RUN, label="Dry run: build the reading, never send it"),
                    sel.SelectOptionDict(value=SSE_LIVE, label="Live: send when I press the button"),
                ],
                mode=sel.SelectSelectorMode.LIST,
            )
        ),
        vol.Optional(CONF_SSE_USERNAME, description={"suggested_value": defaults.get(CONF_SSE_USERNAME)}): sel.TextSelector(
            sel.TextSelectorConfig(type=sel.TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Optional(CONF_SSE_PASSWORD, description={"suggested_value": defaults.get(CONF_SSE_PASSWORD)}): sel.TextSelector(
            sel.TextSelectorConfig(type=sel.TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
        vol.Optional(CONF_SSE_ACCOUNT, description={"suggested_value": defaults.get(CONF_SSE_ACCOUNT)}): sel.TextSelector(),
        vol.Optional(CONF_MPRN, description={"suggested_value": defaults.get(CONF_MPRN)}): sel.TextSelector(),
        vol.Required(CONF_SSE_SYNC, default=defaults.get(CONF_SSE_SYNC, True)): sel.BooleanSelector(),
    }
    return vol.Schema(fields)


def _reading_schema(defaults: dict[str, Any]) -> vol.Schema:
    reading = sel.NumberSelector(
        sel.NumberSelectorConfig(min=0, max=999999, step=1, mode=sel.NumberSelectorMode.BOX, unit_of_measurement="kWh")
    )
    return vol.Schema(
        {
            vol.Optional(CONF_READING_DAY, description={"suggested_value": defaults.get(CONF_READING_DAY)}): reading,
            vol.Optional(CONF_READING_NIGHT, description={"suggested_value": defaults.get(CONF_READING_NIGHT)}): reading,
            vol.Optional(CONF_READING_AT, description={"suggested_value": defaults.get(CONF_READING_AT)}): sel.DateTimeSelector(),
        }
    )


def _esb_schema(defaults: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_ESB_MODE, default=ESB_ACCOUNT if defaults.get(CONF_ESB_MODE) == ESB_ACCOUNT else ESB_OFF): sel.SelectSelector(
            sel.SelectSelectorConfig(
                options=[
                    sel.SelectOptionDict(value=ESB_OFF, label="Off"),
                    sel.SelectOptionDict(value=ESB_ACCOUNT, label="My ESB Networks account"),
                ],
                mode=sel.SelectSelectorMode.LIST,
            )
        ),
        vol.Optional(CONF_ESB_USERNAME, description={"suggested_value": defaults.get(CONF_ESB_USERNAME)}): sel.TextSelector(
            sel.TextSelectorConfig(type=sel.TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Optional(CONF_ESB_PASSWORD, description={"suggested_value": defaults.get(CONF_ESB_PASSWORD)}): sel.TextSelector(
            sel.TextSelectorConfig(type=sel.TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
        vol.Optional(CONF_MPRN, description={"suggested_value": defaults.get(CONF_MPRN)}): sel.TextSelector(),
        vol.Required(
            CONF_ESB_PER_DAY,
            default=str(min(int(defaults.get(CONF_ESB_PER_DAY, DEFAULT_ESB_PER_DAY)), MAX_ESB_PER_DAY)),
        ): sel.SelectSelector(
            sel.SelectSelectorConfig(
                options=[
                    sel.SelectOptionDict(value="0", label="Manual only (button or service)"),
                    sel.SelectOptionDict(value="1", label="Once a day"),
                    sel.SelectOptionDict(value="2", label="Twice a day (the most ESB tolerates)"),
                ]
            )
        ),
    }
    return vol.Schema(fields)


def _guess_weather(hass: HomeAssistant) -> str | None:
    """An AccuWeather weather entity if one is configured in Home Assistant."""
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.domain == "weather" and entry.platform == "accuweather":
            return entry.entity_id
    return None


async def _guess_location(hass: HomeAssistant) -> tuple[dict[str, float], str]:
    """(pin, where it came from): the internet connection's location, else Home Assistant's home.

    The same look-up Home Assistant's own onboarding does. A few kilometres out at worst,
    which is why the map is there to drag the pin onto the house.
    """
    try:
        async with asyncio.timeout(10):
            info = await async_detect_location_info(async_get_clientsession(hass))
    except Exception:  # noqa: BLE001 - best effort; the map works without it
        info = None
    if info is not None and info.latitude is not None and info.longitude is not None:
        place = ", ".join(p for p in (info.city, info.region_name) if p) or "your area"
        return {CONF_LATITUDE: float(info.latitude), CONF_LONGITUDE: float(info.longitude)}, (
            f"The pin starts where your internet connection says you are ({place}), which is only approximate."
        )
    return {CONF_LATITUDE: float(hass.config.latitude), CONF_LONGITUDE: float(hass.config.longitude)}, (
        "The pin starts at Home Assistant's home location."
    )


def _weather_schema(defaults: dict[str, Any], hass: HomeAssistant, pin: dict[str, float]) -> vol.Schema:
    weather_default = defaults.get(CONF_WEATHER_ENTITY) or _guess_weather(hass)
    return vol.Schema(
        {
            vol.Required(CONF_LOCATION, default=pin): sel.LocationSelector(
                sel.LocationSelectorConfig(radius=False, icon="mdi:home-lightning-bolt")
            ),
            vol.Optional(CONF_WEATHER_ENTITY, description={"suggested_value": weather_default}): sel.EntitySelector(
                sel.EntitySelectorConfig(domain="weather")
            ),
        }
    )


def _clean_weather(user_input: dict[str, Any]) -> dict[str, Any]:
    pin = user_input[CONF_LOCATION]
    return {
        CONF_LATITUDE: round(float(pin[CONF_LATITUDE]), 5),
        CONF_LONGITUDE: round(float(pin[CONF_LONGITUDE]), 5),
        CONF_WEATHER_ENTITY: user_input.get(CONF_WEATHER_ENTITY),
    }


def _energy_mode_default(defaults: dict[str, Any]) -> str:
    mode = defaults.get(CONF_ENERGY_MODE)
    if mode in ("replace", "add", "off"):
        return mode
    return "replace" if defaults.get(CONF_ENERGY_PREFS, True) else "off"


def _dashboard_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_DASHBOARD_TITLE, default=defaults.get(CONF_DASHBOARD_TITLE, DEFAULT_DASHBOARD_TITLE)): sel.TextSelector(),
            vol.Required(CONF_DASHBOARD_PATH, default=defaults.get(CONF_DASHBOARD_PATH, DEFAULT_DASHBOARD_PATH)): sel.TextSelector(),
            vol.Required(CONF_ENERGY_MODE, default=_energy_mode_default(defaults)): sel.SelectSelector(
                sel.SelectSelectorConfig(
                    options=[
                        sel.SelectOptionDict(
                            value="replace",
                            label="Use the full-house clamp as the Energy dashboard's grid connection, "
                            "replacing any other grid connection (a copy of it is kept)",
                        ),
                        sel.SelectOptionDict(
                            value="add",
                            label="Add the full-house clamp only if the Energy dashboard has no grid connection yet",
                        ),
                        sel.SelectOptionDict(value="off", label="Leave the Energy dashboard alone"),
                    ],
                    mode=sel.SelectSelectorMode.LIST,
                )
            ),
        }
    )


def _clean_dashboard(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    # The 1.0.x on/off key follows the choice, so anything reading it agrees.
    out[CONF_ENERGY_PREFS] = out.get(CONF_ENERGY_MODE, "replace") != "off"
    return out


def _parse_reading_time(value: Any) -> str | None:
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return parsed.astimezone(IRELAND).isoformat()


def _known_reading() -> dict[str, Any]:
    """The reading pre-filled at setup, shown in Home Assistant's time zone."""
    when = datetime.strptime(DEFAULT_READING_AT, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IRELAND)
    return {
        CONF_READING_DAY: DEFAULT_READING_DAY,
        CONF_READING_NIGHT: DEFAULT_READING_NIGHT,
        CONF_READING_AT: when.astimezone(dt_util.get_default_time_zone()).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _reading_defaults(meter: MeterInfo | None) -> tuple[dict[str, Any], bool]:
    """(defaults, from_sse): the known reading, unless SSE holds a newer one."""
    known, sse = _known_reading(), _sse_reading_defaults(meter)
    if sse.get(CONF_READING_AT) and _parse_reading_time(sse[CONF_READING_AT]) > _parse_reading_time(known[CONF_READING_AT]):
        return sse, True
    return known, False


def _sse_reading_defaults(meter: MeterInfo | None) -> dict[str, Any]:
    """SSE's last accepted reading, offered as the starting reading."""
    if meter is None:
        return {}
    day, night = meter.register("day"), meter.register("night")
    if day is None or night is None or day.last_value is None or night.last_value is None:
        return {}
    stamp = day.last_inserted or day.last_date
    when = None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
        try:
            when = datetime.strptime(str(stamp), fmt).replace(tzinfo=IRELAND)
            break
        except (TypeError, ValueError):
            continue
    return {
        CONF_READING_DAY: int(day.last_value),
        CONF_READING_NIGHT: int(night.last_value),
        CONF_READING_AT: when.strftime("%Y-%m-%d %H:%M:%S") if when else None,
    }


async def _probe_sse(hass: HomeAssistant, data: dict[str, Any]) -> tuple[MeterInfo | None, str | None]:
    """One read-only sign-in. (meter, error key). Never submits."""
    http = async_create_clientsession(hass, auto_cleanup=False, cookie_jar=new_cookie_jar())
    kwargs: dict[str, Any] = {"account": data.get(CONF_SSE_ACCOUNT), "mprn": data.get(CONF_MPRN)}
    try:
        return await SseClient(http, data[CONF_SSE_USERNAME], data[CONF_SSE_PASSWORD], **kwargs).meter_info(), None
    except AuthFailed as exc:
        LOG.info("SSE sign-in rejected during setup: %s", exc)
        return None, "sse_auth"
    except CaptchaEncountered:
        return None, "sse_captcha"
    except (PortalError, aiohttp.ClientError, TimeoutError) as exc:
        LOG.info("SSE could not be checked during setup: %s", exc)
        return None, "sse_unreachable"
    finally:
        http.detach()


async def _probe_esb(hass: HomeAssistant, data: dict[str, Any]) -> str | None:
    """One ESB sign-in attempt. None if fine, else an error key."""
    http = async_create_clientsession(hass, auto_cleanup=False, cookie_jar=new_cookie_jar())
    try:
        client = EsbClient(http, data[CONF_ESB_USERNAME], data[CONF_ESB_PASSWORD], str(data.get(CONF_MPRN, "")).replace(" ", ""))
        await client.ensure_session()
        hass.data[DATA_ESB_HANDOFF] = client.export_session()
        return None
    except CaptchaEncountered:
        return "esb_captcha"
    except AuthFailed as exc:
        return "esb_auth" if "rejected" in str(exc) else "esb_unreachable"
    except (PortalError, aiohttp.ClientError, TimeoutError):
        return "esb_unreachable"
    finally:
        http.detach()


class ElectricityAnalyzerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    # 2: the default tariff starts on 6 October with a €19.10 PSO (see async_migrate_entry).
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self._sse_meter: MeterInfo | None = None

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return ElectricityAnalyzerOptionsFlow()

    # 1 -------------------------------------------------------------- meters
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            house = user_input[conf_entity(CONF_HOUSE, M_ENERGY)]
            hall = user_input.get(conf_entity(CONF_HALL, M_ENERGY))
            if hall and hall == house:
                errors["base"] = "same_entity"
            else:
                self.data.update(user_input)
                return await self.async_step_channels()
        schema = vol.Schema(
            {
                vol.Required(
                    conf_entity(CONF_HOUSE, M_ENERGY), description={"suggested_value": _guess_energy(self.hass, CONF_HOUSE)}
                ): _energy_selector(),
                vol.Optional(
                    conf_entity(CONF_HALL, M_ENERGY), description={"suggested_value": _guess_energy(self.hass, CONF_HALL)}
                ): _energy_selector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # 2 ------------------------------------------------------------ channels
    async def async_step_channels(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for channel in (CONF_HOUSE, CONF_HALL):
                for m, entity_id in (user_input.get(channel) or {}).items():
                    if entity_id:
                        self.data[conf_entity(channel, m)] = entity_id
            if not self.data.get(conf_entity(CONF_HOUSE, M_POWER)):
                return self.async_show_form(
                    step_id="channels", data_schema=self._channels_schema(), errors={"base": "need_power"}
                )
            return await self.async_step_tariff()
        return self.async_show_form(step_id="channels", data_schema=self._channels_schema())

    def _channels_schema(self) -> vol.Schema:
        fields: dict[Any, Any] = {}
        for channel in (CONF_HOUSE, CONF_HALL):
            energy = self.data.get(conf_entity(channel, M_ENERGY))
            if not energy:
                continue
            found = detect_companions(self.hass, energy)
            inner = {
                vol.Optional(m, description={"suggested_value": found.get(m)}): _entity_selector(m)
                for m in MEASUREMENTS
                if m != M_ENERGY
            }
            fields[vol.Required(channel)] = section(vol.Schema(inner), {"collapsed": False})
        return vol.Schema(fields)

    # 3 -------------------------------------------------------------- tariff
    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _tariff_errors(user_input)
            if not errors:
                self.data.update(_clean_tariff(user_input))
                return await self.async_step_sse()
        return self.async_show_form(
            step_id="tariff", data_schema=_tariff_schema({**tariff_defaults(), **(user_input or {})}), errors=errors
        )

    # 4 ----------------------------------------------------------------- SSE
    async def async_step_sse(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            mode = user_input[CONF_SSE_MODE]
            if mode != SSE_OFF and not (user_input.get(CONF_SSE_USERNAME) and user_input.get(CONF_SSE_PASSWORD)):
                errors["base"] = "sse_credentials"
            elif mode == SSE_LIVE:
                meter, error = await _probe_sse(self.hass, user_input)
                if error == "sse_auth":
                    errors["base"] = error
                else:
                    self._sse_meter = meter
                    if error:
                        LOG.info("continuing without an SSE check (%s)", error)
            if not errors:
                self.data.update(user_input)
                return await self.async_step_esb()
        return self.async_show_form(
            step_id="sse",
            data_schema=_sse_schema(user_input or {}),
            errors=errors,
        )

    # 5 ----------------------------------------------------------------- ESB
    async def async_step_esb(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _esb_errors(self.hass, user_input)
            if not errors:
                mprn = user_input.get(CONF_MPRN) or self.data.get(CONF_MPRN)
                user_input[CONF_ESB_PER_DAY] = int(user_input.get(CONF_ESB_PER_DAY, DEFAULT_ESB_PER_DAY))
                self.data.update(user_input)
                if mprn:
                    self.data[CONF_MPRN] = str(mprn).replace(" ", "")
                return await self.async_step_reading()
        return self.async_show_form(
            step_id="esb", data_schema=_esb_schema(user_input or {CONF_MPRN: self.data.get(CONF_MPRN)}), errors=errors
        )

    # 6 ------------------------------------------------------------- reading
    async def async_step_reading(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _reading_errors(user_input)
            if not errors:
                self.data.update(_clean_reading(user_input))
                return await self.async_step_weather()
        defaults, from_sse = _reading_defaults(self._sse_meter)
        placeholders = {
            "sse": "Pre-filled with the last reading SSE holds for your meter."
            if from_sse
            else "Pre-filled with day {}, night {}, read at {}. Overwrite them if yours are different.".format(
                defaults[CONF_READING_DAY], defaults[CONF_READING_NIGHT], defaults[CONF_READING_AT]
            ),
            "now": dt_util.now().strftime("%a %-d %b %Y %H:%M"),
        }
        defaults = user_input or defaults
        return self.async_show_form(
            step_id="reading",
            data_schema=_reading_schema(defaults),
            errors=errors,
            description_placeholders=placeholders,
        )

    # 7 ------------------------------------------------------------- weather
    async def async_step_weather(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.data.update(_clean_weather(user_input))
            return await self.async_step_dashboard()
        pin, origin = await _guess_location(self.hass)
        return self.async_show_form(
            step_id="weather",
            data_schema=_weather_schema({}, self.hass, pin),
            description_placeholders={"origin": origin},
        )

    # 8 ----------------------------------------------------------- dashboard
    async def async_step_dashboard(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.data.update(_clean_dashboard(user_input))
            return self.async_create_entry(title=NAME, data=self.data)
        return self.async_show_form(step_id="dashboard", data_schema=_dashboard_schema({}))


async def _esb_errors(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    """With an account: the details are all there, and one sign-in works."""
    if user_input[CONF_ESB_MODE] != ESB_ACCOUNT:
        return {}
    if not (user_input.get(CONF_ESB_USERNAME) and user_input.get(CONF_ESB_PASSWORD) and user_input.get(CONF_MPRN)):
        return {"base": "esb_credentials"}
    error = await _probe_esb(hass, user_input)
    return {"base": error} if error else {}


def _reading_errors(user_input: dict[str, Any]) -> dict[str, str]:
    day, night = user_input.get(CONF_READING_DAY), user_input.get(CONF_READING_NIGHT)
    if (day is None) != (night is None):
        return {"base": "reading_both"}
    if day is not None and not user_input.get(CONF_READING_AT):
        return {CONF_READING_AT: "reading_time"}
    when = _parse_reading_time(user_input.get(CONF_READING_AT))
    if day is not None and when is not None and dt_util.parse_datetime(when) > dt_util.now():
        return {CONF_READING_AT: "reading_future"}
    return {}


def _clean_reading(user_input: dict[str, Any]) -> dict[str, Any]:
    day, night = user_input.get(CONF_READING_DAY), user_input.get(CONF_READING_NIGHT)
    return {
        CONF_READING_DAY: None if day is None else float(day),
        CONF_READING_NIGHT: None if night is None else float(night),
        CONF_READING_AT: _parse_reading_time(user_input.get(CONF_READING_AT)) if day is not None else None,
    }


class ElectricityAnalyzerOptionsFlow(OptionsFlow):
    """Change anything later. Saving reloads the integration."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["rates", "tariff", "reading", "sse", "esb", "weather", "channels", "dashboard"],
        )

    @property
    def _cfg(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    def _save(self, changes: dict[str, Any]) -> ConfigFlowResult:
        self.hass.config_entries.async_update_entry(self.config_entry, data={**self.config_entry.data, **changes})
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A NEW tariff period from a date. Hours from that date are re-priced; earlier ones keep their price."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _tariff_errors(user_input)
            if not errors:
                return self._save(_clean_tariff(user_input))
        defaults = {**tariff_defaults(), **{k: v for k, v in self._cfg.items() if v is not None}}
        defaults[BILL_MONTHS] = str(defaults.get(BILL_MONTHS, 2))
        return self.async_show_form(step_id="tariff", data_schema=_tariff_schema(defaults), errors=errors)

    def _analyzer(self):
        return getattr(self.config_entry, "runtime_data", None)

    async def async_step_rates(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Prices: always the same four (day, night, standing charge, PSO), for one of three
        spans, or remove a period. Stored, and every hour re-priced, at once (no reload)."""
        if self._analyzer() is None:
            return self.async_abort(reason="not_loaded")
        return self.async_show_menu(
            step_id="rates",
            menu_options=["rates_period", "rates_now", "rates_future", "rates_remove"],
            description_placeholders={"periods": _periods_table(self._analyzer().schedule)},
        )

    async def async_step_rates_period(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """From one day to another, e.g. the previous plan for days that have no price."""
        return await self._rates_form("rates_period", user_input)

    async def async_step_rates_now(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """New prices from today onwards (until the next period, if one is already set)."""
        return await self._rates_form("rates_now", user_input)

    async def async_step_rates_future(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """New prices from a future day; today's prices carry on until then."""
        return await self._rates_form("rates_future", user_input)

    async def _rates_form(self, step_id: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        analyzer = self._analyzer()
        if analyzer is None or analyzer.store is None:
            return self.async_abort(reason="not_loaded")
        today = dt_util.now().astimezone(IRELAND).date()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _tariff_errors(user_input)
            if not errors:
                if step_id == "rates_now":
                    start, end = today, None
                else:
                    start = _as_date(user_input[CONF_VALID_FROM])
                    end = _as_date(user_input["valid_to"]) if step_id == "rates_period" else None
                    if step_id == "rates_future" and start <= today:
                        errors["base"] = "not_future"
            if not errors:
                tariff = replace(
                    analyzer.schedule.for_date(start),  # same meter clock and names as then
                    valid_from=start,
                    day_rate=round(float(user_input[CONF_DAY_RATE]) / 100, 6),
                    night_rate=round(float(user_input[CONF_NIGHT_RATE]) / 100, 6),
                    standing_per_year=float(user_input[CONF_STANDING]),
                    pso_per_year=float(user_input[CONF_PSO]),
                )
                try:
                    await analyzer.async_set_tariff_period(tariff, end)
                except TariffPeriodError as exc:
                    errors["base"] = exc.code
                else:
                    return self.async_create_entry(title="", data=dict(self.config_entry.options))

        current = analyzer.schedule.current
        gap = await self._unpriced_days(analyzer)
        defaults: dict[str, Any] = {
            CONF_DAY_RATE: round(current.day_rate * 100, 4),
            CONF_NIGHT_RATE: round(current.night_rate * 100, 4),
            CONF_STANDING: current.standing_per_year,
            CONF_PSO: current.pso_per_year,
        }
        if step_id == "rates_period" and gap:
            defaults[CONF_VALID_FROM], defaults["valid_to"] = gap[0].isoformat(), gap[1].isoformat()
        if step_id == "rates_future":
            defaults[CONF_VALID_FROM] = (today + timedelta(days=1)).isoformat()
        defaults.update(user_input or {})

        money = lambda step, unit: sel.NumberSelector(  # noqa: E731
            sel.NumberSelectorConfig(min=0, max=100000, step=step, mode=sel.NumberSelectorMode.BOX, unit_of_measurement=unit)
        )
        fields: dict[Any, Any] = {}
        if step_id != "rates_now":
            fields[vol.Required(CONF_VALID_FROM, description={"suggested_value": defaults.get(CONF_VALID_FROM)})] = sel.DateSelector()
        if step_id == "rates_period":
            fields[vol.Required("valid_to", description={"suggested_value": defaults.get("valid_to")})] = sel.DateSelector()
        fields.update({
            vol.Required(CONF_DAY_RATE, default=defaults[CONF_DAY_RATE]): money("any", "c/kWh"),
            vol.Required(CONF_NIGHT_RATE, default=defaults[CONF_NIGHT_RATE]): money("any", "c/kWh"),
            vol.Required(CONF_STANDING, default=defaults[CONF_STANDING]): money(0.01, "€/year"),
            vol.Required(CONF_PSO, default=defaults[CONF_PSO]): money(0.01, "€/year"),
        })
        return self.async_show_form(
            step_id=step_id, data_schema=vol.Schema(fields), errors=errors,
            description_placeholders={
                "periods": _periods_table(analyzer.schedule),
                "gap": (
                    f"Stored hours with no prices: {gap[0]:%-d %b %Y} – {gap[1]:%-d %b %Y} (pre-filled below)."
                    if gap else "Every stored hour has prices."
                ),
                "today": f"{today:%-d %b %Y}",
            },
        )

    async def _unpriced_days(self, analyzer) -> tuple[Any, Any] | None:
        """(first, last) local day of stored hours before the first tariff period, if any."""
        first, _ = await self.hass.async_add_executor_job(analyzer.store.hour_span)
        if first is None:
            return None
        first_day = first.astimezone(IRELAND).date()
        last_day = analyzer.schedule.first_priced_day - timedelta(days=1)
        return (first_day, last_day) if first_day <= last_day else None

    async def async_step_rates_remove(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        analyzer = self._analyzer()
        if analyzer is None or analyzer.store is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await analyzer.async_remove_tariff_period(
                    datetime.strptime(str(user_input[CONF_VALID_FROM])[:10], "%Y-%m-%d").date()
                )
            except TariffPeriodError as exc:
                errors["base"] = exc.code
            else:
                return self.async_create_entry(title="", data=dict(self.config_entry.options))
        options = [
            sel.SelectOptionDict(value=t.valid_from.isoformat(), label=_period_label(t))
            for t in analyzer.schedule.periods
        ]
        schema = vol.Schema(
            {vol.Required(CONF_VALID_FROM): sel.SelectSelector(sel.SelectSelectorConfig(options=options, mode=sel.SelectSelectorMode.LIST))}
        )
        return self.async_show_form(
            step_id="rates_remove", data_schema=schema, errors=errors,
            description_placeholders={"periods": _periods_table(analyzer.schedule)},
        )

    async def async_step_reading(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _reading_errors(user_input)
            if not errors:
                return self._save(_clean_reading(user_input))
        cfg = self._cfg
        defaults = (
            {
                CONF_READING_DAY: cfg[CONF_READING_DAY],
                CONF_READING_NIGHT: cfg[CONF_READING_NIGHT],
                CONF_READING_AT: dt_util.parse_datetime(cfg[CONF_READING_AT])
                .astimezone(dt_util.get_default_time_zone())
                .strftime("%Y-%m-%d %H:%M:%S"),
            }
            if cfg.get(CONF_READING_DAY) is not None and cfg.get(CONF_READING_AT)
            else _known_reading()
        )
        return self.async_show_form(
            step_id="reading",
            data_schema=_reading_schema(defaults),
            errors=errors,
            description_placeholders={"sse": "", "now": dt_util.now().strftime("%a %-d %b %Y %H:%M")},
        )

    def _unchanged(self, user_input: dict[str, Any], keys: tuple[str, ...]) -> bool:
        """Same mode and sign-in details as the running entry (nothing to re-check)."""
        cfg = self._cfg
        return all(str(user_input.get(k) or "").strip() == str(cfg.get(k) or "").strip() for k in keys)

    async def async_step_sse(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_SSE_MODE] != SSE_OFF and not (
                user_input.get(CONF_SSE_USERNAME) and user_input.get(CONF_SSE_PASSWORD)
            ):
                errors["base"] = "sse_credentials"
            elif user_input[CONF_SSE_MODE] == SSE_LIVE and not self._unchanged(
                user_input, (CONF_SSE_MODE, CONF_SSE_USERNAME, CONF_SSE_PASSWORD, CONF_SSE_ACCOUNT, CONF_MPRN)
            ):
                _, error = await _probe_sse(self.hass, user_input)
                if error == "sse_auth":
                    errors["base"] = error
            if not errors:
                return self._save(user_input)
        return self.async_show_form(
            step_id="sse", data_schema=_sse_schema(user_input or self._cfg), errors=errors
        )

    async def async_step_esb(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_MPRN):
                user_input[CONF_MPRN] = str(user_input[CONF_MPRN]).replace(" ", "")
            # Changing only how often to fetch must not sign in again: every sign-in
            # counts towards the two ESB tolerates in 24 hours before its CAPTCHA, and
            # the running integration already holds a working session.
            if self._unchanged(user_input, (CONF_ESB_MODE, CONF_ESB_USERNAME, CONF_ESB_PASSWORD, CONF_MPRN)):
                errors = {}
            else:
                errors = await _esb_errors(self.hass, user_input)
            if not errors:
                user_input[CONF_ESB_PER_DAY] = int(user_input.get(CONF_ESB_PER_DAY, DEFAULT_ESB_PER_DAY))
                return self._save(user_input)
        return self.async_show_form(step_id="esb", data_schema=_esb_schema(user_input or self._cfg), errors=errors)

    async def async_step_weather(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(_clean_weather(user_input))
        cfg = self._cfg
        if cfg.get(CONF_LATITUDE) is not None and cfg.get(CONF_LONGITUDE) is not None:
            pin = {CONF_LATITUDE: cfg[CONF_LATITUDE], CONF_LONGITUDE: cfg[CONF_LONGITUDE]}
            origin = "The pin is where the weather is fetched for now."
        else:
            pin, origin = await _guess_location(self.hass)
        return self.async_show_form(
            step_id="weather",
            data_schema=_weather_schema(cfg, self.hass, pin),
            description_placeholders={"origin": origin},
        )

    async def async_step_channels(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            changes: dict[str, Any] = {}
            for channel in (CONF_HOUSE, CONF_HALL):
                for m in MEASUREMENTS:
                    changes[conf_entity(channel, m)] = (user_input.get(channel) or {}).get(m)
            if not changes.get(conf_entity(CONF_HOUSE, M_ENERGY)):
                return self.async_show_form(step_id="channels", data_schema=self._schema(), errors={"base": "need_house"})
            return self._save(changes)
        return self.async_show_form(step_id="channels", data_schema=self._schema())

    def _schema(self) -> vol.Schema:
        cfg = self._cfg
        fields: dict[Any, Any] = {}
        for channel in (CONF_HOUSE, CONF_HALL):
            inner = {
                vol.Optional(m, description={"suggested_value": cfg.get(conf_entity(channel, m))}): (
                    _energy_selector() if m == M_ENERGY else _entity_selector(m)
                )
                for m in MEASUREMENTS
            }
            fields[vol.Required(channel)] = section(vol.Schema(inner), {"collapsed": False})
        return vol.Schema(fields)

    async def async_step_dashboard(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(_clean_dashboard(user_input))
        return self.async_show_form(step_id="dashboard", data_schema=_dashboard_schema(self._cfg))
