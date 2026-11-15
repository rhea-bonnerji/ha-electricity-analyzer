# Changelog

## 1.0.4 — 2026-11-15

Fixes billing-cycle rollovers once SSE Airtricity issues a statement, adds **Last bill** tracking, and hardens SSE portal discovery against transient web-app blips. All stored settings, credentials, historical statistics, learned weights, and database records are preserved.

### Billing and SSE Airtricity
- **Completed Bill Boundary**: Recognises SSE's `billDate` on `latest-bill-v2` (and `"Bill"` entries in `transaction-history`) and aligns the closed period with SSE's accepted closing reading date when present, so **Next bill (estimate)** starts cleanly after the issued bill instead of stretching back to `supply_start`.
- **Last Bill Sensor & Dashboard**: Adds `sensor.electricity_analyzer_last_bill` and a **Last bill** summary on **Costs & bill** (and **Meter & SSE**) showing the issued invoice amount, billed day/night units, account balance before and after the bill, and the metered comparison over the same days.
- **Privacy Hygiene**: Normalises `latest_bill` and `transactions` before storage and state publication so street addresses and statement/premises identifiers are never stored in metadata or exposed in entity attributes.
- **Resilient Portal Discovery**: Checks HTTP status codes when reading SSE's public Angular bundle, supports single/double-quoted script tags with query strings, and falls back to the last working portal configuration when SSE's static frontend host has a temporary maintenance outage.

## 1.0.3 — 2026-10-31

A hotpatch fixing portal upstream changes from SSE Airtricity and ESB Networks. All stored settings, credentials, historical statistics, learned weights, and database records are fully preserved.

### SSE Airtricity
- **Portal Bundle Discovery**: Updated the regex parser for SSE Airtricity's frontend bundle to match single-quoted configuration properties (`api_url`, `login_url`, `policy`, `client_id`), fixing broken config discovery after SSE's late-October portal build.

### ESB Networks
- **Portal Migration & Session Verification**: Updated the session probe URL to `/energymanagement/historic-consumption` following ESB's retirement of `/Api/HistoricConsumption`.
- **HDF Interval Download**: Updated the half-hourly consumption export endpoint to `/energymanagement/api/consumption/DownloadHdfPeriodic` (fixing HTTP 405 from the deprecated `/DataHub/DownloadHdfPeriodic`).
- **Azure Affinity Routing**: Added `ARRAffinity` and `ARRAffinitySameSite` cookies to session persistence hints so requests are pinned to the active Azure App Service instance, preventing session loss and 500 errors.
- **Mock Server Parity**: Updated `dev/esb_mock/server.py` to support `/energymanagement/...` routes alongside legacy routes.

## 1.0.2 — 2026-10-08

A hotpatch. Nothing has to be set up again: settings, credentials, the ESB session, every
stored hour, the learned weights, the forecast log and every entity and statistic id are
kept. On first start the settings are migrated (only two shipped defaults move, below).

### Tariff and money
- Each day starts at its fixed charge: standing charge + PSO (annual ÷ 365, or 366 in a
  leap year) is due in full from local midnight and booked on the day's first hour; energy
  is added on top as it is used. Today's cost, the bill "so far" and the forecasts follow
  this. Every stored hour is re-priced once under the new rules on first start.
- **Tariff periods** (Configure → Tariff periods): the four prices (day, night, standing
  charge, PSO) for any dates, past (a stretch with no price yet) or future (a price
  increase), with an optional last day (what applied after it carries on). Periods can be
  removed. Each change is stored and every hour re-priced and re-charted at once, without a
  reload; the periods are listed on the Prices and Costs tabs. `add_tariff` takes `valid_to`
  too. A restart never overwrites a period set by hand.
- The SSE 30% Day Night default starts on **6 October 2026** and its PSO levy is **€19.10 a
  year** incl. VAT. Installations that kept 1.0.x's defaults (5 October, €6.67) are moved;
  values typed in by hand are kept. The stored 5 October copy of the same plan is moved, not
  duplicated.
- A tariff is never applied before it began: hours before the first tariff period keep their
  kWh and band but have no price (they are counted as "unpriced hours"); the next-bill
  estimate charges no standing charge or PSO for those days and says how many there are.
  Add the earlier tariff with `add_tariff` and they are priced at it.
- Standing charge and PSO are shown separately (today's cost, period sensors) as well as
  together. The unit-rate entity stays a pure €/kWh energy price.
- "The tariff in force" no longer means a future-dated period.

### Charts
- "Today, hour by hour" (and every chart that reads statistics) could stay on "Loading..."
  for good: an empty answer showed apexcharts-card's "Loading..." text, and one failed or
  unanswered websocket call (a restart, a reload) locked the series. Calls are now bounded,
  retried, and end in "no data" rather than an error; empty charts say why.
- Hourly and daily buckets are drawn over the period they cover. Drawn at their start, the
  first night hour (00:00–01:00 in summer) straddled midnight and was clipped, so night
  seemed to start at 01:00. The stored data and the bands were always right.

### Weather
- Weather no longer waits for electricity data: history (the last two years), the
  forecast and the optional weather entity are fetched independently and each failure is
  isolated; the last good data is kept and the failure is shown.
- Temperature and degree-day statistics are published every hour, not only after each
  twelve-hourly fetch, and the forecast call covers the last week (the archive runs days
  behind), so today's and recent charts are not "No statistics found". History fetched
  later than before is published in full.
- "Heating degree days today" counts completed hours (like today's energy); the whole-day
  expectation is an attribute.

### Downtime
- Hours Home Assistant missed (it was down) are rebuilt from ESB's half-hourly meter data
  once ESB publishes them, and the oversized first hour after the restart, which held the
  whole downtime at that hour's rate, is corrected. Marked "ESB meter" in the hour table;
  never used to learn the clamp-to-meter ratio; nothing estimated where ESB has no data.

### Live
- This week's, month's and year's kWh and cost follow the clamps (every few seconds), not a
  five-minute summary. "Today, hour by hour" includes the current hour as it fills and
  refreshes every 30 s; the data table every minute.
- A period with no data reads "unknown", not 0 kWh / €0.

### Status
- The stored-hours sensor carries a `collection` attribute: loading, no data, collecting
  since…, today so far, weather waiting / unavailable / stale. The dashboard shows it where
  a chart would otherwise just be empty. Sensors fill from the database straight after a
  start instead of waiting for the network steps.

### ESB is the authority
- The clamp prices each hour at once (provisional, clamp x learned meter factor); when ESB
  publishes the hour (2-3 days later) it is re-priced on ESB's own kWh (final) and everything
  built on it follows: costs, bill, period totals, statistics, the Energy dashboard. The
  hour table says what each hour is priced on.
- The forecasts, the learned weights and the scoring of every forecast model use ESB's days
  wherever ESB has them (the clamp only for the last two or three days), so they are revised
  as ESB publishes; the meter factors keep learning from every ESB day. The live meter
  register estimate uses ESB's figures where it has them.
- ESB's history from before the clamp is stored as ESB hours (no hall / rest split) and
  priced by whatever tariff periods cover those dates.

### Energy dashboard
- The grid connection is the meter's energy and energy cost as this integration's own
  statistics (`meter_energy`, `cost_energy`), so it follows every tariff period, past or
  future, and every ESB revision; a price entity cannot (Home Assistant never re-prices the
  past). Fixed charges stay on the Electricity dashboard.
- A choice at setup and in the options: *Replace* (default: this becomes the grid
  connection and any other grid connection is removed, a copy is kept and a notification
  lists it), *Add only*, or *Leave alone*. Only grid connections are ever changed; the
  1.0.x connection on the clamp's sensor is upgraded in place.

### ESB Networks and SSE Airtricity
- After ANY failed ESB or SSE attempt (an outage, a CAPTCHA, a refused sign-in) nothing is
  tried for 12 hours: not the schedule, a restart, a button or a service. Then once by
  itself, and again 12 hours later if that fails too. The buttons that would contact the
  portal are unavailable meanwhile; the status sensors say "Waiting 12 h to retry" and
  when. No Repairs item and no cookie to paste; the saved session is kept. New sign-in
  details start afresh.
- An ESB outage (5xx, 429, network error) no longer looks like an expired session and
  never spends a sign-in.
- Saving the ESB or SSE options without changing the sign-in details no longer signs in.
- An ESB check that was not due no longer re-publishes and re-learns everything every 30
  minutes.
- An unexpected answer from SSE is recorded as SSE's error and touches nothing else.

### Robustness
- The start-up integrity check no longer treats "could not check" (a locked file, a
  read-only folder) as damage; only real damage triggers a restore from backup.
- Every periodic job is isolated and logged; re-pricing is serialised with the hourly rollup;
  the insights coordinator is shut down before the database closes on unload.
- Today's totals never include yesterday's hours between midnight and the next refresh.
- The data card says "starting" while the integration loads and retries soon by itself.

## 1.0.1 — 2026-10-07

Fixes from the first run against the real ESB Networks and SSE Airtricity portals.

### ESB Networks
- Setup's sign-in is handed to the integration instead of being thrown away. Signing in
  a second time seconds later is what made ESB answer with a CAPTCHA.
- At most two sign-ins in any 24 hours, setup's included; "Refresh ESB now" cannot go
  past it. At most two automatic fetches a day, once by default (a saved "three" runs as
  two). Fetches that reuse the saved session are not sign-ins.
- The CAPTCHA hand-over (Settings > System > Repairs) comes back after a restart; before,
  it vanished and ESB sat in its back-off with nothing to fix it from.
- A failure with no text (a timeout) is reported by its type instead of as an empty
  error, which the status sensor took for "no error".

### SSE Airtricity
- The 24-hour cooldown counts a reading SSE already holds (sent from its site or app),
  not only readings sent from here: the status no longer says "ready" when SSE would
  refuse, and "SSE next submission allowed" is known.
- New sensors: SSE next bill date, account balance (with overdue amount and payment
  dates), contract end, and SSE's last accepted day and night readings with their
  history (date, method, units used). An "SSE account" section on the Meter & SSE view.
- An estimated read on SSE's side is never learned from as if it were the meter.

### Other
- The billing-period forecast uses SSE's billing period, as the next-bill estimate
  already did, instead of saying "set a billing date in the options".
- The fill ten minutes after setup ran on a worker thread and never happened; it runs.
- Today's hall and rest-of-house energy are daily totals that reset at midnight (they
  can dip as the hall correction is refined, which `total_increasing` misreads).

## 1.0.0 — 2026-10-07

The Electric Ireland Insights and Electric Ireland Dashboard integrations are replaced by
one integration, **Electricity Analyzer**.

### Sources
- Shelly EM Gen4: the full-house clamp (120 A) as the total and the Energy dashboard's
  grid source; the hall clamp (50 A) as a circuit inside it. Live accounting from the
  clamps' own energy counters, about once a second.
- ESB Networks: half-hourly meter data and actual register reads (manual, or 1–3
  automatic fetches a day; a CAPTCHA is never worked around). Setup signs in once to
  check the account.
- SSE Airtricity: meter readings sent only on a button press (or a confirmed service
  call), at most one per 24 hours, with dry-run mode; billing period and contract read
  from the account (read-only).
- Weather: Open-Meteo history and forecast for the home's location, picked on a map at
  setup; optional blending with a Home Assistant weather entity such as AccuWeather.

### Pricing
- Day/night tariff with the meter's GMT clock (night 23:00–08:00 winter, 00:00–09:00
  summer), switching automatically at the clock changes. Defaults: SSE Airtricity 30% Day
  Night from 5 October 2026 (6 October from 1.0.2), incl. VAT. Dated tariff periods; history is re-priced.
- Standing charge and PSO levy accrue per local day (annual ÷ 365), including on 23- and
  25-hour days.

### Learning
- Meter factors (day, night) learned from every real meter reading and every ESB day;
  every cost is priced on meter-equivalent kWh.
- Hall clamp correction from time-aligned readings: a CT phase error solved from the
  bound hall ≤ house, applied hour by hour by power factor.
- "What drives usage": a regression on degree days, wind/damp, sun, wind, rain, weekday
  and time of year, refitted nightly; weights recorded daily.
- Forecast ensemble of six models, logged nightly, scored and re-weighted, with a bias
  correction; bootstrapped from history on first install.

### Data
- One SQLite database (WAL, full fsync): hours and minutes kept for good, integrity-checked
  on start with automatic restore, nightly rotated backups, CSV export.
- Gaps (Home Assistant down, late or later-imported history) are filled from the recorder.
- Long-term statistics for every derived series, for charts over any date range.

### Dashboard
- One sidebar dashboard with 13 tabs: Overview, Clamps, Explore, Usage, Hall 24/7,
  Weather, Prices, Costs & bill, Forecast, Meter & SSE, ESB, Data, System. Phone, tablet,
  desktop and widescreen layouts.
- A data card showing every stored value at hour/day/month/year resolution with any date
  range, sorting and CSV download.
