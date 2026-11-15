"""The package is shippable: versions agree, translations match, brand images exist."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.electricity_analyzer.const import VERSION

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "electricity_analyzer"


def test_version_matches_manifest():
    assert json.loads((PKG / "manifest.json").read_text())["version"] == VERSION


def test_translations_match_strings():
    assert json.loads((PKG / "strings.json").read_text()) == json.loads((PKG / "translations" / "en.json").read_text())


def test_brand_images_present():
    for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png",
                 "dark_icon.png", "dark_icon@2x.png", "dark_logo.png", "dark_logo@2x.png"):
        assert (PKG / "brand" / name).stat().st_size > 1000, name


def test_every_service_is_documented():
    import yaml

    services = yaml.safe_load((PKG / "services.yaml").read_text())
    strings = json.loads((PKG / "strings.json").read_text())["services"]
    assert set(services) == set(strings)
    for name, spec in services.items():
        assert set((spec or {}).get("fields", {})) <= set(strings[name].get("fields", {})), name
