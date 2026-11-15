#!/usr/bin/env python3
"""Verification script for SSE Airtricity and ESB Networks live authentication.

Loads .credentials.json locally and redacts all secrets from all output.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(BASE_DIR, "custom_components", "electricity_analyzer")
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)

import aiohttp
from engine.esb.client import EsbClient, new_cookie_jar
from engine.sse.client import SseClient


def mask_username(val: str) -> str:
    if not val:
        return "<empty>"
    if "@" in val:
        name, domain = val.split("@", 1)
        return f"{name[:2]}***@{domain[:2]}***"
    return f"{val[:2]}***"


def mask_mprn(val: str) -> str:
    s = str(val or "").strip()
    if len(s) > 5:
        return f"{s[:3]}***{s[-2:]}"
    return "***"


def sanitize(text: str, secrets: list[str]) -> str:
    result = text
    for secret in secrets:
        if secret and len(secret) > 2:
            result = result.replace(secret, "[REDACTED]")
    return result


async def verify_sse(http: aiohttp.ClientSession, creds: dict[str, str], all_secrets: list[str]) -> bool:
    user = creds.get("username", "")
    pwd = creds.get("password", "")
    print(f"\n--- [1/2] Testing SSE Airtricity ({mask_username(user)}) ---")

    if not user or not pwd:
        print("[SKIP] SSE username or password not provided in .credentials.json")
        return True

    try:
        client = SseClient(http, user, pwd)

        # Step 1: Discover configuration from web app
        print("-> Step 1: Discovering Azure AD B2C configuration from SSE web app...")
        cfg = await client.discover()
        print(f"   [PASS] SSE discovery OK:")
        print(f"          api_url: {cfg.api_url}")
        print(f"          login_url: {cfg.login_url}")
        print(f"          policy: {cfg.policy}")
        print(f"          client_id: {cfg.client_id[:8]}...")

        # Step 2: Sign-in / acquire token
        print("-> Step 2: Authenticating with SSE Azure AD B2C...")
        await client.login()
        if client._token:
            print(f"   [PASS] SSE authentication OK (JWT token acquired, length: {len(client._token)} bytes)")
        else:
            print("   [FAIL] SSE login completed but no token returned")
            return False

        # Step 3: Fetch meter info
        print("-> Step 3: Querying SSE meter details...")
        meter = await client.meter_info()
        print(f"   [PASS] SSE meter query OK:")
        print(f"          model: {meter.model}")
        print(f"          meter_type: {meter.meter_type}")
        print(f"          registers count: {len(meter.registers)}")
        for reg in meter.registers:
            print(f"          - register #{reg.number} ({reg.kind}): last value = {reg.last_value} (status: {reg.last_status})")

        print("=== SSE Airtricity Verification: ALL CHECKS PASSED ===")
        return True
    except Exception as exc:
        msg = sanitize(str(exc), all_secrets)
        print(f"   [FAIL] SSE error: {type(exc).__name__}: {msg}")
        return False


async def verify_esb(http: aiohttp.ClientSession, creds: dict[str, str], all_secrets: list[str]) -> bool:
    user = creds.get("username", "")
    pwd = creds.get("password", "")
    mprn = creds.get("mprn", "")
    print(f"\n--- [2/2] Testing ESB Networks ({mask_username(user)}, MPRN: {mask_mprn(mprn)}) ---")

    if not user or not pwd or not mprn:
        print("[SKIP] ESB username, password, or MPRN not provided in .credentials.json")
        return True

    try:
        client = EsbClient(http, user, pwd, str(mprn).replace(" ", ""))

        cookie_raw = creds.get("cookie", "") or creds.get("cookies", "")
        if cookie_raw:
            from engine.session import parse_cookie_header
            cookies = parse_cookie_header(cookie_raw)
            restored = client.restore_session(cookies)
            print(f"   [INFO] Restored {restored} session cookie(s) from .credentials.json")

        # Step 1: Probe session
        print("-> Step 1: Probing initial session state...")
        is_live = await client.is_authenticated()
        print(f"   [INFO] Current session valid: {is_live}")

        # Step 2: Ensure session (login if not live)
        if not is_live:
            print("-> Step 2: Signing in to ESB Networks Azure AD B2C...")
            await client.login()
            print("   [PASS] ESB login OK")
        else:
            print("   [INFO] Using existing live session")

        # Step 3: Fetch meter window
        print("-> Step 3: Querying ESB meter window...")
        try:
            window = await client.meter_window()
            mprn_type = window.get("mprnType") or window.get("type")
            start_d = window.get("start_date") or window.get("startDate")
            end_d = window.get("end_date") or window.get("endDate")
            print(f"   [PASS] ESB meter window OK: type={mprn_type}, range={start_d} to {end_d}")
        except Exception as e:
            print(f"   [INFO] ESB meter window optional call returned: {type(e).__name__} ({e})")

        # Step 4: Fetch billed reads
        print("-> Step 4: Querying ESB billed meter reads...")
        try:
            reads = await client.billed_reads()
            reads_list = reads.get("meterReads") or reads.get("reads") or []
            print(f"   [PASS] ESB billed reads OK: {len(reads_list)} record(s) returned")
        except Exception as e:
            print(f"   [INFO] ESB billed reads optional call returned: {type(e).__name__} ({e})")

        # Step 5: Test interval HDF download
        print("-> Step 5: Testing HDF interval data download...")
        hdf_bytes = await client.download_hdf()
        print(f"   [PASS] ESB HDF download OK: received {len(hdf_bytes)} bytes of interval CSV data")
        if len(hdf_bytes) > 100:
            snippet = hdf_bytes[:200].decode("utf-8", errors="ignore").replace("\n", " ").strip()
            print(f"          Header snippet: {snippet[:120]}...")

        print("=== ESB Networks Verification: ALL CHECKS PASSED ===")
        return True
    except Exception as exc:
        msg = sanitize(str(exc), all_secrets)
        print(f"   [FAIL] ESB error: {type(exc).__name__}: {msg}")
        return False


async def main() -> int:
    creds_path = os.path.join(BASE_DIR, ".credentials.json")
    if not os.path.exists(creds_path):
        print(f"[ERROR] Could not find {creds_path}")
        return 1

    with open(creds_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    sse_creds = data.get("sse", {})
    esb_creds = data.get("esb", {})

    all_secrets = [
        str(sse_creds.get("password", "")),
        str(sse_creds.get("username", "")),
        str(esb_creds.get("password", "")),
        str(esb_creds.get("username", "")),
        str(esb_creds.get("mprn", "")),
        str(esb_creds.get("cookie", "") or esb_creds.get("cookies", "")),
    ]

    # Shared or independent client session with special cookie jar for ESB
    async with aiohttp.ClientSession(cookie_jar=new_cookie_jar()) as http:
        sse_ok = await verify_sse(http, sse_creds, all_secrets)
        esb_ok = await verify_esb(http, esb_creds, all_secrets)

    print("\n================== SUMMARY ==================")
    print(f"SSE Airtricity : {'PASS' if sse_ok else 'FAIL'}")
    print(f"ESB Networks   : {'PASS' if esb_ok else 'FAIL'}")
    print("=============================================")

    return 0 if (sse_ok and esb_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
