"""The next bill, built the way the supplier builds it.

NO HOME ASSISTANT IMPORTS.

A bill is: day units x day rate, night units x night rate, a standing charge per day,
the PSO levy per day, and VAT. Every figure here is already INCLUDING VAT (as on the
price sheet), so VAT is shown as the part of the total it is (total x 9/109), not added.

Units are METER units: the clamp's kWh times the meter factors learned from real
readings, because the bill is what the meter counted. The period is the supplier's
own, read from the SSE account (last bill to next bill date), or from a billing date
entered in the options.

So far = what the stored hours and today's live readings say; to come = the ensemble
forecast for the remaining days; the range is the forecast's 80% interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .sse.client import normalise_latest_bill

VAT_RATE = 0.09
_CLOSING_READ_WINDOW_DAYS = 14


@dataclass
class BillEstimate:
    start: date
    end: date            # inclusive
    source: str
    day_kwh: float
    night_kwh: float
    day_cost: float
    night_cost: float
    standing: float
    pso: float
    so_far: float
    low: float
    high: float
    days: int
    days_elapsed: int
    contract_end: str | None = None
    next_bill_date: str | None = None
    vat_rate: float = VAT_RATE
    # Days of the period before the first tariff period: no price is known for them,
    # so neither their units nor their standing charge/PSO are in the figures.
    unpriced_days: int = 0

    @property
    def total(self) -> float:
        return self.day_cost + self.night_cost + self.standing + self.pso

    @property
    def vat(self) -> float:
        return self.total * self.vat_rate / (1 + self.vat_rate)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "period_label": f"{self.start:%-d %b} – {self.end:%-d %b %Y}, day {self.days_elapsed} of {self.days}",
            "source": self.source,
            "days": self.days,
            "days_elapsed": self.days_elapsed,
            "day_kwh": round(self.day_kwh, 0),
            "night_kwh": round(self.night_kwh, 0),
            "kwh": round(self.day_kwh + self.night_kwh, 0),
            "total": round(self.total, 2),
            "day_cost": round(self.day_cost, 2),
            "night_cost": round(self.night_cost, 2),
            "standing": round(self.standing, 2),
            "pso": round(self.pso, 2),
            "vat": round(self.vat, 2),
            "vat_pct": round(self.vat_rate * 100, 1),
            "so_far": round(self.so_far, 2),
            "low": round(self.low, 2),
            "high": round(self.high, 2),
            "contract_end": self.contract_end,
            "next_bill_date": self.next_bill_date,
            "unpriced_days": self.unpriced_days,
            "note": (
                f"{self.unpriced_days} day(s) of this period are before the first tariff period and "
                "are not priced; add that tariff (electricity_analyzer.add_tariff) to include them"
            )
            if self.unpriced_days
            else None,
        }


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _closing_read_date(
    bill_date: date,
    history: list[dict[str, Any]] | None,
    *,
    after: date | None = None,
) -> date | None:
    """Latest accepted SSE reading within the billing window up to `bill_date`."""
    candidates: list[date] = []
    earliest = bill_date - timedelta(days=_CLOSING_READ_WINDOW_DAYS)
    for row in history or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip().lower() == "rejected":
            continue
        d = _parse_date(row.get("date"))
        if d is None or d < earliest or d > bill_date:
            continue
        if after is not None and d <= after:
            continue
        candidates.append(d)
    return max(candidates) if candidates else None


def resolve_last_bill(
    billing: dict[str, Any] | None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Resolve the most recently issued SSE bill's period and euro/unit figures, or None."""
    if not billing:
        return None
    transactions = billing.get("transactions") if isinstance(billing.get("transactions"), list) else None
    norm = normalise_latest_bill(billing.get("latest_bill"), transactions)

    explicit_end = _parse_date(norm.get("period_end") if norm else None) or _parse_date(billing.get("last_bill_end"))
    explicit_start = _parse_date(norm.get("period_start") if norm else None)
    bill_date = _parse_date(norm.get("bill_date") if norm else None) or _parse_date(billing.get("last_bill_date"))

    bill_tx_dates = sorted(
        {
            d
            for tx in (transactions or [])
            if isinstance(tx, dict)
            and not tx.get("reversed")
            and str(tx.get("transactionDescription") or "").strip().lower() == "bill"
            and str(tx.get("utilityType") or "E").strip().upper() in ("", "E", "ELECTRICITY")
            and (d := _parse_date(tx.get("transactionDate"))) is not None
        },
        reverse=True,
    )
    if bill_date is None and bill_tx_dates:
        bill_date = bill_tx_dates[0]

    if bill_date is None and explicit_end is None:
        return None

    supply_start = _parse_date(billing.get("supply_start"))
    if explicit_end is not None:
        end = explicit_end
    else:
        assert bill_date is not None
        end = _closing_read_date(bill_date, history, after=supply_start) or (bill_date - timedelta(days=1))

    earlier_bills = [d for d in bill_tx_dates if bill_date is not None and d < bill_date]
    prev_close: date | None = None
    if explicit_start is not None:
        start = explicit_start
        prev_close = start - timedelta(days=1)
    elif earlier_bills:
        prev_bill = earlier_bills[0]
        prev_close = _closing_read_date(prev_bill, history, after=supply_start) or (prev_bill - timedelta(days=1))
        start = prev_close + timedelta(days=1)
    elif supply_start is not None and supply_start <= end:
        start = supply_start
        prev_close = supply_start
    else:
        start = end - timedelta(days=60)
        prev_close = start - timedelta(days=1)

    billed_day_kwh: float | None = None
    billed_night_kwh: float | None = None
    if history and prev_close is not None:
        day_units: list[float] = []
        night_units: list[float] = []
        for row in history:
            if not isinstance(row, dict) or str(row.get("status") or "").strip().lower() == "rejected":
                continue
            d = _parse_date(row.get("date"))
            units = row.get("units_used")
            if d is None or units is None or not (prev_close < d <= end):
                continue
            reg = str(row.get("register") or "").strip().lower()
            if reg == "day":
                day_units.append(float(units))
            elif reg == "night":
                night_units.append(float(units))
        if day_units or night_units:
            billed_day_kwh = sum(day_units)
            billed_night_kwh = sum(night_units)

    return {
        "bill_date": bill_date,
        "start": start,
        "end": end,
        "electricity_eur": norm.get("electricity_eur") if norm else None,
        "invoice_eur": norm.get("invoice_eur") if norm else None,
        "balance_after_eur": norm.get("balance_after_eur") if norm else None,
        "balance_after_status": norm.get("balance_after_status") if norm else None,
        "previous_balance_eur": norm.get("previous_balance_eur") if norm else None,
        "previous_balance_status": norm.get("previous_balance_status") if norm else None,
        "billed_day_kwh": billed_day_kwh,
        "billed_night_kwh": billed_night_kwh,
    }


def period_from_sse(
    billing: dict[str, Any] | None,
    today: date,
    history: list[dict[str, Any]] | None = None,
) -> tuple[date, date, dict[str, Any]] | None:
    """(start, end inclusive, extras) from SSE's account data, or None."""
    if not billing or not billing.get("next_bill_date"):
        return None
    next_bill = _parse_date(billing["next_bill_date"])
    if next_bill is None:
        return None
    start = None
    last = resolve_last_bill(billing, history)
    if last is not None:
        start = last["end"] + timedelta(days=1)
    elif supply := _parse_date(billing.get("supply_start")):
        start = supply
    if start is None or start > today:
        start = next_bill - timedelta(days=61)
    end = next_bill - timedelta(days=1)
    if end < start:
        end = max(today, start + timedelta(days=60))
    elif end < today:
        end = today
    return start, end, {"contract_end": billing.get("contract_end"), "next_bill_date": next_bill.isoformat()}


def summarize_last_bill(
    *,
    billing: dict[str, Any] | None,
    history: list[dict[str, Any]] | None,
    anchor_prev: tuple[date, date] | None,
    today: date,
    rows: list[dict[str, Any]],
    schedule,
    factors: dict[str, float],
) -> dict[str, Any] | None:
    """Summary of the most recently completed bill (SSE statement or prior anchor cycle)."""
    resolved = resolve_last_bill(billing, history)
    if resolved is not None:
        start, end = resolved["start"], resolved["end"]
        source = "SSE Airtricity"
    elif anchor_prev is not None:
        start, end = anchor_prev
        if not any(start.isoformat() <= (r.get("local_date") or "") <= end.isoformat() for r in rows):
            return None
        source = "billing date in options"
    else:
        return None

    period_rows = [r for r in rows if start.isoformat() <= (r.get("local_date") or "") <= end.isoformat()]
    metered = estimate(
        start=start,
        end=end,
        today=max(today, end + timedelta(days=1)),
        rows=period_rows,
        live_today={},
        schedule=schedule,
        night_share={},
        factors=factors,
        next_days=[],
        today_remaining_kwh=0.0,
        source=source,
    )
    has_metered = bool(period_rows) and metered.days > metered.unpriced_days
    billed_eur = resolved.get("electricity_eur") if resolved else None
    if billed_eur is None and resolved is not None:
        billed_eur = resolved.get("invoice_eur")
    total = billed_eur if billed_eur is not None else (metered.total if has_metered else None)
    if total is None:
        return None

    day_kwh = (
        resolved["billed_day_kwh"]
        if resolved and resolved.get("billed_day_kwh") is not None
        else metered.day_kwh
    )
    night_kwh = (
        resolved["billed_night_kwh"]
        if resolved and resolved.get("billed_night_kwh") is not None
        else metered.night_kwh
    )
    days = (end - start).days + 1
    return {
        "bill_date": resolved["bill_date"].isoformat() if resolved and resolved.get("bill_date") else None,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
        "period_label": f"{start:%-d %b} – {end:%-d %b %Y} ({days} days)",
        "source": source,
        "total": round(float(total), 2),
        "billed_eur": round(float(billed_eur), 2) if billed_eur is not None else None,
        "metered_total": round(metered.total, 2) if has_metered else None,
        "day_kwh": round(float(day_kwh), 0),
        "night_kwh": round(float(night_kwh), 0),
        "kwh": round(float(day_kwh + night_kwh), 0),
        "metered_day_kwh": round(metered.day_kwh, 0) if has_metered else None,
        "metered_night_kwh": round(metered.night_kwh, 0) if has_metered else None,
        "day_cost": round(metered.day_cost, 2) if has_metered else None,
        "night_cost": round(metered.night_cost, 2) if has_metered else None,
        "standing": round(metered.standing, 2) if has_metered else None,
        "pso": round(metered.pso, 2) if has_metered else None,
        "vat": round(float(total) * VAT_RATE / (1 + VAT_RATE), 2),
        "vat_pct": round(VAT_RATE * 100, 1),
        "previous_balance_eur": resolved.get("previous_balance_eur") if resolved else None,
        "previous_balance_status": resolved.get("previous_balance_status") if resolved else None,
        "balance_after_eur": resolved.get("balance_after_eur") if resolved else None,
        "balance_after_status": resolved.get("balance_after_status") if resolved else None,
    }


def estimate(
    *,
    start: date,
    end: date,
    today: date,
    rows: list[dict[str, Any]],
    live_today: dict[str, Any],
    schedule,
    night_share: dict[int, float],
    factors: dict[str, float],
    next_days,
    today_remaining_kwh: float,
    source: str,
    extras: dict[str, Any] | None = None,
) -> BillEstimate:
    """Price [start, end] like a bill: stored hours, today's live total, then the forecast."""
    extras = extras or {}
    day_kwh = night_kwh = day_cost = night_cost = 0.0
    for r in rows:
        if r.get("local_date") is None or r["local_date"] >= today.isoformat():
            continue
        if r["local_date"] < start.isoformat() or r["local_date"] > end.isoformat():
            continue
        meter = float(r.get("meter_kwh") if r.get("meter_kwh") is not None else r.get("house_kwh") or 0.0)
        cost = float(r.get("house_cost_eur") or 0.0)
        if r.get("band") == "night":
            night_kwh += meter
            night_cost += cost
        else:
            day_kwh += meter
            day_cost += cost
    # Today so far, from the live view (already meter-priced).
    if start <= today <= end:
        live_day = float(live_today.get("day_kwh") or 0.0) * factors.get("day", 1.0)
        live_night = float(live_today.get("night_kwh") or 0.0) * factors.get("night", 1.0)
        day_kwh += live_day
        night_kwh += live_night
        day_cost += float(live_today.get("day_cost") or 0.0)
        night_cost += float(live_today.get("night_cost") or 0.0)
    so_far_energy = day_cost + night_cost

    low = high = 0.0
    remaining = [(today, today_remaining_kwh, today_remaining_kwh, today_remaining_kwh)] if start <= today <= end else []
    remaining += [(d.day, d.kwh, d.low, d.high) for d in next_days if today < d.day <= end]
    for day, kwh, lo, hi in remaining:
        tariff = schedule.in_force(day)
        if tariff is None:
            continue
        ns = night_share.get(day.month, 0.33)
        dk, nk = kwh * (1 - ns) * factors.get("day", 1.0), kwh * ns * factors.get("night", 1.0)
        day_kwh += dk
        night_kwh += nk
        day_cost += dk * tariff.day_rate
        night_cost += nk * tariff.night_rate
        blended = ((1 - ns) * tariff.day_rate * factors.get("day", 1.0) + ns * tariff.night_rate * factors.get("night", 1.0))
        low += (lo - kwh) * blended
        high += (hi - kwh) * blended

    days = (end - start).days + 1
    elapsed = max(min((today - start).days + 1, days), 0)
    standing = pso = fixed_so_far = 0.0
    unpriced = 0
    for i in range(days):
        d = start + timedelta(days=i)
        t = schedule.in_force(d)
        if t is None:
            unpriced += 1
            continue
        standing += t.standing_for_day(d)
        pso += t.pso_for_day(d)
        if d <= today:  # a day's fixed charge is due from its first second
            fixed_so_far += t.fixed_for_day(d)
    est = BillEstimate(
        start=start,
        end=end,
        source=source,
        day_kwh=day_kwh,
        night_kwh=night_kwh,
        day_cost=day_cost,
        night_cost=night_cost,
        standing=standing,
        pso=pso,
        so_far=so_far_energy + fixed_so_far,
        low=0.0,
        high=0.0,
        days=days,
        days_elapsed=elapsed,
        contract_end=extras.get("contract_end"),
        next_bill_date=extras.get("next_bill_date"),
        unpriced_days=unpriced,
    )
    est.low = est.total + low
    est.high = est.total + high
    return est
