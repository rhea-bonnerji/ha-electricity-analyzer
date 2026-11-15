"""Constants for Electricity Analyzer.

Values that describe the outside world (tariffs, portals, Irish time-of-use rules)
live in `engine/` so the pure modules can be exercised without Home Assistant. This
file holds what only the Home Assistant side needs: config keys, entity keys,
statistic ids, dashboard wiring.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "electricity_analyzer"
NAME: Final = "Electricity Analyzer"
# Kept equal to manifest.json's "version" (tests/test_release.py checks it).
VERSION: Final = "1.0.4"

# ------------------------------------------------------------------ config keys
#
# Shelly channels. The full-house clamp (120 A) is the TOTAL and the only thing
# that ever reaches Home Assistant's Energy dashboard. The hall clamp (50 A) is a
# sub-circuit INSIDE that total, so the two are never added together.

CONF_HOUSE: Final = "house"
CONF_HALL: Final = "hall"

# Per channel, which Shelly measurement each entity provides.
M_ENERGY: Final = "energy"
M_POWER: Final = "power"
M_APPARENT: Final = "apparent_power"
M_PF: Final = "power_factor"
M_VOLTAGE: Final = "voltage"
M_CURRENT: Final = "current"
M_FREQUENCY: Final = "frequency"
M_RETURNED: Final = "energy_returned"
MEASUREMENTS: Final = (
    M_ENERGY,
    M_POWER,
    M_APPARENT,
    M_PF,
    M_VOLTAGE,
    M_CURRENT,
    M_FREQUENCY,
    M_RETURNED,
)
REQUIRED_MEASUREMENTS: Final = (M_ENERGY, M_POWER)


def conf_entity(channel: str, measurement: str) -> str:
    """Config key for one channel's entity, e.g. `house_energy`."""
    return f"{channel}_{measurement}"


# Tariff (all money INCLUDING VAT, as printed on a price sheet).
CONF_SUPPLIER: Final = "supplier"
CONF_PLAN: Final = "plan"
CONF_DAY_RATE: Final = "day_rate"          # cent/kWh in the form, €/kWh stored
CONF_NIGHT_RATE: Final = "night_rate"
CONF_STANDING: Final = "standing_charge"   # €/year
CONF_PSO: Final = "pso_levy"               # €/year
CONF_NIGHT_START: Final = "night_start"    # hour 0-23
CONF_NIGHT_END: Final = "night_end"
CONF_CLOCK: Final = "night_clock"          # gmt | local
CONF_VALID_FROM: Final = "valid_from"
CONF_BILL_ANCHOR: Final = "bill_anchor"    # a date a billing period started

# Meter reading anchor: a real reading of the meter's registers and when it was taken.
CONF_READING_DAY: Final = "reading_day"
CONF_READING_NIGHT: Final = "reading_night"
CONF_READING_AT: Final = "reading_at"
# Pre-filled example reading at setup; overwrite with your meter's current reading.
DEFAULT_READING_DAY: Final = 13876
DEFAULT_READING_NIGHT: Final = 5660
DEFAULT_READING_AT: Final = "2026-10-07 18:50:34"

# ESB Networks.
CONF_ESB_MODE: Final = "esb_mode"
ESB_OFF: Final = "off"
ESB_ACCOUNT: Final = "account"
CONF_ESB_USERNAME: Final = "esb_username"
CONF_ESB_PASSWORD: Final = "esb_password"
CONF_MPRN: Final = "mprn"
# hass.data key: the session from the sign-in that setup (or the options) just made,
# handed to the running integration. Without it the integration signs in again seconds
# later, and ESB answers that second sign-in with a CAPTCHA.
DATA_ESB_HANDOFF: Final = f"{DOMAIN}_esb_handoff"
# Automatic ESB fetches per day (0 = manual only). Each one first asks ESB whether
# anything new has been published, and only then downloads.
CONF_ESB_PER_DAY: Final = "esb_fetches_per_day"
DEFAULT_ESB_PER_DAY: Final = 1
MAX_ESB_PER_DAY: Final = 2
# Fresh ESB sign-ins allowed in any 24 hours, setup's included. A third brings on ESB's
# CAPTCHA. Fetches that reuse the saved session are not sign-ins and do not count.
ESB_MAX_SIGN_INS: Final = 2

# SSE Airtricity meter-reading submission.
CONF_SSE_MODE: Final = "sse_mode"
SSE_OFF: Final = "off"
SSE_DRY_RUN: Final = "dry_run"
SSE_LIVE: Final = "live"
CONF_SSE_USERNAME: Final = "sse_username"
CONF_SSE_PASSWORD: Final = "sse_password"
CONF_SSE_ACCOUNT: Final = "sse_account"
# Read-only: once a day, read SSE's last reading so readings submitted on SSE's own
# website or app become learning points too. Never submits anything.
CONF_SSE_SYNC: Final = "sse_sync"

# Dashboard.
CONF_DASHBOARD: Final = "dashboard"
CONF_DASHBOARD_TITLE: Final = "dashboard_title"
CONF_DASHBOARD_PATH: Final = "dashboard_path"
CONF_ENERGY_PREFS: Final = "configure_energy_dashboard"  # 1.0.x: on/off only
# How the Energy dashboard's grid connection is managed: replace | add | off
# (see energy.py). Replaces the on/off choice above, which is kept in step with it.
CONF_ENERGY_MODE: Final = "energy_dashboard_mode"
# A Home Assistant weather entity (e.g. the AccuWeather integration) whose forecast is
# blended with Open-Meteo's for the coming days, weighted by how well each has done here.
CONF_WEATHER_ENTITY: Final = "weather_entity"
# Where the weather is fetched for; picked on a map at setup.
CONF_LOCATION: Final = "location"
CONF_LATITUDE: Final = "latitude"
CONF_LONGITUDE: Final = "longitude"
DEFAULT_DASHBOARD_TITLE: Final = "Electricity"
DEFAULT_DASHBOARD_PATH: Final = "electricity"
DEFAULT_DASHBOARD_ICON: Final = "mdi:lightning-bolt-circle"

# ------------------------------------------------------------------- cadence

# The hourly rollup reads the recorder's compiled hour. Compilation happens a few
# seconds after the hour, so the rollup runs a couple of minutes later and always
# re-reads the last day to catch anything compiled late.
ROLLUP_MINUTE: Final = 3
ROLLUP_LOOKBACK: Final = timedelta(hours=26)
# SSE accepts one reading per meter per 24 hours.
SSE_COOLDOWN: Final = timedelta(hours=24)
SSE_SYNC_INTERVAL: Final = timedelta(hours=24)
# After ANY failed ESB or SSE attempt (an outage, a CAPTCHA, a refused sign-in): nothing
# is tried again for this long, not by the schedule, a restart or a button; then once,
# and if that fails too, another twelve hours. Never a burst of attempts.
RETRY_AFTER_FAILURE: Final = timedelta(hours=12)

# ------------------------------------------------------------- statistics
#
# Published as EXTERNAL long-term statistics so the built-in statistics-graph card
# can chart them over any date range, alongside the Shelly entities' own statistics.

STAT_PREFIX: Final = f"{DOMAIN}:"


def stat_id(key: str) -> str:
    return f"{STAT_PREFIX}{key}"


# key -> (name, kind, unit). kind: "energy" (sum), "money" (sum), "level" (mean).
STATISTICS: Final = {
    "house_energy_day": ("House energy (day rate)", "energy", "kWh"),
    "house_energy_night": ("House energy (night rate)", "energy", "kWh"),
    "hall_energy": ("Hall energy", "energy", "kWh"),
    "rest_energy": ("Rest of house energy", "energy", "kWh"),
    "hall_energy_day": ("Hall energy (day rate)", "energy", "kWh"),
    "hall_energy_night": ("Hall energy (night rate)", "energy", "kWh"),
    "cost_day": ("Cost: day units", "money", "EUR"),
    "cost_night": ("Cost: night units", "money", "EUR"),
    "cost_fixed": ("Cost: standing charge and PSO", "money", "EUR"),
    "cost_total": ("Cost: total", "money", "EUR"),
    # What Home Assistant's Energy dashboard reads (energy.py): what the METER counted (ESB's
    # figure once ESB has the hour, the clamp x factor until then, ESB's history before the
    # clamp) and its energy cost at the tariff period in force. Both are rewritten whenever
    # prices change or ESB revises an hour, so the Energy dashboard follows every revision.
    "meter_energy": ("Energy: meter (ESB, clamp until ESB has it)", "energy", "kWh"),
    "cost_energy": ("Cost: energy (day + night units)", "money", "EUR"),
    "hall_cost": ("Cost: hall", "money", "EUR"),
    "rest_cost": ("Cost: rest of house", "money", "EUR"),
    "esb_energy": ("ESB metered energy", "energy", "kWh"),
    "esb_energy_day": ("ESB metered energy (day)", "energy", "kWh"),
    "esb_energy_night": ("ESB metered energy (night)", "energy", "kWh"),
    "register_day": ("Meter register: day (estimated)", "level", "kWh"),
    "register_night": ("Meter register: night (estimated)", "level", "kWh"),
    "rest_power": ("Rest of house power", "level", "W"),
    # Weather (Open-Meteo), hourly. Levels are mean/min/max of the hour's value.
    "outdoor_temperature": ("Outdoor temperature", "level", "°C"),
    "apparent_temperature": ("Apparent temperature", "level", "°C"),
    "humidity": ("Relative humidity", "level", "%"),
    "wind_speed": ("Wind speed", "level", "km/h"),
    "wind_gusts": ("Wind gusts", "level", "km/h"),
    "cloud_cover": ("Cloud cover", "level", "%"),
    "solar_radiation": ("Solar radiation", "level", "W/m²"),
    "pressure": ("Air pressure", "level", "hPa"),
    "precipitation": ("Precipitation", "total", "mm"),
    "heating_degree_days": ("Heating degree days", "total", "°Cd"),
    "heating_degree_days_apparent": ("Heating degree days (feels like)", "total", "°Cd"),
}
WEATHER_STATISTICS: Final = (
    "outdoor_temperature", "apparent_temperature", "humidity", "wind_speed", "wind_gusts",
    "cloud_cover", "solar_radiation", "pressure", "precipitation", "heating_degree_days",
    "heating_degree_days_apparent",
)

# Lets the native statistics-graph cards follow the dashboard's own date picker.
# The frontend insists this key starts with "energy_".
COLLECTION_KEY: Final = "energy_electricity_analyzer"

# ---------------------------------------------------------------- frontend

APEX_CARD: Final = "custom:apexcharts-card"
DATA_CARD: Final = "custom:electricity-analyzer-data-card"
STATIC_BASE: Final = f"/{DOMAIN}_static"
APEX_FILE: Final = "apexcharts-card.js"
APEX_VERSION: Final = "2.2.3"
APEX_MARKER: Final = "apexcharts-card"
DATA_CARD_FILE: Final = "electricity-analyzer-data-card.js"
DATA_CARD_VERSION: Final = "1.0.4"

# ------------------------------------------------------------------- services

SERVICE_REFRESH_ESB: Final = "refresh_esb"
SERVICE_REBUILD: Final = "rebuild"
SERVICE_SET_READING: Final = "set_meter_reading"
SERVICE_SUBMIT_READING: Final = "submit_meter_reading"
SERVICE_ADD_TARIFF: Final = "add_tariff"
SERVICE_SUPPLY_SESSION: Final = "supply_esb_session"
SERVICE_EXPORT: Final = "export_csv"

# Events and dispatcher signals.
SIGNAL_UPDATED: Final = f"{DOMAIN}_updated"
