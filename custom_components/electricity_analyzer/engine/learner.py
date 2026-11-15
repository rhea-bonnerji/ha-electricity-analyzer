"""What drives this house's usage, as weights that add or subtract, relearned nightly.

NO HOME ASSISTANT IMPORTS.

    kWh(day) = base
             + w_cold    x degree days                (thermometer, base 15.5 °C)
             + w_chill   x extra "feels like" degree days (wind and damp on top)
             + w_sun     x solar radiation, per 100 W/m²   (bright days need less light/heat)
             + w_wind    x wind speed, per 10 km/h
             + w_rain    x rain, per 5 mm                  (people stay in)
             + w_mon..w_sat                               (each weekday vs Sunday)
             + w_sin1, w_cos1, w_sin2, w_cos2             (time of year beyond the weather)

Every weight is in kWh per day, so it reads directly: "each degree day ADDS 1.4 kWh",
"a sunny day SUBTRACTS 0.9 kWh", "Mondays add 0.8 kWh".

HOW IT LEARNS

Weighted ridge regression, refitted every night on the whole history with each day
weighted by recency (half-life 120 days): recent behaviour counts most, so the weights
drift with the house (a new aquarium heater raises w_cold; a changed routine moves the
weekday weights), while a small ridge penalty keeps a weight from swinging on a handful
of odd days. Each night's weights are written to the database, so how they have moved is
itself on the dashboard.

It forecasts with the coming weather where there is a forecast (blended across sources
by how well each has forecast here), and with the normal weather for that date beyond.
It is one model in the ensemble and earns its weight there like every other.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

HALF_LIFE_DAYS = 120.0
RIDGE = 1.0
MIN_DAYS = 28

# feature -> (label, unit description, scale applied to the raw value)
FEATURES: dict[str, tuple[str, str]] = {
    "base": ("Baseline", "kWh/day"),
    "cold": ("Each heating degree day", "kWh per °C·day"),
    "chill": ("Each extra feels-like degree day (wind/damp)", "kWh per °C·day"),
    "sun": ("Each 100 W/m² of sunshine", "kWh"),
    "wind": ("Each 10 km/h of wind", "kWh"),
    "rain": ("Each 5 mm of rain", "kWh"),
    "mon": ("Monday (vs Sunday)", "kWh"),
    "tue": ("Tuesday (vs Sunday)", "kWh"),
    "wed": ("Wednesday (vs Sunday)", "kWh"),
    "thu": ("Thursday (vs Sunday)", "kWh"),
    "fri": ("Friday (vs Sunday)", "kWh"),
    "sat": ("Saturday (vs Sunday)", "kWh"),
    "sin1": ("Time of year (annual, sine)", "kWh"),
    "cos1": ("Time of year (annual, cosine)", "kWh"),
    "sin2": ("Time of year (half-year, sine)", "kWh"),
    "cos2": ("Time of year (half-year, cosine)", "kWh"),
}
NAMES = list(FEATURES)
WEATHER_KEYS = ("hdd", "hdd_app", "rad", "wind", "rain")


@dataclass
class DayWeather:
    hdd: float
    hdd_app: float
    rad: float      # mean W/m²
    wind: float     # mean km/h
    rain: float     # total mm

    def as_dict(self) -> dict[str, float]:
        return {k: round(getattr(self, k), 3) for k in WEATHER_KEYS}


def features(day: date, w: DayWeather) -> list[float]:
    doy = day.timetuple().tm_yday / 365.25 * 2 * math.pi
    wd = day.weekday()  # Monday 0 ... Sunday 6 (baseline)
    return [
        1.0,
        w.hdd,
        max(w.hdd_app - w.hdd, 0.0),
        w.rad / 100.0,
        w.wind / 10.0,
        w.rain / 5.0,
        *(1.0 if wd == i else 0.0 for i in range(6)),
        math.sin(doy),
        math.cos(doy),
        math.sin(2 * doy),
        math.cos(2 * doy),
    ]


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                if f:
                    for k in range(col, n + 1):
                        m[r][k] -= f * m[col][k]
    return [m[i][n] / m[i][i] for i in range(n)]


@dataclass
class Regression:
    weights: dict[str, float] = field(default_factory=dict)
    days: int = 0
    r2: float | None = None
    mape: float | None = None
    fitted_on: date | None = None

    @property
    def ready(self) -> bool:
        return bool(self.weights) and self.days >= MIN_DAYS

    def predict(self, day: date, w: DayWeather) -> float:
        x = features(day, w)
        return max(sum(self.weights[n] * v for n, v in zip(NAMES, x)), 0.0)

    def contributions(self, day: date, w: DayWeather) -> dict[str, float]:
        x = features(day, w)
        out = {n: self.weights[n] * v for n, v in zip(NAMES, x)}
        season = sum(out.pop(k) for k in ("sin1", "cos1", "sin2", "cos2"))
        weekday = sum(out.pop(k) for k in ("mon", "tue", "wed", "thu", "fri", "sat"))
        out["time_of_year"] = season
        out["weekday"] = weekday
        return {k: round(v, 2) for k, v in out.items()}

    def as_dict(self) -> dict[str, Any]:
        return {
            "fitted_on": self.fitted_on.isoformat() if self.fitted_on else None,
            "days": self.days,
            "r2": None if self.r2 is None else round(self.r2, 3),
            "mape_pct": None if self.mape is None else round(self.mape * 100, 1),
            "half_life_days": HALF_LIFE_DAYS,
            "weights": [
                {
                    "feature": n,
                    "label": FEATURES[n][0],
                    "unit": FEATURES[n][1],
                    "weight": round(self.weights.get(n, 0.0), 3),
                    "effect": "adds" if self.weights.get(n, 0.0) >= 0 else "subtracts",
                }
                for n in NAMES
            ],
        }


def fit(kwh: dict[date, float], weather: dict[date, DayWeather], *, today: date) -> Regression:
    keys = [d for d in sorted(kwh) if d in weather and d < today]
    if len(keys) < MIN_DAYS:
        return Regression(days=len(keys))
    n = len(NAMES)
    xtx = [[0.0] * n for _ in range(n)]
    xty = [0.0] * n
    rows = []
    for d in keys:
        x = features(d, weather[d])
        y = kwh[d]
        w = 0.5 ** ((today - d).days / HALF_LIFE_DAYS)
        rows.append((x, y, w))
        for i in range(n):
            wx = w * x[i]
            xty[i] += wx * y
            for j in range(i, n):
                xtx[i][j] += wx * x[j]
    for i in range(n):
        for j in range(i):
            xtx[i][j] = xtx[j][i]
        if i:
            xtx[i][i] += RIDGE   # never penalise the baseline
    beta = _solve(xtx, xty)
    if beta is None:
        return Regression(days=len(keys))
    reg = Regression(weights=dict(zip(NAMES, beta)), days=len(keys), fitted_on=today)
    # Weighted r² and MAPE on the fitted days (in-sample; the ensemble scores it honestly).
    sw = sum(w for _, _, w in rows)
    mean = sum(w * y for _, y, w in rows) / sw
    ss_tot = sum(w * (y - mean) ** 2 for _, y, w in rows)
    ss_res = 0.0
    errs = []
    for x, y, w in rows:
        p = sum(b * v for b, v in zip(beta, x))
        ss_res += w * (y - p) ** 2
        if y > 0.1:
            errs.append(abs(p - y) / y)
    reg.r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
    reg.mape = sum(errs) / len(errs) if errs else None
    return reg


def daily_weather(rows: list[dict[str, Any]], tz: Any, base: float, *, min_hours: int = 20) -> dict[date, DayWeather]:
    """Weather rows (hourly) -> per local day features."""
    acc: dict[date, dict[str, float]] = {}
    for r in rows:
        if r.get("temperature") is None:
            continue
        d = r["hour_utc"].astimezone(tz).date()
        a = acc.setdefault(d, {"n": 0, "hdd": 0.0, "hdd_app": 0.0, "rad": 0.0, "wind": 0.0, "rain": 0.0})
        t = float(r["temperature"])
        ta = float(r["apparent_temperature"]) if r.get("apparent_temperature") is not None else t
        a["n"] += 1
        a["hdd"] += max(0.0, base - t) / 24
        a["hdd_app"] += max(0.0, base - ta) / 24
        a["rad"] += float(r.get("solar_radiation") or 0.0)
        a["wind"] += float(r.get("wind_speed") or 0.0)
        a["rain"] += float(r.get("precipitation") or 0.0)
    out = {}
    for d, a in acc.items():
        n = a["n"]
        if n < min_hours:
            continue
        scale = 24 / n  # a 23- or 25-hour day still counts as one day
        out[d] = DayWeather(a["hdd"] * scale, a["hdd_app"] * scale, a["rad"] / n, a["wind"] / n, a["rain"] * scale)
    return out


def normals(history: dict[date, DayWeather]) -> dict[tuple[int, int], DayWeather]:
    """Climatology: each calendar day's average weather, smoothed over ±7 days."""
    by_doy: dict[tuple[int, int], list[DayWeather]] = {}
    for d, w in history.items():
        by_doy.setdefault((d.month, d.day), []).append(w)
    out = {}
    ref = date(2001, 1, 1)
    for i in range(365):
        day = ref + timedelta(days=i)
        window: list[DayWeather] = []
        for k in range(-7, 8):
            other = day + timedelta(days=k)
            window.extend(by_doy.get((other.month, other.day), []))
        if window:
            out[(day.month, day.day)] = DayWeather(
                *(sum(getattr(w, key) for w in window) / len(window) for key in WEATHER_KEYS)
            )
    if (2, 28) in out:
        out.setdefault((2, 29), out[(2, 28)])
    return out


# ------------------------------------------------- blending forecast sources


def source_weights(log: list[dict[str, Any]], observed: dict[date, float], today: date) -> dict[str, float]:
    """Weights for degree-day forecast sources from how well each has forecast here.

    `log` rows {"made_on", "target", "model": "hdd:<source>", "kwh": <predicted degree days>}.
    Inverse recency-weighted MSE, like the ensemble; equal weights until scored.
    """
    num: dict[str, float] = {}
    den: dict[str, float] = {}
    for r in log:
        if not str(r["model"]).startswith("hdd:"):
            continue
        t = r["target"]
        if t >= today or t not in observed or (today - t).days > 60:
            continue
        lead = (t - r["made_on"]).days
        if lead < 1 or lead > 5:
            continue
        w = 0.5 ** ((today - t).days / 21.0)
        src = r["model"][4:]
        num[src] = num.get(src, 0.0) + w * (r["kwh"] - observed[t]) ** 2
        den[src] = den.get(src, 0.0) + w
    if not den:
        return {}
    raw = {s: 1.0 / (num[s] / den[s] + 0.05) for s in den}
    total = sum(raw.values())
    return {s: v / total for s, v in raw.items()}
