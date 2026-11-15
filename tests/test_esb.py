"""ESB Networks client against the local mock portal (dev/esb_mock).

The integration's own client, unmodified, signs in through the mock's Azure AD B2C
journey and downloads the data, exactly as it would from myaccount.esbnetworks.ie:
the portal's host names resolve to 127.0.0.1 here, and only the port differs.
Nothing touches the network beyond 127.0.0.1.
"""

from __future__ import annotations

import importlib.util
import os
import socket
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver

from custom_components.electricity_analyzer.engine.errors import AuthFailed, CaptchaEncountered
from custom_components.electricity_analyzer.engine.esb import client as esb
from custom_components.electricity_analyzer.engine.esb.hdf import parse_hdf
from custom_components.electricity_analyzer.engine.esb.reads import parse_billed_reads

os.environ["ESB_MOCK_DAYS"] = "20"  # a short history keeps the tests quick
os.environ.pop("SHELLY_SIM_MODEL", None)
_spec = importlib.util.spec_from_file_location(
    "esb_mock_server", Path(__file__).resolve().parents[1] / "dev" / "esb_mock" / "server.py"
)
mock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mock)

MPRN = mock.MPRN


class _Loopback(AbstractResolver):
    """Every host name is this machine: the mock answers for both ESB hosts."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict]:
        return [
            {"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET, "proto": 0, "flags": 0}
        ]

    async def close(self) -> None:
        return None


@pytest.fixture
async def portal(socket_enabled, unused_tcp_port, monkeypatch):
    mock.STATE.clear()
    mock.STATE.update(mock._fresh_state())
    runner = web.AppRunner(mock.make_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    monkeypatch.setattr(esb, "ESB_BASE", f"http://{esb.APP_HOST}:{unused_tcp_port}")
    yield mock.STATE
    await runner.cleanup()


def _session(jar: aiohttp.CookieJar | None = None) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        cookie_jar=jar or esb.new_cookie_jar(), connector=aiohttp.TCPConnector(resolver=_Loopback())
    )


async def test_signs_in_and_downloads_everything(portal):
    async with _session() as http:
        client = esb.EsbClient(http, "x", "z", MPRN)
        assert not await client.is_authenticated()
        await client.ensure_session()
        assert await client.is_authenticated()
        assert portal["logins"] == 1

        window = await client.meter_window()
        data = await mock.dataset()
        assert window["end_date"] == (data.end.astimezone(mock.DUBLIN).date() - timedelta(days=1)).isoformat()

        dataset = parse_hdf(await client.download_hdf())
        imports = dataset.imports()
        assert len(imports) == len(data.intervals)
        # kW per half hour on the wire, kWh after parsing: the same energy.
        assert sum(r.kwh for r in imports) == pytest.approx(sum(k for _, k in data.intervals), abs=0.05)
        assert min(r.start_utc for r in imports) == data.start
        assert max(r.start_utc for r in imports) == data.intervals[-1][0]

        reads = parse_billed_reads(await client.billed_reads())
        assert {band for _, band, _, _ in reads} == {"day", "night"}
        assert all(details == "Scheduled Reading (Actual)" for *_, details in reads)


async def test_reads_count_back_from_the_anchor(portal):
    """A read is the meter's registers at the start of its day, counted from the anchor."""
    data = await mock.dataset()
    stamp, day, night = mock.ANCHOR.split(",")
    anchor_day = datetime.fromisoformat(stamp).date()
    newest_day = max(r[0] for r in data.reads)
    newest = {band: value for d, band, value in data.reads if d == newest_day}
    sign = 1 if newest_day > anchor_day else -1
    span_days = max(abs((newest_day - anchor_day).days), 1) + 2
    assert 0 < sign * (newest["day"] - float(day)) < 25 * span_days
    assert 0 < sign * (newest["night"] - float(night)) < 25 * span_days


async def test_wrong_password_is_rejected(portal):
    async with _session() as http:
        with pytest.raises(AuthFailed, match="rejected"):
            await esb.EsbClient(http, "x", "wrong", MPRN).ensure_session()
    assert portal["failures"] == 1 and portal["logins"] == 0


async def test_captcha_after_repeated_failures_is_not_worked_around(portal):
    for _ in range(mock.CAPTCHA_AFTER_FAILURES):
        async with _session() as http:
            with pytest.raises(AuthFailed):
                await esb.EsbClient(http, "x", "wrong", MPRN).ensure_session()
    async with _session() as http:
        with pytest.raises(CaptchaEncountered, match="landing page"):
            await esb.EsbClient(http, "x", "z", MPRN).ensure_session()
    assert portal["logins"] == 0


async def test_captcha_at_the_credential_step(portal):
    async with _session() as http:
        with pytest.raises(CaptchaEncountered, match="credential"):
            await esb.EsbClient(http, "captcha", "z", MPRN).ensure_session()


async def test_saved_session_skips_signing_in(portal):
    async with _session() as http:
        client = esb.EsbClient(http, "x", "z", MPRN)
        await client.ensure_session()
        saved = client.export_session()
    assert saved and portal["logins"] == 1
    async with _session() as http:
        client = esb.EsbClient(http, "x", "z", MPRN)
        assert client.restore_session(saved) == len(saved)
        await client.ensure_session()
        await client.download_hdf()
    assert portal["logins"] == 1  # the saved session was used; no second sign-in

