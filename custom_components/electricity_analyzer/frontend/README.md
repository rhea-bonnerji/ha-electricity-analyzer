# Frontend assets

* `electricity-analyzer-data-card.js`: this integration's table view of its database
  (hour / day / month / year, date range, sorting, column groups, CSV download).
  Vanilla custom element, no build step.
* `apexcharts-card.js`: [apexcharts-card](https://github.com/RomRider/apexcharts-card)
  by Jérôme Wiedemann, v2.2.3, MIT (`LICENSE-apexcharts-card.txt`), the released build,
  unmodified. Used for the fixed-window charts. If apexcharts-card is already installed
  (e.g. through HACS) that copy is used instead.

Both are served from `/electricity_analyzer_static/` and registered as Lovelace
resources by `dashboard/frontend_assets.py`.
