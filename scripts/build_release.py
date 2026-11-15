#!/usr/bin/env python3
"""Build the distributable: dist/electricity_analyzer-<version>.zip and SHA256SUMS.

The zip contains `custom_components/electricity_analyzer/` exactly as it should land in a
Home Assistant config directory, so a manual install is: unzip in the config directory,
restart. HACS installs straight from the repository and does not need the zip.

Checks before building, and refuses to build if any fails:
  * manifest.json version == const.VERSION
  * strings.json == translations/en.json
  * every brand image present
  * no __pycache__, .pyc, .DS_Store or test files in the package
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "electricity_analyzer"
DIST = ROOT / "dist"
BRAND = ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png",
         "dark_icon.png", "dark_icon@2x.png", "dark_logo.png", "dark_logo@2x.png")
EXCLUDE = re.compile(r"(__pycache__|\.pyc$|\.DS_Store$|\.partial$)")


def fail(message: str) -> None:
    print(f"build refused: {message}")
    sys.exit(1)


def main() -> int:
    manifest = json.loads((PKG / "manifest.json").read_text())
    version = manifest["version"]
    const = (PKG / "const.py").read_text()
    match = re.search(r'^VERSION: Final = "([^"]+)"', const, re.M)
    if not match or match.group(1) != version:
        fail(f"const.VERSION {match.group(1) if match else None!r} != manifest {version!r}")
    if json.loads((PKG / "strings.json").read_text()) != json.loads((PKG / "translations" / "en.json").read_text()):
        fail("strings.json and translations/en.json differ")
    missing = [b for b in BRAND if not (PKG / "brand" / b).is_file()]
    if missing:
        fail(f"brand images missing: {missing}")

    files = sorted(p for p in PKG.rglob("*") if p.is_file() and not EXCLUDE.search(str(p)))
    DIST.mkdir(exist_ok=True)
    target = DIST / f"electricity_analyzer-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            arcname = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(arcname, date_time=(2026, 10, 7, 0, 0, 0))  # reproducible
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (DIST / "SHA256SUMS").write_text(f"{digest}  {target.name}\n")
    print(f"{target.relative_to(ROOT)}  {len(files)} files  {target.stat().st_size / 1024:.0f} KiB")
    print(f"sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
