#!/usr/bin/env python3
"""A local stand-in for ESB Networks' customer portal. DEV AND TESTS ONLY.

It answers under the portal's real host names, so the integration's own, unmodified
client signs in to it exactly as it signs in to ESB:

  myaccount.esbnetworks.ie   the app
    GET  /                                   no session: 302 to the B2C authorize page
    GET  /Api/HistoricConsumption            200 with a session, else 302 to sign-in
    POST /signin-oidc                        code + state -> session cookie, 302 to /
    GET  /af/t                               antiforgery token (session required)
    POST /datahub/GetMprnTypeAndStartEndDate
    POST /DataHub/DownloadHdfPeriodic        the HDF CSV, newest first, kW per half hour
    POST /MeterReadHistory/GetBilledReads    the meter's scheduled register reads
  login.esbnetworks.ie       Azure AD B2C on ESB's own domain
    GET  /<tenant>/<policy>/oauth2/v2.0/authorize      page with `var SETTINGS = {...};`
    POST /<tenant>/<policy>/SelfAsserted               credentials -> {"status": "200"}
    GET  /<tenant>/<policy>/api/CombinedSigninAndSignup/confirmed
                                                       auto-posting form to /signin-oidc

The parts the real portal is strict about, this is strict about too: the CSRF header
must match B2C's csrf cookie; the `x-ms-cpim-cache|...` cookie must come back unquoted
(B2C answers a bare 400 otherwise); every data POST needs a token from /af/t.

Credentials: any email, password "z" (the "x / z" nonsense pair). A wrong password is
rejected the way B2C rejects it; after three wrong ones the sign-in page turns into a
CAPTCHA, as ESB's does. The email "captcha" gets a CAPTCHA straight away.

The data: with SHELLY_SIM_MODEL pointing at the simulated Shelly's model.py, the meter
reads what the simulated full-house clamp measures plus a steady 1.2% (a typical CT
against a utility meter) and a little noise, so the sandbox's ESB figures line up with
its clamp. Without it, a plain synthetic house. Published two to three days late, like
ESB. Register reads are counted back from ESB_MOCK_ANCHOR (UTC time, day, night).

Inspection: GET /__mock/state on either host (or http://localhost:8091/__mock/state).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import secrets
import ssl
import sys
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiohttp import web

UTC = timezone.utc
DUBLIN = ZoneInfo("Europe/Dublin")

APP_HOST = "myaccount.esbnetworks.ie"
LOGIN_HOST = "login.esbnetworks.ie"
TENANT = "mockesbb2c.onmicrosoft.com"
POLICY = "B2C_1A_signup_signin"
CLIENT_ID = "22222222-mock-esb0-2222-222222222222"
PASSWORD = os.environ.get("ESB_MOCK_PASSWORD", "z")
MPRN = "10000000001"
METER = "10000001"
CAPTCHA_AFTER_FAILURES = 3

SESSION_COOKIE = ".AspNetCore.Cookies"
CACHE_COOKIE = "x-ms-cpim-cache|mockesb_0"

METER_BIAS = 1.012
NOISE = 0.015
PUBLISH_HOUR_LOCAL = 10  # a day appears at about 10:00, two days later
READ_EVERY_DAYS = 61

DAYS = int(os.environ.get("ESB_MOCK_DAYS", "400"))
ANCHOR = os.environ.get("ESB_MOCK_ANCHOR", "2026-10-07T17:50:34+00:00,13876,5660")


def _fresh_state() -> dict[str, Any]:
    return {
        "transactions": {},  # tx -> {"csrf", "state", "authenticated", "code"}
        "codes": {},  # code -> tx
        "sessions": set(),
        "xsrf": set(),
        "failures": 0,
        "logins": 0,
        "requests": [],
    }


STATE: dict[str, Any] = _fresh_state()


# ------------------------------------------------------------------- the data


def _load_model() -> Any:
    path = os.environ.get("SHELLY_SIM_MODEL")
    if not path:
        return None
    spec = importlib.util.spec_from_file_location("shelly_sim_model", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # its dataclasses look themselves up there
    spec.loader.exec_module(module)
    return module.ShellySimModel(int(os.environ.get("SHELLY_SIM_SEED", "42")))


def _unit(*key: object) -> float:
    """A repeatable number in [0, 1) for a key."""
    digest = hashlib.sha256(repr(key).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


class Meter:
    """Half-hourly kWh as the utility meter counts it, a pure function of time."""

    def __init__(self, model: Any = None) -> None:
        self.model = model

    def kwh(self, start: datetime) -> float:
        end = start + timedelta(minutes=30)
        if self.model is not None:
            clamp = self.model.energy_kwh(start, end)["house"]
        else:
            local = start.astimezone(DUBLIN)
            hour = local.hour + local.minute / 60
            winter = 0.5 + 0.5 * math.cos(2 * math.pi * (local.timetuple().tm_yday - 15) / 365)
            evening = math.exp(-(((hour - 19) / 2.5) ** 2))
            clamp = 0.18 + 0.12 * winter + 0.35 * evening + 0.1 * _unit("load", start.timestamp())
        noise = 1 + NOISE * (2 * _unit("noise", start.timestamp()) - 1)
        return max(0.0, clamp * METER_BIAS * noise)


def published_through(now: datetime) -> datetime:
    """The newest local midnight ESB would have published up to by `now`."""
    local = now.astimezone(DUBLIN)
    lag = 2 if local.hour >= PUBLISH_HOUR_LOCAL else 3
    day = local.date() - timedelta(days=lag - 1)
    return datetime.combine(day, time(0), DUBLIN).astimezone(UTC)


def _night(start: datetime) -> bool:
    """A day/night meter keeps GMT all year: night is 23:00-08:00 UTC."""
    return start.hour >= 23 or start.hour < 8


class Dataset:
    """Every published interval and the scheduled reads, built once and kept."""

    def __init__(self, meter: Meter, now: datetime, days: int = DAYS) -> None:
        self.end = published_through(now)
        start_day = self.end.astimezone(DUBLIN).date() - timedelta(days=days)
        self.start = datetime.combine(start_day, time(0), DUBLIN).astimezone(UTC)
        self.intervals: list[tuple[datetime, float]] = []
        t = self.start
        while t < self.end:
            self.intervals.append((t, meter.kwh(t)))
            t += timedelta(minutes=30)
        self.reads = self._reads(meter)

    def _reads(self, meter: Meter) -> list[tuple[date, str, float]]:
        """Register reads at the start of every 61st local day, counted from the anchor."""
        when, day, night = ANCHOR.split(",")
        anchor = datetime.fromisoformat(when).astimezone(UTC)
        registers = {"day": float(day), "night": float(night)}
        step = timedelta(minutes=30)
        known = dict(self.intervals)

        def kwh(t: datetime) -> float:
            return known[t] if t in known else meter.kwh(t)

        # Running meter energy per band, from the earliest half hour needed.
        anchor_slot = anchor.replace(minute=anchor.minute // 30 * 30, second=0, microsecond=0)
        lo, hi = min(self.start, anchor_slot), max(self.end, anchor_slot + step)
        running: dict[datetime, dict[str, float]] = {}
        total = {"day": 0.0, "night": 0.0}
        t = lo
        while t <= hi:
            running[t] = dict(total)
            total["night" if _night(t) else "day"] += kwh(t)
            t += step

        def counted(x: datetime) -> dict[str, float]:
            slot = x.replace(minute=x.minute // 30 * 30, second=0, microsecond=0)
            out = dict(running[slot])
            out["night" if _night(slot) else "day"] += kwh(slot) * ((x - slot) / step)
            return out

        at_anchor = counted(anchor)
        out: list[tuple[date, str, float]] = []
        d = self.end.astimezone(DUBLIN).date()
        while datetime.combine(d, time(0), DUBLIN) > self.start:
            then = counted(datetime.combine(d, time(0), DUBLIN).astimezone(UTC))
            for band in ("day", "night"):
                out.append((d, band, math.floor(registers[band] - (at_anchor[band] - then[band]))))
            d -= timedelta(days=READ_EVERY_DAYS)
        return out

    def hdf(self, kind: str) -> str:
        rows = ["MPRN,Meter Serial Number,Read Value,Read Type,Read Date and End Time"]
        per_kw = kind.lower() == "intervalkw"
        label = "Active Import Interval (kW)" if per_kw else "Active Import Interval (kWh)"
        for start, kwh in reversed(self.intervals):  # ESB serves newest first
            end = (start + timedelta(minutes=30)).astimezone(DUBLIN)
            value = kwh * 2 if per_kw else kwh
            rows.append(f"{MPRN},{METER},{value:.3f},{label},{end:%d-%m-%Y %H:%M}")
        return "\n".join(rows) + "\n"


_DATA: dict[str, Any] = {"meter": None, "dataset": None, "lock": None}


async def dataset() -> Dataset:
    """Built on first use (a year of the Shelly model takes a few seconds), then kept for the day."""
    if _DATA["lock"] is None:
        _DATA["lock"] = asyncio.Lock()
    async with _DATA["lock"]:
        now = datetime.now(UTC)
        current = _DATA["dataset"]
        if current is None or current.end != published_through(now):
            if _DATA["meter"] is None:
                _DATA["meter"] = Meter(await asyncio.to_thread(_load_model))
            _DATA["dataset"] = await asyncio.to_thread(Dataset, _DATA["meter"], now)
        return _DATA["dataset"]


# --------------------------------------------------------------------- hosts


def _origin(request: web.Request, host: str) -> str:
    """The other host's origin, keeping this request's scheme and port."""
    port = request.host.partition(":")[2]
    return f"{request.scheme}://{host}{':' + port if port else ''}"


def _b2c(request: web.Request) -> str:
    return f"{_origin(request, LOGIN_HOST)}/{TENANT}/{POLICY}"


def _secure(request: web.Request) -> bool:
    return request.scheme == "https"


def _record(request: web.Request) -> None:
    STATE["requests"].append(f"{request.method} {request.host.partition(':')[0]}{request.path}")


def _has_session(request: web.Request) -> bool:
    return request.cookies.get(SESSION_COOKIE) in STATE["sessions"]


def _to_sign_in(request: web.Request) -> web.Response:
    """What the app does without a session: start an OIDC handshake."""
    state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    query = urlencode(
        {
            "client_id": CLIENT_ID,
            "redirect_uri": f"{_origin(request, APP_HOST)}/signin-oidc",
            "response_type": "code",
            "scope": "openid profile offline_access",
            "response_mode": "form_post",
            "state": state,
            "nonce": nonce,
        }
    )
    response = web.Response(status=302, headers={"Location": f"{_b2c(request).lower()}/oauth2/v2.0/authorize?{query}"})
    kw = {"path": "/signin-oidc", "httponly": True, "secure": _secure(request)}
    response.set_cookie(f".AspNetCore.Correlation.{state[:8]}", "N", **kw)
    response.set_cookie(f".AspNetCore.OpenIdConnect.Nonce.{nonce[:8]}", "N", **kw)
    return response


# ----------------------------------------------------------------------- app


async def app_root(request: web.Request) -> web.Response:
    _record(request)
    if not _has_session(request):
        return _to_sign_in(request)
    return web.Response(text="<html><body>ESB Networks (mock): signed in</body></html>", content_type="text/html")


async def historic(request: web.Request) -> web.Response:
    _record(request)
    if not _has_session(request):
        return _to_sign_in(request)
    return web.Response(text="<html><body>Historic consumption (mock)</body></html>", content_type="text/html")


async def signin_oidc(request: web.Request) -> web.Response:
    _record(request)
    form = await request.post()
    tx = STATE["codes"].pop(str(form.get("code", "")), None)
    if tx is None or STATE["transactions"].get(tx, {}).get("state") != form.get("state"):
        return web.Response(status=400, text="invalid code or state")
    if not any(name.startswith(".AspNetCore.Correlation.") for name in request.cookies):
        return web.Response(status=400, text="correlation failed")
    session = secrets.token_urlsafe(24)
    STATE["sessions"].add(session)
    STATE["logins"] += 1
    response = web.Response(status=302, headers={"Location": "/"})
    response.set_cookie(SESSION_COOKIE, session, path="/", httponly=True, secure=_secure(request))
    return response


async def antiforgery(request: web.Request) -> web.Response:
    _record(request)
    if not _has_session(request):
        return web.Response(status=401)
    token = secrets.token_urlsafe(16)
    STATE["xsrf"].add(token)
    response = web.json_response({"token": token})
    response.set_cookie("XSRF-TOKEN", token, path="/", secure=_secure(request))
    return response


def _data_call(handler):
    async def guarded(request: web.Request) -> web.StreamResponse:
        _record(request)
        if not _has_session(request):
            return web.Response(status=401)
        if request.headers.get("x-xsrf-token") not in STATE["xsrf"]:
            return web.Response(status=400, text="antiforgery token missing or wrong")
        return await handler(request, await request.json())

    return guarded


@_data_call
async def meter_window(request: web.Request, body: dict) -> web.Response:
    if body.get("mprn") != MPRN:
        return web.Response(status=400, text="unknown MPRN")
    data = await dataset()
    return web.json_response(
        {
            "mprn": MPRN,
            "isSmart": True,
            "isNightsaver": True,
            "start_date": data.start.astimezone(DUBLIN).date().isoformat(),
            "end_date": (data.end.astimezone(DUBLIN).date() - timedelta(days=1)).isoformat(),
        }
    )


@_data_call
async def download_hdf(request: web.Request, body: dict) -> web.Response:
    if body.get("mprn") != MPRN:
        return web.Response(status=400, text="unknown MPRN")
    data = await dataset()
    return web.Response(
        body=data.hdf(str(body.get("searchType") or "intervalkw")).encode(),
        content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="HDF_kW_{MPRN}.csv"'},
    )


@_data_call
async def billed_reads(request: web.Request, body: dict) -> web.Response:
    if body.get("MPRN") != MPRN:  # upper case here, as on the real portal
        return web.Response(status=400, text="unknown MPRN")
    data = await dataset()
    readings = [
        {
            "date": f"{d:%d-%b-%y}",
            "meterNumber": METER,
            "registerNumber": "001" if band == "day" else "002",
            "reading": f"{value:.14f}",
            "registerType": band.capitalize(),
            "details": "Scheduled Reading (Actual)",
        }
        for d, band, value in data.reads
    ]
    return web.json_response(
        {"readings": readings, "currentPage": 1, "totalPages": max(1, math.ceil(len(readings) / 8)), "pageSize": 8}
    )


# ---------------------------------------------------------------------- B2C


_CAPTCHA_SETTINGS = '"remoteResource":"https://login.esbnetworks.ie/static/b2cpages/captcha.html"'


def _captcha_page() -> web.Response:
    return web.Response(
        text=f"<html><head><script>var SETTINGS = {{{_CAPTCHA_SETTINGS}}};</script></head><body></body></html>",
        content_type="text/html",
    )


async def authorize(request: web.Request) -> web.Response:
    _record(request)
    if STATE["failures"] >= CAPTCHA_AFTER_FAILURES:
        return _captcha_page()
    tx = "StateProperties=" + secrets.token_urlsafe(24)
    csrf = secrets.token_urlsafe(24)
    STATE["transactions"][tx] = {"csrf": csrf, "state": request.query.get("state"), "authenticated": False}
    settings = {
        "remoteResource": f"{_origin(request, LOGIN_HOST)}/static/b2cpages/selfAsserted.html",
        "csrf": csrf,
        "transId": tx,
        "pageViewId": secrets.token_hex(8),
        "api": "CombinedSigninAndSignup",
        "hosts": {"tenant": f"/{TENANT}/{POLICY}", "policy": POLICY, "static": "/static/"},
        "config": {"enableRememberMe": "true"},
    }
    page = (
        "<!DOCTYPE html><html><head><title>Sign in</title>"
        f"<script>var SETTINGS = {json.dumps(settings)};</script></head>"
        '<body><div id="api"></div></body></html>'
    )
    response = web.Response(text=page, content_type="text/html")
    kw = {"path": f"/{TENANT}", "secure": _secure(request)}
    response.set_cookie("x-ms-cpim-csrf", csrf, **kw)
    response.set_cookie("x-ms-cpim-trans", secrets.token_urlsafe(12), **kw)
    # A value that a quoting cookie jar would wrap in double quotes. B2C refuses those.
    response.set_cookie(CACHE_COOKIE, "eyJUaWQiOiJtb2NrIn0=/mock:0", **kw)
    return response


def _cache_cookie_ok(request: web.Request) -> bool:
    raw = request.headers.get("Cookie", "")
    return f"{CACHE_COOKIE}=eyJ" in raw


async def self_asserted(request: web.Request) -> web.Response:
    _record(request)
    tx = STATE["transactions"].get(request.query.get("tx", ""))
    if (
        tx is None
        or request.headers.get("X-CSRF-TOKEN") != tx["csrf"]
        or request.cookies.get("x-ms-cpim-csrf") != tx["csrf"]
        or not _cache_cookie_ok(request)
    ):
        return web.Response(status=400)  # B2C's bare 400: no body, no hint
    form = await request.post()
    if str(form.get("signInName", "")).lower() == "captcha":
        return web.Response(text=f"<script>var SETTINGS = {{{_CAPTCHA_SETTINGS}}};</script>", content_type="text/html")
    if form.get("password") != PASSWORD or not form.get("signInName"):
        STATE["failures"] += 1
        return web.json_response(
            {"status": "400", "errorCode": "AADB2C90225", "message": "Your password is incorrect."}
        )
    tx["authenticated"] = True
    return web.json_response({"status": "200"})


async def confirmed(request: web.Request) -> web.Response:
    _record(request)
    tx_id = request.query.get("tx", "")
    tx = STATE["transactions"].get(tx_id)
    if tx is None or not tx["authenticated"] or request.query.get("csrf_token") != tx["csrf"]:
        return web.Response(status=400, text="transaction not authenticated")
    code = secrets.token_urlsafe(24)
    STATE["codes"][code] = tx_id
    action = f"{_origin(request, APP_HOST)}/signin-oidc"
    page = (
        "<html><body onload=\"document.getElementById('auto').submit()\">"
        f'<form id="auto" method="post" action="{escape(action)}">'
        f'<input type="hidden" name="state" value="{escape(tx["state"] or "")}">'
        f'<input type="hidden" name="code" value="{code}">'
        "</form></body></html>"
    )
    return web.Response(text=page, content_type="text/html")


# ---------------------------------------------------------------- inspection


async def mock_state(request: web.Request) -> web.Response:
    data = _DATA["dataset"]
    return web.json_response(
        {
            "logins": STATE["logins"],
            "failures": STATE["failures"],
            "requests": STATE["requests"][-50:],
            "intervals": len(data.intervals) if data else None,
            "published_through": data.end.isoformat() if data else None,
            "source": "shelly_sim model" if _DATA["meter"] and _DATA["meter"].model else "synthetic",
        }
    )


async def mock_reset(request: web.Request) -> web.Response:
    STATE.clear()
    STATE.update(_fresh_state())
    return web.json_response({"reset": True})


# --------------------------------------------------------------------- wiring


def make_app() -> web.Application:
    """Both hosts in one app; the Host header decides which one a request meant."""
    app_routes = web.UrlDispatcher()
    login_routes = web.UrlDispatcher()
    app_routes.add_get("/", app_root)
    app_routes.add_get("/Api/HistoricConsumption", historic)
    app_routes.add_get("/energymanagement/historic-consumption", historic)
    app_routes.add_post("/signin-oidc", signin_oidc)
    app_routes.add_get("/af/t", antiforgery)
    app_routes.add_post("/datahub/GetMprnTypeAndStartEndDate", meter_window)
    app_routes.add_post("/DataHub/DownloadHdfPeriodic", download_hdf)
    app_routes.add_post("/energymanagement/api/consumption/DownloadHdfPeriodic", download_hdf)
    app_routes.add_post("/MeterReadHistory/GetBilledReads", billed_reads)
    for path in (f"/{TENANT}/{POLICY}", f"/{TENANT.lower()}/{POLICY.lower()}"):
        login_routes.add_get(f"{path}/oauth2/v2.0/authorize", authorize)
        login_routes.add_post(f"{path}/SelfAsserted", self_asserted)
        login_routes.add_get(f"{path}/api/CombinedSigninAndSignup/confirmed", confirmed)

    async def dispatch(request: web.Request) -> web.StreamResponse:
        if request.path == "/__mock/state":
            return await mock_state(request)
        if request.path == "/__mock/reset" and request.method == "POST":
            return await mock_reset(request)
        host = request.host.partition(":")[0].lower()
        routes = login_routes if host == LOGIN_HOST else app_routes
        match = await routes.resolve(request)
        if match.http_exception is not None:
            raise match.http_exception
        return await match.handler(request)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", dispatch)
    return app


async def _serve(http_port: int, tls_port: int | None, cert: str | None, key: str | None) -> None:
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", http_port).start()
    if tls_port and cert and key:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(cert, key)
        await web.TCPSite(runner, "0.0.0.0", tls_port, ssl_context=context).start()
    asyncio.get_running_loop().create_task(dataset())  # warm the data up front
    print(f"ESB mock: http :{http_port}" + (f", https :{tls_port}" if tls_port else ""), flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--tls-port", type=int)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    args = parser.parse_args()
    asyncio.run(_serve(args.port, args.tls_port, args.cert, args.key))
