# Setup Guide

## Prerequisites

- **Home Assistant** `2026.9.0+`
- **Shelly EM Gen4** added via Home Assistant's Shelly integration
- **Current meter reading** (Day & Night registers + timestamp)
- *(Optional)* **ESB Networks** email, password, and 11-digit MPRN
- *(Optional)* **SSE Airtricity** email and password
- *(Optional)* **AccuWeather** weather entity

## Installation

1. Install via **HACS** (custom repository `https://github.com/rhea-bonnerji/ha-electricity-analyzer`, type *Integration*) or copy `custom_components/electricity_analyzer` into `<config>/custom_components/`.
2. **Restart Home Assistant**.
3. Go to **Settings → Devices & services → Add integration → Electricity Analyzer** and complete the wizard:
   - **Clamps:** Select the main house clamp (120 A) and sub-circuit clamp (50 A). Companion power/voltage/PF entities are auto-detected.
   - **Tariff:** Enter Day/Night unit rates (c/kWh), annual standing charge, and PSO levy — all **including VAT**. Night hours follow the meter's GMT clock (`23:00–08:00` winter, `00:00–09:00` summer). Additional historical or upcoming tariffs can be added anytime under **Configure → Tariff periods**.
   - **Portals:** Choose *Off*, *Dry run*, or *Live* for SSE Airtricity, and *Off* or *Account* for ESB Networks. Failed portal attempts back off automatically for 12 hours to avoid CAPTCHAs.
   - **Meter Anchor & Weather:** Enter your latest Day/Night meter reading, place the map pin for Open-Meteo weather data, and confirm the `/electricity` dashboard.
4. **Hard-refresh your browser once** after setup to load the custom Lovelace cards.
