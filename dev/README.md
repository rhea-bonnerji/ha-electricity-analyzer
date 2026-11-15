# Development sandbox

A throwaway Home Assistant in Docker. Nothing in it is real:

* `shelly_sim` (dev/sim) stands in for the Shelly EM Gen4: two clamps, the same 16
  entities, live readings every 5 s and 400 days of backfilled history, calibrated to sample
  Irish smart-meter data. **Never install it on a real Home Assistant.**
* `sse_mock` (dev/sse_mock) is a local copy of the SSE Airtricity portal's API, shaped from
  a captured session with de-identified values. Credentials `x` / `z`.
* `esb_mock` (dev/esb_mock) is a local copy of ESB Networks' portal and its Azure AD B2C
  sign-in. Credentials `x` / `z`, MPRN `10000000001`. It publishes what the simulated
  full-house clamp measured, as the meter would count it (+1.2%), two to three days late.

The mocks answer under the portals' **real names** (account.sseairtricity.com,
myaccount.esbnetworks.ie, login.esbnetworks.ie), but only inside the sandbox's Docker
network, over HTTPS with a throwaway CA that only the sandbox trusts (`devcerts.py`
makes it; `certs/` is not committed). So the integration runs exactly as installed, with
no "test server" setting anywhere in it, and the real SSE and ESB are never contacted.
Their state is visible from the laptop at http://localhost:8090/__mock/state and
http://localhost:8091/__mock/state.

```bash
.venv-ha/bin/python dev/sandbox.py manual   # start, onboard, simulated Shelly running; YOU add the integration
.venv-ha/bin/python dev/sandbox.py up       # start and set everything up automatically
.venv-ha/bin/python dev/sandbox.py verify   # exhaustive check: entities, charts, tables, gaps, logs
.venv-ha/bin/python dev/sandbox.py down     # stop and delete it
```

Open http://localhost:8126, user `sandbox`, password in `sandbox.py`.

## Values for a manual setup in the sandbox

| Step | Enter |
|---|---|
| Shelly | (pre-selected) |
| Clamp sensors | (pre-filled) |
| Plan | (defaults) |
| SSE | *Live*, email `x`, password `z` |
| ESB | *My ESB Networks account*, email `x`, password `z`, MPRN `10000000001` (pre-filled from SSE) |
| Meter reading | (pre-filled: day 13876, night 5660, read at 2026-10-07 18:50:34) |
| Location and weather | drag the pin to the house; weather entity `weather.forecast_home` (Met.no, standing in for AccuWeather) |
| Dashboard | (defaults) |

`card-harness.html` exercises the data-table card in a plain browser with fake data.
