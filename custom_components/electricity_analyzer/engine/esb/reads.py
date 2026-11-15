"""Parse ESB's billed register reads (`MeterReadHistory/GetBilledReads`).

Shape, from captured traffic:

    {"readings": [{"date": "14-Sep-26", "meterNumber": "10000001",
                   "registerNumber": "002", "reading": "5494.00000000000000",
                   "registerType": "Night", "details": "Scheduled Reading (Actual)"}, ...]}

Register 001 is Day and 002 is Night on a two-register meter. A read dated D is the
meter's scheduled snapshot at the START of local day D (midnight), which is how these
are lined up against the estimated registers.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

_DATE_FORMATS = ("%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y")


def _parse_date(text: str) -> date | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_billed_reads(payload: dict[str, Any] | None) -> list[tuple[date, str, float, str]]:
    """(read_date, 'day'|'night', reading, details) for every usable row."""
    out: list[tuple[date, str, float, str]] = []
    for row in (payload or {}).get("readings") or []:
        when = _parse_date(str(row.get("date") or ""))
        kind = str(row.get("registerType") or "").strip().lower()
        if kind not in ("day", "night"):
            number = str(row.get("registerNumber") or "").lstrip("0")
            kind = {"1": "day", "2": "night"}.get(number, "")
        try:
            value = float(row.get("reading"))
        except (TypeError, ValueError):
            continue
        if when is None or kind not in ("day", "night"):
            continue
        out.append((when, kind, value, str(row.get("details") or "")))
    return out
