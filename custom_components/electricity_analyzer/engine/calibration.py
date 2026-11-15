"""Learn the meter from its own readings, a little better with every one.

NO HOME ASSISTANT IMPORTS.

THE PROBLEM

The full-house clamp is not the meter. A CT clamp reads within a percent or two of
the utility meter, and that error is not the same at all loads: overnight the house
is mostly the hall's pumps and ballasts at a poor power factor, by day it is kettles
and ovens at nearly unity. So "meter kWh per clamp kWh" is learned separately for the
day register and the night register.

THE LESSONS

Every real reading of the meter (typed in at setup or later, read back from SSE
after you submit on their portal or app, or one of ESB's actual scheduled reads)
closes an interval with the reading before it. Over that interval the meter's
registers moved by an exact amount, and the clamp measured an exact amount in the
same band, so the interval says precisely what the ratio was:

    ratio = (register now - register then) / (clamp kWh in that band, same span)

Each lesson is checked before it is believed: a register that went backwards is a
typo, an interval with gaps in the clamp data cannot be compared, a very short one
is all rounding, and a ratio far outside what a CT can be off by is a misread. Those
are listed with the reason rather than learned from.

THE WEIGHTS

    factor = (sum of w * meter kWh + K * prior) / (sum of w * clamp kWh + K)

Energy-weighted (a two-month interval says more than a two-day one), with recent
intervals counting more (half-life 120 days, so a re-seated clamp or a firmware
change is followed), and shrunk towards a prior worth K = 20 kWh: ESB's half-hourly
comparison when there is one, otherwise 1.0. One short interval therefore nudges the
factor; several long ones decide it.

For each lesson the report also shows what the factors learned from EARLIER lessons
would have predicted and how far off that was, so the estimate visibly gets better
as readings accumulate.

BETWEEN READINGS, EXACTLY

Inside an interval that taught a usable lesson, that interval's own ratio is used
for the register history and for cost, so the history passes through every real
reading exactly and the money for that stretch adds up to what the meter recorded.
Outside them (before the first reading, after the latest) the learned factors apply.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .tariff import BAND_DAY, BAND_NIGHT

UTC = timezone.utc
HOUR = timedelta(hours=1)

HALF_LIFE_DAYS = 120.0
PRIOR_KWH = 20.0
MIN_CLAMP_KWH = 10.0
MIN_COVERAGE = 0.9
RATIO_BOUNDS = (0.85, 1.18)


@dataclass(frozen=True)
class Reading:
    """A real reading of the meter's registers."""

    taken_at: datetime
    day: float
    night: float
    origin: str  # setup | manual | submitted | sse | esb

    def value(self, band: str) -> float:
        return self.night if band == BAND_NIGHT else self.day


@dataclass
class Lesson:
    start: datetime
    end: datetime
    origin: str
    meter: dict[str, float]
    clamp: dict[str, float]
    coverage: float
    ratio: dict[str, float | None]
    used: dict[str, bool]
    predicted: dict[str, float | None]
    note: str = ""

    def error(self, band: str) -> float | None:
        p = self.predicted.get(band)
        return None if p is None else self.meter[band] - p

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "days": round((self.end - self.start).total_seconds() / 86400, 1),
            "origin": self.origin,
            "coverage_pct": round(self.coverage * 100, 1),
            "note": self.note,
        }
        for band in (BAND_DAY, BAND_NIGHT):
            out[f"{band}_meter_kwh"] = round(self.meter[band], 1)
            out[f"{band}_clamp_kwh"] = round(self.clamp[band], 1)
            out[f"{band}_ratio"] = None if self.ratio[band] is None else round(self.ratio[band], 4)
            out[f"{band}_used"] = self.used[band]
            out[f"{band}_predicted"] = None if self.predicted[band] is None else round(self.predicted[band], 1)
            err = self.error(band)
            out[f"{band}_error_kwh"] = None if err is None else round(err, 1)
        return out


@dataclass
class Calibration:
    day: float = 1.0
    night: float = 1.0
    source: str = "default"
    prior: float = 1.0
    lessons: list[Lesson] = field(default_factory=list)
    # (start, end, {band: ratio}) for intervals whose own ratio is used inside them.
    segments: list[tuple[datetime, datetime, dict[str, float]]] = field(default_factory=list)
    esb_days_used: int = 0

    def factor(self, band: str) -> float:
        return self.night if band == BAND_NIGHT else self.day

    def factor_at(self, instant: datetime, band: str) -> float:
        """The best meter/clamp ratio for an instant: its own interval's, else the learned one."""
        if self.segments:
            starts = [s for s, _, _ in self.segments]
            i = bisect.bisect_right(starts, instant) - 1
            if i >= 0:
                start, end, ratios = self.segments[i]
                if start <= instant < end and band in ratios:
                    return ratios[band]
        return self.factor(band)

    def fingerprint(self) -> str:
        segs = ";".join(f"{s.isoformat()}:{r}" for s, _, r in self.segments)
        return f"{self.day:.5f}|{self.night:.5f}|{segs}"

    def as_dict(self) -> dict[str, Any]:
        used = [l for l in self.lessons if any(l.used.values())]
        return {
            "day": round(self.day, 5),
            "night": round(self.night, 5),
            "source": self.source,
            "prior": round(self.prior, 5),
            "readings": len(self.lessons) + (1 if self.lessons else 0),
            "lessons_used": len(used),
            "esb_band_days_used": self.esb_days_used,
            "lessons": [l.as_dict() for l in self.lessons[-24:]],
        }


def clamp_between(
    hours: list[dict[str, Any]], start: datetime, end: datetime
) -> tuple[dict[str, float], float]:
    """Clamp kWh per band in [start, end), partial hours pro rata; and data coverage."""
    totals = {BAND_DAY: 0.0, BAND_NIGHT: 0.0}
    covered = 0.0
    for row in hours:
        h0: datetime = row["hour_utc"]
        h1 = h0 + HOUR
        overlap = (min(h1, end) - max(h0, start)).total_seconds()
        if overlap <= 0 or row.get("house_kwh") is None:
            continue
        frac = overlap / 3600
        band = row.get("band") or BAND_DAY
        totals[band] = totals.get(band, 0.0) + float(row["house_kwh"]) * frac
        covered += overlap
    span = (end - start).total_seconds()
    return totals, (covered / span if span > 0 else 0.0)


def merge_readings(readings: Iterable[Reading]) -> list[Reading]:
    """Sorted, with near-duplicates (same values within an hour) collapsed."""
    out: list[Reading] = []
    for r in sorted(readings, key=lambda r: r.taken_at):
        if out and abs((r.taken_at - out[-1].taken_at).total_seconds()) < 3600 and (
            abs(r.day - out[-1].day) < 0.5 and abs(r.night - out[-1].night) < 0.5
        ):
            continue
        out.append(r)
    return out


def learn(
    readings: Iterable[Reading],
    hours: list[dict[str, Any]],
    *,
    now: datetime,
    prior: float | None = None,
    prior_source: str = "default",
    esb_days: list[tuple[datetime, dict[str, float], dict[str, float]]] | None = None,
) -> Calibration:
    """`esb_days`: (day end, meter kWh per band, clamp kWh per band) for complete days."""
    ordered = merge_readings(readings)
    base = prior if prior is not None else 1.0
    cal = Calibration(day=base, night=base, source=prior_source if prior is not None else "default", prior=base)
    num = {BAND_DAY: PRIOR_KWH * base, BAND_NIGHT: PRIOR_KWH * base}
    den = {BAND_DAY: PRIOR_KWH, BAND_NIGHT: PRIOR_KWH}
    any_used = False

    for a, b in zip(ordered, ordered[1:]):
        clamp, coverage = clamp_between(hours, a.taken_at, b.taken_at)
        meter = {band: b.value(band) - a.value(band) for band in (BAND_DAY, BAND_NIGHT)}
        lesson = Lesson(
            start=a.taken_at,
            end=b.taken_at,
            origin=b.origin,
            meter=meter,
            clamp=clamp,
            coverage=coverage,
            ratio={},
            used={},
            predicted={},
        )
        notes = []
        age_days = max((now - b.taken_at).total_seconds() / 86400, 0.0)
        weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
        ratios: dict[str, float] = {}
        for band in (BAND_DAY, BAND_NIGHT):
            current = num[band] / den[band]
            lesson.predicted[band] = clamp[band] * current if coverage >= MIN_COVERAGE else None
            ratio = meter[band] / clamp[band] if clamp[band] > 0 else None
            lesson.ratio[band] = ratio
            ok = True
            if meter[band] < 0:
                ok = False
                notes.append(f"{band} register went backwards (typo?)")
            elif coverage < MIN_COVERAGE:
                ok = False
                notes.append(f"clamp data covers only {coverage:.0%} of the interval")
            elif clamp[band] < MIN_CLAMP_KWH:
                ok = False
                notes.append(f"{band}: too little use ({clamp[band]:.1f} kWh) to learn from")
            elif ratio is None or not (RATIO_BOUNDS[0] <= ratio <= RATIO_BOUNDS[1]):
                ok = False
                notes.append(f"{band} ratio {ratio:.3f} is outside what a clamp can be off by (misread?)")
            lesson.used[band] = ok
            if ok:
                num[band] += weight * meter[band]
                den[band] += weight * clamp[band]
                ratios[band] = ratio
                any_used = True
        lesson.note = "; ".join(dict.fromkeys(notes))
        cal.lessons.append(lesson)
        if ratios:
            cal.segments.append((a.taken_at, b.taken_at, ratios))

    # ESB's own half-hourly data: every complete day is evidence too, weighed exactly
    # like a reading interval (energy x recency), so the factors move a little with
    # each day ESB publishes and jump to a new reading the moment one arrives.
    esb_used = 0
    for end, meter, clamp in esb_days or []:
        age_days = max((now - end).total_seconds() / 86400, 0.0)
        weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
        for band in (BAND_DAY, BAND_NIGHT):
            c = clamp.get(band, 0.0)
            m = meter.get(band, 0.0)
            if c < 1.0:
                continue
            if not (RATIO_BOUNDS[0] <= m / c <= RATIO_BOUNDS[1]):
                continue
            num[band] += weight * m
            den[band] += weight * c
            esb_used += 1
    cal.esb_days_used = esb_used
    if esb_used and not any_used:
        cal.source = "esb"
    elif esb_used and any_used:
        cal.source = "readings+esb"

    # Four decimals: finer than any reading can justify, and it keeps the factors
    # stable between readings so history is only re-priced when something was learned.
    cal.day = round(num[BAND_DAY] / den[BAND_DAY], 4)
    cal.night = round(num[BAND_NIGHT] / den[BAND_NIGHT], 4)
    cal.segments = [(a, b, {k: round(v, 4) for k, v in r.items()}) for a, b, r in cal.segments]
    if any_used and not esb_used:
        cal.source = "readings"
    return cal


def register_series(
    readings: Iterable[Reading],
    hours: list[dict[str, Any]],
    cal: Calibration,
) -> list[tuple[datetime, float, float]]:
    """(hour_end, day_register, night_register) for every stored hour.

    Passes exactly through every real reading: within an interval the clamp's energy
    is scaled by that interval's own ratio, outside them by the learned factor.
    """
    ordered = merge_readings(readings)
    if not hours or not ordered:
        return []
    rows = sorted(hours, key=lambda r: r["hour_utc"])

    # Cumulative clamp energy per band at each hour end, and a function for any instant.
    ends: list[datetime] = []
    cum: list[tuple[float, float]] = []
    d = n = 0.0
    for r in rows:
        kwh = float(r.get("house_kwh") or 0.0)
        if r.get("band") == BAND_NIGHT:
            n += kwh
        else:
            d += kwh
        ends.append(r["hour_utc"] + HOUR)
        cum.append((d, n))
    starts = [r["hour_utc"] for r in rows]

    def F(t: datetime) -> tuple[float, float]:
        i = bisect.bisect_right(starts, t) - 1
        if i < 0:
            return 0.0, 0.0
        prev = cum[i - 1] if i > 0 else (0.0, 0.0)
        if t >= ends[i]:
            return cum[i]
        frac = (t - starts[i]) / HOUR
        return prev[0] + (cum[i][0] - prev[0]) * frac, prev[1] + (cum[i][1] - prev[1]) * frac

    anchors = [(r.taken_at, (r.day, r.night), F(r.taken_at)) for r in ordered]
    times = [a[0] for a in anchors]
    out: list[tuple[datetime, float, float]] = []
    for end, (cd, cn) in zip(ends, cum):
        i = bisect.bisect_right(times, end) - 1
        values = []
        for band, idx, c in ((BAND_DAY, 0, cd), (BAND_NIGHT, 1, cn)):
            if i < 0:
                t0, reg0, f0 = anchors[0]
                values.append(reg0[idx] - (f0[idx] - c) * cal.factor(band))
                continue
            t0, reg0, f0 = anchors[i]
            if i + 1 < len(anchors):
                t1, reg1, f1 = anchors[i + 1]
                span = f1[idx] - f0[idx]
                ratio = (reg1[idx] - reg0[idx]) / span if span > 0.5 else None
                if ratio is None or not (0.5 <= ratio <= 2.0):
                    ratio = cal.factor(band)
                values.append(reg0[idx] + (c - f0[idx]) * ratio)
            else:
                values.append(reg0[idx] + (c - f0[idx]) * cal.factor(band))
        out.append((end, values[0], values[1]))
    return out
