"""Tables for the dashboard's data card: every stored value, at any resolution.

NO HOME ASSISTANT IMPORTS.

One builder serves four resolutions (hour, day, month, year) from the same hourly
rows, so a daily figure is always exactly the sum of its hours. Each row also carries
the estimated meter registers at the END of the period and, where ESB has the same
period complete, ESB's figure and the difference, so "what my clamp says", "what the
meter says" and "what the dial should read" sit side by side.

Periods are LOCAL (Europe/Dublin) calendar periods. Hours are labelled in local time;
the autumn repeat hour is labelled "01:00 (2)" for its second pass.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from .analytics import Totals
from .tariff import BAND_NIGHT, IRELAND

UTC = timezone.utc
PERIODS = ("hour", "day", "month", "year")
DEFAULT_RANGE_DAYS = {"hour": 2, "day": 31, "month": 730, "year": 3650}

ESB_COMPLETE_DAY = 46   # half-hours; 46 on spring-forward day, 48 normally, 50 on fall-back


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    unit: str | None
    decimals: int | None
    group: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "decimals": self.decimals,
            "group": self.group,
        }


def _cols(period: str) -> list[Column]:
    when = {"hour": "Hour", "day": "Day", "month": "Month", "year": "Year"}[period]
    cols = [Column("label", when, None, None, "When")]
    if period == "hour":
        cols.append(Column("band", "Band", None, None, "When"))
    cols += [
        Column("house_kwh", "House", "kWh", 3 if period == "hour" else 2, "Energy"),
        Column("meter_kwh", "Meter-equivalent", "kWh", 3 if period == "hour" else 2, "Energy"),
        Column("day_kwh", "Day units", "kWh", 3 if period == "hour" else 2, "Energy"),
        Column("night_kwh", "Night units", "kWh", 3 if period == "hour" else 2, "Energy"),
        Column("hall_kwh", "Hall", "kWh", 3 if period == "hour" else 2, "Energy"),
        Column("rest_kwh", "Rest of house", "kWh", 3 if period == "hour" else 2, "Energy"),
    ]
    if period != "hour":
        cols += [
            Column("night_share_pct", "Night share", "%", 1, "Energy"),
            Column("hall_share_pct", "Hall share", "%", 1, "Energy"),
        ]
    cols += [
        Column("energy_cost", "Units cost", "€", 2 if period != "hour" else 3, "Cost"),
        Column("fixed_cost", "Standing + PSO", "€", 2 if period != "hour" else 3, "Cost"),
        Column("total_cost", "Total cost", "€", 2 if period != "hour" else 3, "Cost"),
        Column("hall_cost", "Hall cost", "€", 2 if period != "hour" else 3, "Cost"),
        Column("rest_cost", "Rest cost", "€", 2 if period != "hour" else 3, "Cost"),
    ]
    if period == "hour":
        cols.append(Column("unit_rate", "Unit rate", "€/kWh", 4, "Cost"))
    else:
        cols += [
            Column("day_cost", "Day units cost", "€", 2, "Cost"),
            Column("night_cost", "Night units cost", "€", 2, "Cost"),
            Column("effective_rate", "Avg unit price", "€/kWh", 4, "Cost"),
        ]
    cols += [
        Column("avg_w", "House avg", "W", 0, "Power"),
        Column("peak_w", "House peak", "W", 0, "Power"),
        Column("min_w", "House min", "W", 0, "Power"),
        Column("hall_avg_w", "Hall avg", "W", 0, "Power"),
        Column("house_pf", "House PF", None, 3, "Quality"),
        Column("hall_pf", "Hall PF", None, 3, "Quality"),
        Column("voltage_min", "House V min", "V", 1, "Quality"),
        Column("voltage_avg", "House V avg", "V", 1, "Quality"),
        Column("voltage_max", "House V max", "V", 1, "Quality"),
        Column("hall_voltage_min", "Hall V min", "V", 1, "Quality"),
        Column("hall_voltage_avg", "Hall V avg", "V", 1, "Quality"),
        Column("hall_voltage_max", "Hall V max", "V", 1, "Quality"),
        Column("house_current_avg", "House A avg", "A", 2, "Quality"),
        Column("house_current_max", "House A max", "A", 2, "Quality"),
        Column("hall_current_avg", "Hall A avg", "A", 2, "Quality"),
        Column("hall_current_max", "Hall A max", "A", 2, "Quality"),
        Column("frequency", "House Hz", "Hz", 2, "Quality"),
        Column("hall_frequency", "Hall Hz", "Hz", 2, "Quality"),
        Column("hall_factor", "Hall correction", None, 4, "Data"),
        *([Column("source", "Measured by", None, None, "Data"), Column("priced_on", "Priced on", None, None, "Data")]
          if period == "hour" else [Column("final_hours", "Hours final (ESB)", None, 0, "Data")]),
        Column("register_day", "Day register (est.)", "kWh", 1, "Meter"),
        Column("register_night", "Night register (est.)", "kWh", 1, "Meter"),
        Column("esb_kwh", "ESB metered", "kWh", 3 if period == "hour" else 2, "ESB"),
        Column("esb_diff_kwh", "Shelly − ESB", "kWh", 3 if period == "hour" else 2, "ESB"),
        Column("esb_diff_pct", "Shelly − ESB", "%", 1, "ESB"),
        Column("hours", "Hours of data", None, 0, "Data"),
        Column("hall_over_house_hours", "Hall > house", "h", 0, "Data"),
    ]
    return cols


def period_key(period: str, hour_start: datetime) -> str:
    local = hour_start.astimezone(IRELAND)
    if period == "hour":
        return hour_start.astimezone(UTC).isoformat()
    if period == "day":
        return local.date().isoformat()
    if period == "month":
        return local.strftime("%Y-%m")
    return local.strftime("%Y")


def period_label(period: str, key: str, hour_label: str | None = None) -> str:
    if period == "hour":
        start = datetime.fromisoformat(key).astimezone(IRELAND)
        text = start.strftime("%a %-d %b %Y %H:00")
        if hour_label and hour_label.endswith("(2)"):
            text += " (2)"
        return text
    if period == "day":
        return date.fromisoformat(key).strftime("%a %-d %b %Y")
    if period == "month":
        return datetime.strptime(key, "%Y-%m").strftime("%B %Y")
    return key


class TableBuilder:
    def __init__(
        self,
        hours: list[dict[str, Any]],
        *,
        live: list[dict[str, Any]] | None = None,
        esb: list[tuple[datetime, float, str]] | None = None,
        registers: list[tuple[datetime, float, float]] | None = None,
        now: datetime | None = None,
    ) -> None:
        stored = {r["hour_utc"]: r for r in hours}
        for row in live or []:
            # Live rows cover hours the hourly rollup has not reached yet.
            if row["hour_utc"] not in stored:
                stored[row["hour_utc"]] = {**row, "partial": True}
        self.hours = [stored[k] for k in sorted(stored)]
        self.registers = {end: (d, n) for end, d, n in (registers or [])}
        self.now = now or datetime.now(UTC)
        self.esb_hour: dict[datetime, float] = defaultdict(float)
        self.esb_day_count: dict[str, int] = defaultdict(int)
        self.esb_day_kwh: dict[str, float] = defaultdict(float)
        for start, kwh, _band in esb or []:
            hour = start.replace(minute=0, second=0, microsecond=0)
            self.esb_hour[hour] += kwh
            d = start.astimezone(IRELAND).date().isoformat()
            self.esb_day_count[d] += 1
            self.esb_day_kwh[d] += kwh

    def build(
        self, period: str, start: date | None = None, end: date | None = None
    ) -> dict[str, Any]:
        if period not in PERIODS:
            raise ValueError(f"period must be one of {PERIODS}")
        # No dates means everything. The card computes its own default windows and
        # always sends them, so an empty request is the explicit "Everything" preset.
        groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        for row in self.hours:
            local_day = row["hour_utc"].astimezone(IRELAND).date()
            if start and local_day < start:
                continue
            if end and local_day > end:
                continue
            groups.setdefault(period_key(period, row["hour_utc"]), []).append(row)

        rows = [self._row(period, key, members) for key, members in groups.items()]
        rows.reverse()  # newest first

        grand = Totals(label="Total")
        esb_total = diff_total = shelly_on_esb = 0.0
        esb_any = False
        for members in groups.values():
            for m in members:
                grand.add(m)
        for r in rows:
            if r.get("esb_kwh") is not None:
                esb_any = True
                esb_total += r["esb_kwh"]
            if r.get("esb_diff_kwh") is not None:
                diff_total += r["esb_diff_kwh"]
                shelly_on_esb += r["_esb_overlap_kwh"]
        totals = self._format_totals(grand, period)
        if esb_any:
            totals["esb_kwh"] = round(esb_total, 3)
            totals["esb_diff_kwh"] = round(diff_total, 3)
            base = shelly_on_esb - diff_total
            totals["esb_diff_pct"] = round(diff_total / base * 100, 2) if base else None
        for r in rows:
            r.pop("_esb_overlap_kwh", None)

        note = None
        if rows and rows[0].get("partial"):
            note = (
                "The newest row is still in progress: it includes live readings from "
                "the clamps that become a stored hour when the hour completes."
            )
        days = [r["hour_utc"].astimezone(IRELAND).date() for m in groups.values() for r in m]
        actual_start = start or (min(days) if days else None)
        actual_end = end or (max(days) if days else None)
        return {
            "period": period,
            "start": actual_start.isoformat() if actual_start else None,
            "end": actual_end.isoformat() if actual_end else None,
            "columns": [c.as_dict() for c in _cols(period)],
            "rows": rows,
            "totals": totals if rows else None,
            "note": note,
        }

    # ------------------------------------------------------------------ rows

    def _row(self, period: str, key: str, members: list[dict[str, Any]]) -> dict[str, Any]:
        t = Totals()
        for m in members:
            t.add(m)
        row = self._format_totals(t, period)
        row["key"] = key
        row["label"] = period_label(period, key, members[0].get("local_hour"))
        row["final_hours"] = sum(1 for m in members if m.get("meter_source") == "esb")
        row["partial"] = any(m.get("partial") for m in members) or self._in_progress(period, key)

        def agg(key: str, how) -> float | None:
            values = [float(m[key]) for m in members if m.get(key) is not None]
            return round(how(values), 2) if values else None

        mean = lambda v: sum(v) / len(v)  # noqa: E731
        for key, src, how in (
            ("voltage_avg", "voltage_avg", mean),
            ("hall_voltage_min", "hall_voltage_min", min),
            ("hall_voltage_avg", "hall_voltage_avg", mean),
            ("hall_voltage_max", "hall_voltage_max", max),
            ("house_current_avg", "house_current_avg", mean),
            ("house_current_max", "house_current_max", max),
            ("hall_current_avg", "hall_current_avg", mean),
            ("hall_current_max", "hall_current_max", max),
            ("frequency", "frequency_avg", mean),
            ("hall_frequency", "hall_frequency_avg", mean),
            ("hall_factor", "hall_factor", mean),
        ):
            row[key] = agg(src, how)

        last_end = members[-1]["hour_utc"] + timedelta(hours=1)
        regs = self.registers.get(last_end)
        row["register_day"] = None if regs is None else round(regs[0], 1)
        row["register_night"] = None if regs is None else round(regs[1], 1)

        if period == "hour":
            m = members[0]
            row["band"] = "Night" if m.get("band") == BAND_NIGHT else "Day"
            row["source"] = "ESB meter (no clamp data)" if m.get("source") == "esb" else "Clamp"
            row["priced_on"] = (
                "ESB meter (final)" if m.get("meter_source") == "esb"
                else "Clamp, live" if m.get("partial") else "Clamp (until ESB has it)"
            )
            row["unit_rate"] = m.get("unit_rate_eur")
            row["avg_w"] = m.get("house_avg_w")
            row["min_w"] = m.get("house_min_w")
            row["hall_avg_w"] = m.get("hall_avg_w")
            row["house_pf"] = m.get("house_pf")
            row["hall_pf"] = m.get("hall_pf")
            esb = self.esb_hour.get(m["hour_utc"])
            row["esb_kwh"] = None if esb is None else round(esb, 3)
            if esb is not None and m.get("house_kwh") is not None and not m.get("partial"):
                diff = float(m["house_kwh"]) - esb
                row["esb_diff_kwh"] = round(diff, 3)
                row["esb_diff_pct"] = round(diff / esb * 100, 1) if esb else None
                row["_esb_overlap_kwh"] = float(m["house_kwh"])
            else:
                row["esb_diff_kwh"] = row["esb_diff_pct"] = None
                row["_esb_overlap_kwh"] = 0.0
            return row

        # day / month / year: ESB compared only over days complete on both sides.
        day_hours: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for m in members:
            day_hours[m["local_date"]].append(m)
        esb_sum = shelly_sum = 0.0
        esb_seen = False
        for d, hrs in day_hours.items():
            if d in self.esb_day_kwh:
                esb_seen = True
            if self.esb_day_count.get(d, 0) >= ESB_COMPLETE_DAY and len(hrs) >= 23 and not any(
                h.get("partial") for h in hrs
            ):
                esb_sum += self.esb_day_kwh[d]
                shelly_sum += sum(float(h.get("house_kwh") or 0.0) for h in hrs)
        row["esb_kwh"] = (
            round(sum(self.esb_day_kwh[d] for d in day_hours if d in self.esb_day_kwh), 3)
            if esb_seen
            else None
        )
        if esb_sum:
            row["esb_diff_kwh"] = round(shelly_sum - esb_sum, 3)
            row["esb_diff_pct"] = round((shelly_sum - esb_sum) / esb_sum * 100, 2)
        else:
            row["esb_diff_kwh"] = row["esb_diff_pct"] = None
        row["_esb_overlap_kwh"] = shelly_sum
        hall = [float(m["hall_avg_w"]) for m in members if m.get("hall_avg_w") is not None]
        row["hall_avg_w"] = round(sum(hall) / len(hall), 0) if hall else None
        row["avg_w"] = round(t.house_kwh * 1000 / t.hours, 0) if t.hours else None
        row["min_w"] = None if t.min_w is None else round(t.min_w, 0)
        return row

    def _in_progress(self, period: str, key: str) -> bool:
        return period_key(period, self.now - timedelta(seconds=1)) == key and period != "hour"

    @staticmethod
    def _format_totals(t: Totals, period: str) -> dict[str, Any]:
        d = t.as_dict()
        return {
            "label": t.label,
            "house_kwh": d["house_kwh"],
            "meter_kwh": d["meter_kwh"],
            "day_kwh": d["day_kwh"],
            "night_kwh": d["night_kwh"],
            "hall_kwh": d["hall_kwh"],
            "rest_kwh": d["rest_kwh"],
            "night_share_pct": d["night_share_pct"],
            "hall_share_pct": d["hall_share_pct"],
            "energy_cost": round(t.energy_cost, 4),
            "fixed_cost": round(t.fixed_cost, 4),
            "total_cost": round(t.total_cost, 4),
            "hall_cost": round(t.hall_cost, 4),
            "rest_cost": round(t.rest_cost, 4),
            "day_cost": round(t.day_cost, 4),
            "night_cost": round(t.night_cost, 4),
            "effective_rate": d["effective_rate"],
            "peak_w": d["peak_w"],
            "house_pf": d["house_pf"],
            "hall_pf": d["hall_pf"],
            "voltage_min": d["voltage_min"],
            "voltage_max": d["voltage_max"],
            "hours": t.hours,
            "hall_over_house_hours": t.hall_over_house_hours,
        }


SUBMISSION_COLUMNS = [
    Column("created", "Logged", None, None, "When"),
    Column("read_at", "Reading time", None, None, "When"),
    Column("day_kwh", "Day", "kWh", 0, "Reading"),
    Column("night_kwh", "Night", "kWh", 0, "Reading"),
    Column("origin", "From", None, None, "Reading"),
    Column("mode", "Mode", None, None, "Result"),
    Column("status", "Status", None, None, "Result"),
    Column("message", "Message", None, None, "Result"),
]


def submissions_table(rows: list[dict[str, Any]], fmt: Callable[[str], str]) -> dict[str, Any]:
    return {
        "columns": [c.as_dict() for c in SUBMISSION_COLUMNS],
        "rows": [
            {
                "key": r["created_at"],
                "label": fmt(r["created_at"]),
                "created": fmt(r["created_at"]),
                "read_at": fmt(r["read_at"]),
                "day_kwh": r["day_kwh"],
                "night_kwh": r["night_kwh"],
                "origin": r["origin"],
                "mode": r["mode"],
                "status": r["status"],
                "message": r.get("message"),
            }
            for r in rows
        ],
        "note": "Readings are only ever sent when you press the button. SSE accepts one per 24 hours.",
    }
