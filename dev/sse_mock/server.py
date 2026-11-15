#!/usr/bin/env python3
"""A local stand-in for the SSE Airtricity portal. DEV AND TESTS ONLY.

Mirrors the pieces of the real portal the integration touches, shaped exactly like
the captured traffic (7 October 2026), with de-identified values:

  GET  /                                   web app shell, links main.<hash>.bundle.js
  GET  /main.mock.bundle.js                public config: API gateway, subscription key, B2C
  GET  /login/<tenant>/oauth2/v2.0/authorize               B2C page with var SETTINGS
  POST /login/<tenant>/<policy>/SelfAsserted               credentials -> {"status": "200"}
  GET  /login/<tenant>/<policy>/api/CombinedSigninAndSignup/confirmed
                                           302 -> /signin-oidc#access_token=...
  GET  /login/<tenant>/openid/v2.0/userinfo
  GET  /api/premises-details/v2/customer-id/<acct>/premises-v2
  GET  /api/meter-details/v2/premises/<id>/srn-v2
  POST /api/meter-details/v1/premises/<id>/elec-meter-read  -> 201 CREATED

Credentials: any username, password "z" (the "x / z" nonsense pair). Anything else is
rejected the way B2C rejects it, so the failure path can be exercised too.

Inspection for the verifier:  GET /__mock/state   POST /__mock/reset

URLs in the bundle are built from the request's Host header, so the same server works
as http://localhost:8090 from a laptop and as https://account.sseairtricity.com inside
the sandbox's Docker network (see dev/docker-compose.yml and dev/devcerts.py).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import ssl
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from aiohttp import web

PASSWORD = os.environ.get("SSE_MOCK_PASSWORD", "z")
SUB_KEY = "0123456789abcdef0123456789abcdef"
TENANT = "00000000-mock-tenant-0000-000000000000"
POLICY = "B2C_1A_SIGNUPORSIGNINV2-PRD"
CLIENT_ID = "11111111-mock-client-1111-111111111111"
ACCOUNT = "1234567"
PREMISES = "100001"
MPRN = "10000000001"
METER = "10000001"


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _fresh_state() -> dict:
    return {
        "tokens": set(),
        "transactions": {},
        "submissions": [],
        "registers": {
            1: {"value": "13708", "date": "2026-09-27 00:00:00", "status": None, "inserted": ""},
            2: {"value": "5582", "date": "2026-09-27 00:00:00", "status": None, "inserted": ""},
        },
        "last_submit": None,
        "logins": {"ok": 0, "rejected": 0},
    }


STATE = _fresh_state()


def _base(request: web.Request) -> str:
    return f"{request.scheme}://{request.host}"


async def shell(request: web.Request) -> web.Response:
    return web.Response(
        text='<!doctype html><html><head><title>SSE Airtricity - Online Self Service (MOCK)</title>'
        '</head><body><app-root></app-root>'
        '<script type="text/javascript" src="inline.mock.bundle.js"></script>'
        '<script type="text/javascript" src="main.mock.bundle.js"></script></body></html>',
        content_type="text/html",
    )


async def bundle(request: web.Request) -> web.Response:
    base = _base(request)
    config = (
        'var o={production:!0,APIM_BASE_URL:"%s/api",apiSrc:"frontend",ocp_apim_sub_key:"%s",'
        'app_base_url:"%s",azureAdBaseUrl:"%s/login",azureAdTenant:"%s",azureAdClientId:"%s",'
        'azureAdSignInPolicy:"%s",azureAdScope:"https://mock.onmicrosoft.com/ossapi"};'
    ) % (base, SUB_KEY, base, base, TENANT, CLIENT_ID, POLICY)
    return web.Response(text=config, content_type="application/javascript")


async def authorize(request: web.Request) -> web.Response:
    q = request.query
    if q.get("client_id") != CLIENT_ID or "token" not in q.get("response_type", ""):
        return web.Response(status=400, text="AADB2C90079: bad client or response type")
    tx = "StateProperties=" + secrets.token_urlsafe(16)
    csrf = secrets.token_urlsafe(24)
    STATE["transactions"][tx] = {
        "csrf": csrf,
        "redirect_uri": q.get("redirect_uri"),
        "state": q.get("state"),
        "authenticated": False,
    }
    settings = {
        "remoteResource": f"{_base(request)}/static/selfAsserted.html",
        "csrf": csrf,
        "transId": tx,
        "api": "CombinedSigninAndSignup",
        "hosts": {"tenant": f"/login/{TENANT}/{POLICY}", "policy": POLICY, "static": "/static/"},
    }
    html = (
        "<!DOCTYPE html><html><head><title>Sign in (MOCK)</title></head><body>"
        f"<script>var SETTINGS = {json.dumps(settings)};"
        'var SA_FIELDS = {"AttributeFields":[{"ID":"signInName","IS_REQ":true},'
        '{"ID":"password","IS_REQ":true}]};</script></body></html>'
    )
    return web.Response(text=html, content_type="text/html")


async def self_asserted(request: web.Request) -> web.Response:
    tx = request.query.get("tx", "")
    txn = STATE["transactions"].get(tx)
    form = await request.post()
    if not txn or request.headers.get("X-CSRF-TOKEN") != txn["csrf"]:
        return web.json_response({"status": "400", "message": "Your session has expired."})
    if not form.get("signInName") or form.get("password") != PASSWORD:
        STATE["logins"]["rejected"] += 1
        return web.json_response({"status": "400", "errorCode": "AADB2C90225", "message": "Your password is incorrect."})
    txn["authenticated"] = True
    txn["user"] = str(form["signInName"])
    STATE["logins"]["ok"] += 1
    return web.json_response({"status": "200"})


async def confirmed(request: web.Request) -> web.Response:
    tx = request.query.get("tx", "")
    txn = STATE["transactions"].pop(tx, None)
    if not txn or not txn["authenticated"] or request.query.get("csrf_token") != txn["csrf"]:
        return web.Response(status=400, text="AADB2C90118: transaction not authenticated")
    now = int(datetime.now(timezone.utc).timestamp())
    token = ".".join(
        [
            _b64({"alg": "RS256", "typ": "JWT", "kid": "mock"}),
            _b64({"sub": "mock-user", "exp": now + 3600, "accountNumbersAndRolesCSV": f"{ACCOUNT}D"}),
            secrets.token_urlsafe(32),
        ]
    )
    STATE["tokens"].add(token)
    fragment = urlencode(
        {"state": txn["state"] or "", "access_token": token, "token_type": "Bearer", "expires_in": "3600"}
    )
    raise web.HTTPFound(location=f"{txn['redirect_uri']}#{fragment}")


async def userinfo(request: web.Request) -> web.Response:
    if not _authorised(request, api=False):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({"sub": "mock-user", "accountNumbersAndRolesCSV": f"{ACCOUNT}D"})


def _authorised(request: web.Request, *, api: bool = True) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] not in STATE["tokens"]:
        return False
    if api and request.headers.get("Ocp-Apim-Subscription-Key") != SUB_KEY:
        return False
    return True


def _recent_submission() -> bool:
    last = STATE["last_submit"]
    return last is not None and datetime.now(timezone.utc) - last < timedelta(hours=24)


async def premises(request: web.Request) -> web.Response:
    if not _authorised(request):
        return web.json_response({"statusCode": 401, "message": "Access denied"}, status=401)
    if request.match_info["account"] != ACCOUNT:
        return web.json_response({"message": "not your account"}, status=403)
    return web.json_response(
        [
            {
                "premisesId": PREMISES,
                "address": {"addressLine1": "1 MOCK STREET", "town": "DUBLIN", "country": "Republic_of_Ireland"},
                "mprn": MPRN,
                "electicityMeterId": f"{METER}    ",
                "eMeterType": "Legacy",
                "eMeterTypeSince": "2026-09-27",
                "interval": False,
            }
        ]
    )


async def srn(request: web.Request) -> web.Response:
    if not _authorised(request):
        return web.json_response({"statusCode": 401, "message": "Access denied"}, status=401)
    regs = STATE["registers"]
    return web.json_response(
        [
            {
                "srn": MPRN,
                "utilityType": "ELECTRICITY",
                "prePaidMeter": False,
                "meters": [
                    {
                        "number": METER,
                        "model": "RM108",
                        "type": "Day/Night",
                        "servicenum": "1",
                        "dialsNumber": "6",
                        "deDate": datetime.now().strftime("%d/%m/%Y"),
                        "isSubmittedInLast24hrs": _recent_submission(),
                        "registers": [
                            {
                                "number": n,
                                "registerType": "Day" if n == 1 else "Night",
                                "lastReadingUnits": "kWh",
                                "lastReadingDate": r["date"],
                                "lastReadingMethod": "C" if r["status"] else None,
                                "lastReadingValue": r["value"],
                                "latestOssReadInsertDate": r["inserted"],
                                "lastReadingStatus": r["status"],
                            }
                            for n, r in sorted(regs.items())
                        ],
                    }
                ],
                "eMeterType": "Legacy",
            }
        ]
    )


async def submit(request: web.Request) -> web.Response:
    if not _authorised(request):
        return web.json_response({"statusCode": 401, "message": "Access denied"}, status=401)
    body = await request.json()
    expected = {"deDate", "meterModel", "meterNumber", "meterServiceId", "readings"}
    if set(body) != expected or body["meterNumber"] != METER or body["meterServiceId"] != "1":
        return web.json_response({"code": "BAD_REQUEST", "message": f"unexpected body {sorted(body)}"}, status=400)
    if _recent_submission():
        return web.json_response(
            {"code": "BAD_REQUEST", "message": "A meter reading was already submitted in the last 24 hours"},
            status=400,
        )
    now = datetime.now()
    for reading in body["readings"]:
        if set(reading) != {"dialsNum", "meterReading", "registerNum"} or reading["dialsNum"] != 6:
            return web.json_response({"code": "BAD_REQUEST", "message": "bad reading"}, status=400)
        reg = STATE["registers"][int(reading["registerNum"])]
        reg.update(
            value=str(reading["meterReading"]),
            date=now.strftime("%d/%m/%Y"),
            status="Pending",
            inserted=now.strftime("%d/%m/%Y %H:%M:%S"),
        )
    STATE["last_submit"] = datetime.now(timezone.utc)
    STATE["submissions"].append({"at": STATE["last_submit"].isoformat(), "body": body})
    return web.json_response({"code": "CREATED", "message": "Meter reads submitted"}, status=201)


# The reads SSE took the account over with, as its reading history lists them.
OPENING_READS = {1: ("13708", "Day"), 2: ("5582", "Night")}


async def read_history(request: web.Request) -> web.Response:
    """meter-read-history-elec-v2/{Legacy|SmartNonInterval}: newest first, as captured."""
    if not _authorised(request):
        return web.json_response({"statusCode": 401}, status=401)
    if request.match_info["kind"] != "Legacy":
        return web.json_response([])
    rows = []
    for n, r in sorted(STATE["registers"].items()):
        if r["status"]:
            used = int(r["value"]) - int(OPENING_READS[n][0])
            rows.append({"readingDate": r["date"], "readingValue": r["value"], "readingStatus": r["status"],
                         "meterType": OPENING_READS[n][1], "meterNumber": METER, "readingMethod": "C",
                         "unitsUsed": str(used), "meterMultiplier": "1", "energyUsage": str(used),
                         "energyUsageUnits": "kWh", "eMeterType": "Legacy"})
    for n, (value, kind) in sorted(OPENING_READS.items()):
        rows.append({"readingDate": "27/09/2026", "readingValue": value, "readingStatus": "Accepted",
                     "meterType": kind, "meterNumber": METER, "readingMethod": "A", "unitsUsed": "0",
                     "meterMultiplier": "1", "energyUsage": "0", "energyUsageUnits": "kWh", "eMeterType": "Legacy"})
    return web.json_response(rows)


async def billing(request: web.Request) -> web.Response:
    if not _authorised(request):
        return web.json_response({"statusCode": 401}, status=401)
    return web.json_response({"currentBalance": "€0.00", "overdueBalance": "€0.00", "balanceStatus": "CREDIT",
                              "directDebit": True, "earliestUnpaidDueDate": None, "previousPaymentDate": None,
                              "latestBillPaymentDueDate": None, "nextBillDate": "2026-11-08",
                              "amountofCreditDays": None})


async def transactions(request: web.Request) -> web.Response:
    if not _authorised(request):
        return web.json_response({"statusCode": 401}, status=401)
    return web.json_response({"billingTransactionCollection": []})


async def services(request: web.Request) -> web.Response:
    if not _authorised(request):
        return web.json_response({"statusCode": 401}, status=401)
    return web.json_response([{"premnum": PREMISES, "electricUtility": {"utlityStartDate": "27th Sep 2026",
                               "utilityStatus": "Current", "current": True, "prePaidMeter": False}, "mprn": MPRN}])


async def contracts(request: web.Request) -> web.Response:
    if not _authorised(request):
        return web.json_response({"statusCode": 401}, status=401)
    return web.json_response([{"premiseId": int(PREMISES), "elec": [{"start": "27 September 2026",
                               "end": "26 September 2027", "type": "current", "standardtariff": "N"}], "gas": []}])


async def latest_bill(request: web.Request) -> web.Response:
    if not _authorised(request):
        return web.json_response({"statusCode": 401}, status=401)
    return web.json_response({"status": 404, "detail": "NoFinancialHistoryFoundException"}, status=404)


async def mock_state(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "submissions": STATE["submissions"],
            "registers": STATE["registers"],
            "logins": STATE["logins"],
            "submitted_last_24h": _recent_submission(),
        }
    )


async def mock_reset(request: web.Request) -> web.Response:
    STATE.clear()
    STATE.update(_fresh_state())
    return web.json_response({"reset": True})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", shell)
    app.router.add_get("/signin-oidc", shell)
    app.router.add_get("/main.mock.bundle.js", bundle)
    app.router.add_get(f"/login/{TENANT}/oauth2/v2.0/authorize", authorize)
    app.router.add_post(f"/login/{TENANT}/{POLICY}/SelfAsserted", self_asserted)
    app.router.add_get(f"/login/{TENANT}/{POLICY}/api/CombinedSigninAndSignup/confirmed", confirmed)
    app.router.add_get(f"/login/{TENANT}/openid/v2.0/userinfo", userinfo)
    app.router.add_get("/api/premises-details/v2/customer-id/{account}/premises-v2", premises)
    app.router.add_get("/api/meter-details/v2/premises/{premises}/srn-v2", srn)
    app.router.add_post("/api/meter-details/v1/premises/{premises}/elec-meter-read", submit)
    app.router.add_get("/api/billing-details/v1/customers/{account}/billing", billing)
    app.router.add_get("/api/billing-details/v1/customers/{account}/transaction-history", transactions)
    app.router.add_get(
        "/api/meter-details/v2/premises/{premises}/customer-id/{account}/meter-read-history-elec-v2/{kind}", read_history
    )
    app.router.add_get("/api/account-details/v2/{account}/services-v2", services)
    app.router.add_get("/api/premises-details/v1/customer-id/{account}/contracts", contracts)
    app.router.add_get("/api/billing-details/v2/customers/{account}/latest-bill-v2", latest_bill)
    app.router.add_get("/__mock/state", mock_state)
    app.router.add_post("/__mock/reset", mock_reset)
    return app


async def _serve(http_port: int, tls_port: int | None, cert: str | None, key: str | None) -> None:
    """Plain HTTP for a laptop, and HTTPS under the real name inside the sandbox."""
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", http_port).start()
    if tls_port and cert and key:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(cert, key)
        await web.TCPSite(runner, "0.0.0.0", tls_port, ssl_context=context).start()
    print(f"SSE mock: http :{http_port}" + (f", https :{tls_port}" if tls_port else ""), flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--tls-port", type=int)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    args = parser.parse_args()
    asyncio.run(_serve(args.port, args.tls_port, args.cert, args.key))
