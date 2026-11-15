"""SSE Airtricity client against the local mock portal (dev/sse_mock).

The mock reproduces the captured portal's request and response shapes, so these tests
pin down exactly what would be posted, and every guard that stops a bad reading from
being posted. Nothing here touches the network beyond 127.0.0.1.
"""

from __future__ import annotations

from datetime import datetime, timezone

import aiohttp
import pytest
from aiohttp import web

from custom_components.electricity_analyzer.engine.errors import AuthFailed, PortalError
from custom_components.electricity_analyzer.engine.sse.client import (
    MeterInfo,
    PortalConfig,
    Register,
    SseClient,
    SubmissionBlocked,
    accounts_from_claim,
    build_payload,
    history_row,
    is_estimated,
    normalise_latest_bill,
    sanitize_billing,
    validate_reading,
)

import server as mock


@pytest.fixture
async def portal(socket_enabled, unused_tcp_port):
    mock.STATE.clear()
    mock.STATE.update(mock._fresh_state())
    runner = web.AppRunner(mock.make_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    yield f"http://127.0.0.1:{unused_tcp_port}"
    await runner.cleanup()


@pytest.fixture
async def http():
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True, quote_cookie=False)) as s:
        yield s


async def test_wrong_password_is_rejected(portal, http):
    with pytest.raises(AuthFailed):
        await SseClient(http, "x", "wrong", app_url=portal).meter_info()


async def test_reads_meter_and_submits_exact_payload(portal, http):
    client = SseClient(http, "x", "z", app_url=portal)
    info = await client.meter_info()
    assert info.dials == 6 and info.model == "RM108"
    assert info.register("day").last_value == 13708
    payload, answer, after = await client.submit_reading(13876, 5660)
    assert payload == {
        "deDate": info.de_date,
        "meterModel": "RM108",
        "meterNumber": mock.METER,
        "meterServiceId": "1",
        "readings": [
            {"dialsNum": 6, "meterReading": "13876", "registerNum": 1},
            {"dialsNum": 6, "meterReading": "5660", "registerNum": 2},
        ],
    }
    assert answer["code"] == "CREATED"
    assert after.submitted_last_24h
    assert mock.STATE["submissions"][0]["body"] == payload


async def test_second_submission_inside_24_hours_is_blocked(portal, http):
    await SseClient(http, "x", "z", app_url=portal).submit_reading(13876, 5660)
    with pytest.raises(SubmissionBlocked):
        await SseClient(http, "x", "z", app_url=portal).submit_reading(13877, 5661)
    assert len(mock.STATE["submissions"]) == 1


async def test_implausible_readings_never_leave(portal, http):
    client = SseClient(http, "x", "z", app_url=portal)
    for day, night in ((13000, 5660), (138760, 5660), (13876, 99999)):
        with pytest.raises(SubmissionBlocked):
            await client.submit_reading(day, night)
    assert mock.STATE["submissions"] == []


def test_validation_rules():
    validate_reading("day", 13876, 6, 13708, last_date="2026-09-27 00:00:00", today=datetime(2026, 10, 7, tzinfo=timezone.utc))
    with pytest.raises(SubmissionBlocked):
        validate_reading("day", 1_000_000, 6, None)
    with pytest.raises(SubmissionBlocked):
        validate_reading("day", 20000, 6, 13708, last_date="2026-09-27 00:00:00", today=datetime(2026, 10, 7, tzinfo=timezone.utc))


def test_payload_maps_registers_by_type():
    meter = MeterInfo("1", "m", "n", "RM108", "1", 6, "07/10/2026", "Day/Night", False,
                      [Register(2, "night", 5582, None, None, None), Register(1, "day", 13708, None, None, None)])
    body = build_payload(meter, 13876, 5660)
    assert [r["registerNum"] for r in body["readings"]] == [1, 2]
    assert body["readings"][0]["meterReading"] == "13876"


def test_account_claim():
    assert accounts_from_claim("1234567D") == ["1234567"]
    assert accounts_from_claim("1D,22S") == ["1", "22"]


async def test_reads_history_and_account_details(portal, http):
    client = SseClient(http, "x", "z", app_url=portal)
    info = await client.meter_info()
    assert info.meter_kind == "Legacy"
    await client.submit_reading(13876, 5660)
    history = await client.reading_history(info.premises_id)
    # Newest first; the opening reads are ESB actuals, ours are customer reads.
    assert [(r["date"], r["register"], r["value"], r["method"]) for r in history][-2:] == [
        ("2026-09-27", "night", 5582.0, "A"),
        ("2026-09-27", "day", 13708.0, "A"),
    ]
    newest = {r["register"]: r for r in history[:2]}
    assert newest["day"]["value"] == 13876 and newest["day"]["units_used"] == 168
    assert newest["night"]["units_used"] == 78 and not newest["night"]["estimated"]
    billing = await client.billing_info()
    assert billing["next_bill_date"] == "2026-11-08"
    assert billing["balance_eur"] == 0.0 and billing["balance_status"] == "CREDIT"
    assert billing["contract_end"] == "2027-09-26" and billing["supply_status"] == "Current"
    assert billing["transactions"] == [] and billing["latest_bill"] is None


def test_history_row_matches_the_portal():
    """A row exactly as SSE's portal returned it (7 October 2026)."""
    row = {
        "readingDate": "07/10/2026", "readingValue": "13876", "readingStatus": "Accepted", "meterType": "Day",
        "meterNumber": "10000001", "readingMethod": "C", "unitsUsed": "168", "meterMultiplier": "1",
        "energyUsage": "168", "energyUsageUnits": "kWh", "eMeterType": "Legacy",
    }
    assert history_row(row) == {
        "date": "2026-10-07", "register": "day", "value": 13876.0, "status": "Accepted",
        "method": "C", "estimated": False, "units_used": 168.0,
    }
    assert history_row({"readingDate": "", "readingValue": "1"}) is None


def test_estimates_are_recognised():
    assert is_estimated("E", "Accepted")
    assert is_estimated(None, "Estimated")
    assert not is_estimated("C", "Accepted") and not is_estimated("A", None)


def test_latest_bill_normalisation_and_pii_stripping():
    raw_latest = {
        "billDate": "2026-11-09",
        "statementNumber": "99999999",
        "totalBillAmount": "€7.16",
        "billAmountStatus": "CREDIT",
        "previousBalance": "€316.22",
        "previousBalanceStatus": "CREDIT",
        "premisesCount": 1,
        "premisesBillSummary": [
            {
                "premisesId": "100001",
                "premisesAddress": "1 MOCK STREET, DUBLIN",
                "totalElectricityInvoice": "€309.06",
                "totalGasInvoice": "€0.00",
                "totalAllInvoices": "€309.06",
                "eMeterType": "Legacy",
            }
        ],
    }
    raw_txs = [
        {
            "transactionDate": "2026-11-09",
            "transactionDescription": "Bill",
            "statementNumber": "99999999",
            "premiseId": "100001",
            "utilityType": "E",
            "transactionAmount": "€309.06",
            "transactionStatus": "DEBIT",
            "reversed": False,
        }
    ]
    norm = normalise_latest_bill(raw_latest, raw_txs)
    assert norm == {
        "bill_date": "2026-11-09",
        "period_start": None,
        "period_end": None,
        "electricity_eur": 309.06,
        "gas_eur": 0.0,
        "invoice_eur": 309.06,
        "balance_after_eur": 7.16,
        "balance_after_status": "CREDIT",
        "previous_balance_eur": 316.22,
        "previous_balance_status": "CREDIT",
    }
    sanitized = sanitize_billing({"latest_bill": raw_latest, "transactions": raw_txs, "next_bill_date": "2027-01-08"})
    dumped = str(sanitized)
    assert "1 MOCK STREET" not in dumped
    assert "99999999" not in dumped
    assert sanitized["last_bill_date"] == "2026-11-09"


async def test_discover_falls_back_to_cached_config_on_outage(socket_enabled, unused_tcp_port, http):
    async def maintenance(_req):
        return web.Response(status=503, text="Maintenance")

    app = web.Application()
    app.router.add_get("/", maintenance)
    runner = web.AppRunner(app)
    await runner.setup()
    url = f"http://127.0.0.1:{unused_tcp_port}"
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    try:
        with pytest.raises(PortalError, match="HTTP 503"):
            await SseClient(http, "x", "z", app_url=url).discover()
        cached = PortalConfig(url, f"{url}/api", "sub", f"{url}/login", "t", "c", "p", "s")
        cfg = await SseClient(http, "x", "z", app_url=url, fallback_config=cached).discover()
        assert cfg == cached
    finally:
        await runner.cleanup()
