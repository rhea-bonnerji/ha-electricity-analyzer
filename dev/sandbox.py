#!/usr/bin/env python3
"""Run Electricity Analyzer in a throwaway Home Assistant and check it end to end.

    .venv-ha/bin/python dev/sandbox.py up       start, onboard, set up, verify (leaves it running)
    .venv-ha/bin/python dev/sandbox.py verify   re-run the checks against a running sandbox
    .venv-ha/bin/python dev/sandbox.py down     stop it and delete its config

    .venv-ha/bin/python dev/sandbox.py manual   start and onboard; YOU add the integration

Nothing here is real: the Shelly is `shelly_sim`, and SSE and ESB are the local mocks
in dev/sse_mock and dev/esb_mock, answering under the portals' real names inside the
sandbox's Docker network only (HTTPS with a throwaway CA, see dev/devcerts.py). The
integration is set up exactly as a person would, with the nonsense credentials x / z;
the real SSE and ESB portals are never contacted.

Setup uses the default mock anchor reading: day 13876, night 5660, read at
18:50:34 on 7 October 2026.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

import devcerts

HERE = Path(__file__).resolve().parent
COMPOSE = HERE / "docker-compose.yml"
CONFIG = HERE / "sandbox-config"
HOST = "http://localhost:8126"
CLIENT_ID = f"{HOST}/"
# Test-only credentials for this throwaway local instance.
USERNAME = "sandbox"
PASSWORD = "sandbox-only-not-a-real-password"
TOKEN_FILE = CONFIG / ".sandbox_token.json"

ANCHOR = {"reading_day": 13876, "reading_night": 5660, "reading_at": "2026-10-07 18:50:34"}
LOCATION = {"latitude": 53.3498, "longitude": -6.2603}

OK = "  \033[32mPASS\033[0m"
BAD = "  \033[31mFAIL\033[0m"
failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"{OK if ok else BAD} {label}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(label)
    return ok


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args], check=True, cwd=HERE)


def write_config() -> None:
    CONFIG.mkdir(exist_ok=True)
    (CONFIG / "configuration.yaml").write_text(
        "default_config:\n\n"
        "homeassistant:\n"
        "  name: Electricity Analyzer sandbox\n"
        "  time_zone: Europe/Dublin\n"
        "  latitude: 53.3498\n"
        "  longitude: -6.2603\n"
        "  country: IE\n"
        "  currency: EUR\n"
        "  unit_system: metric\n\n"
        "shelly_sim:\n"
        "  history_days: 400\n\n"
        "logger:\n"
        "  default: warning\n"
        "  logs:\n"
        "    custom_components.electricity_analyzer: debug\n"
        "    custom_components.shelly_sim: info\n"
    )


class WS:
    def __init__(self, token: str) -> None:
        self.token = token
        self._id = 0

    async def __aenter__(self) -> WS:
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(f"{HOST}/api/websocket", max_msg_size=0)
        await self.ws.receive_json()
        await self.ws.send_json({"type": "auth", "access_token": self.token})
        msg = await self.ws.receive_json()
        if msg["type"] != "auth_ok":
            raise RuntimeError(msg)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.ws.close()
        await self.session.close()

    async def cmd(self, **payload: Any) -> Any:
        self._id += 1
        mine = self._id
        await self.ws.send_json({"id": mine, **payload})
        while True:
            msg = await self.ws.receive_json()
            if msg.get("id") == mine and msg.get("type") == "result":
                if not msg.get("success"):
                    raise RuntimeError(f"{payload.get('type')}: {msg.get('error')}")
                return msg.get("result")


async def wait_up(s: aiohttp.ClientSession, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            async with s.get(f"{HOST}/api/onboarding") as r:
                if r.status < 500:
                    return
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(2)
    raise SystemExit("Home Assistant did not come up")


async def tokens(s: aiohttp.ClientSession) -> dict[str, Any]:
    async with s.get(f"{HOST}/api/onboarding") as r:
        steps = await r.json() if r.status == 200 else []
    if any(step.get("step") == "user" and not step.get("done") for step in steps):
        r = await s.post(
            f"{HOST}/api/onboarding/users",
            json={"name": "Sandbox", "username": USERNAME, "password": PASSWORD, "client_id": CLIENT_ID, "language": "en"},
        )
        code = (await r.json())["auth_code"]
    else:
        r = await s.post(
            f"{HOST}/auth/login_flow",
            json={"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID},
        )
        flow = await r.json()
        r = await s.post(
            f"{HOST}/auth/login_flow/{flow['flow_id']}",
            json={"client_id": CLIENT_ID, "username": USERNAME, "password": PASSWORD},
        )
        code = (await r.json())["result"]
    r = await s.post(f"{HOST}/auth/token", data={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID})
    tok = await r.json()
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    for step, payload in (("core_config", {}), ("analytics", {}), ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID})):
        await s.post(f"{HOST}/api/onboarding/{step}", json=payload, headers=auth)
    TOKEN_FILE.write_text(json.dumps(tok))
    return tok


async def states(s: aiohttp.ClientSession, token: str) -> dict[str, dict[str, Any]]:
    async with s.get(f"{HOST}/api/states", headers={"Authorization": f"Bearer {token}"}) as r:
        return {st["entity_id"]: st for st in await r.json()}


async def flow(s: aiohttp.ClientSession, token: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    auth = {"Authorization": f"Bearer {token}"}
    r = await s.post(
        f"{HOST}/api/config/config_entries/flow",
        json={"handler": "electricity_analyzer", "show_advanced_options": True},
        headers=auth,
    )
    result = await r.json()
    for payload in steps:
        if result.get("type") != "form":
            break
        print(f"      flow step {result['step_id']}: submitting")
        r = await s.post(f"{HOST}/api/config/config_entries/flow/{result['flow_id']}", json=payload, headers=auth)
        result = await r.json()
        if result.get("errors"):
            print(f"      errors: {result['errors']}")
    return result


async def setup(s: aiohttp.ClientSession, token: str) -> None:
    async with s.get(f"{HOST}/api/config/config_entries/entry", headers={"Authorization": f"Bearer {token}"}) as r:
        entries = await r.json()
    if any(e["domain"] == "electricity_analyzer" for e in entries):
        print("      electricity_analyzer already set up")
        return
    house = "sensor.full_house_energy_monitor"
    hall = "sensor.hall_energy_monitor"
    channel = lambda p: {  # noqa: E731
        "power": f"{p}_power",
        "apparent_power": f"{p}_apparent_power",
        "power_factor": f"{p}_power_factor",
        "voltage": f"{p}_voltage",
        "current": f"{p}_current",
        "frequency": f"{p}_frequency",
        "energy_returned": f"{p}_energy_returned",
    }
    result = await flow(
        s,
        token,
        [
            {"house_energy": f"{house}_energy", "hall_energy": f"{hall}_energy"},
            {"house": channel(house), "hall": channel(hall)},
            {},  # tariff: the SSE 30% Day Night defaults
            {"sse_mode": "live", "sse_username": "x", "sse_password": "z"},
            {
                "esb_mode": "account",
                "esb_username": "x",
                "esb_password": "z",
                "mprn": "10000000001",
                "esb_fetches_per_day": "2",
            },
            ANCHOR,
            {
                "location": LOCATION,
                # Met.no stands in for AccuWeather here (no key needed); on the real system
                # pick the AccuWeather integration's weather entity.
                **({"weather_entity": "weather.forecast_home"} if "weather.forecast_home" in await states(s, token) else {}),
            },
            {},  # dashboard: defaults
        ],
    )
    check(result.get("type") == "create_entry", "config flow completed", str(result.get("errors") or result.get("type")))


async def verify(s: aiohttp.ClientSession, token: str) -> None:
    """Everything a person would see, checked: entities, charts, tables, gaps, logs."""
    import re
    from datetime import datetime, timedelta, timezone

    st = await states(s, token)
    ours = {k: v for k, v in st.items() if k.split(".")[1].startswith("electricity_analyzer")}
    check(len(ours) >= 70, "integration entities exist", f"{len(ours)} entities")
    unavailable = [k for k, v in ours.items() if v["state"] == "unavailable"]
    check(not unavailable, "no entity is unavailable", ", ".join(unavailable[:8]))
    # Unknown is legitimate only where nothing can exist yet.
    allowed_unknown = {"sensor.electricity_analyzer_sse_next_submission_allowed", "sensor.electricity_analyzer_forecast_billing_period_cost"}
    unknown = [k for k, v in ours.items() if v["state"] == "unknown" and k not in allowed_unknown and not k.startswith(("button.",))]
    check(not unknown, "every sensor has a value", ", ".join(unknown[:10]))

    async with WS(token) as ws:
        cfg = await ws.cmd(type="lovelace/config", url_path="electricity")
        views = cfg.get("views", [])
        check(len(views) >= 12, "dashboard tabs", ", ".join(v["title"] for v in views))
        referenced: set[str] = set()
        stats_needed: set[str] = set()
        cards = 0

        def walk(node):
            nonlocal cards
            if isinstance(node, dict):
                if "type" in node and isinstance(node.get("type"), str) and node.get("type") not in ("grid", "sections"):
                    cards += 1
                for key in ("entity",):
                    if isinstance(node.get(key), str) and "." in node[key] and ":" not in node[key]:
                        referenced.add(node[key])
                if node.get("type") == "statistics-graph":
                    for e in node.get("entities", []):
                        ref = e["entity"] if isinstance(e, dict) else e
                        stats_needed.add(ref)
                gen = node.get("data_generator")
                if isinstance(gen, str):
                    for sid in re.findall(r"'((?:electricity_analyzer:|sensor\.)[a-z0-9_\.:]+)'", gen):
                        stats_needed.add(sid)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(views)
        missing = sorted(e for e in referenced if e not in st)
        check(not missing, f"all {len(referenced)} entities on the dashboard exist", ", ".join(missing[:8]))
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        result = await ws.cmd(
            type="recorder/statistics_during_period", start_time=start.isoformat(), end_time=end.isoformat(),
            statistic_ids=sorted(stats_needed), period="day", types=["mean", "change"],
        )
        empty = sorted(sid for sid in stats_needed if not result.get(sid))
        check(not empty, f"all {len(stats_needed)} chart statistics have data", ", ".join(empty[:8]))
        print(f"      {cards} cards across {len(views)} tabs")

        for period in ("hour", "day", "month", "year"):
            table = await ws.cmd(type="electricity_analyzer/table", period=period)
            check(bool(table["rows"]), f"data table ({period})", f"{len(table['rows'])} rows, {len(table['columns'])} columns")
        hours = await ws.cmd(
            type="electricity_analyzer/table", period="hour",
            start=(end - timedelta(days=30)).date().isoformat(), end=(end - timedelta(days=1)).date().isoformat(),
        )
        partial = [r for r in hours["rows"] if r.get("hours", 1) == 0]
        expected = 29 * 24
        check(len(hours["rows"]) >= expected - 1 and not partial, "no gaps in the last 30 days of hours",
              f"{len(hours['rows'])} of ~{expected}")
        await ws.cmd(type="electricity_analyzer/readings")
        minutes = await ws.cmd(type="electricity_analyzer/minutes", start=(end - timedelta(hours=1)).isoformat())
        check(len(minutes["rows"]) >= 1, "minute-by-minute storage", f"{len(minutes['rows'])} minute(s) in the last hour")

        resources = await ws.cmd(type="lovelace/resources")
        urls = " ".join(r["url"] for r in resources)
        check("apexcharts-card" in urls and "electricity-analyzer-data-card" in urls, "chart and data cards registered")
        prefs = await ws.cmd(type="energy/get_prefs")
        grids = [src for src in prefs.get("energy_sources", []) if src.get("type") == "grid"]
        check(len(grids) == 1 and grids[0].get("stat_energy_from") == "electricity_analyzer:meter_energy"
              and grids[0].get("stat_cost") == "electricity_analyzer:cost_energy",
              "Energy dashboard: the meter's energy and cost (revised statistics), nothing else")
        issues = await ws.cmd(type="repairs/list_issues")
        ours_issues = [i["issue_id"] for i in issues.get("issues", []) if i["domain"] == "electricity_analyzer"]
        check(not ours_issues, "no repair issues raised", ", ".join(ours_issues))
        log = await ws.cmd(type="system_log/list")
        errors = [
            f"{e['level']} {e['name']}: {str(e['message'])[:120]}"
            for e in log
            if e["level"] in ("ERROR", "CRITICAL")
            or (e["level"] == "WARNING" and "electricity_analyzer" in e["name"])
        ]
        check(not errors, "no errors in the Home Assistant log", " | ".join(errors[:5]))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["up", "verify", "down", "manual"])
    args = parser.parse_args()
    if args.command == "down":
        compose("down", "-v")
        shutil.rmtree(CONFIG, ignore_errors=True)
        return 0
    if args.command in ("up", "manual"):
        write_config()
        devcerts.ensure()
        compose("up", "-d")
    async with aiohttp.ClientSession() as s:
        await wait_up(s)
        tok = await tokens(s)
        token = tok["access_token"]
        if args.command == "manual":
            print("      waiting for the simulated Shelly entities")
            for _ in range(120):
                if "sensor.full_house_energy_monitor_energy" in await states(s, token):
                    break
                await asyncio.sleep(2)
            print(f"\nReady for you: {HOST}  (user {USERNAME!r}, password in dev/sandbox.py)")
            print("Settings -> Devices & services -> Add integration -> Electricity Analyzer")
            print("SSE: Live, x / z.  ESB: My ESB Networks account, x / z, MPRN 10000000001.")
            return 0
        if args.command == "up":
            print("      waiting for the simulated Shelly entities")
            for _ in range(120):
                if "sensor.full_house_energy_monitor_energy" in await states(s, token):
                    break
                await asyncio.sleep(2)
            await setup(s, token)
            await asyncio.sleep(20)
        await verify(s, token)
    print(f"\n{HOST}  user {USERNAME!r}  ({len(failures)} failure(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
