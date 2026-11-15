"""Sensors. Every figure the dashboard shows is one of these, or a statistic.

Two feeds:

* LIVE: recomputed from the Shelly's own state pushes, published at most every five
  seconds. Today so far, the spend rate, the band, the meter registers right now.
* INSIGHTS: recomputed every five minutes and after each hourly rollup. Periods,
  the 24/7 hall load, forecasts, the ESB comparison, SSE status.

Bulky attributes (per-day forecast lists, profiles) are excluded from the recorder:
they are there for the dashboard's charts, not for history.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .engine.tariff import local_date, local_midnight
from .entity import AnalyzerEntity
from .runtime import Analyzer

EUR = "EUR"
_LOG = logging.getLogger(__name__)

_BIG = frozenset(
    {
        "per_day",
        "months",
        "seasons",
        "profile",
        "house_kw",
        "hall_kw",
        "rest_kw",
        "weekday_kwh",
        "month_factors",
        "weekday_factors",
        "month_evidence_days",
        "reads",
        "reads_compare",
        "anchors",
        "tariffs",
        "tariff",
        "hall_months",
        "anomalies",
        "lessons",
        "price_segments",
        "recent",
        "weights",
        "samples",
        "history",
        "today",
        "transactions",
        "last_bill",
        "sse",
        "collection",
        "hourly",
    }
)


@dataclass(frozen=True, kw_only=True)
class AnalyzerSensorDescription(SensorEntityDescription):
    feed: str  # "live" | "insights"
    value_fn: Callable[[dict[str, Any], Analyzer], Any]
    attrs_fn: Callable[[dict[str, Any], Analyzer], dict[str, Any] | None] | None = None
    # Today's total that may dip as it is re-corrected (hall, rest of house): a TOTAL
    # that resets at Irish midnight, since TOTAL_INCREASING takes any dip for a reset.
    daily_total: bool = False


def _g(d: dict[str, Any] | None, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _r(v: Any, n: int = 2) -> Any:
    return None if v is None else round(float(v), n)


def _today(key: str, n: int = 3):
    return lambda d, a: _r(_g(d, "today", key), n)


def _period(name: str, key: str, n: int = 2):
    def value(d, a):
        summary = _g(d, "summaries", name) or {}
        # A period with no measured hour has no figure: "unknown", not 0 kWh / €0.
        if not summary.get("hours") and not summary.get("house_kwh"):
            return None
        return _r(summary.get(key), n)

    return value


def _fc(d: dict[str, Any]):
    return d.get("forecast")


def _fc_attr(fn):
    def inner(d, a):
        report = _fc(d)
        if report is None or not report.available:
            return {"reason": getattr(report, "reason", None)}
        return fn(report)

    return inner


_PERIOD_KEYS = (
    "house_kwh", "day_kwh", "night_kwh", "hall_kwh", "rest_kwh", "energy_cost", "day_cost", "night_cost",
    "fixed_cost", "standing_cost", "pso_cost", "total_cost", "hall_cost", "rest_cost",
)


def _live_period_dict(d: dict[str, Any], a: Analyzer, name: str) -> dict[str, Any] | None:
    """A period that includes today: its stored days (recomputed every few minutes) plus
    today's live figures (every few seconds), so the sensor moves with the clamps."""
    base = _g(a.insights_coordinator.data, "summaries_base", name)
    if base is None:
        return None
    today = d.get("today") or {}
    out = {k: (base.get(k) or 0.0) + (today.get(k) or 0.0) for k in _PERIOD_KEYS}
    out["hours"] = (base.get("hours") or 0) + (today.get("hours") or 0)
    out["days"] = (base.get("days") or 0) + (1 if today.get("house_kwh") or today.get("hours") else 0)
    out["unpriced_hours"] = (base.get("unpriced_hours") or 0) + (today.get("unpriced_hours") or 0)
    if out["house_kwh"]:
        out["night_share_pct"] = round(out["night_kwh"] / out["house_kwh"] * 100, 1)
    return out


def _live_period(name: str, key: str, n: int = 2):
    def value(d, a):
        period = _live_period_dict(d, a, name)
        if period is None or (not period["hours"] and not period["house_kwh"] and not period["total_cost"]):
            return None
        return _r(period.get(key), n)

    return value


def _live_period_attrs(name: str):
    def inner(d, a):
        period = _live_period_dict(d, a, name)
        return None if period is None else {k: _r(v, 3) if isinstance(v, float) else v for k, v in period.items()}

    return inner


def _period_attrs(name: str):
    def inner(d, a):
        s = _g(d, "summaries", name) or {}
        keep = (
            "house_kwh", "day_kwh", "night_kwh", "hall_kwh", "rest_kwh", "energy_cost",
            "day_cost", "night_cost", "fixed_cost", "standing_cost", "pso_cost", "total_cost",
            "hall_cost", "rest_cost", "night_share_pct", "hall_share_pct", "effective_rate",
            "peak_w", "house_pf", "hall_pf", "days", "hours", "unpriced_hours",
        )
        return {k: s.get(k) for k in keep if k in s}

    return inner


def _registers(kind: str):
    def value(d, a):
        return _r(_g(d, "registers", kind), 2)

    def attrs(d, a):
        regs = d.get("registers")
        if not regs:
            return {"note": "enter a meter reading in the integration's options to start the estimate"}
        return {
            "dial": regs.get(f"{kind}_dial"),
            "as_of": regs.get("as_of"),
            "anchor_reading": regs.get(f"anchor_{kind}"),
            "anchor_taken_at": regs.get("anchor_taken_at"),
            "anchor_origin": regs.get("anchor_origin"),
            "used_since_anchor_kwh": regs.get(f"since_anchor_{kind}_kwh"),
            "calibration": regs.get("calibration"),
            "hours_missing": regs.get("hours_missing"),
            "notes": regs.get("notes"),
        }

    return value, attrs


_reg_day = _registers("day")
_reg_night = _registers("night")


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return dt_util.parse_datetime(str(value))


LIVE: tuple[AnalyzerSensorDescription, ...] = (
    AnalyzerSensorDescription(
        key="rest_power",
        name="Rest of house power",
        feed="live",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        icon="mdi:home-lightning-bolt-outline",
        value_fn=lambda d, a: _r(d.get("rest_w"), 1),
        attrs_fn=lambda d, a: {"house_w": d.get("house_w"), "hall_w": d.get("hall_w")},
    ),
    AnalyzerSensorDescription(
        key="spend_rate",
        name="Spend rate",
        feed="live",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/h",
        suggested_display_precision=3,
        icon="mdi:cash-clock",
        value_fn=lambda d, a: _r(d.get("cost_rate_eur_h"), 4),
        attrs_fn=lambda d, a: {"unit_rate": d.get("rate"), "band": d.get("band"), "excludes": "standing charge and PSO"},
    ),
    AnalyzerSensorDescription(
        key="tariff_band",
        translation_key="tariff_band",
        name="Tariff band",
        feed="live",
        device_class=SensorDeviceClass.ENUM,
        options=["day", "night"],
        icon="mdi:weather-night",
        value_fn=lambda d, a: d.get("band"),
        attrs_fn=lambda d, a: {
            "unit_rate": d.get("rate"),
            "next_band": d.get("next_band"),
            "next_change": _ts(d.get("next_change")).isoformat() if d.get("next_change") else None,
            "night_window_tonight": d.get("tonight"),
            "meter_clock": _g(d, "tariff", "window", "clock"),
            "supplier": _g(d, "tariff", "supplier"),
            "plan": _g(d, "tariff", "plan"),
        },
    ),
    AnalyzerSensorDescription(
        key="price_now",
        name="Price now",
        feed="live",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="c/kWh",
        suggested_display_precision=2,
        icon="mdi:cash-fast",
        value_fn=lambda d, a: None if d.get("rate") is None else round(d["rate"] * 100, 2),
        attrs_fn=lambda d, a: {
            "band": d.get("band"),
            "next_price": _next_price(d),
            "next_change": _ts(d.get("next_change")).isoformat() if d.get("next_change") else None,
            "includes_vat": True,
            "price_segments": d.get("price_segments"),
        },
    ),
    AnalyzerSensorDescription(
        key="clock_offset",
        name="Clock offset",
        feed="live",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock-check-outline",
        value_fn=lambda d, a: _g(d, "clock", "offset"),
        attrs_fn=lambda d, a: {
            **(d.get("clock") or {}),
            "meaning": "how far Home Assistant's clock is from NTP time; band switching uses NTP-corrected time "
            "when this is a second or more",
        },
    ),
    AnalyzerSensorDescription(
        key="next_band_change",
        name="Next band change",
        feed="live",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-start",
        value_fn=lambda d, a: _ts(d.get("next_change")),
        attrs_fn=lambda d, a: {"to": d.get("next_band"), "night_window_tonight": d.get("tonight")},
    ),
    AnalyzerSensorDescription(
        key="unit_rate",
        name="Unit rate",
        feed="live",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        icon="mdi:currency-eur",
        value_fn=lambda d, a: d.get("rate"),
        attrs_fn=lambda d, a: {
            "band": d.get("band"),
            "day_rate": _g(d, "tariff", "day_rate"),
            "night_rate": _g(d, "tariff", "night_rate"),
            "includes_vat": True,
        },
    ),
    AnalyzerSensorDescription(
        key="today_energy",
        name="Today energy",
        feed="live",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_today("house_kwh"),
        attrs_fn=lambda d, a: {
            **{
                k: _g(d, "today", k)
                for k in ("day_kwh", "night_kwh", "hall_kwh", "rest_kwh", "night_share_pct", "hall_share_pct", "peak_w")
            },
            # Each hour of today, the current one as it fills: the "Today, hour by hour" chart.
            "hourly": d.get("today_hourly"),
        },
    ),
    AnalyzerSensorDescription(
        key="today_day_energy",
        name="Today day-rate energy",
        feed="live",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        icon="mdi:white-balance-sunny",
        value_fn=_today("day_kwh"),
    ),
    AnalyzerSensorDescription(
        key="today_night_energy",
        name="Today night-rate energy",
        feed="live",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        icon="mdi:weather-night",
        value_fn=_today("night_kwh"),
    ),
    AnalyzerSensorDescription(
        key="today_hall_energy",
        name="Today hall energy",
        feed="live",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        daily_total=True,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        icon="mdi:fishbowl-outline",
        value_fn=_today("hall_kwh"),
    ),
    AnalyzerSensorDescription(
        key="today_rest_energy",
        name="Today rest of house energy",
        feed="live",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        daily_total=True,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        icon="mdi:home-outline",
        value_fn=_today("rest_kwh"),
    ),
    AnalyzerSensorDescription(
        key="today_cost",
        name="Today cost",
        feed="live",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        value_fn=_today("total_cost", 3),
        attrs_fn=lambda d, a: {
            "units": _g(d, "today", "energy_cost"),
            "day_units": _g(d, "today", "day_cost"),
            "night_units": _g(d, "today", "night_cost"),
            "standing_and_pso_so_far": _g(d, "today", "fixed_cost"),
            "standing_charge_so_far": _g(d, "today", "standing_cost"),
            "pso_levy_so_far": _g(d, "today", "pso_cost"),
            "hall": _g(d, "today", "hall_cost"),
            "rest_of_house": _g(d, "today", "rest_cost"),
            "unpriced_hours": _g(d, "today", "unpriced_hours"),
            "includes_vat": True,
        },
    ),
    AnalyzerSensorDescription(
        key="today_hall_cost",
        name="Today hall cost",
        feed="live",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        icon="mdi:fishbowl-outline",
        value_fn=_today("hall_cost", 3),
    ),
    AnalyzerSensorDescription(
        key="meter_reading_day",
        name="Meter reading day",
        feed="live",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        icon="mdi:counter",
        value_fn=_reg_day[0],
        attrs_fn=_reg_day[1],
    ),
    AnalyzerSensorDescription(
        key="meter_reading_night",
        name="Meter reading night",
        feed="live",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        icon="mdi:counter",
        value_fn=_reg_night[0],
        attrs_fn=_reg_night[1],
    ),
    AnalyzerSensorDescription(
        key="hall_above_house_today",
        name="Hall above house today",
        feed="live",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:scale-unbalanced",
        value_fn=lambda d, a: d.get("hall_over_minutes"),
        attrs_fn=lambda d, a: {
            "meaning": "time the hall clamp read more power than the full-house clamp, comparing only readings "
            "taken within 2 s of each other (CT tolerance and power factor)",
            "counter_resets": d.get("counter_resets"),
        },
    ),
    AnalyzerSensorDescription(
        key="hall_correction",
        name="Hall clamp correction",
        feed="live",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=4,
        icon="mdi:scale-balance",
        value_fn=lambda d, a: _g(d, "hall_correction", "factor"),
        attrs_fn=lambda d, a: {
            "meaning": "scale applied to the hall clamp so it never exceeds the whole house; the smallest "
            "correction the time-aligned readings prove (never applied upwards, at most 10%)",
            **{k: v for k, v in (d.get("hall_correction") or {}).items() if k != "factor"},
        },
    ),
)


def _energy(key: str, name: str, period: str, icon: str | None = None, *, live: bool = False) -> AnalyzerSensorDescription:
    return AnalyzerSensorDescription(
        key=key,
        name=name,
        feed="live" if live else "insights",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        icon=icon,
        value_fn=_live_period(period, "house_kwh", 2) if live else _period(period, "house_kwh", 2),
        attrs_fn=_live_period_attrs(period) if live else _period_attrs(period),
    )


def _money(
    key: str, name: str, value_fn, attrs_fn=None, icon: str | None = None, *, feed: str = "insights"
) -> AnalyzerSensorDescription:
    return AnalyzerSensorDescription(
        key=key,
        name=name,
        feed=feed,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        icon=icon,
        value_fn=value_fn,
        attrs_fn=attrs_fn,
    )


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _pick(d: dict[str, Any] | None, *keys: str) -> dict[str, Any] | None:
    return {k: d.get(k) for k in keys} if d else None


_READ_METHODS = {"C": "customer", "A": "actual (ESB)", "E": "estimate"}


def _sse_register(d: dict[str, Any], kind: str) -> dict[str, Any]:
    meter = _g(d, "sse", "meter") or {}
    return next((r for r in meter.get("registers") or [] if r.get("kind") == kind), {})


def _sse_register_attrs(d: dict[str, Any], kind: str) -> dict[str, Any] | None:
    reg = _sse_register(d, kind)
    if not reg:
        return None
    history = [r for r in d.get("sse_history") or [] if r.get("register") == kind]
    method = str(reg.get("last_method") or "").upper()
    return {
        "date": reg.get("last_date"),
        "sent_at": reg.get("last_inserted"),
        "status": reg.get("last_status"),
        "method": _READ_METHODS.get(method, reg.get("last_method")),
        "units_used": history[0].get("units_used") if history else None,
        "history": history[:24],
    }


def _sse_reading(kind: str, name: str, icon: str) -> AnalyzerSensorDescription:
    """The last reading SSE holds for one register, as SSE has it (not our estimate)."""
    return AnalyzerSensorDescription(
        key=f"sse_last_reading_{kind}",
        name=name,
        feed="insights",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        icon=icon,
        value_fn=lambda d, a: _sse_register(d, kind).get("last_value"),
        attrs_fn=lambda d, a: _sse_register_attrs(d, kind),
    )


INSIGHTS: tuple[AnalyzerSensorDescription, ...] = (
    _energy("yesterday_energy", "Yesterday energy", "yesterday"),
    _money("yesterday_cost", "Yesterday cost", _period("yesterday", "total_cost"), _period_attrs("yesterday")),
    # Periods that include today follow the live feed (every few seconds).
    _energy("week_energy", "This week energy", "week", live=True),
    _money("week_cost", "This week cost", _live_period("week", "total_cost"), _live_period_attrs("week"), feed="live"),
    _energy("month_energy", "This month energy", "month", live=True),
    _money("month_cost", "This month cost", _live_period("month", "total_cost"), _live_period_attrs("month"), feed="live"),
    _energy("year_energy", "This year energy", "year", live=True),
    _money("year_cost", "This year cost", _live_period("year", "total_cost"), _live_period_attrs("year"), feed="live"),
    AnalyzerSensorDescription(
        key="avg_daily_energy",
        name="Average daily energy",
        feed="insights",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda d, a: _r(
            (_g(d, "summaries", "last_30", "house_kwh") or 0) / max(_g(d, "summaries", "last_30", "days") or 0, 1), 2
        )
        if _g(d, "summaries", "last_30", "days")
        else None,
        attrs_fn=lambda d, a: {"window": "last 30 complete days", "days": _g(d, "summaries", "last_30", "days")},
    ),
    _money(
        "avg_daily_cost",
        "Average daily cost",
        lambda d, a: _r(
            (_g(d, "summaries", "last_30", "total_cost") or 0) / max(_g(d, "summaries", "last_30", "days") or 0, 1), 2
        )
        if _g(d, "summaries", "last_30", "days")
        else None,
        lambda d, a: {"window": "last 30 complete days", "includes": "units, standing charge, PSO, VAT"},
    ),
    AnalyzerSensorDescription(
        key="week_change",
        name="Last 7 days vs previous 7",
        feed="insights",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        icon="mdi:swap-vertical",
        value_fn=lambda d, a: _g(d, "compare_week", "change_pct"),
        attrs_fn=lambda d, a: d.get("compare_week"),
    ),
    AnalyzerSensorDescription(
        key="baseline_power",
        name="Always-on baseline",
        feed="insights",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        icon="mdi:power-plug-outline",
        value_fn=lambda d, a: _r(d.get("baseline_w"), 0),
        attrs_fn=lambda d, a: {"method": "5th percentile of hourly average power, last 14 days"},
    ),
    AnalyzerSensorDescription(
        key="hall_average_power",
        name="Hall average power",
        feed="insights",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        icon="mdi:fishbowl-outline",
        value_fn=lambda d, a: _g(d, "hall", "avg_w_7d"),
        attrs_fn=lambda d, a: d.get("hall"),
    ),
    AnalyzerSensorDescription(
        key="hall_share",
        name="Hall share of usage",
        feed="insights",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        icon="mdi:chart-pie",
        value_fn=lambda d, a: _g(d, "hall", "share_30d_pct"),
        attrs_fn=lambda d, a: {"window": "last 30 complete days", "hall_kwh": _g(d, "hall", "kwh_30d")},
    ),
    _money(
        "hall_daily_cost",
        "Hall daily cost",
        lambda d, a: _g(d, "hall", "daily_cost"),
        lambda d, a: {"window": "last 30 complete days", "excludes": "standing charge and PSO"},
        icon="mdi:fishbowl-outline",
    ),
    _money(
        "hall_yearly_cost",
        "Hall yearly cost",
        lambda d, a: _g(d, "hall_forecast", "year_energy_cost") or _g(d, "hall", "annual_cost"),
        lambda d, a: {
            "basis": "forecast" if d.get("hall_forecast") else "last 30 days x 365",
            "year_kwh": _g(d, "hall_forecast", "year_kwh") or _g(d, "hall", "annual_kwh"),
            "months": _g(d, "hall_forecast", "months"),
        },
        icon="mdi:fishbowl-outline",
    ),
    AnalyzerSensorDescription(
        key="night_share",
        name="Night-rate share",
        feed="insights",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        icon="mdi:weather-night",
        value_fn=lambda d, a: _g(d, "rates", "night_share_30d_pct"),
        attrs_fn=lambda d, a: {"window": "last 30 complete days"},
    ),
    AnalyzerSensorDescription(
        key="average_unit_price",
        name="Average unit price",
        feed="insights",
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        icon="mdi:currency-eur",
        value_fn=lambda d, a: _r(_g(d, "rates", "effective_30d"), 4),
        attrs_fn=lambda d, a: {
            "all_in_incl_standing_and_pso": _r(_g(d, "rates", "all_in_30d"), 4),
            "window": "last 30 complete days",
        },
    ),
    AnalyzerSensorDescription(
        key="house_power_factor",
        name="House power factor",
        feed="insights",
        device_class=SensorDeviceClass.POWER_FACTOR,
        suggested_display_precision=3,
        value_fn=lambda d, a: _g(d, "power", "house_pf_7d"),
        attrs_fn=lambda d, a: {**(d.get("power") or {}), "window": "last 7 days, real/apparent energy"},
    ),
    AnalyzerSensorDescription(
        key="hall_power_factor",
        name="Hall power factor",
        feed="insights",
        device_class=SensorDeviceClass.POWER_FACTOR,
        suggested_display_precision=3,
        value_fn=lambda d, a: _g(d, "power", "hall_pf_7d"),
        attrs_fn=lambda d, a: {"window": "last 7 days, real/apparent energy"},
    ),
    AnalyzerSensorDescription(
        key="forecast_today_energy",
        name="Forecast today energy",
        feed="insights",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        icon="mdi:crystal-ball",
        value_fn=lambda d, a: _r(_fc(d).today.kwh, 2) if _fc(d) and _fc(d).available else None,
        attrs_fn=_fc_attr(lambda r: {**r.today.as_dict(), "remaining_kwh": _r(r.today_remaining_kwh, 2)}),
    ),
    _money(
        "forecast_today_cost",
        "Forecast today cost",
        lambda d, a: _r(_fc(d).today.cost, 2) if _fc(d) and _fc(d).available else None,
        _fc_attr(lambda r: r.today.as_dict()),
        icon="mdi:crystal-ball",
    ),
    _money(
        "forecast_tomorrow_cost",
        "Forecast tomorrow cost",
        lambda d, a: _r(_fc(d).tomorrow.cost, 2) if _fc(d) and _fc(d).available else None,
        _fc_attr(lambda r: r.tomorrow.as_dict()),
        icon="mdi:crystal-ball",
    ),
    AnalyzerSensorDescription(
        key="forecast_month_energy",
        name="Forecast this month energy",
        feed="insights",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        icon="mdi:crystal-ball",
        value_fn=lambda d, a: _r(_fc(d).this_month.total_kwh, 1) if _fc(d) and _fc(d).available else None,
        attrs_fn=_fc_attr(lambda r: r.this_month.as_dict()),
    ),
    _money(
        "forecast_month_cost",
        "Forecast this month cost",
        lambda d, a: _r(_fc(d).this_month.total_cost, 2) if _fc(d) and _fc(d).available else None,
        _fc_attr(lambda r: r.this_month.as_dict()),
        icon="mdi:crystal-ball",
    ),
    _money(
        "forecast_30_days_cost",
        "Forecast next 30 days cost",
        lambda d, a: _fc(d).window(30)["cost"] if _fc(d) and _fc(d).available else None,
        _fc_attr(lambda r: r.window(30)),
        icon="mdi:crystal-ball",
    ),
    _money(
        "forecast_12_months_cost",
        "Forecast next 12 months cost",
        lambda d, a: _r(_fc(d).year_cost, 0) if _fc(d) and _fc(d).available else None,
        _fc_attr(
            lambda r: {
                "kwh": _r(r.year_kwh, 0),
                "low_kwh": _r(r.year_low, 0),
                "high_kwh": _r(r.year_high, 0),
                "months": [p.as_dict() for p in r.months],
                "seasons": [p.as_dict() for p in r.seasons],
            }
        ),
        icon="mdi:calendar-month",
    ),
    _money(
        "forecast_bill_cost",
        "Forecast billing period cost",
        lambda d, a: _r(_fc(d).bill_period.total_cost, 2)
        if _fc(d) and _fc(d).available and _fc(d).bill_period
        else None,
        _fc_attr(lambda r: r.bill_period.as_dict() if r.bill_period else {"note": "no billing period yet: SSE in live mode reads it from the account, or set a billing date in the options"}),
        icon="mdi:receipt-text-outline",
    ),
    AnalyzerSensorDescription(
        key="forecast_accuracy",
        name="Forecast accuracy",
        feed="insights",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:bullseye-arrow",
        value_fn=lambda d, a: _accuracy(d),
        attrs_fn=lambda d, a: {
            **(d.get("engine") or {}),
            "seasonal_model": _fc(d).summary() if _fc(d) else None,
        },
    ),
    AnalyzerSensorDescription(
        key="outdoor_temperature",
        name="Outdoor temperature",
        feed="insights",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda d, a: _g(d, "weather", "now", "temperature"),
        attrs_fn=lambda d, a: {**(_g(d, "weather", "now") or {}), "attribution": _g(d, "weather", "attribution")},
    ),
    AnalyzerSensorDescription(
        key="apparent_temperature",
        name="Feels like",
        feed="insights",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        icon="mdi:thermometer-lines",
        value_fn=lambda d, a: _g(d, "weather", "now", "apparent_temperature"),
    ),
    AnalyzerSensorDescription(
        key="wind_speed",
        name="Wind speed",
        feed="insights",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=0,
        value_fn=lambda d, a: _g(d, "weather", "now", "wind_speed"),
    ),
    AnalyzerSensorDescription(
        key="heating_degree_days_today",
        name="Heating degree days today",
        feed="insights",
        native_unit_of_measurement="°Cd",
        suggested_display_precision=1,
        icon="mdi:snowflake-thermometer",
        value_fn=lambda d, a: _g(d, "weather", "hdd_today"),
        attrs_fn=lambda d, a: {
            "base_temperature_c": 15.5,
            "meaning": "degree days accrued by today's completed hours so far (like today's energy)",
            "hours_counted": _g(d, "weather", "hdd_today_hours"),
            "expected_full_day": _g(d, "weather", "hdd_today_expected"),
        },
    ),
    AnalyzerSensorDescription(
        key="heating_sensitivity",
        name="Energy per degree day",
        feed="insights",
        native_unit_of_measurement="kWh/°Cd",
        suggested_display_precision=2,
        icon="mdi:home-thermometer",
        value_fn=lambda d, a: _r(_g(d, "weather", "fit", "kwh_per_degree_day"), 3),
        attrs_fn=lambda d, a: {
            "baseline_kwh": _g(d, "weather", "fit", "baseline_kwh"),
            "r2": _g(d, "weather", "fit", "r2"),
            "basis": _g(d, "weather", "fit", "basis"),
            "days": _g(d, "weather", "fit", "days"),
            "reliable": _g(d, "weather", "fit", "reliable"),
            "heating_share_pct": _g(d, "weather", "heating_share_pct"),
        },
    ),
    AnalyzerSensorDescription(
        key="hall_heating_sensitivity",
        name="Hall energy per degree day",
        feed="insights",
        native_unit_of_measurement="kWh/°Cd",
        suggested_display_precision=3,
        icon="mdi:fishbowl-outline",
        value_fn=lambda d, a: _r(_g(d, "hall_weather", "kwh_per_degree_day"), 3),
        attrs_fn=lambda d, a: d.get("hall_weather"),
    ),
    _money(
        "weather_cost_per_degree_day",
        "Cost per degree day",
        lambda d, a: _g(d, "weather", "eur_per_degree_day"),
        icon="mdi:snowflake-thermometer",
    ),
    _money(
        "forecast_weather_7_days",
        "Forecast next 7 days (weather)",
        lambda d, a: _g(d, "weather", "forecast", "cost_7"),
        lambda d, a: {
            "kwh": _g(d, "weather", "forecast", "kwh_7"),
            "seasonal_kwh": _g(d, "weather", "forecast", "seasonal_kwh_7"),
            "gap_pct": _g(d, "weather", "forecast", "gap_pct"),
            "per_day": _g(d, "weather", "forecast", "per_day"),
        },
        icon="mdi:weather-snowy-rainy",
    ),
    AnalyzerSensorDescription(
        key="forecast_weather_gap",
        name="Weather vs seasonal forecast",
        feed="insights",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        icon="mdi:swap-vertical",
        value_fn=lambda d, a: _g(d, "weather", "forecast", "gap_pct"),
        attrs_fn=lambda d, a: {"meaning": "the next 7 days: weather-driven model minus seasonal model, in %"},
    ),
    AnalyzerSensorDescription(
        key="open_meteo_requests",
        name="Open-Meteo requests",
        feed="insights",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cloud-download-outline",
        value_fn=lambda d, a: _g(d, "weather", "requests"),
        attrs_fn=lambda d, a: {"hours_held": _g(d, "weather", "hours"), "error": _g(d, "weather", "error")},
    ),
    _money(
        "bill_estimate",
        "Next bill estimate",
        lambda d, a: _g(d, "bill", "total"),
        lambda d, a: {
            **(d.get("bill") or {}),
            "last_bill": d.get("last_bill"),
            "sse": {k: v for k, v in (d.get("sse_billing") or {}).items() if k != "transactions"},
        },
        icon="mdi:receipt-text-outline",
    ),
    _money(
        "last_bill",
        "Last bill",
        lambda d, a: _g(d, "last_bill", "total"),
        lambda d, a: {k: v for k, v in (d.get("last_bill") or {}).items() if k != "total"} or None,
        icon="mdi:receipt-text-check-outline",
    ),
    AnalyzerSensorDescription(
        key="usage_drivers",
        name="Usage drivers",
        feed="insights",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        icon="mdi:tune-vertical-variant",
        value_fn=lambda d, a: None if _g(d, "drivers", "mape_pct") is None else round(100 - _g(d, "drivers", "mape_pct"), 1),
        attrs_fn=lambda d, a: {
            "meaning": "a learned model of what drives daily usage; each weight adds or subtracts kWh "
            "and is refitted every night (recent days count most)",
            **{k: v for k, v in (d.get("drivers") or {}).items()},
        },
    ),
    AnalyzerSensorDescription(
        key="esb_data_through",
        name="ESB data through",
        feed="insights",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:transmission-tower",
        value_fn=lambda d, a: _ts(_g(d, "reconcile", "esb_through")),
        attrs_fn=lambda d, a: {"mode": _g(d, "esb", "mode"), "lag_hours": _g(d, "reconcile", "lag_hours")},
    ),
    AnalyzerSensorDescription(
        key="esb_lag",
        name="ESB data lag",
        feed="insights",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=0,
        icon="mdi:timer-sand",
        value_fn=lambda d, a: _g(d, "reconcile", "lag_hours"),
    ),
    AnalyzerSensorDescription(
        key="esb_latest_day",
        name="ESB latest day energy",
        feed="insights",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        icon="mdi:transmission-tower",
        value_fn=lambda d, a: _g(d, "reconcile", "latest", "esb_kwh"),
        attrs_fn=lambda d, a: _g(d, "reconcile", "latest"),
    ),
    AnalyzerSensorDescription(
        key="shelly_vs_esb",
        name="Shelly vs ESB",
        feed="insights",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=2,
        icon="mdi:scale-balance",
        value_fn=lambda d, a: _g(d, "reconcile", "last_30_diff_pct"),
        attrs_fn=lambda d, a: {
            k: v for k, v in (d.get("reconcile") or {}).items() if k != "latest"
        },
    ),
    AnalyzerSensorDescription(
        key="meter_factor_day",
        name="Meter factor day",
        feed="insights",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=4,
        icon="mdi:tune-variant",
        value_fn=lambda d, a: _g(d, "calibration", "day"),
        attrs_fn=lambda d, a: {
            "meaning": "meter kWh per clamp kWh in the DAY register, learned from real meter readings; "
            "used for the meter-reading estimate and for every cost",
            **{k: v for k, v in (d.get("calibration") or {}).items() if k not in ("day", "night")},
        },
    ),
    AnalyzerSensorDescription(
        key="meter_factor_night",
        name="Meter factor night",
        feed="insights",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=4,
        icon="mdi:tune-variant",
        value_fn=lambda d, a: _g(d, "calibration", "night"),
        attrs_fn=lambda d, a: {
            "meaning": "meter kWh per clamp kWh in the NIGHT register, learned from real meter readings",
            "source": _g(d, "calibration", "source"),
            "readings": _g(d, "calibration", "readings"),
        },
    ),
    AnalyzerSensorDescription(
        key="esb_calibration",
        name="ESB calibration",
        feed="insights",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=4,
        icon="mdi:tune-variant",
        value_fn=lambda d, a: _g(d, "reconcile", "calibration"),
        attrs_fn=lambda d, a: {
            "meaning": "ESB half-hourly meter kWh per clamp kWh (median of recent complete days); the starting "
            "point for the meter factors until real readings outweigh it",
            "days": _g(d, "reconcile", "calibration_days"),
            "reads_compare": d.get("reads_compare"),
        },
    ),
    AnalyzerSensorDescription(
        key="esb_status",
        translation_key="esb_status",
        name="ESB status",
        feed="insights",
        device_class=SensorDeviceClass.ENUM,
        options=["off", "ok", "error", "waiting", "retrying"],
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:transmission-tower",
        value_fn=lambda d, a: (
            "off"
            if _g(d, "esb", "mode") == "off"
            else "retrying"  # a failure: tried again by itself 12 hours later (next_attempt)
            if _g(d, "esb", "failures")
            else "ok"
            if _g(d, "esb", "last_success")
            else "waiting"
        ),
        attrs_fn=lambda d, a: d.get("esb"),
    ),
    AnalyzerSensorDescription(
        key="sse_status",
        translation_key="sse_status",
        name="SSE status",
        feed="insights",
        device_class=SensorDeviceClass.ENUM,
        options=["off", "dry_run", "ready", "cooldown", "error", "retrying"],
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:send-check",
        value_fn=lambda d, a: _sse_status(d),
        attrs_fn=lambda d, a: {
            "mode": _g(d, "sse", "mode"),
            "next_allowed": _g(d, "sse", "next_allowed"),
            "last_submitted": _g(d, "sse", "last_submitted"),
            "last_attempt": _g(d, "sse", "last_attempt"),
            "sse_last_reading": _sse_last(d),
            "error": _g(d, "sse", "error"),
            "next_attempt": _g(d, "sse", "next_attempt"),
        },
    ),
    AnalyzerSensorDescription(
        key="sse_next_allowed",
        name="SSE next submission allowed",
        feed="insights",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-clock",
        value_fn=lambda d, a: _ts(_g(d, "sse", "next_allowed")),
    ),
    AnalyzerSensorDescription(
        key="sse_next_bill_date",
        name="SSE next bill date",
        feed="insights",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:calendar-start",
        value_fn=lambda d, a: _day(_g(d, "sse_billing", "next_bill_date")),
        attrs_fn=lambda d, a: _pick(
            d.get("sse_billing"), "supply_start", "last_bill_date", "last_bill_end", "payment_due", "fetched_at"
        ),
    ),
    _money(
        "sse_balance",
        "SSE account balance",
        lambda d, a: _g(d, "sse_billing", "balance_eur"),
        lambda d, a: _pick(
            d.get("sse_billing"), "balance_status", "overdue_eur", "direct_debit", "payment_due",
            "earliest_unpaid_due", "last_payment", "transactions", "fetched_at",
        ),
        icon="mdi:account-cash-outline",
    ),
    AnalyzerSensorDescription(
        key="sse_contract_end",
        name="SSE contract end",
        feed="insights",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:file-sign",
        value_fn=lambda d, a: _day(_g(d, "sse_billing", "contract_end")),
        attrs_fn=lambda d, a: _pick(d.get("sse_billing"), "contract_start", "plan", "standard_tariff", "supply_status"),
    ),
    _sse_reading("day", "SSE last reading day", "mdi:white-balance-sunny"),
    _sse_reading("night", "SSE last reading night", "mdi:weather-night"),
    AnalyzerSensorDescription(
        key="stored_hours",
        name="Stored hours",
        feed="insights",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:database",
        value_fn=lambda d, a: _g(d, "store", "hours"),
        attrs_fn=lambda d, a: {
            **(d.get("store") or {}),
            "tariffs": d.get("tariffs"),
            "anchors": d.get("anchors"),
            # What state the data is in (loading, collecting, today so far, weather...),
            # worded for the dashboard to show as it is.
            "collection": d.get("collection"),
            "energy_dashboard": d.get("energy_dashboard"),
        },
    ),
    AnalyzerSensorDescription(
        key="usage_profile",
        name="Usage profile",
        feed="insights",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:chart-bell-curve",
        value_fn=lambda d, a: _r(d.get("baseline_w"), 0),
        native_unit_of_measurement=UnitOfPower.WATT,
        attrs_fn=lambda d, a: {**(d.get("profile") or {}), "anomalies": d.get("anomalies")},
    ),
)


def _accuracy(d: dict[str, Any]) -> float | None:
    """100 minus the ensemble's mean absolute daily error over its recent scored days."""
    recent = [r["error_pct"] for r in _g(d, "engine", "recent") or [] if r.get("error_pct") is not None]
    if len(recent) >= 5:
        return round(100 - sum(abs(x) for x in recent) / len(recent), 1)
    report = _fc(d)
    if report and report.available and report.mape is not None:
        return round(100 - report.mape * 100, 1)
    return None


def _next_price(d: dict[str, Any]) -> float | None:
    segments = d.get("price_segments") or []
    nxt = _ts(d.get("next_change"))
    for seg in segments:
        if nxt is not None and _ts(seg["start"]) == nxt:
            return None if seg.get("rate") is None else round(seg["rate"] * 100, 2)
    return None


def _sse_status(d: dict[str, Any]) -> str:
    mode = _g(d, "sse", "mode")
    if mode in (None, "off"):
        return "off"
    if mode == "dry_run":
        return "dry_run"
    if _g(d, "sse", "failures"):
        return "retrying"  # tried again by itself 12 hours after the failure (next_attempt)
    if _g(d, "sse", "error"):
        return "error"
    nxt = _ts(_g(d, "sse", "next_allowed"))
    if nxt and nxt > dt_util.utcnow():
        return "cooldown"
    return "ready"


def _sse_last(d: dict[str, Any]) -> dict[str, Any] | None:
    meter = _g(d, "sse", "meter")
    if not meter:
        return None
    return {
        r["kind"]: {"value": r.get("last_value"), "date": r.get("last_date"), "status": r.get("last_status")}
        for r in meter.get("registers") or []
    } | {"submitted_last_24h": meter.get("submitted_last_24h"), "fetched_at": meter.get("fetched_at")}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    analyzer: Analyzer = entry.runtime_data
    entities = [AnalyzerSensor(analyzer, d) for d in (*LIVE, *INSIGHTS)]
    async_add_entities(entities)


class AnalyzerSensor(AnalyzerEntity, SensorEntity):
    entity_description: AnalyzerSensorDescription
    _unrecorded_attributes = _BIG

    def __init__(self, analyzer: Analyzer, description: AnalyzerSensorDescription) -> None:
        coordinator = analyzer.live_coordinator if description.feed == "live" else analyzer.insights_coordinator
        super().__init__(analyzer, coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data
        if data is None:
            return None
        try:
            return self.entity_description.value_fn(data, self.analyzer)
        except (TypeError, ValueError, ZeroDivisionError, AttributeError) as exc:
            # Missing inputs (no data yet) read as "unknown", but are not hidden from a log.
            _LOG.debug("%s: no value (%s)", self.entity_description.key, exc)
            return None

    @property
    def last_reset(self) -> datetime | None:
        if not self.entity_description.daily_total:
            return None
        return local_midnight(local_date(dt_util.utcnow()))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None or self.entity_description.attrs_fn is None:
            return None
        try:
            return self.entity_description.attrs_fn(data, self.analyzer)
        except (TypeError, ValueError, ZeroDivisionError, AttributeError) as exc:
            _LOG.debug("%s: no attributes (%s)", self.entity_description.key, exc)
            return None
