"""Deterministic household load model behind the simulated Shelly EM Gen4.

Pure Python with no Home Assistant imports, so it can be profiled, plotted and
sanity-checked on its own (``python model.py`` prints a short summary).

WHAT IS BEING SIMULATED

One Shelly EM Gen4 with two CT clamps in a Dublin house:

* ``house`` - a 120 A clamp on the mains tails: TOTAL house consumption.
* ``hall``  - a 50 A clamp on one sub-circuit that is *inside* the house total
  and runs aquarium kit 24/7 (pumps, two thermostatic heaters, timed LEDs).

The house reading is therefore ``hall + rest of house`` as seen by its own CT.

WHY EVERYTHING IS A PURE FUNCTION OF TIME

Every quantity is derived from ``(seed, timestamp)`` alone - there is no state
carried from one call to the next. That buys three things the integration
relies on:

1. The history backfill (``hourly``) and the live sensors (``sample`` and
   ``energy_kwh``) are the *same* signal, so the live energy counter continues
   the imported counter exactly instead of drifting away from it.
2. ``energy_kwh(a, b) + energy_kwh(b, c) == energy_kwh(a, c)``, so a restart can
   fast-forward the counter from *any* earlier restore point and land on the
   value it would have had if Home Assistant had never stopped. Even a crash
   that loses the last 15 minutes of restore data cannot make the counter dip.
3. Re-running with the same seed reproduces the same house, which makes bugs in
   the downstream analyzer reproducible.

Randomness comes from two sources, both keyed by the seed: a splitmix64 hash of
(stream, index) for per-sample noise (cheap, random access, no state), and
``random.Random`` seeded with a string for the per-hour / per-day schedules
(heater cycles, appliance events) which are generated once and cached.

ENERGY ACCOUNTING

Energy is integrated from one power sample per minute (sample-and-hold), the
same samples ``hourly`` aggregates. All load switching happens at arbitrary
sub-minute instants, so the per-minute integration is unbiased; it is not exact
to the second, and nothing downstream needs it to be.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
import math
import random
from typing import Any
from zoneinfo import ZoneInfo

HOUSE = "house"
HALL = "hall"
CHANNELS = (HOUSE, HALL)

QUANTITIES = (
    "power",
    "apparent_power",
    "power_factor",
    "voltage",
    "current",
    "frequency",
)

# The house is in Dublin whatever time zone Home Assistant is configured for:
# people boil kettles by local clock time, and Irish Standard Time / GMT
# switching moves the whole profile by an hour twice a year.
LOCAL_TZ = ZoneInfo("Europe/Dublin")

# ---------------------------------------------------------------------------
# Calibration: sample Irish smart-meter data.
# ---------------------------------------------------------------------------

HOUSE_KWH_PER_DAY: dict[int, float] = {
    1: 44.0, 2: 36.4, 3: 31.3, 4: 24.7, 5: 19.7, 6: 20.4,
    7: 18.8, 8: 23.7, 9: 22.4, 10: 26.6, 11: 26.1, 12: 35.6,
}  # fmt: skip

WEEKDAY_FACTOR: dict[int, float] = {
    0: 1.031, 1: 0.969, 2: 0.96, 3: 0.981, 4: 1.015, 5: 1.01, 6: 1.033,
}  # fmt: skip

# Mean kW by local hour of day. Only the SHAPE is used: each day is rescaled
# so its total matches the month's kWh/day.
PROFILE_KW: dict[int, tuple[float, ...]] = {
    1: (2.09, 1.46, 1.1, 1.12, 1.16, 1.82, 1.86, 1.14, 1.03, 1.08, 1.36, 2.78,
        2.42, 1.88, 1.92, 3.09, 2.6, 1.92, 2.29, 2.29, 2.38, 1.94, 1.45, 1.86),
    4: (0.86, 0.73, 0.56, 0.52, 0.52, 0.49, 1.07, 0.87, 0.57, 0.56, 0.57, 0.76,
        1.89, 1.36, 0.87, 0.69, 1.54, 1.56, 1.58, 1.49, 1.57, 1.88, 1.24, 0.91),
    7: (0.63, 0.6, 0.5, 0.48, 0.5, 0.5, 0.95, 0.61, 0.49, 0.57, 0.61, 0.74,
        1.69, 1.05, 0.57, 0.61, 1.01, 0.95, 0.97, 0.93, 1.18, 1.15, 0.83, 0.63),
    10: (0.9, 0.55, 0.53, 0.51, 0.52, 0.68, 1.07, 0.87, 0.53, 0.66, 1.02, 1.34,
         2.59, 1.66, 1.12, 0.96, 1.75, 1.05, 1.34, 1.6, 1.62, 1.49, 1.41, 0.88),
}  # fmt: skip

# Jan/Apr/Jul/Oct use their measured shape for the whole month; the months in
# between blend the two nearest references by month distance (Feb = 2/3 Jan +
# 1/3 Apr, ...). Per-month rather than per-day interpolation so that a month's
# hourly means reproduce its reference profile exactly instead of a blend.
_PROFILE_REFS = (1, 4, 7, 10)

DAY_SIGMA = 0.15  # lognormal day-to-day variation of the house total

# ---------------------------------------------------------------------------
# Hall circuit (aquariums, 24/7).
# ---------------------------------------------------------------------------

PUMP_W = 220.0
PUMP_PF = 0.65  # induction motors: poor PF. 0.65 rather than 0.6 so the
# circuit's combined PF lands in the observed 0.75-0.85 band.
HEATER_W = (195.0, 205.0)  # two nominal 200 W heaters, not identical
HEATER_PF = 1.0
HEATER_DUTY_WINTER = 0.55
HEATER_DUTY_SUMMER = 0.15
HEATER_ON_MIN_RANGE = (10.0, 25.0)  # minutes per ON phase
LIGHT_W = 160.0
LIGHT_PF = 0.9
LIGHT_ON_S = 10 * 3600  # 10:00 local
LIGHT_OFF_S = 22 * 3600  # 22:00 local
LIGHT_RAMP_S = 30 * 60  # aquarium LED controllers fade in and out

# ---------------------------------------------------------------------------
# Rest of house.
# ---------------------------------------------------------------------------

FLOOR_W = 80.0  # always-on standby (router, alarm, chargers...)
FLOOR_PF = 0.85
BASE_PF = 0.85  # the smooth "everything else" (TV, lights, PCs, heating fans)
FRIDGE_W = 110.0
FRIDGE_PF = 0.8
FRIDGE_ON_MIN_RANGE = (14.0, 22.0)
FRIDGE_OFF_MIN_RANGE = (26.0, 40.0)
FRIDGE_MEAN_W = FRIDGE_W * 18.0 / (18.0 + 33.0)  # mean of the two ranges
RESISTIVE_PF = 0.99
_BASE_RAMP_S = 300.0  # half-width of the base ramp across each hour boundary

# Share of the "above-floor" energy in each LOCAL hour that is delivered as
# discrete appliance events rather than as the smooth base. The rest goes to
# the smooth base, which is what keeps hourly MEANS on the profile whatever the
# events do: the base is defined as "target minus expected events".
_EVENT_SHARE = (
    0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5,
    0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.3,
)  # fmt: skip

# Relative weight of each appliance type by local hour, and a cap on how often
# it can happen per hour: a household does not boil the kettle five times an
# hour just because January afternoons are expensive - that extra energy is
# heating, which belongs in the smooth base.
# Indexed by local hour 0..23.
_W_KETTLE = (0.1, 0, 0, 0, 0, 0.1, 1, 2, 2, 1.5, 1.5, 1, 1, 1.5, 1, 1.5, 1.5, 1, 1, 1, 1.5, 1.5, 1, 0.5)
_W_COOK = (0, 0, 0, 0, 0, 0, 0, 0.5, 0.5, 0, 0, 0.5, 3, 2, 0, 0, 1, 3, 4, 3, 1, 0, 0, 0)
_W_LAUNDRY = (0, 0, 0, 0, 0, 0, 0, 0, 0.5, 2, 2, 2, 1.5, 1.5, 2, 2, 1.5, 1, 1, 1.5, 2, 2, 1, 0)
_W_SHOWER = (0, 0, 0, 0, 0, 0, 1.5, 3, 2, 0.5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.5, 1.5, 2, 1.5, 0.5)

# (name, hourly weights, max events per hour, expected Wh per event, seconds
# from an event's start to the centre of its energy). Starts are pulled back by
# that lead so an event's energy lands, on average, in the hour whose budget
# paid for it; otherwise a 40 minute dinner booked at 18:xx mostly shows up at
# 19:00 and every peak in the profile drifts an hour late.
_EVENT_TYPES = (
    ("kettle", _W_KETTLE, 1.0, 2150.0 * 180 / 3600, 90.0),
    ("cook", _W_COOK, 0.7, 950.0, 1200.0),
    ("laundry", _W_LAUNDRY, 0.35, 850.0, 900.0),
    ("shower", _W_SHOWER, 0.3, 9000.0 * 8 / 60, 240.0),
)

# ---------------------------------------------------------------------------
# Measurement.
# ---------------------------------------------------------------------------

# CT calibration: the hall clamp reads high and the house clamp reads low.
HALL_GAIN = 1.02
HOUSE_GAIN = 0.995

# Each reading is the mean over a short measurement window, and the two
# channels' windows are not aligned: in Home Assistant every Shelly channel
# arrives as its own status update. So for a moment after a hall heater
# switches on, the hall reading already carries part of the step and the house
# reading does not. Calibration and noise alone cannot make hall > house while
# the rest of the house draws >= ~80 W (that would need ~70 W of error); this
# can, and with the symptoms seen on the real system: occasionally, by up to a
# few tens of watts, mostly at night when the rest of the house is near its
# floor and a heater kicks in. With 5 s samples that is ~3-5 a week from spring
# to autumn and none in midwinter, when the night base load is far higher.
WINDOW_S = 1.0
CHANNEL_SKEW_S = 0.5  # the house window ends this much before the hall's

# Voltage at no load by local time of day: high in the small hours, sagging to
# the evening peak. (hour, volts), interpolated linearly and wrapped.
_V_KNOTS = (
    (0.0, 235.5), (3.5, 238.0), (7.0, 233.0), (12.0, 230.5),
    (18.5, 226.0), (21.0, 228.5), (24.0, 235.5),
)  # fmt: skip
V_DROP_PER_A = 0.15  # house's own drop on the service cable
NOMINAL_V = 230.0

# ---------------------------------------------------------------------------
# Deterministic hashing.
# ---------------------------------------------------------------------------

_MASK = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15
_TWO_PI = 2.0 * math.pi
_INV_2_32 = 1.0 / 4294967296.0

# Noise streams. Values are arbitrary but must never change: changing one
# changes the simulated house for every existing seed.
_S_DAY = 1
_S_FLOOR = 2
_S_HOUSE_SMOOTH = 3
_S_HOUSE_WHITE = 4
_S_HALL_SMOOTH = 5
_S_HALL_WHITE = 6
_S_V_SMOOTH = 7
_S_V_WHITE = 8
_S_V_HALL = 9
_S_F_FAST = 10
_S_F_SLOW = 11
_S_PF_HOUSE = 12
_S_PF_HALL = 13
_S_PUMP = 14
_S_BASE = 15
_S_V_DAY = 16
_N_STREAMS = 17


def _mix64(x: int) -> int:
    """splitmix64 finalizer: a fast, well-distributed 64-bit integer hash."""
    x = (x + _GOLDEN) & _MASK
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _MASK
    return x ^ (x >> 31)


def _interp_cyclic(knots: tuple[tuple[float, float], ...], x: float) -> float:
    """Linear interpolation through (x, y) knots whose first/last x bracket x."""
    for (x0, y0), (x1, y1) in zip(knots, knots[1:], strict=False):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return knots[-1][1]


def _lights_fraction(local_sod: float) -> float:
    """Aquarium LED output (0..1) at a local second-of-day, with fades."""
    if local_sod < LIGHT_ON_S or local_sod >= LIGHT_OFF_S:
        return 0.0
    if local_sod < LIGHT_ON_S + LIGHT_RAMP_S:
        return (local_sod - LIGHT_ON_S) / LIGHT_RAMP_S
    if local_sod >= LIGHT_OFF_S - LIGHT_RAMP_S:
        return (LIGHT_OFF_S - local_sod) / LIGHT_RAMP_S
    return 1.0


# Mean light output in each local hour, used to work out how much of the house
# target the hall circuit already accounts for.
_LIGHT_HOUR_MEAN = tuple(
    sum(_lights_fraction(h * 3600 + s + 0.5) for s in range(3600)) / 3600
    for h in range(24)
)


def heater_duty(doy: int) -> float:
    """Seasonal heater duty: 55 % mid-January, 15 % mid-July."""
    mid = (HEATER_DUTY_WINTER + HEATER_DUTY_SUMMER) / 2
    amp = (HEATER_DUTY_WINTER - HEATER_DUTY_SUMMER) / 2
    return mid + amp * math.cos(_TWO_PI * (doy - 15) / 365.25)


def _month_shape(month: int) -> tuple[float, ...]:
    """Normalised (sums to 1) 24-hour shape for a calendar month."""
    lo = max(r for r in _PROFILE_REFS if r <= month)
    hi = lo + 3
    w = (month - lo) / 3
    p0, p1 = PROFILE_KW[lo], PROFILE_KW[1 if hi > 12 else hi]
    s0, s1 = sum(p0), sum(p1)
    return tuple((1 - w) * a / s0 + w * b / s1 for a, b in zip(p0, p1, strict=True))


_SHAPES = {month: _month_shape(month) for month in range(1, 13)}


@dataclass(frozen=True, slots=True)
class _HourCtx:
    """Everything about one UTC hour that does not change within it."""

    base_w: float  # smooth rest-of-house load above floor and fridge
    floor_w: float  # today's standby floor
    offset_s: int  # local UTC offset (Dublin changes it on hour boundaries)
    rates: tuple[float, ...]  # expected events per hour, per _EVENT_TYPES
    target_w: float  # calibrated house mean for this hour


class ShellySimModel:
    """The simulated house. Thread-safe for reads; cheap to construct."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = int(seed)
        base = _mix64(self.seed & _MASK)
        self._salts = tuple(
            _mix64((base + s * 0x632BE59BD9B4E019) & _MASK) for s in range(_N_STREAMS)
        )
        # Per-instance caches (a class-level lru_cache would pin every instance).
        self._hour_ctx = lru_cache(maxsize=4096)(self._hour_ctx_uncached)
        self._active_segments = lru_cache(maxsize=512)(self._active_segments_uncached)
        self._hour_events = lru_cache(maxsize=512)(self._hour_events_uncached)
        self._heater_sched = lru_cache(maxsize=64)(self._heater_sched_uncached)
        self._fridge_sched = lru_cache(maxsize=32)(self._fridge_sched_uncached)
        self._minute_power = lru_cache(maxsize=4096)(self._minute_power_uncached)

    # ------------------------------------------------------------ randomness

    def _gauss(self, stream: int, idx: int) -> float:
        """Standard normal deviate for (stream, idx), via Box-Muller on one hash."""
        h = _mix64((self._salts[stream] + idx) & _MASK)
        u1 = ((h >> 32) + 1.0) * _INV_2_32  # (0, 1]: log is finite
        u2 = (h & 0xFFFFFFFF) * _INV_2_32
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(_TWO_PI * u2)

    def _smooth(self, stream: int, ts: float, period: float) -> float:
        """Continuous noise: N(0,1) knots every `period` s, smoothstep between.

        Real measurement and supply noise is correlated over seconds to
        minutes; independent noise on every sample looks wrong on a graph.
        """
        x = ts / period
        k = math.floor(x)
        f = x - k
        f = f * f * (3.0 - 2.0 * f)
        a = self._gauss(stream, k)
        return a + (self._gauss(stream, k + 1) - a) * f

    def _rng(self, *key: object) -> random.Random:
        # String seeds are hashed with SHA-512 by `random`, so they are stable
        # across processes (unlike hash(), which is salted per process).
        return random.Random("/".join(str(k) for k in (self.seed, *key)))

    # ---------------------------------------------------------- per-hour data

    def _hour_ctx_uncached(self, k: int) -> _HourCtx:
        local = datetime.fromtimestamp(k * 3600, LOCAL_TZ)
        offset = local.utcoffset()
        offset_s = int(offset.total_seconds()) if offset else 0
        lh = local.hour
        doy = local.timetuple().tm_yday
        day_ord = local.date().toordinal()

        z = self._gauss(_S_DAY, day_ord)
        day_factor = math.exp(DAY_SIGMA * z - DAY_SIGMA * DAY_SIGMA / 2)
        day_kwh = (
            HOUSE_KWH_PER_DAY[local.month] * WEEKDAY_FACTOR[local.weekday()] * day_factor
        )
        target_w = day_kwh * _SHAPES[local.month][lh] * 1000.0

        zf = self._gauss(_S_FLOOR, day_ord)
        floor_w = min(100.0, max(62.0, FLOOR_W * math.exp(0.12 * zf - 0.0072)))

        hall_expected = (
            PUMP_W
            + sum(HEATER_W) * heater_duty(doy)
            + LIGHT_W * _LIGHT_HOUR_MEAN[lh]
        )
        rest_target = target_w - hall_expected
        avail = max(0.0, rest_target - floor_w - FRIDGE_MEAN_W)

        total_weight = sum(t[1][lh] for t in _EVENT_TYPES)
        rates: list[float] = []
        events_w = 0.0
        for _, weights, cap, wh, _ in _EVENT_TYPES:
            if total_weight <= 0 or weights[lh] <= 0:
                rates.append(0.0)
                continue
            budget = _EVENT_SHARE[lh] * avail * weights[lh] / total_weight
            rate = min(cap, budget / wh)
            rates.append(rate)
            events_w += rate * wh

        base_w = max(0.0, rest_target - floor_w - FRIDGE_MEAN_W - events_w)
        return _HourCtx(
            base_w=base_w,
            floor_w=floor_w,
            offset_s=offset_s,
            rates=tuple(rates),
            target_w=target_w,
        )

    def _hour_events_uncached(self, k: int) -> tuple[tuple[float, float, float, float], ...]:
        """Appliance segments BOOKED to UTC hour k: (start, end, W, VA).

        Segments may start up to ~20 minutes before the hour (see the lead in
        _EVENT_TYPES) and run up to ~80 minutes after it.
        """
        ctx = self._hour_ctx(k)
        rng = self._rng("events", k)
        t0 = k * 3600.0
        segs: list[tuple[float, float, float, float]] = []

        def add(start: float, dur: float, watts: float, pf: float) -> None:
            segs.append((start, start + dur, watts, watts / pf))

        for (name, _, _, _, lead), rate in zip(_EVENT_TYPES, ctx.rates, strict=True):
            # Poisson count by inversion; rates are < 1 so this is a loop of 1-3.
            n, p, u = 0, math.exp(-rate), rng.random()
            cdf = p
            while u > cdf and n < 5:
                n += 1
                p *= rate / n
                cdf += p
            for _ in range(n):
                start = t0 + rng.uniform(0, 3600) - lead
                if name == "kettle":
                    add(start, rng.uniform(120, 240), rng.uniform(2050, 2250), RESISTIVE_PF)
                elif name == "shower":
                    add(start, rng.uniform(360, 600), rng.uniform(8500, 9500), RESISTIVE_PF)
                elif name == "laundry":
                    # fill (valve + electronics), heat, then wash/spin on the motor
                    heat = rng.uniform(15, 25) * 60
                    add(start + 300, heat, rng.uniform(1900, 2200), RESISTIVE_PF)
                    add(start + 300 + heat, rng.uniform(30, 50) * 60, rng.uniform(180, 320), 0.6)
                else:  # cook: preheat, then thermostat cycling
                    rated = rng.uniform(1500, 3000)
                    end = start + rng.uniform(20, 60) * 60
                    t = start
                    on = rng.uniform(5, 12) * 60
                    while t < end:
                        on = min(on, end - t)
                        add(t, on, rated, RESISTIVE_PF)
                        t += on + rng.uniform(60, 240)
                        on = rng.uniform(90, 300)
        return tuple(segs)

    def _active_segments_uncached(self, k: int) -> tuple[tuple[float, float, float, float], ...]:
        """Segments that overlap UTC hour k (booked from k-2 to k+1)."""
        lo, hi = k * 3600.0, (k + 1) * 3600.0
        return tuple(
            seg
            for kk in (k - 2, k - 1, k, k + 1)
            for seg in self._hour_events(kk)
            if seg[0] < hi and seg[1] > lo
        )

    # ---------------------------------------------------------- per-day data

    def _cycle_sched(
        self,
        rng: random.Random,
        day: int,
        on_range: tuple[float, float],
        off_minutes: Callable[[float], float],
    ) -> tuple[tuple[float, ...], tuple[float, ...]]:
        """ON intervals of a thermostat-style cycle within one UTC day.

        Generated per day so any instant can be evaluated without replaying
        history. Each day opens part-way through an OFF phase and any ON phase
        is cut at midnight: the joins are then at worst a heater switching OFF
        at 00:00, never a new day's schedule switching one ON out of nowhere
        (which, with the channel skew, would fake a hall > house every night).
        """
        lo, hi = day * 86400.0, (day + 1) * 86400.0
        t = lo + off_minutes(rng.uniform(*on_range) * 60) * 60 * rng.random()
        starts: list[float] = []
        ends: list[float] = []
        while t < hi:
            on = rng.uniform(*on_range) * 60
            starts.append(t)
            ends.append(min(hi, t + on))
            t += on + off_minutes(on) * 60
        return tuple(starts), tuple(ends)

    def _heater_sched_uncached(self, i: int, day: int) -> tuple[tuple[float, ...], tuple[float, ...]]:
        rng = self._rng("heater", i, day)
        doy = datetime.fromtimestamp(day * 86400, UTC).timetuple().tm_yday
        duty = heater_duty(doy)
        ratio = (1 - duty) / duty
        return self._cycle_sched(
            rng,
            day,
            HEATER_ON_MIN_RANGE,
            lambda on: on / 60 * ratio * rng.uniform(0.8, 1.2),
        )

    def _fridge_sched_uncached(self, day: int) -> tuple[tuple[float, ...], tuple[float, ...]]:
        rng = self._rng("fridge", day)
        return self._cycle_sched(
            rng, day, FRIDGE_ON_MIN_RANGE, lambda _on: rng.uniform(*FRIDGE_OFF_MIN_RANGE)
        )

    @staticmethod
    def _on_seconds(
        sched: tuple[tuple[float, ...], tuple[float, ...]], a: float, b: float
    ) -> float:
        """Seconds of [a, b] covered by the schedule's ON intervals.

        Windows are ~1 s and phases >= 10 min, so only the interval starting
        last before b and the one before it can overlap.
        """
        starts, ends = sched
        i = bisect_right(starts, b) - 1
        total = 0.0
        for j in (i - 1, i):
            if j >= 0:
                overlap = min(b, ends[j]) - max(a, starts[j])
                if overlap > 0:
                    total += overlap
        return total

    def _heaters_w(self, a: float, b: float) -> float:
        """Mean heater power over [a, b] (all heaters are resistive)."""
        days = {math.floor(a / 86400), math.floor(b / 86400)}
        watts = 0.0
        for i, rated in enumerate(HEATER_W):
            on = sum(self._on_seconds(self._heater_sched(i, d), a, b) for d in days)
            watts += rated * on
        return watts / (b - a)

    def _rest_switching(self, a: float, b: float) -> tuple[float, float]:
        """Mean (W, VA) of the fridge and appliance events over [a, b]."""
        span = b - a
        days = {math.floor(a / 86400), math.floor(b / 86400)}
        fridge_on = sum(self._on_seconds(self._fridge_sched(d), a, b) for d in days)
        watts = FRIDGE_W * fridge_on
        va = FRIDGE_W / FRIDGE_PF * fridge_on
        ka, kb = math.floor(a / 3600), math.floor(b / 3600)
        segs = self._active_segments(kb)
        if ka != kb:  # window straddles an hour: union, no double counting
            segs = tuple(set(segs) | set(self._active_segments(ka)))
        for start, end, seg_w, seg_va in segs:
            overlap = min(b, end) - max(a, start)
            if overlap > 0:
                watts += seg_w * overlap
                va += seg_va * overlap
        return watts / span, va / span

    # ------------------------------------------------------------- readings

    def _reading(self, ts: float) -> tuple[float, ...]:
        """Both channels' CT readings at a POSIX timestamp, as a flat tuple.

        (house P, S, PF, V, I, f, hall P, S, PF, V, I, f) - flat because the
        backfill calls this half a million times and dicts cost real time.
        """
        k = math.floor(ts / 3600)
        ctx = self._hour_ctx(k)

        # Slow loads: evaluated once at ts - they do not move within the
        # 1.5 s spanned by the two channels' measurement windows.
        pump = PUMP_W * (1 + 0.01 * self._smooth(_S_PUMP, ts, 300))
        lights = LIGHT_W * _lights_fraction((ts + ctx.offset_s) % 86400)
        slow_hall_p = pump + lights
        slow_hall_s = pump / PUMP_PF + lights / LIGHT_PF

        # The base holds each hour's value and ramps to the next over the ten
        # minutes straddling the boundary: continuous (no square steps on a
        # graph) while moving each hourly mean by only ~2 % of the difference
        # to its neighbours. Interpolating between hour centres would flatten
        # the profile's peaks by a quarter.
        sec = ts - k * 3600.0
        base = ctx.base_w
        if sec < _BASE_RAMP_S:
            prev = self._hour_ctx(k - 1).base_w
            base = prev + (base - prev) * (0.5 + sec / (2 * _BASE_RAMP_S))
        elif sec > 3600.0 - _BASE_RAMP_S:
            nxt = self._hour_ctx(k + 1).base_w
            base += (nxt - base) * (sec - 3600.0 + _BASE_RAMP_S) / (2 * _BASE_RAMP_S)
        base *= max(0.0, 1 + 0.08 * self._smooth(_S_BASE, ts, 600))
        slow_rest_p = ctx.floor_w + base
        slow_rest_s = ctx.floor_w / FLOOR_PF + base / BASE_PF

        # Switching loads, averaged over each channel's own window.
        hall_p = slow_hall_p + self._heaters_w(ts - WINDOW_S, ts)
        hall_s = slow_hall_s + (hall_p - slow_hall_p) / HEATER_PF
        lag_end = ts - CHANNEL_SKEW_S
        lag_heat = self._heaters_w(lag_end - WINDOW_S, lag_end)
        rest_sw_p, rest_sw_s = self._rest_switching(lag_end - WINDOW_S, lag_end)
        house_true_p = slow_hall_p + lag_heat + slow_rest_p + rest_sw_p
        house_true_s = slow_hall_s + lag_heat / HEATER_PF + slow_rest_s + rest_sw_s

        # CT readings: calibration gain + correlated noise + a little jitter.
        # The 120 A clamp has the coarser absolute noise floor.
        house_p = (
            HOUSE_GAIN * house_true_p
            + (2.0 + 0.003 * house_true_p) * self._smooth(_S_HOUSE_SMOOTH, ts, 30)
            + (1.0 + 0.001 * house_true_p) * self._gauss(_S_HOUSE_WHITE, math.floor(ts))
        )
        hall_p_read = (
            HALL_GAIN * hall_p
            + (1.0 + 0.002 * hall_p) * self._smooth(_S_HALL_SMOOTH, ts, 30)
            + (0.5 + 0.001 * hall_p) * self._gauss(_S_HALL_WHITE, math.floor(ts))
        )
        house_pf = min(1.0, max(0.05, house_true_p / house_true_s
                                + 0.004 * self._smooth(_S_PF_HOUSE, ts, 60)))
        hall_pf = min(1.0, max(0.05, hall_p / hall_s
                               + 0.004 * self._smooth(_S_PF_HALL, ts, 60)))
        house_s = house_p / house_pf
        hall_s_read = hall_p_read / hall_pf

        # Supply voltage: time-of-day shape + slow wander + jitter, minus the
        # house's own drop. The hall channel shares the supply but its reading
        # differs by a few tenths of a volt.
        local_h = ((ts + ctx.offset_s) % 86400) / 3600
        volts = (
            _interp_cyclic(_V_KNOTS, local_h)
            + 0.6 * self._smooth(_S_V_DAY, ts, 6 * 3600)
            + 0.7 * self._smooth(_S_V_SMOOTH, ts, 120)
            + 0.2 * self._gauss(_S_V_WHITE, math.floor(ts))
            - V_DROP_PER_A * house_s / NOMINAL_V
        )
        hall_v = volts + 0.25 * self._smooth(_S_V_HALL, ts, 45)

        # Grid frequency: one value for the whole island grid; both channels
        # see it. Slow and fast wander, never outside the statutory band.
        freq = 50.0 + 0.012 * self._smooth(_S_F_SLOW, ts, 300) + 0.018 * self._smooth(
            _S_F_FAST, ts, 20
        )
        freq = min(50.1, max(49.9, freq))

        return (
            house_p, house_s, house_pf, volts, house_s / volts, freq,
            hall_p_read, hall_s_read, hall_pf, hall_v, hall_s_read / hall_v, freq,
        )  # fmt: skip

    def sample(self, t: datetime) -> dict[str, dict[str, float]]:
        """Instantaneous readings of both channels at an aware datetime."""
        r = self._reading(t.timestamp())
        return {
            HOUSE: dict(zip(QUANTITIES, r[:6], strict=True)),
            HALL: dict(zip(QUANTITIES, r[6:], strict=True)),
        }

    # ----------------------------------------------------------------- energy

    def _minute_power_uncached(self, m: int) -> tuple[float, float]:
        r = self._reading(m * 60.0)
        return r[0], r[6]

    def energy_kwh(self, start: datetime, end: datetime) -> dict[str, float]:
        """Energy each CT registers over [start, end), in kWh.

        One power sample per minute, held for that minute and pro-rated for
        partial minutes. Additive over adjacent intervals by construction.
        """
        a, b = start.timestamp(), end.timestamp()
        house = hall = 0.0
        m = math.floor(a / 60)
        while m * 60.0 < b:
            seconds = min(b, (m + 1) * 60.0) - max(a, m * 60.0)
            if seconds > 0:
                hp, lp = self._minute_power(m)
                house += hp * seconds
                hall += lp * seconds
            m += 1
        return {HOUSE: house / 3.6e6, HALL: hall / 3.6e6}

    def hourly(self, start_utc: datetime) -> dict[str, dict[str, Any]]:
        """One UTC hour of statistics from 60 per-minute samples.

        Returns ``{channel: {"energy_kwh": float, <quantity>: {"mean", "min",
        "max"}}}``. Energy is the same per-minute integration `energy_kwh` uses,
        so the backfill and the live counter can never disagree.
        """
        ts0 = start_utc.timestamp()
        if ts0 % 3600:
            raise ValueError(f"{start_utc} is not the top of an hour")
        n = len(QUANTITIES)
        sums = [0.0] * (2 * n)
        mins = [math.inf] * (2 * n)
        maxs = [-math.inf] * (2 * n)
        for m in range(60):
            r = self._reading(ts0 + 60.0 * m)
            for i, v in enumerate(r):
                sums[i] += v
                if v < mins[i]:
                    mins[i] = v
                if v > maxs[i]:
                    maxs[i] = v
        out: dict[str, dict[str, Any]] = {}
        for c, channel in enumerate(CHANNELS):
            stats: dict[str, Any] = {"energy_kwh": sums[c * n] / 60 / 1000}
            for q, name in enumerate(QUANTITIES):
                i = c * n + q
                stats[name] = {"mean": sums[i] / 60, "min": mins[i], "max": maxs[i]}
            out[channel] = stats
        return out

    def hour_target_kw(self, start_utc: datetime) -> float:
        """The calibrated house mean for an hour (for checks, not for sensors)."""
        return self._hour_ctx(math.floor(start_utc.timestamp() / 3600)).target_w / 1000


def _summary() -> None:  # pragma: no cover - developer convenience
    """Print a calibration summary: python model.py"""
    import time  # noqa: PLC0415

    model = ShellySimModel(42)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    t = time.perf_counter()
    days = 7
    house = hall = 0.0
    for h in range(days * 24):
        hr = model.hourly(now - timedelta(hours=days * 24 - h))
        house += hr[HOUSE]["energy_kwh"]
        hall += hr[HALL]["energy_kwh"]
    took = time.perf_counter() - t
    print(f"last {days} days: house {house / days:.1f} kWh/day, hall {hall / days:.1f}")
    print(f"{days * 24 * 60} samples in {took:.2f}s ({took / days / 1440 * 1e6:.0f} us each)")
    print(model.sample(datetime.now(UTC)))


if __name__ == "__main__":  # pragma: no cover
    _summary()
