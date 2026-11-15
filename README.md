<p align="center"><img src="docs/brand/logo.png" alt="Electricity Analyzer" height="110"></p>

# Electricity Analyzer

Home Assistant custom integration for Irish homes combining **Shelly EM Gen4** clamps, **ESB Networks** smart-meter data, and **SSE Airtricity** Day/Night tariffs into one sidebar dashboard.

## Features

- **Live Tracking** — Real-time house total (120 A clamp), sub-circuit breakdown (50 A clamp), current Day/Night band, spending rate, and running meter register estimates.
- **ESB Networks Sync** — Pulls official 30-minute HDF interval data, reconciles clamp drift, backfills Home Assistant downtime, and updates the HA Energy dashboard.
- **Irish Day/Night Tariffs** — Year-round GMT meter clock handling (23:00–08:00 winter, 00:00–09:00 summer), daily standing charge + PSO levy accounting, and multi-period tariff history.
- **SSE Airtricity Integration** — Reads billing dates, balances, and issued bills, and submits Day/Night meter readings **only on manual confirmation** (with 24-hour and plausibility guards, plus dry-run mode).
- **Forecasting & History** — Weather-aware usage/bill forecasts (Open-Meteo + optional AccuWeather) and permanent local SQLite storage (`<config>/electricity_analyzer/electricity.db`) with CSV export.

## Hardware & Requirements

### Energy Monitor & CT Clamps
Built around a 2-channel **Shelly EM Gen4** (`S4EM-002CXCEU`) added to Home Assistant via the built-in **Shelly** integration, paired with split-core current transformer (CT) clamps:
- **Shelly 120 A Split-Core CT Clamp** *(required, Channel 1 / `em1:0`)* — clipped around the mains live tail at the consumer unit to measure **full-house** consumption (`energy`, `power`, `apparent_power`, `power_factor`, `voltage`, `current`, `frequency`).
- **Shelly 50 A Split-Core CT Clamp** *(optional, Channel 2 / `em1:1`)* — clipped onto a dedicated sub-circuit inside the house total to break out its share separately (tracked inside the 120 A total, never double-counted).

### Software & Accounts
- **Home Assistant** `2026.9.0` or newer
- **Initial Meter Reading** — one Day & Night register snapshot off your electricity meter (value + timestamp) to anchor live register tracking
- *(Optional)* **ESB Networks** account (`myaccount.esbnetworks.ie` email, password, 11-digit MPRN) for official 30-minute HDF smart-meter sync and clamp calibration
- *(Optional)* **SSE Airtricity** account (email, password) for billing cycle dates, issued bill tracking, and manual reading submission
- *(Optional)* **AccuWeather** integration entity to blend with built-in Open-Meteo forecasts

## Getting Started

### 1. Install

**Via HACS:**
1. Open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/rhea-bonnerji/ha-electricity-analyzer` with category **Integration**.
3. Install **Electricity Analyzer** and **restart Home Assistant**.

**Manual:**
```bash
cp -r custom_components/electricity_analyzer /path/to/homeassistant/config/custom_components/
```
Then **restart Home Assistant**.

### 2. Configure

1. Go to **Settings → Devices & services → Add integration → Electricity Analyzer**.
2. Follow the setup prompts (tariff rates and the starting meter reading come pre-filled with sample defaults — overwrite them with your own):
   - Select your **Shelly** house and sub-circuit energy/power sensors.
   - Enter your **Day/Night tariff rates**, standing charge, and PSO levy (pre-filled with SSE Airtricity *30% Day Night* incl. VAT as a template — edit any field for your supplier/plan).
   - Connect **SSE Airtricity** (*Off* / *Dry run* / *Live*) and **ESB Networks** (*Off* / *Account*).
   - Enter your **current Day & Night meter reading** and timestamp (if SSE *Live* is connected, it pre-fills from SSE's latest reading; otherwise overwrite the sample numbers with the figures on your meter), pick your **home location** for weather, and confirm the **Electricity** sidebar dashboard.
3. **Reload your browser once** (`Cmd+Shift+R` / `Ctrl+F5`) so the bundled chart and table cards load.

### 3. Updating Meter Readings & Tariff Prices Anytime

Everything pre-filled at setup remains editable without reinstalling:
- **Change tariff rates / add price periods:** **Settings → Devices & services → Electricity Analyzer → Configure → Prices** (update rates from today, schedule a future rate change, or add/remove a past tariff period — stored history re-prices automatically).
- **Change plan details or night hours:** **Configure → Plan from setup** (supplier/plan name, night window, billing period).
- **Update your meter reading anchor:** **Configure → Meter reading**, or enter a new Day/Night reading directly on the **Electricity → Meter & SSE** dashboard tab (or via `electricity_analyzer.set_meter_reading`).

## Services

| Service | Description |
|---|---|
| `electricity_analyzer.submit_meter_reading` | Submit Day/Night reading to SSE (`confirm: true` required) |
| `electricity_analyzer.set_meter_reading` | Record a manual meter reading anchor |
| `electricity_analyzer.add_tariff` | Add a past or future tariff period and re-price stored hours |
| `electricity_analyzer.refresh_esb` | Trigger an immediate ESB Networks sync |
| `electricity_analyzer.supply_esb_session` | Hand over an active ESB browser session cookie |
| `electricity_analyzer.rebuild` | Rebuild hourly history and statistics from the HA recorder |
| `electricity_analyzer.export_csv` | Export stored tables to `<config>/electricity_analyzer/exports/` |

## License

MIT © Rhea Bonnerji — see [LICENSE](LICENSE). Bundled `apexcharts-card` © Jérôme Wiedemann (MIT). Weather data via [Open-Meteo](https://open-meteo.com/) (CC BY 4.0).
