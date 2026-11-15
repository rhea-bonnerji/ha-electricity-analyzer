"""What the clamp saw versus what the utility meter recorded.

NO HOME ASSISTANT IMPORTS.

ESB Networks publishes the meter's own half-hourly data two to three days late. Once
it lands, every complete day can be compared with the full-house clamp. Two things
come out of that:

* a per-day difference, so a drifting or mis-seated clamp is visible;
* a CALIBRATION factor (meter kWh per clamp kWh), the median of recent daily ratios.
  It is what turns the clamp's running total into a credible meter-register
  estimate, and it is applied there only once it rests on enough days.

Days count only when both sides are complete: 23+ hours from the clamp, 46+
half-hours from ESB (46 on the spring-forward day). Partial days are not compared,
because a partial day always "disagrees".
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .tariff import IRELAND

UTC = timezone.utc
MIN_DAYS_FOR_CALIBRATION = 7
CALIBRATION_WINDOW = 30
# A clamp more than 15% off the meter on a day is not "calibration", it is a fault
# or an outage on one side. Such days are reported but not learned from.
OUTLIER_RATIO = 0.15


@dataclass
class DayCheck:
    day: date
    shelly_kwh: float
    esb_kwh: float
    shelly_night: float
    esb_night: float

    @property
    def diff(self) -> float:
        return self.shelly_kwh - self.esb_kwh

    @property
    def pct(self) -> float | None:
        return self.diff / self.esb_kwh * 100 if self.esb_kwh else None

    @property
    def ratio(self) -> float | None:
        return self.esb_kwh / self.shelly_kwh if self.shelly_kwh else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "shelly_kwh": round(self.shelly_kwh, 3),
            "esb_kwh": round(self.esb_kwh, 3),
            "diff_kwh": round(self.diff, 3),
            "diff_pct": None if self.pct is None else round(self.pct, 2),
            "shelly_night_kwh": round(self.shelly_night, 3),
            "esb_night_kwh": round(self.esb_night, 3),
        }


@dataclass
class Reconciliation:
    days: list[DayCheck] = field(default_factory=list)
    esb_through: datetime | None = None
    calibration: float | None = None
    calibration_days: int = 0

    @property
    def latest(self) -> DayCheck | None:
        return self.days[-1] if self.days else None

    def mean_abs_pct(self, n: int = 30) -> float | None:
        values = [abs(d.pct) for d in self.days[-n:] if d.pct is not None]
        return sum(values) / len(values) if values else None

    def total_diff(self, n: int = 30) -> tuple[float, float]:
        recent = self.days[-n:]
        return sum(d.shelly_kwh for d in recent), sum(d.esb_kwh for d in recent)

    def lag_hours(self, now: datetime) -> float | None:
        if self.esb_through is None:
            return None
        return (now - self.esb_through).total_seconds() / 3600

    def as_dict(self, now: datetime) -> dict[str, Any]:
        shelly, esb = self.total_diff()
        return {
            "esb_through": self.esb_through.isoformat() if self.esb_through else None,
            "lag_hours": None if self.lag_hours(now) is None else round(self.lag_hours(now), 1),
            "days_compared": len(self.days),
            "calibration": None if self.calibration is None else round(self.calibration, 4),
            "calibration_days": self.calibration_days,
            "last_30_shelly_kwh": round(shelly, 2),
            "last_30_esb_kwh": round(esb, 2),
            "last_30_diff_pct": round((shelly - esb) / esb * 100, 2) if esb else None,
            "mean_abs_daily_pct": None if self.mean_abs_pct() is None else round(self.mean_abs_pct(), 2),
            "latest": self.latest.as_dict() if self.latest else None,
            "per_day": [d.as_dict() for d in self.days[-60:]],
        }


def reconcile(
    hours: list[dict[str, Any]],
    esb: list[tuple[datetime, float, str]],
) -> Reconciliation:
    shelly: dict[str, list[float]] = {}
    for r in hours:
        if r.get("house_kwh") is None or r.get("partial"):
            continue
        acc = shelly.setdefault(r["local_date"], [0.0, 0.0, 0])
        acc[0] += float(r["house_kwh"])
        if r.get("band") == "night":
            acc[1] += float(r["house_kwh"])
        acc[2] += 1

    meter: dict[str, list[float]] = {}
    through = None
    for start, kwh, band in esb:
        d = start.astimezone(IRELAND).date().isoformat()
        acc = meter.setdefault(d, [0.0, 0.0, 0])
        acc[0] += kwh
        if band == "night":
            acc[1] += kwh
        acc[2] += 1
        end = start.astimezone(UTC).replace(second=0, microsecond=0)
        through = end if through is None or end > through else through

    out = Reconciliation()
    if through is not None:
        out.esb_through = through + timedelta(minutes=30)

    for d in sorted(set(shelly) & set(meter)):
        s, e = shelly[d], meter[d]
        if s[2] < 23 or e[2] < 46:
            continue
        out.days.append(DayCheck(date.fromisoformat(d), s[0], e[0], s[1], e[1]))

    ratios = [
        c.ratio
        for c in out.days[-CALIBRATION_WINDOW:]
        if c.ratio is not None and abs(c.ratio - 1.0) <= OUTLIER_RATIO
    ]
    out.calibration_days = len(ratios)
    if len(ratios) >= MIN_DAYS_FOR_CALIBRATION:
        out.calibration = statistics.median(ratios)
    return out
