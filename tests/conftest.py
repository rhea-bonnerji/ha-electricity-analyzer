"""Shared fixtures. The repository root comes first on sys.path, so `custom_components`
resolves to this repository's package rather than the test plugin's own.

No autouse fixture requests `hass`: the recorder fixture has to be set up BEFORE
`hass` exists, so the Home Assistant tests request `recorder_mock` first and
`enable_custom_integrations` explicitly (see test_integration.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev" / "sse_mock"))
