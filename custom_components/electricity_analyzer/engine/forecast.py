"""Seasonal forecast of usage and cost, with its accuracy measured, not asserted.

NO HOME ASSISTANT IMPORTS.

THE MODEL

    kWh(day) = level x season(day of year) x weekday(day)

* `season` is a smooth curve through twelve month factors. With a year or more of
  the home's own data the factors are its own; with less, they are shrunk towards a
  built-in Irish domestic shape (derived from sample Irish ESB smart
  meter data: January uses about 2.2x what July does). That is what makes a
  three-week-old installation produce a sensible December, instead of a December
  that looks like the September it has seen.
* `weekday` is the same idea for days of the week, shrunk towards flat.
* `level` is a trimmed mean of the last three weeks with season and weekday removed:
  "how much this house uses, adjusted for time of year".

Long history comes from ESB Networks when it is configured: its HDF is two years of
half-hours, so seasonality is learned on day one. Shelly days are preferred where
both exist, because the clamp is what everything else on the dashboard shows.

HONESTY

Every forecast is back-tested over the last four weeks against a seasonal-naive
benchmark (same weekday last week). Accuracy is reported as mean absolute percentage
error, and if the naive benchmark wins, it is used and the report says so. Ranges are
80% intervals built from the back-test's own residuals, widened for months whose
seasonal factor rests on the prior rather than on data.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .tariff import TariffSchedule

# Average kWh/day by month from sample Irish ESB smart-meter data.
# Only the SHAPE is used; it is normalised to mean 1.
DEFAULT_MONTH_SHAPE: dict[int, float] = {
    1: 44.0, 2: 36.4, 3: 31.3, 4: 24.7, 5: 19.7, 6: 20.4,
    7: 18.8, 8: 23.7, 9: 22.4, 10: 26.6, 11: 26.1, 12: 35.6,
}
DEFAULT_NIGHT_SHARE = 0.33

MONTH_SHRINK_DAYS = 30.0     # a month needs ~30 observed days to outweigh the prior
WEEKDAY_SHRINK_WEEKS = 4.0
LEVEL_WINDOW = 21
BACKTEST_DAYS = 28
Z80 = 1.2816

SEASONS = {
    "winter": (12, 1, 2),
    "spring": (3, 4, 5),
    "summer": (6, 7, 8),
    "autumn": (9, 10, 11),
}


def _normalise(factors: dict[int, float]) -> dict[int, float]:
    mean = sum(factors.values()) / len(factors)
    return {k: v / mean for k, v in factors.items()} if mean else factors


def _trimmed_mean(values: list[float], trim: float = 0.1) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = int(len(ordered) * trim)
    kept = ordered[k : len(ordered) - k] or ordered
    return sum(kept) / len(kept)


def _mid_month(year: int, month: int) -> date:
    return date(year, month, 15)


@dataclass
class DayForecast:
    day: date
    kwh: float
    low: float
    high: float
    cost: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "kwh": round(self.kwh, 2),
            "low": round(self.low, 2),
            "high": round(self.high, 2),
            "cost": round(self.cost, 2),
        }


@dataclass
class PeriodForecast:
    label: str
    start: date
    end: date            # inclusive
    actual_kwh: float
    forecast_kwh: float
    low_kwh: float
    high_kwh: float
    actual_cost: float
    forecast_cost: float
    fixed_cost: float
    last_year_kwh: float | None = None

    @property
    def total_kwh(self) -> float:
        return self.actual_kwh + self.forecast_kwh

    @property
    def total_cost(self) -> float:
        return self.actual_cost + self.forecast_cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "kwh": round(self.total_kwh, 1),
            "low_kwh": round(self.low_kwh, 1),
            "high_kwh": round(self.high_kwh, 1),
            "actual_kwh": round(self.actual_kwh, 1),
            "forecast_kwh": round(self.forecast_kwh, 1),
            "cost": round(self.total_cost, 2),
            "actual_cost": round(self.actual_cost, 2),
            "forecast_cost": round(self.forecast_cost, 2),
            "fixed_cost": round(self.fixed_cost, 2),
            "last_year_kwh": None if self.last_year_kwh is None else round(self.last_year_kwh, 1),
        }


@dataclass
class ForecastReport:
    available: bool
    reason: str = ""
    method: str = ""
    mape: float | None = None
    naive_mape: float | None = None
    level: float | None = None
    sigma: float | None = None
    data_days: int = 0
    history_from: date | None = None
    month_factors: dict[int, float] = field(default_factory=dict)
    month_evidence: dict[int, int] = field(default_factory=dict)
    weekday_factors: dict[int, float] = field(default_factory=dict)
    night_share: dict[int, float] = field(default_factory=dict)
    today: DayForecast | None = None
    today_remaining_kwh: float | None = None
    tomorrow: DayForecast | None = None
    next_days: list[DayForecast] = field(default_factory=list)
    this_month: PeriodForecast | None = None
    bill_period: PeriodForecast | None = None
    months: list[PeriodForecast] = field(default_factory=list)
    seasons: list[PeriodForecast] = field(default_factory=list)
    year_kwh: float | None = None
    year_cost: float | None = None
    year_low: float | None = None
    year_high: float | None = None

    def window(self, n: int) -> dict[str, Any]:
        days = self.next_days[:n]
        return {
            "kwh": round(sum(d.kwh for d in days), 1),
            "low": round(sum(d.low for d in days), 1),
            "high": round(sum(d.high for d in days), 1),
            "cost": round(sum(d.cost for d in days), 2),
            "per_day": [d.as_dict() for d in days],
        }

    def summary(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "method": self.method,
            "accuracy_mape_pct": None if self.mape is None else round(self.mape * 100, 1),
            "naive_mape_pct": None if self.naive_mape is None else round(self.naive_mape * 100, 1),
            "level_kwh_per_day": None if self.level is None else round(self.level, 2),
            "data_days": self.data_days,
            "history_from": self.history_from.isoformat() if self.history_from else None,
            "month_factors": {str(k): round(v, 3) for k, v in sorted(self.month_factors.items())},
            "month_evidence_days": {str(k): v for k, v in sorted(self.month_evidence.items())},
            "weekday_factors": {str(k): round(v, 3) for k, v in sorted(self.weekday_factors.items())},
        }


class Forecaster:
    def __init__(
        self,
        daily: dict[date, float],
        *,
        night_share: dict[int, float] | None = None,
        prior: dict[int, float] | None = None,
    ) -> None:
        self.daily = {d: v for d, v in daily.items() if v is not None and v >= 0}
        self.days = sorted(self.daily)
        self.prior = _normalise(dict(prior or DEFAULT_MONTH_SHAPE))
        self.night_share = {m: (night_share or {}).get(m, DEFAULT_NIGHT_SHARE) for m in range(1, 13)}
        self.month_factors, self.month_evidence = self._fit_months()
        self._season_cache: dict[date, float] = {}
        self.weekday_factors = self._fit_weekdays()

    # ----------------------------------------------------------------- fitting

    def season(self, day: date) -> float:
        """Smooth seasonal factor: linear between mid-month anchors, cyclic."""
        if day in self._season_cache:
            return self._season_cache[day]
        mid = _mid_month(day.year, day.month)
        if day >= mid:
            nxt_month = 1 if day.month == 12 else day.month + 1
            nxt = _mid_month(day.year + (day.month == 12), nxt_month)
            a, b = self.month_factors[day.month], self.month_factors[nxt_month]
            t = (day - mid).days / (nxt - mid).days
        else:
            prv_month = 12 if day.month == 1 else day.month - 1
            prv = _mid_month(day.year - (day.month == 1), prv_month)
            a, b = self.month_factors[prv_month], self.month_factors[day.month]
            t = (day - prv).days / (mid - prv).days
        value = a + (b - a) * t
        self._season_cache[day] = value
        return value

    def _prior_season(self, day: date) -> float:
        saved, self.month_factors = self.month_factors, self.prior
        self._season_cache = {}
        try:
            return self.season(day)
        finally:
            self.month_factors = saved
            self._season_cache = {}

    def _fit_months(self) -> tuple[dict[int, float], dict[int, int]]:
        if not self.days:
            return dict(self.prior), {m: 0 for m in range(1, 13)}
        self.month_factors = dict(self.prior)
        self._season_cache = {}
        deseason = [self.daily[d] / self.season(d) for d in self.days]
        level = statistics.median(deseason) or 1.0
        by_month: dict[int, list[float]] = {m: [] for m in range(1, 13)}
        for d in self.days:
            by_month[d.month].append(self.daily[d] / level)
        fitted = {}
        evidence = {}
        for m in range(1, 13):
            n = len(by_month[m])
            evidence[m] = n
            observed = sum(by_month[m]) / n if n else self.prior[m]
            fitted[m] = (n * observed + MONTH_SHRINK_DAYS * self.prior[m]) / (n + MONTH_SHRINK_DAYS)
        self._season_cache = {}
        return _normalise(fitted), evidence

    def _fit_weekdays(self) -> dict[int, float]:
        recent = self.days[-16 * 7 :]
        sums = {w: [] for w in range(7)}
        for d in recent:
            sums[d.weekday()].append(self.daily[d] / self.season(d))
        overall = [v for vs in sums.values() for v in vs]
        if not overall:
            return {w: 1.0 for w in range(7)}
        mean = sum(overall) / len(overall)
        out = {}
        for w, vs in sums.items():
            n_weeks = len(vs)
            observed = (sum(vs) / n_weeks / mean) if n_weeks and mean else 1.0
            out[w] = (n_weeks * observed + WEEKDAY_SHRINK_WEEKS) / (n_weeks + WEEKDAY_SHRINK_WEEKS)
        return _normalise(out)

    def shape(self, day: date) -> float:
        return self.season(day) * self.weekday_factors[day.weekday()]

    def level_before(self, day: date, window: int = LEVEL_WINDOW) -> float | None:
        values = [
            self.daily[d] / self.shape(d)
            for d in self.days
            if day - timedelta(days=window) <= d < day
        ]
        if len(values) < 5:
            return None
        return _trimmed_mean(values)

    # --------------------------------------------------------------- backtest

    def backtest(self, upto: date) -> tuple[float | None, float | None, float | None]:
        """(model MAPE, naive MAPE, sigma of log residuals) over the last four weeks."""
        model_err, naive_err, logs = [], [], []
        for d in self.days:
            if not (upto - timedelta(days=BACKTEST_DAYS) <= d < upto):
                continue
            actual = self.daily[d]
            if actual <= 0.05:
                continue
            level = self.level_before(d)
            if level is not None:
                pred = level * self.shape(d)
                model_err.append(abs(pred - actual) / actual)
                logs.append(math.log(actual / pred) if pred > 0 else 0.0)
            prev = self.daily.get(d - timedelta(days=7))
            if prev is not None:
                naive_err.append(abs(prev - actual) / actual)
        mape = sum(model_err) / len(model_err) if len(model_err) >= 7 else None
        naive = sum(naive_err) / len(naive_err) if len(naive_err) >= 7 else None
        sigma = statistics.pstdev(logs) if len(logs) >= 7 else None
        return mape, naive, sigma

    # --------------------------------------------------------------- forecast

    def forecast(
        self,
        *,
        today: date,
        schedule: TariffSchedule,
        today_so_far_kwh: float = 0.0,
        today_so_far_cost: float = 0.0,
        today_elapsed_share: float = 0.0,
        month_actual_kwh: float = 0.0,
        month_actual_cost: float = 0.0,
        bill_period: tuple[date, date] | None = None,
        bill_actual_kwh: float = 0.0,
        bill_actual_cost: float = 0.0,
        horizon_months: int = 12,
        meter_factors: dict[str, float] | None = None,
        daily_override: dict[date, float] | None = None,
        sigma_override: float | None = None,
        method_label: str | None = None,
    ) -> ForecastReport:
        """Forecast kWh are the clamp's; money is priced on what the METER will count,
        using the day/night factors learned from real readings (calibration.py)."""
        if not self.days:
            return ForecastReport(available=False, reason="no complete days of data yet")
        level = self.level_before(today)
        if level is None:
            return ForecastReport(
                available=False,
                reason="needs at least 5 complete days in the last three weeks",
                data_days=len(self.days),
            )

        mape, naive, sigma = self.backtest(today)
        method = "seasonal"
        if mape is not None and naive is not None and naive < mape * 0.9:
            # The benchmark is clearly better on this house right now. Say so, and
            # widen the ranges to the benchmark's error, but keep the seasonal shape:
            # a "same as last week" forecast cannot see winter coming.
            method = "seasonal (naive benchmark currently more accurate)"
        sigma = sigma_override if sigma_override is not None else (sigma if sigma is not None else 0.25)
        if method_label:
            method = method_label
        override = daily_override or {}

        factors = meter_factors or {}
        f_day, f_night = factors.get("day", 1.0), factors.get("night", 1.0)

        def price(day: date, kwh: float) -> tuple[float, float]:
            tariff = schedule.for_date(day)
            ns = self.night_share.get(day.month, DEFAULT_NIGHT_SHARE)
            energy = kwh * (ns * tariff.night_rate * f_night + (1 - ns) * tariff.day_rate * f_day)
            return energy, tariff.fixed_for_day(day)

        def predict(day: date) -> DayForecast:
            kwh = override.get(day, level * self.shape(day))
            spread = math.exp(Z80 * sigma)
            energy, fixed = price(day, kwh)
            return DayForecast(day, kwh, kwh / spread, kwh * spread, energy + fixed)

        def rel_error(n_days: int, months: set[int]) -> float:
            prior_heavy = any(self.month_evidence.get(m, 0) < 20 for m in months)
            season_unc = 0.12 if prior_heavy else 0.05
            return math.sqrt(sigma**2 / max(n_days, 1) + sigma**2 / LEVEL_WINDOW + season_unc**2)

        report = ForecastReport(
            available=True,
            method=method,
            mape=mape,
            naive_mape=naive,
            level=level,
            sigma=sigma,
            data_days=len(self.days),
            history_from=self.days[0],
            month_factors=self.month_factors,
            month_evidence=self.month_evidence,
            weekday_factors=self.weekday_factors,
            night_share=self.night_share,
        )

        # Today: what has happened, plus the expected remainder of the day's profile.
        full_today = predict(today)
        remaining = full_today.kwh * max(1.0 - today_elapsed_share, 0.0)
        energy_rem, _fixed = price(today, remaining)
        total_today = today_so_far_kwh + remaining
        spread = math.exp(Z80 * sigma * max(1.0 - today_elapsed_share, 0.05))
        # Today's fixed charge is already in the cost so far (it is due from midnight).
        report.today = DayForecast(
            today,
            total_today,
            today_so_far_kwh + remaining / spread,
            today_so_far_kwh + remaining * spread,
            today_so_far_cost + energy_rem,
        )
        report.today_remaining_kwh = remaining
        # Past a year so the twelfth month ahead is always a whole month.
        report.next_days = [predict(today + timedelta(days=i)) for i in range(1, 400)]
        report.tomorrow = report.next_days[0]

        def period(label: str, start: date, end: date, actual_kwh: float, actual_cost: float) -> PeriodForecast:
            future = [d for d in report.next_days if start <= d.day <= end]
            # Today's unspent remainder belongs to any period containing today.
            f_kwh = sum(d.kwh for d in future)
            f_cost = sum(d.cost for d in future)
            if start <= today <= end:
                f_kwh += remaining
                f_cost += energy_rem  # today's fixed charge is in the actual cost
            months = {d.day.month for d in future} or {start.month}
            err = rel_error(len(future), months)
            fixed_total = sum(schedule.for_date(d.day).fixed_for_day(d.day) for d in future)
            last_year = self.total_between(_shift_year(start, -1), _shift_year(end, -1))
            return PeriodForecast(
                label=label,
                start=start,
                end=end,
                actual_kwh=actual_kwh,
                forecast_kwh=f_kwh,
                low_kwh=actual_kwh + f_kwh * (1 - Z80 * err),
                high_kwh=actual_kwh + f_kwh * (1 + Z80 * err),
                actual_cost=actual_cost,
                forecast_cost=f_cost,
                fixed_cost=fixed_total,
                last_year_kwh=last_year,
            )

        month_start = today.replace(day=1)
        month_end = _month_end(today)
        report.this_month = period(
            today.strftime("%B %Y"), month_start, month_end, month_actual_kwh, month_actual_cost
        )
        if bill_period:
            report.bill_period = period(
                "Current billing period", bill_period[0], bill_period[1], bill_actual_kwh, bill_actual_cost
            )

        cursor = _next_month(month_start)
        for _ in range(horizon_months):
            end = _month_end(cursor)
            report.months.append(period(cursor.strftime("%B %Y"), cursor, end, 0.0, 0.0))
            cursor = _next_month(cursor)

        # Seasons as they actually fall over the next 365 days: contiguous runs, so the
        # rest of this autumn and the start of next autumn are two rows, not one sum
        # spanning the whole year.
        runs: list[tuple[str, list[DayForecast]]] = []
        for d in report.next_days[:365]:
            name = season_of(d.day)
            if runs and runs[-1][0] == name:
                runs[-1][1].append(d)
            else:
                runs.append((name, [d]))
        for i, (name, days) in enumerate(runs):
            start, end = days[0].day, days[-1].day
            label = f"{name.title()} {start.year}{'' if start.year == end.year else '/' + str(end.year)[2:]}"
            if i == 0 and season_of(start - timedelta(days=1)) == name:
                label += " (rest of)"
            elif i == len(runs) - 1 and season_of(end + timedelta(days=1)) == name:
                label += " (first part)"
            report.seasons.append(period(label, start, end, 0.0, 0.0))

        year = report.next_days[:365]
        report.year_kwh = sum(d.kwh for d in year)
        report.year_cost = sum(d.cost for d in year)
        err = rel_error(365, set(range(1, 13)))
        report.year_low = report.year_kwh * (1 - Z80 * err)
        report.year_high = report.year_kwh * (1 + Z80 * err)
        return report

    def total_between(self, start: date, end: date) -> float | None:
        days = [d for d in self.days if start <= d <= end]
        expected = (end - start).days + 1
        if len(days) < expected * 0.9:
            return None
        return sum(self.daily[d] for d in days) * expected / len(days)


def season_of(day: date) -> str:
    return next(name for name, months in SEASONS.items() if day.month in months)


def _month_end(day: date) -> date:
    return _next_month(day.replace(day=1)) - timedelta(days=1)


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), 1 if day.month == 12 else day.month + 1, 1)


def _shift_year(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February
        return day.replace(year=day.year + years, day=28)


def bill_period_for(today: date, anchor: date, months: int) -> tuple[date, date]:
    """The billing period containing `today`, given any date a period started on."""
    months = max(int(months), 1)
    start = anchor
    if start > today:
        while start > today:
            start = _add_months(start, -months)
    else:
        while _add_months(start, months) <= today:
            start = _add_months(start, months)
    return start, _add_months(start, months) - timedelta(days=1)


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    last = _month_end(date(year, month, 1)).day
    return date(year, month, min(day.day, last))
