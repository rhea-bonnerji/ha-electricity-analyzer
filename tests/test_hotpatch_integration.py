"""1.0.2 hotpatch, inside Home Assistant: upgrading an existing 1.0.1 installation keeps
every row, weight and session; the Energy dashboard; ESB never signs in on an outage or
after a CAPTCHA by itself; weather needs no electricity data; the dashboard always has
a state to show. Nothing here touches a network: portals and Open-Meteo are faked."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components import persistent_notification
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.electricity_analyzer.const import DOMAIN
from custom_components.electricity_analyzer.engine import weather as wx
from custom_components.electricity_analyzer.engine.errors import AuthFailed, CaptchaEncountered, PortalError
from custom_components.electricity_analyzer.engine.hourly import build_hour
from custom_components.electricity_analyzer.engine.store import Store
from custom_components.electricity_analyzer.engine.tariff import (
    DEFAULT_TARIFF,
    IRELAND,
    Tariff,
    TariffSchedule,
    local_date,
    local_midnight,
)
from custom_components.electricity_analyzer.engine.weather import Observation
from test_integration import _CountingEsb, _no_ip_lookup, _shelly, HALL, HOUSE  # noqa: F401 - fixture reused

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

RUNTIME = "custom_components.electricity_analyzer.runtime"
FLOW = "custom_components.electricity_analyzer.config_flow"
UTC = timezone.utc
LEGACY = Tariff(date(2026, 10, 5), "SSE Airtricity", "30% Day Night (Nightsaver)", 0.3238, 0.196, 359.71, 6.67)


def _channel_keys(prefix: str, channel: str) -> dict[str, str]:
    ms = ("energy", "power", "apparent_power", "power_factor", "voltage", "current", "frequency", "energy_returned")
    return {f"{channel}_{m}": f"{prefix}_{m}" for m in ms}


def _entry_data(**over) -> dict:
    """A 1.0.1 config entry exactly as its setup flow wrote it (defaults kept)."""
    data = {
        **_channel_keys(HOUSE, "house"),
        **_channel_keys(HALL, "hall"),
        "supplier": "SSE Airtricity", "plan": "30% Day Night (Nightsaver)", "day_rate": 32.38, "night_rate": 19.6,
        "standing_charge": 359.71, "pso_levy": 6.67, "night_start": 23, "night_end": 8, "night_clock": "gmt",
        "valid_from": "2026-10-05", "bill_anchor": None, "bill_months": 2,
        "sse_mode": "dry_run", "sse_username": "x", "sse_password": "z", "sse_sync": True,
        "esb_mode": "off", "esb_fetches_per_day": 1,
        "reading_day": 13876.0, "reading_night": 5660.0, "reading_at": "2026-10-07T18:50:34+01:00",
        "latitude": 53.3498, "longitude": -6.2603, "weather_entity": None,
        "dashboard_title": "Electricity", "dashboard_path": "electricity", "configure_energy_dashboard": False,
    }
    data.update(over)
    return data


async def _base(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    await async_setup_component(hass, "lovelace", {})
    await async_setup_component(hass, "frontend", {})
    _shelly(hass)


async def _start(hass: HomeAssistant, data: dict, *, minor: int = 1) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Electricity Analyzer", data=data, version=1, minor_version=minor, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def _stop(hass: HomeAssistant, entry) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _seed_1_0_1_database(path: Path) -> dict[str, int]:
    """What 1.0.1 left behind: hours since 27 September priced at the 5 October default,
    learned weights, the forecast log, the reading anchor and a saved ESB session."""
    store = Store(path)
    sched = TariffSchedule([LEGACY])
    start = datetime(2026, 9, 27, 14, tzinfo=IRELAND).astimezone(UTC)
    rows = []
    for i in range(24 * 10):
        h = start + timedelta(hours=i)
        stats = {"energy": {"change": 0.6}, "power": {"mean": 600}}
        rows.append(build_hour(h, stats, {"energy": {"change": 0.2}, "power": {"mean": 200}}, sched))
    store.upsert_hours(rows)
    store.save_tariff(LEGACY)
    store.save_weights(date(2026, 10, 7), "drivers", {"base": 11.5, "cold": 1.4})
    store.log_forecasts([{"made_on": date(2026, 10, 7), "target": date(2026, 10, 7), "model": "seasonal", "kwh": 14.0}])
    store.add_anchor(datetime(2026, 10, 7, 17, 50, 34, tzinfo=UTC), 13876, 5660, "setup")
    store.save_session("esb", {".AspNetCore.Cookies": "kept"})
    counts = {"hours": store.hour_count(), "anchors": len(store.anchors())}
    store.close()
    return counts


# ------------------------------------------------------- upgrading a 1.0.1 install


async def test_upgrade_keeps_every_row_and_moves_the_tariff_to_6_october(hass: HomeAssistant, tmp_path):
    await _base(hass, tmp_path)
    db = tmp_path / DOMAIN / "electricity.db"
    seeded = _seed_1_0_1_database(db)

    entry = await _start(hass, _entry_data())
    # The settings: only the two shipped defaults moved; nothing to set up again.
    assert entry.minor_version == 2
    assert entry.data["valid_from"] == "2026-10-06" and entry.data["pso_levy"] == pytest.approx(19.10)
    assert entry.data["day_rate"] == 32.38 and entry.data["sse_password"] == "z"
    analyzer = entry.runtime_data
    store = analyzer.store

    # One tariff period, from 6 October, PSO €19.10: the 5 October copy was moved, not duplicated.
    assert [(t.valid_from, t.pso_per_year) for t in store.tariffs()] == [(date(2026, 10, 6), 19.10)]
    # Nothing lost.
    assert store.hour_count() == seeded["hours"]
    assert len(store.anchors()) == seeded["anchors"]
    assert store.weight_history("drivers", date(2026, 1, 1))["cold"] == [("2026-10-07", 1.4)]
    kept = [r for r in store.forecast_log(date(2026, 1, 1)) if r["made_on"] == date(2026, 10, 7)]
    assert [(r["model"], r["kwh"]) for r in kept] == [("seasonal", 14.0)]
    assert store.load_session("esb") == {".AspNetCore.Cookies": "kept"}
    # Hours before 6 October keep their kWh and lose the retroactive price; later ones are priced.
    hours = store.hours()
    before = [h for h in hours if h["local_date"] < "2026-10-06"]
    after = [h for h in hours if h["local_date"] >= "2026-10-06"]
    assert before and after
    assert all(h["house_cost_eur"] is None and h["house_kwh"] == pytest.approx(0.6) for h in before)
    assert all(h["house_cost_eur"] is not None and h["tariff_from"] == "2026-10-06" for h in after)
    assert hass.states.get("sensor.electricity_analyzer_stored_hours").attributes["unpriced_hours"] == len(before)

    # A reload (or a restart) changes nothing more.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    store = entry.runtime_data.store
    assert store.hour_count() == seeded["hours"] and len(store.tariffs()) == 1
    await _stop(hass, entry)


async def test_migration_keeps_values_typed_in_by_hand(hass: HomeAssistant, tmp_path):
    await _base(hass, tmp_path)
    entry = await _start(hass, _entry_data(valid_from="2026-09-01", pso_levy=21.5, configure_energy_dashboard=False))
    assert entry.data["valid_from"] == "2026-09-01" and entry.data["pso_levy"] == 21.5
    assert entry.data["energy_dashboard_mode"] == "off"
    await _stop(hass, entry)


# ------------------------------------------------------------- Energy dashboard


async def _energy_prefs(hass: HomeAssistant, sources: list[dict]) -> object:
    from homeassistant.components.energy.data import async_get_manager

    manager = await async_get_manager(hass)
    await manager.async_update({"energy_sources": sources})
    return manager


OLD_GRID = {
    "type": "grid", "stat_energy_from": "electric_ireland:consumption", "stat_energy_to": None, "stat_cost": None,
    "entity_energy_price": None, "number_energy_price": None, "stat_compensation": None,
    "entity_energy_price_export": None, "number_energy_price_export": None, "cost_adjustment_day": 0.0,
}
SOLAR = {"type": "solar", "stat_energy_from": "sensor.solar_energy", "config_entry_solar_forecast": None}


async def test_replace_mode_makes_the_meter_statistics_the_only_grid_and_keeps_a_copy(hass: HomeAssistant, tmp_path):
    from custom_components.electricity_analyzer.energy import COST_STAT, ENERGY_STAT

    await _base(hass, tmp_path)
    manager = await _energy_prefs(hass, [OLD_GRID, SOLAR])
    entry = await _start(hass, _entry_data(configure_energy_dashboard=True))
    assert entry.data["energy_dashboard_mode"] == "replace"
    sources = manager.data["energy_sources"]
    grids = [s for s in sources if s["type"] == "grid"]
    assert len(grids) == 1
    assert grids[0]["stat_energy_from"] == ENERGY_STAT and grids[0]["stat_cost"] == COST_STAT
    assert grids[0]["entity_energy_price"] is None and grids[0]["number_energy_price"] is None
    assert SOLAR in sources  # never touched
    kept = entry.runtime_data.store.get_json("energy_grid_replaced")
    assert kept[0]["source"] == OLD_GRID
    notes = persistent_notification._async_get_or_create_notifications(hass)
    assert f"{DOMAIN}_energy_replaced" in notes
    # The unit-rate entity stays a pure energy price for anything that uses it.
    assert float(hass.states.get("sensor.electricity_analyzer_unit_rate").state) in (0.3238, 0.196)

    # Idempotent across a reload.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.energy_status == "already"
    assert len(entry.runtime_data.store.get_json("energy_grid_replaced")) == 1
    await _stop(hass, entry)


async def test_the_1_0_x_grid_connection_is_upgraded_and_the_energy_dashboard_follows_revisions(
    hass: HomeAssistant, tmp_path
):
    """The Energy dashboard's cost is our statistic: a tariff period set afterwards, or ESB's
    figure for an hour, rewrites it (the history included)."""
    from custom_components.electricity_analyzer.energy import COST_STAT, ENERGY_STAT

    await _base(hass, tmp_path)
    old_ours = {**OLD_GRID, "stat_energy_from": f"{HOUSE}_energy", "entity_energy_price": "sensor.electricity_analyzer_unit_rate"}
    manager = await _energy_prefs(hass, [old_ours])
    store = Store(tmp_path / DOMAIN / "electricity.db")
    sched = TariffSchedule([DEFAULT_TARIFF])
    start = datetime(2026, 10, 7, 10, tzinfo=IRELAND).astimezone(UTC)
    store.upsert_hours([build_hour(start + timedelta(hours=i), {"energy": {"change": 1.0}}, None, sched) for i in range(4)])
    store.close()
    entry = await _start(hass, _entry_data(valid_from="2026-10-06", pso_levy=19.10, energy_dashboard_mode="add",
                                           configure_energy_dashboard=True), minor=2)
    grids = [s for s in manager.data["energy_sources"] if s["type"] == "grid"]
    assert len(grids) == 1 and grids[0]["stat_energy_from"] == ENERGY_STAT and grids[0]["stat_cost"] == COST_STAT
    assert entry.runtime_data.energy_status == "upgraded"
    analyzer = entry.runtime_data
    await analyzer.async_publish(full=True)

    async def energy_cost() -> float:
        return sum(r["change"] or 0 for r in await _stats(hass, "cost_energy", start, {"change"}))

    assert await energy_cost() == pytest.approx(4 * 0.3238, abs=1e-4)
    # New prices for those days: the Energy dashboard's cost is rewritten.
    await analyzer.async_set_tariff_period(Tariff(date(2026, 10, 6), "SSE", "x", 0.50, 0.25, 359.71, 19.10))
    assert await energy_cost() == pytest.approx(4 * 0.50, abs=1e-4)
    # ESB's figure for the first two hours (the meter counted 1.1 kWh each): revised again.
    analyzer.store.upsert_esb_intervals([(start + timedelta(minutes=30 * i), 0.55, "day") for i in range(4)], "esb")
    await analyzer.async_apply_esb()
    assert await energy_cost() == pytest.approx((1.1 * 2 + 1.0 * 2) * 0.50, abs=1e-4)
    meter = await _stats(hass, "meter_energy", start, {"change"})
    assert [round(r["change"], 3) for r in meter] == [1.1, 1.1, 1.0, 1.0]
    await _stop(hass, entry)


async def test_add_mode_leaves_an_existing_grid_alone(hass: HomeAssistant, tmp_path):
    await _base(hass, tmp_path)
    manager = await _energy_prefs(hass, [OLD_GRID])
    entry = await _start(hass, _entry_data(energy_dashboard_mode="add", configure_energy_dashboard=True), minor=2)
    assert manager.data["energy_sources"] == [OLD_GRID]
    assert entry.runtime_data.energy_status == "other"
    await _stop(hass, entry)


async def test_energy_configure_states():
    """configured / already / other / off, touching nothing else."""
    from custom_components.electricity_analyzer.energy import async_configure, grid_source

    class _Manager:
        def __init__(self, sources):
            self.data = {"energy_sources": sources}
            self.writes = 0

        async def async_update(self, update):
            self.writes += 1
            self.data = {**self.data, **update}

    async def run(manager, mode="replace"):
        with patch("homeassistant.components.energy.data.async_get_manager", return_value=manager):
            return await async_configure(None, "sensor.house", mode)

    m = _Manager([SOLAR])
    assert (await run(m)).outcome == "configured"
    assert m.data["energy_sources"] == [SOLAR, grid_source()]
    assert (await run(m)).outcome == "already" and m.writes == 1
    assert (await run(_Manager([OLD_GRID]), "add")).outcome == "other"
    assert (await run(_Manager([OLD_GRID]), "off")).outcome == "off"


# ------------------------------------------------------------------------- ESB


class _FakeEsb:
    logins = 0
    probe = "dead"           # live | dead | down
    login_error: Exception | None = None

    def __init__(self, http, username, password, mprn, **_kw) -> None:
        self._cookies: dict[str, str] = {}

    def restore_session(self, cookies):
        self._cookies = dict(cookies or {})
        return len(self._cookies)

    def export_session(self):
        return dict(self._cookies)

    async def is_authenticated(self) -> bool:
        if type(self).probe == "down":
            raise PortalError("ESB answered HTTP 503 (myaccount.esbnetworks.ie)")
        return type(self).probe == "live"

    async def login(self) -> None:
        type(self).logins += 1
        if type(self).login_error is not None:
            raise type(self).login_error
        self._cookies = {".AspNetCore.Cookies": "fresh"}

    async def meter_window(self):
        return {}

    async def download_hdf(self):
        rows = "".join(f"10000000001,1,0.4,Active Import Interval (kW),07-10-2026 0{h}:30\n" for h in range(4))
        return ("MPRN,Meter Serial Number,Read Value,Read Type,Read Date and End Time\n" + rows).encode()

    async def billed_reads(self):
        return {}


ESB = {"esb_mode": "account", "esb_username": "x", "esb_password": "z", "mprn": "10000000001"}


@pytest.fixture
def fake_esb():
    _FakeEsb.logins, _FakeEsb.probe, _FakeEsb.login_error = 0, "dead", None
    with patch(f"{RUNTIME}.EsbClient", _FakeEsb):
        yield _FakeEsb


async def test_esb_outage_never_spends_a_sign_in(hass: HomeAssistant, tmp_path, fake_esb):
    await _base(hass, tmp_path)
    fake_esb.probe = "down"
    entry = await _start(hass, _entry_data(**ESB), minor=2)
    state = entry.runtime_data.store.fetch_state("esb")
    assert fake_esb.logins == 0
    assert state.failures == 1 and "503" in state.last_error
    assert await entry.runtime_data._recent_esb_sign_ins() == []
    await _stop(hass, entry)


def _later(store: Store, source: str, hours: float = 12.1) -> None:
    """Move the last failed attempt back in time, as if `hours` had passed."""
    when = (dt_util.utcnow() - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    with store._tx() as c:
        c.execute("UPDATE fetch_state SET last_attempt = ? WHERE source = ?", (when, source))
    if source == "esb":  # the sign-ins made then age too (at most two in any 24 hours)
        stamps = store.get_json("esb_sign_ins") or []
        store.set_json("esb_sign_ins", [
            (datetime.fromisoformat(t) - timedelta(hours=hours)).isoformat() for t in stamps
        ])


async def test_esb_failure_waits_12_hours_then_retries_by_itself(hass: HomeAssistant, tmp_path, fake_esb):
    """A CAPTCHA (or any failure): no retry for 12 hours, not by schedule, button, service
    or restart; then one by itself; no Repairs item, no cookie to paste; session kept."""
    await _base(hass, tmp_path)
    store = Store(tmp_path / DOMAIN / "electricity.db")
    store.save_session("esb", {".AspNetCore.Cookies": "old"})
    store.close()
    fake_esb.login_error = CaptchaEncountered("ESB Networks is showing a CAPTCHA at the credential step")
    entry = await _start(hass, _entry_data(**ESB), minor=2)
    analyzer = entry.runtime_data
    assert fake_esb.logins == 1
    assert analyzer.store.load_session("esb") == {".AspNetCore.Cookies": "old"}  # kept
    assert ir.async_get(hass).async_get_issue(DOMAIN, "esb_captcha") is None  # nothing to hand over
    await analyzer.insights_coordinator.async_refresh()
    await hass.async_block_till_done()
    status = hass.states.get("sensor.electricity_analyzer_esb_status")
    assert status.state == "retrying"
    next_try = dt_util.parse_datetime(status.attributes["next_attempt"])
    assert timedelta(hours=11.9) < next_try - dt_util.utcnow() <= timedelta(hours=12)
    assert hass.states.get("button.electricity_analyzer_refresh_esb_now").state == "unavailable"

    with patch("custom_components.electricity_analyzer.engine.store.FetchState.is_due", return_value=True):
        await analyzer.async_esb_tick()                      # the schedule
        await analyzer.async_esb_tick(force=True)            # anything forcing it
    with pytest.raises(HomeAssistantError, match="tried again by itself"):
        await hass.services.async_call(DOMAIN, "refresh_esb", {}, blocking=True)
    assert await hass.config_entries.async_reload(entry.entry_id)  # a restart
    await hass.async_block_till_done(wait_background_tasks=True)
    analyzer = entry.runtime_data
    assert fake_esb.logins == 1

    _later(analyzer.store, "esb")
    await analyzer.async_esb_tick()  # 12 hours on: tried once, by itself
    assert fake_esb.logins == 2
    assert analyzer.waiting_until("esb") is not None  # failed again: another 12 hours
    _later(analyzer.store, "esb")
    fake_esb.login_error = None
    await analyzer.async_esb_tick()
    assert fake_esb.logins == 3
    assert analyzer.store.fetch_state("esb").failures == 0 and analyzer.waiting_until("esb") is None
    await analyzer.insights_coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.electricity_analyzer_esb_status").state == "ok"
    assert hass.states.get("button.electricity_analyzer_refresh_esb_now").state != "unavailable"
    await _stop(hass, entry)


async def test_esb_new_credentials_do_not_wait_out_the_old_ones(hass: HomeAssistant, tmp_path, fake_esb):
    await _base(hass, tmp_path)
    fake_esb.login_error = AuthFailed("credentials rejected: wrong password")
    entry = await _start(hass, _entry_data(**ESB), minor=2)
    assert fake_esb.logins == 1
    await entry.runtime_data.async_esb_tick(force=True)
    assert fake_esb.logins == 1  # inside the 12 hours
    fake_esb.login_error = None
    hass.config_entries.async_update_entry(entry, data={**entry.data, "esb_password": "corrected"})  # reloads
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_esb.logins == 2
    assert entry.runtime_data.store.fetch_state("esb").failures == 0
    await _stop(hass, entry)


class _FakeSse:
    calls = 0
    fail = True

    def __init__(self, http, username, password, **_kw) -> None:
        pass

    async def meter_info(self):
        from custom_components.electricity_analyzer.engine.sse.client import MeterInfo, Register

        type(self).calls += 1
        if type(self).fail:
            raise PortalError("SSE answered 503")
        return MeterInfo(
            premises_id="1", mprn="10000000001", meter_number="1", model="RM108", service_number="1", dials=6,
            de_date="08/10/2026", meter_type="Day/Night", submitted_last_24h=False,
            registers=[Register(1, "day", 13876.0, "07/10/2026", "Accepted", "07/10/2026 18:50:34", "C"),
                       Register(2, "night", 5660.0, "07/10/2026", "Accepted", "07/10/2026 18:50:34", "C")],
        )

    async def billing_info(self):
        return {"next_bill_date": "2026-11-20"}

    async def reading_history(self, premises_id):
        return []

    async def submit_reading(self, day, night):
        type(self).calls += 1
        raise PortalError("SSE answered 503")


async def test_sse_failure_waits_12_hours_and_blocks_buttons_and_sends(hass: HomeAssistant, tmp_path):
    await _base(hass, tmp_path)
    _FakeSse.calls, _FakeSse.fail = 0, True
    with patch(f"{RUNTIME}.SseClient", _FakeSse):
        entry = await _start(hass, _entry_data(sse_mode="live"), minor=2)
        analyzer = entry.runtime_data
        assert _FakeSse.calls == 1  # the start-up check, which failed
        await analyzer.insights_coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get("sensor.electricity_analyzer_sse_status").state == "retrying"
        assert hass.states.get("button.electricity_analyzer_check_sse_account").state == "unavailable"
        assert hass.states.get("button.electricity_analyzer_submit_meter_reading_to_sse").state == "unavailable"
        with pytest.raises(HomeAssistantError, match="not sent"):
            await analyzer.async_submit_reading(force=True)
        await analyzer._sse_sync()
        assert _FakeSse.calls == 1  # nothing tried inside the 12 hours
        assert analyzer.store.submissions()[0]["status"] == "blocked"

        _later(analyzer.store, "sse")
        await analyzer._load_retry_state()
        _FakeSse.fail = False
        await analyzer._sse_sync()  # 12 hours on: by itself
        assert _FakeSse.calls == 2 and analyzer.store.fetch_state("sse").failures == 0
        await analyzer.insights_coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get("sensor.electricity_analyzer_sse_status").state != "retrying"
        assert hass.states.get("button.electricity_analyzer_check_sse_account").state != "unavailable"
        await _stop(hass, entry)


async def test_esb_options_without_new_credentials_do_not_sign_in(hass: HomeAssistant, tmp_path, fake_esb):
    await _base(hass, tmp_path)
    fake_esb.probe = "live"
    entry = await _start(hass, _entry_data(**ESB), minor=2)
    _CountingEsb.logins = 0
    with patch(f"{FLOW}.EsbClient", _CountingEsb):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "esb"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**ESB, "esb_fetches_per_day": "2"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done(wait_background_tasks=True)
        assert _CountingEsb.logins == 0 and entry.data["esb_fetches_per_day"] == 2
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "esb"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**ESB, "esb_password": "new", "esb_fetches_per_day": "2"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done(wait_background_tasks=True)
        assert _CountingEsb.logins == 1  # new credentials are checked, once
    await _stop(hass, entry)


async def test_esb_probe_tells_an_outage_from_a_dead_session(socket_enabled, unused_tcp_port, monkeypatch):
    from aiohttp import web

    from custom_components.electricity_analyzer.engine.esb import client as esb
    from test_esb import _session

    status = {"code": 503}

    async def page(request):
        if status["code"] == 302:
            raise web.HTTPFound("http://login.esbnetworks.ie:%d/tenant/oauth2/v2.0/authorize" % unused_tcp_port)
        return web.Response(status=status["code"], text="x")

    async def login_page(request):
        return web.Response(text="sign in")

    app = web.Application()
    app.router.add_get("/Api/HistoricConsumption", page)
    app.router.add_get("/tenant/oauth2/v2.0/authorize", login_page)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    monkeypatch.setattr(esb, "ESB_BASE", f"http://{esb.APP_HOST}:{unused_tcp_port}")
    try:
        async with _session() as http:
            client = esb.EsbClient(http, "x", "z", "1")
            with pytest.raises(PortalError, match="503"):
                await client.is_authenticated()
            status["code"] = 429
            with pytest.raises(PortalError, match="slow down"):
                await client.is_authenticated()
            status["code"] = 302
            assert await client.is_authenticated() is False
            status["code"] = 200
            assert await client.is_authenticated() is True
    finally:
        await runner.cleanup()
    monkeypatch.setattr(esb, "ESB_BASE", f"http://{esb.APP_HOST}:1")  # nothing listens there
    async with _session() as http:
        with pytest.raises(PortalError, match="could not be reached"):
            await esb.EsbClient(http, "x", "z", "1").is_authenticated()


# --------------------------------------------------------------------- weather


def _hours(start: datetime, end: datetime, temp: float) -> list[Observation]:
    out, t = [], start
    while t < end:
        out.append(Observation(t, {"temperature": temp, "apparent_temperature": temp - 2, "precipitation": 0.0}))
        t += timedelta(hours=1)
    return out


@pytest.fixture
def open_meteo():
    calls: dict[str, list] = {"archive": [], "forecast": []}
    behaviour = {"forecast_fails": False}

    async def archive(http, lat, lon, start, end):
        calls["archive"].append((start, end))
        last = min(local_midnight(end + timedelta(days=1)), dt_util.utcnow() - timedelta(days=5))
        return _hours(local_midnight(start), last, 9.0), 1

    async def forecast(http, lat, lon):
        calls["forecast"].append(True)
        if behaviour["forecast_fails"]:
            raise wx.WeatherUnavailable("Open-Meteo answered 503: try later")
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        return _hours(now - timedelta(days=2), now + timedelta(days=16), 5.5)

    with patch(f"{RUNTIME}.wx.fetch_archive", archive), patch(f"{RUNTIME}.wx.fetch_forecast", forecast):
        yield calls, behaviour


async def _stats(hass: HomeAssistant, key: str, start: datetime, types: set[str]) -> list[dict]:
    await async_wait_recording_done(hass)
    sid = f"{DOMAIN}:{key}"
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {sid}, "hour", None, types
    )
    return result.get(sid, [])


async def test_weather_works_with_no_electricity_data_at_all(hass: HomeAssistant, tmp_path, open_meteo):
    calls, _ = open_meteo
    await _base(hass, tmp_path)
    entry = await _start(hass, _entry_data(), minor=2)
    analyzer = entry.runtime_data
    assert analyzer.store.hour_count() == 0  # no electricity history
    today = local_date(dt_util.utcnow())
    assert calls["archive"][0][0] == today - timedelta(days=730)  # two years of history regardless
    assert float(hass.states.get("sensor.electricity_analyzer_outdoor_temperature").state) == 5.5
    collection = hass.states.get("sensor.electricity_analyzer_stored_hours").attributes["collection"]
    assert collection["weather_state"] == "ok"
    assert collection["state"] == "no_data" and "first hour is stored" in collection["message"]
    # Temperature and degree days are long-term statistics, including today's completed hours.
    midnight = local_midnight(today)
    temps = await _stats(hass, "outdoor_temperature", midnight - timedelta(days=3), {"mean"})
    hdd = await _stats(hass, "heating_degree_days", midnight, {"change"})
    assert temps and hdd
    completed_today = int((dt_util.utcnow() - midnight).total_seconds() // 3600)
    assert len(hdd) >= completed_today
    assert hdd[0]["change"] == pytest.approx((15.5 - 5.5) / 24)
    await _stop(hass, entry)


async def test_weather_failure_keeps_the_last_data_and_says_so(hass: HomeAssistant, tmp_path, open_meteo):
    _, behaviour = open_meteo
    behaviour["forecast_fails"] = True
    await _base(hass, tmp_path)
    entry = await _start(hass, _entry_data(), minor=2)
    analyzer = entry.runtime_data
    assert "forecast" in analyzer.weather_error and analyzer.store.weather()  # history still stored
    collection = hass.states.get("sensor.electricity_analyzer_stored_hours").attributes["collection"]
    assert collection["weather_state"] == "error" and "Open-Meteo is unavailable" in collection["weather_message"]
    await _stop(hass, entry)


async def test_degree_days_today_count_completed_hours_only(hass: HomeAssistant, tmp_path, open_meteo):
    await _base(hass, tmp_path)
    entry = await _start(hass, _entry_data(), minor=2)
    state = hass.states.get("sensor.electricity_analyzer_heating_degree_days_today")
    now = dt_util.utcnow()
    done = int((now - local_midnight(local_date(now))).total_seconds() // 3600)
    assert float(state.state) == pytest.approx(done * (15.5 - 5.5) / 24, abs=0.01)
    assert state.attributes["hours_counted"] == done
    assert state.attributes["expected_full_day"] >= float(state.state)
    await _stop(hass, entry)


async def test_hourly_rollup_publishes_newly_completed_weather_hours(hass: HomeAssistant, tmp_path, open_meteo):
    await _base(hass, tmp_path)
    entry = await _start(hass, _entry_data(), minor=2)
    analyzer = entry.runtime_data
    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    analyzer.store.upsert_weather([Observation(hour, {"temperature": -1.0})], "archive")
    await analyzer._rollup_and_refresh()
    rows = await _stats(hass, "outdoor_temperature", hour, {"mean"})
    assert rows[0]["mean"] == pytest.approx(-1.0)
    await _stop(hass, entry)


# ------------------------------------------------------- states the dashboard shows


async def test_a_few_hours_of_history_is_collecting_not_empty(hass: HomeAssistant, tmp_path):
    await _base(hass, tmp_path)
    store = Store(tmp_path / DOMAIN / "electricity.db")
    sched = TariffSchedule([DEFAULT_TARIFF])
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    store.upsert_hours([build_hour(now - timedelta(hours=i), {"energy": {"change": 0.5}}, None, sched) for i in (3, 2, 1)])
    store.close()
    entry = await _start(hass, _entry_data(), minor=2)
    collection = hass.states.get("sensor.electricity_analyzer_stored_hours").attributes["collection"]
    assert collection["state"] == "collecting" and collection["hours"] == 3
    assert "Collecting since" in collection["message"]
    assert collection["initial_load"] == "done"
    forecast = hass.states.get("sensor.electricity_analyzer_forecast_today_cost")
    assert forecast.state == "unknown" and "complete days" in forecast.attributes["reason"]
    await _stop(hass, entry)


async def test_websocket_table_states(hass: HomeAssistant, tmp_path, hass_ws_client):
    await _base(hass, tmp_path)
    await async_setup_component(hass, DOMAIN, {})
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/table", "period": "hour"})
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "not_loaded"  # the card says "starting"
    entry = await _start(hass, _entry_data(), minor=2)
    await client.send_json({"id": 2, "type": f"{DOMAIN}/table", "period": "hour"})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["rows"] == [] and msg["result"]["columns"]
    await _stop(hass, entry)


async def test_today_never_includes_yesterdays_cached_hours(hass: HomeAssistant, tmp_path):
    await _base(hass, tmp_path)
    entry = await _start(hass, _entry_data(), minor=2)
    analyzer = entry.runtime_data
    yesterday = local_date(dt_util.utcnow()) - timedelta(days=1)
    stale = build_hour(local_midnight(yesterday) + timedelta(hours=12), {"energy": {"change": 5.0}}, None, analyzer.schedule)
    stale["local_date"] = yesterday.isoformat()
    analyzer._today_rows = [stale]  # as between local midnight and the next cache refresh
    assert (analyzer.compute_live()["today"]["house_kwh"] or 0) < 5.0
    await _stop(hass, entry)


async def test_a_failing_background_job_is_isolated_and_logged(hass: HomeAssistant, tmp_path, caplog):
    await _base(hass, tmp_path)
    entry = await _start(hass, _entry_data(), minor=2)
    ran = []

    async def boom():
        raise ValueError("broken")

    async def fine():
        ran.append(True)

    with caplog.at_level(logging.ERROR):
        entry.runtime_data._spawn(boom, "test job")
        entry.runtime_data._spawn(fine, "other job")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert ran == [True]
    assert "test job failed" in caplog.text
    await _stop(hass, entry)


# ------------------------------------------------- a new install: data starts today


async def _import_today(hass: HomeAssistant, hours: int) -> datetime:
    """The recorder holds the clamps' statistics from local midnight today, nothing older."""
    from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
    from homeassistant.components.recorder.statistics import async_import_statistics

    midnight = local_midnight(local_date(dt_util.utcnow()))
    for prefix, kwh, watts in ((HOUSE, 0.6, 600.0), (HALL, 0.2, 200.0)):
        energy = StatisticMetaData(
            mean_type=StatisticMeanType.NONE, has_sum=True, name=None, source="recorder",
            statistic_id=f"{prefix}_energy", unit_class="energy", unit_of_measurement="kWh",
        )
        async_import_statistics(hass, energy, [
            StatisticData(start=midnight + timedelta(hours=i), state=100 + kwh * (i + 1), sum=kwh * (i + 1))
            for i in range(hours)
        ])
        power = StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC, has_sum=False, name=None, source="recorder",
            statistic_id=f"{prefix}_power", unit_class="power", unit_of_measurement="W",
        )
        async_import_statistics(hass, power, [
            StatisticData(start=midnight + timedelta(hours=i), mean=watts, min=watts / 2, max=watts * 2)
            for i in range(hours)
        ])
    await async_wait_recording_done(hass)
    return midnight


async def test_a_new_install_whose_data_starts_today_works_without_warnings(
    hass: HomeAssistant, tmp_path, open_meteo, caplog, hass_ws_client
):
    now = dt_util.utcnow()
    midnight = local_midnight(local_date(now))
    hours = int((now.replace(minute=0, second=0, microsecond=0) - midnight) / timedelta(hours=1))
    if hours < 2:
        pytest.skip("too close to midnight for a day's worth of completed hours")
    await _base(hass, tmp_path)
    await _import_today(hass, hours)
    with caplog.at_level(logging.WARNING, logger="custom_components.electricity_analyzer"):
        entry = await _start(hass, _entry_data(), minor=2)
        analyzer = entry.runtime_data

        # Stored: today's completed hours, nothing invented before them.
        stored = analyzer.store.hours()
        assert len(stored) == hours and {r["local_date"] for r in stored} == {local_date(now).isoformat()}
        assert all(r["house_cost_eur"] is not None for r in stored)  # 6 October tariff in force
        bands = [(r["hour_utc"].astimezone(IRELAND).hour, r["band"]) for r in stored]
        night_end = 9 if datetime.now(IRELAND).utcoffset() == timedelta(hours=1) else 8
        assert all(band == ("night" if h < night_end else "day") for h, band in bands)

        # What the dashboard shows.
        coll = hass.states.get("sensor.electricity_analyzer_stored_hours").attributes["collection"]
        assert coll["state"] == "collecting" and coll["today_hours"] == hours and coll["complete_days"] == 0
        assert "Collecting since" in coll["message"]
        assert coll["weather_state"] == "ok"
        assert float(hass.states.get("sensor.electricity_analyzer_today_energy").state) >= 0.6 * hours - 0.01
        cost = hass.states.get("sensor.electricity_analyzer_today_cost")
        assert float(cost.state) > 0 and cost.attributes["pso_levy_so_far"] > 0
        assert hass.states.get("sensor.electricity_analyzer_yesterday_energy").state == "unknown"  # no data, not 0
        assert float(hass.states.get("sensor.electricity_analyzer_this_month_energy").state) > 0
        forecast = hass.states.get("sensor.electricity_analyzer_forecast_today_cost")
        assert forecast.state == "unknown" and forecast.attributes["reason"]
        assert float(hass.states.get("sensor.electricity_analyzer_outdoor_temperature").state) == 5.5
        ours = [s for s in hass.states.async_all() if "electricity_analyzer" in s.entity_id]
        assert ours and not [s.entity_id for s in ours if s.state == "unavailable"]

        # The charts' statistics: today's hours, night in the night buckets.
        night = await _stats(hass, "house_energy_night", midnight, {"change"})
        day = await _stats(hass, "house_energy_day", midnight, {"change"})
        assert len(night) == len(day) == hours
        assert night[0]["change"] == pytest.approx(0.6) and day[0]["change"] == pytest.approx(0.0)

        client = await hass_ws_client(hass)
        await client.send_json({"id": 1, "type": f"{DOMAIN}/table", "period": "hour"})
        msg = await client.receive_json()
        assert msg["success"] and len(msg["result"]["rows"]) >= hours
        await client.send_json({"id": 2, "type": f"{DOMAIN}/table", "period": "day"})
        msg = await client.receive_json()
        assert msg["success"] and msg["result"]["rows"][0]["partial"] is True  # today is in progress
        await _stop(hass, entry)
    ours_logged = [r for r in caplog.records if r.name.startswith("custom_components.electricity_analyzer")]
    assert not ours_logged, [f"{r.levelname} {r.getMessage()[:160]}" for r in ours_logged]


async def test_downtime_is_rebuilt_from_esb_inside_home_assistant(hass: HomeAssistant, tmp_path):
    """HA down overnight: the hole and the restart lump are rebuilt from ESB's data, the night
    hours land back in the night band, the clamp-vs-meter learning ignores them, and it is
    idempotent."""
    await _base(hass, tmp_path)
    store = Store(tmp_path / DOMAIN / "electricity.db")
    sched = TariffSchedule([DEFAULT_TARIFF])
    start = datetime(2026, 10, 7, 20, tzinfo=IRELAND).astimezone(UTC)
    restart = start + timedelta(hours=13)  # 09:00 on the 8th
    clamp = {start: 0.5, start + timedelta(hours=1): 0.5, restart: 6.0, restart + timedelta(hours=1): 0.5}
    store.upsert_hours([build_hour(h, {"energy": {"change": k}}, None, sched) for h, k in clamp.items()])
    store.upsert_esb_intervals(
        [(start + timedelta(minutes=30 * i), 0.25, DEFAULT_TARIFF.window.band_at(start + timedelta(minutes=30 * i)))
         for i in range(2 * 15)], "esb",
    )
    store.close()
    entry = await _start(hass, _entry_data(), minor=2)
    analyzer = entry.runtime_data
    # The 11-hour hole and the restart lump filled, and the 3 clamp hours ESB covers revised.
    assert await analyzer.async_repair_downtime() == 15
    rows = {r["hour_utc"]: r for r in analyzer.store.hours()}
    assert len(rows) == 15
    assert rows[restart]["house_kwh"] == pytest.approx(0.5) and rows[restart]["source"] == "esb"
    night = [r for r in rows.values() if r["band"] == "night"]
    assert len(night) == 9 and all(r["source"] == "esb" for r in night)  # 00:00-09:00 IST
    assert sum(r["house_kwh"] for r in rows.values()) == pytest.approx(0.5 * 15)
    await analyzer.insights_coordinator.async_refresh()
    assert analyzer.insights_coordinator.data["reconcile"]["days_compared"] == 0  # ESB-filled days are not "clamp"
    assert await analyzer.async_repair_downtime() == 0  # idempotent
    await _stop(hass, entry)


async def test_today_starts_at_the_daily_minimum_and_periods_move_with_the_clamps(hass: HomeAssistant, tmp_path):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    await _base(hass, tmp_path)
    entry = await _start(hass, _entry_data(), minor=2)
    today = local_date(dt_util.utcnow())
    tariff = entry.runtime_data.schedule.in_force(today)
    cost = hass.states.get("sensor.electricity_analyzer_today_cost")
    # Nothing used yet, and today already costs its standing charge and PSO, in full.
    assert float(cost.state) == pytest.approx(tariff.fixed_for_day(today), abs=0.005)  # to the cent
    assert cost.attributes["standing_charge_so_far"] == pytest.approx(tariff.standing_for_day(today), abs=0.005)
    assert cost.attributes["pso_levy_so_far"] == pytest.approx(tariff.pso_for_day(today), abs=0.005)
    week_before = float(hass.states.get("sensor.electricity_analyzer_this_week_energy").state)
    month_cost_before = float(hass.states.get("sensor.electricity_analyzer_this_month_cost").state)

    _shelly(hass, house_kwh=100.02, hall_kwh=40.005)  # the clamps count up
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.electricity_analyzer_this_week_energy").state) == pytest.approx(week_before + 0.02, abs=0.002)
    assert float(hass.states.get("sensor.electricity_analyzer_this_month_cost").state) > month_cost_before
    hourly = hass.states.get("sensor.electricity_analyzer_today_energy").attributes["hourly"]
    assert hourly and hourly[-1]["live"] is True and hourly[-1]["kwh"] == pytest.approx(0.02, abs=0.002)
    await _stop(hass, entry)


async def _rates(hass: HomeAssistant, entry, data: dict, kind: str | None = None) -> dict:
    """Configure -> Prices -> one of: a period / from today / from a future date."""
    kind = kind or ("rates_period" if data.get("valid_to") else "rates_future")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "rates"})
    assert result["type"] is FlowResultType.MENU and set(result["menu_options"]) == {
        "rates_period", "rates_now", "rates_future", "rates_remove"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": kind})
    assert result["step_id"] == kind and "Before" in result["description_placeholders"]["periods"]
    return await hass.config_entries.options.async_configure(result["flow_id"], data)


async def test_tariff_periods_for_past_and_future_are_set_in_the_ui_and_priced_at_once(hass: HomeAssistant, tmp_path):
    await _base(hass, tmp_path)
    _seed_1_0_1_database(tmp_path / DOMAIN / "electricity.db")  # hours from 27 September
    entry = await _start(hass, _entry_data())
    analyzer = entry.runtime_data
    before = [h for h in analyzer.store.hours() if h["local_date"] < "2026-10-06"]
    assert before and all(h["house_cost_eur"] is None for h in before)

    # The previous plan, for the days that had no price.
    previous = {"valid_from": "2026-09-27", "valid_to": "2026-10-05", "day_rate": 29.17, "night_rate": 18.66,
                "standing_charge": 338.98, "pso_levy": 19.10}
    result = await _rates(hass, entry, previous)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.runtime_data is analyzer  # stored and re-priced without a reload
    assert [(t.valid_from, t.day_rate) for t in analyzer.store.tariffs()] == [
        (date(2026, 9, 27), 0.2917), (date(2026, 10, 6), 0.3238)]
    hours = {h["hour_utc"]: h for h in analyzer.store.hours()}
    sep = [h for h in hours.values() if h["local_date"] < "2026-10-06"]
    assert all(h["house_cost_eur"] is not None and h["tariff_from"] == "2026-09-27" for h in sep)
    day_hour = next(h for h in sep if h["band"] == "day")
    assert day_hour["unit_rate_eur"] == pytest.approx(0.2917)
    first_day = [h for h in sep if h["local_date"] == "2026-09-27"]
    assert sum(h["fixed_cost_eur"] for h in first_day) == pytest.approx((338.98 + 19.10) / 365, abs=1e-5)
    stats = await _stats(hass, "cost_day", datetime(2026, 9, 27, tzinfo=UTC), {"change"})
    assert sum(r["change"] or 0 for r in stats) > 0  # the charts follow straight away

    # A price increase from 1 December.
    result = await _rates(hass, entry, {"valid_from": "2026-12-01", "day_rate": 35.0, "night_rate": 21.0,
                                        "standing_charge": 370.0, "pso_levy": 19.10})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert analyzer.schedule.for_date(date(2026, 12, 5)).day_rate == pytest.approx(0.35)
    assert analyzer.schedule.current.valid_from == date(2026, 10, 6)  # still in force today

    # New prices for 6-31 October only: the 6 October prices carry on from 1 November.
    result = await _rates(hass, entry, {"valid_from": "2026-10-06", "valid_to": "2026-10-31", "day_rate": 30.0,
                                        "night_rate": 18.0, "standing_charge": 359.71, "pso_levy": 19.10})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    nov = analyzer.schedule.for_date(date(2026, 11, 1))
    assert nov.valid_from == date(2026, 11, 1) and nov.day_rate == pytest.approx(0.3238)

    # Errors are said in the form; nothing changes.
    result = await _rates(hass, entry, {"valid_from": "2026-01-01", "valid_to": "2026-01-31", "day_rate": 30.0,
                                        "night_rate": 18.0, "standing_charge": 359.71, "pso_levy": 19.10})
    assert result["errors"] == {"base": "end_gap"}
    result = await _rates(hass, entry, {"valid_from": "2026-11-10", "valid_to": "2026-11-01", "day_rate": 30.0,
                                        "night_rate": 18.0, "standing_charge": 359.71, "pso_levy": 19.10})
    assert result["errors"] == {"base": "end_before_start"}

    # A restart keeps every period (the unchanged plan from setup does not overwrite them).
    periods = [(t.valid_from, t.day_rate) for t in analyzer.store.tariffs()]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    analyzer = entry.runtime_data
    assert [(t.valid_from, t.day_rate) for t in analyzer.store.tariffs()] == periods

    # Removing a period.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "rates"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "rates_remove"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"valid_from": "2026-12-01"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert date(2026, 12, 1) not in {t.valid_from for t in analyzer.store.tariffs()}

    # The same through the service, with an end date.
    await hass.services.async_call(
        DOMAIN, "add_tariff",
        {"valid_from": "2026-12-24", "valid_to": "2026-12-26", "day_rate": 10, "night_rate": 5},
        blocking=True, return_response=True,
    )
    assert analyzer.schedule.for_date(date(2026, 12, 25)).day_rate == pytest.approx(0.10)
    assert analyzer.schedule.for_date(date(2026, 12, 27)).day_rate == pytest.approx(0.3238)
    await _stop(hass, entry)



async def test_price_change_three_ways(hass: HomeAssistant, tmp_path):
    """1. a period X to Y (pre-filled with the days that have no price); 2. from today onwards;
    3. from a future date, with today's prices carrying on until then."""
    await _base(hass, tmp_path)
    _seed_1_0_1_database(tmp_path / DOMAIN / "electricity.db")
    entry = await _start(hass, _entry_data())
    analyzer = entry.runtime_data
    today = local_date(dt_util.utcnow())
    prices = {"day_rate": 33.0, "night_rate": 20.0, "standing_charge": 360.0, "pso_levy": 19.10}

    # 1. The period form offers the unpriced days.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "rates"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "rates_period"})
    suggested = {str(k): k.description["suggested_value"] for k in result["data_schema"].schema if k.description}
    assert (suggested["valid_from"], suggested["valid_to"]) == ("2026-09-27", "2026-10-05")
    assert "27 Sep 2026 – 5 Oct 2026" in result["description_placeholders"]["gap"]

    # 2. From today onwards: no dates asked; earlier days keep their prices.
    result = await _rates(hass, entry, prices, "rates_now")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert analyzer.schedule.current.valid_from == today and analyzer.schedule.current.day_rate == pytest.approx(0.33)
    assert analyzer.schedule.for_date(today - timedelta(days=1)).day_rate == pytest.approx(0.3238)
    assert analyzer.compute_live()["tariff"]["day_rate"] == pytest.approx(0.33)  # live at once

    # 3. From a future date: today's prices until then.
    later = today + timedelta(days=20)
    result = await _rates(hass, entry, {"valid_from": later.isoformat(), **prices, "day_rate": 40.0}, "rates_future")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert analyzer.schedule.current.day_rate == pytest.approx(0.33)
    assert analyzer.schedule.for_date(later).day_rate == pytest.approx(0.40)
    result = await _rates(hass, entry, {"valid_from": today.isoformat(), **prices}, "rates_future")
    assert result["errors"] == {"base": "not_future"}
    await _stop(hass, entry)


async def test_esb_history_before_the_clamp_becomes_priced_meter_hours(hass: HomeAssistant, tmp_path):
    """ESB's meter data from before the clamp is stored as ESB hours (no hall split), priced by
    whatever tariff periods cover those dates, and never used to learn the clamp ratio."""
    await _base(hass, tmp_path)
    store = Store(tmp_path / DOMAIN / "electricity.db")
    sched = TariffSchedule([DEFAULT_TARIFF])
    clamp_start = datetime(2026, 10, 7, 0, tzinfo=IRELAND).astimezone(UTC)
    store.upsert_hours([
        build_hour(clamp_start + timedelta(hours=i), {"energy": {"change": 0.5}}, {"energy": {"change": 0.2}}, sched)
        for i in range(24)
    ])
    esb_start = clamp_start - timedelta(days=12)  # 25 September
    store.upsert_esb_intervals(
        [(esb_start + timedelta(minutes=30 * i), 0.3, DEFAULT_TARIFF.window.band_at(esb_start + timedelta(minutes=30 * i)))
         for i in range(2 * 24 * 12 + 1)],  # 12 days and one stray half-hour
        "esb",
    )
    store.close()
    entry = await _start(hass, _entry_data(valid_from="2026-10-06", pso_levy=19.10), minor=2)
    analyzer = entry.runtime_data
    assert await analyzer.async_repair_downtime() == 12 * 24
    rows = analyzer.store.hours()
    esb_rows = [r for r in rows if r["source"] == "esb"]
    assert len(esb_rows) == 12 * 24 and all(r["hall_kwh"] is None and r["house_kwh"] == pytest.approx(0.6) for r in esb_rows)
    assert all(r["house_cost_eur"] is None for r in esb_rows if r["local_date"] < "2026-10-06")  # no price yet
    assert all(r["house_cost_eur"] is not None for r in esb_rows if r["local_date"] >= "2026-10-06")
    # The previous plan for those days prices them at once.
    await analyzer.async_set_tariff_period(
        Tariff(date(2026, 9, 25), "Old", "old", 0.30, 0.18, 340.0, 19.10), date(2026, 10, 5)
    )
    priced = [r for r in analyzer.store.hours() if r["local_date"] < "2026-10-06"]
    assert all(r["house_cost_eur"] is not None and r["tariff_from"] == "2026-09-25" for r in priced)
    await analyzer.insights_coordinator.async_refresh()
    data = analyzer.insights_coordinator.data
    assert data["collection"]["esb_history_from"].startswith("2026-09-25")
    assert data["collection"]["first_hour"].startswith("2026-10-07")
    assert data["reconcile"]["days_compared"] == 0  # ESB hours are not the clamp
    assert await analyzer.async_repair_downtime() == 0
    await _stop(hass, entry)



async def test_clamp_prices_are_provisional_and_esb_revises_them(hass: HomeAssistant, tmp_path):
    """The clamp prices an hour at once (provisional); when ESB's figure for it arrives the hour
    is re-priced on ESB's kWh (final). Costs, totals and statistics follow; later tariff or
    factor changes keep ESB's figure; the live register estimate uses it too."""
    await _base(hass, tmp_path)
    store = Store(tmp_path / DOMAIN / "electricity.db")
    sched = TariffSchedule([DEFAULT_TARIFF])
    start = datetime(2026, 10, 7, 10, tzinfo=IRELAND).astimezone(UTC)
    store.upsert_hours([build_hour(start + timedelta(hours=i), {"energy": {"change": 1.0}}, {"energy": {"change": 0.4}}, sched)
                        for i in range(6)])
    # ESB has published the first four hours: the meter counted 3% more than the clamp.
    store.upsert_esb_intervals([(start + timedelta(minutes=30 * i), 0.515, "day") for i in range(8)], "esb")
    store.close()
    entry = await _start(hass, _entry_data(valid_from="2026-10-06", pso_levy=19.10), minor=2)
    analyzer = entry.runtime_data
    before = {r["hour_utc"]: r for r in analyzer.store.hours()}
    assert all(r["meter_source"] in (None, "clamp") for r in before.values())  # provisional

    assert await analyzer.async_apply_esb() == 4
    rows = {r["hour_utc"]: r for r in analyzer.store.hours()}
    for i in range(6):
        r = rows[start + timedelta(hours=i)]
        assert r["house_kwh"] == pytest.approx(1.0)  # the clamp's own reading is kept
        if i < 4:
            assert r["meter_source"] == "esb" and r["meter_kwh"] == pytest.approx(1.03)
            assert r["house_cost_eur"] == pytest.approx(1.03 * 0.3238, abs=1e-6)
            assert r["hall_cost_eur"] + r["rest_cost_eur"] == pytest.approx(r["house_cost_eur"], abs=1e-6)
        else:
            assert r["meter_source"] == "clamp"  # ESB has not got these yet
    stats = await _stats(hass, "cost_day", start, {"change"})
    assert sum(x["change"] for x in stats) == pytest.approx(4 * 1.03 * 0.3238 + 2 * rows[start + timedelta(hours=5)]["house_cost_eur"], abs=1e-4)

    # A tariff change re-prices everything, and final hours stay on ESB's figure.
    await analyzer.async_set_tariff_period(Tariff(date(2026, 10, 6), "SSE", "x", 0.40, 0.20, 359.71, 19.10))
    r = {x["hour_utc"]: x for x in analyzer.store.hours()}[start]
    assert r["meter_source"] == "esb" and r["house_cost_eur"] == pytest.approx(1.03 * 0.40, abs=1e-6)
    assert await analyzer.async_apply_esb() == 0  # nothing left to revise

    # The table says which is which.
    table = await analyzer.async_table("hour", date(2026, 10, 7), date(2026, 10, 7))
    priced_on = {row["key"]: row["priced_on"] for row in table["rows"]}
    assert priced_on[start.isoformat()] == "ESB meter (final)"
    assert priced_on[(start + timedelta(hours=5)).isoformat()] == "Clamp (until ESB has it)"
    await _stop(hass, entry)
