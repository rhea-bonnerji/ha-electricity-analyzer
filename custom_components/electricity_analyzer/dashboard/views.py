"""The dashboard's tabs, laid out as a grid rather than packed.

LAYOUT RULES (the previous version was "all over the place", so these are strict)

* Every tab is a sections view with `dense_section_placement` OFF: sections are laid
  out in reading order, left to right, so the page has rows, not holes.
* Tile groups are one column wide and hold an even number of half-width tiles, so
  neighbouring groups end at about the same height. Charts are two columns wide and
  always full width inside their section. The date picker and tables span the page.
* Each tab opens with a row of badges: the handful of numbers that tab is about.
* Tabs have text titles, not icons.
* Prose is kept to a line or two; detail lives in attributes, tables and the README.

Every card is optional: a card whose sensor did not resolve is dropped, and a
section left with nothing but its heading is dropped with it.
"""

from __future__ import annotations

from typing import Any

from ..const import stat_id
from .cards import (
    C_DAY,
    C_ESB,
    C_FIXED,
    C_FORECAST,
    C_HALL,
    C_HOUSE,
    C_LAST_YEAR,
    C_NIGHT,
    C_REST,
    C_VOLT,
    Cards,
)

S = stat_id
U = "{% set unknown = ['unknown', 'unavailable', 'none', ''] %}"
MAX_COLUMNS = 4


# ------------------------------------------------------------------ helpers


def section(c: Cards, title: str, icon: str, cards: list[Any], *, span: int = 1,
            badges: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    body = Cards.clean(cards)
    if not body:
        return None
    heading: dict[str, Any] = {"type": "heading", "heading": title, "icon": icon, "heading_style": "title"}
    if badges:
        heading["badges"] = Cards.clean(badges)
    return {"type": "grid", "column_span": span, "cards": [heading, *body]}


def view(title: str, path: str, sections: list[dict[str, Any] | None], *,
         badges: list[dict[str, Any] | None] | None = None, max_columns: int = MAX_COLUMNS) -> dict[str, Any] | None:
    sections = [s for s in sections if s]
    if not sections:
        return None
    out: dict[str, Any] = {
        "type": "sections",
        "title": title,
        "path": path,
        "max_columns": max_columns,
        "dense_section_placement": False,
        "sections": sections,
    }
    badges = [b for b in (badges or []) if b]
    if badges:
        out["badges"] = badges
    return out


def badge(c: Cards, key: str, name: str, *, icon: str | None = None, color: str | None = None) -> dict[str, Any] | None:
    entity_id = c.eid(key)
    if not entity_id:
        return None
    out: dict[str, Any] = {
        "type": "entity",
        "entity": entity_id,
        "name": name,
        "show_name": True,
        "show_state": True,
        "show_icon": True,
    }
    if icon:
        out["icon"] = icon
    if color:
        out["color"] = color
    return out


def tile(c: Cards, key: str, name: str, *, icon: str | None = None, color: str | None = None,
         graph: int | None = None, wide: bool = False) -> dict[str, Any] | None:
    """A tile; `graph` adds a sparkline of the last N hours under the value."""
    entity_id = c.eid(key)
    if not entity_id:
        return None
    card: dict[str, Any] = {
        "type": "tile",
        "entity": entity_id,
        "name": name,
        "vertical": False,
        "grid_options": {"columns": 12 if wide or graph else 6, "rows": 2 if graph else 1},
    }
    if icon:
        card["icon"] = icon
    if color:
        card["color"] = color
    if graph:
        card["features"] = [{"type": "trend-graph", "hours_to_show": graph}]
        card["features_position"] = "bottom"
    return card


def when_known(c: Cards, key: str, card: dict[str, Any] | None) -> dict[str, Any] | None:
    """Show a card only while its entity has a value."""
    entity_id = c.eid(key)
    if card is None or entity_id is None:
        return None
    return {
        "type": "conditional",
        "conditions": [{"condition": "state", "entity": entity_id, "state_not": ["unknown", "unavailable"]}],
        "card": card,
        "grid_options": card.get("grid_options", {}),
    }


def note(text: str) -> dict[str, Any]:
    return {"type": "markdown", "content": text, "text_only": True, "grid_options": {"columns": "full"}}


def full(card: dict[str, Any] | None) -> dict[str, Any] | None:
    if card is not None:
        card["grid_options"] = {"columns": "full"}
    return card


# What state the data is in, from the `collection` attribute of the stored-hours sensor:
# "loading", "collecting since…", "today so far…", "waiting for weather…", "Open-Meteo
# unavailable…". Said in words where a chart would otherwise just be empty.
def _data_note(c: Cards) -> str:
    return c.jinja(
        U + """{% set s = state_attr('$h', 'collection') %}{% if states('$h') | lower in unknown %}\
<small>⏳ Starting up: reading what is already stored…</small>{% elif s and s.state != 'ok' %}\
<small>⏳ {{ s.message }}{% if s.initial_load == 'running' and s.initial_step %} Now: {{ s.initial_step }}…{% endif %}</small>{% endif %}""",
        h="stored_hours",
    )


def _today_note(c: Cards) -> str:
    return c.jinja(
        U + """{% set s = state_attr('$h', 'collection') %}{% if s and s.today_message %}<small>{{ s.today_message }}</small>{% endif %}""",
        h="stored_hours",
    )


def _weather_note(c: Cards) -> str:
    return c.jinja(
        U + """{% set s = state_attr('$h', 'collection') %}{% if s and s.weather_state != 'ok' %}<small>☁️ {{ s.weather_message }}</small>{% endif %}""",
        h="stored_hours",
    )


def _periods_note(c: Cards) -> str:
    """Every tariff period, and where to change them."""
    return c.jinja(
        U + """{% set ts = state_attr('$h', 'tariffs') or [] %}{% if ts %}\
| Prices from | Day | Night | Standing | PSO |
|:--|--:|--:|--:|--:|
{% for t in ts %}| {{ (t.valid_from | as_datetime).strftime('%-d %b %Y') }} | {{ '%.2f' | format(t.day_rate * 100) }}c \
| {{ '%.2f' | format(t.night_rate * 100) }}c | €{{ '%.2f' | format(t.standing_per_year) }}/yr | €{{ '%.2f' | format(t.pso_per_year) }}/yr |
{% endfor %}
<small>Before the first date there are no prices: those hours are kept, not priced. Set prices for any dates, past or \
future, under the integration's *Configure → Tariff periods*; every hour is re-priced at once.</small>{% endif %}""",
        h="stored_hours",
    )


def _today_hours(band: str) -> str:
    """Today's hours from the live sensor (stored hours plus the one filling now), each
    drawn over its own hour: the middle of [start, end), so in Irish summer time the first
    night hour is 00:00-01:00, never a bar straddling midnight."""
    return (
        f"const rows = (entity.attributes.hourly || []).filter(r => r.band === '{band}');"
        " return rows.map(r => [(new Date(r.start).getTime() + new Date(r.end).getTime()) / 2, r.kwh]);"
    )


def _profile(attr: str) -> str:
    # Keys are Irish clock hours. Each point is placed at the middle of that local hour
    # by setting the hour itself, not by adding hours to midnight, which is an hour out
    # after 01:00 on the 23- and 25-hour days the clocks change.
    return (
        f"const p = entity.attributes.{attr} || {{}};"
        " return Object.keys(p).map(h => { const d = new Date(); d.setHours(Number(h), 30, 0, 0);"
        "   return [d.getTime(), p[h]]; }).sort((a, b) => a[0] - b[0]);"
    )


# ------------------------------------------------------------------- overview


def view_overview(c: Cards) -> dict[str, Any] | None:
    meter_line = c.jinja(
        U + """{% set at = state_attr('$rd', 'anchor_taken_at') %}{% if at %}\
<small>From the reading of {{ (at | as_datetime | as_local).strftime('%-d %b %Y %H:%M') }} \
(day {{ state_attr('$rd', 'anchor_reading') | int }}, night {{ state_attr('$rn', 'anchor_reading') | int }}) \
plus the full-house clamp since, × day {{ states('$fd') }} / night {{ states('$fn') }}.</small>{% endif %}""",
        rd="meter_reading_day", rn="meter_reading_night", fd="meter_factor_day", fn="meter_factor_night",
    )
    starting = c.jinja(
        U + """{% set f = states('$f') %}{% if f | lower in unknown %}{% set why = state_attr('$f', 'reason') %}\
<small>⏳ Forecasts start {{ why if why else 'after the first data load finishes' }}. \
Hours stored so far: {{ states('$h') }}.</small>{% endif %}""",
        f="forecast_today_cost", h="stored_hours",
    )
    band_line = c.jinja(
        U + """{% set t = state_attr('$band', 'night_window_tonight') %}{% if t %}<small>Night rate tonight: {{ t }}</small>{% endif %}""",
        band="tariff_band",
    )
    return view(
        "Overview",
        "now",
        [
            section(c, "Live", "mdi:flash", [
                tile(c, "house_power", "Whole house", color="light-blue", graph=6),
                tile(c, "hall_power", "Hall", icon="mdi:fishbowl-outline", color="teal", graph=6),
                tile(c, "rest_power", "Rest of house", color="blue", graph=6),
                tile(c, "price_now", "Price now", icon="mdi:cash-fast", color="amber"),
                tile(c, "spend_rate", "Spending now", color="amber"),
            ]),
            section(c, "Today", "mdi:calendar-today", [
                tile(c, "today_energy", "Energy", color="light-blue"),
                tile(c, "today_cost", "Cost", color="amber"),
                tile(c, "today_day_energy", "Day units", icon="mdi:white-balance-sunny", color="amber"),
                tile(c, "today_night_energy", "Night units", icon="mdi:weather-night", color="indigo"),
                tile(c, "today_hall_energy", "Hall", icon="mdi:fishbowl-outline", color="teal"),
                tile(c, "forecast_today_cost", "Forecast", icon="mdi:crystal-ball", color="purple"),
                tile(c, "next_band_change", "Rate changes", icon="mdi:clock-start"),
                tile(c, "unit_rate", "Unit rate", icon="mdi:currency-eur"),
                note(band_line),
                note(_data_note(c)),
                note(starting),
            ]),
            section(c, "Meter now", "mdi:counter", [
                tile(c, "meter_reading_day", "Day register", icon="mdi:white-balance-sunny", color="amber", wide=True),
                tile(c, "meter_reading_night", "Night register", icon="mdi:weather-night", color="indigo", wide=True),
                note(meter_line),
                tile(c, "sse_status", "SSE"),
                when_known(c, "sse_next_allowed", tile(c, "sse_next_allowed", "Next send")),
            ]),
            section(c, "This month", "mdi:calendar-month", [
                tile(c, "month_energy", "Energy", color="light-blue"),
                tile(c, "month_cost", "Cost", color="amber"),
                tile(c, "forecast_month_cost", "Forecast", icon="mdi:crystal-ball", color="purple"),
                tile(c, "bill_estimate", "Next bill", icon="mdi:receipt-text-outline", color="purple"),
                tile(c, "avg_daily_cost", "Per day (30 d)"),
                tile(c, "week_change", "vs last week", icon="mdi:swap-vertical"),
                tile(c, "outdoor_temperature", "Outside", icon="mdi:thermometer"),
                tile(c, "heating_degree_days_today", "Degree days", icon="mdi:snowflake-thermometer"),
            ]),
            section(c, "Power, last 24 hours", "mdi:chart-line", [full(c.chart(
                "Hourly average",
                "Whole house, the hall circuit and everything else.",
                [
                    c.stat_series("Whole house", "house_power", period="hour", colour=C_HOUSE, unit=" W", value_key="mean", series_type="area", precision=0),
                    c.stat_series("Hall", "hall_power", period="hour", colour=C_HALL, unit=" W", value_key="mean", series_type="line", precision=0),
                    c.stat_series("Rest of house", S("rest_power"), period="hour", colour=C_REST, unit=" W", value_key="mean", series_type="line", precision=0),
                ],
                span="24h", end="hour", default_type="line", label_format="HH:mm", y_title="W", decimals=0, update_interval="5min",
            ))], span=2),
            section(c, "Today, hour by hour", "mdi:chart-bar", [full(c.chart(
                "kWh per hour",
                "Amber is day rate, indigo night rate. The current hour fills as it goes.",
                [
                    c.attr_series("Day rate", "today_energy", _today_hours("day"), colour=C_DAY),
                    c.attr_series("Night rate", "today_energy", _today_hours("night"), colour=C_NIGHT),
                ],
                span="1d", stacked=True, label_format="HH:mm", y_title="kWh", decimals=2, update_interval="30s",
                no_data="Nothing measured yet today.",
            )), note(_today_note(c))], span=2),
        ],
        badges=[
            badge(c, "price_now", "Price", icon="mdi:cash-fast", color="amber"),
            badge(c, "house_power", "House", color="light-blue"),
            badge(c, "hall_power", "Hall", icon="mdi:fishbowl-outline", color="teal"),
            badge(c, "spend_rate", "Spending", color="amber"),
            badge(c, "today_cost", "Today", color="amber"),
            badge(c, "tariff_band", "Rate", color="indigo"),
            badge(c, "meter_reading_day", "Day reg.", icon="mdi:white-balance-sunny"),
            badge(c, "meter_reading_night", "Night reg.", icon="mdi:weather-night"),
        ],
    )


# --------------------------------------------------------------------- clamps


def _clamp(c: Cards, prefix: str, title: str, color: str) -> dict[str, Any] | None:
    pf = f"{prefix}_power_factor_live"
    return section(c, title, "mdi:current-ac", [
        tile(c, f"{prefix}_power", "Power", color=color, graph=24),
        tile(c, f"{prefix}_apparent_power", "Apparent power", color=color),
        tile(c, pf, "Power factor", color=color),
        tile(c, f"{prefix}_voltage", "Voltage", color=color),
        tile(c, f"{prefix}_current", "Current", color=color),
        tile(c, f"{prefix}_frequency", "Frequency", color=color),
        tile(c, f"{prefix}_energy_returned", "Energy returned", color=color),
        tile(c, f"{prefix}_energy", "Energy (counter)", color=color, wide=True),
    ])


def _pair(c: Cards, title: str, caption: str, key: str, value_key: str, unit: str, decimals: int, *, span: str = "48h") -> dict[str, Any] | None:
    return full(c.chart(
        title,
        caption,
        [
            c.stat_series("Whole house", f"house_{key}", period="hour", colour=C_HOUSE, unit=unit, value_key=value_key, series_type="line", precision=decimals),
            c.stat_series("Hall", f"hall_{key}", period="hour", colour=C_HALL, unit=unit, value_key=value_key, series_type="line", precision=decimals),
        ],
        span=span, end="hour", default_type="line", y_title=unit.strip(), decimals=decimals, update_interval="10min",
    ))


def view_clamps(c: Cards) -> dict[str, Any] | None:
    agreement = c.jinja(
        U + """{% set f = states('$f') %}{% if f | lower not in unknown %}\
<small>The hall circuit is inside the whole house, so the hall clamp can never truly read more. \
Comparing readings taken within 2 s of each other, it read above the house {{ state_attr('$f', 'hall_over_house_pct') or 0 }}% of the time. \
{% set d = state_attr('$f', 'delta_deg') or 0 %}{% if d > 0 %}That is a CT phase error of about {{ d }}°, which inflates low power-factor loads most: \
hall readings are scaled by ×{{ state_attr('$f', 'factor_at_pf_0_6') }} at PF 0.6 and ×{{ state_attr('$f', 'factor_at_pf_0_8') }} at PF 0.8 (×{{ f }} right now).\
{% else %}No correction is needed yet ({{ state_attr('$f', 'aligned_samples') }} aligned readings).{% endif %}</small>{% endif %}""",
        f="hall_correction",
    )
    return view(
        "Clamps",
        "clamps",
        [
            _clamp(c, "house", "Whole house · 120 A", "light-blue"),
            _clamp(c, "hall", "Hall · 50 A", "teal"),
            section(c, "Last 3 hours, minute by minute", "mdi:timer-outline", [full(c.chart(
                "Average power each minute", "From the clamps' second-by-second readings, stored for good.",
                [c.minute_series("Whole house", "house_w", colour=C_HOUSE, series_type="area"),
                 c.minute_series("Hall", "hall_w", colour=C_HALL),
                 c.minute_series("Rest of house", "rest_w", colour=C_REST)],
                span="3h", end="minute", default_type="line", label_format="HH:mm", y_title="W", decimals=0,
                update_interval="1min",
            ))], span=2),
            section(c, "Power factor", "mdi:angle-acute", [_pair(c, "Hourly mean", "1.0 is purely resistive; pumps and ballasts pull it down.", "power_factor_live", "mean", "", 3)], span=2),
            section(c, "Voltage", "mdi:sine-wave", [_pair(c, "Hourly mean", "Both clamps sense the same supply.", "voltage", "mean", " V", 1)], span=2),
            section(c, "Current", "mdi:current-ac", [_pair(c, "Hourly peak", "Highest current each hour.", "current", "max", " A", 1)], span=2),
            section(c, "Clamp agreement", "mdi:scale-balance", [
                tile(c, "hall_correction", "Hall correction", icon="mdi:scale-balance"),
                tile(c, "hall_above_house_today", "Hall above house today", icon="mdi:alert-outline"),
                tile(c, "house_power_factor", "House PF (7 d)"),
                tile(c, "hall_power_factor", "Hall PF (7 d)"),
                note(agreement),
            ]),
            section(c, "Apparent power", "mdi:flash-outline", [_pair(c, "Hourly mean", "Volts × amps, before power factor.", "apparent_power", "mean", " VA", 0)]),
        ],
        badges=[
            badge(c, "house_power", "House", color="light-blue"),
            badge(c, "hall_power", "Hall", icon="mdi:fishbowl-outline", color="teal"),
            badge(c, "house_power_factor_live", "House PF"),
            badge(c, "hall_power_factor_live", "Hall PF"),
            badge(c, "house_voltage", "Voltage", icon="mdi:sine-wave"),
            badge(c, "hall_correction", "Hall corr.", icon="mdi:scale-balance"),
        ],
    )


# -------------------------------------------------------------------- explore


def view_explore(c: Cards) -> dict[str, Any] | None:
    energy = ["change"]

    def graph(title: str, series, stat_types, chart_type: str) -> dict[str, Any] | None:
        return section(c, title, "mdi:chart-box-outline", [full(c.stat_graph(title, series, stat_types=stat_types, chart_type=chart_type))], span=2)

    picker = c.date_picker()
    picker["grid_options"] = {"columns": "full"}
    return view(
        "Explore",
        "explore",
        [
            {"type": "grid", "column_span": MAX_COLUMNS, "cards": [
                {"type": "heading", "heading": "Pick a day, week, month, year or any range", "icon": "mdi:calendar-range"},
                picker,
                note(_data_note(c)),
                note(_weather_note(c)),
                note(c.jinja(
                    U + """{% set s = state_attr('$h', 'collection') %}{% if s and s.first_hour %}<small>The clamp measures \
from {{ (s.first_hour | as_datetime | as_local).strftime('%-d %b %Y %H:%M') }}{% if s.esb_history_from %}; before that, \
ESB's meter data from {{ (s.esb_history_from | as_datetime | as_local).strftime('%-d %b %Y') }} (no hall / rest split)\
{% endif %}; weather from Open-Meteo for longer. Each hour is added a few minutes after it ends, so today is always in \
progress; "No statistics found" on a range before that means there was nothing to measure then.</small>{% endif %}""",
                    h="stored_hours",
                )),
            ]},
            graph("Energy by rate", [(S("house_energy_day"), "Day rate"), (S("house_energy_night"), "Night rate")], energy, "bar-stack"),
            graph("Hall vs rest of house", [(S("hall_energy"), "Hall"), (S("rest_energy"), "Rest of house")], energy, "bar-stack"),
            graph("Cost incl. VAT", [(S("cost_day"), "Day units"), (S("cost_night"), "Night units"), (S("cost_fixed"), "Standing + PSO")], energy, "bar-stack"),
            graph("Cost: hall vs rest", [(S("hall_cost"), "Hall"), (S("rest_cost"), "Rest of house")], energy, "bar-stack"),
            graph("Power", [("house_power", "Whole house"), ("hall_power", "Hall"), (S("rest_power"), "Rest of house")], ["mean", "max"], "line"),
            graph("Power factor", [("house_power_factor_live", "Whole house"), ("hall_power_factor_live", "Hall")], ["mean", "min"], "line"),
            graph("Voltage", [("house_voltage", "Whole house"), ("hall_voltage", "Hall")], ["min", "mean", "max"], "line"),
            graph("Current", [("house_current", "Whole house"), ("hall_current", "Hall")], ["mean", "max"], "line"),
            graph("Apparent power", [("house_apparent_power", "Whole house"), ("hall_apparent_power", "Hall")], ["mean"], "line"),
            graph("Frequency", [("house_frequency", "Whole house"), ("hall_frequency", "Hall")], ["mean"], "line"),
            graph("Meter registers (estimated)", [(S("register_day"), "Day"), (S("register_night"), "Night")], ["max"], "line"),
            graph("Clamp vs ESB meter", [("house_energy", "Shelly whole house"), (S("esb_energy"), "ESB metered")], energy, "bar"),
            graph("Temperature", [(S("outdoor_temperature"), "Outside"), (S("apparent_temperature"), "Feels like")], ["min", "mean", "max"], "line"),
            graph("Heating degree days", [(S("heating_degree_days"), "Thermometer"), (S("heating_degree_days_apparent"), "Feels like")], energy, "bar"),
        ],
    )


# ---------------------------------------------------------------------- usage


def view_usage(c: Cards) -> dict[str, Any] | None:
    def day_night(period: str):
        return [
            c.stat_series("Day rate", S("house_energy_day"), period=period, colour=C_DAY),
            c.stat_series("Night rate", S("house_energy_night"), period=period, colour=C_NIGHT),
        ]

    def chart_section(title, card, span=2):
        return section(c, title, "mdi:chart-bar", [full(card)], span=span)

    return view(
        "Usage",
        "usage",
        [
            chart_section("Last 30 days", c.chart(
                "kWh per day", "Day and night units, with the next week's forecast in purple.",
                [*day_night("day"), c.attr_series(
                    "Forecast", "forecast_30_days_cost",
                    "const rows = (entity.attributes.per_day || []).slice(0, 7);"
                    " return rows.map(r => [new Date(r.date + 'T12:00:00').getTime(), r.kwh]);",
                    colour=C_FORECAST, opacity=0.55)],
                span="37d", stacked=True, offset="+7d", y_title="kWh")),
            chart_section("Last 26 weeks", c.chart(
                "kWh per week", "Monday to Sunday.", day_night("week"),
                span="26w", stacked=True, end="isoWeek", y_title="kWh", decimals=0)),
            chart_section("Last 24 months", c.chart(
                "kWh per month", "Calendar months.", day_night("month"),
                span="24month", stacked=True, end="month", label_format="MMM yy", y_title="kWh", decimals=0)),
            chart_section("Year by year", c.chart(
                "kWh per year", "The current year is to date.",
                [c.yearly_series("Total", [S("house_energy_day"), S("house_energy_night")], colour=C_HOUSE)],
                span="5y", end="year", label_format="yyyy", y_title="kWh", decimals=0)),
            chart_section("A typical day", c.chart(
                "Average kW by hour (4 weeks)", "The hall is the flat floor; the rest moves.",
                [c.attr_series("Hall", "usage_profile", _profile("hall_kw"), colour=C_HALL, unit=" kW", series_type="area"),
                 c.attr_series("Rest of house", "usage_profile", _profile("rest_kw"), colour=C_REST, unit=" kW", series_type="area")],
                span="1d", stacked=True, label_format="HH:mm", y_title="kW", decimals=2, update_interval="1h")),
            chart_section("A typical week", c.chart(
                "Average kWh by weekday (12 weeks)", "",
                [c.attr_series(
                    "Average", "usage_profile",
                    "const p = entity.attributes.weekday_kwh || {};"
                    " const base = new Date(); base.setHours(12, 0, 0, 0);"
                    " const monday = new Date(base.getTime() - ((base.getDay() + 6) % 7) * 86400000);"
                    " return Object.keys(p).map(w => [monday.getTime() + Number(w) * 86400000, p[w]]);",
                    colour=C_HOUSE)],
                span="7d", end="isoWeek", label_format="ddd", y_title="kWh", update_interval="1h")),
        ],
        badges=[
            badge(c, "yesterday_energy", "Yesterday"),
            badge(c, "week_energy", "This week"),
            badge(c, "month_energy", "This month"),
            badge(c, "year_energy", "This year"),
            badge(c, "avg_daily_energy", "Daily avg"),
            badge(c, "night_share", "Night share", icon="mdi:weather-night"),
            badge(c, "baseline_power", "Always on", icon="mdi:power-plug-outline"),
        ],
    )


# ----------------------------------------------------------------------- hall


def view_hall(c: Cards) -> dict[str, Any] | None:
    return view(
        "Hall 24/7",
        "hall",
        [
            section(c, "The always-on circuit", "mdi:fishbowl-outline", [
                tile(c, "hall_power", "Now", color="teal", graph=24),
                tile(c, "hall_average_power", "Average (7 d)", color="teal"),
                tile(c, "hall_share", "Share of usage", color="teal"),
                tile(c, "today_hall_energy", "Today", color="teal"),
                tile(c, "today_hall_cost", "Today", color="amber"),
                tile(c, "hall_daily_cost", "Per day", color="amber"),
                tile(c, "hall_yearly_cost", "Per year", color="amber"),
            ]),
            section(c, "Hall vs the rest, 48 hours", "mdi:chart-line", [full(c.chart(
                "Hourly average", "Teal is the hall, blue the rest of the house.",
                [c.stat_series("Hall", "hall_power", period="hour", colour=C_HALL, unit=" W", value_key="mean", series_type="area", precision=0),
                 c.stat_series("Rest of house", S("rest_power"), period="hour", colour=C_REST, unit=" W", value_key="mean", series_type="line", precision=0)],
                span="48h", end="hour", default_type="line", y_title="W", decimals=0, update_interval="10min"))], span=2),
            section(c, "Weather and the hall", "mdi:thermometer", [
                tile(c, "hall_heating_sensitivity", "Heaters per degree day", icon="mdi:snowflake-thermometer", color="teal"),
                tile(c, "hall_power_factor", "Power factor (7 d)", color="teal"),
                tile(c, "hall_correction", "Clamp correction", icon="mdi:scale-balance"),
                tile(c, "hall_above_house_today", "Read above house", icon="mdi:alert-outline"),
            ]),
            section(c, "Last 60 days", "mdi:chart-bar", [full(c.chart(
                "kWh per day", "Teal hall, blue rest of house.",
                [c.stat_series("Hall", S("hall_energy"), period="day", colour=C_HALL),
                 c.stat_series("Rest of house", S("rest_energy"), period="day", colour=C_REST)],
                span="60d", stacked=True, y_title="kWh"))], span=2),
            section(c, "By month", "mdi:calendar-month", [full(c.chart(
                "kWh and cost", "Units cost only, before standing charge.",
                [c.stat_series("kWh", S("hall_energy"), period="month", colour=C_HALL),
                 c.stat_series("Cost", S("hall_cost"), period="month", colour=C_DAY, unit=" €", series_type="line", yaxis_id="eur")],
                span="12month", end="month", label_format="MMM yy",
                yaxis=[{"id": "kwh", "decimals": 0, "apex_config": {"title": {"text": "kWh"}}},
                       {"id": "eur", "opposite": True, "decimals": 0, "apex_config": {"title": {"text": "€"}}}]))], span=2),
        ],
        badges=[
            badge(c, "hall_power", "Now", icon="mdi:fishbowl-outline", color="teal"),
            badge(c, "hall_average_power", "7-day avg", color="teal"),
            badge(c, "hall_share", "Share", color="teal"),
            badge(c, "hall_yearly_cost", "Per year", color="amber"),
        ],
    )


# -------------------------------------------------------------------- weather


def view_weather(c: Cards) -> dict[str, Any] | None:
    fit = c.jinja(
        U + """{% set s = states('$s') %}{% if s | lower not in unknown %}\
<small>Fitted on this house: about **{{ s }} kWh per heating degree day** on top of **{{ state_attr('$s', 'baseline_kwh') }} kWh/day** \
that does not depend on the weather (r² {{ state_attr('$s', 'r2') }}, {{ state_attr('$s', 'basis') }}). \
{{ state_attr('$s', 'heating_share_pct') }}% of the last year's usage went on the cold.</small>{% else %}\
<small>The kWh-per-degree-day fit needs at least 14 complete days of electricity data (30 before it is trusted). \
Temperatures and degree days do not wait for that.</small>{% endif %}""",
        s="heating_sensitivity",
    )
    return view(
        "Weather",
        "weather",
        [
            section(c, "Outside now", "mdi:weather-partly-cloudy", [
                tile(c, "outdoor_temperature", "Temperature", color="orange", graph=24),
                tile(c, "apparent_temperature", "Feels like", color="deep-orange"),
                tile(c, "wind_speed", "Wind", icon="mdi:weather-windy"),
                tile(c, "heating_degree_days_today", "Degree days today", icon="mdi:snowflake-thermometer", color="blue"),
                tile(c, "heating_sensitivity", "kWh per degree day", icon="mdi:home-thermometer", color="blue"),
                note(_weather_note(c)),
            ]),
            section(c, "What the cold costs", "mdi:snowflake-thermometer", [
                tile(c, "heating_sensitivity", "Per degree day", icon="mdi:home-thermometer", color="blue"),
                tile(c, "weather_cost_per_degree_day", "€ per degree day", icon="mdi:currency-eur", color="amber"),
                tile(c, "forecast_weather_7_days", "Next 7 days (weather)", icon="mdi:crystal-ball", color="purple"),
                tile(c, "forecast_weather_gap", "vs seasonal model", icon="mdi:swap-vertical"),
                note(fit),
            ]),
            section(c, "Usage against temperature, 60 days", "mdi:chart-line", [full(c.chart(
                "kWh per day and outside temperature", "Bars are usage; the line is the day's mean temperature.",
                [c.sum_series("Usage", [S("house_energy_day"), S("house_energy_night")], period="day", colour=C_HOUSE),
                 c.stat_series("Temperature", S("outdoor_temperature"), period="day", colour="#f97316", unit=" °C", value_key="mean", series_type="line", precision=1, yaxis_id="temp")],
                span="60d",
                yaxis=[{"id": "kwh", "decimals": 0, "apex_config": {"title": {"text": "kWh"}}},
                       {"id": "temp", "opposite": True, "decimals": 0, "apex_config": {"title": {"text": "°C"}}}]))], span=2),
            section(c, "Next 16 days", "mdi:crystal-ball", [full(c.chart(
                "Weather-driven forecast", "kWh from the coming weather; the line is the seasonal model.",
                [c.attr_series("Weather model", "forecast_weather_7_days",
                    "const rows = entity.attributes.per_day || [];"
                    " return rows.map(r => [new Date(r.date + 'T12:00:00').getTime(), r.kwh]);",
                    colour=C_FORECAST, series_type="column"),
                 c.attr_series("Seasonal model", "forecast_30_days_cost",
                    "const rows = (entity.attributes.per_day || []).slice(0, 16);"
                    " return rows.map(r => [new Date(r.date + 'T12:00:00').getTime(), r.kwh]);",
                    colour=C_LAST_YEAR, series_type="line"),
                 c.attr_series("Feels like", "forecast_weather_7_days",
                    "const rows = entity.attributes.per_day || [];"
                    " return rows.map(r => [new Date(r.date + 'T12:00:00').getTime(), r.apparent_c]);",
                    colour="#f97316", series_type="line", unit=" °C", yaxis_id="temp", precision=1)],
                span="16d", offset="+16d", update_interval="1h",
                yaxis=[{"id": "kwh", "decimals": 0, "apex_config": {"title": {"text": "kWh"}}},
                       {"id": "temp", "opposite": True, "decimals": 0, "apex_config": {"title": {"text": "°C"}}}]))], span=2),
            section(c, "Wind and rain, 30 days", "mdi:weather-rainy", [full(c.chart(
                "Daily", "Mean wind speed and total rain.",
                [c.stat_series("Rain", S("precipitation"), period="day", colour="#38bdf8", unit=" mm", precision=1),
                 c.stat_series("Wind", S("wind_speed"), period="day", colour="#a3a3a3", unit=" km/h", value_key="mean", series_type="line", precision=0, yaxis_id="wind")],
                span="30d",
                yaxis=[{"id": "rain", "decimals": 0, "apex_config": {"title": {"text": "mm"}}},
                       {"id": "wind", "opposite": True, "decimals": 0, "apex_config": {"title": {"text": "km/h"}}}]))], span=2),
            section(c, "By month", "mdi:calendar-month", [full(c.chart(
                "Degree days and usage", "Heating demand against what the house used.",
                [c.stat_series("Degree days", S("heating_degree_days"), period="month", colour="#60a5fa", unit=" °Cd", precision=0, yaxis_id="hdd"),
                 c.sum_series("kWh", [S("house_energy_day"), S("house_energy_night")], period="month", colour=C_HOUSE, series_type="line")],
                span="12month", end="month", label_format="MMM yy",
                yaxis=[{"id": "hdd", "decimals": 0, "apex_config": {"title": {"text": "°C·days"}}},
                       {"id": "kwh", "opposite": True, "decimals": 0, "apex_config": {"title": {"text": "kWh"}}}]))], span=2),
            section(c, "", "mdi:information-outline", [note("<small>Weather data by Open-Meteo.com (CC BY 4.0).</small>")], span=MAX_COLUMNS),
        ],
        badges=[
            badge(c, "outdoor_temperature", "Outside", icon="mdi:thermometer", color="orange"),
            badge(c, "apparent_temperature", "Feels like", color="deep-orange"),
            badge(c, "heating_degree_days_today", "Degree days", color="blue"),
            badge(c, "heating_sensitivity", "kWh/°Cd", color="blue"),
        ],
    )


# --------------------------------------------------------------------- prices


def _segments_series(c: Cards) -> dict[str, Any] | None:
    series = c.attr_series(
        "Unit price", "price_now",
        "const s = entity.attributes.price_segments || []; const out = [];"
        " for (const x of s) { out.push([new Date(x.start).getTime(), x.rate * 100]);"
        "   out.push([new Date(x.end).getTime() - 1000, x.rate * 100]); }"
        " return out;",
        colour=C_DAY, unit=" c/kWh", series_type="area", precision=2)
    if series:
        series["curve"] = "stepline"
    return series


def view_prices(c: Cards) -> dict[str, Any] | None:
    tariff = c.jinja(
        U + """| incl. VAT | per unit | |
|:--|--:|:--|
| Day rate | {{ '%.2f' | format((state_attr('$r', 'day_rate') or 0) * 100) }} c/kWh | {{ state_attr('$band', 'plan') }} |
| Night rate | {{ '%.2f' | format((state_attr('$r', 'night_rate') or 0) * 100) }} c/kWh | {{ state_attr('$band', 'night_window_tonight') }} |
| Paid per unit, last 30 days | {{ '%.2f' | format(states('$avg') | float(0) * 100) }} c/kWh | units only |
| All-in, last 30 days | {{ '%.2f' | format((state_attr('$avg', 'all_in_incl_standing_and_pso') or 0) * 100) }} c/kWh | incl. standing charge + PSO |

<small>{{ state_attr('$band', 'supplier') }}. Standing charge and PSO levy accrue per day (annual ÷ 365). The meter keeps GMT, \
so night is 23:00–08:00 in winter and midnight–09:00 in summer time.</small>""",
        r="unit_rate", avg="average_unit_price", band="tariff_band",
    )
    return view(
        "Prices",
        "prices",
        [
            section(c, "Price now", "mdi:cash-fast", [
                tile(c, "price_now", "Price now", icon="mdi:cash-fast", color="amber", graph=48),
                tile(c, "tariff_band", "Rate", color="indigo"),
                tile(c, "next_band_change", "Changes", icon="mdi:clock-start"),
                tile(c, "spend_rate", "Spending now", color="amber"),
                tile(c, "unit_rate", "Unit rate", icon="mdi:currency-eur"),
            ]),
            section(c, "Today and tomorrow", "mdi:chart-timeline-variant", [full(c.chart(
                "Unit price by hour", "Day rate amber, night rate the dip. Times are local.",
                [_segments_series(c)],
                span="2d", end="day", offset="+1d", label_format="ddd HH:mm", y_title="c/kWh", decimals=1, update_interval="15min",
            ))], span=2),
            section(c, "Tariff", "mdi:tag-outline", [
                {"type": "markdown", "content": tariff, "grid_options": {"columns": "full"}},
                {"type": "markdown", "content": _periods_note(c), "grid_options": {"columns": "full"}},
            ]),
            section(c, "What a unit really cost", "mdi:chart-line", [full(c.chart(
                "€ per kWh, month by month", "Units only, and all-in with standing charge and PSO.",
                [c.ratio_series("Units only", [S("cost_day"), S("cost_night")], [S("house_energy_day"), S("house_energy_night")],
                                period="month", colour=C_DAY, unit=" €/kWh", precision=4, series_type="line"),
                 c.ratio_series("All-in", [S("cost_total")], [S("house_energy_day"), S("house_energy_night")],
                                period="month", colour=C_FIXED, unit=" €/kWh", precision=4, series_type="line")],
                span="12month", end="month", label_format="MMM yy", decimals=3,
            ))], span=2),
            section(c, "Day vs night, 30 days", "mdi:chart-bar", [full(c.chart(
                "€ per day by rate", "How much of each day's bill was day units and how much night.",
                [c.stat_series("Day units", S("cost_day"), period="day", colour=C_DAY, unit=" €"),
                 c.stat_series("Night units", S("cost_night"), period="day", colour=C_NIGHT, unit=" €")],
                span="30d", stacked=True, y_title="€", decimals=2,
            ))], span=2),
        ],
        badges=[
            badge(c, "price_now", "Now", icon="mdi:cash-fast", color="amber"),
            badge(c, "tariff_band", "Rate", color="indigo"),
            badge(c, "next_band_change", "Changes", icon="mdi:clock-start"),
            badge(c, "average_unit_price", "Paid (30 d)"),
            badge(c, "night_share", "Night share", icon="mdi:weather-night"),
        ],
    )


def view_system(c: Cards) -> dict[str, Any] | None:
    return view(
        "System",
        "system",
        [
            section(c, "Data sources", "mdi:database-sync", [
                tile(c, "esb_status", "ESB", icon="mdi:transmission-tower"),
                tile(c, "esb_lag", "ESB lag"),
                tile(c, "sse_status", "SSE", icon="mdi:send-check"),
                tile(c, "open_meteo_requests", "Open-Meteo requests"),
                tile(c, "stored_hours", "Hours stored", icon="mdi:database"),
                tile(c, "clock_offset", "Clock vs NTP", icon="mdi:clock-check-outline"),
            ]),
            section(c, "Accuracy", "mdi:bullseye-arrow", [
                tile(c, "meter_factor_day", "Meter factor day", icon="mdi:tune-variant"),
                tile(c, "meter_factor_night", "Meter factor night", icon="mdi:tune-variant"),
                tile(c, "esb_calibration", "ESB ÷ clamp"),
                tile(c, "shelly_vs_esb", "Clamp vs meter"),
                tile(c, "forecast_accuracy", "Forecast accuracy"),
                tile(c, "hall_correction", "Hall correction"),
            ]),
            section(c, "Live accounting", "mdi:pulse", [
                tile(c, "hall_above_house_today", "Hall above house today"),
                tile(c, "usage_profile", "Always-on (profile)"),
                tile(c, "check_sse", "Check SSE (read only)", icon="mdi:account-check"),
                tile(c, "refresh_esb", "Fetch ESB now", icon="mdi:transmission-tower-import"),
            ]),
            section(c, "How it works", "mdi:information-outline", [note(
                "<small>Live figures follow the Shelly's own pushes (about once a second). Every hour is stored "
                "for good and priced at the meter: clamp kWh × day/night factors learned from real meter readings "
                "and ESB's data. The hall clamp is corrected for its power factor so it never exceeds the whole "
                "house. Forecasts are a weighted ensemble whose weights are re-tuned every night from how each "
                "model actually did. Nothing is ever sent to SSE without a button press.</small>")]),
        ],
        badges=[
            badge(c, "esb_status", "ESB", icon="mdi:transmission-tower"),
            badge(c, "sse_status", "SSE", icon="mdi:send-check"),
            badge(c, "clock_offset", "Clock", icon="mdi:clock-check-outline"),
            badge(c, "stored_hours", "Hours", icon="mdi:database"),
        ],
    )


# ---------------------------------------------------------------- costs & bill


def view_costs(c: Cards) -> dict[str, Any] | None:
    bill = c.jinja(
        U + """{% set b = states('$b') %}{% if b | lower not in unknown %}\
| | kWh | € |
|:--|--:|--:|
| Day units | {{ state_attr('$b', 'day_kwh') }} | {{ '%.2f' | format(state_attr('$b', 'day_cost') | float(0)) }} |
| Night units | {{ state_attr('$b', 'night_kwh') }} | {{ '%.2f' | format(state_attr('$b', 'night_cost') | float(0)) }} |
| Standing charge ({{ state_attr('$b', 'days') }} days) | | {{ '%.2f' | format(state_attr('$b', 'standing') | float(0)) }} |
| PSO levy | | {{ '%.2f' | format(state_attr('$b', 'pso') | float(0)) }} |
| **Total incl. VAT** | **{{ state_attr('$b', 'kwh') }}** | **€{{ '%.2f' | format(b | float) }}** |
| of which VAT ({{ state_attr('$b', 'vat_pct') }}%) | | {{ '%.2f' | format(state_attr('$b', 'vat') | float(0)) }} |

<small>{{ state_attr('$b', 'period_label') }} · €{{ '%.2f' | format(state_attr('$b', 'so_far') | float(0)) }} so far, likely €{{ '%.0f' | format(state_attr('$b', 'low') | float(0)) }}–{{ '%.0f' | format(state_attr('$b', 'high') | float(0)) }} · {{ state_attr('$b', 'source') }}\
{% if state_attr('$b', 'contract_end') %} · contract ends {{ state_attr('$b', 'contract_end') }}{% endif %}</small>
{% else %}<small>Set a billing date in the options, or connect SSE (live) to read it from the account.</small>{% endif %}""",
        b="bill_estimate",
    )
    prev_bill = c.jinja(
        U + """{% set lb = states('$lb') %}{% if lb | lower not in unknown %}\
{% if state_attr('$lb', 'billed_eur') is not none %}\
| | kWh | € |
|:--|--:|--:|
| **Billed incl. VAT** | **{{ state_attr('$lb', 'kwh') | int }}** | **€{{ '%.2f' | format(lb | float) }}** |
| Day / night units billed | {{ state_attr('$lb', 'day_kwh') | int }} / {{ state_attr('$lb', 'night_kwh') | int }} | |
| of which VAT ({{ state_attr('$lb', 'vat_pct') }}%) | | {{ '%.2f' | format(state_attr('$lb', 'vat') | float(0)) }} |
{% if state_attr('$lb', 'metered_total') is not none %}| Metered over full days ({{ state_attr('$lb', 'days') }} d) | {{ ((state_attr('$lb', 'metered_day_kwh') or 0) + (state_attr('$lb', 'metered_night_kwh') or 0)) | int }} | {{ '%.2f' | format(state_attr('$lb', 'metered_total') | float(0)) }} |
{% endif %}{% if state_attr('$lb', 'previous_balance_eur') is not none %}| Balance before bill ({{ (state_attr('$lb', 'previous_balance_status') or '') | lower }}) | | {{ '%.2f' | format(state_attr('$lb', 'previous_balance_eur') | float(0)) }} |
{% endif %}{% if state_attr('$lb', 'balance_after_eur') is not none %}| Account after bill ({{ (state_attr('$lb', 'balance_after_status') or '') | lower }}) | | {{ '%.2f' | format(state_attr('$lb', 'balance_after_eur') | float(0)) }} |
{% endif %}{% else %}\
| | kWh | € |
|:--|--:|--:|
| Day units | {{ state_attr('$lb', 'day_kwh') | int }} | {{ '%.2f' | format(state_attr('$lb', 'day_cost') | float(0)) }} |
| Night units | {{ state_attr('$lb', 'night_kwh') | int }} | {{ '%.2f' | format(state_attr('$lb', 'night_cost') | float(0)) }} |
| Standing charge ({{ state_attr('$lb', 'days') }} days) | | {{ '%.2f' | format(state_attr('$lb', 'standing') | float(0)) }} |
| PSO levy | | {{ '%.2f' | format(state_attr('$lb', 'pso') | float(0)) }} |
| **Total incl. VAT** | **{{ state_attr('$lb', 'kwh') | int }}** | **€{{ '%.2f' | format(lb | float) }}** |
{% endif %}
<small>{% if state_attr('$lb', 'bill_date') %}Issued {{ state_attr('$lb', 'bill_date') }} · {% endif %}{{ state_attr('$lb', 'period_label') }} · {{ state_attr('$lb', 'source') }}</small>
{% endif %}""",
        lb="last_bill",
    )
    tariff = c.jinja(
        U + """| incl. VAT | |
|:--|--:|
| Day rate | {{ '%.2f' | format((state_attr('$r', 'day_rate') or 0) * 100) }} c/kWh |
| Night rate | {{ '%.2f' | format((state_attr('$r', 'night_rate') or 0) * 100) }} c/kWh |
| Paid per unit (30 d) | {{ '%.2f' | format(states('$avg') | float(0) * 100) }} c/kWh |
| All-in incl. standing + PSO | {{ '%.2f' | format((state_attr('$avg', 'all_in_incl_standing_and_pso') or 0) * 100) }} c/kWh |

<small>{{ state_attr('$band', 'supplier') }} · {{ state_attr('$band', 'plan') }} · night {{ state_attr('$band', 'night_window_tonight') }}</small>""",
        r="unit_rate", avg="average_unit_price", band="tariff_band",
    )
    return view(
        "Costs & bill",
        "costs",
        [
            section(c, "Next bill (estimate)", "mdi:receipt-text-outline", [
                {"type": "markdown", "content": bill, "grid_options": {"columns": "full"}},
                when_known(c, "last_bill", {"type": "heading", "heading": "Last bill", "heading_style": "title", "icon": "mdi:receipt-text-check-outline", "grid_options": {"columns": "full"}}),
                when_known(c, "last_bill", {"type": "markdown", "content": prev_bill, "grid_options": {"columns": "full"}}),
            ], span=2),
            section(c, "Spent", "mdi:currency-eur", [
                tile(c, "today_cost", "Today", color="amber"),
                tile(c, "yesterday_cost", "Yesterday", color="amber"),
                tile(c, "week_cost", "This week", color="amber"),
                tile(c, "month_cost", "This month", color="amber"),
                tile(c, "year_cost", "This year", color="amber"),
                tile(c, "avg_daily_cost", "Per day (30 d)", color="amber"),
            ]),
            section(c, "Tariff", "mdi:tag-outline", [
                {"type": "markdown", "content": tariff, "grid_options": {"columns": "full"}},
                {"type": "markdown", "content": _periods_note(c), "grid_options": {"columns": "full"}},
            ]),
            section(c, "Last 30 days", "mdi:chart-bar", [full(c.chart(
                "€ per day", "Day units, night units, standing charge + PSO. Incl. VAT.",
                [c.stat_series("Day units", S("cost_day"), period="day", colour=C_DAY, unit=" €"),
                 c.stat_series("Night units", S("cost_night"), period="day", colour=C_NIGHT, unit=" €"),
                 c.stat_series("Standing + PSO", S("cost_fixed"), period="day", colour=C_FIXED, unit=" €")],
                span="30d", stacked=True, y_title="€", decimals=2))], span=2),
            section(c, "Last 12 months", "mdi:chart-bar", [full(c.chart(
                "€ per month", "Incl. VAT.",
                [c.stat_series("Day units", S("cost_day"), period="month", colour=C_DAY, unit=" €"),
                 c.stat_series("Night units", S("cost_night"), period="month", colour=C_NIGHT, unit=" €"),
                 c.stat_series("Standing + PSO", S("cost_fixed"), period="month", colour=C_FIXED, unit=" €")],
                span="12month", end="month", stacked=True, label_format="MMM yy", y_title="€", decimals=0))], span=2),
        ],
        badges=[
            badge(c, "bill_estimate", "Next bill", icon="mdi:receipt-text-outline", color="purple"),
            badge(c, "last_bill", "Last bill", icon="mdi:receipt-text-check-outline"),
            badge(c, "today_cost", "Today", color="amber"),
            badge(c, "month_cost", "Month", color="amber"),
            badge(c, "year_cost", "Year", color="amber"),
            badge(c, "average_unit_price", "€/kWh"),
        ],
    )


# ------------------------------------------------------------------- forecast


def view_forecast(c: Cards) -> dict[str, Any] | None:
    seasons = c.jinja(
        U + """{% set m = state_attr('$y', 'seasons') %}{% if m %}
| Season | kWh | likely range | € |
|:--|--:|--:|--:|
{% for s in m %}| {{ s.label }} | {{ s.kwh | round(0) | int }} | {{ s.low_kwh | round(0) | int }}–{{ s.high_kwh | round(0) | int }} | {{ '%.0f' | format(s.cost) }} |
{% endfor %}| **Next 12 months** | **{{ state_attr('$y', 'kwh') | int }}** | | **{{ states('$y') | float | round(0) | int }}** |
{% endif %}""",
        y="forecast_12_months_cost",
    )
    engine = c.jinja(
        U + """{% set w = state_attr('$a', 'weights') or {} %}{% if w %}
| Model | Weight | Recent error |
|:--|--:|--:|
{% for name, v in w.items() %}| {{ name }} | {{ '%.0f' | format(v.weight * 100) }}% | {{ v.error_pct if v.error_pct is not none else '–' }}% |
{% endfor %}
<small>Each model's predictions are logged nightly and scored when the day is metered; weights follow the recent errors \
(half-life {{ state_attr('$a', 'half_life_days') }} days). Bias correction ×{{ state_attr('$a', 'bias') }}.</small>
{% else %}<small>The engine starts weighting its models once it has scored a few days.</small>{% endif %}""",
        a="forecast_accuracy",
    )
    drivers = c.jinja(
        U + """{% set w = state_attr('$d', 'weights') or [] %}{% set t = state_attr('$d', 'today') or {} %}{% if w %}
| What it is | Weight | Today |
|:--|--:|--:|
{% for x in w if x.feature not in ['sin1','cos1','sin2','cos2','mon','tue','wed','thu','fri','sat'] %}\
| {{ x.label }} | {{ '%+.2f' | format(x.weight) }} {{ x.unit }} | {{ '%+.1f' | format(t[x.feature]) if x.feature in t else '' }} |
{% endfor %}| Weekday effect | | {{ '%+.1f' | format(t.weekday) if 'weekday' in t else '' }} |
| Time of year | | {{ '%+.1f' | format(t.time_of_year) if 'time_of_year' in t else '' }} |

<small>Refitted every night from {{ state_attr('$d', 'days') }} days (recent days count most); fits {{ states('$d') }}% of days. \
Forecast weather: {% for s, v in (state_attr('$d', 'forecast_sources') or {}).items() %}{{ s }} {{ '%.0f' | format(v * 100) }}%{% if not loop.last %}, {% endif %}{% endfor %}.</small>
{% else %}<small>Learning starts after four weeks of data with weather.</small>{% endif %}""",
        d="usage_drivers",
    )

    def weight_line(feature: str, name: str, colour: str) -> dict[str, Any] | None:
        return c.attr_series(
            name, "usage_drivers",
            f"const h = (entity.attributes.history || {{}})['{feature}'] || [];"
            " return h.map(([d, v]) => [new Date(d + 'T12:00:00').getTime(), v]);",
            colour=colour, unit=" kWh", series_type="line", precision=2)

    return view(
        "Forecast",
        "forecast",
        [
            section(c, "What drives usage (learned)", "mdi:tune-vertical-variant", [
                {"type": "markdown", "content": drivers, "grid_options": {"columns": "full"}}], span=2),
            section(c, "How the weights have moved", "mdi:chart-timeline-variant-shimmer", [full(c.chart(
                "Learned weights, 6 months", "Each line is one weight, refitted nightly.",
                [weight_line("cold", "Per degree day", "#60a5fa"),
                 weight_line("chill", "Per wind/damp degree day", "#a78bfa"),
                 weight_line("sun", "Per 100 W/m² sun", "#facc15"),
                 weight_line("wind", "Per 10 km/h wind", "#94a3b8")],
                span="180d", default_type="line", y_title="kWh", decimals=2, update_interval="1h",
            ))], span=2),
            section(c, "Ahead", "mdi:crystal-ball", [
                tile(c, "forecast_today_cost", "Today", color="purple"),
                tile(c, "forecast_tomorrow_cost", "Tomorrow", color="purple"),
                tile(c, "forecast_month_cost", "This month", color="purple"),
                tile(c, "forecast_30_days_cost", "Next 30 days", color="purple"),
                tile(c, "forecast_12_months_cost", "Next 12 months", color="purple"),
                tile(c, "bill_estimate", "Next bill", icon="mdi:receipt-text-outline", color="purple"),
                tile(c, "forecast_weather_7_days", "Next 7 days (weather)", color="purple"),
                tile(c, "forecast_accuracy", "Accuracy", icon="mdi:bullseye-arrow"),
            ]),
            section(c, "How the engine is weighting its models", "mdi:tune-vertical", [{"type": "markdown", "content": engine, "grid_options": {"columns": "full"}}]),
            section(c, "Next 12 months", "mdi:calendar-month", [full(c.chart(
                "Forecast by month", "Bars are kWh (grey: same month last year); the line is cost.",
                [c.attr_series("Forecast kWh", "forecast_12_months_cost",
                    "const m = entity.attributes.months || [];"
                    " return m.map(r => [new Date(r.start + 'T12:00:00').getTime(), r.kwh]);",
                    colour=C_FORECAST, series_type="column", yaxis_id="kwh", precision=0),
                 c.attr_series("Last year", "forecast_12_months_cost",
                    "const m = entity.attributes.months || [];"
                    " return m.map(r => [new Date(r.start + 'T12:00:00').getTime(), r.last_year_kwh]);",
                    colour=C_LAST_YEAR, series_type="column", yaxis_id="kwh", precision=0, opacity=0.5),
                 c.attr_series("Cost", "forecast_12_months_cost",
                    "const m = entity.attributes.months || [];"
                    " return m.map(r => [new Date(r.start + 'T12:00:00').getTime(), r.cost]);",
                    colour=C_DAY, series_type="line", unit=" €", yaxis_id="eur", precision=0)],
                span="12month", end="month", offset="+12month", label_format="MMM yy", update_interval="1h",
                yaxis=[{"id": "kwh", "decimals": 0, "apex_config": {"title": {"text": "kWh"}}},
                       {"id": "eur", "opposite": True, "decimals": 0, "apex_config": {"title": {"text": "€"}}}]))], span=2),
            section(c, "Seasons ahead", "mdi:weather-snowy-rainy", [{"type": "markdown", "content": seasons, "grid_options": {"columns": "full"}}], span=2),
            section(c, "Next 30 days", "mdi:chart-bar", [full(c.chart(
                "kWh per day", "The ensemble forecast and its likely high.",
                [c.attr_series("Forecast", "forecast_30_days_cost",
                    "const rows = entity.attributes.per_day || [];"
                    " return rows.map(r => [new Date(r.date + 'T12:00:00').getTime(), r.kwh]);",
                    colour=C_FORECAST, series_type="column"),
                 c.attr_series("Likely high", "forecast_30_days_cost",
                    "const rows = entity.attributes.per_day || [];"
                    " return rows.map(r => [new Date(r.date + 'T12:00:00').getTime(), r.high]);",
                    colour=C_LAST_YEAR, series_type="line")],
                span="30d", offset="+30d", y_title="kWh", update_interval="1h"))], span=2),
            section(c, "Forecast vs what happened", "mdi:bullseye-arrow", [full(c.chart(
                "Day-ahead forecast and actual, 30 days", "How well yesterday's forecast matched each day.",
                [c.attr_series("Forecast", "forecast_accuracy",
                    "const rows = entity.attributes.recent || [];"
                    " return rows.map(r => [new Date(r.date + 'T12:00:00').getTime(), r.forecast]);",
                    colour=C_FORECAST, series_type="column", opacity=0.6),
                 c.attr_series("Actual", "forecast_accuracy",
                    "const rows = entity.attributes.recent || [];"
                    " return rows.map(r => [new Date(r.date + 'T12:00:00').getTime(), r.actual]);",
                    colour=C_HOUSE, series_type="line")],
                span="30d", y_title="kWh", update_interval="1h"))], span=2),
        ],
        badges=[
            badge(c, "forecast_today_cost", "Today", color="purple"),
            badge(c, "forecast_month_cost", "Month", color="purple"),
            badge(c, "forecast_12_months_cost", "12 months", color="purple"),
            badge(c, "forecast_accuracy", "Accuracy", icon="mdi:bullseye-arrow"),
        ],
    )


# ---------------------------------------------------------------------- meter


def view_meter(c: Cards) -> dict[str, Any] | None:
    lessons = c.jinja(
        """{% set l = state_attr('$f', 'lessons') or [] %}{% if l %}
| Up to | Days | Day meter / clamp | Day error before | Night meter / clamp | Night error before | |
|:--|--:|--:|--:|--:|--:|:--|
{% for x in l[-8:] %}| {{ (x.to | as_datetime | as_local).strftime('%-d %b %y') }} | {{ x.days }} | {{ x.day_meter_kwh }} / {{ x.day_clamp_kwh }} | {{ x.day_error_kwh if x.day_error_kwh is not none else '' }} | {{ x.night_meter_kwh }} / {{ x.night_clamp_kwh }} | {{ x.night_error_kwh if x.night_error_kwh is not none else '' }} | {{ x.note or ('learned' if x.day_used or x.night_used else '') }} |
{% endfor %}
<small>"Error before" is how far the estimate was from the meter using only what earlier readings had taught.</small>
{% else %}<small>Every real reading — entered here, sent from here, submitted on SSE's site or app, or one of ESB's actual reads — teaches the estimate. A second reading starts the learning.</small>{% endif %}""",
        f="meter_factor_day",
    )
    compare = c.jinja(
        """{% set rows = state_attr('$cal', 'reads_compare') or [] %}{% if rows %}
| ESB read | Day actual | Day est. | Night actual | Night est. |
|:--|--:|--:|--:|--:|
{% for r in rows[-6:] %}| {{ r.read_date }} | {{ r.day_actual | int if r.day_actual is defined else '' }} | {{ r.day_estimated if r.day_estimated is defined else '' }} | {{ r.night_actual | int if r.night_actual is defined else '' }} | {{ r.night_estimated if r.night_estimated is defined else '' }} |
{% endfor %}{% else %}<small>No ESB register reads yet.</small>{% endif %}""",
        cal="esb_calibration",
    )
    sse_history = c.jinja(
        """{% set days = state_attr('$day', 'history') or [] %}{% set nights = state_attr('$night', 'history') or [] %}{% if days %}
| Read on | Day | Night | Day units | Night units | Status |
|:--|--:|--:|--:|--:|:--|
{% for r in days[:12] %}{% set n = nights | selectattr('date', 'eq', r.date) | list | first %}| {{ r.date }} | {{ r.value | int }} | {{ n.value | int if n else '' }} | {{ r.units_used | int if r.units_used is not none else '' }} | {{ n.units_used | int if n and n.units_used is not none else '' }} | {{ r.status }}{{ ' (SSE estimate)' if r.estimated else '' }} |
{% endfor %}{% else %}<small>No readings from SSE yet. With SSE in live mode they are read once a day, or now with Check SSE.</small>{% endif %}""",
        day="sse_last_reading_day",
        night="sse_last_reading_night",
    )
    return view(
        "Meter & SSE",
        "meter",
        [
            section(c, "Meter now", "mdi:counter", [
                tile(c, "meter_reading_day", "Day register", icon="mdi:white-balance-sunny", color="amber", wide=True),
                tile(c, "meter_reading_night", "Night register", icon="mdi:weather-night", color="indigo", wide=True),
                tile(c, "meter_factor_day", "Day factor", icon="mdi:tune-variant", color="amber"),
                tile(c, "meter_factor_night", "Night factor", icon="mdi:tune-variant", color="indigo"),
            ]),
            section(c, "Send to SSE", "mdi:send", [
                c.entities("Reading to send", ("reading_to_submit_day", "Day"), ("reading_to_submit_night", "Night"),
                           ("use_estimate", "Reset to the estimate")),
                c.confirm_button(
                    "submit_reading", "Send meter reading",
                    "Send the day and night reading shown above to SSE Airtricity now? This cannot be undone, and SSE accepts one reading per 24 hours.",
                    icon="mdi:send-check"),
                tile(c, "sse_status", "SSE"),
                when_known(c, "sse_next_allowed", tile(c, "sse_next_allowed", "Next allowed")),
                note("<small>Nothing is sent until you press the button and confirm. The boxes follow the estimate; type the numbers off the meter to send those instead.</small>"),
            ]),
            section(c, "SSE account", "mdi:account-cash-outline", [
                when_known(c, "sse_last_reading_day", tile(c, "sse_last_reading_day", "SSE day reading", icon="mdi:white-balance-sunny", color="amber")),
                when_known(c, "sse_last_reading_night", tile(c, "sse_last_reading_night", "SSE night reading", icon="mdi:weather-night", color="indigo")),
                when_known(c, "sse_next_bill_date", tile(c, "sse_next_bill_date", "Next bill", color="purple")),
                when_known(c, "last_bill", tile(c, "last_bill", "Last bill", icon="mdi:receipt-text-check-outline")),
                when_known(c, "sse_balance", tile(c, "sse_balance", "Balance")),
                when_known(c, "sse_contract_end", tile(c, "sse_contract_end", "Contract ends")),
                tile(c, "check_sse", "Check SSE (read only)", icon="mdi:account-check"),
                {"type": "markdown", "content": sse_history, "grid_options": {"columns": "full"}},
            ]),
            section(c, "Getting better with every reading", "mdi:school-outline", [{"type": "markdown", "content": lessons, "grid_options": {"columns": "full"}}], span=2),
            section(c, "Register history, 60 days", "mdi:chart-line", [full(c.chart(
                "End of each day", "Passes exactly through every real reading.",
                [c.stat_series("Day register", S("register_day"), period="day", colour=C_DAY, value_key="max", series_type="line", precision=0),
                 c.stat_series("Night register", S("register_night"), period="day", colour=C_NIGHT, value_key="max", series_type="line", precision=0, yaxis_id="night")],
                span="60d",
                yaxis=[{"id": "day", "decimals": 0, "apex_config": {"title": {"text": "Day kWh"}}},
                       {"id": "night", "opposite": True, "decimals": 0, "apex_config": {"title": {"text": "Night kWh"}}}]))], span=2),
            section(c, "Estimate vs ESB's actual reads", "mdi:transmission-tower", [{"type": "markdown", "content": compare, "grid_options": {"columns": "full"}}], span=2),
            {"type": "grid", "column_span": MAX_COLUMNS, "cards": Cards.clean([
                {"type": "heading", "heading": "Readings sent and attempted", "icon": "mdi:history"},
                c.data_card(dataset="readings", title="", max_height=360),
            ])} if c.has_data_card else None,
        ],
        badges=[
            badge(c, "meter_reading_day", "Day", icon="mdi:white-balance-sunny", color="amber"),
            badge(c, "meter_reading_night", "Night", icon="mdi:weather-night", color="indigo"),
            badge(c, "meter_factor_day", "Day ×", icon="mdi:tune-variant"),
            badge(c, "meter_factor_night", "Night ×", icon="mdi:tune-variant"),
            badge(c, "sse_status", "SSE", icon="mdi:send-check"),
        ],
    )


# ------------------------------------------------------------------------ ESB


def view_esb(c: Cards) -> dict[str, Any] | None:
    note_text = c.jinja(
        U + """<small>ESB Networks publishes the meter's half-hourly data two to three days late and rate-limits sign-ins, \
so it is fetched at most three times a day (choose how often under Configure → ESB Networks).</small>""",
        st="esb_status",
    )
    return view(
        "ESB",
        "esb",
        [
            section(c, "ESB Networks", "mdi:transmission-tower", [
                tile(c, "esb_status", "Status"),
                tile(c, "esb_lag", "Lag"),
                tile(c, "esb_data_through", "Data through", wide=True),
                tile(c, "esb_latest_day", "Latest metered day"),
                tile(c, "shelly_vs_esb", "Clamp vs meter (30 d)"),
                tile(c, "esb_calibration", "Meter ÷ clamp"),
                tile(c, "refresh_esb", "Fetch now", icon="mdi:transmission-tower-import"),
                note(note_text),
            ]),
            section(c, "Clamp vs meter, 30 days", "mdi:scale-balance", [full(c.chart(
                "kWh per day", "Whole-house clamp and ESB's meter; the line is the difference in %.",
                [c.sum_series("Shelly", [S("house_energy_day"), S("house_energy_night")], period="day", colour=C_HOUSE),
                 c.stat_series("ESB", S("esb_energy"), period="day", colour=C_ESB),
                 c.ratio_series("Difference", [S("house_energy_day"), S("house_energy_night")], [S("esb_energy")],
                                period="day", colour=C_VOLT, unit=" %", scale=100, minus_one=True, yaxis_id="pct")],
                span="30d",
                yaxis=[{"id": "kwh", "decimals": 0, "apex_config": {"title": {"text": "kWh"}}},
                       {"id": "pct", "opposite": True, "decimals": 1, "apex_config": {"title": {"text": "%"}}}]))], span=3),
        ],
        badges=[
            badge(c, "esb_status", "ESB", icon="mdi:transmission-tower"),
            badge(c, "esb_lag", "Lag"),
            badge(c, "shelly_vs_esb", "Clamp vs meter"),
        ],
    )


# ----------------------------------------------------------------------- data


def view_data(c: Cards) -> dict[str, Any] | None:
    header = c.jinja(
        """<small>Every hour since the clamps were installed, kept forever in **{{ state_attr('$h', 'path') }}**: \
{{ states('$h') }} hours. Energy, the day/night split, hall and rest of house, meter-equivalent kWh, cost at the price in force, \
power, power factor, voltage, current and frequency for both clamps, the estimated registers and ESB's figures. \
Choose a resolution and range, sort any column, or download CSV.</small>""",
        h="stored_hours",
    )
    if not c.has_data_card:
        return None
    return view(
        "Data",
        "data",
        [
            {"type": "grid", "column_span": MAX_COLUMNS, "cards": Cards.clean([
                {"type": "heading", "heading": "Everything stored", "icon": "mdi:database"},
                note(header),
                c.data_card(dataset="usage", period="day", title="By day / month / year"),
                c.data_card(dataset="usage", period="hour", title="Hour by hour"),
            ])},
        ],
        badges=[badge(c, "stored_hours", "Hours stored", icon="mdi:database")],
    )


def build_views(c: Cards) -> list[dict[str, Any]]:
    builders = (
        view_overview,
        view_clamps,
        view_explore,
        view_usage,
        view_hall,
        view_weather,
        view_prices,
        view_costs,
        view_forecast,
        view_meter,
        view_esb,
        view_data,
        view_system,
    )
    return [v for v in (b(c) for b in builders) if v]
