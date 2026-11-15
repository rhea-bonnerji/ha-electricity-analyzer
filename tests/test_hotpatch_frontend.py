"""1.0.2 hotpatch, the generated dashboard: charts can never sit on "Loading..." for
ever, and hourly buckets are drawn over the hour they cover.

The chart bodies are executed with Node exactly as apexcharts-card executes them (an
async function of entity, start, end, hass, moment), against a fake `hass` whose
websocket call succeeds, fails, hangs or answers nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from custom_components.electricity_analyzer.const import stat_id
from custom_components.electricity_analyzer.dashboard.cards import NO_DATA, Cards, Source
from custom_components.electricity_analyzer.dashboard.views import build_views

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
UTC = timezone.utc


class _Everything(dict):
    """Resolves every key, so every card the dashboard can have is built."""

    def get(self, key, default=None):  # noqa: ANN001
        return f"sensor.electricity_analyzer_{key}"


def _cards() -> Cards:
    return Cards(Source(ours=_Everything()), apex=True, data_card=True)


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _apex_cards() -> list[dict[str, Any]]:
    return [n for n in _walk(build_views(_cards())) if n.get("type") == "custom:apexcharts-card"]


def test_every_chart_says_empty_in_words_never_loading():
    charts = _apex_cards()
    assert len(charts) > 20
    for card in charts:
        text = card["apex_config"]["noData"]["text"]
        assert text and "Loading" not in text, card["header"]["title"]


def test_today_chart_is_live_and_says_when_nothing_is_measured_yet():
    today = next(c for c in _apex_cards() if c["header"]["title"] == "kWh per hour")
    assert today["apex_config"]["noData"]["text"] == "Nothing measured yet today."
    assert today["update_interval"] == "30s"
    bodies = [s["data_generator"] for s in today["series"]]
    assert all("entity.attributes.hourly" in b and "callWS" not in b for b in bodies)


def test_every_recorder_call_is_guarded():
    bodies = [s["data_generator"] for c in _apex_cards() for s in c["series"] if "data_generator" in s]
    calling = [b for b in bodies if "callWS" in b]
    assert calling
    for body in calling:
        assert body.startswith("const ws = async"), body[:80]
        assert body.count("hass.callWS(") == 1  # only inside the guard
        assert "await ws(" in body


# ----------------------------------------------------------------- run in Node

HARNESS = r"""
const fs = require('fs');
const [body, mode] = [fs.readFileSync(process.argv[2], 'utf8'), process.argv[3]];
const realTimeout = setTimeout;
globalThis.setTimeout = (fn, ms) => realTimeout(fn, Math.min(ms, 5));   // fast clock
const H = 3600e3, start = new Date(Date.UTC(2026, 9, 7, 23)), end = new Date(Date.UTC(2026, 9, 8, 23));
let calls = 0;
const hass = { callWS: (msg) => { calls += 1;
  if (mode === 'reject') return Promise.reject({code: 3, message: 'Connection lost'});
  if (mode === 'hang') return new Promise(() => {});
  if (mode === 'empty') return Promise.resolve({});
  if (mode === 'flaky' && calls < 3) return Promise.reject({code: 3, message: 'Connection lost'});
  const id = msg.statistic_ids[0];
  const rows = [0, 1].map(i => ({start: start.getTime() + i * H, end: start.getTime() + (i + 1) * H, change: 1 + i}));
  return Promise.resolve({[id]: rows}); } };
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const fn = new AsyncFunction('entity', 'start', 'end', 'hass', 'moment', "'use strict'; " + body);
const guard = realTimeout(() => { console.log(JSON.stringify({hung: true})); process.exit(0); }, 3000);
console.warn = () => {};
fn({state: '1', attributes: {}}, start, end, hass, null).then(
  (data) => { clearTimeout(guard); console.log(JSON.stringify({data, calls})); },
  (err) => { clearTimeout(guard); console.log(JSON.stringify({threw: String(err && (err.message || err)), calls})); });
"""


def _run(body: str, mode: str, tmp_path: Path) -> dict[str, Any]:
    (tmp_path / "harness.js").write_text(HARNESS)
    (tmp_path / "body.js").write_text(body)
    out = subprocess.run(
        [NODE, str(tmp_path / "harness.js"), str(tmp_path / "body.js"), mode],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _today_series_body() -> str:
    return _cards().stat_series("Night rate", stat_id("house_energy_night"), period="hour", colour="#000")["data_generator"]


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_buckets_are_placed_at_the_middle_of_their_hour(tmp_path):
    result = _run(_today_series_body(), "ok", tmp_path)
    xs = [datetime.fromtimestamp(x / 1000, UTC) for x, _ in result["data"]]
    # 23:00-00:00 UTC is 00:00-01:00 in Irish summer time: drawn at 00:30 local, inside
    # the day, not centred on midnight (where it was clipped and night seemed to start at 01:00).
    assert xs == [datetime(2026, 10, 7, 23, 30, tzinfo=UTC), datetime(2026, 10, 8, 0, 30, tzinfo=UTC)]


@needs_node
@pytest.mark.parametrize("mode", ["reject", "hang", "empty"])
def test_a_failing_or_silent_websocket_ends_in_no_data_not_a_throw(tmp_path, mode):
    result = _run(_today_series_body(), mode, tmp_path)
    assert "threw" not in result and not result.get("hung"), result
    assert result["data"] == []
    if mode != "empty":
        assert result["calls"] == 3  # retried, then gave up cleanly


@needs_node
def test_a_dropped_connection_during_a_restart_recovers_by_retrying(tmp_path):
    result = _run(_today_series_body(), "flaky", tmp_path)
    assert result["calls"] == 3 and len(result["data"]) == 2


@needs_node
def test_every_generated_body_runs(tmp_path):
    for card in _apex_cards():
        for series in card["series"]:
            body = series.get("data_generator")
            if not body or "callWS" not in body or "electricity_analyzer/minutes" in body:
                continue
            result = _run(body, "reject", tmp_path)
            assert result.get("data") == [], (card["header"]["title"], result)


@needs_node
def test_data_card_parses():
    subprocess.run(
        [NODE, "--check", str(ROOT / "custom_components/electricity_analyzer/frontend/electricity-analyzer-data-card.js")],
        check=True, timeout=30,
    )


CLOCK_HARNESS = r"""
const fs = require('fs');
const body = fs.readFileSync(process.argv[2], 'utf8');
const RealDate = Date, FIXED = Number(process.argv[3]);
globalThis.Date = class extends RealDate {
  constructor(...a) { if (a.length) super(...a); else super(FIXED); }
  static now() { return FIXED; }
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const fn = new AsyncFunction('entity', 'start', 'end', 'hass', 'moment', "'use strict'; " + body);
const entity = {attributes: JSON.parse(process.argv[4])};
fn(entity, null, null, null, null).then((rows) => {
  const fmt = new Intl.DateTimeFormat('en-GB', {hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'Europe/Dublin'});
  console.log(JSON.stringify(rows.map(([x]) => fmt.format(new RealDate(x)))));
});
"""


@needs_node
@pytest.mark.parametrize(
    "day",
    [datetime(2026, 10, 25, 12, tzinfo=UTC), datetime(2026, 3, 29, 12, tzinfo=UTC), datetime(2026, 7, 1, 12, tzinfo=UTC)],
    ids=["autumn-back", "spring-forward", "summer"],
)
def test_typical_day_hours_are_irish_clock_hours_on_every_day(tmp_path, day):
    from custom_components.electricity_analyzer.dashboard.views import _profile

    (tmp_path / "clock.js").write_text(CLOCK_HARNESS)
    (tmp_path / "body.js").write_text(_profile("house_kw"))
    profile = {"house_kw": {str(h): 1.0 for h in (0, 1, 3, 8, 9, 23)}}
    out = subprocess.run(
        [NODE, str(tmp_path / "clock.js"), str(tmp_path / "body.js"), str(int(day.timestamp() * 1000)), json.dumps(profile)],
        capture_output=True, text=True, timeout=30, check=True, env={"TZ": "Europe/Dublin", "PATH": "/usr/bin:/bin"},
    )
    labels = json.loads(out.stdout)
    if day.month == 3:
        # 01:00-02:00 does not exist on the spring-forward day; the browser moves it on.
        assert labels == ["00:30", "02:30", "03:30", "08:30", "09:30", "23:30"]
    else:
        assert labels == ["00:30", "01:30", "03:30", "08:30", "09:30", "23:30"]


@needs_node
def test_hour_buckets_show_at_their_irish_local_hour(tmp_path):
    """The bucket for 23:00-00:00 UTC in summer is the 00:00-01:00 IST hour, shown at 00:30;
    in winter 23:00-00:00 UTC is 23:00-00:00 local, shown at 23:30."""
    result = _run(_today_series_body(), "ok", tmp_path)
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, UTC).astimezone(  # noqa: E731
        __import__("zoneinfo").ZoneInfo("Europe/Dublin")).strftime("%H:%M")
    assert [fmt(x) for x, _ in result["data"]] == ["00:30", "01:30"]


@needs_node
def test_today_chart_draws_each_band_over_its_own_irish_hour(tmp_path):
    from custom_components.electricity_analyzer.dashboard.views import _today_hours

    (tmp_path / "clock.js").write_text(CLOCK_HARNESS)
    hourly = [  # 7/8 October 2026, Irish summer time: night is 00:00-09:00
        {"start": "2026-10-07T22:00:00+00:00", "end": "2026-10-07T23:00:00+00:00", "band": "day", "kwh": 0.4},
        {"start": "2026-10-07T23:00:00+00:00", "end": "2026-10-08T00:00:00+00:00", "band": "night", "kwh": 0.5},
        {"start": "2026-10-08T00:00:00+00:00", "end": "2026-10-08T01:00:00+00:00", "band": "night", "kwh": 0.6},
        {"start": "2026-10-08T08:00:00+00:00", "end": "2026-10-08T09:00:00+00:00", "band": "day", "kwh": 0.7},
    ]
    labels = {}
    for band in ("day", "night"):
        (tmp_path / "body.js").write_text(_today_hours(band))
        out = subprocess.run(
            [NODE, str(tmp_path / "clock.js"), str(tmp_path / "body.js"), "0", json.dumps({"hourly": hourly})],
            capture_output=True, text=True, timeout=30, check=True, env={"TZ": "Europe/Dublin", "PATH": "/usr/bin:/bin"},
        )
        labels[band] = json.loads(out.stdout)
    assert labels == {"day": ["23:30", "09:30"], "night": ["00:30", "01:30"]}
