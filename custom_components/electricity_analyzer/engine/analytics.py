"""Summaries over the hourly store: totals, profiles, the 24/7 load, comparisons.

NO HOME ASSISTANT IMPORTS. Pure functions over store rows (dicts with `hour_utc`
as an aware datetime), so every number on the dashboard can be checked offline.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from .tariff import BAND_NIGHT, IRELAND, local_midnight


def _split_for(schedule: Any, tariff_from: Any, fixed: float) -> tuple[float, float] | None:
    """(standing, PSO) parts of an hour's fixed charges, from the tariff that priced it."""
    if schedule is None or not tariff_from:
        return None
    try:
        tariff = schedule.for_date(date.fromisoformat(str(tariff_from)[:10]))
    except ValueError:
        return None
    return tariff.split_fixed(fixed)


def _f(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    return float(value) if value is not None else 0.0


@dataclass
class Totals:
    """Sums over a span of hours. Money incl VAT."""

    label: str = ""
    hours: int = 0
    house_kwh: float = 0.0
    meter_kwh: float = 0.0
    day_kwh: float = 0.0
    night_kwh: float = 0.0
    hall_kwh: float = 0.0
    rest_kwh: float = 0.0
    hall_day_kwh: float = 0.0
    hall_night_kwh: float = 0.0
    energy_cost: float = 0.0
    day_cost: float = 0.0
    night_cost: float = 0.0
    fixed_cost: float = 0.0
    hall_cost: float = 0.0
    rest_cost: float = 0.0
    peak_w: float | None = None
    min_w: float | None = None
    sum_w: float = 0.0
    sum_va: float = 0.0
    hall_sum_w: float = 0.0
    hall_sum_va: float = 0.0
    voltage_min: float | None = None
    voltage_max: float | None = None
    hall_over_house_hours: int = 0
    days: set = field(default_factory=set)
    # Hours with no tariff in force (before the first tariff period): their kWh count,
    # their money is unknown and is NOT in the cost totals.
    unpriced_hours: int = 0
    priced_kwh: float = 0.0
    # House kWh in the hours the hall clamp also measured (ESB-filled hours have no hall).
    hall_base_kwh: float = 0.0
    # The fixed charges split into their two parts (needs the schedule to know each
    # tariff's ratio; None without one).
    schedule: Any = field(default=None, repr=False, compare=False)
    standing_cost: float = 0.0
    pso_cost: float = 0.0

    def add(self, row: dict[str, Any]) -> None:
        self.hours += 1
        kwh = _f(row, "house_kwh")
        self.house_kwh += kwh
        self.meter_kwh += _f(row, "meter_kwh") if row.get("meter_kwh") is not None else kwh
        night = row.get("band") == BAND_NIGHT
        cost = _f(row, "house_cost_eur")
        if row.get("house_cost_eur") is None and row.get("unit_rate_eur") is None:
            self.unpriced_hours += 1
        else:
            self.priced_kwh += kwh
        fixed = _f(row, "fixed_cost_eur")
        parts = _split_for(self.schedule, row.get("tariff_from"), fixed)
        if parts is not None:
            self.standing_cost += parts[0]
            self.pso_cost += parts[1]
        hall = _f(row, "hall_kwh") * float(row.get("hall_factor") or 1.0)
        if row.get("hall_kwh") is not None:
            self.hall_base_kwh += kwh  # the share is over hours the hall was measured
        if night:
            self.night_kwh += kwh
            self.night_cost += cost
            self.hall_night_kwh += hall
        else:
            self.day_kwh += kwh
            self.day_cost += cost
            self.hall_day_kwh += hall
        self.hall_kwh += hall
        self.rest_kwh += _f(row, "rest_kwh")
        self.energy_cost += cost
        self.fixed_cost += _f(row, "fixed_cost_eur")
        self.hall_cost += _f(row, "hall_cost_eur")
        self.rest_cost += _f(row, "rest_cost_eur")
        if row.get("house_max_w") is not None:
            self.peak_w = max(self.peak_w or 0.0, float(row["house_max_w"]))
        if row.get("house_min_w") is not None:
            v = float(row["house_min_w"])
            self.min_w = v if self.min_w is None else min(self.min_w, v)
        if row.get("house_avg_w") is not None and row.get("house_avg_va"):
            self.sum_w += float(row["house_avg_w"])
            self.sum_va += float(row["house_avg_va"])
        if row.get("hall_avg_w") is not None and row.get("hall_avg_va"):
            self.hall_sum_w += float(row["hall_avg_w"])
            self.hall_sum_va += float(row["hall_avg_va"])
        if row.get("voltage_min") is not None:
            v = float(row["voltage_min"])
            self.voltage_min = v if self.voltage_min is None else min(self.voltage_min, v)
        if row.get("voltage_max") is not None:
            v = float(row["voltage_max"])
            self.voltage_max = v if self.voltage_max is None else max(self.voltage_max, v)
        self.hall_over_house_hours += int(row.get("hall_over_house") or 0)
        self.days.add(row.get("local_date"))

    def add_live(
        self,
        house_kwh: float,
        hall_kwh: float | None,
        band: str,
        rate: float | None,
        fixed: float | None,
        factor: float = 1.0,
        tariff: Any = None,
    ) -> None:
        """Fold in energy not yet rolled up into an hourly row. Money on meter kWh.

        `rate` and `fixed` are None when no tariff is in force: the kWh still count.
        """
        self.house_kwh += house_kwh
        self.meter_kwh += house_kwh * factor
        if rate is None:
            self.unpriced_hours += 1
            rate, fixed = 0.0, 0.0
        else:
            self.priced_kwh += house_kwh
            if tariff is not None and fixed:
                standing, pso = tariff.split_fixed(fixed)
                self.standing_cost += standing
                self.pso_cost += pso
        fixed = fixed or 0.0
        rate = rate * factor
        cost = house_kwh * rate
        if band == BAND_NIGHT:
            self.night_kwh += house_kwh
            self.night_cost += cost
        else:
            self.day_kwh += house_kwh
            self.day_cost += cost
        self.energy_cost += cost
        self.fixed_cost += fixed
        if hall_kwh is not None:
            self.hall_base_kwh += house_kwh
            self.hall_kwh += hall_kwh
            alloc = min(hall_kwh, house_kwh)
            self.hall_cost += alloc * rate
            rest = max(house_kwh - hall_kwh, 0.0)
            self.rest_kwh += rest
            self.rest_cost += rest * rate
            if band == BAND_NIGHT:
                self.hall_night_kwh += hall_kwh
            else:
                self.hall_day_kwh += hall_kwh

    @property
    def total_cost(self) -> float:
        return self.energy_cost + self.fixed_cost

    @property
    def house_pf(self) -> float | None:
        return self.sum_w / self.sum_va if self.sum_va else None

    @property
    def hall_pf(self) -> float | None:
        return self.hall_sum_w / self.hall_sum_va if self.hall_sum_va else None

    @property
    def night_share(self) -> float | None:
        return self.night_kwh / self.house_kwh if self.house_kwh else None

    @property
    def hall_share(self) -> float | None:
        return min(self.hall_kwh / self.hall_base_kwh, 1.0) if self.hall_base_kwh else None

    @property
    def effective_rate(self) -> float | None:
        """Units only, € per kWh, over the hours that had a price."""
        return self.energy_cost / self.priced_kwh if self.priced_kwh else None

    @property
    def all_in_rate(self) -> float | None:
        return self.total_cost / self.priced_kwh if self.priced_kwh else None

    def as_dict(self) -> dict[str, Any]:
        def r(v, n=3):
            return None if v is None else round(v, n)

        return {
            "label": self.label,
            "hours": self.hours,
            "days": len(self.days),
            "house_kwh": r(self.house_kwh),
            "meter_kwh": r(self.meter_kwh),
            "day_kwh": r(self.day_kwh),
            "night_kwh": r(self.night_kwh),
            "hall_kwh": r(self.hall_kwh),
            "rest_kwh": r(self.rest_kwh),
            "energy_cost": r(self.energy_cost, 2),
            "day_cost": r(self.day_cost, 2),
            "night_cost": r(self.night_cost, 2),
            "fixed_cost": r(self.fixed_cost, 2),
            "standing_cost": r(self.standing_cost, 2) if self.schedule is not None else None,
            "pso_cost": r(self.pso_cost, 2) if self.schedule is not None else None,
            "total_cost": r(self.total_cost, 2),
            "hall_cost": r(self.hall_cost, 2),
            "rest_cost": r(self.rest_cost, 2),
            "peak_w": r(self.peak_w, 0),
            "house_pf": r(self.house_pf),
            "hall_pf": r(self.hall_pf),
            "night_share_pct": r(None if self.night_share is None else self.night_share * 100, 1),
            "hall_share_pct": r(None if self.hall_share is None else self.hall_share * 100, 1),
            "effective_rate": r(self.effective_rate, 4),
            "voltage_min": r(self.voltage_min, 1),
            "voltage_max": r(self.voltage_max, 1),
            "hall_over_house_hours": self.hall_over_house_hours,
            "unpriced_hours": self.unpriced_hours,
        }


def totals(rows: Iterable[dict[str, Any]], label: str = "", *, schedule: Any = None) -> Totals:
    t = Totals(label=label, schedule=schedule)
    for row in rows:
        t.add(row)
    return t


def rows_between(rows: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [r for r in rows if start <= r["hour_utc"] < end]


def local_range(first: date, last: date) -> tuple[datetime, datetime]:
    """[local midnight of first, local midnight after last) in UTC."""
    return local_midnight(first), local_midnight(last + timedelta(days=1))


# ------------------------------------------------------------------ profiles


def hour_profile(rows: list[dict[str, Any]], *, field_name: str = "house_kwh") -> dict[int, float]:
    """Mean kWh per LOCAL clock hour (= mean kW), over whatever rows are given."""
    sums: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(field_name) is None:
            continue
        hour = r["hour_utc"].astimezone(IRELAND).hour
        sums[hour].append(float(r[field_name]))
    return {h: (sum(v) / len(v)) for h, v in sorted(sums.items())}


def hour_shares(profile: dict[int, float]) -> list[float]:
    """Each local hour's share of a typical day (sums to 1)."""
    values = [max(profile.get(h, 0.0), 0.0) for h in range(24)]
    total = sum(values)
    return [v / total for v in values] if total else [1 / 24] * 24


def elapsed_share(shares: list[float], now: datetime) -> float:
    local = now.astimezone(IRELAND)
    done = sum(shares[: local.hour])
    return min(done + shares[local.hour] * (local.minute * 60 + local.second) / 3600, 1.0)


def weekday_profile(rows: list[dict[str, Any]]) -> dict[int, float]:
    by_day: dict[str, float] = defaultdict(float)
    weekday: dict[str, int] = {}
    for r in rows:
        if r.get("house_kwh") is None:
            continue
        by_day[r["local_date"]] += float(r["house_kwh"])
        weekday[r["local_date"]] = date.fromisoformat(r["local_date"]).weekday()
    groups: dict[int, list[float]] = defaultdict(list)
    for d, kwh in by_day.items():
        groups[weekday[d]].append(kwh)
    return {w: sum(v) / len(v) for w, v in sorted(groups.items())}


def baseline_w(rows: list[dict[str, Any]], *, field_name: str = "house_avg_w", pct: float = 0.05) -> float | None:
    """The always-on floor: a low percentile of hourly average power."""
    values = sorted(float(r[field_name]) for r in rows if r.get(field_name) is not None)
    if len(values) < 24:
        return None
    return values[int(len(values) * pct)]


def night_share_by_month(daily: dict[str, dict[str, Any]]) -> dict[int, float]:
    """Night share of kWh per calendar month, from v_daily-style rows."""
    acc: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for d, row in daily.items():
        month = int(d[5:7])
        acc[month][0] += float(row.get("night_kwh") or 0.0)
        acc[month][1] += float(row.get("house_kwh") or 0.0)
    return {m: n / t for m, (n, t) in acc.items() if t > 0}


def daily_series(rows: list[dict[str, Any]], key: str = "house_kwh", *, min_hours: int = 23) -> dict[date, float]:
    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get(key) is None:
            continue
        sums[r["local_date"]] += float(r[key])
        counts[r["local_date"]] += 1
    return {date.fromisoformat(d): v for d, v in sums.items() if counts[d] >= min_hours}


def anomalies(daily: dict[date, float], *, days: int = 60, sigma: float = 2.0) -> list[dict[str, Any]]:
    """Days unusually far from their weekday norm."""
    recent = sorted(daily)[-days:]
    by_wd: dict[int, list[float]] = defaultdict(list)
    for d in recent:
        by_wd[d.weekday()].append(daily[d])
    out = []
    for d in recent:
        values = by_wd[d.weekday()]
        if len(values) < 4:
            continue
        mean = statistics.mean(values)
        sd = statistics.pstdev(values)
        if sd and abs(daily[d] - mean) > sigma * sd:
            out.append(
                {
                    "date": d.isoformat(),
                    "kwh": round(daily[d], 2),
                    "typical": round(mean, 2),
                    "direction": "high" if daily[d] > mean else "low",
                }
            )
    return out


def compare(current: Totals, previous: Totals) -> dict[str, Any]:
    def pct(a: float, b: float) -> float | None:
        return round((a - b) / b * 100, 1) if b else None

    return {
        "current_kwh": round(current.house_kwh, 2),
        "previous_kwh": round(previous.house_kwh, 2),
        "change_kwh": round(current.house_kwh - previous.house_kwh, 2),
        "change_pct": pct(current.house_kwh, previous.house_kwh),
        "current_cost": round(current.total_cost, 2),
        "previous_cost": round(previous.total_cost, 2),
        "change_cost": round(current.total_cost - previous.total_cost, 2),
        "hall_change_pct": pct(current.hall_kwh, previous.hall_kwh),
        "rest_change_pct": pct(current.rest_kwh, previous.rest_kwh),
    }
