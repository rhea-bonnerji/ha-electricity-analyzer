"""SSE Airtricity (Ireland) home account: read the meter details, submit a reading.

NO HOME ASSISTANT IMPORTS.

NOTHING HERE RUNS ON ITS OWN

A reading is only ever sent from `submit_reading`, which the integration calls from
a button press or an explicitly confirmed service call. There is no schedule, no
retry loop, no "submit when due". SSE accepts one reading per meter per 24 hours;
that is enforced here as well as in the integration, and SSE's own
`isSubmittedInLast24hrs` flag is checked immediately before posting.

WHAT THE PORTAL LOOKS LIKE (from a captured session, 7 October 2026)

* The web app (account.sseairtricity.com) is an Angular build whose public bundle
  carries its configuration: the API gateway, the API Management subscription key
  every request sends as `Ocp-Apim-Subscription-Key`, and the Azure AD B2C tenant,
  client id, policy and scope. All of it is DISCOVERED at runtime from that public
  bundle rather than copied into this file, so a rotated key or a moved gateway
  does not break the integration.
* Sign-in is Azure AD B2C on login.sseairtricity.com using the implicit flow
  (`response_type=id_token token`): after the credential POST, the `confirmed`
  endpoint answers with a 302 to `/signin-oidc#...&access_token=...`. The access
  token is read from that redirect's fragment; it is never followed.
* The account number arrives as the claim `accountNumbersAndRolesCSV` ("1234567D":
  account number plus a role letter), in the token or from the userinfo endpoint.
* Then, against the gateway:
    GET  /premises-details/v2/customer-id/{account}/premises-v2
    GET  /meter-details/v2/premises/{premisesId}/srn-v2
    POST /meter-details/v1/premises/{premisesId}/elec-meter-read
         {"deDate": "07/10/2026", "meterModel": "RM108", "meterNumber": "10000001",
          "meterServiceId": "1",
          "readings": [{"dialsNum": 6, "meterReading": "13876", "registerNum": 1},
                       {"dialsNum": 6, "meterReading": "5660",  "registerNum": 2}]}
         -> 201 {"code": "CREATED", "message": "Meter reads submitted"}
  `deDate`, the model, the meter number, the service number and the dial count all
  come from srn-v2 and are sent back verbatim, which is what the web app does.

The credential step itself was not in the capture (recording began just after it),
so it follows the standard B2C self-asserted journey that ESB Networks also uses and
that ESB's client has verified live. A CAPTCHA is never solved or bypassed: the
client stops and says so.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp

from ..errors import AuthFailed, CaptchaEncountered, PortalError
from ..esb.client import _captcha_reason, _extract_b2c_settings

LOG = logging.getLogger(__name__)

APP_URL = "https://account.sseairtricity.com"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

# Scopes the web app asks for. Only ossAPI is needed for meter reads, but asking for
# exactly what the real client asks for keeps the token identical to a browser's.
_SCOPE_NAMES = (
    "ossAPI",
    "paymentServiceAPI",
    "customerExtractAPI",
    "commAdminAPI",
    "commReportAPI",
    "oss",
    "utilsInvite",
)

_CONFIG_KEYS = {
    "api_url": "APIM_BASE_URL",
    "subscription_key": "ocp_apim_sub_key",
    "login_url": "azureAdBaseUrl",
    "tenant": "azureAdTenant",
    "client_id": "azureAdClientId",
    "policy": "azureAdSignInPolicy",
    "scope": "azureAdScope",
}

_TIMEOUT = aiohttp.ClientTimeout(total=45)


class SubmissionBlocked(PortalError):
    """The reading was not sent, deliberately (cooldown, validation, SSE says no)."""


@dataclass
class PortalConfig:
    app_url: str
    api_url: str
    subscription_key: str
    login_url: str
    tenant: str
    client_id: str
    policy: str
    scope: str

    @property
    def authorize_url(self) -> str:
        return f"{self.login_url}/{self.tenant}/oauth2/v2.0/authorize"

    @property
    def userinfo_url(self) -> str:
        return f"{self.login_url}/{self.tenant}/openid/v2.0/userinfo"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> PortalConfig | None:
        if not d:
            return None
        try:
            return cls(**{k: str(d[k]) for k in ("app_url", *_CONFIG_KEYS)})
        except (KeyError, TypeError):
            return None


@dataclass
class Register:
    number: int
    kind: str              # "day" | "night" | other
    last_value: float | None
    last_date: str | None
    last_status: str | None
    last_inserted: str | None
    last_method: str | None = None   # "C" customer, "A" actual (ESB), "E" estimate
    category: str | None = None      # e.g. "readInLastThirtyDays"

    @property
    def estimated(self) -> bool:
        return is_estimated(self.last_method, self.last_status)


@dataclass
class MeterInfo:
    premises_id: str
    mprn: str
    meter_number: str
    model: str
    service_number: str
    dials: int
    de_date: str
    meter_type: str
    submitted_last_24h: bool
    registers: list[Register] = field(default_factory=list)
    fetched_at: str = ""
    meter_kind: str = ""   # "Legacy" (dials) or a smart meter type

    def register(self, kind: str) -> Register | None:
        return next((r for r in self.registers if r.kind == kind), None)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> MeterInfo | None:
        if not d:
            return None
        regs = [Register(**r) for r in d.get("registers") or []]
        data = {k: v for k, v in d.items() if k != "registers"}
        return cls(registers=regs, **data)


def is_estimated(method: str | None, status: str | None) -> bool:
    """An estimate is SSE's guess, not the meter: never learn from one."""
    return str(method or "").strip().upper() == "E" or "estim" in str(status or "").lower()


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip()) if value not in (None, "") else None
    except ValueError:
        return None


def _euro(value: Any) -> float | None:
    """'€1,234.56' -> 1234.56 (SSE sends money as display strings)."""
    return _num(str(value).replace("€", "")) if value not in (None, "") else None


def history_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """One row of SSE's reading history, normalised. None if it has no date or value."""
    day = _ordinal_date(row.get("readingDate"))
    value = _num(row.get("readingValue"))
    if day is None or value is None:
        return None
    method, status = row.get("readingMethod"), row.get("readingStatus")
    return {
        "date": day,
        "register": str(row.get("meterType") or "").strip().lower(),
        "value": value,
        "status": status,
        "method": method,
        "estimated": is_estimated(method, status),
        "units_used": _num(row.get("unitsUsed")),
    }


def _decode_jwt(token: str) -> dict[str, Any]:
    """Claims from a JWT, unverified. Only used to read the account number."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


def accounts_from_claim(value: str | None) -> list[str]:
    """'1234567D,7654321S' -> ['1234567', '7654321']."""
    out = []
    for part in (value or "").split(","):
        digits = re.match(r"\s*(\d+)", part)
        if digits:
            out.append(digits.group(1))
    return out


def build_payload(meter: MeterInfo, day: int, night: int) -> dict[str, Any]:
    """The exact body the web app posts. Validates before anything is sent."""
    readings = []
    for kind, value in (("day", day), ("night", night)):
        reg = meter.register(kind)
        if reg is None:
            raise SubmissionBlocked(f"the meter has no {kind} register on the SSE account")
        validate_reading(kind, value, meter.dials, reg.last_value, last_date=reg.last_date)
        readings.append(
            {"dialsNum": int(meter.dials), "meterReading": str(int(value)), "registerNum": int(reg.number)}
        )
    readings.sort(key=lambda r: r["registerNum"])
    return {
        "deDate": meter.de_date,
        "meterModel": meter.model,
        "meterNumber": meter.meter_number,
        "meterServiceId": meter.service_number,
        "readings": readings,
    }


# A home on a day/night meter rarely exceeds ~60 kWh a day in total. The cap per
# register is deliberately generous (150 kWh/day) so a genuine reading is never
# refused, while a typo with an extra digit always is.
MAX_KWH_PER_DAY = 150.0
MIN_ALLOWANCE = 50.0


def validate_reading(
    kind: str,
    value: int,
    dials: int,
    last: float | None,
    *,
    last_date: str | None = None,
    today: datetime | None = None,
) -> None:
    """Refuse anything that is not a plausible next reading of this register.

    This is the last line of defence before a number reaches the supplier, and a
    wrong reading becomes a wrong bill, so it errs towards refusing.
    """
    if not isinstance(value, int) or value < 0:
        raise SubmissionBlocked(f"{kind} reading must be a whole, non-negative number")
    if dials and value >= 10**dials:
        raise SubmissionBlocked(f"{kind} reading {value} has more digits than the meter's {dials} dials")
    if last is None:
        return
    if value < int(last):
        raise SubmissionBlocked(
            f"{kind} reading {value} is lower than the last reading SSE holds ({int(last)}); "
            "a meter does not run backwards"
        )
    days = _days_since(last_date, today)
    allowance = MIN_ALLOWANCE + MAX_KWH_PER_DAY * (days if days is not None else 31)
    if value - int(last) > allowance:
        raise SubmissionBlocked(
            f"{kind} reading {value} is {value - int(last)} units above SSE's last reading "
            f"({int(last)}"
            + (f", {days:.0f} days ago" if days is not None else "")
            + f"); more than {allowance:.0f} is not plausible for a home, so it was not sent"
        )


def _days_since(last_date: str | None, today: datetime | None) -> float | None:
    if not last_date:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            then = datetime.strptime(last_date.strip(), fmt)
            break
        except ValueError:
            continue
    else:
        return None
    now = (today or datetime.now(timezone.utc)).replace(tzinfo=None)
    return max((now - then).total_seconds() / 86400, 0.0)

class SseClient:
    """One instance per action. Holds a token in memory only."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        account: str | None = None,
        mprn: str | None = None,
        app_url: str = APP_URL,
        overrides: dict[str, str] | None = None,
        fallback_config: PortalConfig | None = None,
    ) -> None:
        self._http = http
        self._user = username
        self._pass = password
        self.account = (account or "").strip() or None
        self.mprn = (mprn or "").replace(" ", "") or None
        self.app_url = app_url.rstrip("/")
        self._overrides = {k: v for k, v in (overrides or {}).items() if v}
        self._fallback_config = (
            fallback_config if fallback_config and fallback_config.app_url.rstrip("/") == self.app_url else None
        )
        self.config: PortalConfig | None = None
        self._token: str | None = None

    # ------------------------------------------------------------- discovery

    async def discover(self) -> PortalConfig:
        """Read the web app's public configuration out of its own bundle."""
        if self.config:
            return self.config
        try:
            async with self._http.get(
                f"{self.app_url}/", headers={"User-Agent": UA}, timeout=_TIMEOUT
            ) as r:
                if r.status >= 400:
                    raise PortalError(f"SSE's web app ({urlsplit(self.app_url).netloc}) answered HTTP {r.status}")
                html = await r.text()
            found: dict[str, str] = {}
            bundles = re.findall(r"""src=['"]([^'"]*main[.\w-]*\.js(?:\?[^'"]*)?)['"]""", html)
            for src in bundles:
                url = src if src.startswith("http") else f"{self.app_url}/{src.lstrip('/')}"
                async with self._http.get(url, headers={"User-Agent": UA}, timeout=_TIMEOUT) as r:
                    if r.status >= 400:
                        raise PortalError(f"SSE's web bundle answered HTTP {r.status}")
                    js = await r.text()
                for name, key in _CONFIG_KEYS.items():
                    m = re.search(rf"\b{key}\s*:\s*['\"]([^'\"]+)['\"]", js)
                    if m and name not in found:
                        found[name] = m.group(1)
            found.update(self._overrides)
            missing = [k for k in _CONFIG_KEYS if k not in found]
            if missing:
                if not bundles:
                    raise PortalError(
                        "SSE's web app returned no application bundle (maintenance or temporary outage)"
                    )
                raise PortalError(
                    "could not read SSE's portal configuration from its web app "
                    f"(missing {', '.join(missing)}); the site may have changed"
                )
            self.config = PortalConfig(app_url=self.app_url, **{k: found[k].rstrip("/") for k in _CONFIG_KEYS})
        except (PortalError, aiohttp.ClientError, TimeoutError) as exc:
            if self._fallback_config is None:
                raise
            LOG.warning("SSE bundle discovery failed (%s); falling back to cached portal config", exc)
            self.config = self._fallback_config
        LOG.debug("SSE portal: api %s, login %s", self.config.api_url, self.config.login_url)
        return self.config

    # ---------------------------------------------------------------- sign-in

    async def login(self) -> None:
        """Azure AD B2C self-asserted sign-in, implicit flow. Exactly one attempt."""
        cfg = await self.discover()
        LOG.info("SSE sign-in (single attempt)")
        scope = " ".join(["openid", *(f"{cfg.scope}/{s}" for s in _SCOPE_NAMES)])
        state = secrets.token_hex(12)
        params = {
            "p": cfg.policy.lower(),
            "client_id": cfg.client_id,
            "redirect_uri": f"{self.app_url}/signin-oidc",
            "response_type": "id_token token",
            "scope": scope,
            "nonce": secrets.token_hex(12),
            "state": state,
        }
        headers = {"User-Agent": UA}
        async with self._http.get(
            cfg.authorize_url, params=params, headers=headers, timeout=_TIMEOUT
        ) as r:
            page = await r.text()
            page_url = str(r.url)
        if reason := _captcha_reason(page):
            raise CaptchaEncountered(f"SSE is showing a CAPTCHA at sign-in ({reason}); not continuing")
        settings = _extract_b2c_settings(page)
        tenant_path = str((settings.get("hosts") or {}).get("tenant") or "").rstrip("/")
        policy = (settings.get("hosts") or {}).get("policy") or cfg.policy
        base = f"{urlsplit(page_url).scheme}://{urlsplit(page_url).netloc}{tenant_path}"
        field_name = _identifier_field(page)
        origin = f"{urlsplit(page_url).scheme}://{urlsplit(page_url).netloc}"

        async with self._http.post(
            f"{base}/SelfAsserted",
            params={"tx": settings["transId"], "p": policy},
            data={"request_type": "RESPONSE", field_name: self._user, "password": self._pass},
            headers={
                **headers,
                "X-CSRF-TOKEN": settings["csrf"],
                "X-Requested-With": "XMLHttpRequest",
                "Referer": page_url,
                "Origin": origin,
            },
            timeout=_TIMEOUT,
        ) as r:
            body = await r.text()
        if reason := _captcha_reason(body):
            raise CaptchaEncountered(f"SSE is showing a CAPTCHA after the credentials ({reason})")
        try:
            status = json.loads(body)
        except ValueError:
            raise AuthFailed(f"unexpected SSE sign-in response: {body[:160]}") from None
        if str(status.get("status")) != "200":
            raise AuthFailed(f"SSE rejected the sign-in: {status.get('message') or status}")

        async with self._http.get(
            f"{base}/api/CombinedSigninAndSignup/confirmed",
            params={
                "rememberMe": "false",
                "csrf_token": settings["csrf"],
                "tx": settings["transId"],
                "p": policy,
            },
            headers={**headers, "Referer": page_url},
            allow_redirects=False,
            timeout=_TIMEOUT,
        ) as r:
            location = r.headers.get("Location", "")
            if r.status not in (301, 302, 303) or "#" not in location:
                text = await r.text()
                if reason := _captcha_reason(text):
                    raise CaptchaEncountered(f"SSE is showing a CAPTCHA ({reason})")
                raise AuthFailed(f"SSE sign-in did not return a token (HTTP {r.status})")
        fragment = parse_qs(location.split("#", 1)[1])
        if fragment.get("error"):
            raise AuthFailed(f"SSE sign-in failed: {fragment.get('error_description') or fragment['error']}")
        token = (fragment.get("access_token") or [None])[0]
        if not token:
            raise AuthFailed("SSE sign-in redirect carried no access token")
        if (fragment.get("state") or [None])[0] not in (state, None):
            raise AuthFailed("SSE sign-in returned a different state; refusing the token")
        self._token = token
        LOG.info("SSE sign-in OK")

    async def account_number(self) -> str:
        if self._token is None:
            await self.login()
        claims = _decode_jwt(self._token or "")
        accounts = accounts_from_claim(claims.get("accountNumbersAndRolesCSV"))
        if not accounts:
            info = await self._get_json(self.config.userinfo_url, params={"p": self.config.policy.lower()}, api=False)
            accounts = accounts_from_claim(info.get("accountNumbersAndRolesCSV"))
        if self.account:
            if accounts and self.account not in accounts:
                raise AuthFailed(
                    f"account {self.account} is not on this SSE login (it has {', '.join(accounts)})"
                )
            return self.account
        if not accounts:
            raise PortalError("SSE did not say which account this login belongs to; enter the account number")
        return accounts[0]

    # ------------------------------------------------------------------ API

    def _api_headers(self) -> dict[str, str]:
        assert self.config is not None
        return {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
            "Ocp-Apim-Subscription-Key": self.config.subscription_key,
            "Origin": self.app_url,
            "Referer": f"{self.app_url}/",
        }

    async def _get_json(self, url: str, *, params: dict | None = None, api: bool = True) -> Any:
        headers = self._api_headers() if api else {
            "User-Agent": UA,
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        async with self._http.get(url, params=params, headers=headers, timeout=_TIMEOUT) as r:
            if r.status in (401, 403):
                raise AuthFailed(f"SSE refused {urlsplit(url).path} ({r.status})")
            if r.status >= 400:
                raise PortalError(f"SSE {urlsplit(url).path} answered {r.status}: {(await r.text())[:200]}")
            return await r.json(content_type=None)

    async def meter_info(self) -> MeterInfo:
        """Sign in if needed, then find the electricity meter. Read-only."""
        if self._token is None:
            await self.login()
        cfg = self.config
        assert cfg is not None
        account = await self.account_number()
        premises = await self._get_json(f"{cfg.api_url}/premises-details/v2/customer-id/{account}/premises-v2")
        if not isinstance(premises, list) or not premises:
            raise PortalError("the SSE account lists no premises")
        chosen = None
        for p in premises:
            if self.mprn and str(p.get("mprn") or "").strip() == self.mprn:
                chosen = p
                break
        if chosen is None:
            electric = [p for p in premises if p.get("mprn")]
            if self.mprn and electric:
                raise PortalError(f"MPRN {self.mprn} is not on this SSE account")
            chosen = (electric or premises)[0]
        premises_id = str(chosen["premisesId"])

        srns = await self._get_json(f"{cfg.api_url}/meter-details/v2/premises/{premises_id}/srn-v2")
        srn = next((s for s in srns or [] if str(s.get("utilityType", "")).upper() == "ELECTRICITY"), None)
        if not srn or not srn.get("meters"):
            raise PortalError("no electricity meter found on the SSE premises")
        if srn.get("prePaidMeter"):
            raise PortalError("this is a pay-as-you-go meter; readings are not submitted this way")
        meter = srn["meters"][0]
        registers = []
        for reg in meter.get("registers") or []:
            kind = str(reg.get("registerType") or "").strip().lower()
            try:
                value = float(reg.get("lastReadingValue")) if reg.get("lastReadingValue") not in (None, "") else None
            except (TypeError, ValueError):
                value = None
            registers.append(
                Register(
                    number=int(reg.get("number")),
                    kind=kind,
                    last_value=value,
                    last_date=reg.get("lastReadingDate"),
                    last_status=reg.get("lastReadingStatus"),
                    last_inserted=reg.get("latestOssReadInsertDate") or None,
                    last_method=reg.get("lastReadingMethod") or None,
                    category=reg.get("readCategory") or None,
                )
            )
        return MeterInfo(
            premises_id=premises_id,
            mprn=str(srn.get("srn") or chosen.get("mprn") or ""),
            meter_number=str(meter.get("number") or "").strip(),
            model=str(meter.get("model") or ""),
            service_number=str(meter.get("servicenum") or "1"),
            dials=int(meter.get("dialsNumber") or 0),
            de_date=str(meter.get("deDate") or datetime.now(timezone.utc).strftime("%d/%m/%Y")),
            meter_type=str(meter.get("type") or srn.get("eMeterType") or ""),
            submitted_last_24h=bool(meter.get("isSubmittedInLast24hrs")),
            registers=registers,
            fetched_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            meter_kind=str(srn.get("eMeterType") or chosen.get("eMeterType") or ""),
        )

    async def reading_history(self, premises_id: str) -> list[dict[str, Any]]:
        """Every reading SSE holds for the electricity meter, newest first. Read-only.

        The web app asks for dial ("Legacy") and smart non-interval readings
        separately; a meter only ever has rows in one of them.
        """
        if self._token is None:
            await self.login()
        cfg = self.config
        assert cfg is not None
        account = await self.account_number()
        rows: list[dict[str, Any]] = []
        for kind in ("Legacy", "SmartNonInterval"):
            url = (
                f"{cfg.api_url}/meter-details/v2/premises/{premises_id}"
                f"/customer-id/{account}/meter-read-history-elec-v2/{kind}"
            )
            rows += [r for row in await self._get_json(url) or [] if (r := history_row(row))]
        rows.sort(key=lambda r: (r["date"], r["register"]), reverse=True)
        return rows

    async def billing_info(self) -> dict[str, Any]:
        """Read-only: billing dates, contract, latest bill (when there is one)."""
        if self._token is None:
            await self.login()
        cfg = self.config
        assert cfg is not None
        account = await self.account_number()
        out: dict[str, Any] = {"fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
        try:
            billing = await self._get_json(f"{cfg.api_url}/billing-details/v1/customers/{account}/billing")
            out["next_bill_date"] = billing.get("nextBillDate")
            out["balance"] = billing.get("currentBalance")
            out["balance_eur"] = _euro(billing.get("currentBalance"))
            out["balance_status"] = billing.get("balanceStatus")
            out["overdue_eur"] = _euro(billing.get("overdueBalance"))
            out["direct_debit"] = billing.get("directDebit")
            out["payment_due"] = _ordinal_date(billing.get("latestBillPaymentDueDate"))
            out["earliest_unpaid_due"] = _ordinal_date(billing.get("earliestUnpaidDueDate"))
            out["last_payment"] = _ordinal_date(billing.get("previousPaymentDate"))
        except PortalError as exc:
            LOG.debug("SSE billing details unavailable: %s", exc)
        try:
            services = await self._get_json(f"{cfg.api_url}/account-details/v2/{account}/services-v2")
            elec = next((s.get("electricUtility") for s in services or [] if s.get("electricUtility")), None)
            if elec and elec.get("utlityStartDate"):
                out["supply_start"] = _ordinal_date(elec["utlityStartDate"])
            if elec:
                out["supply_status"] = elec.get("utilityStatus")
                out["transfer_in_progress"] = elec.get("transferInProgress")
        except PortalError as exc:
            LOG.debug("SSE services unavailable: %s", exc)
        try:
            contracts = await self._get_json(f"{cfg.api_url}/premises-details/v1/customer-id/{account}/contracts")
            for premises in contracts or []:
                for c in premises.get("elec") or []:
                    if c.get("type") == "current":
                        out["contract_start"] = _ordinal_date(c.get("start"))
                        out["contract_end"] = _ordinal_date(c.get("end"))
                        out["plan"] = c.get("plantype")
                        out["standard_tariff"] = str(c.get("standardtariff") or "").upper() == "Y"
        except PortalError as exc:
            LOG.debug("SSE contracts unavailable: %s", exc)
        raw_latest: dict[str, Any] | None = None
        try:
            raw_latest = await self._get_json(f"{cfg.api_url}/billing-details/v2/customers/{account}/latest-bill-v2")
        except PortalError:
            raw_latest = None  # SSE answers 404 until the first bill
        try:
            history = await self._get_json(f"{cfg.api_url}/billing-details/v1/customers/{account}/transaction-history")
            out["transactions"] = [
                clean_transaction(t)
                for t in ((history or {}).get("billingTransactionCollection") or [])[:50]
                if isinstance(t, dict)
            ]
        except PortalError as exc:
            LOG.debug("SSE transaction history unavailable: %s", exc)
        latest_norm = normalise_latest_bill(raw_latest, out.get("transactions"))
        out["latest_bill"] = latest_norm
        if latest_norm:
            if latest_norm.get("bill_date"):
                out["last_bill_date"] = latest_norm["bill_date"]
            if latest_norm.get("period_end"):
                out["last_bill_end"] = latest_norm["period_end"]
        return out

    async def submit_reading(self, day: int, night: int) -> tuple[dict[str, Any], dict[str, Any], MeterInfo]:
        """POST one reading. Returns (payload sent, SSE's answer, meter info after)."""
        meter = await self.meter_info()
        if meter.submitted_last_24h:
            raise SubmissionBlocked("SSE already has a reading for this meter from the last 24 hours")
        payload = build_payload(meter, day, night)
        cfg = self.config
        assert cfg is not None
        url = f"{cfg.api_url}/meter-details/v1/premises/{meter.premises_id}/elec-meter-read"
        async with self._http.post(url, json=payload, headers=self._api_headers(), timeout=_TIMEOUT) as r:
            text = await r.text()
            try:
                answer = json.loads(text) if text else {}
            except ValueError:
                answer = {"raw": text[:500]}
            if r.status in (401, 403):
                raise AuthFailed(f"SSE refused the reading ({r.status})")
            if r.status >= 400:
                raise PortalError(f"SSE did not accept the reading (HTTP {r.status}): {answer}")
        after = await self.meter_info()
        return payload, answer, after


def clean_transaction(tx: dict[str, Any]) -> dict[str, Any]:
    """Keep financial fields from a transaction row without account/premises identifiers."""
    return {
        "transactionDate": _ordinal_date(tx.get("transactionDate")) or tx.get("transactionDate"),
        "transactionDescription": tx.get("transactionDescription"),
        "utilityType": tx.get("utilityType"),
        "transactionAmount": tx.get("transactionAmount"),
        "amount_eur": _euro(tx.get("transactionAmount")),
        "transactionStatus": tx.get("transactionStatus"),
        "reversed": bool(tx.get("reversed")),
    }


def normalise_latest_bill(
    latest: dict[str, Any] | None,
    transactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Extract dates and euro totals from SSE's latest-bill-v2 (or Bill transactions), PII-free."""
    bill_txs = [
        tx
        for tx in (transactions or [])
        if isinstance(tx, dict)
        and not tx.get("reversed")
        and str(tx.get("transactionDescription") or "").strip().lower() == "bill"
        and str(tx.get("utilityType") or "E").strip().upper() in ("", "E", "ELECTRICITY")
    ]
    if not latest and not bill_txs:
        return None
    latest = latest or {}
    # Support already-normalised dicts (e.g. re-read from store) alongside raw portal payloads.
    if "bill_date" in latest or "electricity_eur" in latest:
        return {
            "bill_date": _ordinal_date(latest.get("bill_date")),
            "period_start": _ordinal_date(latest.get("period_start")),
            "period_end": _ordinal_date(latest.get("period_end")),
            "electricity_eur": _num(latest.get("electricity_eur")),
            "gas_eur": _num(latest.get("gas_eur")),
            "invoice_eur": _num(latest.get("invoice_eur")),
            "balance_after_eur": _num(latest.get("balance_after_eur")),
            "balance_after_status": latest.get("balance_after_status"),
            "previous_balance_eur": _num(latest.get("previous_balance_eur")),
            "previous_balance_status": latest.get("previous_balance_status"),
        }

    bill_date = _ordinal_date(latest.get("billDate") or latest.get("issueDate") or latest.get("statementDate"))
    if bill_date is None and bill_txs:
        dates = sorted((d for tx in bill_txs if (d := _ordinal_date(tx.get("transactionDate")))), reverse=True)
        bill_date = dates[0] if dates else None

    period_start = None
    for key in ("billFromDate", "billStartDate", "periodStartDate", "fromDate", "startDate"):
        if latest.get(key):
            period_start = _ordinal_date(latest[key])
            if period_start:
                break

    period_end = None
    for key in ("billToDate", "billEndDate", "periodEndDate", "toDate", "endDate"):
        if latest.get(key):
            period_end = _ordinal_date(latest[key])
            if period_end:
                break

    summaries = [s for s in (latest.get("premisesBillSummary") or []) if isinstance(s, dict)]
    elec_vals = [v for s in summaries if (v := _euro(s.get("totalElectricityInvoice"))) is not None]
    gas_vals = [v for s in summaries if (v := _euro(s.get("totalGasInvoice"))) is not None]
    inv_vals = [v for s in summaries if (v := _euro(s.get("totalAllInvoices"))) is not None]
    electricity_eur = round(sum(elec_vals), 2) if elec_vals else None
    if electricity_eur is None and bill_txs:
        tx_match = next(
            (tx for tx in bill_txs if _ordinal_date(tx.get("transactionDate")) == bill_date),
            bill_txs[0],
        )
        electricity_eur = _num(tx_match.get("amount_eur"))
        if electricity_eur is None:
            electricity_eur = _euro(tx_match.get("transactionAmount"))

    if bill_date is None and period_end is None and electricity_eur is None:
        return None

    return {
        "bill_date": bill_date,
        "period_start": period_start,
        "period_end": period_end,
        "electricity_eur": electricity_eur,
        "gas_eur": round(sum(gas_vals), 2) if gas_vals else None,
        "invoice_eur": round(sum(inv_vals), 2) if inv_vals else electricity_eur,
        "balance_after_eur": _euro(latest.get("totalBillAmount")),
        "balance_after_status": latest.get("billAmountStatus"),
        "previous_balance_eur": _euro(latest.get("previousBalance")),
        "previous_balance_status": latest.get("previousBalanceStatus"),
    }


def sanitize_billing(billing: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalise an sse_billing dict (including pre-1.0.4 cached copies) and strip PII."""
    if not billing:
        return None
    out = dict(billing)
    txs = [
        clean_transaction(t) if "premiseId" in t or "statementNumber" in t else t
        for t in (out.get("transactions") or [])
        if isinstance(t, dict)
    ]
    if "transactions" in out:
        out["transactions"] = txs
    norm = normalise_latest_bill(out.get("latest_bill"), txs)
    out["latest_bill"] = norm
    if norm:
        if norm.get("bill_date") and not out.get("last_bill_date"):
            out["last_bill_date"] = norm["bill_date"]
        if norm.get("period_end") and not out.get("last_bill_end"):
            out["last_bill_end"] = norm["period_end"]
    return out


def _ordinal_date(text: Any) -> str | None:
    """'27th Sep 2026', '27 September 2026', '2026-09-27', '27/09/2026' -> ISO date."""
    if not text:
        return None
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", str(text).strip())
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%d-%b-%y"):
        try:
            return datetime.strptime(cleaned[:len(cleaned)], fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned).date().isoformat()
    except ValueError:
        return None


def _identifier_field(page: str) -> str:
    """Which form field B2C wants the username in (signInName on most policies)."""
    for candidate in ("signInName", "logonIdentifier", "email"):
        if re.search(rf'"ID"\s*:\s*"{candidate}"', page) or f'id="{candidate}"' in page:
            return candidate
    return "signInName"

