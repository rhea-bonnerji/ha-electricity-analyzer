"""The running integration: one object that owns the data and every moving part.

    Shelly state pushes (≈1 s) ──► LiveTracker ──► live coordinator (sensors, ≤5 s)
                                                     today so far, cost rate, registers now
    recorder hourly statistics ──► hourly rollup (HH:03) ──► SQLite store
                                                     ──► long-term statistics (charts)
    ESB Networks ──────────────────► esb_intervals / register reads (12 h, jittered)
    store + live ──► insights coordinator (5 min): summaries, forecast, ESB check
    button press ──► SSE Airtricity meter reading (never automatic)

Everything blocking (SQLite, the forecaster over a year of hours) runs in the
executor. Everything that talks to a portal is single-attempt with a persisted
backoff, and never runs on startup unless it is due.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
import logging
from pathlib import Path
import random
from datetime import date, datetime, timedelta, timezone
from functools import partial
from typing import Any, Callable

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue, async_delete_issue
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import longterm, recorder_io
from .const import (
    CONF_BILL_ANCHOR,
    CONF_ESB_MODE,
    CONF_ESB_PASSWORD,
    CONF_ESB_PER_DAY,
    DEFAULT_ESB_PER_DAY,
    ESB_MAX_SIGN_INS,
    MAX_ESB_PER_DAY,
    CONF_ESB_USERNAME,
    CONF_HALL,
    CONF_HOUSE,
    CONF_MPRN,
    CONF_READING_AT,
    CONF_READING_DAY,
    CONF_READING_NIGHT,
    CONF_SSE_ACCOUNT,
    CONF_SSE_MODE,
    CONF_SSE_PASSWORD,
    CONF_SSE_SYNC,
    CONF_WEATHER_ENTITY,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_SSE_USERNAME,
    DATA_ESB_HANDOFF,
    DOMAIN,
    ESB_ACCOUNT,
    ESB_OFF,
    M_APPARENT,
    M_ENERGY,
    M_POWER,
    M_VOLTAGE,
    MEASUREMENTS,
    RETRY_AFTER_FAILURE,
    ROLLUP_LOOKBACK,
    ROLLUP_MINUTE,
    SSE_COOLDOWN,
    SSE_DRY_RUN,
    SSE_LIVE,
    SSE_SYNC_INTERVAL,
    WEATHER_STATISTICS,
    SSE_OFF,
    conf_entity,
)
from .engine import analytics, forecast as fc, reconcile as rc, registers as reg
from .engine import calibration as calib
from .engine import bill as billing, clock, downtime, ensemble as ens, learner, weather as wx
from .engine.hallfix import HallCorrection, hour_factor
from .engine.errors import AuthFailed, CaptchaEncountered, PortalError, SessionExpired
from .engine.esb.client import EsbClient, new_cookie_jar
from .engine.esb.hdf import parse_hdf
from .engine.esb.reads import parse_billed_reads
from .engine.hourly import book_fixed, build_hour, reprice
from .engine.live import LiveTracker
from .engine.minutes import MinuteBuffer
from .engine import durability
from .engine.sse.client import MeterInfo, PortalConfig, SseClient, SubmissionBlocked, build_payload, sanitize_billing
from .engine.store import Store
from .engine.tables import TableBuilder, submissions_table
from .engine.tariff import (
    BAND_NIGHT,
    DEFAULT_TARIFF,
    IRELAND,
    LEGACY_DEFAULT_PSO,
    LEGACY_DEFAULT_VALID_FROM,
    Tariff,
    TariffSchedule,
    floor_hour,
    local_date,
    local_midnight,
)
from .tariff_config import tariff_from_config

LOG = logging.getLogger(__name__)
UTC = timezone.utc
HOUR = timedelta(hours=1)

META_SSE_METER = "sse_meter_info"
META_ESB_WATERMARK = "esb_data_window_end"
META_ESB_SIGN_INS = "esb_sign_ins"
META_PRICED_WITH = "priced_with"
# The plan from setup / the options as it was last written into the stored periods.
META_CONFIGURED_TARIFF = "configured_tariff"
# How hours are priced, as code: bumping it re-prices every stored hour once on start.
# 2 (1.0.2): no price before the first tariff; a day's fixed charge in full on its first hour.
PRICING_RULES = 2
META_ANCHOR_SPLIT = "anchor_hour_after_kwh"
META_SSE_SYNCED = "sse_last_sync"
META_HALL_GAIN = "hall_correction_samples"
META_SSE_BILLING = "sse_billing"
META_SSE_HISTORY = "sse_reading_history"
META_SSE_CONFIG = "sse_portal_config"
META_WEATHER_FORECAST_AT = "weather_forecast_fetched"
META_WEATHER_REQUESTS = "weather_requests"
META_WEATHER_LOCATION = "weather_location"
META_WEATHER_OK = "weather_last_success"
# Weather history fetched whatever the electricity data covers: two years, so every chart
# (up to 24 months) and every calendar day's normal has it (two requests, once).
WEATHER_HISTORY_DAYS = 730
# Move the pin further than this (about 5 km) and the weather is fetched afresh for the new place.
WEATHER_MOVE_DEG = 0.05
META_CLOCK = "clock_check"
META_BOOTSTRAPPED = "forecast_bootstrapped"
META_ESB_RATIO = "esb_ratio"
# Which ESB / SSE sign-in details the current failure streak belongs to (a hash only):
# new details are a new start and need not sit out the previous details' twelve hours.
META_CREDENTIALS = "credentials_fingerprint"
WEATHER_INTERVAL = timedelta(hours=12)
CLOCK_INTERVAL = timedelta(hours=6)
CLOCK_WARN_SECONDS = 2.0
LIVE_PUSH = timedelta(seconds=5)
INSIGHT_INTERVAL = timedelta(minutes=5)
ESB_JITTER = 900
# How close a submitted reading must be to the integration's own estimate before it
# is sent without the user explicitly overriding: whichever is larger of 25 units or
# 5% of what has been used since the anchor reading.
ESTIMATE_MARGIN_UNITS = 25
ESTIMATE_MARGIN_SHARE = 0.05


def _sse_time(text: str | None) -> datetime | None:
    """SSE's timestamps ("07/10/2026 18:50:34", Irish time) as UTC."""
    stamp = str(text or "").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=IRELAND).astimezone(UTC)
        except ValueError:
            continue
    return None


def _describe(exc: BaseException) -> str:
    """An error's text, or its type when it has none (a timeout has no text)."""
    return str(exc) or type(exc).__name__


class TariffPeriodError(HomeAssistantError):
    """A tariff period that cannot be set as asked. `code` is the form's error key."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _local_time(when: datetime | None) -> str:
    return "-" if when is None else when.astimezone(IRELAND).strftime("%a %-d %b %H:%M")


def _legacy_default_row(stored: list[Tariff], configured: Tariff) -> Tariff | None:
    """The configured plan as 1.0.x stored it under its old default start (5 October,
    possibly with the old €6.67 PSO), when the plan really starts on 6 October.

    Only that exact case: the configured period must start on the corrected date and
    the stored one on the old default date, with identical rates, standing charge and
    night window. Any other stored period is somebody's real tariff and is never matched.
    """
    if configured.valid_from != DEFAULT_TARIFF.valid_from:
        return None
    for t in stored:
        if t.valid_from != LEGACY_DEFAULT_VALID_FROM:
            continue
        same_plan = (
            abs(t.day_rate - configured.day_rate) < 1e-9
            and abs(t.night_rate - configured.night_rate) < 1e-9
            and abs(t.standing_per_year - configured.standing_per_year) < 1e-9
            and t.window == configured.window
        )
        levy = abs(t.pso_per_year - configured.pso_per_year) < 1e-9 or abs(t.pso_per_year - LEGACY_DEFAULT_PSO) < 1e-9
        if same_plan and levy:
            return t
    return None


def _float_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


class Analyzer:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store: Store | None = None
        self.schedule = TariffSchedule([])
        self.live = LiveTracker()
        self.minutes = MinuteBuffer()
        self._unsubs: list[CALLBACK_TYPE] = []
        self._dirty = False
        self._today_rows: list[dict[str, Any]] = []
        self._today: date | None = None
        self._anchor: reg.Anchor | None = None
        self._anchor_rows: list[dict[str, Any]] = []
        self._anchor_after_kwh: float | None = None
        self._last_stored: datetime | None = None
        # What real meter readings have taught about clamp vs meter (calibration.py).
        self.cal = calib.Calibration()
        self._esb_ratio: float | None = None
        self._readings: list[calib.Reading] = []
        # The hall clamp's correction, learned from time-aligned readings (hallfix.py).
        self.hall_fix = HallCorrection()
        self.hall_model: dict[str, Any] = {"kind": "phase", "delta_deg": 0.0, "gain": 1.0}
        self._hall_summary: dict[str, Any] = {}
        self._weather_lock = asyncio.Lock()
        self.weather_error: str | None = None
        self.clock_offset = 0.0
        self.clock_info: dict[str, Any] | None = None
        # Per-day forecast from the chosen weather entity (e.g. AccuWeather), by source.
        self._entity_forecast: dict[date, learner.DayWeather] = {}
        self._rollup_lock = asyncio.Lock()
        self._esb_lock = asyncio.Lock()
        self._sse_lock = asyncio.Lock()
        self._esb_http: aiohttp.ClientSession | None = None
        self._sse_http: aiohttp.ClientSession | None = None
        self._esb_restored = False
        self.esb_error: str | None = None
        self.sse_error: str | None = None
        self.sse_meter: MeterInfo | None = None
        self.sse_portal_config: PortalConfig | None = None
        # The reading the number entities will submit. None = follow the estimate.
        self.override_day: int | None = None
        self.override_night: int | None = None
        self.rollup_rows_total = 0
        self.db_status: dict[str, Any] = {}
        # Gaps the recorder had nothing for, and when that was last checked.
        self._empty_runs: dict[tuple[datetime, datetime], datetime] = {}
        self.dashboard = None  # set by __init__ once the dashboard manager exists
        self.backfill_done = False
        self.energy_status: str | None = None  # what the Energy dashboard set-up did
        # The first load after a start: which step it is on, so the dashboard can say
        # "reading history…" instead of "no data" while it runs.
        self.initial_load: dict[str, Any] = {"state": "pending", "step": None, "started": None, "finished": None}
        # Weather's own health, independent of electricity (last good data is kept).
        self.weather_status: dict[str, Any] = {"last_attempt": None, "last_success": None}
        # After a failed ESB / SSE attempt: when the next one may be made (RETRY_AFTER_FAILURE).
        self._retry_at: dict[str, datetime | None] = {"esb": None, "sse": None}

        self.live_coordinator: DataUpdateCoordinator[dict[str, Any]] = DataUpdateCoordinator(
            hass, LOG, config_entry=entry, name=f"{DOMAIN} live", update_interval=None
        )
        self.insights_coordinator: DataUpdateCoordinator[dict[str, Any]] = DataUpdateCoordinator(
            hass,
            LOG,
            config_entry=entry,
            name=f"{DOMAIN} insights",
            update_interval=INSIGHT_INTERVAL,
            update_method=self._async_compute_insights,
        )

    # ------------------------------------------------------------------ config

    @property
    def cfg(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def entities(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for channel in (CONF_HOUSE, CONF_HALL):
            mapping = {
                m: self.cfg.get(conf_entity(channel, m))
                for m in MEASUREMENTS
                if self.cfg.get(conf_entity(channel, m))
            }
            if mapping.get(M_ENERGY):
                out[channel] = mapping
        return out

    @property
    def has_hall(self) -> bool:
        return CONF_HALL in self.entities

    @property
    def esb_mode(self) -> str:
        return ESB_ACCOUNT if self.cfg.get(CONF_ESB_MODE) == ESB_ACCOUNT else ESB_OFF

    @property
    def sse_mode(self) -> str:
        return self.cfg.get(CONF_SSE_MODE, SSE_OFF)

    async def _run(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        return await self.hass.async_add_executor_job(partial(fn, *args, **kwargs))

    # ------------------------------------------------------------------- setup

    async def async_setup(self) -> None:
        path = Path(self.hass.config.path(DOMAIN, "electricity.db"))
        outcome, detail = await self._run(durability.recover, path)
        if outcome in ("restored", "unrecoverable"):
            LOG.error("electricity database was damaged: %s", detail)
            async_create_issue(
                self.hass, DOMAIN, "database_restored", is_fixable=False, severity=IssueSeverity.ERROR,
                translation_key="database_restored", translation_placeholders={"detail": detail or ""},
            )
        self.db_status = {"opened": outcome, "detail": detail}
        self.store = await self._run(Store, path)
        await self._sync_tariffs()
        self.hall_fix = HallCorrection.from_json(await self._run(self.store.get_json, META_HALL_GAIN))
        self.hall_fix.prune(local_date(dt_util.utcnow()))
        self.hall_model = await self._run(self.hall_fix.model)
        self._hall_summary = await self._run(self.hall_fix.as_dict)
        self.live.hall_model = self.hall_model
        self.clock_info = await self._run(self.store.get_json, META_CLOCK)
        if self.clock_info:
            self.clock_offset = float(self.clock_info.get("offset") or 0.0)
        ratio = await self._run(self.store.get_meta, META_ESB_RATIO)
        self._esb_ratio = float(ratio) if ratio else None
        self.weather_status["last_success"] = await self._run(self.store.get_meta, META_WEATHER_OK)
        # Failures are not a Repairs job: each one is simply retried twelve hours later
        # (an item 1.0.x may have left behind is cleared). The wait survives a restart.
        async_delete_issue(self.hass, DOMAIN, "esb_captcha")
        await self._load_retry_state()
        await self._sync_anchor()
        await self._relearn()
        cached = await self._run(self.store.get_json, META_SSE_METER)
        self.sse_meter = MeterInfo.from_dict(cached)
        self.sse_portal_config = PortalConfig.from_dict(await self._run(self.store.get_json, META_SSE_CONFIG))
        await self._seed_live()

        energy_power = [
            eid
            for channel, mapping in self.entities.items()
            for m, eid in mapping.items()
            if m in (M_ENERGY, M_POWER, M_APPARENT) or (m == M_VOLTAGE and channel == CONF_HOUSE)
        ]
        self._unsubs.append(
            async_track_state_change_event(self.hass, energy_power, self._on_state_change)
        )
        self._unsubs.append(async_track_time_interval(self.hass, self._push_live, LIVE_PUSH))
        # Live sensors that add today to the stored days (week, month, year) re-render as
        # soon as a new summary lands, not only at the clamps' next push.
        self._unsubs.append(self.insights_coordinator.async_add_listener(self._push_live_now))
        self._unsubs.append(async_track_time_change(self.hass, self._on_minute, second=3))
        self._unsubs.append(async_track_time_change(self.hass, self._on_backup_time, hour=3, minute=30, second=0))
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_save_hall_gain, timedelta(minutes=10))
        )
        self._unsubs.append(
            async_track_time_change(self.hass, self._on_rollup_time, minute=ROLLUP_MINUTE, second=0)
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_esb_time, timedelta(minutes=30))
        )
        self._unsubs.append(
            async_track_time_change(self.hass, self._on_midnight, hour=0, minute=0, second=5)
        )
        self._unsubs.append(async_track_time_interval(self.hass, self._on_weather_time, WEATHER_INTERVAL))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_clock_time, CLOCK_INTERVAL))
        if self.sse_mode == SSE_LIVE and self.cfg.get(CONF_SSE_SYNC, True):
            self._unsubs.append(async_track_time_interval(self.hass, self._on_sse_sync, timedelta(hours=1)))
        await self._refresh_caches()
        self.live_coordinator.async_set_updated_data(self.compute_live())
        # And once more ten minutes after setup: a history import still running when the
        # integration was added is picked up then, not at the next hourly check.
        self._unsubs.append(async_call_later(self.hass, 600, self._on_late_gaps))
        # The first rollup can backfill months of recorder history; do it in the
        # background so setup is never held up by it.
        self._spawn(self._async_initial, "initial load")

    def _spawn(self, factory: Callable[[], Any], name: str) -> None:
        """Run a periodic job in the background, isolated: an error is logged with its
        traceback and the job simply runs again on its next tick. Nothing it raises can
        reach the other jobs or leave the integration half set up."""

        async def guarded() -> None:
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - logged in full; the next tick retries
                LOG.exception("%s failed; it will run again on its next tick", name)

        self.entry.async_create_background_task(self.hass, guarded(), f"{DOMAIN} {name}")

    async def _async_initial(self) -> None:
        """First run gathers everything; later runs only what is new or missing.

        Local data first (recorder history into the store), then the sensors are given
        what the store already holds, so the dashboard is never blank while the network
        steps run. Weather does not wait for ESB: it needs no electricity data at all.
        """
        self.initial_load.update(state="running", started=dt_util.utcnow().isoformat())
        steps: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("recorder history", self.async_rollup),
            ("filling gaps", self.async_fill_gaps),
            ("first summary", self.insights_coordinator.async_refresh),
            ("weather", self.async_weather_tick),
            ("ESB Networks", lambda: self.async_esb_tick(initial=True)),
            ("clock check", self.async_clock_tick),
            ("SSE Airtricity", self._initial_sse_sync),
        )
        try:
            for label, step in steps:
                self.initial_load["step"] = label
                try:
                    await step()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one failing source never stops the others
                    LOG.exception("initial data load step failed: %s", label)
        finally:
            self.initial_load.update(state="done", step=None, finished=dt_util.utcnow().isoformat())
        await self.insights_coordinator.async_refresh()

    async def _initial_sse_sync(self) -> None:
        if self.sse_mode == SSE_LIVE and self.cfg.get(CONF_SSE_SYNC, True):
            await self._sse_sync()

    def now(self) -> datetime:
        """UTC now, corrected by the NTP-measured offset when the clock is off."""
        now = dt_util.utcnow()
        if abs(self.clock_offset) >= 1.0:
            now += timedelta(seconds=self.clock_offset)
        return now

    @callback
    def _on_late_gaps(self, _now: datetime) -> None:
        # A @callback, so it runs on the event loop: a bare lambda is sent to a worker
        # thread, where creating the task fails and the fill never runs.
        self._spawn(self.async_fill_gaps, "late gaps")

    @callback
    def _on_save_hall_gain(self, _now: datetime) -> None:
        if self.store is not None:
            self._spawn(self._save_hall, "hall")

    async def _save_hall(self) -> None:
        """Persist the aligned-sample statistics and refresh what the dashboard shows.

        The correction APPLIED to stored hours only changes at midnight (it re-prices
        history); the summary of the evidence is refreshed every ten minutes.
        """
        assert self.store is not None
        await self._run(self.store.set_json, META_HALL_GAIN, self.hall_fix.to_json())
        self._hall_summary = await self._run(self.hall_fix.as_dict)
        self._hall_summary["applied"] = dict(self.hall_model)

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        # No insights computation may start (or be scheduled) against a closing store.
        await self.insights_coordinator.async_shutdown()
        if self.store is not None:
            await self._run(self.store.set_json, META_HALL_GAIN, self.hall_fix.to_json())
        for http in (self._esb_http, self._sse_http):
            if http is not None:
                http.detach()
        self._esb_http = self._sse_http = None
        if self.store is not None:
            rows = self.minutes.drain(dt_util.utcnow() + timedelta(minutes=1))
            await self._run(self.store.upsert_minutes, rows)
            await self._run(self.store.close)

    # ---------------------------------------------------------------- tariffs

    async def _sync_tariffs(self) -> None:
        """Bring the plan from setup / the options into the stored periods.

        Only when that plan CHANGED since it was last stored (or on the first run): periods
        set by hand for the same date (Tariff periods, `add_tariff`) are never overwritten by
        an unchanged setting on a restart.
        """
        assert self.store is not None
        configured = tariff_from_config(self.cfg)
        if await self._run(self.store.get_json, META_CONFIGURED_TARIFF) == configured.as_dict():
            self.schedule = TariffSchedule(await self._run(self.store.tariffs))
            return
        await self._run(self.store.set_json, META_CONFIGURED_TARIFF, configured.as_dict())
        stored = await self._run(self.store.tariffs)
        legacy = _legacy_default_row(stored, configured)
        if legacy is not None:
            # The same plan, stored by 1.0.x under its old default date: moved (or, if
            # the corrected period is already stored, the stale copy dropped), so the
            # days before the plan really started are not priced at it. Every other
            # stored period is left exactly as it was.
            if any(t == configured for t in stored):
                await self._run(self.store.delete_tariff, legacy.valid_from)
            elif not await self._run(self.store.replace_tariff, legacy.valid_from, configured):
                await self._run(self.store.save_tariff, configured)
            LOG.info(
                "tariff: %s now starts on %s, not %s (PSO €%.2f/year); hours before it are not priced at it",
                configured.plan, configured.valid_from, legacy.valid_from, configured.pso_per_year,
            )
            stored = await self._run(self.store.tariffs)
        if not any(t == configured for t in stored):
            await self._run(self.store.save_tariff, configured)
            stored = await self._run(self.store.tariffs)
        self.schedule = TariffSchedule(stored)

    def tariff_fingerprint(self) -> str:
        return "|".join(
            f"{t.valid_from}:{t.day_rate}:{t.night_rate}:{t.standing_per_year}:{t.pso_per_year}:"
            f"{t.window.start_hour}-{t.window.end_hour}-{t.window.clock}"
            for t in self.schedule.periods
        )

    async def async_add_tariff(self, tariff: Tariff) -> int:
        return await self.async_set_tariff_period(tariff)

    async def async_set_tariff_period(self, tariff: Tariff, until: date | None = None) -> int:
        """Day rate, night rate, standing charge and PSO from `tariff.valid_from`, in the past
        (a period that had no price) or the future (a price increase), until the next period
        or, with `until`, to that day inclusive. A period starting the same day is replaced.
        Every stored hour is re-priced and every statistic rewritten at once.
        """
        assert self.store is not None
        if until is not None:
            if until < tariff.valid_from:
                raise TariffPeriodError("end_before_start", "the last day is before the first day")
            after = until + timedelta(days=1)
            periods = await self._run(self.store.tariffs)
            if not any(p.valid_from == after for p in periods):
                # What applied the day after, before this change, applies again from then.
                following = TariffSchedule(periods).in_force(after) if periods else None
                if following is None:
                    raise TariffPeriodError(
                        "end_gap",
                        "there is no tariff after that day to go back to; leave the last day empty "
                        "and these prices apply until the next period",
                    )
                await self._run(self.store.save_tariff, replace(following, valid_from=after))
        await self._run(self.store.save_tariff, tariff)
        LOG.info(
            "tariff period from %s%s: day %.4f, night %.4f €/kWh, standing %.2f, PSO %.2f €/year",
            tariff.valid_from, f" to {until}" if until else "", tariff.day_rate, tariff.night_rate,
            tariff.standing_per_year, tariff.pso_per_year,
        )
        return await self._after_tariff_change()

    async def async_remove_tariff_period(self, valid_from: date) -> int:
        """Forget one period; the one before it (if any) carries on in its place."""
        assert self.store is not None
        periods = await self._run(self.store.tariffs)
        if not any(p.valid_from == valid_from for p in periods):
            raise TariffPeriodError("no_period", f"there is no tariff period starting {valid_from}")
        if len(periods) == 1:
            raise TariffPeriodError("last_period", "the only tariff period cannot be removed; change its prices instead")
        await self._run(self.store.delete_tariff, valid_from)
        LOG.info("tariff period from %s removed", valid_from)
        return await self._after_tariff_change()

    async def _after_tariff_change(self) -> int:
        assert self.store is not None
        self.schedule = TariffSchedule(await self._run(self.store.tariffs))
        repriced = await self._reprice_all()
        await self.async_publish(full=True)
        await self._refresh_caches()
        self._push_live_now()
        await self.insights_coordinator.async_request_refresh()
        return repriced

    async def _reprice_all(self) -> int:
        """Re-price every stored hour. Serialised with the rollup: a re-price works from a
        snapshot of the store, and must not write it back over an hour the rollup has
        just stored."""
        async with self._rollup_lock:
            return await self._reprice_all_locked()

    def _book_fixed_days(self, days: set[str]) -> int:
        """Each of these local days' fixed charge on its first stored hour. Executor only."""
        assert self.store is not None
        if not days:
            return 0
        first, last = min(days), max(days)
        rows = self.store.hours(
            local_midnight(date.fromisoformat(first)), local_midnight(date.fromisoformat(last) + timedelta(days=1))
        )
        changed = book_fixed([r for r in rows if r["local_date"] in days], self.schedule)
        return self.store.upsert_hours(changed)

    async def _reprice_all_locked(self) -> int:
        assert self.store is not None

        def work() -> int:
            rows = self.store.hours()
            updated = [reprice(r, self.schedule, self.cal.factor_at, self.hall_model) for r in rows]
            fixed = {r["hour_utc"]: r for r in book_fixed(updated, self.schedule)}
            updated = [fixed.get(r["hour_utc"], r) for r in updated]
            self.store.upsert_hours(updated)
            self.store.set_meta(META_PRICED_WITH, self.pricing_fingerprint())
            return len(updated)

        count = await self._run(work)
        LOG.info("re-priced %d stored hours (tariff schedule and meter factors)", count)
        return count

    # ----------------------------------------------------------------- anchor

    async def _sync_anchor(self) -> None:
        assert self.store is not None
        cfg = self.cfg
        if cfg.get(CONF_READING_DAY) is not None and cfg.get(CONF_READING_NIGHT) is not None and cfg.get(CONF_READING_AT):
            taken = dt_util.parse_datetime(str(cfg[CONF_READING_AT]))
            if taken is not None:
                if taken.tzinfo is None:
                    taken = taken.replace(tzinfo=IRELAND)
                taken = taken.astimezone(UTC).replace(microsecond=0)
                anchors = await self._run(self.store.anchors)
                if not any(a["taken_at"] == taken for a in anchors):
                    await self._run(
                        self.store.add_anchor,
                        taken,
                        float(cfg[CONF_READING_DAY]),
                        float(cfg[CONF_READING_NIGHT]),
                        "setup",
                    )

    def _load_readings(self) -> list[calib.Reading]:
        """Every real reading of the meter: entered, submitted, read back from SSE, or ESB's."""
        assert self.store is not None
        out = [
            calib.Reading(a["taken_at"], float(a["day_kwh"]), float(a["night_kwh"]), a["origin"])
            for a in self.store.anchors()
        ]
        by_date: dict[str, dict[str, float]] = {}
        for r in self.store.register_reads():
            if "estimate" in str(r.get("kind") or "").lower():
                continue  # an estimated read is not the meter speaking
            by_date.setdefault(r["read_date"], {})[r["register"]] = float(r["reading"])
        for read_date, regs in by_date.items():
            if "day" in regs and "night" in regs:
                out.append(
                    calib.Reading(local_midnight(date.fromisoformat(read_date)), regs["day"], regs["night"], "esb")
                )
        return calib.merge_readings(out)

    def _esb_evidence(self, hours: list[dict[str, Any]]) -> list[tuple[datetime, dict[str, float], dict[str, float]]]:
        """(day end, meter kWh per band, clamp kWh per band) for days complete on both sides."""
        assert self.store is not None
        if self.esb_mode == ESB_OFF:
            return []
        clamp: dict[str, list[float]] = {}
        for r in hours:
            if r.get("house_kwh") is None:
                continue
            acc = clamp.setdefault(r["local_date"], [0.0, 0.0, 0])
            acc[1 if r.get("band") == BAND_NIGHT else 0] += float(r["house_kwh"])
            acc[2] += 1
        meter: dict[str, list[float]] = {}
        for start, kwh, band in self.store.esb_intervals(hours[0]["hour_utc"] if hours else None, None):
            d = start.astimezone(IRELAND).date().isoformat()
            acc = meter.setdefault(d, [0.0, 0.0, 0])
            acc[1 if band == BAND_NIGHT else 0] += kwh
            acc[2] += 1
        out = []
        for d in sorted(set(clamp) & set(meter)):
            c, m = clamp[d], meter[d]
            if c[2] < 23 or m[2] < 46:
                continue
            end = local_midnight(date.fromisoformat(d) + timedelta(days=1))
            out.append((end, {"day": m[0], "night": m[1]}, {"day": c[0], "night": c[1]}))
        return out[-120:]

    def pricing_fingerprint(self) -> str:
        m = self.hall_model
        return (
            f"rules:{PRICING_RULES}#{self.tariff_fingerprint()}#{self.cal.fingerprint()}"
            f"#hall:{m.get('kind')}:{m.get('delta_deg')}:{m.get('gain')}"
        )

    async def _relearn(self) -> bool:
        """Re-learn from the readings; re-price and re-publish history if anything changed."""
        assert self.store is not None
        now = dt_util.utcnow()
        prior = self._esb_ratio

        def work() -> tuple[list[calib.Reading], calib.Calibration]:
            readings = self._load_readings()
            # Hours filled from ESB after a downtime ARE the meter: learning the clamp's
            # ratio to the meter from them would only learn ESB's ratio to itself.
            hours = [h for h in self.store.hours() if h.get("source") != downtime.SOURCE]
            esb_days = self._esb_evidence(hours)
            return readings, calib.learn(
                readings, hours, now=now, prior=prior, prior_source="esb", esb_days=esb_days
            )

        readings, cal = await self._run(work)
        self._readings = readings
        latest = readings[-1] if readings else None
        anchor = reg.Anchor(latest.taken_at, latest.day, latest.night, latest.origin) if latest else None
        anchor_moved = anchor != self._anchor
        self._anchor = anchor
        if anchor_moved:
            self._anchor_after_kwh = None
        changed = cal.fingerprint() != self.cal.fingerprint() or anchor_moved
        self.cal = cal
        stored = await self._run(self.store.get_meta, META_PRICED_WITH)
        if stored != self.pricing_fingerprint():
            await self._reprice_all()
            await self.async_publish(full=True)
            changed = True
        elif anchor_moved:
            await self.async_publish(full=True, keys={"register_day", "register_night"})
        if changed:
            LOG.info(
                "meter factors from %d reading(s): day x%.4f, night x%.4f (%s)",
                len(readings), cal.day, cal.night, cal.source,
            )
            await self._refresh_caches()
            self._push_live_now()
        return changed

    async def async_set_anchor(
        self, day: float, night: float, taken_at: datetime, origin: str = "manual"
    ) -> None:
        assert self.store is not None
        if taken_at.tzinfo is None:
            taken_at = taken_at.replace(tzinfo=IRELAND)
        if taken_at > dt_util.utcnow() + timedelta(minutes=5):
            raise HomeAssistantError("a meter reading cannot be taken in the future")
        await self._run(self.store.add_anchor, taken_at.astimezone(UTC), day, night, origin)
        self.override_day = self.override_night = None
        await self._relearn()
        await self._refresh_caches()
        self._push_live_now()
        await self.insights_coordinator.async_request_refresh()

    async def _anchor_hour_after(self) -> float | None:
        """kWh used in the anchor's own hour AFTER the reading was taken."""
        anchor = self._anchor
        if anchor is None or CONF_HOUSE not in self.entities:
            return None
        key = f"{META_ANCHOR_SPLIT}:{anchor.taken_at.isoformat()}"
        assert self.store is not None
        cached = await self._run(self.store.get_meta, key)
        if cached is not None:
            return float(cached)
        hour_end = floor_hour(anchor.taken_at) + HOUR
        if dt_util.utcnow() < hour_end + timedelta(minutes=10):
            return None  # the anchor hour is not finished; the live tail covers it
        value = await recorder_io.five_minute_change(
            self.hass, self.entities[CONF_HOUSE][M_ENERGY], anchor.taken_at, hour_end
        )
        if value is not None:
            await self._run(self.store.set_meta, key, str(value))
        return value

    # ------------------------------------------------------------------- live

    async def _seed_live(self) -> None:
        """Start the live counters from the current state and this hour's start."""
        now = dt_util.utcnow()
        hour = floor_hour(now)
        for channel, mapping in self.entities.items():
            entity = mapping[M_ENERGY]
            value = _float_state(self.hass, entity)
            if value is None:
                continue
            base = await recorder_io.counter_at(self.hass, entity, hour)
            so_far = value - base if base is not None and value >= base else None
            counter = self.live.house if channel == CONF_HOUSE else self.live.hall
            counter.seed(value, now, hour_so_far=so_far)
            power = _float_state(self.hass, mapping.get(M_POWER))
            if power is not None:
                self.live.feed_power(channel, power, now, local_date(now).isoformat())

    @callback
    def _on_state_change(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is None or new.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            return
        try:
            value = float(new.state)
        except ValueError:
            return
        entity_id = event.data["entity_id"]
        when = new.last_reported if getattr(new, "last_reported", None) else new.last_updated
        for channel, mapping in self.entities.items():
            if mapping.get(M_ENERGY) == entity_id:
                unit = new.attributes.get("unit_of_measurement")
                if unit == "Wh":
                    value /= 1000.0
                elif unit == "MWh":
                    value *= 1000.0
                credited = self.live.feed_energy(channel, value, when)
                if credited:
                    self.minutes.add_energy(channel, credited, when)
                self._dirty = True
            elif mapping.get(M_POWER) == entity_id:
                unit = new.attributes.get("unit_of_measurement")
                if unit == "kW":
                    value *= 1000.0
                day = local_date(when).isoformat()
                self.live.feed_power(channel, value, when, day)
                self.minutes.add(f"{channel}_w", value, when)
                if channel == CONF_HOUSE and self.live.rest_power is not None:
                    self.minutes.add("rest_w", self.live.rest_power, when)
                if self.live.aligned and self.live.house_power is not None and self.live.hall_power is not None:
                    self.hall_fix.observe(
                        self.live.house_power, self.live.hall_power, self.live.hall_va, day, when.timestamp()
                    )
                self._dirty = True
            elif mapping.get(M_APPARENT) == entity_id:
                if channel == CONF_HALL:
                    self.live.hall_va = value
                self.minutes.add(f"{channel}_va", value, when)
            elif mapping.get(M_VOLTAGE) == entity_id and channel == CONF_HOUSE:
                self.minutes.add("voltage", value, when)

    @callback
    def _on_minute(self, now: datetime) -> None:
        rows = self.minutes.drain(now)
        if rows and self.store is not None:
            self.entry.async_create_background_task(
                self.hass, self._run(self.store.upsert_minutes, rows), f"{DOMAIN} minutes"
            )

    @callback
    def _on_backup_time(self, _now: datetime) -> None:
        self._spawn(self.async_backup, "backup")

    async def async_backup(self) -> dict[str, Any]:
        """Tonight's backups (daily, plus weekly on Sundays and monthly on the 1st)."""
        assert self.store is not None
        targets = await self._run(durability.rotate, self.store.path, local_date(dt_util.utcnow()))
        written = []
        for target in targets:
            size = await self._run(self.store.backup_to, target)
            written.append({"file": target.name, "bytes": size})
        self.db_status["last_backup"] = dt_util.utcnow().replace(microsecond=0).isoformat()
        self.db_status["backups"] = [p.name for p in await self._run(durability.backups, self.store.path)]
        LOG.info("database backed up: %s", written)
        return {"written": written}

    async def async_export(self) -> dict[str, Any]:
        assert self.store is not None
        folder = self.store.path.parent / "exports" / dt_util.now().strftime("%Y-%m-%d_%H%M")
        counts = await self._run(
            self.store.export_csv,
            folder,
            ("hourly", "minutely", "esb_intervals", "esb_register_reads", "weather_hourly", "tariffs",
             "meter_anchors", "meter_submissions", "forecast_log", "learned_weights"),
        )
        return {"folder": str(folder), "rows": counts}

    async def async_minutes(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        assert self.store is not None
        rows = await self._run(self.store.minutes, start, end)
        return [{**r, "minute_utc": r["minute_utc"].isoformat()} for r in rows]

    @callback
    def _push_live(self, _now: datetime | None = None) -> None:
        if not self._dirty:
            return
        self._push_live_now()

    @callback
    def _push_live_now(self) -> None:
        self._dirty = False
        try:
            self.live_coordinator.async_set_updated_data(self.compute_live())
        except Exception:  # noqa: BLE001
            LOG.exception("live update failed")

    def _pending(self) -> dict[datetime, tuple[float, float | None]]:
        return self.live.pending_hours(self._last_stored)

    def compute_live(self) -> dict[str, Any]:
        """Everything that moves second by second. Cheap: no I/O."""
        now = self.now()
        tariff = self.schedule.for_instant(now)
        band = tariff.window.band_at(now)
        today = local_date(now)
        in_force = self.schedule.in_force(today)
        rate = None if in_force is None else in_force.rate(band)
        current_hour = floor_hour(now)

        totals = analytics.Totals(label="Today", schedule=self.schedule)
        for row in self._today_rows:
            # Only today's own hours: between local midnight and the next cache refresh
            # the cache can still hold yesterday's.
            if row.get("local_date") == today.isoformat():
                totals.add(row)
        pending = self._pending()
        pseudo_rows: list[dict[str, Any]] = []
        hourly: list[dict[str, Any]] = [
            {"start": r["hour_utc"].isoformat(), "end": (r["hour_utc"] + HOUR).isoformat(), "band": r["band"],
             "kwh": round(float(r.get("house_kwh") or 0.0), 3), "live": False}
            for r in self._today_rows if r.get("local_date") == today.isoformat()
        ]
        for hour, (house_kwh, hall_kwh) in pending.items():
            band_h = self.schedule.for_instant(hour).window.band_at(hour)
            t_h = self.schedule.in_force(local_date(hour))
            fixed = None if t_h is None else 0.0  # the day's fixed charge is added once, below
            if local_date(hour) == today:
                hourly.append({"start": hour.isoformat(), "end": (hour + HOUR).isoformat(), "band": band_h,
                               "kwh": round(house_kwh, 3), "live": True})
                # kWh stay as the clamp measured them; money is the meter's.
                totals.add_live(
                    house_kwh,
                    None if hall_kwh is None else hall_kwh * self._live_hall_factor(),
                    band_h,
                    None if t_h is None else t_h.rate(band_h),
                    fixed,
                    self.cal.factor_at(hour, band_h),
                    tariff=t_h,
                )
            pseudo_rows.append({"hour_utc": hour, "band": band_h, "house_kwh": house_kwh})

        # A day costs at least its fixed charge, from its first second: standing charge
        # and PSO (annual / 365, or 366) in full, energy on top as it is used.
        if in_force is not None:
            totals.fixed_cost = in_force.fixed_for_day(today)
            totals.standing_cost = in_force.standing_for_day(today)
            totals.pso_cost = in_force.pso_for_day(today)

        registers = None
        if self._anchor is not None:
            estimate = reg.estimate(
                self._anchor,
                [*self._anchor_rows, *pseudo_rows],
                as_of=now,
                calibration={"day": self.cal.day, "night": self.cal.night},
                anchor_hour_after_kwh=self._anchor_after_kwh,
            )
            registers = estimate.as_dict()

        next_change = tariff.window.next_change(now)
        tonight = tariff.window.describe(today)
        house_w = self.live.house_power
        return {
            "updated": now.isoformat(),
            "band": band,
            "rate": rate,
            "tariff": tariff.as_dict(),
            "next_change": next_change,
            "next_band": "night" if band != BAND_NIGHT else "day",
            "tonight": tonight,
            "house_w": house_w,
            "hall_w": self.live.hall_power,
            "rest_w": self.live.rest_power,
            "cost_rate_eur_h": None if house_w is None or rate is None else house_w / 1000.0 * rate * self.cal.factor(band),
            "meter_factors": {"day": self.cal.day, "night": self.cal.night, "source": self.cal.source},
            "today": totals.as_dict(),
            # Today hour by hour, the current hour included as it fills (for the chart).
            "today_hourly": sorted(hourly, key=lambda h: h["start"]),
            "registers": registers,
            "hall_over_minutes": round(self.live.hall_over_seconds / 60.0, 1),
            "hall_correction": {"factor": round(self._live_hall_factor(), 4), **self._hall_summary},
            "price_segments": self._price_segments(now),
            "clock": self.clock_info,
            "counter_resets": self.live.house.resets + self.live.hall.resets,
            "submit": self.reading_to_submit(registers),
        }

    def _live_hall_factor(self) -> float:
        return hour_factor(self.hall_model, self.live.hall_power, self.live.hall_va)

    def _price_segments(self, now: datetime) -> list[dict[str, Any]]:
        """Today's and tomorrow's unit price, as runs of equal price (local times)."""
        start = local_midnight(local_date(now))
        out: list[dict[str, Any]] = []
        for i in range(48):
            h = start + timedelta(hours=i)
            band = self.schedule.for_instant(h).window.band_at(h)
            t = self.schedule.in_force(local_date(h))
            rate = None if t is None else t.rate(band)
            if out and out[-1]["rate"] == rate:
                out[-1]["end"] = (h + HOUR).isoformat()
            else:
                out.append({"start": h.isoformat(), "end": (h + HOUR).isoformat(), "band": band, "rate": rate})
        return out

    def reading_to_submit(self, registers: dict[str, Any] | None = None) -> dict[str, Any]:
        if registers is None and self.live_coordinator.data:
            registers = self.live_coordinator.data.get("registers")
        day = self.override_day if self.override_day is not None else (registers or {}).get("day_dial")
        night = self.override_night if self.override_night is not None else (registers or {}).get("night_dial")
        return {
            "day": day,
            "night": night,
            "origin": "manual" if (self.override_day is not None or self.override_night is not None) else "estimate",
        }

    @callback
    def _on_midnight(self, _now: datetime) -> None:
        self._spawn(self._async_midnight, "midnight")

    async def _async_midnight(self) -> None:
        self.hall_fix.prune(local_date(dt_util.utcnow()))
        model = await self._run(self.hall_fix.model)
        self._hall_summary = await self._run(self.hall_fix.as_dict)
        if model != self.hall_model:
            LOG.info("hall clamp correction now %s", model)
            self.hall_model = model
            self.live.hall_model = model
            await self._reprice_all()
            await self.async_publish(full=True)
        await self.async_fill_gaps()
        await self.async_apply_esb()
        await self._refresh_caches()

    async def _refresh_caches(self) -> None:
        """Re-read the slices of the store the live view needs."""
        assert self.store is not None
        now = dt_util.utcnow()
        today = local_date(now)
        start = local_midnight(today)
        self._today = today
        self._today_rows = await self._run(self.store.hours, start, None)
        _, last = await self._run(self.store.hour_span)
        self._last_stored = last
        if last is not None:
            self.live.forget_stored(last)
        if self._anchor is not None:
            self._anchor_rows = await self._run(self.store.hours, floor_hour(self._anchor.taken_at), None)
            self._anchor_after_kwh = await self._anchor_hour_after()
        else:
            self._anchor_rows = []
        self._dirty = True

    # ----------------------------------------------------------------- rollup

    @callback
    def _on_rollup_time(self, _now: datetime) -> None:
        self._spawn(self._rollup_and_refresh, "rollup")

    async def _rollup_and_refresh(self) -> None:
        try:
            await self.async_rollup()
            await self.async_fill_gaps()
        except Exception:  # noqa: BLE001
            LOG.exception("hourly rollup failed")
        try:
            # Weather hours that have just completed are already in the store (the
            # forecast covers today); publishing them every hour, not only after a fetch
            # every twelve, is what keeps today's temperature and degree-day charts filled.
            await self.async_publish_weather()
        except Exception:  # noqa: BLE001
            LOG.exception("hourly weather statistics failed")
        await self.insights_coordinator.async_request_refresh()

    async def async_rollup(self, *, rebuild: bool = False) -> int:
        """Read compiled hours from the recorder into the store, then publish."""
        if CONF_HOUSE not in self.entities:
            return 0
        assert self.store is not None
        async with self._rollup_lock:
            now = dt_util.utcnow()
            end = floor_hour(now)
            first, last = await self._run(self.store.hour_span)
            if rebuild or last is None:
                start = await recorder_io.first_hour(self.hass, self.entities[CONF_HOUSE][M_ENERGY])
                if start is None:
                    LOG.debug("no recorder statistics yet for %s", self.entities[CONF_HOUSE][M_ENERGY])
                    return 0
            else:
                start = min(last + HOUR, end) - ROLLUP_LOOKBACK
            if start >= end:
                return 0
            stats = await recorder_io.hourly(self.hass, self.entities, start, end)
            # An hour ESB already settled keeps ESB's figure when the recorder's is re-read.
            final = {
                r["hour_utc"]: float(r["meter_kwh"])
                for r in (await self._run(self.store.hours, min(stats), max(stats) + HOUR) if stats else [])
                if r.get("meter_source") == downtime.SOURCE and r.get("source") != downtime.SOURCE
                and r.get("meter_kwh") is not None
            }
            rows = []
            for hour in sorted(stats):
                channels = stats[hour]
                row = build_hour(
                    hour,
                    channels.get(CONF_HOUSE),
                    channels.get(CONF_HALL),
                    self.schedule,
                    factor=self.cal.factor_at,
                    hall_model=self.hall_model,
                    meter_kwh=None if rebuild else final.get(hour),
                )
                if row is not None:
                    rows.append(row)
            if rows and not rebuild:
                # An hour already rebuilt from ESB after a downtime keeps that: the
                # recorder's version of it is the restart lump.
                repaired = {
                    r["hour_utc"]
                    for r in await self._run(self.store.hours, rows[0]["hour_utc"], rows[-1]["hour_utc"] + HOUR)
                    if r.get("source") == downtime.SOURCE
                }
                rows = [r for r in rows if r["hour_utc"] not in repaired]
            written = await self._run(self.store.upsert_hours, rows)
            await self._run(self._book_fixed_days, {r["local_date"] for r in rows if "local_date" in r} | {
                local_date(r["hour_utc"]).isoformat() for r in rows
            })
            self.rollup_rows_total += written
            fingerprint = await self._run(self.store.get_meta, META_PRICED_WITH)
            if fingerprint != self.pricing_fingerprint():
                await self._reprice_all_locked()
                full = True
            else:
                full = rebuild or last is None
            if written:
                LOG.debug("rollup stored %d hour(s) from %s", written, rows[0]["hour_utc"] if rows else None)
            self.backfill_done = True
        await self._refresh_caches()
        await self.async_publish(full=full)
        self._push_live_now()
        return written

    async def async_fill_gaps(self) -> int:
        """Fill holes in the stored hours (HA was down, the recorder compiled late)."""
        if CONF_HOUSE not in self.entities:
            return 0
        assert self.store is not None
        first = await self._run(partial(self.store.first_hour_from, not_source=downtime.SOURCE))
        # The recorder may hold OLDER history than the store: the integration was added
        # while a history import was still running, statistics were imported later, or the
        # recorder compiled late. Everything from the recorder's first hour is in scope.
        oldest = await recorder_io.first_hour(self.hass, self.entities[CONF_HOUSE][M_ENERGY])
        candidates = [t for t in (first, oldest) if t is not None]
        if not candidates:
            return 0
        runs = await self._run(self.store.missing_hours, min(candidates), floor_hour(dt_util.utcnow()) - HOUR)
        if not runs:
            return 0
        filled = 0
        now = dt_util.utcnow()
        self._empty_runs = {k: v for k, v in self._empty_runs.items() if now - v < timedelta(hours=24)}
        for start, end in runs[:500]:
            if (start, end) in self._empty_runs:
                continue
            stats = await recorder_io.hourly(self.hass, self.entities, start, end)
            if not stats:
                self._empty_runs[(start, end)] = now
                continue
            rows = [
                row
                for hour in sorted(stats)
                if (row := build_hour(
                    hour, stats[hour].get(CONF_HOUSE), stats[hour].get(CONF_HALL), self.schedule,
                    factor=self.cal.factor_at, hall_model=self.hall_model,
                )) is not None
            ]
            filled += await self._run(self.store.upsert_hours, rows)
            await self._run(self._book_fixed_days, {local_date(r["hour_utc"]).isoformat() for r in rows})
        if filled:
            LOG.info("filled %d missing hour(s) from the recorder", filled)
            await self.async_publish(full=True)
            await self._refresh_caches()
            if filled > 24:
                # A real backfill: everything derived from history follows at once.
                self._spawn(self._after_backfill, "backfill")
        return filled

    async def _after_backfill(self) -> None:
        for step in (
            lambda: self.async_esb_tick(initial=True),
            self.async_weather_tick,
            self._relearn,
        ):
            try:
                await step()
            except Exception:  # noqa: BLE001
                LOG.exception("post-backfill step failed")
        await self.insights_coordinator.async_request_refresh()

    # --------------------------------------------------------------- weather

    def _location(self) -> tuple[float, float] | None:
        """The pin chosen at setup; Home Assistant's home for entries made before there was one."""
        lat, lon = self.cfg.get(CONF_LATITUDE), self.cfg.get(CONF_LONGITUDE)
        if lat is None or lon is None:
            lat, lon = self.hass.config.latitude, self.hass.config.longitude
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)

    def _weather_moved(self, location: tuple[float, float]) -> bool:
        """Forget weather fetched for somewhere else. Runs in the executor."""
        assert self.store is not None
        before = self.store.get_meta(META_WEATHER_LOCATION)
        self.store.set_meta(META_WEATHER_LOCATION, f"{location[0]:.5f},{location[1]:.5f}")
        if not before:
            return False
        lat, lon = (float(x) for x in before.split(","))
        if abs(lat - location[0]) <= WEATHER_MOVE_DEG and abs(lon - location[1]) <= WEATHER_MOVE_DEG:
            return False
        self.store.clear_weather()
        self.store.set_meta(META_WEATHER_FORECAST_AT, None)
        return True

    @callback
    def _on_weather_time(self, _now: datetime) -> None:
        self._spawn(self.async_weather_tick, "weather")

    async def async_weather_tick(self) -> None:
        """Weather on its own terms: it never waits for electricity data.

        History goes back WEATHER_HISTORY_DAYS (or further, to the first electricity or
        ESB day), so a brand-new installation has temperatures, degree days and the
        climate normals the forecasts need from the first run. Afterwards only what is
        missing is fetched. The archive, the forecast and the weather entity are separate
        steps: one failing never stops the others, and whatever is already stored (the
        last good data) is always published.
        """
        location = self._location()
        if location is None:
            self.weather_error = "no location: set one in the integration's weather options"
            return
        assert self.store is not None
        async with self._weather_lock:
            if await self._run(self._weather_moved, location):
                LOG.info("weather: location changed, fetching it afresh for %.3f, %.3f", *location)
            now = dt_util.utcnow()
            today = local_date(now)
            self.weather_status["last_attempt"] = now.isoformat()
            first_hour, _ = await self._run(self.store.hour_span)
            first_esb, _ = await self._run(self.store.esb_span)
            first_day = min(
                [today - timedelta(days=WEATHER_HISTORY_DAYS)]
                + [t.astimezone(IRELAND).date() for t in (first_hour, first_esb) if t is not None]
            )
            have = await self._run(self.store.weather_archive_days)
            oldest_before = min(have) if have else None
            missing = [
                first_day + timedelta(days=i)
                for i in range((today - first_day).days)
                if have.get((first_day + timedelta(days=i)).isoformat(), 0) < 23
                or first_day + timedelta(days=i) >= today - timedelta(days=wx.OVERLAP_DAYS)
            ]
            runs: list[list[date]] = []
            for d in missing:
                if runs and (d - runs[-1][-1]).days == 1:
                    runs[-1].append(d)
                else:
                    runs.append([d])
            http = async_get_clientsession(self.hass)
            requests = 0
            errors: list[str] = []
            try:
                for run in runs:
                    obs, n = await wx.fetch_archive(http, *location, run[0], run[-1])
                    requests += n
                    # Recent days the archive cannot answer yet come back empty: never let
                    # an empty archive hour replace a forecast one.
                    obs = [o for o in obs if o.get("temperature") is not None]
                    await self._run(self.store.upsert_weather, obs, "archive")
            except wx.WeatherUnavailable as exc:
                errors.append(f"history: {exc}")
            except Exception as exc:  # noqa: BLE001 - logged; the forecast still runs
                LOG.exception("weather history step failed")
                errors.append(f"history: {_describe(exc)}")
            try:
                last = await self._run(self.store.get_meta, META_WEATHER_FORECAST_AT)
                if not last or now - datetime.fromisoformat(last) >= WEATHER_INTERVAL - timedelta(minutes=5):
                    forecast = await wx.fetch_forecast(http, *location)
                    requests += 1
                    await self._run(self.store.upsert_weather, forecast, "forecast")
                    await self._run(self.store.set_meta, META_WEATHER_FORECAST_AT, now.isoformat())
            except wx.WeatherUnavailable as exc:
                errors.append(f"forecast: {exc}")
            except Exception as exc:  # noqa: BLE001
                LOG.exception("weather forecast step failed")
                errors.append(f"forecast: {_describe(exc)}")
            try:
                await self._fetch_entity_forecast()
            except Exception:  # noqa: BLE001 - an optional extra; Open-Meteo stands alone
                LOG.exception("weather entity forecast failed")
                self._entity_forecast = {}
            if errors:
                self.weather_error = "; ".join(errors)
                LOG.warning("weather: %s (the last data received is kept)", self.weather_error)
            else:
                self.weather_error = None
                self.weather_status["last_success"] = now.isoformat()
                await self._run(self.store.set_meta, META_WEATHER_OK, now.isoformat())
            if requests:
                total = int(await self._run(self.store.get_meta, META_WEATHER_REQUESTS) or 0) + requests
                await self._run(self.store.set_meta, META_WEATHER_REQUESTS, str(total))
                LOG.debug("weather: %d Open-Meteo request(s), %d in total", requests, total)
            have = await self._run(self.store.weather_archive_days)
            # History older than what was there before (a first run, or the window grew)
            # is published in full; otherwise only the recent hours are rewritten.
            backfilled = bool(have) and (oldest_before is None or min(have) < oldest_before)
        await self.async_publish_weather(full=backfilled)

    async def _fetch_entity_forecast(self) -> None:
        """The chosen weather entity's daily forecast (e.g. AccuWeather's RealFeel)."""
        entity_id = self.cfg.get(CONF_WEATHER_ENTITY)
        if not entity_id or self.hass.states.get(entity_id) is None:
            self._entity_forecast = {}
            return
        try:
            response = await self.hass.services.async_call(
                "weather", "get_forecasts", {"entity_id": entity_id, "type": "daily"},
                blocking=True, return_response=True,
            )
        except HomeAssistantError as exc:
            LOG.info("weather entity %s gave no daily forecast: %s", entity_id, exc)
            self._entity_forecast = {}
            return
        base = wx.DEFAULT_BASE_TEMP_C
        out: dict[date, learner.DayWeather] = {}
        for item in (response or {}).get(entity_id, {}).get("forecast", []):
            try:
                when = dt_util.parse_datetime(str(item["datetime"]))
                if when is None:
                    continue
                day = when.astimezone(IRELAND).date()
                high = float(item["temperature"])
                low = float(item.get("templow", high))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            mean = (high + low) / 2
            feels = float(item.get("apparent_temperature", mean)) if item.get("apparent_temperature") is not None else mean
            # A daily high/low only gives the mean, so degree days are mean-based here.
            out[day] = learner.DayWeather(
                hdd=max(0.0, base - mean),
                hdd_app=max(0.0, base - min(feels, mean)),
                rad=0.0,
                wind=float(item.get("wind_speed") or 0.0),
                rain=float(item.get("precipitation") or 0.0),
            )
        self._entity_forecast = out
        LOG.debug("weather entity %s: %d forecast day(s)", entity_id, len(out))

    async def async_publish_weather(self, *, full: bool = False) -> None:
        assert self.store is not None
        now = dt_util.utcnow()
        rows = await self._run(self.store.weather)
        series = await self._run(longterm.build_weather_series, rows, now, wx.DEFAULT_BASE_TEMP_C)
        await longterm.async_publish(self.hass, series, full=full, keys=set(WEATHER_STATISTICS))

    # ----------------------------------------------------------------- clock

    @callback
    def _on_clock_time(self, _now: datetime) -> None:
        self._spawn(self.async_clock_tick, "clock")

    async def async_clock_tick(self) -> None:
        try:
            result = await clock.check()
        except (OSError, TimeoutError, RuntimeError) as exc:
            LOG.debug("NTP check failed: %s", exc)
            return
        self.clock_offset = result.offset
        self.clock_info = {
            "server": result.server,
            "offset": round(result.offset, 3),
            "delay": round(result.delay, 3),
            "checked_at": dt_util.utcnow().replace(microsecond=0).isoformat(),
            "corrected": abs(result.offset) >= 1.0,
        }
        assert self.store is not None
        await self._run(self.store.set_json, META_CLOCK, self.clock_info)
        if abs(result.offset) >= CLOCK_WARN_SECONDS:
            async_create_issue(
                self.hass, DOMAIN, "clock_offset", is_fixable=False, severity=IssueSeverity.WARNING,
                translation_key="clock_offset",
                translation_placeholders={"offset": f"{result.offset:+.1f}", "server": result.server},
            )
        else:
            async_delete_issue(self.hass, DOMAIN, "clock_offset")
        self._push_live_now()

    # ------------------------------------------------------- long-term stats

    async def async_publish(self, *, full: bool = False, keys: set[str] | None = None) -> dict[str, int]:
        assert self.store is not None

        def gather():
            hours = self.store.hours()
            esb = self.store.esb_intervals()
            series_regs = calib.register_series(self._readings, hours, self.cal)
            return longterm.build_series(hours, esb, series_regs)

        series = await self._run(gather)
        written = await longterm.async_publish(self.hass, series, full=full, keys=keys)
        LOG.debug("published long-term statistics: %s", written)
        return written

    # -------------------------------------------------------------------- ESB

    @callback
    def _on_esb_time(self, _now: datetime) -> None:
        if self.esb_mode == ESB_ACCOUNT:
            self._spawn(self.async_esb_tick, "ESB")

    async def async_esb_tick(self, *, force: bool = False, initial: bool = False) -> None:
        if self.esb_mode == ESB_OFF:
            return
        async with self._esb_lock:
            attempted = await self._esb_account(force=force)
        if not (attempted or initial or force):
            return  # not due: nothing new to publish or learn from (this runs every 30 min)
        await self.async_publish(keys={"esb_energy", "esb_energy_day", "esb_energy_night"})
        # ESB's data is the authority: revise the hours it now covers, fill what the clamp missed.
        await self.async_apply_esb()
        # ESB's actual register reads are readings too.
        await self._relearn()

    async def async_apply_esb(self) -> int:
        """ESB is the authority; the clamp is the early estimate. With ESB's (2-3 days late)
        data: every clamp hour it now covers is re-priced on ESB's own figure (provisional ->
        final); hours the clamp missed (Home Assistant down, before the clamp existed) are
        filled from it, and a restart lump is put right (downtime.py). Everything derived
        (costs, bills, statistics) follows; the meter factors and the forecast scores learn
        from the same ESB days on the next re-learn / insights pass."""
        assert self.store is not None
        ratio = self._esb_ratio or 1.0

        def work() -> list[dict[str, Any]]:
            hours = self.store.hours()
            if not hours:
                return []
            esb = self.store.esb_intervals(None, hours[-1]["hour_utc"] + HOUR)  # from ESB's very first
            rows = []
            for hour, kwh in downtime.plan(hours, esb, ratio=ratio):
                row = build_hour(
                    hour, {"energy": {"change": kwh}}, None, self.schedule,
                    factor=self.cal.factor_at, hall_model=self.hall_model, source=downtime.SOURCE,
                    meter_kwh=kwh * ratio,
                )
                if row is not None:
                    rows.append(row)
            filled = {r["hour_utc"] for r in rows}
            stored = {h["hour_utc"]: h for h in hours}
            for hour, meter in downtime.settle(hours, esb):
                if hour not in filled:
                    rows.append(reprice(stored[hour], self.schedule, self.cal.factor_at, self.hall_model, meter_kwh=meter))
            self.store.upsert_hours(rows)
            self._book_fixed_days({local_date(r["hour_utc"]).isoformat() for r in rows})
            return rows

        async with self._rollup_lock:
            rows = await self._run(work)
        if rows:
            settled = sum(1 for r in rows if r.get("source") != downtime.SOURCE)
            LOG.info(
                "ESB: %d hour(s) re-priced on the meter's own data, %d filled where the clamp had none",
                settled, len(rows) - settled,
            )
            await self.async_publish(full=True)
            await self._refresh_caches()
        return len(rows)

    # Before 1.0.2's last change this only filled gaps; the name is kept for callers.
    async_repair_downtime = async_apply_esb

    def _esb_session(self) -> aiohttp.ClientSession:
        # Its own session: B2C cookie names need an unquoting jar (see new_cookie_jar).
        if self._esb_http is None or self._esb_http.closed:
            self._esb_http = async_create_clientsession(self.hass, auto_cleanup=False, cookie_jar=new_cookie_jar())
        return self._esb_http

    def _sse_session(self) -> aiohttp.ClientSession:
        # SSE authenticates with a bearer token, so no cookie needs to survive between
        # actions; the jar is emptied each time so every action starts a clean sign-in.
        if self._sse_http is None or self._sse_http.closed:
            self._sse_http = async_create_clientsession(self.hass, auto_cleanup=False, cookie_jar=new_cookie_jar())
        self._sse_http.cookie_jar.clear()
        return self._sse_http

    async def _esb_account(self, *, force: bool, handover: bool = False) -> bool:
        """One scheduled (or forced) ESB fetch, if due. True if ESB was contacted.

        After a failed attempt NOTHING is tried for RETRY_AFTER_FAILURE: not the schedule,
        not a restart, not "Refresh ESB now" (`force`). Only a session handed over by a
        person (`handover`) is used at once, since that needs no sign-in.
        """
        assert self.store is not None
        state = await self._run(self.store.fetch_state, "esb")
        self._retry_at["esb"] = state.retry_at(RETRY_AFTER_FAILURE)
        if state.waiting(RETRY_AFTER_FAILURE) and not handover:
            return False
        per_day = int(self.cfg.get(CONF_ESB_PER_DAY, DEFAULT_ESB_PER_DAY))
        if not force and not handover:
            if per_day <= 0:
                return False  # manual only
            interval = timedelta(hours=24 / min(per_day, MAX_ESB_PER_DAY)) + timedelta(seconds=random.randint(0, ESB_JITTER))
            if not state.failures and not state.is_due(interval):
                return False
        cfg = self.cfg
        client = EsbClient(
            self._esb_session(),
            username=cfg.get(CONF_ESB_USERNAME, ""),
            password=cfg.get(CONF_ESB_PASSWORD, ""),
            mprn=str(cfg.get(CONF_MPRN, "")).replace(" ", ""),
        )
        await self._run(self.store.mark_attempt, "esb")
        try:
            if not self._esb_restored:
                self._esb_restored = True
                # The sign-in setup or the options just made is the newest session there
                # is. Signing in again instead is what brings on ESB's CAPTCHA.
                handoff = self.hass.data.pop(DATA_ESB_HANDOFF, None)
                if handoff:
                    await self._count_esb_sign_in()  # setup's sign-in uses up the budget too
                client.restore_session(handoff or await self._run(self.store.load_session, "esb"))
            if not await client.is_authenticated():
                await self._esb_sign_in(client)
            await self._run(self.store.save_session, "esb", client.export_session())
            watermark = None
            try:
                window = await client.meter_window()
                watermark = str(window.get("end_date") or "") or None
            except (PortalError, aiohttp.ClientError) as exc:
                LOG.debug("ESB meter window unavailable: %s", exc)
            seen = await self._run(self.store.get_meta, META_ESB_WATERMARK)
            _, have = await self._run(self.store.esb_span)
            if watermark is None or watermark != seen or have is None or force:
                raw = await client.download_hdf()
                dataset = await self._run(parse_hdf, raw)
                if not dataset.readings:
                    raise PortalError("HDF parsed to zero intervals: " + "; ".join(dataset.warnings))
                window_now = self.schedule.current.window
                rows = [
                    (r.start_utc, r.kwh, window_now.band_at(r.start_utc))
                    for r in dataset.imports()
                ]
                n = await self._run(self.store.upsert_esb_intervals, rows, "esb")
                LOG.info("ESB: %d interval(s) stored", n)
                if watermark:
                    await self._run(self.store.set_meta, META_ESB_WATERMARK, watermark)
            try:
                reads = parse_billed_reads(await client.billed_reads())
                await self._run(self.store.upsert_register_reads, reads)
            except (PortalError, aiohttp.ClientError) as exc:
                LOG.debug("ESB billed reads unavailable: %s", exc)
            await self._run(self.store.save_session, "esb", client.export_session())
            await self._run(self.store.mark_success, "esb")
            self._retry_at["esb"] = None
            self.esb_error = None
        except (PortalError, aiohttp.ClientError, TimeoutError) as exc:
            # Whatever it was (ESB down, a CAPTCHA, a refused sign-in, the sign-in budget):
            # recorded, shown on the status with the time of the next try, and left alone
            # for twelve hours. The saved session is kept; no person is asked for anything.
            error = _describe(exc)
            if isinstance(exc, CaptchaEncountered):
                error = f"captcha: {error}"
            await self._run(self.store.mark_failure, "esb", error)
            self.esb_error = error
            after = await self._run(self.store.fetch_state, "esb")
            self._retry_at["esb"] = after.retry_at(RETRY_AFTER_FAILURE)
            (LOG.warning if after.failures == 1 else LOG.info)(
                "ESB: %s; trying again at %s", error, _local_time(self._retry_at["esb"])
            )
        return True

    async def _recent_esb_sign_ins(self) -> list[datetime]:
        assert self.store is not None
        now = dt_util.utcnow()
        stamps = [datetime.fromisoformat(t) for t in await self._run(self.store.get_json, META_ESB_SIGN_INS) or []]
        return [t for t in stamps if now - t < timedelta(hours=24)]

    async def _count_esb_sign_in(self) -> None:
        assert self.store is not None
        stamps = [*await self._recent_esb_sign_ins(), dt_util.utcnow()]
        await self._run(self.store.set_json, META_ESB_SIGN_INS, [t.isoformat() for t in stamps])

    async def _esb_sign_in(self, client: EsbClient) -> None:
        """A fresh sign-in, never more than ESB_MAX_SIGN_INS in 24 hours (or ESB asks for a CAPTCHA)."""
        assert self.store is not None
        recent = await self._recent_esb_sign_ins()
        if len(recent) >= ESB_MAX_SIGN_INS:
            allowed = min(recent) + timedelta(hours=24)
            raise PortalError(
                f"not signing in to ESB: {len(recent)} sign-ins in the last 24 hours already, and another "
                f"brings on its CAPTCHA; the next is allowed from {allowed.astimezone(IRELAND):%a %-d %b %H:%M}"
            )
        await self._count_esb_sign_in()
        await client.login()

    # ------------------------------------------------------- retry after failure

    def _credentials_key(self, source: str) -> str:
        cfg = self.cfg
        keys = (CONF_ESB_USERNAME, CONF_ESB_PASSWORD, CONF_MPRN) if source == "esb" else (
            CONF_SSE_USERNAME, CONF_SSE_PASSWORD, CONF_SSE_ACCOUNT, CONF_MPRN
        )
        raw = "\n".join(str(cfg.get(k) or "") for k in keys)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    async def _load_retry_state(self) -> None:
        """At start: changed sign-in details start afresh; otherwise a wait carries on."""
        assert self.store is not None
        known = await self._run(self.store.get_json, META_CREDENTIALS) or {}
        current = {source: self._credentials_key(source) for source in ("esb", "sse")}
        for source, key in current.items():
            if known.get(source) not in (None, key):
                await self._run(self.store.reset_failures, source)
            state = await self._run(self.store.fetch_state, source)
            self._retry_at[source] = state.retry_at(RETRY_AFTER_FAILURE)
        if known != current:
            await self._run(self.store.set_json, META_CREDENTIALS, current)

    def esb_wait_message(self) -> str | None:
        until = self.waiting_until("esb")
        if until is None:
            return None
        return (
            f"ESB did not work last time ({self.esb_error or 'see the ESB status'}); it is tried again by "
            f"itself at {_local_time(until)}, and not before. Nothing was tried now."
        )

    def waiting_until(self, source: str) -> datetime | None:
        """When ESB / SSE may be tried again after a failure, or None if it may now."""
        at = self._retry_at.get(source)
        return at if at is not None and dt_util.utcnow() < at else None

    async def async_supply_esb_session(self, cookies: dict[str, str]) -> None:
        """A session signed in elsewhere, handed over (the supply_esb_session service).

        Using it needs no sign-in, so it is tried at once even during a wait.
        """
        assert self.store is not None
        await self._run(self.store.save_session, "esb", cookies)
        self._esb_restored = False
        if self._esb_http is not None:
            self._esb_http.detach()
            self._esb_http = None
        if self.esb_mode != ESB_OFF:
            async with self._esb_lock:
                await self._esb_account(force=True, handover=True)
            await self.async_publish(keys={"esb_energy", "esb_energy_day", "esb_energy_night"})
            await self._relearn()
        await self.insights_coordinator.async_request_refresh()

    # --------------------------------------------------------------- insights

    async def _async_compute_insights(self) -> dict[str, Any]:
        assert self.store is not None
        now = dt_util.utcnow()
        live = self.live_coordinator.data or self.compute_live()
        cfg = self.cfg

        def work() -> dict[str, Any]:
            return self._insights(now, live, cfg)

        data = await self._run(work)
        # ESB's half-hourly ratio is the learner's prior until readings outweigh it.
        ratio = (data.get("reconcile") or {}).get("calibration")
        ratio = round(ratio, 4) if ratio else None
        if ratio != self._esb_ratio:
            self._esb_ratio = ratio
            assert self.store is not None
            await self._run(self.store.set_meta, META_ESB_RATIO, None if ratio is None else str(ratio))
            await self._relearn()
        data["calibration"] = self.cal.as_dict()
        return data

    def _insights(self, now: datetime, live: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        store = self.store
        assert store is not None
        today = local_date(now)
        since = local_midnight(today - timedelta(days=430))
        rows = store.hours(since, None)
        esb = store.esb_intervals(since, None)
        reads = store.register_reads()

        def span(first: date, last: date) -> analytics.Totals:
            a, b = analytics.local_range(first, last)
            return analytics.totals(analytics.rows_between(rows, a, b), schedule=self.schedule)

        today_live = live.get("today") or {}

        def with_today(t: analytics.Totals, first: date, last: date) -> dict[str, Any]:
            """Fold today's not-yet-stored hours into a period that contains today."""
            d = t.as_dict()
            if not (first <= today <= last):
                return d
            stored_today = analytics.totals(
                [r for r in self._today_rows if r["hour_utc"] >= local_midnight(today)], schedule=self.schedule
            )
            for key in ("house_kwh", "day_kwh", "night_kwh", "hall_kwh", "rest_kwh",
                        "energy_cost", "day_cost", "night_cost", "fixed_cost", "standing_cost",
                        "pso_cost", "total_cost", "hall_cost", "rest_cost"):
                extra = (today_live.get(key) or 0.0) - (stored_today.as_dict().get(key) or 0.0)
                d[key] = round((d.get(key) or 0.0) + max(extra, 0.0), 3)
            return d

        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)
        year_start = today.replace(month=1, day=1)
        yesterday = today - timedelta(days=1)
        summaries = {
            "today": today_live,
            "yesterday": span(yesterday, yesterday).as_dict(),
            "week": with_today(span(week_start, today), week_start, today),
            "month": with_today(span(month_start, today), month_start, today),
            "year": with_today(span(year_start, today), year_start, today),
            "last_7": span(today - timedelta(days=7), yesterday).as_dict(),
            "previous_7": span(today - timedelta(days=14), today - timedelta(days=8)).as_dict(),
            "last_30": span(today - timedelta(days=30), yesterday).as_dict(),
        }
        # The same periods WITHOUT today: the live sensors add today's figures (every few
        # seconds) to these, so week / month / year move with the clamps, not every 5 min.
        summaries_base = {
            name: (span(first, yesterday) if first <= yesterday else analytics.Totals(schedule=self.schedule)).as_dict()
            for name, first in (("week", week_start), ("month", month_start), ("year", year_start))
        }
        last_year_month_start = month_start.replace(year=month_start.year - 1)
        try:
            last_year_same_day = today.replace(year=today.year - 1)
        except ValueError:
            last_year_same_day = today.replace(year=today.year - 1, day=28)
        summaries["month_last_year"] = span(last_year_month_start, last_year_same_day).as_dict()

        a14, _ = analytics.local_range(today - timedelta(days=14), today)
        recent14 = [r for r in rows if r["hour_utc"] >= a14]
        a28, _ = analytics.local_range(today - timedelta(days=28), today)
        recent28 = [r for r in rows if r["hour_utc"] >= a28 and not r.get("partial")]
        a7, _ = analytics.local_range(today - timedelta(days=7), today)
        recent7 = [r for r in rows if r["hour_utc"] >= a7]
        t7 = analytics.totals(recent7)
        t30 = span(today - timedelta(days=30), yesterday)

        house_profile = analytics.hour_profile(recent28)
        hall_profile = analytics.hour_profile(recent28, field_name="hall_kwh")
        rest_profile = analytics.hour_profile(recent28, field_name="rest_kwh")
        shares = analytics.hour_shares(house_profile)

        hall_avg_w = (t7.hall_kwh / t7.hours * 1000) if t7.hours else None
        days30 = max(len(t30.days), 1)
        hall_daily_cost = t30.hall_cost / days30 if t30.hours else None
        hall = {
            "avg_w_7d": None if hall_avg_w is None else round(hall_avg_w, 0),
            "baseline_w": analytics.baseline_w(recent14, field_name="hall_avg_w"),
            "share_30d_pct": None if t30.hall_share is None else round(t30.hall_share * 100, 1),
            "kwh_30d": round(t30.hall_kwh, 2),
            "cost_30d": round(t30.hall_cost, 2),
            "daily_cost": None if hall_daily_cost is None else round(hall_daily_cost, 3),
            "annual_cost": None if hall_daily_cost is None else round(hall_daily_cost * 365, 0),
            "annual_kwh": round(t30.hall_kwh / days30 * 365, 0) if t30.hours else None,
            "pf_7d": None if t7.hall_pf is None else round(t7.hall_pf, 3),
            "over_house_hours_7d": t7.hall_over_house_hours,
            "night_share_pct": round(t30.hall_night_kwh / t30.hall_kwh * 100, 1) if t30.hall_kwh else None,
        }

        # Forecast: clamp days first, ESB days (scaled into clamp terms) to fill.
        calibration = self._esb_ratio or 1.0
        clamp_daily = {d: v for d, v in analytics.daily_series(rows).items() if d < today}
        esb_daily: dict[date, float] = {}
        esb_counts: dict[date, int] = {}
        for start, kwh, _band in esb:
            d = start.astimezone(IRELAND).date()
            esb_daily[d] = esb_daily.get(d, 0.0) + kwh
            esb_counts[d] = esb_counts.get(d, 0) + 1
        # ESB is the authority: a day it covers completely is ESB's figure (in clamp terms, so
        # the meter factors price it); the clamp only stands in for days ESB has not
        # published yet (the last two or three). The forecasts, the learned weights and the
        # scoring of every model are all revised as ESB's days arrive.
        esb_days = {d: v / calibration for d, v in esb_daily.items() if esb_counts[d] >= 46 and d < today}
        merged = {**clamp_daily, **esb_days}

        night_share: dict[int, float] = {}
        acc: dict[int, list[float]] = {}
        for r in rows:
            m = r["hour_utc"].astimezone(IRELAND).month
            a = acc.setdefault(m, [0.0, 0.0])
            kwh = float(r.get("house_kwh") or 0.0)
            a[1] += kwh
            if r.get("band") == BAND_NIGHT:
                a[0] += kwh
        for m, (n, t) in acc.items():
            if t > 0:
                night_share[m] = n / t

        # The billing period: SSE's own account dates when there are any (the next-bill
        # estimate uses the same), otherwise the billing date set in the options.
        sse_billing = sanitize_billing(store.get_json(META_SSE_BILLING))
        sse_history = store.get_json(META_SSE_HISTORY)
        sse_period = billing.period_from_sse(sse_billing, today, history=sse_history)
        bill = None
        anchor_prev = None
        bill_actual = (0.0, 0.0)
        if sse_period is not None:
            bill = (sse_period[0], sse_period[1])
        elif cfg.get(CONF_BILL_ANCHOR):
            try:
                anchor_day = date.fromisoformat(str(cfg[CONF_BILL_ANCHOR]))
                months = int(cfg.get("bill_months", 2))
                bill = fc.bill_period_for(today, anchor_day, months)
                if bill is not None:
                    anchor_prev = fc.bill_period_for(bill[0] - timedelta(days=1), anchor_day, months)
            except (ValueError, TypeError):
                bill = None
        if bill is not None:
            bt = with_today(span(bill[0], today), bill[0], today)
            bill_actual = (bt["house_kwh"] or 0.0, bt["total_cost"] or 0.0)

        month_t = summaries["month"]
        forecaster = fc.Forecaster(merged, night_share=night_share)

        # ---- weather: degree days, the kWh-per-degree-day fit, the weather forecast
        base = wx.DEFAULT_BASE_TEMP_C
        wrows = store.weather(since, None)
        dry = wx.by_local_day(wx.degree_hours(wrows, "temperature", base), IRELAND)
        app = wx.by_local_day(wx.degree_hours(wrows, "apparent_temperature", base), IRELAND)
        year_ago = today - timedelta(days=365)
        fit_kwh = {d: v for d, v in merged.items() if d >= year_ago}
        weather_fit = wx.best_fit(fit_kwh, dry, app, base)
        basis_hdd = app if weather_fit is not None and weather_fit.basis == "feels like" else dry
        hall_fit = wx.fit(
            {d: v for d, v in hall_daily_all.items() if d >= year_ago}, basis_hdd,
            basis=weather_fit.basis if weather_fit else "thermometer", base=base,
        ) if (hall_daily_all := {d: v for d, v in analytics.daily_series(rows, "hall_kwh").items() if d < today}) else None
        weather = self._weather_summary(now, today, wrows, dry, app, basis_hdd, weather_fit, hall_fit, fit_kwh, night_share)

        # ---- the ensemble: score the log, tune the weights, write tonight's predictions
        # ---- "what drives usage": the learned regression, and the weather it forecasts with
        wx_days = learner.daily_weather(wrows, IRELAND, base)
        observed = {d: w for d, w in wx_days.items() if d < today}
        sources = self._blend_sources(store, today, wx_days, observed)
        regression = learner.fit(merged, observed, today=today)
        if regression.ready and store.get_meta("drivers_saved_on") != today.isoformat():
            store.save_weights(today, "drivers", regression.weights)
            store.set_meta("drivers_saved_on", today.isoformat())
        weather_normals = learner.normals(observed)
        ctx = ens.Context(
            daily=merged, hdd=basis_hdd, normal=wx.normal_hdd(basis_hdd), fit=weather_fit, seasonal=forecaster,
            regression=regression, weather_days=sources["days"], weather_normals=weather_normals,
        )
        drivers = {
            **regression.as_dict(),
            "today": regression.contributions(today, sources["days"].get(today) or weather_normals.get((today.month, today.day)))
            if regression.ready and (sources["days"].get(today) or weather_normals.get((today.month, today.day))) else None,
            "history": store.weight_history("drivers", today - timedelta(days=180)),
            "forecast_sources": sources["weights"],
        }
        log = store.forecast_log(today - timedelta(days=ens.SCORE_WINDOW_DAYS + 30))
        scored_live = {r["target"] for r in log if r["kind"] == "live" and r["made_on"] == r["target"] and r["target"] < today}
        if len(scored_live) < 14 and not store.get_meta(META_BOOTSTRAPPED) and len(merged) >= 21:
            store.log_forecasts(ens.bootstrap(ctx, today), kind="backtest")
            store.set_meta(META_BOOTSTRAPPED, today.isoformat())
            log = store.forecast_log(today - timedelta(days=ens.SCORE_WINDOW_DAYS + 30))
        weights = ens.tune(log, merged, today=today)
        weights.bootstrapped = len(scored_live) < 14
        if not store.logged_on(today):
            store.log_forecasts(ens.log_rows(ctx, weights, today), kind="live")
        ensemble_days = ens.forecast_days(ctx, weights, today)
        engine = {
            **weights.as_dict(),
            "recent": ens.recent_scores(log, merged, today),
            "models_today": {m: round(v, 2) for m, v in ctx.all_models(today, today).items()},
        }
        report = forecaster.forecast(
            today=today,
            schedule=self.schedule,
            today_so_far_kwh=float(today_live.get("house_kwh") or 0.0),
            today_so_far_cost=float(today_live.get("total_cost") or 0.0),
            today_elapsed_share=analytics.elapsed_share(shares, now),
            month_actual_kwh=float(month_t.get("house_kwh") or 0.0),
            month_actual_cost=float(month_t.get("total_cost") or 0.0),
            bill_period=bill,
            bill_actual_kwh=bill_actual[0],
            bill_actual_cost=bill_actual[1],
            meter_factors={"day": self.cal.day, "night": self.cal.night},
            daily_override=ensemble_days,
            sigma_override=ens.residual_sigma(log, merged, today),
            method_label="ensemble" if ensemble_days else None,
        )
        if weather.get("forecast"):
            seasonal7 = sum(
                v for i in range(1, 8)
                if (v := ctx.predict("seasonal", today, today + timedelta(days=i))) is not None
            )
            w7 = weather["forecast"]["kwh_7"]
            weather["forecast"]["seasonal_kwh_7"] = round(seasonal7, 1)
            weather["forecast"]["gap_pct"] = round((w7 - seasonal7) / seasonal7 * 100, 1) if seasonal7 else None

        # ---- the next bill, priced like the supplier prices it
        bill_estimate = None
        period = sse_period
        source = "SSE account"
        if period is None and bill is not None:
            period = (bill[0], bill[1], {})
            source = "billing date in options"
        if period is not None and report.available:
            start, end, extras = period
            bill_estimate = billing.estimate(
                start=start, end=end, today=today,
                rows=[r for r in rows if r["local_date"] >= start.isoformat()],
                live_today=today_live, schedule=self.schedule, night_share=night_share,
                factors={"day": self.cal.day, "night": self.cal.night},
                next_days=report.next_days, today_remaining_kwh=report.today_remaining_kwh or 0.0,
                source=source, extras=extras,
            ).as_dict()
        last_bill = billing.summarize_last_bill(
            billing=sse_billing,
            history=sse_history,
            anchor_prev=anchor_prev,
            today=today,
            rows=rows,
            schedule=self.schedule,
            factors={"day": self.cal.day, "night": self.cal.night},
        )

        hall_daily = hall_daily_all
        hall_fc = None
        if hall_daily:
            # The 24/7 circuit has no strong seasonal shape of its own to borrow, so its
            # prior is flat; its night share is simply its own.
            hall_night = (hall["night_share_pct"] or 37.5) / 100
            hall_forecaster = fc.Forecaster(
                hall_daily,
                night_share={m: hall_night for m in range(1, 13)},
                prior={m: 1.0 for m in range(1, 13)},
            )
            hall_report = hall_forecaster.forecast(
                today=today, schedule=self.schedule, meter_factors={"day": self.cal.day, "night": self.cal.night}
            )
            if hall_report.available:
                hall_fc = {
                    "year_kwh": round(hall_report.year_kwh or 0.0, 0),
                    "year_energy_cost": round(sum(d.cost - self.schedule.for_date(d.day).fixed_for_day(d.day) for d in hall_report.next_days[:365]), 0),
                    "months": [
                        {"label": p.label, "start": p.start.isoformat(), "kwh": round(p.total_kwh, 1),
                         "cost": round(p.total_cost - p.fixed_cost, 2)}
                        for p in hall_report.months
                    ],
                }

        reconciliation = rc.reconcile(
            [r for r in rows if r["hour_utc"] >= local_midnight(today - timedelta(days=60))
             and r.get("source") != downtime.SOURCE],
            [e for e in esb if e[0] >= local_midnight(today - timedelta(days=60))],
        )

        reads_compare: list[dict[str, Any]] = []
        if self._readings and reads:
            series = calib.register_series(
                [r for r in self._readings if r.origin != "esb"], rows, self.cal
            )
            reads_compare = reg.compare_with_reads(
                series,
                reads,
                lambda d: local_midnight(date.fromisoformat(d)),
            )

        fetch = store.fetch_state("esb")
        sse_fetch = store.fetch_state("sse")
        last_sub = store.last_submission(status="submitted")
        last_any = store.last_submission()
        next_allowed = self._sse_next_allowed(last_sub)
        anchors = store.anchors()
        stats = store.stats()
        collection = self._collection_status(now, today, stats, rows, wrows, weather)

        return {
            "computed": now.isoformat(),
            "summaries": summaries,
            "summaries_base": summaries_base,
            "compare_week": analytics.compare(
                span(today - timedelta(days=7), yesterday), span(today - timedelta(days=14), today - timedelta(days=8))
            ),
            "baseline_w": analytics.baseline_w(recent14),
            "hall": hall,
            "hall_forecast": hall_fc,
            "profile": {
                "house_kw": {str(h): round(v, 3) for h, v in house_profile.items()},
                "hall_kw": {str(h): round(v, 3) for h, v in hall_profile.items()},
                "rest_kw": {str(h): round(v, 3) for h, v in rest_profile.items()},
                "weekday_kwh": {str(w): round(v, 2) for w, v in analytics.weekday_profile(rows[-24 * 7 * 12 :]).items()},
            },
            "power": {
                "house_pf_7d": None if t7.house_pf is None else round(t7.house_pf, 3),
                "hall_pf_7d": None if t7.hall_pf is None else round(t7.hall_pf, 3),
                "voltage_min_7d": t7.voltage_min,
                "voltage_max_7d": t7.voltage_max,
                "peak_w_7d": t7.peak_w,
            },
            "rates": {
                "effective_30d": t30.effective_rate,
                "all_in_30d": t30.all_in_rate,
                "night_share_30d_pct": None if t30.night_share is None else round(t30.night_share * 100, 1),
            },
            "forecast": report,
            "engine": engine,
            "drivers": drivers,
            "weather": weather,
            "bill": bill_estimate,
            "last_bill": last_bill,
            "sse_billing": sse_billing,
            "sse_history": sse_history,
            "hall_weather": hall_fit.as_dict() if hall_fit else None,
            "anomalies": analytics.anomalies(merged),
            "reconcile": reconciliation.as_dict(now),
            "reads_compare": reads_compare,
            "esb": {
                "mode": self.esb_mode,
                "last_success": fetch.last_success.isoformat() if fetch.last_success else None,
                "last_attempt": fetch.last_attempt.isoformat() if fetch.last_attempt else None,
                "failures": fetch.failures,
                "error": self.esb_error or fetch.last_error,
                "next_attempt": None if (w := self.waiting_until("esb")) is None else w.isoformat(),
                "reads": reads[-12:],
            },
            "sse": {
                "mode": self.sse_mode,
                "meter": self.sse_meter.as_dict() if self.sse_meter else None,
                "last_submitted": last_sub,
                "last_attempt": last_any,
                "next_allowed": next_allowed.isoformat() if next_allowed else None,
                "error": self.sse_error or (sse_fetch.last_error if sse_fetch.failures else None),
                "failures": sse_fetch.failures,
                "next_attempt": None if (w := self.waiting_until("sse")) is None else w.isoformat(),
            },
            "anchors": [
                {**a, "taken_at": a["taken_at"].isoformat()} for a in anchors[-10:]
            ],
            "store": {**stats, "integrity": store.integrity_ok()[1], **self.db_status},
            "tariffs": [t.as_dict() for t in self.schedule.periods],
            "collection": collection,
            "energy_dashboard": self.energy_status,
        }

    def _collection_status(
        self, now: datetime, today: date, stats: dict[str, Any], rows: list[dict[str, Any]],
        wrows: list[dict[str, Any]], weather: dict[str, Any],
    ) -> dict[str, Any]:
        """Which of "loading", "no data", "collecting", "today so far", "weather waiting",
        "upstream failed" the data is in, in words the dashboard can show as they are.

        Nothing is inferred from an absence alone: an empty store while the first load
        is still running is "loading", not "no data"; a few days of history is
        "collecting", not an error; today is always incomplete until midnight.
        """
        loading = self.initial_load.get("state") in ("pending", "running")
        hours = int(stats.get("hours") or 0)
        first = stats.get("clamp_first_hour") or stats.get("first_hour")
        first_local = datetime.fromisoformat(first).astimezone(IRELAND) if first else None
        esb_first = stats.get("first_hour") if stats.get("esb_hours") else None
        esb_local = datetime.fromisoformat(esb_first).astimezone(IRELAND) if esb_first else None
        complete_days = len([d for d in analytics.daily_series(rows) if d < today])
        today_hours = sum(1 for r in rows if r.get("local_date") == today.isoformat())
        elapsed = max(int((floor_hour(now) - local_midnight(today)) / HOUR), 0)
        if hours == 0:
            state = "loading" if loading else "no_data"
            message = (
                "Reading the clamp's history from Home Assistant's recorder…"
                if loading
                else "No electricity data yet: the first hour is stored a few minutes after it ends."
            )
        elif complete_days < 7:
            state = "collecting"
            message = (
                f"Collecting since {first_local:%-d %b %H:%M}: {hours} hour(s) stored, "
                f"{complete_days} complete day(s). Forecasts start after 5 complete days and "
                "improve with every day; nothing is filled in that was not measured."
            )
        elif esb_local is not None and first_local is not None and esb_local < first_local and (
            now - first_local < timedelta(days=7)
        ):
            state = "collecting"
            message = (
                f"The clamp has measured since {first_local:%-d %b %H:%M}; the history before it, from "
                f"{esb_local:%-d %b %Y}, is ESB's meter data (no hall / rest split)."
            )
        else:
            state, message = "ok", ""
        if today_hours == 0:
            today_message = (
                "No completed hour stored yet today: each hour is added a few minutes after it ends."
                if hours
                else ""
            )
        elif today_hours + 1 < elapsed:
            today_message = f"Today so far: {today_hours} of {elapsed} completed hours stored (the rest are being read)."
        else:
            today_message = f"Today so far: {today_hours} completed hour(s); the current hour is live."

        latest = (weather.get("now") or {}).get("hour")
        last_ok = self.weather_status.get("last_success")
        if not wrows:
            w_state = "loading" if loading else ("error" if self.weather_error else "waiting")
            w_message = {
                "loading": "Fetching weather from Open-Meteo…",
                "error": f"Open-Meteo is unavailable ({self.weather_error}); it is tried again every 12 hours.",
                "waiting": "Waiting for the first weather data from Open-Meteo.",
            }[w_state]
        elif self.weather_error:
            w_state = "error"
            w_message = f"Open-Meteo is unavailable ({self.weather_error}); showing the last data received."
        elif last_ok and now - datetime.fromisoformat(last_ok) > timedelta(hours=36):
            w_state = "stale"
            w_message = "Weather has not been refreshed for over a day; showing the last data received."
        else:
            w_state, w_message = "ok", ""

        return {
            "state": state,
            "message": message,
            "today_message": today_message,
            "today_hours": today_hours,
            "hours": hours,
            "complete_days": complete_days,
            "first_hour": first_local.isoformat() if first_local else None,
            "esb_history_from": esb_local.isoformat() if esb_local else None,
            "esb_hours": int(stats.get("esb_hours") or 0),
            "last_hour": stats.get("last_hour"),
            "initial_load": self.initial_load.get("state"),
            "initial_step": self.initial_load.get("step"),
            "unpriced_hours": int(stats.get("unpriced_hours") or 0),
            "first_priced_day": self.schedule.first_priced_day.isoformat(),
            "weather_state": w_state,
            "weather_message": w_message,
            "weather_latest_hour": latest,
            "weather_last_success": last_ok,
        }

    def _blend_sources(self, store, today: date, wx_days: dict, observed: dict) -> dict[str, Any]:
        """Blend Open-Meteo and the weather entity's forecast by how well each has done.

        Every night both sources' degree-day forecasts for the next five days are logged;
        once those days have happened they are scored against the observed archive, and
        the blend follows the scores. Until then the two are averaged.
        """
        entity = self._entity_forecast
        days = dict(wx_days)
        if not entity:
            return {"days": days, "weights": {"openmeteo": 1.0}}
        if not store.get_meta(f"hdd_logged:{today.isoformat()}"):
            rows = []
            for i in range(1, 6):
                target = today + timedelta(days=i)
                if target in wx_days:
                    rows.append({"made_on": today, "target": target, "model": "hdd:openmeteo", "kwh": wx_days[target].hdd})
                if target in entity:
                    rows.append({"made_on": today, "target": target, "model": "hdd:entity", "kwh": entity[target].hdd})
            store.log_forecasts(rows, kind="weather")
            store.set_meta(f"hdd_logged:{today.isoformat()}", "1")
        log = store.forecast_log(today - timedelta(days=70))
        weights = learner.source_weights(log, {d: w.hdd for d, w in observed.items()}, today) or {
            "openmeteo": 0.5, "entity": 0.5
        }
        w_om, w_en = weights.get("openmeteo", 0.0), weights.get("entity", 0.0)
        for d, e in entity.items():
            if d < today:
                continue
            o = wx_days.get(d)
            if o is None:
                days[d] = e
                continue
            total = (w_om + w_en) or 1.0
            mix = lambda a, b: (a * w_om + b * w_en) / total  # noqa: E731
            days[d] = learner.DayWeather(
                hdd=mix(o.hdd, e.hdd), hdd_app=mix(o.hdd_app, e.hdd_app), rad=o.rad,
                wind=mix(o.wind, e.wind) if e.wind else o.wind, rain=mix(o.rain, e.rain),
            )
        return {"days": days, "weights": weights}

    def _weather_summary(self, now, today, wrows, dry, app, basis_hdd, fit, hall_fit, kwh, night_share) -> dict[str, Any]:
        """Outside now, degree days today, the fitted sensitivity, the weather forecast."""
        past = [r for r in wrows if r["hour_utc"] <= now and r.get("temperature") is not None]
        latest = past[-1] if past else None
        today_rows = [r for r in wrows if r["local_date"] == today.isoformat() and r.get("temperature") is not None]
        # Degree days accrue like energy: what today's COMPLETED hours have added so far.
        # The whole-day figure, which leans on the forecast for hours still to come, is
        # reported alongside, never instead.
        done_rows = [r for r in today_rows if r["hour_utc"] + HOUR <= now]
        out: dict[str, Any] = {
            "error": self.weather_error,
            "requests": int(self.store.get_meta(META_WEATHER_REQUESTS) or 0),
            "hours": len(wrows),
            "attribution": wx.ATTRIBUTION,
            "now": None if latest is None else {
                k: latest.get(k) for k in ("temperature", "apparent_temperature", "humidity", "wind_speed",
                                           "wind_gusts", "precipitation", "cloud_cover", "pressure", "source")
            } | {"hour": latest["hour_utc"].isoformat()},
            "hdd_today": round(sum(max(0.0, wx.DEFAULT_BASE_TEMP_C - float(r["temperature"])) for r in done_rows) / 24, 2)
            if today_rows else None,
            "hdd_today_hours": len(done_rows),
            "hdd_today_expected": round(
                sum(max(0.0, wx.DEFAULT_BASE_TEMP_C - float(r["temperature"])) for r in today_rows) / 24, 2
            )
            if today_rows else None,
            "fit": fit.as_dict() if fit else None,
            "hall_fit": hall_fit.as_dict() if hall_fit else None,
        }
        if fit is not None:
            recent = [d for d in kwh if d in basis_hdd and d >= today - timedelta(days=365)]
            total = sum(kwh[d] for d in recent)
            heating = sum(fit.slope * basis_hdd[d] for d in recent)
            out["heating_share_pct"] = round(heating / total * 100, 1) if total else None
            tariff = self.schedule.current
            ns = night_share.get(today.month, 0.33)
            blended = (1 - ns) * tariff.day_rate * self.cal.day + ns * tariff.night_rate * self.cal.night
            out["eur_per_degree_day"] = round(fit.slope * blended, 3)
            # The coming sixteen days, from the forecast hours.
            future = [r for r in wrows if r["source"] == "forecast" and r["local_date"] >= today.isoformat()]
            per_day: dict[str, list[dict[str, Any]]] = {}
            for r in future:
                per_day.setdefault(r["local_date"], []).append(r)
            days = []
            for d, hours in sorted(per_day.items()):
                if len(hours) < 20:
                    continue
                col = "apparent_temperature" if fit.basis == "feels like" else "temperature"
                temps = [float(h[col]) for h in hours if h.get(col) is not None]
                if not temps:
                    continue
                hdd = sum(max(0.0, wx.DEFAULT_BASE_TEMP_C - t) for t in temps) / len(temps)
                day = date.fromisoformat(d)
                kwh_day = fit.predict(hdd)
                t = self.schedule.for_date(day)
                ns = night_share.get(day.month, 0.33)
                cost = kwh_day * ((1 - ns) * t.day_rate * self.cal.day + ns * t.night_rate * self.cal.night) + t.fixed_for_day(day)
                days.append({
                    "date": d,
                    "kwh": round(kwh_day, 2),
                    "cost": round(cost, 2),
                    "hdd": round(hdd, 2),
                    "temperature_c": round(sum(float(h["temperature"]) for h in hours if h.get("temperature") is not None) / len(hours), 1),
                    "apparent_c": round(sum(float(h["apparent_temperature"]) for h in hours if h.get("apparent_temperature") is not None) / len(hours), 1),
                })
            if days:
                out["forecast"] = {
                    "kwh_7": round(sum(x["kwh"] for x in days[1:8]), 1),
                    "cost_7": round(sum(x["cost"] for x in days[1:8]), 2),
                    "per_day": days,
                }
        return out

    # ----------------------------------------------------------------- tables

    async def async_table(self, period: str, start: date | None, end: date | None) -> dict[str, Any]:
        assert self.store is not None
        now = dt_util.utcnow()
        pending = self._pending()
        live_rows = []
        for hour, (house_kwh, hall_kwh) in pending.items():
            band = self.schedule.for_instant(hour).window.band_at(hour)
            t = self.schedule.in_force(local_date(hour))
            fixed = None if t is None else t.fixed_for_hour(hour)  # the day's charge, at midnight
            factor = self.cal.factor_at(hour, band)
            rate = None if t is None else t.rate(band) * factor
            hall_f = self._live_hall_factor()
            priced = rate is not None
            live_rows.append(
                {
                    "hour_utc": hour,
                    "local_date": local_date(hour).isoformat(),
                    "band": band,
                    "house_kwh": round(house_kwh, 4),
                    "hall_kwh": None if hall_kwh is None else round(hall_kwh, 4),
                    "hall_factor": hall_f,
                    "rest_kwh": None if hall_kwh is None else round(max(house_kwh - hall_kwh * hall_f, 0.0), 4),
                    "unit_rate_eur": rate,
                    "house_cost_eur": house_kwh * rate if priced else None,
                    "hall_cost_eur": None if hall_kwh is None or not priced else min(hall_kwh * hall_f, house_kwh) * rate,
                    "rest_cost_eur": None if hall_kwh is None or not priced else max(house_kwh - hall_kwh * hall_f, 0.0) * rate,
                    "meter_kwh": round(house_kwh * factor, 4),
                    "meter_factor": factor,
                    "fixed_cost_eur": fixed,
                    "tariff_from": None if t is None else t.valid_from.isoformat(),
                    "partial": True,
                }
            )
        readings = list(self._readings)
        cal = self.cal

        def work() -> dict[str, Any]:
            a = local_midnight(start) if start is not None else None
            b = local_midnight(end + timedelta(days=1)) if end is not None else None
            hours = self.store.hours(a, b)
            esb = self.store.esb_intervals(a, b)
            # A day with no stored hour yet still starts at its fixed charge.
            stored_days = {r["local_date"] for r in hours}
            for row in book_fixed([r for r in live_rows if r["local_date"] not in stored_days], self.schedule):
                live_rows[:] = [row if r["hour_utc"] == row["hour_utc"] else r for r in live_rows]
            series = []
            if readings:
                # The register history passes through every real reading, so it is
                # computed over the whole record and then windowed by the table.
                series = calib.register_series(readings, self.store.hours() + live_rows, cal)
            builder = TableBuilder(hours, live=live_rows, esb=esb, registers=series, now=now)
            return builder.build(period, start, end)

        return await self._run(work)

    async def async_submissions(self) -> dict[str, Any]:
        assert self.store is not None
        rows = await self._run(self.store.submissions)

        def fmt(text: str) -> str:
            try:
                return datetime.fromisoformat(text).astimezone(IRELAND).strftime("%a %-d %b %Y %H:%M:%S")
            except (TypeError, ValueError):
                return str(text)

        return submissions_table(rows, fmt)

    # -------------------------------------------------------------------- SSE

    def _sse_client(self, http: aiohttp.ClientSession) -> SseClient:
        cfg = self.cfg
        return SseClient(
            http,
            cfg.get(CONF_SSE_USERNAME, ""),
            cfg.get(CONF_SSE_PASSWORD, ""),
            account=cfg.get(CONF_SSE_ACCOUNT),
            mprn=cfg.get(CONF_MPRN),
            fallback_config=self.sse_portal_config,
        )

    @callback
    def _on_sse_sync(self, _now: datetime) -> None:
        self._spawn(self._sse_sync, "SSE sync")

    async def _sse_sync(self) -> None:
        """Once a day, read-only: has SSE got a reading we have not seen?

        After a failure, twelve hours later (and again twelve hours after that, and so
        on); never sooner, a restart included.
        """
        assert self.store is not None
        state = await self._run(self.store.fetch_state, "sse")
        self._retry_at["sse"] = state.retry_at(RETRY_AFTER_FAILURE)
        if state.waiting(RETRY_AFTER_FAILURE):
            return
        if not state.failures:
            last = state.last_success
            if last is None and (synced := await self._run(self.store.get_meta, META_SSE_SYNCED)):
                last = datetime.fromisoformat(synced)  # 1.0.x kept only this
            have_billing = bool(await self._run(self.store.get_json, META_SSE_BILLING))
            if last and have_billing and dt_util.utcnow() - last < SSE_SYNC_INTERVAL:
                return
        await self._run(self.store.set_meta, META_SSE_SYNCED, dt_util.utcnow().isoformat())
        try:
            await self.async_check_sse()
        except HomeAssistantError as exc:
            LOG.debug("daily SSE check: %s", exc)

    async def _sse_failed(self, error: str) -> None:
        """Record a failed SSE contact: shown on the status, retried in twelve hours."""
        assert self.store is not None
        self.sse_error = error
        await self._run(self.store.mark_failure, "sse", error)
        after = await self._run(self.store.fetch_state, "sse")
        self._retry_at["sse"] = after.retry_at(RETRY_AFTER_FAILURE)
        (LOG.warning if after.failures == 1 else LOG.info)(
            "SSE: %s; trying again at %s", error, _local_time(self._retry_at["sse"])
        )

    def _sse_wait_message(self) -> str | None:
        until = self.waiting_until("sse")
        if until is None:
            return None
        return (
            f"SSE did not answer properly last time ({self.sse_error or 'see the SSE status'}); it is tried "
            f"again by itself at {_local_time(until)}, and not before. Nothing was sent or tried now."
        )

    def _sse_next_allowed(self, last_sub: dict[str, Any] | None) -> datetime | None:
        """When SSE takes the next reading: 24 hours after the last one it accepted.

        That is the later of a reading sent from here and the newest one SSE itself
        holds, which may have been entered on SSE's own site or by phone.
        """
        sent = [datetime.fromisoformat(last_sub["created_at"])] if last_sub is not None else []
        held = [
            t for r in (self.sse_meter.registers if self.sse_meter else [])
            if (t := _sse_time(r.last_inserted)) is not None
        ]
        times = sent + held
        return max(times) + SSE_COOLDOWN if times else None

    def _sse_reading(self, info: MeterInfo) -> calib.Reading | None:
        day, night = info.register("day"), info.register("night")
        if day is None or night is None or day.last_value is None or night.last_value is None:
            return None
        if day.estimated or night.estimated:
            return None  # SSE's own guess, not the meter: nothing to learn from
        when = _sse_time(day.last_inserted or day.last_date)
        if when is None:
            return None
        return calib.Reading(when, float(day.last_value), float(night.last_value), "sse")

    async def _ingest_sse(self, info: MeterInfo) -> bool:
        """SSE's latest reading becomes a learning point, unless it is ours echoed back."""
        assert self.store is not None
        reading = self._sse_reading(info)
        if reading is None:
            return False
        if any(abs(r.day - reading.day) < 0.5 and abs(r.night - reading.night) < 0.5 for r in self._readings):
            return False
        submissions = await self._run(self.store.submissions, 100)
        if any(
            s["status"] == "submitted" and s["origin"] == "estimate"
            and int(s["day_kwh"]) == int(reading.day) and int(s["night_kwh"]) == int(reading.night)
            for s in submissions
        ):
            LOG.debug("SSE's last reading is the estimate we sent; not learning from it")
            return False
        await self._run(self.store.add_anchor, reading.taken_at, reading.day, reading.night, "sse")
        LOG.info("new reading from SSE (%s): day %s night %s", reading.taken_at, reading.day, reading.night)
        await self._relearn()
        return True

    async def async_check_sse(self) -> MeterInfo:
        """Read-only: sign in and read the meter details. Never submits anything."""
        if self.sse_mode != SSE_LIVE:
            raise HomeAssistantError(
                "SSE is not in live mode, so the integration does not contact SSE at all. "
                "Change the SSE mode in the integration's options to use this."
            )
        if (wait := self._sse_wait_message()) is not None:
            raise HomeAssistantError(wait)
        assert self.store is not None
        async with self._sse_lock:
            await self._run(self.store.mark_attempt, "sse")
            try:
                client = self._sse_client(self._sse_session())
                info = await client.meter_info()
                billing_info = await client.billing_info()
            except (PortalError, aiohttp.ClientError, TimeoutError) as exc:
                await self._sse_failed(_describe(exc))
                raise HomeAssistantError(f"SSE check failed: {self.sse_error}") from exc
            except Exception as exc:  # noqa: BLE001 - an answer we could not read
                # The portal changed shape: say so on the status, keep the last good
                # account data, and never let it reach anything else.
                LOG.debug("SSE answered something unexpected", exc_info=True)
                await self._sse_failed(f"unexpected answer from SSE: {_describe(exc)}")
                raise HomeAssistantError(f"SSE check failed: {self.sse_error}") from exc
            try:
                history = await client.reading_history(info.premises_id)
            except Exception as exc:  # noqa: BLE001 - optional; the meter details stand alone
                LOG.debug("SSE reading history unavailable: %s", _describe(exc))
                history = None
            assert self.store is not None
            if client.config is not None:
                self.sse_portal_config = client.config
                await self._run(self.store.set_json, META_SSE_CONFIG, client.config.as_dict())
            await self._run(self.store.set_json, META_SSE_BILLING, billing_info)
            if history is not None:
                await self._run(self.store.set_json, META_SSE_HISTORY, history)
            await self._run(self.store.mark_success, "sse")
            self._retry_at["sse"] = None
            self.sse_error = None
            self.sse_meter = info
            assert self.store is not None
            await self._run(self.store.set_json, META_SSE_METER, info.as_dict())
        await self._ingest_sse(info)
        await self.insights_coordinator.async_request_refresh()
        return info

    async def async_submit_reading(
        self, day: int | None = None, night: int | None = None, *, force: bool = False
    ) -> dict[str, Any]:
        """Send a meter reading. Only ever called from a button press or a confirmed service call."""
        assert self.store is not None
        if self.sse_mode == SSE_OFF:
            raise HomeAssistantError("SSE meter reading submission is turned off in the integration's options")
        live = self.compute_live()
        planned = self.reading_to_submit(live.get("registers"))
        origin = planned["origin"]
        if day is not None or night is not None:
            origin = "manual"
        day = int(day if day is not None else planned["day"]) if (day is not None or planned["day"] is not None) else None
        night = int(night if night is not None else planned["night"]) if (night is not None or planned["night"] is not None) else None
        if day is None or night is None:
            raise HomeAssistantError(
                "there is no reading to submit yet: enter the meter's current reading in the "
                "integration's options (or the reading boxes on the dashboard)"
            )
        now = dt_util.utcnow()
        record: dict[str, Any] = {
            "read_at": now.replace(microsecond=0).isoformat(),
            "day_kwh": day,
            "night_kwh": night,
            "origin": origin,
            "mode": self.sse_mode,
        }

        async with self._sse_lock:
            # Refuse here, before signing in, when SSE already has a reading inside its
            # 24 hours: one sent from here, or one entered on SSE's own site or app.
            last = await self._run(self.store.last_submission, status="submitted")
            allowed = self._sse_next_allowed(last)
            if allowed is not None and now < allowed:
                msg = (
                    "not sent: SSE accepts one reading every 24 hours and has one from "
                    f"{(allowed - SSE_COOLDOWN).astimezone(IRELAND):%a %-d %b %H:%M}; "
                    f"the next is allowed from {allowed.astimezone(IRELAND):%a %-d %b %H:%M}"
                )
                await self._run(self.store.log_submission, {**record, "status": "blocked", "message": msg})
                raise HomeAssistantError(msg)

            estimate = live.get("registers")
            if estimate and not force:
                problem = _differs_from_estimate(day, night, estimate)
                if problem:
                    msg = f"not sent: {problem}. Check the meter, or submit with force if you are sure."
                    await self._run(self.store.log_submission, {**record, "status": "blocked", "message": msg})
                    raise HomeAssistantError(msg)

            if self.sse_mode == SSE_DRY_RUN:
                payload = (
                    build_payload(self.sse_meter, day, night)
                    if self.sse_meter is not None
                    else {
                        "deDate": now.astimezone(IRELAND).strftime("%d/%m/%Y"),
                        "meterModel": "<from SSE>",
                        "meterNumber": "<from SSE>",
                        "meterServiceId": "<from SSE>",
                        "readings": [
                            {"dialsNum": "<from SSE>", "meterReading": str(day), "registerNum": 1},
                            {"dialsNum": "<from SSE>", "meterReading": str(night), "registerNum": 2},
                        ],
                    }
                )
                msg = "dry run: nothing was sent to SSE. This is exactly what live mode would post."
                await self._run(
                    self.store.log_submission, {**record, "status": "dry_run", "message": msg, "payload": payload}
                )
                persistent_notification.async_create(
                    self.hass,
                    f"**Dry run, nothing sent.** Day **{day}**, night **{night}** would be submitted to "
                    f"SSE Airtricity as:\n\n```\n{payload}\n```",
                    title="Electricity Analyzer: meter reading (dry run)",
                    notification_id=f"{DOMAIN}_reading",
                )
                await self.insights_coordinator.async_request_refresh()
                return {"status": "dry_run", "payload": payload}

            if (wait := self._sse_wait_message()) is not None:
                await self._run(self.store.log_submission, {**record, "status": "blocked", "message": wait})
                raise HomeAssistantError(f"not sent: {wait}")
            await self._run(self.store.mark_attempt, "sse")
            try:
                client = self._sse_client(self._sse_session())
                payload, answer, after = await client.submit_reading(day, night)
            except SubmissionBlocked as exc:
                await self._run(self.store.log_submission, {**record, "status": "blocked", "message": str(exc)})
                raise HomeAssistantError(f"not sent: {exc}") from exc
            except (PortalError, aiohttp.ClientError, TimeoutError) as exc:
                await self._sse_failed(_describe(exc))
                await self._run(self.store.log_submission, {**record, "status": "failed", "message": self.sse_error})
                raise HomeAssistantError(f"SSE did not accept the reading: {self.sse_error}") from exc

            if client.config is not None:
                self.sse_portal_config = client.config
                await self._run(self.store.set_json, META_SSE_CONFIG, client.config.as_dict())
            await self._run(self.store.mark_success, "sse")
            self._retry_at["sse"] = None
            self.sse_error = None
            self.sse_meter = after
            await self._run(self.store.set_json, META_SSE_METER, after.as_dict())
            await self._run(
                self.store.log_submission,
                {
                    **record,
                    "status": "submitted",
                    "message": str(answer.get("message") or "accepted"),
                    "payload": payload,
                    "response": answer,
                },
            )
        if origin == "manual":
            # A reading typed in from the meter is a better anchor than the estimate.
            await self.async_set_anchor(day, night, now, origin="submitted")
        self.override_day = self.override_night = None
        persistent_notification.async_create(
            self.hass,
            f"Submitted to SSE Airtricity: day **{day}**, night **{night}**. SSE answered: "
            f"{answer.get('message') or answer}.",
            title="Electricity Analyzer: meter reading sent",
            notification_id=f"{DOMAIN}_reading",
        )
        self._push_live_now()
        await self.insights_coordinator.async_request_refresh()
        return {"status": "submitted", "payload": payload, "answer": answer}


def _differs_from_estimate(day: int, night: int, estimate: dict[str, Any]) -> str | None:
    """Why a reading is too far from the integration's own estimate, or None."""
    problems = []
    for kind, value in (("day", day), ("night", night)):
        est = estimate.get(f"{kind}_dial")
        since = float(estimate.get(f"since_anchor_{kind}_kwh") or 0.0)
        if est is None:
            continue
        margin = max(ESTIMATE_MARGIN_UNITS, ESTIMATE_MARGIN_SHARE * since)
        if abs(value - est) > margin:
            problems.append(f"{kind} {value} is {value - est:+d} from the estimate {est} (allowed ±{margin:.0f})")
    return "; ".join(problems) or None
