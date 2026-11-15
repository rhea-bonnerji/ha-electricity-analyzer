"""Hours the clamp never saw, filled from the meter's own data once ESB has it.

NO HOME ASSISTANT IMPORTS.

WHAT A DOWNTIME LOOKS LIKE

The Shelly keeps counting on the device while Home Assistant is down (or cannot reach it).
The recorder has no statistics for those hours, and when it comes back the counter has
jumped: the whole downtime's energy lands in the FIRST hour after it, and is priced in
that hour's band (Home Assistant's own statistics, and so its Energy dashboard, record it
the same way). So the store has a hole, then one hour that is far too big.

THE REPAIR

ESB's half-hourly data covers every hour, two or three days later. Once it covers the hole
and the hour after it completely, the hole is filled from it, in clamp terms (ESB kWh
divided by the ESB-to-clamp ratio, so the meter factor maps it back to what the meter
counted). The hour after is replaced too, but only when it is plainly the lump: when its
clamp kWh matches ESB's energy over the whole span rather than over that hour alone.

BEFORE THE CLAMP

ESB's download is up to two years of the meter's own half-hours, from long before the clamp
was installed. Every hour of it that ESB covers completely becomes a stored hour too, so
the history, the charts and the costs (at whatever tariff periods are set for those dates)
reach back as far as the meter does. Those hours are the meter, not the clamp: they carry
source "esb", have no hall/rest split, and never teach the clamp-to-meter ratio.

Nothing is estimated where ESB has no data: an hour ESB only half covers stays empty.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

HOUR = timedelta(hours=1)
SOURCE = "esb"
# The hour after a hole is "the lump" when it holds at least this much more than ESB says
# that hour alone used, and is within LUMP_TOLERANCE of ESB's whole span.
MIN_LUMP_KWH = 0.2
LUMP_TOLERANCE = 0.25


def settle(stored: list[dict[str, Any]], esb: list[tuple[datetime, float, str]]) -> list[tuple[datetime, float]]:
    """(hour start, ESB kWh) for every clamp hour ESB now covers completely whose money does
    not yet rest on ESB's figure: those hours are re-priced on it (provisional -> final)."""
    per_hour: dict[datetime, list[float]] = defaultdict(list)
    for start, kwh, _band in esb:
        per_hour[start.replace(minute=0, second=0, microsecond=0)].append(float(kwh))
    out = []
    for row in stored:
        hour = row["hour_utc"]
        halves = per_hour.get(hour, ())
        if row.get("source") == SOURCE or len(halves) < 2:
            continue
        meter = sum(halves)
        if row.get("meter_source") != SOURCE or abs(float(row.get("meter_kwh") or 0.0) - round(meter, 4)) > 1e-4:
            out.append((hour, meter))
    return out


def plan(
    stored: list[dict[str, Any]],
    esb: list[tuple[datetime, float, str]],
    *,
    ratio: float = 1.0,
) -> list[tuple[datetime, float]]:
    """(hour start, clamp-equivalent kWh) for every hour to write from ESB."""
    if not stored:
        return []
    ratio = ratio if ratio and ratio > 0 else 1.0
    rows = {r["hour_utc"]: r for r in stored}
    per_hour: dict[datetime, list[float]] = defaultdict(list)
    for start, kwh, _band in esb:
        per_hour[start.replace(minute=0, second=0, microsecond=0)].append(float(kwh))

    def covered(hour: datetime) -> bool:
        return len(per_hour.get(hour, ())) >= 2  # both half-hours

    def clamp_terms(hour: datetime) -> float:
        return sum(per_hour[hour]) / ratio

    def from_clamp(hour: datetime) -> bool:
        row = rows.get(hour)
        return row is not None and row.get("source") != SOURCE

    out: list[tuple[datetime, float]] = []
    clamp_hours = sorted(h for h in rows if from_clamp(h))
    if clamp_hours:
        # The meter's history before the clamp: every hour ESB covers completely.
        out.extend((h, clamp_terms(h)) for h in sorted(per_hour) if h < clamp_hours[0] and covered(h))
    for before, after in zip(clamp_hours, clamp_hours[1:]):
        if after - before <= HOUR:
            continue
        # The hole: every hour between two clamp hours (missing, or filled earlier).
        hole = []
        t = before + HOUR
        while t < after:
            hole.append(t)
            t += HOUR
        if not all(covered(h) for h in (*hole, after)):
            continue  # ESB has not published all of it yet: try again later
        out.extend((h, clamp_terms(h)) for h in hole)
        own = clamp_terms(after)
        span = own + sum(clamp_terms(h) for h in hole)
        measured = float(rows[after].get("house_kwh") or 0.0)
        if span - own >= MIN_LUMP_KWH and abs(measured - span) <= LUMP_TOLERANCE * span:
            out.append((after, own))

    def changes(hour: datetime, kwh: float) -> bool:
        row = rows.get(hour)
        return (
            row is None
            or row.get("source") != SOURCE
            or abs(float(row.get("house_kwh") or 0.0) - round(kwh, 4)) > 1e-4
        )

    # Only what differs from what is stored, so running it again changes nothing.
    return [(h, kwh) for h, kwh in out if changes(h, kwh)]
