"""ESB Networks portal constants. No Home Assistant imports."""

from __future__ import annotations

from typing import Final

ESB_BASE: Final = "https://myaccount.esbnetworks.ie"
ESB_LOGIN_PAGE: Final = "https://myaccount.esbnetworks.ie"

# The portal sends this "account start" date with every data request; early enough
# for any account. Taken from captured traffic.
DEFAULT_COLE_DATE: Final = "2020-06-01"

IRELAND_TZ: Final = "Europe/Dublin"
