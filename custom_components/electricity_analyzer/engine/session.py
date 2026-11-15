"""Portal session state: capture it, persist it, put it back.

WHY THIS MODULE EXISTS

Portals gate sign-in, and ESB gates it with a CAPTCHA that this project
will not solve or bypass. That makes a persisted session not a nicety but the
primary mechanism: sign in rarely, keep the session, and only fall back to asking
a human when it genuinely dies.

The failure mode this removes is the one that makes a scraper unusable. Without
persistence every Home Assistant restart starts from a cold jar, every cold jar
means a sign-in, and a few sign-ins a day is exactly what brings the CAPTCHA on.
The integration then spends its life locked out of a portal it has valid
credentials for.

WHAT IS AND IS NOT STORED

Only cookies whose names look like session or antiforgery state. Analytics and
consent cookies are dropped: they are noise, they change on every request, and
persisting fewer secrets is better. The values are bearer credentials for as long
as they live, so nothing here is ever logged — callers get names and counts.

This module has no Home Assistant imports, so the same code serves the
integration and the offline harness. One definition, one behaviour.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

LOG = logging.getLogger(__name__)

# Cookies worth keeping. Matched as substrings, case-insensitively.
SESSION_COOKIE_HINTS: tuple[str, ...] = (
    ".aspnetcore.cookies",
    ".aspnetcore.antiforgery",
    ".aspnetcore.correlation",
    ".aspnetcore.openidconnect",
    "arraffinity",
    "x-ms-cpim",
    "asp.net_sessionid",
    "__requestverificationtoken",
    ".asponxsrf",
    "authsession",
    "sessionid",
)

# Cookies deliberately discarded even though the portal sets them.
IGNORE_COOKIE_HINTS: tuple[str, ...] = (
    "optanon",
    "ai_session",
    "ai_user",
    "_ga",
    "_gid",
    "_gcl",
    "cookieconsent",
    "cookiebot",
    "hjsession",
    "_fbp",
)


def is_session_cookie(name: str, *, strict: bool = True) -> bool:
    """Should this cookie be persisted?

    `strict` keeps only recognised session/antiforgery names. Turning it off
    keeps anything not explicitly ignored, which is the right choice when
    accepting a session handed over by a human: portals invent cookie names, and
    refusing an unrecognised one would silently drop the very thing that carries
    the session.
    """
    low = (name or "").lower()
    if not low:
        return False
    if any(hint in low for hint in IGNORE_COOKIE_HINTS):
        return False
    if not strict:
        return True
    return any(hint in low for hint in SESSION_COOKIE_HINTS)


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse a `Cookie:` header as copied from a browser's devtools.

    Deliberately forgiving about what a human pastes: a leading "Cookie:" label,
    stray whitespace and blank segments are all tolerated, because the
    alternative is a hand-holding flow that rejects the thing it asked for.
    Cookie VALUES may legitimately contain "=", so the split is on the first one
    only.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    # Tolerate the whole "Cookie: a=b; c=d" line, or a bare "a=b; c=d".
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]
    # A pasted header may arrive wrapped across lines.
    text = " ".join(text.split("\n"))

    out: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            out[name] = value
    return out


def export_cookies(
    jar: Iterable[Any], domains: tuple[str, ...], *, strict: bool = True
) -> dict[str, str]:
    """Serialisable session state for the given domains.

    Takes any iterable of Morsel-like cookies, which is what aiohttp's jar is, so
    this stays testable without constructing a real jar.
    """
    out: dict[str, str] = {}
    for cookie in jar:
        try:
            domain = (cookie.get("domain") or "").lower()
            name = cookie.key
            value = cookie.value
        except AttributeError:  # pragma: no cover - not a Morsel
            continue
        if domains and not any(d in domain for d in domains):
            continue
        if not is_session_cookie(name, strict=strict):
            continue
        out[name] = value
    return out


def dumps(cookies: dict[str, str]) -> str:
    return json.dumps(cookies, separators=(",", ":"))


def loads(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except ValueError:
        LOG.warning("stored session state was not valid JSON; ignoring it")
        return {}
    if not isinstance(obj, dict):
        LOG.warning("stored session state was not an object; ignoring it")
        return {}
    return {str(k): str(v) for k, v in obj.items()}


def describe(cookies: dict[str, str]) -> str:
    """A log-safe description. Names and lengths only, never values."""
    if not cookies:
        return "no cookies"
    return ", ".join(f"{name}({len(value)}b)" for name, value in sorted(cookies.items()))
