# Brand

`mark.svg` is the source of the icon: a day/night tile (the meter's two registers), the
bolt crossing from day into night, and the analysis rising on the night side.
`logo-light.svg` / `logo-dark.svg` add the wordmark (Avenir Next).

The PNGs Home Assistant shows live in `custom_components/electricity_analyzer/brand/`
(Home Assistant serves a custom integration's own `brand/` folder before its CDN):

| File | Size |
|---|---|
| `icon.png`, `dark_icon.png` | 256 × 256 |
| `icon@2x.png`, `dark_icon@2x.png` | 512 × 512 |
| `logo.png`, `dark_logo.png` | height 128 |
| `logo@2x.png`, `dark_logo@2x.png` | height 256 |

`render.sh` renders an SVG to a transparent PNG with headless Chromium.
