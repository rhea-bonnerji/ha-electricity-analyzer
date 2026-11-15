"""Card and chart factories.

NO NUMBER IS EVER BAKED IN. Every figure is a Jinja template Home Assistant re-renders,
a chart that queries the recorder, or the data card that queries the database, so the
generated dashboard only changes when entity ids change.

THREE KINDS OF CHART

* `stat_graph`: Home Assistant's own statistics-graph. With `date_picker=True` it
  follows the view's energy-date-selection card, so any day, week, month, year or
  custom range can be charted. Used for everything that is a long-term statistic.
* `chart`: apexcharts-card (bundled), for fixed windows, mixed series, forecasts
  and anything read from a sensor attribute.
* `data_card`: the integration's own table, for reading exact values.

Hard-won apexcharts constraints, enforced in `chart`:
- series `type` must be line, column or area (anything else makes setConfig throw
  and the card vanish without a message);
- `apex_config.responsive` is never emitted (it throws while cloning options);
- a second y-axis is declared at card level, not inside apex_config;
- `update_interval` is always set, so a chart anchored to a sensor that changes every
  second does not re-query the recorder every second.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from string import Template
from typing import Any

from ..const import APEX_CARD, COLLECTION_KEY, DATA_CARD

ALLOWED_SERIES_TYPES = frozenset({"line", "column", "area"})
MISSING = "sensor.electricity_analyzer_unavailable"

# What an empty chart says. apexcharts-card's own default is "Loading...", which an
# empty answer (no completed hour yet today, a first day, nothing published yet) then
# shows for ever. The card's spinner ring is what says "loading"; this says "empty".
NO_DATA = "No data for this period yet."

# Prepended to every chart body that asks Home Assistant for data. apexcharts-card
# locks a series for good if its data generator throws or never settles (its
# `_updating` flag is set before the call and only cleared after it), which is how a
# websocket hiccup during a restart or a reload became a chart stuck on "Loading..."
# until the page was reloaded. So: each call is bounded, retried twice, and a failure
# returns no data (logged to the browser console) instead of throwing; the chart's next
# update interval simply asks again.
#
# `mid` places a statistics bucket at its MIDDLE, as Home Assistant's own statistics
# chart does for hourly bars and apexcharts-card does for its own `statistics:` series.
# At the bucket's start, a column is centred on the hour it begins, so it appears to
# cover the half hour either side: the first night hour (00:00-01:00 in summer) was
# drawn straddling midnight and clipped at the chart's edge, and night seemed to start
# at 01:00.
WS_HELPERS = (
    "const ws = async (msg) => { for (let i = 0; i < 3; i += 1) { try {"
    " return await Promise.race([hass.callWS(msg), new Promise((_, no) => setTimeout(() => no(new Error('timed out')), 20000))]);"
    " } catch (e) { if (i === 2) { console.warn('Electricity Analyzer chart: no data', msg.type, e && (e.message || e.code || e)); return null; }"
    " await new Promise((ok) => setTimeout(ok, 2000 * (i + 1))); } } return null; };"
    " const mid = (x) => (typeof x.end === 'number' ? (x.start + x.end) / 2 : x.start);"
)

# Palette: one meaning per colour across the whole dashboard.
C_DAY = "#f59e0b"      # day rate: amber
C_NIGHT = "#6366f1"    # night rate: indigo
C_HALL = "#14b8a6"     # hall (24/7): teal
C_REST = "#3b82f6"     # rest of house: blue
C_HOUSE = "#0ea5e9"
C_FIXED = "#94a3b8"    # standing + PSO: slate
C_ESB = "#ef4444"      # ESB metered: red
C_FORECAST = "#a855f7"  # forecast: purple
C_LAST_YEAR = "#9ca3af"
C_VOLT = "#22c55e"


@dataclass
class Source:
    """Resolved entity ids: our sensors by key, the Shelly entities by role."""

    ours: dict[str, str] = field(default_factory=dict)
    shelly: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        return self.ours.get(key) or self.shelly.get(key)


class Cards:
    def __init__(self, source: Source, *, apex: bool, data_card: bool) -> None:
        self.src = source
        self.apex = apex
        self.has_data_card = data_card

    def eid(self, key: str) -> str | None:
        return self.src.get(key)

    def jinja(self, body: str, **keys: str) -> str:
        return Template(body).safe_substitute({n: (self.eid(k) or MISSING) for n, k in keys.items()})

    @staticmethod
    def clean(items: list[Any]) -> list[Any]:
        return [i for i in items if i]

    # ------------------------------------------------------------------ simple

    @staticmethod
    def heading(text: str, icon: str, *, style: str = "title") -> dict[str, Any]:
        return {"type": "heading", "heading": text, "heading_style": style, "icon": icon}

    @staticmethod
    def markdown(text: str, *, columns: int | str = 12) -> dict[str, Any]:
        return {"type": "markdown", "content": text, "grid_options": {"columns": columns}}

    def tile(self, key: str, name: str, *, icon: str | None = None, columns: int = 6) -> dict[str, Any] | None:
        entity_id = self.eid(key)
        if not entity_id:
            return None
        card: dict[str, Any] = {
            "type": "tile",
            "entity": entity_id,
            "name": name,
            "vertical": False,
            "grid_options": {"columns": columns, "rows": 1},
        }
        if icon:
            card["icon"] = icon
        return card

    def tiles(self, *pairs: tuple[str, str], columns: int = 6) -> list[dict[str, Any]]:
        return self.clean([self.tile(k, n, columns=columns) for k, n in pairs])

    def entities(self, title: str, *pairs: tuple[str, str], columns: int | str = 12) -> dict[str, Any] | None:
        rows = [{"entity": e, "name": n} for k, n in pairs if (e := self.eid(k))]
        if not rows:
            return None
        return {"type": "entities", "title": title, "entities": rows, "grid_options": {"columns": columns}}

    def confirm_button(self, key: str, name: str, confirm: str, *, icon: str, columns: int = 12) -> dict[str, Any] | None:
        entity_id = self.eid(key)
        if not entity_id:
            return None
        return {
            "type": "button",
            "entity": entity_id,
            "name": name,
            "icon": icon,
            "show_state": False,
            "icon_height": "40px",
            "tap_action": {
                "action": "perform-action",
                "perform_action": "button.press",
                "target": {"entity_id": entity_id},
                "confirmation": {"text": confirm},
            },
            "hold_action": {"action": "none"},
            "grid_options": {"columns": columns, "rows": 2},
        }

    def data_card(self, *, dataset: str = "usage", period: str = "day", title: str | None = None,
                  columns: list[str] | None = None, max_height: int = 560) -> dict[str, Any] | None:
        if not self.has_data_card:
            return None
        card: dict[str, Any] = {
            "type": DATA_CARD,
            "dataset": dataset,
            "max_height": max_height,
            "grid_options": {"columns": "full"},
        }
        if dataset == "usage":
            card["period"] = period
        if title is not None:
            card["title"] = title
        if columns:
            card["columns"] = columns
        return card

    # -------------------------------------------------- native statistics graph

    @staticmethod
    def date_picker() -> dict[str, Any]:
        return {
            "type": "energy-date-selection",
            "collection_key": COLLECTION_KEY,
            "grid_options": {"columns": "full"},
        }

    def stat_graph(
        self,
        title: str,
        series: list[tuple[str, str | None]],
        *,
        stat_types: list[str],
        chart_type: str = "bar",
        date_picker: bool = True,
        period: str = "day",
        days: int = 30,
        unit: str | None = None,
        columns: int | str = 12,
    ) -> dict[str, Any] | None:
        """`series` is (statistic id or our/Shelly key, display name)."""
        entities = []
        for ref, name in series:
            sid = ref if ":" in ref else self.eid(ref)
            if not sid:
                continue
            item: dict[str, Any] = {"entity": sid}
            if name:
                item["name"] = name
            entities.append(item)
        if not entities:
            return None
        card: dict[str, Any] = {
            "type": "statistics-graph",
            "title": title,
            "entities": entities,
            "stat_types": stat_types,
            "chart_type": chart_type,
            "hide_legend": len(entities) == 1,
            "grid_options": {"columns": columns, "rows": 6},
        }
        if date_picker:
            card["energy_date_selection"] = True
            card["collection_key"] = COLLECTION_KEY
            card["period"] = "auto"
        else:
            card["period"] = period
            card["days_to_show"] = days
        if unit:
            card["unit"] = unit
        return card

    # -------------------------------------------------------------- apexcharts

    def chart(
        self,
        title: str,
        caption: str,
        series: list[dict[str, Any] | None],
        *,
        span: str,
        default_type: str = "column",
        stacked: bool = False,
        height: int = 260,
        y_title: str = "",
        end: str | None = "day",
        offset: str | None = None,
        label_format: str | None = None,
        yaxis: list[dict[str, Any]] | None = None,
        decimals: int = 1,
        update_interval: str = "10min",
        columns: int | str = 12,
        no_data: str = NO_DATA,
    ) -> dict[str, Any] | None:
        series = self.clean(series)
        if not series or not self.apex:
            return None
        for item in series:
            item.setdefault("type", default_type)
            if item["type"] not in ALLOWED_SERIES_TYPES:
                raise ValueError(f"{title!r}: series type {item['type']!r} not allowed")
        apex: dict[str, Any] = {
            "chart": {
                "height": height,
                "toolbar": {"show": False},
                "zoom": {"enabled": False},
                "animations": {"enabled": False},
                "fontFamily": "var(--paper-font-body1_-_font-family)",
                "parentHeightOffset": 0,
            },
            "subtitle": {
                "text": caption,
                "offsetY": 2,
                "style": {"fontSize": "11px", "fontWeight": 400, "color": "var(--secondary-text-color)"},
            },
            "grid": {"borderColor": "rgba(127,127,127,0.18)", "strokeDashArray": 3, "padding": {"left": 4, "right": 8, "top": 0}},
            "dataLabels": {"enabled": False},
            "legend": {
                "show": len(series) > 1,
                "position": "bottom",
                "horizontalAlign": "left",
                "fontSize": "11px",
                "markers": {"size": 5},
                "itemMargin": {"horizontal": 8},
            },
            "stroke": {
                "curve": "smooth",
                "width": [0 if s.get("type") == "column" else 2 for s in series],
                "lineCap": "round",
            },
            "markers": {"size": 0, "strokeWidth": 0, "hover": {"sizeOffset": 3}},
            "plotOptions": {
                "bar": {
                    "columnWidth": "72%",
                    "borderRadius": 2,
                    "borderRadiusApplication": "end",
                    "borderRadiusWhenStacked": "last",
                }
            },
            "yaxis": {
                "title": {"text": y_title, "style": {"fontSize": "10px", "fontWeight": 400}},
                "forceNiceScale": True,
                "decimalsInFloat": decimals,
                "labels": {"style": {"fontSize": "10px"}},
            },
            "xaxis": {
                "axisBorder": {"show": False},
                "axisTicks": {"show": False},
                "tickAmount": 6,
                "labels": {"style": {"fontSize": "10px"}, "hideOverlappingLabels": True, "trim": False},
            },
            "tooltip": {"shared": True, "followCursor": False, "intersect": False},
            "noData": {"text": no_data, "style": {"fontSize": "13px", "color": "var(--secondary-text-color)"}},
        }
        if any(s.get("type") == "area" for s in series):
            apex["fill"] = {
                "type": "gradient",
                "gradient": {"shadeIntensity": 0.2, "opacityFrom": 0.45, "opacityTo": 0.05, "stops": [0, 95, 100]},
            }
        if any(s.get("type") in ("column", "area") for s in series):
            # Bars must start at zero or a day of 22 kWh looks like half of one of 31.
            apex["yaxis"]["min"] = 0
        if label_format:
            apex["xaxis"]["labels"]["format"] = label_format
            apex["tooltip"]["x"] = {"format": label_format}
        card: dict[str, Any] = {
            "type": APEX_CARD,
            "header": {"show": True, "title": title, "show_states": False},
            "graph_span": span,
            "stacked": stacked,
            "update_interval": update_interval,
            "apex_config": apex,
            "series": series,
            "grid_options": {"columns": columns},
        }
        if yaxis:
            # With more than one axis every series must name its axis, or the card
            # throws "yaxis_id ... doesn't exist". Unnamed series go on the first.
            for item in series:
                item.setdefault("yaxis_id", yaxis[0]["id"])
            for axis in yaxis:
                uses_bars = any(
                    s.get("yaxis_id") == axis["id"] and s.get("type") in ("column", "area") for s in series
                )
                if uses_bars:
                    axis.setdefault("min", 0)
            card["yaxis"] = yaxis
            apex.pop("yaxis")
        span_cfg: dict[str, Any] = {}
        if end:
            span_cfg["end"] = end
        if offset:
            span_cfg["offset"] = offset
        if span_cfg:
            card["span"] = span_cfg
        return card

    def anchor(self) -> str | None:
        """A sensor that always exists, to hang recorder-reading series on."""
        return self.eid("stored_hours") or self.eid("today_energy")

    def stat_series(
        self,
        name: str,
        ref: str,
        *,
        period: str,
        colour: str,
        unit: str = " kWh",
        value_key: str = "change",
        series_type: str | None = None,
        precision: int = 2,
        yaxis_id: str | None = None,
        opacity: float | None = None,
    ) -> dict[str, Any] | None:
        """One recorder statistic, fetched inside the card. `ref` is a statistic id or a key."""
        anchor = self.anchor()
        sid = ref if ":" in ref else self.eid(ref)
        if anchor is None or sid is None:
            return None
        js = (
            WS_HELPERS
            + f" const id = '{sid}';"
            " const r = await ws({type: 'recorder/statistics_during_period',"
            "  start_time: start.toISOString(), end_time: end.toISOString(),"
            f"  statistic_ids: [id], period: '{period}', types: ['{value_key}']}});"
            " const rows = (r && r[id]) || [];"
            f" return rows.map(x => [mid(x), x.{value_key} === undefined ? null : x.{value_key}]);"
        )
        series: dict[str, Any] = {
            "entity": anchor,
            "name": name,
            "color": colour,
            "unit": unit,
            "float_precision": precision,
            "data_generator": js,
            "group_by": {"func": "raw", "duration": "1d", "fill": "null"},
            "extend_to": False,
            "show": {"legend_value": False},
        }
        if series_type:
            series["type"] = series_type
        if yaxis_id:
            series["yaxis_id"] = yaxis_id
        if opacity is not None:
            series["opacity"] = opacity
        return series

    def sum_series(
        self,
        name: str,
        refs: list[str],
        *,
        period: str,
        colour: str,
        unit: str = " kWh",
        series_type: str | None = None,
        precision: int = 2,
    ) -> dict[str, Any] | None:
        """Several statistics added bucket by bucket (e.g. day + night = total)."""
        anchor = self.anchor()
        ids = [r if ":" in r else self.eid(r) for r in refs]
        if anchor is None or not all(ids):
            return None
        js = (
            WS_HELPERS
            + f" const ids = {ids!r};"
            " const r = await ws({type: 'recorder/statistics_during_period',"
            "  start_time: start.toISOString(), end_time: end.toISOString(),"
            f"  statistic_ids: ids, period: '{period}', types: ['change']}});"
            " const total = new Map();"
            " for (const id of ids) for (const x of (r && r[id]) || []) {"
            "   if (x.change === null || x.change === undefined) continue;"
            "   total.set(mid(x), (total.get(mid(x)) || 0) + x.change); }"
            " return [...total.entries()].sort((a, b) => a[0] - b[0]);"
        )
        series = {
            "entity": anchor,
            "name": name,
            "color": colour,
            "unit": unit,
            "float_precision": precision,
            "data_generator": js,
            "group_by": {"func": "raw", "duration": "1d", "fill": "null"},
            "extend_to": False,
            "show": {"legend_value": False},
        }
        if series_type:
            series["type"] = series_type
        return series

    def yearly_series(self, name: str, refs: list[str], *, colour: str, unit: str = " kWh") -> dict[str, Any] | None:
        """Calendar-year totals (the statistics API has no year bucket with local years)."""
        anchor = self.anchor()
        ids = [r if ":" in r else self.eid(r) for r in refs]
        if anchor is None or not all(ids):
            return None
        js = (
            WS_HELPERS
            + f" const ids = {ids!r};"
            " const r = await ws({type: 'recorder/statistics_during_period',"
            "  start_time: start.toISOString(), end_time: end.toISOString(),"
            "  statistic_ids: ids, period: 'month', types: ['change']});"
            " const byYear = new Map();"
            " for (const id of ids) for (const x of (r && r[id]) || []) {"
            "   if (x.change === null || x.change === undefined) continue;"
            "   const y = new Date(x.start).getFullYear();"
            "   byYear.set(y, (byYear.get(y) || 0) + x.change); }"
            " return [...byYear.entries()].sort((a, b) => a[0] - b[0])"
            "   .map(([y, v]) => [new Date(y, 6, 1).getTime(), v]);"
        )
        return {
            "entity": anchor,
            "name": name,
            "color": colour,
            "unit": unit,
            "float_precision": 0,
            "data_generator": js,
            "group_by": {"func": "raw", "duration": "1d"},
            "extend_to": False,
            "show": {"legend_value": False},
        }

    def ratio_series(
        self, name: str, top: list[str], bottom: list[str], *, period: str, colour: str, unit: str,
        scale: float = 1.0, minus_one: bool = False, precision: int = 2, yaxis_id: str | None = None,
        series_type: str = "line",
    ) -> dict[str, Any] | None:
        """sum(top) / sum(bottom) per bucket, optionally as a percentage difference."""
        anchor = self.anchor()
        t = [r if ":" in r else self.eid(r) for r in top]
        b = [r if ":" in r else self.eid(r) for r in bottom]
        if anchor is None or not all(t) or not all(b):
            return None
        js = (
            WS_HELPERS
            + f" const top = {t!r}, bottom = {b!r};"
            " const r = await ws({type: 'recorder/statistics_during_period',"
            "  start_time: start.toISOString(), end_time: end.toISOString(),"
            f"  statistic_ids: [...top, ...bottom], period: '{period}', types: ['change']}});"
            " const sum = (ids) => { const m = new Map();"
            "   for (const id of ids) for (const x of (r && r[id]) || []) {"
            "     if (x.change === null || x.change === undefined) continue;"
            "     m.set(mid(x), (m.get(mid(x)) || 0) + x.change); } return m; };"
            " const a = sum(top), c = sum(bottom); const out = [];"
            " for (const [k, v] of a) { const d = c.get(k); if (!d) continue;"
            f"   out.push([k, (v / d {'- 1' if minus_one else ''}) * {scale}]); }}"
            " return out.sort((x, y) => x[0] - y[0]);"
        )
        series: dict[str, Any] = {
            "entity": anchor,
            "name": name,
            "color": colour,
            "unit": unit,
            "type": series_type,
            "float_precision": precision,
            "data_generator": js,
            "group_by": {"func": "raw", "duration": "1d"},
            "extend_to": False,
            "show": {"legend_value": False},
        }
        if yaxis_id:
            series["yaxis_id"] = yaxis_id
        return series

    def minute_series(
        self, name: str, field: str, *, colour: str, unit: str = " W", series_type: str = "line", precision: int = 0,
    ) -> dict[str, Any] | None:
        """A column from the integration's own minute table (kept for good)."""
        anchor = self.anchor()
        if anchor is None:
            return None
        js = (
            WS_HELPERS
            + " const r = await ws({type: 'electricity_analyzer/minutes',"
            "  start: start.toISOString(), end: end.toISOString()});"
            f" return ((r && r.rows) || []).map(x => [new Date(x.minute_utc).getTime(), x.{field}]);"
        )
        return {
            "entity": anchor,
            "name": name,
            "color": colour,
            "unit": unit,
            "type": series_type,
            "float_precision": precision,
            "data_generator": js,
            "group_by": {"func": "raw", "duration": "1min"},
            "extend_to": False,
            "show": {"legend_value": False},
        }

    def entity_series(
        self, name: str, key: str, *, colour: str, unit: str = " W", func: str = "avg", duration: str = "2min",
        series_type: str = "line",
    ) -> dict[str, Any] | None:
        """A live entity's own history (state changes), averaged into buckets."""
        entity_id = self.eid(key)
        if not entity_id:
            return None
        return {
            "entity": entity_id,
            "name": name,
            "color": colour,
            "unit": unit,
            "type": series_type,
            "float_precision": 0,
            "group_by": {"func": func, "duration": duration},
            "extend_to": "now",
            "show": {"legend_value": False},
        }

    def attr_series(
        self, name: str, key: str, body: str, *, colour: str, unit: str = " kWh", series_type: str | None = None,
        opacity: float | None = None, precision: int = 2, yaxis_id: str | None = None,
    ) -> dict[str, Any] | None:
        """A series built from a sensor's attributes (forecasts, profiles)."""
        entity_id = self.eid(key)
        if not entity_id:
            return None
        series: dict[str, Any] = {
            "entity": entity_id,
            "name": name,
            "color": colour,
            "unit": unit,
            "float_precision": precision,
            "data_generator": body,
            "group_by": {"func": "raw", "duration": "1d"},
            "extend_to": False,
            "show": {"legend_value": False},
        }
        if series_type:
            series["type"] = series_type
        if opacity is not None:
            series["opacity"] = opacity
        if yaxis_id:
            series["yaxis_id"] = yaxis_id
        return series
