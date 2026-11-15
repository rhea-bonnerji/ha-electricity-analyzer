"""Weather: what it was like, what it is going to be like, and what that costs.

NO HOME ASSISTANT IMPORTS. The caller passes an aiohttp session.

Ported from the previous dashboard's weather module, which was verified against the
live Open-Meteo service. The parts kept:

* SIXTEEN HOURLY VARIABLES from Open-Meteo's archive (no key, no account, CC BY 4.0),
  requested in UTC so they line up hour for hour with the clamps' UTC hours.
* HEATING DEGREE DAYS TWO WAYS: from the thermometer and from the apparent ("feels
  like") temperature, which folds in wind chill and humidity. Ireland's weather is
  defined by wind and damp, so the second often explains a house better; both are fitted
  and the better one is used, and the gap between them is what the wind costs.
* A LEAST-SQUARES FIT of daily kWh against degree days: kWh per degree day (how much the
  cold costs this house) on top of a weather-independent baseline. It is only quoted
  when it is worth quoting: enough days, and r² high enough that a straight line is
  defensible (a house heated by gas legitimately scores near zero, and saying so beats a
  meaningless slope).
* FETCH POLICY: the first run reads the whole span the electricity data covers, in
  year-sized chunks; afterwards only days not already held, plus a few days of overlap
  because Open-Meteo replaces its most recent (forecast-model) days with reanalysis a
  few days later. A restart with a warm cache makes no request at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import aiohttp

LOG = logging.getLogger(__name__)
UTC = timezone.utc

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=90)
DEFAULT_BASE_TEMP_C = 15.5     # UK/Ireland heating balance point
CHUNK_DAYS = 370
OVERLAP_DAYS = 5
FORECAST_DAYS = 16
# The archive (reanalysis) is several days behind, so the forecast call also returns the
# last week: today and the recent days the archive cannot answer yet are never a gap in
# the charts. Archive values replace these as soon as the archive has them.
FORECAST_PAST_DAYS = 7


@dataclass(frozen=True, slots=True)
class Variable:
    column: str
    api: str
    name: str
    unit: str
    kind: str          # mean | circular | total
    unit_class: str | None = None
    publish: bool = True


VARIABLES: tuple[Variable, ...] = (
    Variable("temperature", "temperature_2m", "Outdoor temperature", "°C", "mean", "temperature"),
    Variable("apparent_temperature", "apparent_temperature", "Apparent temperature", "°C", "mean", "temperature"),
    Variable("humidity", "relative_humidity_2m", "Relative humidity", "%", "mean"),
    Variable("dew_point", "dew_point_2m", "Dew point", "°C", "mean", "temperature"),
    Variable("wind_speed", "wind_speed_10m", "Wind speed", "km/h", "mean", "speed"),
    Variable("wind_gusts", "wind_gusts_10m", "Wind gusts", "km/h", "mean", "speed"),
    Variable("wind_direction", "wind_direction_10m", "Wind direction", "°", "circular", publish=False),
    Variable("precipitation", "precipitation", "Precipitation", "mm", "total"),
    Variable("rain", "rain", "Rain", "mm", "total", publish=False),
    Variable("snowfall", "snowfall", "Snowfall", "cm", "total", publish=False),
    Variable("snow_depth", "snow_depth", "Snow depth", "m", "mean", publish=False),
    Variable("cloud_cover", "cloud_cover", "Cloud cover", "%", "mean"),
    Variable("solar_radiation", "shortwave_radiation", "Solar radiation", "W/m²", "mean"),
    Variable("sunshine", "sunshine_duration", "Sunshine", "s", "total", publish=False),
    Variable("pressure", "surface_pressure", "Air pressure", "hPa", "mean", "pressure"),
    Variable("soil_temperature", "soil_temperature_0_to_7cm", "Soil temperature", "°C", "mean", "temperature", publish=False),
)
COLUMNS = tuple(v.column for v in VARIABLES)
BY_API = {v.api: v.column for v in VARIABLES}
# The forecast endpoint does not offer every archive variable.
FORECAST_APIS = (
    "temperature_2m", "apparent_temperature", "relative_humidity_2m", "dew_point_2m",
    "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m", "precipitation", "rain",
    "snowfall", "cloud_cover", "shortwave_radiation", "surface_pressure",
)


class WeatherUnavailable(Exception):
    """Open-Meteo could not be read. Never fatal: weather cards simply wait."""


@dataclass(slots=True)
class Observation:
    hour: datetime
    values: dict[str, float | None] = field(default_factory=dict)

    def get(self, column: str) -> float | None:
        return self.values.get(column)


async def _get(http: aiohttp.ClientSession, url: str, params: dict[str, str]) -> dict[str, Any]:
    try:
        async with http.get(url, params=params, timeout=REQUEST_TIMEOUT) as r:
            if r.status != 200:
                raise WeatherUnavailable(f"Open-Meteo answered {r.status}: {(await r.text())[:200]}")
            payload = await r.json()
    except WeatherUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - network, JSON, anything
        raise WeatherUnavailable(f"could not reach Open-Meteo: {exc}") from exc
    if payload.get("error"):
        raise WeatherUnavailable(str(payload.get("reason") or "unknown error"))
    return payload


def _parse(payload: dict[str, Any]) -> list[Observation]:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    series = {BY_API[api]: values for api, values in hourly.items() if api in BY_API}
    out = []
    for i, stamp in enumerate(times):
        try:
            when = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        values = {
            col: (None if i >= len(vals) or vals[i] is None else float(vals[i]))
            for col, vals in series.items()
        }
        out.append(Observation(when.astimezone(UTC), values))
    return out


async def fetch_archive(
    http: aiohttp.ClientSession, latitude: float, longitude: float, start: date, end: date
) -> tuple[list[Observation], int]:
    """Every variable, hourly, for [start, end]. Returns (observations, requests made)."""
    out: list[Observation] = []
    requests = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS - 1), end)
        payload = await _get(
            http,
            ARCHIVE_URL,
            {
                "latitude": f"{latitude:.4f}",
                "longitude": f"{longitude:.4f}",
                "start_date": cursor.isoformat(),
                "end_date": chunk_end.isoformat(),
                "hourly": ",".join(v.api for v in VARIABLES),
                "timezone": "UTC",
                "wind_speed_unit": "kmh",
                "precipitation_unit": "mm",
            },
        )
        requests += 1
        out.extend(_parse(payload))
        cursor = chunk_end + timedelta(days=1)
    return out, requests


async def fetch_forecast(
    http: aiohttp.ClientSession, latitude: float, longitude: float
) -> list[Observation]:
    """The next sixteen days, hourly, plus the past week (to cover the archive's lag)."""
    payload = await _get(
        http,
        FORECAST_URL,
        {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "hourly": ",".join(FORECAST_APIS),
            "forecast_days": str(FORECAST_DAYS),
            "past_days": str(FORECAST_PAST_DAYS),
            "timezone": "UTC",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
        },
    )
    return _parse(payload)


# ---------------------------------------------------------------- arithmetic


def degree_hours(rows: Iterable[dict[str, Any]], column: str, base: float) -> dict[datetime, float]:
    """Degree days accrued in each hour (1/24 of a degree day per degree below base)."""
    return {
        r["hour_utc"]: max(0.0, base - float(r[column])) / 24.0
        for r in rows
        if r.get(column) is not None
    }


def by_local_day(pairs: dict[datetime, float], tz: Any, *, average: bool = False, min_hours: int = 20) -> dict[date, float]:
    sums: dict[date, float] = {}
    counts: dict[date, int] = {}
    for when, value in pairs.items():
        d = when.astimezone(tz).date()
        sums[d] = sums.get(d, 0.0) + value
        counts[d] = counts.get(d, 0) + 1
    if average:
        return {d: sums[d] / counts[d] for d in sums if counts[d] >= min_hours}
    return {d: sums[d] for d in sums if counts[d] >= min_hours}


def ols(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-9:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx
    r2 = 0.0 if syy <= 1e-9 else max(0.0, min(1.0, sxy * sxy / (sxx * syy)))
    return slope, my - slope * mx, r2


@dataclass(slots=True)
class Fit:
    basis: str
    slope: float          # kWh per degree day
    baseline: float       # kWh per day regardless of weather
    r2: float
    days: int
    base_temp_c: float

    @property
    def reliable(self) -> bool:
        return self.days >= 30 and self.r2 >= 0.35 and self.slope > 0

    def predict(self, degree_days: float) -> float:
        return max(self.baseline + self.slope * degree_days, 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "basis": self.basis,
            "kwh_per_degree_day": round(self.slope, 3),
            "baseline_kwh": round(self.baseline, 2),
            "r2": round(self.r2, 3),
            "days": self.days,
            "base_temp_c": self.base_temp_c,
            "reliable": self.reliable,
        }


def fit(kwh: dict[date, float], hdd: dict[date, float], *, basis: str, base: float, min_days: int = 14) -> Fit | None:
    keys = sorted(set(kwh) & set(hdd))
    if len(keys) < min_days:
        return None
    result = ols([hdd[k] for k in keys], [kwh[k] for k in keys])
    if result is None:
        return None
    slope, intercept, r2 = result
    return Fit(basis, slope, intercept, r2, len(keys), base)


def best_fit(kwh: dict[date, float], hdd_dry: dict[date, float], hdd_apparent: dict[date, float], base: float) -> Fit | None:
    """The better-explaining of the thermometer and feels-like fits."""
    a = fit(kwh, hdd_dry, basis="thermometer", base=base)
    b = fit(kwh, hdd_apparent, basis="feels like", base=base)
    candidates = [f for f in (a, b) if f is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.r2)


def normal_hdd(history: dict[date, float]) -> dict[tuple[int, int], float]:
    """Average degree days for each calendar day, smoothed over ±7 days (climatology)."""
    by_doy: dict[tuple[int, int], list[float]] = {}
    for d, v in history.items():
        by_doy.setdefault((d.month, d.day), []).append(v)
    out = {}
    ref = date(2001, 1, 1)
    for i in range(365):
        day = ref + timedelta(days=i)
        window = []
        for k in range(-7, 8):
            other = day + timedelta(days=k)
            window.extend(by_doy.get((other.month, other.day), []))
        if window:
            out[(day.month, day.day)] = sum(window) / len(window)
    out.setdefault((2, 29), out.get((2, 28), 0.0))
    return out
