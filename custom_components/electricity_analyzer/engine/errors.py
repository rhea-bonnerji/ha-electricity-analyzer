"""Shared error types for the portal clients (ESB Networks, SSE Airtricity)."""

from __future__ import annotations


class PortalError(RuntimeError):
    """Any failure talking to a supplier or network portal."""


class AuthFailed(PortalError):
    """Credentials rejected, or the sign-in flow no longer matches."""


class CaptchaEncountered(PortalError):
    """A portal served a CAPTCHA.

    We stop here on purpose. The CAPTCHA is the portal operator's access
    control and their way of rate-limiting automated sign-ins; working around
    it is not ours to do. The integration surfaces a re-auth prompt so the
    person signs in once in a browser, and the persisted session carries on
    from there.
    """


class RateLimited(PortalError):
    """Portal asked us to slow down, or we tripped a throttle."""


class SessionExpired(PortalError):
    """Persisted session is no longer valid; a fresh sign-in is required."""
