"""Keep the hall clamp honest: it can never really read more than the whole house.

NO HOME ASSISTANT IMPORTS.

WHY THE HALL CLAMP OVER-READS, AND WHY IT DEPENDS ON POWER FACTOR

A CT clamp has a small gain error and a small PHASE error. Real power is
V x I x cos(phase angle), so a phase error of δ turns cos(φ) into cos(φ - δ): almost no
difference for a resistive load (φ ≈ 0), but a real one for a reactive load. At the
whole house's typical PF of 0.96 one degree of phase error is about 0.5% of real power;
at the hall's 0.79 (pumps, ballasts) it is about 1.3%, and when only the pumps run
(PF ≈ 0.6) it is about 2.3%. So when the rest of the house is idle the hall clamp can
read more than the whole house, which is physically impossible: the hall circuit is
inside the house.

A second, smaller cause is timing: the two channels report independently.

THE CORRECTION: A PHASE ERROR, SOLVED FROM THE PHYSICS

The full-house clamp is tied to the utility meter by the reading-based learner. The
hall clamp has no meter, but it has a hard bound at every instant:

    true hall power  <=  true house power

For each pair of readings taken within ALIGN_SECONDS of each other, the hall's own
power factor gives its phase angle φ = acos(W / VA). The correction is the smallest
phase error δ for which the corrected hall power, VA x cos(φ + δ), stays at or below
the house's at the 99.5th percentile of all aligned readings. Each stored hour then
gets the correction for ITS OWN power factor:

    hall factor(pf) = cos(acos(pf) + δ) / pf

so an hour when only the pumps ran is corrected more than an hour when the heaters
dominated, exactly as the error itself behaves. δ is a lower bound (it assumes the rest
of the house drew nothing at its lowest moment), so it never over-corrects; it is
capped at 8 degrees; and a hall that reads LOW cannot be detected this way, so nothing
is ever scaled up.

Without an apparent-power sensor the same bound is solved as a flat gain instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

ALIGN_SECONDS = 2.0
MIN_HOUSE_W = 100.0
NEAR_IDLE = 0.80          # only moments when the hall is >= 80% of the house can say anything
WINDOW_DAYS = 14
QUANTILE = 0.995
MIN_SAMPLES = 300
MAX_DELTA_DEG = 8.0
MAX_GAIN_CORRECTION = 0.10
DECIMATE_SECONDS = 10.0
RESERVOIR_PER_DAY = 2000


def phase_factor(pf: float | None, delta_deg: float) -> float:
    """Multiplier on measured hall W for a phase error of `delta_deg` at power factor `pf`."""
    if pf is None or delta_deg <= 0 or pf <= 0:
        return 1.0
    pf = min(pf, 1.0)
    corrected = math.cos(min(math.acos(pf) + math.radians(delta_deg), math.pi / 2))
    return max(min(corrected / pf, 1.0), 0.0)


@dataclass
class HallCorrection:
    """Aligned-sample statistics per day, and the correction they imply."""

    total: dict[str, int] = field(default_factory=dict)       # aligned samples per day
    over: dict[str, int] = field(default_factory=dict)        # raw hall > house per day
    near: dict[str, int] = field(default_factory=dict)        # near-idle samples seen per day
    samples: dict[str, list[list[float]]] = field(default_factory=dict)  # kept: [house_w, hall_w, hall_va]
    _last_kept: float = 0.0

    def observe(self, house_w: float, hall_w: float, hall_va: float | None, day: str, t: float) -> None:
        if house_w < MIN_HOUSE_W or hall_w <= 0:
            return
        self.total[day] = self.total.get(day, 0) + 1
        ratio = hall_w / house_w
        if ratio > 1.0:
            self.over[day] = self.over.get(day, 0) + 1
        if ratio < NEAR_IDLE:
            return
        self.near[day] = self.near.get(day, 0) + 1
        if t - self._last_kept < DECIMATE_SECONDS:
            return
        kept = self.samples.setdefault(day, [])
        if len(kept) >= RESERVOIR_PER_DAY:
            return
        self._last_kept = t
        kept.append([round(house_w, 1), round(hall_w, 1), round(hall_va, 1) if hall_va else 0.0])

    def prune(self, today: date) -> None:
        keep = {(today - timedelta(days=i)).isoformat() for i in range(WINDOW_DAYS + 1)}
        for store in (self.total, self.over, self.near, self.samples):
            for d in [d for d in store if d not in keep]:
                del store[d]

    # ---------------------------------------------------------------- solving

    def _weighted(self) -> list[tuple[float, float, float, float]]:
        """(house_w, hall_w, hall_va, weight): each kept sample stands for near/kept seen."""
        out = []
        for day, kept in self.samples.items():
            if not kept:
                continue
            w = self.near.get(day, len(kept)) / len(kept)
            out.extend((h, a, s, w) for h, a, s in kept)
        return out

    def _allowed(self) -> float:
        return (1 - QUANTILE) * sum(self.total.values())

    def _exceed(self, samples, delta: float | None = None, gain: float | None = None) -> float:
        n = 0.0
        for house, hall, va, w in samples:
            if delta is not None and va > 0:
                corrected = va * math.cos(min(math.acos(min(hall / va, 1.0)) + math.radians(delta), math.pi / 2))
            else:
                corrected = hall * (gain if gain is not None else 1.0)
            if corrected > house:
                n += w
        return n

    @property
    def ready(self) -> bool:
        return sum(self.total.values()) >= MIN_SAMPLES

    def delta(self) -> float:
        """Phase error in degrees (0 when the readings never break the bound)."""
        samples = [s for s in self._weighted() if s[2] > 0]
        if not self.ready or not samples:
            return 0.0
        allowed = self._allowed()
        if self._exceed(samples, delta=0.0) <= allowed:
            return 0.0
        lo, hi = 0.0, MAX_DELTA_DEG
        if self._exceed(samples, delta=hi) > allowed:
            return MAX_DELTA_DEG
        for _ in range(30):
            mid = (lo + hi) / 2
            if self._exceed(samples, delta=mid) > allowed:
                lo = mid
            else:
                hi = mid
        return round(hi, 2)

    def gain(self) -> float:
        """Fallback when there is no apparent power: a flat scale on the hall clamp."""
        samples = self._weighted()
        if not self.ready or not samples:
            return 1.0
        allowed = self._allowed()
        if self._exceed(samples, gain=1.0) <= allowed:
            return 1.0
        lo, hi = 1.0 - MAX_GAIN_CORRECTION, 1.0
        for _ in range(30):
            mid = (lo + hi) / 2
            if self._exceed(samples, gain=mid) > allowed:
                hi = mid
            else:
                lo = mid
        return round(lo, 4)

    def model(self) -> dict[str, float | str]:
        has_va = any(s[2] > 0 for kept in self.samples.values() for s in kept)
        if has_va:
            return {"kind": "phase", "delta_deg": self.delta(), "gain": 1.0}
        return {"kind": "gain", "delta_deg": 0.0, "gain": self.gain()}

    def as_dict(self) -> dict[str, Any]:
        total = sum(self.total.values())
        over = sum(self.over.values())
        model = self.model()
        return {
            **model,
            "aligned_samples": total,
            "near_idle_samples": sum(self.near.values()),
            "hall_over_house_samples": over,
            "hall_over_house_pct": round(over / total * 100, 3) if total else None,
            "factor_at_pf_0_6": round(phase_factor(0.6, float(model["delta_deg"])), 4),
            "factor_at_pf_0_8": round(phase_factor(0.8, float(model["delta_deg"])), 4),
            "factor_at_pf_1_0": 1.0,
            "window_days": WINDOW_DAYS,
        }

    def to_json(self) -> dict[str, Any]:
        return {"total": self.total, "over": self.over, "near": self.near, "samples": self.samples}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> HallCorrection:
        data = data or {}
        return cls(
            total={k: int(v) for k, v in (data.get("total") or {}).items()},
            over={k: int(v) for k, v in (data.get("over") or {}).items()},
            near={k: int(v) for k, v in (data.get("near") or {}).items()},
            samples={k: [list(map(float, s)) for s in v] for k, v in (data.get("samples") or {}).items()},
        )


def hour_factor(model: dict[str, Any], hall_w: float | None, hall_va: float | None) -> float:
    """The correction for one hour (or one instant), from that period's own power factor."""
    if model.get("kind") == "phase":
        if not hall_w or not hall_va:
            return 1.0
        return phase_factor(hall_w / hall_va, float(model.get("delta_deg") or 0.0))
    return float(model.get("gain") or 1.0)
