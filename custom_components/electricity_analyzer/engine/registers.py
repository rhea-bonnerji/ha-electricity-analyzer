"""The meter's day and night registers, as they stand right now.

NO HOME ASSISTANT IMPORTS.

THE IDEA

Somebody reads the meter (or a reading is accepted by the supplier) at a known
moment, e.g. day 13876, night 5660 at 18:50:34 on 7 October 2026. From then on every
kWh the full-house clamp sees lands in exactly one register, chosen by the meter's
day/night clock (see tariff.py: GMT, so 23:00-08:00 in winter and 00:00-09:00 in
summer). So

    register(t) = reading at anchor + kWh the clamp saw in that band since the anchor

and the same identity run backwards gives every earlier register value too, which is
how the history of "what the meter would have said" is produced for the charts.

`calibration` scales the clamp's kWh into the meter's kWh. It defaults to 1.0 and is
learned from ESB (the meter's own half-hourly data) once enough days overlap, because
a CT clamp is typically within one or two percent of the utility meter, and over two
months one percent of 1,500 kWh is fifteen units on the dial.

THE PARTIAL HOURS

Hours are the stored resolution, but neither the anchor nor "now" falls on an hour
boundary. The anchor hour is split pro rata unless the caller supplies the measured
kWh after the anchor (the Home Assistant side does, from 5-minute statistics, while
they are still retained). The stretch since the last stored hour is supplied by the
caller as `tail` (the live counter's movement since that hour ended).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .tariff import BAND_DAY, BAND_NIGHT

UTC = timezone.utc
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Anchor:
    taken_at: datetime
    day: float
    night: float
    origin: str = "setup"

    @classmethod
    def from_row(cls, row: dict[str, Any] | None) -> Anchor | None:
        if not row:
            return None
        taken = row["taken_at"]
        if isinstance(taken, str):
            taken = datetime.fromisoformat(taken)
        return cls(taken.astimezone(UTC), float(row["day_kwh"]), float(row["night_kwh"]), row.get("origin", "setup"))


@dataclass
class RegisterEstimate:
    """The registers at `as_of`, and how they were arrived at."""

    as_of: datetime
    day: float
    night: float
    anchor: Anchor
    since_day_kwh: float
    since_night_kwh: float
    calibration: float | dict[str, float]
    hours_used: int
    hours_missing: int
    notes: list[str] = field(default_factory=list)

    @property
    def day_dial(self) -> int:
        """What the dial shows: whole units, never rounded up."""
        return math.floor(self.day + 1e-9)

    @property
    def night_dial(self) -> int:
        return math.floor(self.night + 1e-9)

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "day": round(self.day, 3),
            "night": round(self.night, 3),
            "day_dial": self.day_dial,
            "night_dial": self.night_dial,
            "anchor_taken_at": self.anchor.taken_at.isoformat(),
            "anchor_day": self.anchor.day,
            "anchor_night": self.anchor.night,
            "anchor_origin": self.anchor.origin,
            "since_anchor_day_kwh": round(self.since_day_kwh, 3),
            "since_anchor_night_kwh": round(self.since_night_kwh, 3),
            "calibration": self.calibration,
            "hours_used": self.hours_used,
            "hours_missing": self.hours_missing,
            "notes": list(self.notes),
        }


def _floor_hour(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def estimate(
    anchor: Anchor,
    hours: Iterable[dict[str, Any]],
    *,
    as_of: datetime,
    tail_kwh: float = 0.0,
    tail_band: str | None = None,
    calibration: float | dict[str, float] = 1.0,
    anchor_hour_after_kwh: float | None = None,
) -> RegisterEstimate:
    """Registers at `as_of` (>= anchor), from stored hours plus the live tail.

    `hours` are store rows (`hour_utc`, `band`, `house_kwh`) covering the anchor hour
    onwards. `tail_kwh` is energy after the last stored hour, up to `as_of`.
    """
    as_of = as_of.astimezone(UTC)
    scale = calibration if isinstance(calibration, dict) else {BAND_DAY: calibration, BAND_NIGHT: calibration}
    anchor_hour = _floor_hour(anchor.taken_at)
    since = {BAND_DAY: 0.0, BAND_NIGHT: 0.0}
    used = 0
    last_end = anchor_hour
    notes: list[str] = []

    for row in hours:
        start: datetime = row["hour_utc"]
        end = start + HOUR
        if end <= anchor.taken_at or start >= as_of:
            continue
        kwh = float(row.get("house_kwh") or 0.0)
        share = 1.0
        if start == anchor_hour:
            if anchor_hour_after_kwh is not None:
                share = max(min(anchor_hour_after_kwh, kwh), 0.0) / kwh if kwh > 1e-9 else 0.0
            else:
                share = (end - anchor.taken_at) / HOUR
        if row.get("meter_source") == "esb" and row.get("meter_kwh") is not None:
            # ESB's own figure for the hour: what the register really advanced by.
            since[row["band"]] = since.get(row["band"], 0.0) + float(row["meter_kwh"]) * share
        else:
            since[row["band"]] = since.get(row["band"], 0.0) + kwh * share * scale.get(row["band"], 1.0)
        used += 1
        last_end = max(last_end, end)

    if tail_kwh and tail_band:
        since[tail_band] = since.get(tail_band, 0.0) + max(tail_kwh, 0.0) * scale.get(tail_band, 1.0)

    expected = max(int((_floor_hour(as_of) - anchor_hour) / HOUR), 0)
    missing = max(expected - used, 0)
    if missing:
        notes.append(
            f"{missing} hour(s) since the anchor have no clamp data; the estimate "
            "under-reads by whatever was used in them"
        )
    if as_of < anchor.taken_at:
        notes.append("as_of is before the anchor; use register_series for history")

    return RegisterEstimate(
        as_of=as_of,
        day=anchor.day + since[BAND_DAY],
        night=anchor.night + since[BAND_NIGHT],
        anchor=anchor,
        since_day_kwh=since[BAND_DAY],
        since_night_kwh=since[BAND_NIGHT],
        calibration=calibration,
        hours_used=used,
        hours_missing=missing,
        notes=notes,
    )


def register_series(
    anchor: Anchor,
    hours: list[dict[str, Any]],
    *,
    calibration: float = 1.0,
) -> list[tuple[datetime, float, float]]:
    """(hour_end, day_register, night_register) for every stored hour, both sides of the anchor.

    register(t) = anchor + F(t) - F(anchor), where F is cumulative clamp energy per
    band from the start of the series, linear inside the anchor's own hour. Missing
    hours simply add nothing, so a gap shows as a flat stretch rather than a jump.
    """
    if not hours:
        return []
    ordered = sorted(hours, key=lambda r: r["hour_utc"])
    cumulative: list[tuple[datetime, float, float]] = []
    day = night = 0.0
    f_anchor_day = f_anchor_night = None
    for row in ordered:
        start: datetime = row["hour_utc"]
        end = start + HOUR
        kwh = float(row.get("house_kwh") or 0.0) * calibration
        is_night = row.get("band") == BAND_NIGHT
        if f_anchor_day is None and start <= anchor.taken_at < end:
            frac = (anchor.taken_at - start) / HOUR
            f_anchor_day = day + (0.0 if is_night else kwh * frac)
            f_anchor_night = night + (kwh * frac if is_night else 0.0)
        if is_night:
            night += kwh
        else:
            day += kwh
        cumulative.append((end, day, night))

    if f_anchor_day is None:
        # Anchor outside the stored span: pin to the nearest edge.
        if anchor.taken_at < ordered[0]["hour_utc"]:
            f_anchor_day = f_anchor_night = 0.0
        else:
            f_anchor_day, f_anchor_night = day, night

    return [
        (end, anchor.day + d - f_anchor_day, anchor.night + n - f_anchor_night)
        for end, d, n in cumulative
    ]


def compare_with_reads(
    series: list[tuple[datetime, float, float]],
    reads: list[dict[str, Any]],
    read_instant,
) -> list[dict[str, Any]]:
    """Estimated vs actual register at each ESB/SSE read we have.

    `read_instant(read_date_str) -> datetime` maps a read's date to the instant it
    represents (ESB's scheduled reads are midnight snapshots).
    """
    if not series:
        return []
    by_end = {end: (d, n) for end, d, n in series}
    out = []
    grouped: dict[str, dict[str, float]] = {}
    for r in reads:
        grouped.setdefault(r["read_date"], {})[r["register"]] = float(r["reading"])
    for read_date, regs in sorted(grouped.items()):
        instant = read_instant(read_date)
        est = by_end.get(instant)
        if est is None:
            continue
        row = {"read_date": read_date}
        for reg, est_value in (("day", est[0]), ("night", est[1])):
            if reg in regs:
                row[f"{reg}_actual"] = regs[reg]
                row[f"{reg}_estimated"] = round(est_value, 1)
                row[f"{reg}_difference"] = round(regs[reg] - est_value, 1)
        out.append(row)
    return out
