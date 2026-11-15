"""The Home Assistant side: setup flow, entities, live accounting, readings, safety.

Shelly entities are stood in for by plain states with the real Shelly entity ids, so
auto-detection, live accounting and the meter-reading path are all exercised exactly
as on the real device. SSE runs in dry-run mode here: nothing leaves the test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.location import LocationInfo
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.electricity_analyzer.const import DOMAIN

FLOW = "custom_components.electricity_analyzer.config_flow"

# The recorder must exist before `hass` does, so it is requested ahead of every test.
pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

HOUSE = "sensor.full_house_energy_monitor"
HALL = "sensor.hall_energy_monitor"


def _shelly(hass: HomeAssistant, house_kwh: float = 100.0, hall_kwh: float = 40.0, house_w: float = 4766, hall_w: float = 536) -> None:
    for prefix, kwh, watts, pf in ((HOUSE, house_kwh, house_w, 0.96), (HALL, hall_kwh, hall_w, 0.79)):
        hass.states.async_set(f"{prefix}_energy", str(kwh), {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total_increasing", "friendly_name": prefix.split(".")[1].replace("_", " ").title() + " Energy"})
        hass.states.async_set(f"{prefix}_power", str(watts), {"device_class": "power", "unit_of_measurement": "W", "state_class": "measurement"})
        hass.states.async_set(f"{prefix}_apparent_power", str(round(watts / pf)), {"device_class": "apparent_power", "unit_of_measurement": "VA"})
        hass.states.async_set(f"{prefix}_power_factor", str(pf), {"device_class": "power_factor"})
        hass.states.async_set(f"{prefix}_voltage", "229.6", {"device_class": "voltage", "unit_of_measurement": "V"})
        hass.states.async_set(f"{prefix}_current", str(round(watts / pf / 229.6, 2)), {"device_class": "current", "unit_of_measurement": "A"})
        hass.states.async_set(f"{prefix}_frequency", "50", {"device_class": "frequency", "unit_of_measurement": "Hz"})
        hass.states.async_set(f"{prefix}_energy_returned", "0", {"device_class": "energy", "unit_of_measurement": "kWh"})


def _channel(prefix: str) -> dict[str, str]:
    return {m: f"{prefix}_{m}" for m in ("power", "apparent_power", "power_factor", "voltage", "current", "frequency", "energy_returned")}


# What Home Assistant's own IP look-up would answer; no network in tests.
PIN = LocationInfo(
    ip="192.0.2.1", country_code="IE", currency="EUR", region_code="L", region_name="Leinster",
    city="Dublin", zip_code="", time_zone="Europe/Dublin", latitude=53.35, longitude=-6.26, use_metric=True,
)


@pytest.fixture(autouse=True)
def _no_ip_lookup():
    with patch(f"{FLOW}.async_detect_location_info", return_value=PIN):
        yield


async def _to_reading_step(hass: HomeAssistant, esb: dict | None = None) -> dict:
    _shelly(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"house_energy": f"{HOUSE}_energy", "hall_energy": f"{HALL}_energy"}
    )
    assert result["step_id"] == "channels"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"house": _channel(HOUSE), "hall": _channel(HALL)})
    assert result["step_id"] == "tariff", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "sse", result
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sse_mode": "dry_run", "sse_username": "x", "sse_password": "z"}
    )
    assert result["step_id"] == "esb", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], esb or {"esb_mode": "off"})
    assert result["step_id"] == "reading", result
    return result


async def _setup(hass: HomeAssistant, *, reading: bool = True, esb: dict | None = None) -> config_entries.ConfigEntry:
    result = await _to_reading_step(hass, esb)
    taken = (dt_util.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"reading_day": 13876, "reading_night": 5660, "reading_at": taken} if reading else {},
    )
    assert result["step_id"] == "weather", result
    # The pin starts at the internet connection's location (looked up, not guessed).
    assert result["data_schema"]({})["location"] == {"latitude": PIN.latitude, "longitude": PIN.longitude}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"location": {"latitude": 53.3498, "longitude": -6.2603}}
    )
    assert result["step_id"] == "dashboard", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"energy_dashboard_mode": "off"})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    return result["result"]


async def test_reading_step_prefilled_with_known_reading(hass: HomeAssistant):
    """The form offers 13876 / 5660 at 18:50:34 Irish time, newer than the 27 September reading SSE holds."""
    result = await _to_reading_step(hass)
    suggested = {str(key): key.description["suggested_value"] for key in result["data_schema"].schema}
    assert suggested["reading_day"] == 13876
    assert suggested["reading_night"] == 5660
    # Shown in Home Assistant's own time zone; read back it is 18:50:34 in Ireland.
    taken = dt_util.parse_datetime(suggested["reading_at"]).replace(tzinfo=dt_util.get_default_time_zone())
    assert taken == datetime(2026, 10, 7, 17, 50, 34, tzinfo=timezone.utc)
    assert "13876" in result["description_placeholders"]["sse"]


@pytest.fixture
async def ready(hass: HomeAssistant, tmp_path):
    hass.config.config_dir = str(tmp_path)
    await async_setup_component(hass, "lovelace", {})
    await async_setup_component(hass, "frontend", {})
    entry = await _setup(hass)
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_flow_defaults_are_sse_tariff(hass: HomeAssistant, ready):
    data = ready.data
    assert data["day_rate"] == pytest.approx(32.38)
    assert data["night_rate"] == pytest.approx(19.60)
    assert data["standing_charge"] == pytest.approx(359.71)
    assert data["pso_levy"] == pytest.approx(19.10)
    assert data["valid_from"] == "2026-10-06"
    assert data["night_clock"] == "gmt"
    assert (data["latitude"], data["longitude"]) == (53.3498, -6.2603)
    assert data["house_power"] == f"{HOUSE}_power"


async def test_entities_and_live_accounting(hass: HomeAssistant, ready):
    assert hass.states.get("sensor.electricity_analyzer_tariff_band").state in ("day", "night")
    assert hass.states.get("sensor.electricity_analyzer_unit_rate").state in ("0.3238", "0.196")
    rest = hass.states.get("sensor.electricity_analyzer_rest_of_house_power")
    assert float(rest.state) == pytest.approx(4766 - 536)

    # The clamps count up; the live view follows the counters exactly. (Small steps:
    # a jump of more than 25 kW-worth inside a minute is rejected as impossible.)
    _shelly(hass, house_kwh=100.012, hall_kwh=40.003)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    today = float(hass.states.get("sensor.electricity_analyzer_today_energy").state)
    assert today == pytest.approx(0.012, abs=1e-3)
    hall_today = float(hass.states.get("sensor.electricity_analyzer_today_hall_energy").state)
    assert hall_today == pytest.approx(0.003, abs=1e-3)
    reg_day = hass.states.get("sensor.electricity_analyzer_meter_reading_day")
    reg_night = hass.states.get("sensor.electricity_analyzer_meter_reading_night")
    assert float(reg_day.state) + float(reg_night.state) == pytest.approx(13876 + 5660 + 0.012, abs=0.006)


async def test_dry_run_submission_sends_nothing_and_is_logged(hass: HomeAssistant, ready):
    analyzer = ready.runtime_data
    result = await analyzer.async_submit_reading()
    assert result["status"] == "dry_run"
    assert result["payload"]["readings"][0]["meterReading"] == "13876"
    log = await analyzer.async_submissions()
    assert log["rows"][0]["status"] == "dry_run"


async def test_submission_far_from_estimate_is_refused(hass: HomeAssistant, ready):
    analyzer = ready.runtime_data
    with pytest.raises(HomeAssistantError, match="from the estimate"):
        await analyzer.async_submit_reading(99999, 5660)
    log = await analyzer.async_submissions()
    assert log["rows"][0]["status"] == "blocked"


async def test_service_requires_confirm(hass: HomeAssistant, ready):
    with pytest.raises(HomeAssistantError, match="confirm"):
        await hass.services.async_call(DOMAIN, "submit_meter_reading", {"confirm": False}, blocking=True)


async def test_typed_reading_overrides_estimate(hass: HomeAssistant, ready):
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.electricity_analyzer_day_reading_to_submit", "value": 13880}, blocking=True
    )
    assert ready.runtime_data.reading_to_submit()["day"] == 13880
    assert ready.runtime_data.reading_to_submit()["origin"] == "manual"


async def test_websocket_table(hass: HomeAssistant, ready, hass_ws_client):
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/table", "period": "day"})
    msg = await client.receive_json()
    assert msg["success"], msg
    assert any(c["key"] == "register_day" for c in msg["result"]["columns"])
    await client.send_json({"id": 2, "type": f"{DOMAIN}/readings"})
    msg = await client.receive_json()
    assert msg["success"]


async def test_dashboard_is_written(hass: HomeAssistant, ready, hass_ws_client):
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "lovelace/config", "url_path": "electricity"})
    msg = await client.receive_json()
    assert msg["success"], msg
    titles = [v["title"] for v in msg["result"]["views"]]
    assert titles[:3] == ["Overview", "Clamps", "Explore"]
    for expected in ("Weather", "Costs & bill", "Forecast", "Meter & SSE", "Data"):
        assert expected in titles


async def test_new_reading_flows_through_everything(hass: HomeAssistant, ready):
    """A new real reading moves the anchor, the live registers and the boxes at once."""
    analyzer = ready.runtime_data
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.electricity_analyzer_day_reading_to_submit", "value": 13880}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, "set_meter_reading", {"day": 13900, "night": 5670, "taken_at": dt_util.now().isoformat()}, blocking=True
    )
    await hass.async_block_till_done()
    assert analyzer._anchor.day == 13900 and analyzer._anchor.origin == "manual"
    assert [r.origin for r in analyzer._readings][-1] == "manual"
    day = float(hass.states.get("sensor.electricity_analyzer_meter_reading_day").state)
    night = float(hass.states.get("sensor.electricity_analyzer_meter_reading_night").state)
    assert day == pytest.approx(13900, abs=0.05) and night == pytest.approx(5670, abs=0.05)
    # The boxes go back to following the estimate.
    assert analyzer.reading_to_submit()["origin"] == "estimate"
    assert analyzer.reading_to_submit()["day"] == 13900


class _CountingEsb:
    """Stands in for EsbClient in the flow and the runtime; counts real sign-ins."""

    logins = 0

    def __init__(self, http, username, password, mprn, **_kwargs) -> None:
        self._cookies: dict[str, str] = {}

    def restore_session(self, cookies: dict[str, str]) -> int:
        self._cookies = dict(cookies or {})
        return len(self._cookies)

    def export_session(self) -> dict[str, str]:
        return dict(self._cookies)

    async def is_authenticated(self) -> bool:
        return bool(self._cookies)

    async def login(self) -> None:
        type(self).logins += 1
        self._cookies = {".AspNetCore.Cookies": "signed-in"}

    async def ensure_session(self) -> None:
        if not await self.is_authenticated():
            await self.login()

    async def meter_window(self) -> dict:
        return {}

    async def download_hdf(self) -> bytes:
        rows = "".join(f"10000000001,1,0.4,Active Import Interval (kW),07-10-2026 0{h}:30\n" for h in range(4))
        return ("MPRN,Meter Serial Number,Read Value,Read Type,Read Date and End Time\n" + rows).encode()

    async def billed_reads(self) -> dict:
        return {}


async def test_setup_esb_sign_in_is_reused_not_repeated(hass: HomeAssistant, tmp_path):
    """Setup checks the ESB login; the integration must carry on with that session.

    Signing in again seconds later is exactly what made ESB answer with a CAPTCHA.
    """
    hass.config.config_dir = str(tmp_path)
    await async_setup_component(hass, "lovelace", {})
    await async_setup_component(hass, "frontend", {})
    _CountingEsb.logins = 0
    esb = {"esb_mode": "account", "esb_username": "x", "esb_password": "z", "mprn": "10000000001"}
    with patch(f"{FLOW}.EsbClient", _CountingEsb), patch(
        "custom_components.electricity_analyzer.runtime.EsbClient", _CountingEsb
    ):
        entry = await _setup(hass, esb=esb)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.runtime_data.store.fetch_state("esb").last_attempt is not None
        assert _CountingEsb.logins == 1
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_sse_cooldown_counts_a_reading_entered_on_sse(hass: HomeAssistant, ready):
    """A reading SSE already holds (sent from its own site) starts the 24 hours too."""
    from custom_components.electricity_analyzer.engine.sse.client import MeterInfo, Register

    analyzer = ready.runtime_data
    analyzer.sse_meter = MeterInfo(
        premises_id="1", mprn="10000000001", meter_number="1", model="RM108", service_number="1",
        dials=6, de_date="08/10/2026", meter_type="Day/Night", submitted_last_24h=True,
        registers=[
            Register(1, "day", 13876.0, "07/10/2026", "Accepted", "07/10/2026 18:50:34"),
            Register(2, "night", 5660.0, "07/10/2026", "Accepted", "07/10/2026 18:50:34"),
        ],
    )
    # 18:50:34 Irish summer time is 17:50:34 UTC; the next is allowed a day later.
    assert analyzer._sse_next_allowed(None) == datetime(2026, 10, 8, 17, 50, 34, tzinfo=timezone.utc)


async def test_an_sse_estimate_is_never_learned_from(hass: HomeAssistant, ready):
    from custom_components.electricity_analyzer.engine.sse.client import MeterInfo, Register

    def meter(method: str) -> MeterInfo:
        return MeterInfo(
            premises_id="1", mprn="10000000001", meter_number="1", model="RM108", service_number="1",
            dials=6, de_date="08/10/2026", meter_type="Day/Night", submitted_last_24h=False,
            registers=[
                Register(1, "day", 13990.0, "07/10/2026", "Accepted", "07/10/2026 18:50:34", method),
                Register(2, "night", 5700.0, "07/10/2026", "Accepted", "07/10/2026 18:50:34", method),
            ],
        )

    analyzer = ready.runtime_data
    assert analyzer._sse_reading(meter("E")) is None
    assert analyzer._sse_reading(meter("C")).day == 13990.0


async def test_esb_captcha_wait_survives_a_restart(hass: HomeAssistant, tmp_path):
    """After a CAPTCHA a restart must not try again: the 12-hour wait is in the database.
    (No Repairs item: nothing is handed over by hand; it simply retries 12 hours later.)"""
    from homeassistant.helpers import issue_registry as ir

    hass.config.config_dir = str(tmp_path)
    await async_setup_component(hass, "lovelace", {})
    await async_setup_component(hass, "frontend", {})
    esb = {"esb_mode": "account", "esb_username": "x", "esb_password": "z", "mprn": "10000000001"}
    with patch(f"{FLOW}.EsbClient", _CountingEsb), patch(
        "custom_components.electricity_analyzer.runtime.EsbClient", _CountingEsb
    ):
        entry = await _setup(hass, esb=esb)
        await hass.async_block_till_done(wait_background_tasks=True)
        entry.runtime_data.store.mark_failure("esb", "captcha: ESB Networks is showing a CAPTCHA")
        _CountingEsb.logins = 0
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert _CountingEsb.logins == 0
        assert entry.runtime_data.waiting_until("esb") is not None
        assert ir.async_get(hass).async_get_issue(DOMAIN, "esb_captcha") is None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_never_more_than_two_esb_sign_ins_a_day(hass: HomeAssistant, tmp_path):
    """Setup's sign-in plus one more, then no further sign-in for 24 hours, even when forced."""
    hass.config.config_dir = str(tmp_path)
    await async_setup_component(hass, "lovelace", {})
    await async_setup_component(hass, "frontend", {})
    _CountingEsb.logins = 0
    esb = {"esb_mode": "account", "esb_username": "x", "esb_password": "z", "mprn": "10000000001"}
    with patch(f"{FLOW}.EsbClient", _CountingEsb), patch(
        "custom_components.electricity_analyzer.runtime.EsbClient", _CountingEsb
    ):
        entry = await _setup(hass, esb=esb)
        await hass.async_block_till_done(wait_background_tasks=True)
        analyzer = entry.runtime_data
        assert _CountingEsb.logins == 1 and len(await analyzer._recent_esb_sign_ins()) == 1
        for _ in range(3):  # the session dies each time; "Fetch ESB now" pressed again and again
            analyzer.store.clear_session("esb")
            analyzer._esb_restored = False
            await analyzer.async_esb_tick(force=True)
        assert _CountingEsb.logins == 2
        assert "not signing in to ESB" in analyzer.store.fetch_state("esb").last_error
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_submit_refused_locally_when_sse_already_has_a_reading(hass: HomeAssistant, ready):
    """No sign-in at all when SSE's own last reading is inside its 24 hours."""
    from custom_components.electricity_analyzer.engine.sse.client import MeterInfo, Register

    analyzer = ready.runtime_data
    taken = dt_util.now().astimezone(dt_util.get_time_zone("Europe/Dublin")) - timedelta(hours=2)
    stamp = taken.strftime("%d/%m/%Y %H:%M:%S")
    analyzer.sse_meter = MeterInfo(
        premises_id="1", mprn="10000000001", meter_number="1", model="RM108", service_number="1",
        dials=6, de_date=taken.strftime("%d/%m/%Y"), meter_type="Day/Night", submitted_last_24h=True,
        registers=[Register(1, "day", 13876.0, stamp[:10], "Accepted", stamp, "C"),
                   Register(2, "night", 5660.0, stamp[:10], "Accepted", stamp, "C")],
    )
    with patch.object(type(analyzer), "sse_mode", new_callable=PropertyMock, return_value="live"), patch(
        "custom_components.electricity_analyzer.runtime.SseClient"
    ) as client:
        with pytest.raises(HomeAssistantError, match="has one from"):
            await analyzer.async_submit_reading(13900, 5670)
    client.assert_not_called()
