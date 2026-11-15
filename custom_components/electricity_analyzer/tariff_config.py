"""Config entry <-> Tariff. The form works in cent/kWh; the engine in €/kWh."""

from __future__ import annotations

from datetime import date
from typing import Any

from .const import (
    CONF_CLOCK,
    CONF_DAY_RATE,
    CONF_NIGHT_END,
    CONF_NIGHT_RATE,
    CONF_NIGHT_START,
    CONF_PLAN,
    CONF_PSO,
    CONF_STANDING,
    CONF_SUPPLIER,
    CONF_VALID_FROM,
)
from .engine.tariff import DEFAULT_TARIFF, NightWindow, Tariff


def tariff_defaults() -> dict[str, Any]:
    t = DEFAULT_TARIFF
    return {
        CONF_SUPPLIER: t.supplier,
        CONF_PLAN: t.plan,
        CONF_DAY_RATE: round(t.day_rate * 100, 4),
        CONF_NIGHT_RATE: round(t.night_rate * 100, 4),
        CONF_STANDING: t.standing_per_year,
        CONF_PSO: t.pso_per_year,
        CONF_NIGHT_START: t.window.start_hour,
        CONF_NIGHT_END: t.window.end_hour,
        CONF_CLOCK: t.window.clock,
        CONF_VALID_FROM: t.valid_from.isoformat(),
    }


def tariff_from_config(cfg: dict[str, Any]) -> Tariff:
    d = {**tariff_defaults(), **{k: v for k, v in cfg.items() if v is not None}}
    return Tariff(
        valid_from=date.fromisoformat(str(d[CONF_VALID_FROM])[:10]),
        supplier=str(d[CONF_SUPPLIER]),
        plan=str(d[CONF_PLAN]),
        day_rate=round(float(d[CONF_DAY_RATE]) / 100, 6),
        night_rate=round(float(d[CONF_NIGHT_RATE]) / 100, 6),
        standing_per_year=float(d[CONF_STANDING]),
        pso_per_year=float(d[CONF_PSO]),
        window=NightWindow(int(d[CONF_NIGHT_START]), int(d[CONF_NIGHT_END]), str(d[CONF_CLOCK])),
    )
