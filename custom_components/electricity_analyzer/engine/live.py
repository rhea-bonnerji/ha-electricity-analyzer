"""Second-by-second accounting from the clamps' own energy counters.

NO HOME ASSISTANT IMPORTS. The integration feeds every state the Shelly pushes
(roughly once a second) into `LiveTracker.feed`, and reads the totals back.

WHY COUNTERS, NOT POWER

Integrating power samples drifts: every missed sample is energy that never happened.
The Shelly's energy counter is integrated on the device itself, so the difference
between two counter readings is exact no matter how many updates were missed in
between. The tracker therefore accumulates counter DELTAS, and only uses power for
the things power is good for (the instantaneous figures).

HOURS AND BANDS

Deltas are attributed to the UTC hour they happened in. When a delta straddles an
hour boundary it is split in proportion to time on each side; with updates every
second the split is a few watt-hours at most, and with a slow update stream it is
still the best estimate available. Because Irish day/night boundaries fall on whole
hours (GMT or local, see tariff.py), attributing to the hour also attributes to the
band exactly.

Completed hours wait here until the hourly rollup has stored the recorder's compiled
version of the same hour; the store then supersedes them.

COUNTER RESETS

A counter that goes backwards has been reset (device restart, firmware update). The
new value is then the energy since the reset, which is what gets added. This is the
same rule Home Assistant applies to `total_increasing` sensors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
HOUR = timedelta(hours=1)
# A single delta larger than this is not consumption; it is a counter that jumped
# (replaced device, restored backup). Ignored rather than booked as one huge hour.
MAX_PLAUSIBLE_KW = 25.0


def _floor(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


@dataclass
class Counter:
    """One channel's energy counter."""

    last_value: float | None = None
    last_time: datetime | None = None
    hours: dict[datetime, float] = field(default_factory=dict)
    resets: int = 0
    rejected: int = 0

    def seed(self, value: float, when: datetime, *, hour_so_far: float | None = None) -> None:
        """Start from `value` at `when`, optionally knowing this hour's kWh so far."""
        self.last_value = value
        self.last_time = when
        if hour_so_far is not None and hour_so_far >= 0:
            self.hours[_floor(when)] = hour_so_far

    def feed(self, value: float, when: datetime) -> float:
        """Add a counter reading. Returns the kWh credited."""
        if self.last_value is None or self.last_time is None:
            self.last_value, self.last_time = value, when
            return 0.0
        if when <= self.last_time:
            self.last_value = value
            return 0.0
        if value < self.last_value:
            self.resets += 1
            delta = max(value, 0.0)
        else:
            delta = value - self.last_value
        elapsed_h = (when - self.last_time).total_seconds() / 3600
        if delta > MAX_PLAUSIBLE_KW * max(elapsed_h, 1 / 60):
            self.rejected += 1
            self.last_value, self.last_time = value, when
            return 0.0
        self._spread(delta, self.last_time, when)
        self.last_value, self.last_time = value, when
        return delta

    def _spread(self, delta: float, start: datetime, end: datetime) -> None:
        total = (end - start).total_seconds()
        cursor = start
        while cursor < end:
            hour = _floor(cursor)
            boundary = min(hour + HOUR, end)
            share = (boundary - cursor).total_seconds() / total if total else 1.0
            self.hours[hour] = self.hours.get(hour, 0.0) + delta * share
            cursor = boundary

    def drop_before(self, hour: datetime) -> None:
        for h in [h for h in self.hours if h < hour]:
            del self.hours[h]


@dataclass
class LiveTracker:
    house: Counter = field(default_factory=Counter)
    hall: Counter = field(default_factory=Counter)
    house_power: float | None = None
    hall_power: float | None = None
    house_time: datetime | None = None
    hall_time: datetime | None = None
    power_time: datetime | None = None
    hall_va: float | None = None
    hall_model: dict = field(default_factory=dict)
    # Seconds today the hall clamp read more power than the house clamp.
    hall_over_seconds: float = 0.0
    hall_over_day: str | None = None

    def feed_energy(self, channel: str, value: float, when: datetime) -> float:
        return (self.house if channel == "house" else self.hall).feed(value, when)

    def feed_power(self, channel: str, watts: float, when: datetime, local_day: str) -> None:
        if self.hall_over_day != local_day:
            self.hall_over_day = local_day
            self.hall_over_seconds = 0.0
        if (
            self.power_time is not None
            and self.aligned
            and self.house_power is not None
            and self.hall_power is not None
            and self.hall_power > self.house_power
        ):
            self.hall_over_seconds += min((when - self.power_time).total_seconds(), 60.0)
        if channel == "house":
            self.house_power, self.house_time = watts, when
        else:
            self.hall_power, self.hall_time = watts, when
        self.power_time = when

    @property
    def aligned(self) -> bool:
        """Both clamps reported within a couple of seconds of each other."""
        if self.house_time is None or self.hall_time is None:
            return False
        return abs((self.house_time - self.hall_time).total_seconds()) <= 2.0

    @property
    def rest_power(self) -> float | None:
        if self.house_power is None:
            return None
        if self.hall_power is None:
            return None
        from .hallfix import hour_factor

        factor = hour_factor(self.hall_model, self.hall_power, self.hall_va)
        return max(self.house_power - self.hall_power * factor, 0.0)

    def pending_hours(self, after: datetime | None) -> dict[datetime, tuple[float, float | None]]:
        """Hours (house kWh, hall kWh) newer than the last stored hour `after`."""
        out: dict[datetime, tuple[float, float | None]] = {}
        for hour, kwh in self.house.hours.items():
            if after is not None and hour <= after:
                continue
            out[hour] = (kwh, self.hall.hours.get(hour) if self.hall.last_value is not None else None)
        return dict(sorted(out.items()))

    def forget_stored(self, last_stored: datetime) -> None:
        """The rollup has stored everything up to and including `last_stored`."""
        self.house.drop_before(last_stored + HOUR)
        self.hall.drop_before(last_stored + HOUR)
