"""A forecast made of weighted models, with the weights tuned by what actually happened.

NO HOME ASSISTANT IMPORTS.

THE MODELS (each predicts one day's kWh)

  seasonal   level x season x weekday (forecast.py): knows that January is not July.
  weather    baseline + kWh-per-degree-day x the day's degree days: the coming
             weather for the next sixteen days, the normal weather for that date
             beyond. Knows a cold snap is coming; seasonal cannot.
  last_week  the same weekday last week. Hard to beat when habits dominate.
  recent     the last seven days' average, shaped by weekday.
  last_year  the same weekday a year ago, scaled by how this year's level compares.
  drivers    the learned regression (learner.py): baseline + cold + wind/damp + sun +
             wind + rain + weekday + time of year, each a weight relearned nightly.

HOW THE WEIGHTS ARE TUNED

Every night each model's predictions for the next seven days are written to the
forecast log in the database. When a day completes, every prediction made for it at
the start of that day is scored against what the clamp measured. A model's weight is

    1 / (recency-weighted mean squared percentage error + floor)

with a 21-day half-life, so the engine follows changes in the house (a new heater, a
change in routine) within weeks, and a model that is wrong stops being listened to.
Weights are renormalised over whichever models can predict a given day (last_year
needs a year of history; weather needs a fitted relationship).

A BIAS CORRECTION is learned the same way, from the ensemble's own scored days, and
applied on top: if the ensemble has been running 4% low for three weeks, the next
forecast is raised 4%.

On a new installation the log is empty, so it is BOOTSTRAPPED: the last sixty days are
back-tested exactly as if the engine had been running, and those scores seed the
weights. They are replaced by real scores as days pass.

Money is not a model: kWh are priced per day by forecast.py using the night share for
the month and the meter factors learned from real readings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .forecast import Forecaster
from .learner import DayWeather, Regression
from .weather import Fit

MODELS = ("seasonal", "weather", "drivers", "last_week", "recent", "last_year")
ENSEMBLE = "ensemble"
HALF_LIFE_DAYS = 21.0
SCORE_WINDOW_DAYS = 90
MIN_SCORED = 5
ERROR_FLOOR = 0.0025         # (5%)²: no model is ever trusted infinitely
BIAS_LIMITS = (0.85, 1.15)
BOOTSTRAP_DAYS = 60
LOG_HORIZON = 7


@dataclass
class Context:
    daily: dict[date, float]                       # complete days, clamp kWh
    hdd: dict[date, float] = field(default_factory=dict)          # observed or forecast
    normal: dict[tuple[int, int], float] = field(default_factory=dict)
    fit: Fit | None = None
    seasonal: Forecaster | None = None
    regression: Regression | None = None
    weather_days: dict[date, DayWeather] = field(default_factory=dict)
    weather_normals: dict[tuple[int, int], DayWeather] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.seasonal is None:
            self.seasonal = Forecaster(self.daily)
        self.days = sorted(self.daily)

    def _before(self, made_on: date, n: int) -> list[float]:
        return [self.daily[d] for d in self.days if made_on - timedelta(days=n) <= d < made_on]

    def degree_days(self, target: date) -> float | None:
        if target in self.hdd:
            return self.hdd[target]
        return self.normal.get((target.month, target.day))

    def predict(self, model: str, made_on: date, target: date) -> float | None:
        """What `model` would say about `target`, knowing only days before `made_on`."""
        s = self.seasonal
        if model == "seasonal":
            level = s.level_before(made_on)
            return None if level is None else level * s.shape(target)
        if model == "weather":
            if self.fit is None or not self.fit.reliable:
                return None
            dd = self.degree_days(target)
            return None if dd is None else self.fit.predict(dd)
        if model == "drivers":
            if self.regression is None or not self.regression.ready:
                return None
            w = self.weather_days.get(target) or self.weather_normals.get((target.month, target.day))
            return None if w is None else self.regression.predict(target, w)
        if model == "last_week":
            ref = target - timedelta(days=7)
            while ref >= made_on:
                ref -= timedelta(days=7)
            return self.daily.get(ref)
        if model == "recent":
            week = self._before(made_on, 7)
            if len(week) < 5:
                return None
            wf = s.weekday_factors
            mean_wf = sum(wf.values()) / 7
            return (sum(week) / len(week)) * wf[target.weekday()] / mean_wf
        if model == "last_year":
            ref = target - timedelta(days=364)
            then = self.daily.get(ref)
            if then is None:
                return None
            now_level = self._before(made_on, 28)
            then_level = [
                self.daily[d] for d in self.days
                if made_on - timedelta(days=364 + 28) <= d < made_on - timedelta(days=364)
            ]
            if len(now_level) < 20 or len(then_level) < 20:
                return None
            return then * (sum(now_level) / len(now_level)) / (sum(then_level) / len(then_level))
        raise ValueError(model)

    def all_models(self, made_on: date, target: date) -> dict[str, float]:
        out = {}
        for m in MODELS:
            v = self.predict(m, made_on, target)
            if v is not None and v >= 0 and math.isfinite(v):
                out[m] = v
        return out


@dataclass
class Weights:
    weights: dict[str, float]
    errors: dict[str, float | None]
    scored: dict[str, int]
    bias: float = 1.0
    bootstrapped: bool = False

    def combine(self, predictions: dict[str, float]) -> float | None:
        usable = {m: p for m, p in predictions.items() if self.weights.get(m, 0) > 0}
        if not usable:
            return None
        total = sum(self.weights[m] for m in usable)
        return sum(self.weights[m] * p for m, p in usable.items()) / total * self.bias

    def as_dict(self) -> dict[str, Any]:
        return {
            "weights": {
                m: {
                    "weight": round(self.weights.get(m, 0.0), 4),
                    "error_pct": None if self.errors.get(m) is None else round(self.errors[m] * 100, 1),
                    "scored_days": self.scored.get(m, 0),
                }
                for m in MODELS
            },
            "bias": round(self.bias, 4),
            "half_life_days": HALF_LIFE_DAYS,
            "bootstrapped": self.bootstrapped,
        }


def tune(log: list[dict[str, Any]], actual: dict[date, float], *, today: date) -> Weights:
    """Weights from the log's start-of-day predictions scored against actual days.

    `log` rows: {"made_on": date, "target": date, "model": str, "kwh": float}.
    """
    num: dict[str, float] = {}
    den: dict[str, float] = {}
    count: dict[str, int] = {}
    bias_num = bias_den = 0.0
    for row in log:
        target = row["target"]
        if row["made_on"] != target or target >= today or target not in actual:
            continue
        age = (today - target).days
        if age > SCORE_WINDOW_DAYS:
            continue
        a = actual[target]
        if a <= 0.1:
            continue
        w = 0.5 ** (age / HALF_LIFE_DAYS)
        model = row["model"]
        if model == ENSEMBLE:
            bias_num += w * a
            bias_den += w * row["kwh"]
            continue
        err = (row["kwh"] - a) / a
        num[model] = num.get(model, 0.0) + w * err * err
        den[model] = den.get(model, 0.0) + w
        count[model] = count.get(model, 0) + 1

    errors: dict[str, float | None] = {}
    raw: dict[str, float] = {}
    for m in MODELS:
        if count.get(m, 0) >= MIN_SCORED and den.get(m):
            mse = num[m] / den[m]
            errors[m] = math.sqrt(mse)
            raw[m] = 1.0 / (mse + ERROR_FLOOR)
        else:
            errors[m] = None
    if raw:
        total = sum(raw.values())
        weights = {m: raw.get(m, 0.0) / total for m in MODELS}
    else:
        # Nothing scored yet: lean on the seasonal model, give the others a voice.
        weights = {"seasonal": 0.3, "weather": 0.15, "drivers": 0.2, "last_week": 0.1, "recent": 0.15, "last_year": 0.1}
    bias = bias_num / bias_den if bias_den > 0 else 1.0
    bias = min(max(bias, BIAS_LIMITS[0]), BIAS_LIMITS[1])
    return Weights(weights=weights, errors=errors, scored=count, bias=bias)


def log_rows(ctx: Context, weights: Weights | None, made_on: date, horizon: int = LOG_HORIZON) -> list[dict[str, Any]]:
    """What to write to the forecast log tonight: every model, and the ensemble."""
    rows = []
    for i in range(horizon):
        target = made_on + timedelta(days=i)
        preds = ctx.all_models(made_on, target)
        for m, v in preds.items():
            rows.append({"made_on": made_on, "target": target, "model": m, "kwh": v})
        if weights is not None:
            combined = weights.combine(preds)
            if combined is not None:
                rows.append({"made_on": made_on, "target": target, "model": ENSEMBLE, "kwh": combined})
    return rows


def bootstrap(ctx: Context, today: date, days: int = BOOTSTRAP_DAYS) -> list[dict[str, Any]]:
    """Back-test the last `days` days as if the engine had run each morning."""
    rows: list[dict[str, Any]] = []
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        if d not in ctx.daily:
            continue
        for m, v in ctx.all_models(d, d).items():
            rows.append({"made_on": d, "target": d, "model": m, "kwh": v})
    # Score the ensemble too, with weights tuned only on what came before each day.
    out = list(rows)
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        if d not in ctx.daily:
            continue
        w = tune([r for r in rows if r["target"] < d], ctx.daily, today=d)
        preds = {r["model"]: r["kwh"] for r in rows if r["target"] == d}
        combined = w.combine(preds)
        if combined is not None:
            out.append({"made_on": d, "target": d, "model": ENSEMBLE, "kwh": combined})
    return out


def forecast_days(ctx: Context, weights: Weights, today: date, horizon: int = 400) -> dict[date, float]:
    """The ensemble's daily kWh for today and the year ahead."""
    out = {}
    for i in range(horizon):
        target = today + timedelta(days=i)
        combined = weights.combine(ctx.all_models(today, target))
        if combined is not None:
            out[target] = combined
    return out


def recent_scores(log: list[dict[str, Any]], actual: dict[date, float], today: date, days: int = 30) -> list[dict[str, Any]]:
    """Start-of-day ensemble forecast vs actual, for the dashboard's accuracy chart."""
    out = []
    for row in sorted(log, key=lambda r: r["target"]):
        t = row["target"]
        if row["model"] != ENSEMBLE or row["made_on"] != t or t >= today or (today - t).days > days:
            continue
        a = actual.get(t)
        out.append({
            "date": t.isoformat(),
            "forecast": round(row["kwh"], 2),
            "actual": None if a is None else round(a, 2),
            "error_pct": None if not a else round((row["kwh"] - a) / a * 100, 1),
        })
    return out


def residual_sigma(log: list[dict[str, Any]], actual: dict[date, float], today: date) -> float | None:
    logs = [
        math.log(actual[r["target"]] / r["kwh"])
        for r in log
        if r["model"] == ENSEMBLE and r["made_on"] == r["target"] and r["target"] < today
        and actual.get(r["target"], 0) > 0.1 and r["kwh"] > 0.1 and (today - r["target"]).days <= SCORE_WINDOW_DAYS
    ]
    if len(logs) < 10:
        return None
    mean = sum(logs) / len(logs)
    return math.sqrt(sum((x - mean) ** 2 for x in logs) / len(logs))
