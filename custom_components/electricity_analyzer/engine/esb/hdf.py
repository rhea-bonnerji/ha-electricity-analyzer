"""Parser for ESB Networks HDF (Harmonised Data Format) interval exports.

Format as served by DataHub/DownloadHdfPeriodic:

    MPRN,Meter Serial Number,Read Value,Read Type,Read Date and End Time
    10000000001,10000001,0.412,Active Import Interval (kW),01-11-2024 00:30

Four things need care.

1. UNITS. Read Value for an "Interval (kW)" read is average POWER over the half
   hour, not energy. kWh = kW / 2. Treating the column as kWh doubles every
   figure. The read type string is authoritative, so we switch on it rather than
   assuming.

2. LOCAL TIMESTAMPS. "Read Date and End Time" is Europe/Dublin wall clock, not
   UTC. Everything downstream works in UTC, so we convert on the way in.

3. DST AMBIGUITY. Because timestamps are local wall clock, the autumn fall-back
   day contains the local hour 01:00-02:00 TWICE, giving two rows with byte
   identical timestamps that are an hour apart in reality. Python cannot tell
   them apart from the string alone; the only signal is the order the rows
   arrive in, so the resolution depends on which way the file runs.

   ESB serves this export NEWEST FIRST, which inverts the obvious rule. Treating
   a descending file as ascending maps both repeats to the same winter instant:
   one reading is lost to a duplicate key and the other is filed an hour out, on
   precisely the night when the night-band boundary is hardest to get right. See
   `_localise` and `_is_descending`.

4. INTERVAL END vs START. Rows are labelled with the END of the interval. The
   row stamped 23:00 covers 22:30-23:00, which is day-band, not night. We emit
   interval START so band classification lines up (see tariff.py).
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .constants import IRELAND_TZ

LOG = logging.getLogger(__name__)

DUBLIN = ZoneInfo(IRELAND_TZ)

# Half-hourly is what ESB publishes, but derive it from the data rather than
# assuming, so a future 15-minute feed does not silently break the kW->kWh maths.
DEFAULT_INTERVAL = timedelta(minutes=30)

_TIMESTAMP_FORMATS = (
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
)

# Column headers vary in spacing/case between exports, so match loosely.
_COL_ALIASES = {
    "mprn": ("mprn",),
    "meter": ("meter serial number", "meterserialnumber", "serial", "meter number"),
    "value": ("read value", "readvalue", "value"),
    "read_type": ("read type", "readtype", "type"),
    "timestamp": (
        "read date and end time",
        "readdateandendtime",
        "read date and time",
        "date and end time",
        "timestamp",
    ),
}


@dataclass(frozen=True)
class IntervalReading:
    """One settlement interval, normalised to UTC and kWh."""

    start_utc: datetime
    end_utc: datetime
    kwh: float
    read_type: str
    mprn: str
    meter: str

    @property
    def is_export(self) -> bool:
        return "export" in self.read_type.lower()


@dataclass
class HdfDataset:
    """Parsed HDF plus what we noticed while parsing.

    The warnings are not decoration. They drive the data-quality sensors, so a
    silently truncated download shows up instead of looking like low usage.
    """

    readings: list[IntervalReading] = field(default_factory=list)
    interval: timedelta = DEFAULT_INTERVAL
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    dst_ambiguous_resolved: int = 0

    # ------------------------------------------------------------ accessors

    def imports(self) -> list[IntervalReading]:
        return [r for r in self.readings if not r.is_export]

    def exports(self) -> list[IntervalReading]:
        return [r for r in self.readings if r.is_export]

    def import_intervals(self) -> list[tuple[datetime, float]]:
        """(interval_start_utc, kwh) pairs, ready for the tariff engine."""
        return [(r.start_utc, r.kwh) for r in self.imports()]

    def export_intervals(self) -> list[tuple[datetime, float]]:
        return [(r.start_utc, r.kwh) for r in self.exports()]

    @property
    def mprns(self) -> set[str]:
        return {r.mprn for r in self.readings}

    @property
    def first_start(self) -> datetime | None:
        return min((r.start_utc for r in self.readings), default=None)

    @property
    def last_end(self) -> datetime | None:
        return max((r.end_utc for r in self.readings), default=None)

    def total_kwh(self, *, exports: bool = False) -> float:
        rows = self.exports() if exports else self.imports()
        return sum(r.kwh for r in rows)

    # -------------------------------------------------------- data quality

    def find_gaps(self) -> list[tuple[datetime, datetime]]:
        """Missing stretches in the import series, as (gap_start, gap_end) UTC."""
        rows = sorted(self.imports(), key=lambda r: r.start_utc)
        gaps: list[tuple[datetime, datetime]] = []
        for prev, nxt in zip(rows, rows[1:]):
            if nxt.start_utc > prev.end_utc:
                gaps.append((prev.end_utc, nxt.start_utc))
        return gaps

    def find_duplicates(self) -> list[datetime]:
        seen: set[datetime] = set()
        dupes: list[datetime] = []
        for r in self.imports():
            if r.start_utc in seen:
                dupes.append(r.start_utc)
            seen.add(r.start_utc)
        return dupes


def parse_hdf(payload: bytes | str) -> HdfDataset:
    """Parse an HDF export into UTC/kWh intervals."""
    text = payload.decode("utf-8-sig", errors="replace") if isinstance(payload, bytes) else payload
    if not text.strip():
        ds = HdfDataset()
        ds.warnings.append("HDF payload was empty")
        return ds

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        ds = HdfDataset()
        ds.warnings.append("HDF payload had no header row")
        return ds

    cols = _map_columns(header)
    missing = {"value", "read_type", "timestamp"} - cols.keys()
    if missing:
        ds = HdfDataset()
        ds.warnings.append(
            f"HDF header missing required column(s) {sorted(missing)}; saw {header}"
        )
        return ds

    ds = HdfDataset()

    # Two phases on purpose. Resolving the DST fall-back repeat needs to know
    # whether the file runs oldest-first or newest-first, and that cannot be
    # known until enough rows have been seen — so rows are decoded first, the
    # direction is settled, and only then are timestamps localised.
    decoded: list[tuple[datetime, float, str, str, str]] = []

    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        try:
            raw_value = row[cols["value"]].strip()
            read_type = row[cols["read_type"]].strip()
            stamp = row[cols["timestamp"]].strip()
            mprn = row[cols["mprn"]].strip() if "mprn" in cols else ""
            meter = row[cols["meter"]].strip() if "meter" in cols else ""
        except IndexError:
            ds.skipped_rows += 1
            continue

        if not raw_value or not stamp:
            ds.skipped_rows += 1
            continue

        try:
            value = float(raw_value)
        except ValueError:
            ds.skipped_rows += 1
            continue

        naive = _parse_timestamp(stamp)
        if naive is None:
            ds.skipped_rows += 1
            if len(ds.warnings) < 5:
                ds.warnings.append(f"unrecognised timestamp format: {stamp!r}")
            continue

        decoded.append((naive, value, read_type, mprn, meter))

    descending = _is_descending([d[0] for d in decoded])
    if descending:
        LOG.debug("HDF runs newest-first; resolving DST repeats accordingly")

    # Per (mprn, meter, read_type) series, so a fall-back repeat in one series
    # is not confused with the start of another.
    last_local_utc: dict[tuple[str, str, str], datetime] = {}

    for naive, value, read_type, mprn, meter in decoded:
        key = (mprn, meter, read_type)
        end_utc, folded = _localise(
            naive, last_local_utc.get(key), descending=descending
        )
        if folded:
            ds.dst_ambiguous_resolved += 1
        last_local_utc[key] = end_utc

        start_utc = end_utc - ds.interval
        ds.readings.append(
            IntervalReading(
                start_utc=start_utc,
                end_utc=end_utc,
                kwh=_to_kwh(value, read_type, ds.interval),
                read_type=read_type,
                mprn=mprn,
                meter=meter,
            )
        )

    if not ds.readings:
        ds.warnings.append("HDF parsed but contained no usable interval rows")
        return ds

    inferred = _infer_interval(ds.readings)
    if inferred and inferred != ds.interval:
        # Re-derive with the real cadence: both the kW->kWh divisor and the
        # end->start shift depend on it.
        LOG.info("HDF cadence is %s, not %s; reparsing", inferred, ds.interval)
        ds.interval = inferred
        ds.readings = [
            IntervalReading(
                start_utc=r.end_utc - inferred,
                end_utc=r.end_utc,
                kwh=_rescale(r.kwh, DEFAULT_INTERVAL, inferred, r.read_type),
                read_type=r.read_type,
                mprn=r.mprn,
                meter=r.meter,
            )
            for r in ds.readings
        ]

    if dupes := ds.find_duplicates():
        ds.warnings.append(
            f"{len(dupes)} duplicate interval start(s), first at {dupes[0].isoformat()}"
        )
    if gaps := ds.find_gaps():
        total = sum((b - a for a, b in gaps), timedelta())
        ds.warnings.append(
            f"{len(gaps)} gap(s) totalling {total} in the import series, "
            f"first {gaps[0][0].isoformat()} -> {gaps[0][1].isoformat()}"
        )
    if ds.skipped_rows:
        ds.warnings.append(f"skipped {ds.skipped_rows} unparseable row(s)")

    return ds


# ------------------------------------------------------------------ internals


def _map_columns(header: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for idx, raw in enumerate(header):
        name = raw.strip().lower().replace("_", " ")
        for key, aliases in _COL_ALIASES.items():
            if key in found:
                continue
            if name in aliases or name.replace(" ", "") in aliases:
                found[key] = idx
    return found


def _parse_timestamp(value: str) -> datetime | None:
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _localise(
    naive: datetime, previous_utc: datetime | None, *, descending: bool = False
) -> tuple[datetime, bool]:
    """Local wall clock -> UTC, resolving the fall-back repeat.

    Returns (utc, used_fold) where used_fold marks an ambiguous local time we had
    to disambiguate.

    The only signal distinguishing the two passes through the repeated hour is
    the order the rows arrive in, so the rule depends on which way the file runs.
    ESB's periodic export is NEWEST FIRST, which inverts it:

        ascending  — prefer summer (fold=0); a repeat that would move backwards
                     is winter (fold=1)
        descending — prefer winter (fold=1); a repeat that would move forwards
                     is summer (fold=0)

    Assuming ascending on a descending file maps BOTH repeats to the winter
    instant, which is not a rounding difference: one reading is silently lost to
    a duplicate key and the other is filed an hour out, on the one night a year
    when the night band boundary matters most.
    """
    first = naive.replace(tzinfo=DUBLIN, fold=0).astimezone(timezone.utc)
    second = naive.replace(tzinfo=DUBLIN, fold=1).astimezone(timezone.utc)

    if first == second:
        return first, False  # unambiguous

    if previous_utc is None:
        # Nothing to compare against. Take the pass the file's direction implies.
        return (second, True) if descending else (first, False)

    if descending:
        # Keep the series moving backwards.
        if second >= previous_utc:
            return first, True
        return second, True

    # Keep the series moving forward.
    if first <= previous_utc:
        return second, True
    return first, False


def _is_descending(stamps: list[datetime]) -> bool:
    """Does this file run newest-first?

    Decided by majority over consecutive pairs rather than by comparing the ends,
    so a single out-of-order row or a fall-back repeat cannot flip the verdict.
    """
    forward = backward = 0
    for a, b in zip(stamps, stamps[1:]):
        if b > a:
            forward += 1
        elif b < a:
            backward += 1
    return backward > forward


def _to_kwh(value: float, read_type: str, interval: timedelta) -> float:
    """Convert a raw read into kWh based on the declared unit.

    "Interval (kW)" is average power, so energy = power x duration. At the usual
    half hour that is value / 2.
    """
    unit = read_type.lower()
    if "kwh" in unit:
        return value
    if "kw" in unit:
        return value * (interval.total_seconds() / 3600.0)
    # Unknown unit: ESB's periodic export is kW, so follow that but say so.
    LOG.debug("unrecognised read type %r, assuming kW", read_type)
    return value * (interval.total_seconds() / 3600.0)


def _rescale(kwh: float, assumed: timedelta, actual: timedelta, read_type: str) -> float:
    if "kwh" in read_type.lower():
        return kwh
    return kwh * (actual.total_seconds() / assumed.total_seconds())


def _infer_interval(readings: list[IntervalReading]) -> timedelta | None:
    """Most common spacing between consecutive end stamps in the first series."""
    ends = sorted({r.end_utc for r in readings})
    if len(ends) < 3:
        return None
    deltas: dict[timedelta, int] = {}
    for a, b in zip(ends, ends[1:]):
        d = b - a
        if d > timedelta(0):
            deltas[d] = deltas.get(d, 0) + 1
    if not deltas:
        return None
    return max(deltas.items(), key=lambda kv: kv[1])[0]
