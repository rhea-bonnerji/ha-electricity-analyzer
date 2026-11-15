"""Minute-by-minute aggregation of the Shelly's (about once a second) pushes.

NO HOME ASSISTANT IMPORTS.

Hours are the analytical unit, but a person looking at "what happened at 18:42" wants
minutes. The recorder only keeps 5-minute statistics for ten days, so the integration
keeps its own: every push is folded into the current minute in O(1) (sums, minima,
maxima, sample counts), and each completed minute becomes one row in the database,
kept for good. About half a million rows, ~40 MB, a year.

Energy per minute comes from the counters' deltas, not from integrating power, for the
same reason as everywhere else: the counter is exact even when pushes are missed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc


def floor_minute(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(second=0, microsecond=0)


@dataclass
class Agg:
    n: int = 0
    total: float = 0.0
    lo: float | None = None
    hi: float | None = None

    def add(self, v: float) -> None:
        self.n += 1
        self.total += v
        self.lo = v if self.lo is None else min(self.lo, v)
        self.hi = v if self.hi is None else max(self.hi, v)

    @property
    def mean(self) -> float | None:
        return self.total / self.n if self.n else None


@dataclass
class Minute:
    start: datetime
    values: dict[str, Agg] = field(default_factory=dict)
    energy: dict[str, float] = field(default_factory=dict)

    def add(self, key: str, v: float) -> None:
        self.values.setdefault(key, Agg()).add(v)

    def row(self, last: dict[str, float]) -> dict[str, Any]:
        """One stored row. A minute with no new push carries the last known value."""
        def mean(key: str) -> float | None:
            agg = self.values.get(key)
            return round(agg.mean, 3) if agg and agg.n else last.get(key)

        def lo(key: str) -> float | None:
            agg = self.values.get(key)
            return round(agg.lo, 3) if agg and agg.lo is not None else last.get(key)

        def hi(key: str) -> float | None:
            agg = self.values.get(key)
            return round(agg.hi, 3) if agg and agg.hi is not None else last.get(key)

        house, hall = mean("house_w"), mean("hall_w")
        house_va, hall_va = mean("house_va"), mean("hall_va")
        return {
            "minute_utc": self.start,
            "house_w": house,
            "house_w_min": lo("house_w"),
            "house_w_max": hi("house_w"),
            "hall_w": hall,
            "hall_w_max": hi("hall_w"),
            "rest_w": mean("rest_w"),
            "house_pf": round(house / house_va, 3) if house is not None and house_va else None,
            "hall_pf": round(hall / hall_va, 3) if hall is not None and hall_va else None,
            "voltage": mean("voltage"),
            "voltage_min": lo("voltage"),
            "voltage_max": hi("voltage"),
            "house_wh": round(self.energy.get("house", 0.0) * 1000, 2),
            "hall_wh": round(self.energy.get("hall", 0.0) * 1000, 2),
            "samples": sum(a.n for a in self.values.values()),
        }


class MinuteBuffer:
    """Open minutes, keyed by start; completed ones are drained to the store."""

    def __init__(self) -> None:
        self.open: dict[datetime, Minute] = {}
        self.last: dict[str, float] = {}

    def add(self, key: str, value: float, when: datetime) -> None:
        start = floor_minute(when)
        self.open.setdefault(start, Minute(start)).add(key, value)
        self.last[key] = value

    def add_energy(self, channel: str, kwh: float, when: datetime) -> None:
        start = floor_minute(when)
        m = self.open.setdefault(start, Minute(start))
        m.energy[channel] = m.energy.get(channel, 0.0) + kwh

    def drain(self, now: datetime) -> list[dict[str, Any]]:
        current = floor_minute(now)
        done = sorted(k for k in self.open if k < current)
        rows = []
        for k in done:
            rows.append(self.open.pop(k).row(self.last))
        return rows
