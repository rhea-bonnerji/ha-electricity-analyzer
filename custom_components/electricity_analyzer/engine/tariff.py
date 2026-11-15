"""Day/night tariff: which band an hour falls in, and what it costs.

NO HOME ASSISTANT IMPORTS. Pure, so it can be tested and reused by the offline tools.

THE NIGHT WINDOW AND IRISH CLOCK CHANGES

A two-register Irish meter (Day + Night, the old "Nightsaver" arrangement, which a
smart meter keeps unless it is moved to a smart tariff) switches registers on its
own clock, and that clock does NOT observe summer time:

    winter (GMT, UTC+0)   night 23:00 - 08:00 local
    summer (IST, UTC+1)   night 00:00 - 09:00 local

That is one fixed window in UTC, 23:00-08:00, all year. It was verified against
ESB register snapshots: over April-September the night register
advanced 1061.1 kWh, the GMT window classifies 1061.0 kWh of the half-hourly data as
night, and a local 23:00-08:00 window would have said 1107.3. So `clock="gmt"` is the
default, and "local" exists for smart tariffs, which do follow the wall clock.

Both windows start and end on whole hours in UTC (Ireland's offsets are whole
hours), so classifying an hourly bucket by its start instant is exact: no hour is
ever split between bands. On the autumn fall-back night the local hour 01:00-02:00
happens twice; in UTC they are two distinct hours, so nothing is double counted.

MONEY

Every figure the user types is INCLUDING VAT, as printed on the supplier's price
sheet, so no VAT is applied here. Standing charge and PSO levy are annual figures;
a day's share is annual / 365 (366 in a leap year). It is charged IN FULL at the start
of each local day: a day's cost starts at its fixed charge and the energy is added on
top as it is used. So a 23- or 25-hour day costs exactly one day of fixed charges.

Standing charge and PSO are FIXED charges: they never enter a €/kWh unit rate. An
hour before the earliest tariff period has no price at all (rate, energy cost and
fixed cost are None) rather than being priced retroactively at a later tariff.

Floats are used deliberately: these are running estimates, rounded for display, not
an invoice. Nothing here claims to reproduce a bill to the cent.
"""

from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

IRELAND = ZoneInfo("Europe/Dublin")
UTC = timezone.utc

BAND_DAY = "day"
BAND_NIGHT = "night"
BANDS = (BAND_DAY, BAND_NIGHT)

CLOCK_GMT = "gmt"
CLOCK_LOCAL = "local"


@dataclass(frozen=True)
class NightWindow:
    """When the night register runs. Hours are on `clock`, end exclusive."""

    start_hour: int = 23
    end_hour: int = 8
    clock: str = CLOCK_GMT

    def _hour(self, instant: datetime) -> int:
        if instant.tzinfo is None:
            raise ValueError("naive datetime; pass an aware UTC or local time")
        if self.clock == CLOCK_LOCAL:
            return instant.astimezone(IRELAND).hour
        return instant.astimezone(UTC).hour

    def contains_hour(self, hour: int) -> bool:
        if self.start_hour == self.end_hour:
            return False  # no night band at all
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour

    def band_at(self, instant: datetime) -> str:
        """The band the meter is recording into at `instant`."""
        return BAND_NIGHT if self.contains_hour(self._hour(instant)) else BAND_DAY

    def next_change(self, after: datetime) -> datetime:
        """The next instant the meter switches register, in UTC."""
        current = self.band_at(after)
        probe = after.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        for _ in range(50):
            probe += timedelta(hours=1)
            if self.band_at(probe) != current:
                return probe
        return probe  # unreachable for any real window

    def local_window(self, on: date) -> tuple[datetime, datetime]:
        """The night that STARTS on local date `on`, as local datetimes.

        For the default GMT window this is 23:00-08:00 in winter and 00:00-09:00 in
        summer; in summer the night "starting" on `on` therefore begins at midnight
        at the very start of the following local day.
        """
        if self.clock == CLOCK_LOCAL:
            start = datetime.combine(on, time(self.start_hour), IRELAND)
            end_day = on if self.end_hour > self.start_hour else on + timedelta(days=1)
            end = datetime.combine(end_day, time(self.end_hour), IRELAND)
            return start, end
        start_utc = datetime.combine(on, time(self.start_hour), UTC)
        hours = (self.end_hour - self.start_hour) % 24 or 24
        end_utc = start_utc + timedelta(hours=hours)
        return start_utc.astimezone(IRELAND), end_utc.astimezone(IRELAND)

    def describe(self, on: date) -> str:
        """`23:00-08:00 (GMT, winter time)` style label for one night."""
        start, end = self.local_window(on)
        label = f"{start:%H:%M}–{end:%H:%M}"
        if self.clock == CLOCK_LOCAL:
            return f"{label} local time"
        # Not `dst()`: tzdata models Europe/Dublin with NEGATIVE daylight saving (IST is
        # the standard time and winter GMT is the "DST" period), so dst() is zero in
        # summer. The UTC offset is unambiguous.
        summer = start.utcoffset() == timedelta(hours=1)
        return f"{label} ({'summer time' if summer else 'winter time'}, meter on GMT)"


@dataclass(frozen=True)
class Tariff:
    """One price period. Rates in €/kWh and charges in €/year, all INCLUDING VAT."""

    valid_from: date
    supplier: str
    plan: str
    day_rate: float
    night_rate: float
    standing_per_year: float
    pso_per_year: float
    window: NightWindow = field(default_factory=NightWindow)

    def rate(self, band: str) -> float:
        return self.night_rate if band == BAND_NIGHT else self.day_rate

    def rate_at(self, instant: datetime) -> float:
        return self.rate(self.window.band_at(instant))

    @property
    def fixed_per_day(self) -> float:
        return (self.standing_per_year + self.pso_per_year) / 365.0

    @property
    def standing_per_day(self) -> float:
        return self.standing_per_year / 365.0

    @property
    def pso_per_day(self) -> float:
        return self.pso_per_year / 365.0

    def standing_for_day(self, day: date) -> float:
        return self.standing_per_year / days_in_year(day.year)

    def pso_for_day(self, day: date) -> float:
        return self.pso_per_year / days_in_year(day.year)

    def fixed_for_day(self, day: date) -> float:
        """Standing charge + PSO for one local day: annual / 365, or / 366 in a leap year."""
        return (self.standing_per_year + self.pso_per_year) / days_in_year(day.year)

    def fixed_for_hour(self, hour_start: datetime) -> float:
        """The fixed charge booked on an hour: the whole day's on the day's first hour
        (local midnight), nothing on the others. A day starts at its minimum charge."""
        day = local_date(hour_start)
        return self.fixed_for_day(day) if hour_start == local_midnight(day) else 0.0

    def split_fixed(self, fixed: float) -> tuple[float, float]:
        """(standing charge, PSO levy) parts of an amount of fixed charges."""
        total = self.standing_per_year + self.pso_per_year
        if not total:
            return 0.0, 0.0
        standing = fixed * self.standing_per_year / total
        return standing, fixed - standing

    def as_dict(self) -> dict:
        d = asdict(self)
        d["valid_from"] = self.valid_from.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Tariff:
        w = d.get("window") or {}
        return cls(
            valid_from=date.fromisoformat(str(d["valid_from"])),
            supplier=str(d.get("supplier") or ""),
            plan=str(d.get("plan") or ""),
            day_rate=float(d["day_rate"]),
            night_rate=float(d["night_rate"]),
            standing_per_year=float(d.get("standing_per_year") or 0.0),
            pso_per_year=float(d.get("pso_per_year") or 0.0),
            window=NightWindow(
                start_hour=int(w.get("start_hour", 23)),
                end_hour=int(w.get("end_hour", 8)),
                clock=str(w.get("clock", CLOCK_GMT)),
            ),
        )


# SSE Airtricity "30% Day Night", 1YR-ELEC-30 V7, Nightsaver (two-register) meter,
# urban. Discount applies to unit rates only. In force from
# 6 October 2026.
#   Day   29.71c ex VAT -> 32.38c inc 9% VAT
#   Night 17.98c ex VAT -> 19.60c inc 9% VAT
#   Urban Nightsaver standing charge 98.55c/day inc VAT = €359.71/year
#   PSO levy €19.10/year inc VAT
# Source: https://sseairtricity.com/assets/Tariffs/ROI/Current/1YR-Elec-30.pdf
DEFAULT_TARIFF = Tariff(
    valid_from=date(2026, 10, 6),
    supplier="SSE Airtricity",
    plan="30% Day Night (Nightsaver)",
    day_rate=0.3238,
    night_rate=0.1960,
    standing_per_year=359.71,
    pso_per_year=19.10,
    window=NightWindow(23, 8, CLOCK_GMT),
)

# What version 1.0.0/1.0.1 shipped as the default: the same plan, but dated 5 October
# and with a PSO levy of €6.67. Installations that kept those defaults are moved onto
# DEFAULT_TARIFF's date and levy (and nothing else) on upgrade; anything typed in by
# hand is left exactly as it is.
LEGACY_DEFAULT_VALID_FROM = date(2026, 10, 5)
LEGACY_DEFAULT_PSO = 6.67


class TariffSchedule:
    """Dated tariff periods. The one in force on a day is the latest that started.

    Days before the earliest period have NO known price. `for_date` still answers
    with the earliest period, because the meter's day/night window is physics and is
    needed to file kWh in a register whatever the price; money must use `in_force`,
    which says None there, so a new tariff is never applied to hours before it began.
    """

    def __init__(self, periods: list[Tariff]) -> None:
        if not periods:
            periods = [DEFAULT_TARIFF]
        self._periods = sorted(periods, key=lambda t: t.valid_from)

    @property
    def periods(self) -> list[Tariff]:
        return list(self._periods)

    @property
    def current(self) -> Tariff:
        """The period in force today (not a later one that has not started yet)."""
        return self.for_date(datetime.now(UTC).astimezone(IRELAND).date())

    @property
    def first_priced_day(self) -> date:
        return self._periods[0].valid_from

    def in_force(self, day: date) -> Tariff | None:
        """The period that prices `day`, or None before the earliest one began."""
        return None if self.is_estimated(day) else self.for_date(day)

    def for_date(self, day: date) -> Tariff:
        chosen = self._periods[0]
        for period in self._periods:
            if period.valid_from <= day:
                chosen = period
            else:
                break
        return chosen

    def for_instant(self, instant: datetime) -> Tariff:
        return self.for_date(local_date(instant))

    def is_estimated(self, day: date) -> bool:
        return day < self._periods[0].valid_from

    def with_period(self, tariff: Tariff) -> TariffSchedule:
        kept = [p for p in self._periods if p.valid_from != tariff.valid_from]
        return TariffSchedule([*kept, tariff])


@dataclass(frozen=True)
class HourCost:
    """`rate`, `energy_cost` and `fixed_cost` are None when no tariff was in force."""

    band: str
    rate: float | None
    energy_cost: float | None
    fixed_cost: float | None
    tariff: Tariff | None = None

    @property
    def priced(self) -> bool:
        return self.tariff is not None

    @property
    def total(self) -> float | None:
        if self.energy_cost is None or self.fixed_cost is None:
            return None
        return self.energy_cost + self.fixed_cost


def cost_hour(schedule: TariffSchedule, hour_start: datetime, kwh: float) -> HourCost:
    # The band comes from the meter's clock whatever the price; the price only from a
    # tariff that had started by this hour's local date.
    band = schedule.for_instant(hour_start).window.band_at(hour_start)
    tariff = schedule.in_force(local_date(hour_start))
    if tariff is None:
        return HourCost(band=band, rate=None, energy_cost=None, fixed_cost=None)
    rate = tariff.rate(band)
    return HourCost(
        band=band,
        rate=rate,
        energy_cost=kwh * rate,
        fixed_cost=tariff.fixed_for_hour(hour_start),
        tariff=tariff,
    )


# ------------------------------------------------------------------- time helpers


def days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


def local_date(instant: datetime) -> date:
    return instant.astimezone(IRELAND).date()


def local_midnight(day: date) -> datetime:
    """Start of a local calendar day, as an aware UTC datetime."""
    return datetime.combine(day, time(0), IRELAND).astimezone(UTC)


def hours_in_local_day(day: date) -> int:
    """23 on the spring-forward day, 25 on the fall-back day, otherwise 24."""
    start = local_midnight(day)
    end = local_midnight(day + timedelta(days=1))
    return round((end - start).total_seconds() / 3600)


def floor_hour(instant: datetime) -> datetime:
    return instant.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def with_window(tariff: Tariff, window: NightWindow) -> Tariff:
    return replace(tariff, window=window)
