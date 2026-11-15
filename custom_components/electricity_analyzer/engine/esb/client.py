"""ESB Networks (myaccount.esbnetworks.ie) client.

Source of truth for interval usage. Three things shape this design:

* `DownloadHdfPeriodic` returns the FULL interval history in a single request,
  so there is nothing to gain from frequent polling and a missed run heals
  itself on the next one.
* Sign-in is gated by a CAPTCHA after repeated failures per day. We make one
  attempt, persist the session cookie, and stop on failure. No retry loops,
  and no attempt to solve or sidestep a CAPTCHA.
* Every mutating POST needs an antiforgery token from `GET /af/t`, sent back as
  the `x-xsrf-token` header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from bs4 import BeautifulSoup
from yarl import URL

from .. import session as session_state
from .constants import DEFAULT_COLE_DATE, ESB_BASE
from ..errors import AuthFailed, CaptchaEncountered, PortalError, RateLimited, SessionExpired

LOG = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

# CAPTCHA detection, structural rather than by substring.
#
# Bare substring matching does not work on these pages. The B2C sign-in page is
# 165 KB and the post-credential page 210 KB, both dominated by a JS bundle and
# an embedded field-definition blob. That blob legitimately contains the string
# "g-recaptcha-response-toms" as a *field id* on pages where no challenge is
# shown, so testing for "captcha" anywhere in the HTML reports a CAPTCHA on a
# perfectly ordinary response, and the real cause of a failure gets buried.
#
# What actually identifies the CAPTCHA step is the page B2C says it is rendering:
# `SETTINGS.remoteResource` points at `b2cpages/captcha.html`. Failing that, a
# required g-recaptcha field or a genuine challenge widget is conclusive.
_CAPTCHA_REMOTE_RESOURCE = re.compile(r'"remoteResource"\s*:\s*"[^"]*captcha\.html"', re.I)
_CAPTCHA_REQUIRED_FIELD = re.compile(
    r'"ID"\s*:\s*"g-recaptcha-response[^"]*"[^}]*?"IS_REQ"\s*:\s*true', re.I | re.S
)
_CAPTCHA_WIDGETS = (
    "data-sitekey",
    "recaptcha/api.js",
    "www.google.com/recaptcha",
    "hcaptcha.com/1/api.js",
    "cf-turnstile",
)
# Phrases specific enough not to appear in a framework bundle.
_CAPTCHA_PHRASES = (
    "are you a robot",
    "verify you are human",
    "unusual activity",
)


def _captcha_reason(html: str) -> str | None:
    """Why this page looks like a CAPTCHA challenge, or None if it does not."""
    if not html:
        return None
    if _CAPTCHA_REMOTE_RESOURCE.search(html):
        return "B2C is rendering its captcha.html page"
    if _CAPTCHA_REQUIRED_FIELD.search(html):
        return "the page requires a g-recaptcha response field"
    low = html.lower()
    for widget in _CAPTCHA_WIDGETS:
        if widget in low:
            return f"the page loads a challenge widget ({widget})"
    for phrase in _CAPTCHA_PHRASES:
        if phrase in low:
            return f"the page says {phrase!r}"
    return None

# ESB fronts Azure AD B2C on its OWN domain, login.esbnetworks.ie, rather than
# on *.b2clogin.com. Testing for "b2clogin" alone therefore never matches, and
# because the login host still ends in esbnetworks.ie a naive
# "is it still on esbnetworks.ie?" check reports the sign-in form as a live
# session. Match on the host and the B2C path shape instead.
LOGIN_URL_MARKERS = (
    "login.esbnetworks.ie",
    "b2clogin.com",
    "/oauth2/v2.0/authorize",
    "/b2c_1a_",
)

# The host that serves actual account data. A probe that ends anywhere else has
# been bounced.
APP_HOST = "myaccount.esbnetworks.ie"

# Domains whose cookies belong to this portal.
SESSION_DOMAINS = ("esbnetworks.ie", "b2clogin.com")


def _is_login_url(url: str) -> bool:
    low = (url or "").lower()
    return any(marker in low for marker in LOGIN_URL_MARKERS)


def new_cookie_jar() -> aiohttp.CookieJar:
    """A cookie jar ESB's Azure AD B2C will actually accept.

    THIS IS LOAD-BEARING. Azure AD B2C sets cookies whose *names* contain
    characters that RFC 6265 does not allow in a token::

        x-ms-cpim-cache|gccqdo_ujk2zr7jqtrgzma_0
        x-ms-cpim-sso:esbntwkscustportalprdb2c01.onmicrosoft.com_0

    aiohttp defaults to ``quote_cookie=True``, which quotes such cookies on the
    way back out. B2C cannot parse the result and answers the credential POST
    with a bare ``400 Bad Request`` and no body — no error code, no JSON, nothing
    that hints at cookies. That is indistinguishable from "the sign-in flow
    changed", which is exactly how it was misdiagnosed before being measured:
    every field-name and header permutation failed identically, and the single
    switch below fixed all of them.

    Consequence for Home Assistant: the ESB client must NOT be handed the shared
    ``async_get_clientsession`` session, because its jar is not built this way.
    It needs its own session. See ``coordinator._esb_session``.
    """
    return aiohttp.CookieJar(quote_cookie=False)

# Politeness delay between consecutive portal calls. Nothing here is latency
# sensitive and the data is a day old anyway.
_CALL_SPACING = 0.4


HISTORIC_PATH: Final = "energymanagement/historic-consumption"


class EsbClient:
    """Async ESB Networks client.

    `session_state` is an opaque dict of cookies owned by the caller (the HA
    config entry, or a file in the dev harness) so a warm session survives
    restarts and we almost never touch the login form.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        mprn: str,
        cole_date: str = DEFAULT_COLE_DATE,
    ) -> None:
        self._http = session
        self._user = username
        self._pass = password
        self.mprn = mprn
        self.cole_date = cole_date

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Per-request headers, always carrying a browser User-Agent.

        Azure AD B2C is the most UA-sensitive of the three portals, and the
        session is shared with the rest of Home Assistant so a session-level
        default header is not an option.
        """
        headers = {"User-Agent": UA}
        if extra:
            headers.update(extra)
        return headers

    # ------------------------------------------------------------ session

    def export_session(self) -> dict[str, str]:
        """Serialisable session state, for persisting between runs."""
        return session_state.export_cookies(self._http.cookie_jar, SESSION_DOMAINS)

    def restore_session(self, cookies: dict[str, str]) -> int:
        """Put a previously saved, or human-supplied, session back into the jar.

        Scoped to the app host. B2C's own cookies live on login.esbnetworks.ie and
        are not needed to *use* a session, only to establish one, so a restored
        session is expected to be just the app cookie.
        """
        if not cookies:
            return 0
        self._http.cookie_jar.update_cookies(cookies, response_url=URL(ESB_BASE))
        LOG.debug("restored ESB session: %s", session_state.describe(cookies))
        return len(cookies)

    async def is_authenticated(self) -> bool:
        """True with a live session; False only when ESB sends us to sign in.

        The data page returns 200 with a live session and bounces to the sign-in page
        without one. Anything else (ESB down for maintenance, a 5xx, rate limiting, a
        network error) RAISES instead of answering False: none of those says the
        session is dead, and treating them as if they did spends a fresh sign-in on an
        outage, which is exactly what brings on ESB's CAPTCHA.
        """
        try:
            async with self._http.get(
                f"{ESB_BASE}/{HISTORIC_PATH}",
                allow_redirects=True,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                url = str(r.url)
                host = (r.url.host or "").lower()
                if _is_login_url(url) or r.status in (401, 403):
                    live = False
                elif r.status == 200 and host == APP_HOST:
                    live = True
                elif r.status == 429:
                    raise RateLimited("ESB asked to slow down (HTTP 429); trying again later")
                else:
                    raise PortalError(f"ESB answered HTTP {r.status} ({host}); not treating that as a dead session")
                LOG.debug("ESB session probe: %s (%s, %s)", "live" if live else "dead", r.status, host)
                return live
        except aiohttp.ClientError as exc:
            raise PortalError(f"ESB could not be reached: {exc or type(exc).__name__}") from exc

    async def ensure_session(self) -> None:
        if await self.is_authenticated():
            return
        await self.login()

    def _clear_portal_cookies(self) -> None:
        """Drop only this portal's cookies, never the whole jar.

        The session probe starts an OIDC handshake that it abandons, leaving
        `.AspNetCore.Correlation.*` and `.AspNetCore.OpenIdConnect.Nonce.*`
        behind. Beginning a second handshake on top of those makes the authorize
        endpoint answer a bare `400 Bad Request` with no SETTINGS blob, so
        sign-in reports "the flow has changed" when nothing has changed at all.
        Starting from a clean slate fixes it.

        Scoped by domain on purpose: inside Home Assistant this is the *shared*
        client session, so clearing everything would evict cookies belonging to
        unrelated integrations.
        """

        def mine(cookie: Any) -> bool:
            domain = (cookie.get("domain") or "").lower()
            return "esbnetworks.ie" in domain or "b2clogin.com" in domain

        try:
            self._http.cookie_jar.clear(mine)
        except TypeError:  # pragma: no cover - very old aiohttp
            LOG.debug("cookie jar does not support predicate clearing")

    async def login(self) -> None:
        """Azure AD B2C self-asserted sign-in. Exactly one attempt."""
        LOG.info("ESB sign-in (single attempt)")

        self._clear_portal_cookies()

        async with self._http.get(
            f"{ESB_BASE}/",
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            landing = await r.text()
            landing_url = str(r.url)

        if not _is_login_url(landing_url) and await self.is_authenticated():
            LOG.info("ESB already signed in")
            return

        _guard_captcha(landing, "landing page")

        cfg = _extract_b2c_settings(landing)
        csrf = cfg["csrf"]
        tx = cfg["transId"]
        policy = cfg["hosts"]["policy"]
        b2c_root = _b2c_root(landing_url, cfg)

        # 1. present credentials
        async with self._http.post(
            f"{b2c_root}/SelfAsserted",
            params={"tx": tx, "p": policy},
            data={
                "request_type": "RESPONSE",
                "signInName": self._user,
                "password": self._pass,
            },
            headers=self._headers({
                "X-CSRF-TOKEN": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": landing_url,
                "Origin": f"{urlsplit(landing_url).scheme}://{urlsplit(landing_url).netloc}",
            }),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            body = await r.text()

        _guard_captcha(body, "credential")
        try:
            status = json.loads(body)
        except ValueError:
            raise AuthFailed(
                f"unexpected SelfAsserted response (flow may have changed): {body[:200]}"
            ) from None
        if str(status.get("status")) != "200":
            raise AuthFailed(f"credentials rejected: {status.get('message') or status}")

        # 2. exchange for an id_token
        async with self._http.get(
            f"{b2c_root}/api/CombinedSigninAndSignup/confirmed",
            params={"rememberMe": "false", "csrf_token": csrf, "tx": tx, "p": policy},
            headers=self._headers({"Referer": landing_url}),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            confirmed = await r.text()

        # Credentials have been accepted by this point, so a challenge here is
        # the journey's own extra step, not a rejection.
        _guard_captcha(confirmed, "post-credential")
        action, fields = _extract_callback_form(confirmed)

        # 3. hand the token to the app so it mints a session cookie
        async with self._http.post(
            action or f"{ESB_BASE}/signin-oidc",
            data=fields,
            headers=self._headers({
                "Referer": f"{urlsplit(landing_url).scheme}://{urlsplit(landing_url).netloc}/",
                "Origin": f"{urlsplit(landing_url).scheme}://{urlsplit(landing_url).netloc}",
            }),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            await r.read()

        if not await self.is_authenticated():
            raise AuthFailed("sign-in completed but the session probe still fails")

        LOG.info("ESB sign-in OK")

    # -------------------------------------------------------- antiforgery

    async def _xsrf(self) -> str:
        async with self._http.get(
            f"{ESB_BASE}/af/t",
            headers=self._headers({"Referer": f"{ESB_BASE}/{HISTORIC_PATH}"}),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status == 401 or r.status == 403:
                raise SessionExpired("antiforgery endpoint rejected the session")
            r.raise_for_status()
            return (await r.json())["token"]

    async def _post(self, path: str, payload: dict, *, referer: str) -> aiohttp.ClientResponse:
        token = await self._xsrf()
        r = await self._http.post(
            f"{ESB_BASE}/{path.lstrip('/')}",
            json=payload,
            headers=self._headers({
                "Content-Type": "application/json; charset=UTF-8",
                "x-xsrf-token": token,
                "x-returnurl": referer,
                "Referer": referer,
                "Origin": ESB_BASE,
                "Accept": "*/*",
            }),
            timeout=aiohttp.ClientTimeout(total=90),
        )
        if r.status in (401, 403):
            r.release()
            raise SessionExpired(f"{path} rejected the session ({r.status})")
        r.raise_for_status()
        await asyncio.sleep(_CALL_SPACING)
        return r

    # --------------------------------------------------------------- data

    async def meter_window(self) -> dict[str, Any]:
        """Available data range plus meter capabilities."""
        r = await self._post(
            "datahub/GetMprnTypeAndStartEndDate",
            {"mprn": self.mprn, "coleDate": self.cole_date},
            referer=f"{ESB_BASE}/{HISTORIC_PATH}",
        )
        async with r:
            return await r.json()

    async def time_series(self, date: str, chart_type: str) -> dict[str, Any]:
        """Banded aggregates. chart_type: Day | Week | Month | Year.

        Used to cross-check our own band maths against ESB's, not as the
        primary source. The HDF is the primary source.
        """
        r = await self._post(
            "datahub/GetTimeSeriesData",
            {
                "mprn": self.mprn,
                "coleDate": self.cole_date,
                "date": date,
                "chartType": chart_type,
            },
            referer=f"{ESB_BASE}/{HISTORIC_PATH}",
        )
        async with r:
            return await r.json()

    async def billed_reads(self) -> dict[str, Any]:
        """Every register read handed to the supplier. One call gets them all.

        Two details worth recording, because both invite a wrong "fix".

        The key is upper-case `MPRN` here, unlike the datahub endpoints which use
        lower case. Confirmed from capture.

        The response carries `currentPage: 1, totalPages: 4, pageSize: 8`, which
        looks exactly like a paginated endpoint hiding three quarters of the
        data. It is not. All 28 readings arrive in this single response; the page
        counts describe how the portal's own UI *renders* them, eight at a time,
        and `MeterReadHistory/GetMeterReadingHistoryCards` exists only to render a
        slice of rows the client already has as HTML cards. Adding a page
        parameter here does not fetch more, it just deviates from the request the
        portal actually makes.
        """
        r = await self._post(
            "MeterReadHistory/GetBilledReads",
            {"MPRN": self.mprn},
            referer=f"{ESB_BASE}/MeterReadHistory",
        )
        async with r:
            return await r.json()

    async def download_hdf(self, search_type: str = "intervalkw") -> bytes:
        """Full interval history as an HDF CSV. One call gets everything."""
        r = await self._post(
            "energymanagement/api/consumption/DownloadHdfPeriodic",
            {"mprn": self.mprn, "searchType": search_type},
            referer=f"{ESB_BASE}/{HISTORIC_PATH}",
        )
        async with r:
            return await r.read()


# ------------------------------------------------------------------ helpers


def _guard_captcha(html: str, step: str = "sign-in") -> None:
    """Stop if ESB is presenting a CAPTCHA. It is never solved or bypassed.

    `step` names where in the journey this happened, which matters: a challenge
    on the landing page means the account is gated before credentials are even
    offered, whereas one after the credential POST means the credentials were
    accepted and only the challenge stands in the way. Those call for different
    user action, so the message says which it was.
    """
    reason = _captcha_reason(html)
    if reason is None:
        return
    raise CaptchaEncountered(
        f"ESB Networks is showing a CAPTCHA at the {step} step ({reason}). "
        "Stopping rather than working around it; the integration tries again by itself "
        "after twelve hours. Repeated sign-ins bring this on, so nothing is tried sooner."
    )


def _extract_b2c_settings(html: str) -> dict[str, Any]:
    m = re.search(r"var SETTINGS = (\{.*?\});", html, re.S)
    if not m:
        raise AuthFailed(
            "could not find the B2C SETTINGS blob on the sign-in page; the "
            "sign-in flow has changed and needs re-capturing"
        )
    try:
        return json.loads(m.group(1))
    except ValueError as exc:
        raise AuthFailed(f"unparseable B2C SETTINGS blob: {exc}") from exc


def _b2c_root(landing_url: str, cfg: dict[str, Any]) -> str:
    """Base URL for the B2C policy endpoints.

    Preferred source is the page's own ``hosts.tenant``
    (``/<tenant>.onmicrosoft.com/B2C_1A_signup_signin``), which is exactly what
    the portal's JavaScript posts against. Slicing the authorize URL is kept as
    a fallback for when that key is absent.
    """
    tenant = (cfg.get("hosts") or {}).get("tenant")
    parsed = urlsplit(landing_url)
    if tenant:
        return f"{parsed.scheme}://{parsed.netloc}{str(tenant).rstrip('/')}"
    return landing_url.split("/oauth2")[0].rsplit("/", 1)[0]


# The authorize request uses response_type=code with response_mode=form_post,
# so the confirmed endpoint returns an auto-submitting form carrying `code` and
# `state` — not `id_token`. Accept whichever credential the flow actually used
# rather than insisting on an implicit-flow token that never arrives.
_CALLBACK_CREDENTIALS = ("code", "id_token")


def _extract_callback_form(html: str) -> tuple[str | None, dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", id="auto") or soup.find("form")
    if not form:
        raise AuthFailed("no callback form returned after the credential step")
    fields = {
        i["name"]: i.get("value", "") for i in form.find_all("input") if i.get("name")
    }
    if not any(k in fields for k in _CALLBACK_CREDENTIALS):
        raise AuthFailed(
            "callback form carried neither 'code' nor 'id_token'; got "
            f"{list(fields)}"
        )
    return form.get("action"), fields
