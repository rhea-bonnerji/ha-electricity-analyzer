/**
 * Electricity Analyzer: data card
 * ===============================
 *
 * A Lovelace card that is the user's window into the Electricity Analyzer
 * database: hourly / daily / monthly / yearly tables of energy, cost, power,
 * power quality and the ESB comparison, plus the meter-reading submission log.
 * Sortable, column groups that can be hidden, CSV export, light and dark themes.
 *
 * Self-contained: no imports, no build step, no CDN. It is written to load as a
 * module resource (`type: module`) but also works as a classic script.
 *
 *   type: custom:electricity-analyzer-data-card
 *   title: Data                  # optional
 *   dataset: usage               # usage | readings
 *   period: day                  # hour | day | month | year   (usage only)
 *   range_days: 31               # default per period: hour 2, day 31, month 730, year 3650
 *   columns: [label, house_kwh]  # optional initial subset + order of column keys
 *   show_controls: true
 *   max_height: 560              # px; the table scrolls inside the card
 *
 * Websocket commands used:
 *   {type: "electricity_analyzer/table", period, start?, end?}   (inclusive local dates)
 *   {type: "electricity_analyzer/readings"}
 *
 * Fetches happen when the card first has hass + config, when a control changes,
 * on the Refresh button and every 5 minutes while the card is on screen; never
 * merely because hass changed.
 */
(() => {
  "use strict";

  const TAG = "electricity-analyzer-data-card";
  const VERSION = "1.0.0";
  // The newest rows include the live hour, so the table follows the clamps closely.
  const REFRESH_MS = 60 * 1000;
  const WS_TIMEOUT_MS = 60 * 1000;
  // Errors that go away on their own (a restart, a reload, a dropped connection) are
  // retried soon, backing off, rather than only at the next five-minute refresh.
  const TRANSIENT = new Set(["not_loaded", "timeout", "3", "home_assistant_error"]);
  const RETRY_FIRST_MS = 15 * 1000;
  const DATE_DEBOUNCE_MS = 450;
  const ROW_PAGE = 500;
  const STORE = "electricity-analyzer-data-card:v1";

  const DATASETS = ["usage", "readings"];
  const DEFAULT_TITLE = { usage: "Electricity data", readings: "Meter readings" };
  const PERIODS = {
    hour: { label: "Hourly", days: 2 },
    day: { label: "Daily", days: 31 },
    month: { label: "Monthly", days: 730 },
    year: { label: "Yearly", days: 3650 },
  };
  const GROUP_ORDER = [
    "When", "Energy", "Cost", "Power", "Quality", "Meter", "ESB", "Data", "Reading", "Result",
  ];
  const STATUS_TONE = {
    submitted: "success",
    dry_run: "primary",
    failed: "error",
    prepared: "secondary",
    blocked: "warning",
  };
  const ISO_DATETIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$/;
  const SIGNED_KEY = /(^|_)(diff|delta|change|net)(_|$)/i;

  // Material Design Icons (Apache-2.0), inlined so nothing depends on ha-icon loading.
  const ICON = {
    refresh:
      "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z",
    download: "M5,20H19V18H5M19,9H15V3H9V9H5L12,16L19,9Z",
    alert:
      "M11,15H13V17H11V15M11,7H13V13H11V7M12,2C6.47,2 2,6.5 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2M12,20A8,8 0 0,1 4,12A8,8 0 0,1 12,4A8,8 0 0,1 20,12A8,8 0 0,1 12,20Z",
    check: "M21,7L9,19L3.5,13.5L4.91,12.09L9,16.17L19.59,5.59L21,7Z",
    up: "M13,20H11V8L5.5,13.5L4.08,12.08L12,4.16L19.92,12.08L18.5,13.5L13,8V20Z",
    down: "M11,4H13V16L18.5,10.5L19.92,11.92L12,19.84L4.08,11.92L5.5,10.5L11,16V4Z",
    table:
      "M5,4H19A2,2 0 0,1 21,6V18A2,2 0 0,1 19,20H5A2,2 0 0,1 3,18V6A2,2 0 0,1 5,4M5,8V12H11V8H5M13,8V12H19V8H13M5,14V18H11V14H5M13,14V18H19V14H13Z",
  };

  // ------------------------------------------------------------------ helpers

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
  const svg = (path, cls = "") =>
    `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="${path}"/></svg>`;
  const NIL = '<span class="nil" aria-label="no value">—</span>';
  const isBlank = (v) => v === null || v === undefined || v === "";

  // Calendar dates are Date objects at UTC midnight, so arithmetic never trips on DST.
  const pad2 = (n) => String(n).padStart(2, "0");
  const utcDate = (y, m, d) => new Date(Date.UTC(y, m, d));
  const isoDay = (d) =>
    `${String(d.getUTCFullYear()).padStart(4, "0")}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
  const parseDay = (s) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(typeof s === "string" ? s.trim() : "");
    if (!m) return null;
    const d = utcDate(+m[1], +m[2] - 1, +m[3]);
    return isoDay(d) === `${m[1]}-${m[2]}-${m[3]}` ? d : null; // rejects 2026-02-31
  };
  const addDays = (d, n) => utcDate(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + n);
  const monthStart = (d, k = 0) => utcDate(d.getUTCFullYear(), d.getUTCMonth() + k, 1);
  const monthEnd = (d, k = 0) => utcDate(d.getUTCFullYear(), d.getUTCMonth() + k + 1, 0);
  const yearStart = (d, k = 0) => utcDate(d.getUTCFullYear() + k, 0, 1);
  const yearEnd = (d, k = 0) => utcDate(d.getUTCFullYear() + k, 11, 31);

  /** Today's calendar date in `timeZone` (Home Assistant's zone), else the browser's. */
  function todayIn(timeZone) {
    const now = new Date();
    if (timeZone) {
      try {
        const p = {};
        const fmt = new Intl.DateTimeFormat("en-US", {
          timeZone,
          year: "numeric",
          month: "numeric",
          day: "numeric",
        });
        for (const part of fmt.formatToParts(now)) p[part.type] = part.value;
        const d = utcDate(+p.year, +p.month - 1, +p.day);
        if (!Number.isNaN(d.getTime())) return d;
      } catch (_) {
        /* unknown zone: use the browser's */
      }
    }
    return utcDate(now.getFullYear(), now.getMonth(), now.getDate());
  }

  /** Default range of `days` ending `end`, snapped to whole months / years. */
  function rangeForDays(period, days, end) {
    if (period === "month") {
      return [monthStart(end, -(Math.max(1, Math.round(days / 30.4375)) - 1)), end];
    }
    if (period === "year") {
      return [yearStart(end, -(Math.max(1, Math.round(days / 365.25)) - 1)), end];
    }
    return [addDays(end, -(Math.max(1, days) - 1)), end];
  }

  const preset = (id, label, range) => ({ id, label, range });
  const PRESETS = {
    hour: [
      preset("today", "Today", (t) => [t, t]),
      preset("yesterday", "Yesterday", (t) => [addDays(t, -1), addDays(t, -1)]),
      preset("2d", "2 days", (t) => [addDays(t, -1), t]),
      preset("7d", "7 days", (t) => [addDays(t, -6), t]),
    ],
    day: [
      preset("7d", "7 days", (t) => [addDays(t, -6), t]),
      preset("30d", "30 days", (t) => [addDays(t, -29), t]),
      preset("this-month", "This month", (t) => [monthStart(t), t]),
      preset("last-month", "Last month", (t) => [monthStart(t, -1), monthEnd(t, -1)]),
      preset("this-year", "This year", (t) => [yearStart(t), t]),
      preset("all", "Everything", () => null),
    ],
    month: [
      preset("this-year", "This year", (t) => [yearStart(t), t]),
      preset("last-year", "Last year", (t) => [yearStart(t, -1), yearEnd(t, -1)]),
      preset("12m", "12 months", (t) => [monthStart(t, -11), t]),
      preset("24m", "24 months", (t) => [monthStart(t, -23), t]),
      preset("all", "Everything", () => null),
    ],
    year: [
      preset("5y", "5 years", (t) => [yearStart(t, -4), t]),
      preset("10y", "10 years", (t) => [yearStart(t, -9), t]),
      preset("all", "Everything", () => null),
    ],
  };

  /** {start, end} for a preset, null for "everything", undefined if unknown. */
  function presetRange(period, id, today) {
    if (id === "all") return null;
    const p = (PRESETS[period] || []).find((x) => x.id === id);
    if (!p) return undefined;
    const r = p.range(today);
    return r ? { start: isoDay(r[0]), end: isoDay(r[1]) } : null;
  }

  const sameRange = (a, b) =>
    a === b || (!!a && !!b && a.start === b.start && a.end === b.end);

  function normalizeConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("Electricity Analyzer data card: invalid configuration.");
    }
    const dataset = config.dataset ?? "usage";
    if (!DATASETS.includes(dataset)) {
      throw new Error(`dataset must be "usage" or "readings" (got "${dataset}").`);
    }
    const period = config.period ?? "day";
    if (!Object.prototype.hasOwnProperty.call(PERIODS, period)) {
      throw new Error(`period must be one of hour, day, month, year (got "${period}").`);
    }
    let rangeDays = null;
    if (config.range_days !== undefined && config.range_days !== null) {
      const n = Number(config.range_days);
      if (!Number.isFinite(n) || n < 1) throw new Error("range_days must be a number of days (1 or more).");
      rangeDays = Math.round(n);
    }
    let columns = null;
    if (config.columns !== undefined && config.columns !== null) {
      if (!Array.isArray(config.columns) || config.columns.some((k) => typeof k !== "string")) {
        throw new Error("columns must be a list of column keys, e.g. [label, house_kwh, total_cost].");
      }
      columns = config.columns.length ? [...config.columns] : null;
    }
    let maxHeight = 560;
    if (config.max_height !== undefined && config.max_height !== null) {
      const n = Number(config.max_height);
      if (!Number.isFinite(n) || n <= 0) throw new Error("max_height must be a number of pixels.");
      maxHeight = Math.max(120, Math.round(n));
    }
    return {
      ...config,
      dataset,
      period,
      range_days: rangeDays,
      columns,
      max_height: maxHeight,
      show_controls: config.show_controls !== false,
      title: config.title === undefined || config.title === null ? DEFAULT_TITLE[dataset] : String(config.title),
    };
  }

  function normalizeResponse(res) {
    const r = res && typeof res === "object" ? res : {};
    const rows = Array.isArray(r.rows) ? r.rows.filter((x) => x && typeof x === "object") : [];
    let columns = Array.isArray(r.columns)
      ? r.columns
          .filter((c) => c && typeof c === "object" && !isBlank(c.key))
          .map((c) => ({
            key: String(c.key),
            label: isBlank(c.label) ? String(c.key) : String(c.label),
            unit: isBlank(c.unit) ? null : String(c.unit),
            decimals: Number.isInteger(c.decimals) ? Math.min(10, Math.max(0, c.decimals)) : null,
            group: isBlank(c.group) ? "Data" : String(c.group),
          }))
      : [];
    if (!columns.length && rows.length) {
      // No column metadata: show whatever the rows carry rather than nothing.
      columns = Object.keys(rows[0])
        .filter((k) => k !== "key" && k !== "partial")
        .map((k, i) => ({ key: k, label: k, unit: null, decimals: null, group: i === 0 ? "When" : "Data" }));
    }
    return {
      period: isBlank(r.period) ? null : String(r.period),
      start: isBlank(r.start) ? null : String(r.start),
      end: isBlank(r.end) ? null : String(r.end),
      columns,
      rows,
      totals: r.totals && typeof r.totals === "object" ? r.totals : null,
      note: isBlank(r.note) ? null : String(r.note),
    };
  }

  function describeError(err) {
    const obj = err && typeof err === "object" ? err : {};
    const code = obj.code ?? obj.error?.code;
    const raw = obj.message ?? obj.error?.message ?? (typeof err === "string" ? err : "");
    const message = isBlank(raw) ? "" : String(raw);
    if (code === "unknown_command") {
      return {
        title: "Electricity Analyzer isn't available",
        detail:
          "Home Assistant doesn't recognise the Electricity Analyzer data command. Make sure the " +
          "integration is installed, set up and up to date (Settings → Devices & services), then retry.",
        code: "unknown_command",
        message,
      };
    }
    if (code === 3) {
      return {
        title: "Lost connection to Home Assistant",
        detail: "The card retries automatically, or press Retry.",
        code: "",
        message: "",
      };
    }
    if (code === "not_loaded") {
      return {
        title: "Electricity Analyzer is starting",
        detail: "It is loading its data after a restart or a reload. This card tries again by itself.",
        code: "",
        message: "",
      };
    }
    if (code === "timeout") return { title: "Home Assistant didn't answer in time", detail: message, code: "", message: "" };
    return { title: "Couldn't load data", detail: message || "Unknown error.", code: isBlank(code) ? "" : String(code), message: "" };
  }

  function withTimeout(promise, ms) {
    return new Promise((resolve, reject) => {
      const t = setTimeout(
        () => reject({ code: "timeout", message: `No answer after ${Math.round(ms / 1000)} seconds.` }),
        ms
      );
      Promise.resolve(promise).then(
        (v) => {
          clearTimeout(t);
          resolve(v);
        },
        (e) => {
          clearTimeout(t);
          reject(e);
        }
      );
    });
  }

  // ---------------------------------------------------------------------- CSV

  function csvValue(v) {
    if (v === null || v === undefined) return "";
    let s;
    if (typeof v === "number") {
      if (!Number.isFinite(v)) return "";
      s = String(v);
      if (/e/i.test(s)) s = v.toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: 20 });
    } else if (typeof v === "boolean") {
      s = v ? "true" : "false";
    } else if (typeof v === "object") {
      s = JSON.stringify(v);
    } else {
      s = String(v);
    }
    return /[",\r\n]|^\s|\s$/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }

  /** All loaded columns (visible or not), the rows in on-screen order, then totals. */
  function buildCsv(columns, rows, totals) {
    // `key` is machine-readable (ISO date / month / UTC hour) where the label is
    // display text, so it leads the file whenever the rows carry one.
    const withKey = rows.some((r) => !isBlank(r.key)) && !columns.some((c) => c.key === "key");
    const header = (withKey ? ["Key"] : []).concat(
      columns.map((c) => (c.unit ? `${c.label} (${c.unit})` : c.label))
    );
    const line = (cells) => cells.map(csvValue).join(",");
    const out = [line(header)];
    for (const r of rows) out.push(line((withKey ? [r.key] : []).concat(columns.map((c) => r[c.key]))));
    if (totals) out.push(line((withKey ? [""] : []).concat(columns.map((c) => totals[c.key]))));
    return `${out.join("\r\n")}\r\n`;
  }

  // ------------------------------------------------------------------- styles

  const STYLES = `
    :host { display: block; }
    :host([dark]) { color-scheme: dark; }
    [hidden] { display: none !important; }
    ha-card { display: block; }
    .root { container: ea / inline-size; }
    .body {
      --ea-bg: var(--ha-card-background, var(--card-background-color, #fff));
      --ea-fg: var(--primary-text-color, #212121);
      --ea-muted: var(--secondary-text-color, #727272);
      --ea-div: var(--divider-color, rgba(0, 0, 0, 0.12));
      --ea-accent: var(--primary-color, #03a9f4);
      --ea-on-accent: var(--text-primary-color, #fff);
      --ea-soft: var(--secondary-background-color, #f0f0f0);
      --ea-hover: var(--secondary-background-color, #f0f0f0);
      --ea-tint: var(--secondary-background-color, #f0f0f0);
      --ea-err: var(--error-color, #db4437);
      --ea-pad: 16px;
      --ea-cx: 12px;
      --ea-cy: 7px;
      --ea-fs: 14px;
      --ea-grp-h: 22px;
      color: var(--ea-fg);
      font-size: var(--ea-fs);
      line-height: 1.4;
    }
    @supports (color: color-mix(in srgb, red 50%, blue)) {
      .body {
        --ea-hover: color-mix(in srgb, var(--ea-accent) 7%, transparent);
        --ea-tint: color-mix(in srgb, var(--ea-accent) 15%, transparent);
      }
    }
    svg { width: 20px; height: 20px; fill: currentColor; flex: none; }

    /* ---------------------------------------------------------- header */
    .head { display: flex; align-items: center; gap: 8px; padding: 12px 8px 8px var(--ea-pad); min-height: 36px; }
    .title { flex: 1; min-width: 0; font-size: 18px; font-weight: 500; line-height: 1.3;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .actions { display: flex; align-items: center; gap: 4px; margin-left: auto; padding-right: 8px; }
    .ibtn, .btn, .chip, .seg button, .sort, .link {
      all: unset; box-sizing: border-box; cursor: pointer; -webkit-tap-highlight-color: transparent;
    }
    .ibtn { width: 36px; height: 36px; display: inline-flex; align-items: center; justify-content: center;
      border-radius: 50%; color: var(--ea-muted); }
    .ibtn:hover { background: var(--ea-hover); color: var(--ea-fg); }
    .ibtn.busy svg { animation: ea-spin 0.9s linear infinite; }
    .btn { display: inline-flex; align-items: center; gap: 6px; height: 32px; padding: 0 12px;
      border-radius: 16px; border: 1px solid var(--ea-div); color: var(--ea-fg);
      font-size: 13px; font-weight: 500; white-space: nowrap; }
    .btn svg { width: 18px; height: 18px; color: var(--ea-muted); }
    .btn:hover { background: var(--ea-hover); }
    .btn:disabled { opacity: 0.4; cursor: default; background: none; }
    .link { color: var(--ea-accent); font-size: 12px; font-weight: 500; padding: 2px 6px; border-radius: 4px; }
    .link:hover { text-decoration: underline; }

    /* -------------------------------------------------------- controls */
    .controls { display: flex; flex-direction: column; gap: 10px; padding: 0 var(--ea-pad) 12px; }
    .row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; }
    .seg { display: inline-flex; gap: 2px; padding: 3px; border-radius: 18px; background: var(--ea-soft); }
    .seg button { padding: 5px 14px; border-radius: 15px; font-size: 13px; font-weight: 500;
      color: var(--ea-muted); text-align: center; white-space: nowrap; }
    .seg button:hover { color: var(--ea-fg); }
    .seg button[aria-pressed="true"] { background: var(--ea-accent); color: var(--ea-on-accent); }
    .dates { display: flex; align-items: center; gap: 8px; }
    .dates label { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ea-muted); }
    .dates input { box-sizing: border-box; height: 32px; padding: 0 8px; border: 1px solid var(--ea-div);
      border-radius: 8px; background: transparent; color: var(--ea-fg); font: inherit; font-size: 13px;
      font-variant-numeric: tabular-nums; }
    .dates input:hover { border-color: var(--ea-muted); }
    .dash { color: var(--ea-muted); }
    .chips { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
    .lead { font-size: 12px; color: var(--ea-muted); margin-right: 2px; }
    .chip { display: inline-flex; align-items: center; gap: 4px; height: 28px; padding: 0 12px;
      border-radius: 14px; border: 1px solid var(--ea-div); font-size: 13px; color: var(--ea-muted);
      white-space: nowrap; }
    .chip:hover { background: var(--ea-hover); color: var(--ea-fg); }
    .chip[aria-pressed="true"] { background: var(--ea-tint); border-color: transparent; color: var(--ea-fg); font-weight: 500; }
    .chip svg { width: 16px; height: 16px; margin-left: -4px; color: var(--ea-accent); }
    :is(.ibtn, .btn, .chip, .seg button, .sort, .link, .dates input, .wrap):focus-visible {
      outline: 2px solid var(--ea-accent); outline-offset: 2px;
    }
    .wrap:focus-visible { outline-offset: -2px; }

    /* ----------------------------------------------------------- alert */
    .alert { display: flex; align-items: flex-start; gap: 12px; margin: 0 var(--ea-pad) 12px;
      padding: 10px 12px; border-radius: 8px; border: 1px solid var(--ea-err); }
    .alert.big { margin: var(--ea-pad); }
    .alert > svg { color: var(--ea-err); margin-top: 1px; }
    .am { flex: 1; min-width: 0; }
    .at { font-weight: 500; }
    .ad { color: var(--ea-muted); font-size: 13px; overflow-wrap: anywhere; }
    .ac { color: var(--ea-muted); font-size: 12px; margin-top: 4px; font-family: var(--code-font-family, monospace); overflow-wrap: anywhere; }
    .alert .btn { flex: none; }
    @supports (color: color-mix(in srgb, red 50%, blue)) {
      .alert { background: color-mix(in srgb, var(--ea-err) 8%, transparent);
        border-color: color-mix(in srgb, var(--ea-err) 45%, transparent); }
    }

    /* ----------------------------------------------------------- table */
    .area { position: relative; border-top: 1px solid var(--ea-div); }
    .progress { position: absolute; top: 0; left: 0; right: 0; height: 2px; overflow: hidden;
      z-index: 6; pointer-events: none; opacity: 0; transition: opacity 0.2s; }
    .progress.on { opacity: 1; }
    .progress::after { content: ""; position: absolute; top: 0; bottom: 0; left: -35%; width: 35%;
      background: var(--ea-accent); animation: ea-slide 1.2s ease-in-out infinite; }
    .wrap { overflow: auto; overscroll-behavior-x: contain; -webkit-overflow-scrolling: touch; }
    .wrap table { transition: opacity 0.2s; }
    .wrap.dim table { opacity: 0.5; }
    table { border-collapse: separate; border-spacing: 0; min-width: 100%; font-variant-numeric: tabular-nums; }
    th, td { padding: var(--ea-cy) var(--ea-cx); white-space: nowrap; text-align: left;
      background-color: var(--ea-bg); border-bottom: 1px solid var(--ea-div); }
    .n { text-align: right; }
    thead th { position: sticky; top: 0; z-index: 2; font-size: 12px; font-weight: 500;
      color: var(--ea-muted); vertical-align: bottom; }
    table.grouped thead tr.cols th { top: var(--ea-grp-h); }
    thead tr.grps th { height: var(--ea-grp-h); box-sizing: border-box; padding-top: 0; padding-bottom: 0;
      border-bottom: 0; font-size: 11px; font-weight: 500; letter-spacing: 0.06em; text-transform: uppercase;
      vertical-align: middle; color: var(--ea-muted); }
    .gs { border-left: 1px solid var(--ea-div); }
    .sc { position: sticky; left: 0; z-index: 1; box-shadow: inset -1px 0 0 var(--ea-div); }
    thead .sc, tfoot .sc { z-index: 3; }
    tbody tr:hover td { background-image: linear-gradient(var(--ea-hover), var(--ea-hover)); }
    tbody tr:last-child td { border-bottom: 0; }
    tfoot td { position: sticky; bottom: 0; z-index: 2; font-weight: 600; border-bottom: 0;
      border-top: 2px solid var(--ea-div); background-image: linear-gradient(var(--ea-hover), var(--ea-hover)); }
    tr.partial td.sc { box-shadow: inset 3px 0 0 var(--ea-accent), inset -1px 0 0 var(--ea-div); }
    .sort { display: inline-flex; align-items: center; gap: 4px; border-radius: 4px; }
    .n .sort { flex-direction: row-reverse; }
    .n .hl { text-align: right; }
    .sort .ar { width: 14px; height: 14px; opacity: 0; transition: opacity 0.15s; }
    .sort:hover .ar { opacity: 0.45; }
    th[aria-sort] { color: var(--ea-fg); }
    th[aria-sort] .sort .ar { opacity: 1; color: var(--ea-accent); }
    .unit { font-weight: 400; opacity: 0.8; }
    .nil { color: var(--ea-muted); opacity: 0.6; }
    td.wr { white-space: normal; min-width: 34ch; max-width: 56ch; }
    .live { display: inline-flex; align-items: center; gap: 4px; margin-left: 8px; font-size: 11px;
      font-weight: 500; color: var(--ea-accent); vertical-align: 1px; }
    .live i { width: 6px; height: 6px; border-radius: 50%; background: currentColor;
      animation: ea-pulse 2s ease-in-out infinite; }
    .pill { display: inline-block; padding: 0 8px; border-radius: 10px; font-size: 12px; font-weight: 500;
      line-height: 20px; background: var(--ea-soft); color: var(--ea-fg); }
    .pill.t { color: var(--tone); background: transparent; box-shadow: inset 0 0 0 1px var(--tone); }
    .pill.success { --tone: var(--success-color, #43a047); }
    .pill.primary { --tone: var(--ea-accent); }
    .pill.error { --tone: var(--ea-err); }
    .pill.warning { --tone: var(--warning-color, #ffa600); }
    .pill.secondary { --tone: var(--ea-muted); }
    @supports (color: color-mix(in srgb, red 50%, blue)) {
      .pill.t { box-shadow: none; background: color-mix(in srgb, var(--tone) 16%, transparent);
        color: color-mix(in srgb, var(--tone) 78%, var(--ea-fg)); }
      .pill.secondary { background: var(--ea-soft); color: var(--ea-muted); }
    }

    /* ---------------------------------------------- empty / skeleton */
    .empty { padding: 40px var(--ea-pad); text-align: center; color: var(--ea-muted); }
    .empty > svg { width: 40px; height: 40px; opacity: 0.45; }
    .et { color: var(--ea-fg); font-size: 15px; font-weight: 500; margin-top: 8px; }
    .es { font-size: 13px; margin: 4px auto 0; max-width: 52ch; }
    .empty .btn { margin-top: 14px; }
    .skel .r { display: flex; gap: 20px; padding: 11px var(--ea-pad); border-bottom: 1px solid var(--ea-div); }
    .skel .r:last-child { border-bottom: 0; }
    .skel .b { height: 12px; border-radius: 6px; background: var(--ea-soft); animation: ea-fade 1.4s ease-in-out infinite; }

    /* -------------------------------------------------- more / footer */
    .more { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 8px;
      padding: 8px var(--ea-pad); border-top: 1px solid var(--ea-div); font-size: 12px; color: var(--ea-muted); }
    .more:empty { display: none; }
    .footer { padding: 8px var(--ea-pad) 12px; border-top: 1px solid var(--ea-div); font-size: 12px; color: var(--ea-muted); }
    .meta { display: flex; flex-wrap: wrap; align-items: baseline; row-gap: 2px; }
    .meta .it + .it::before { content: "·"; margin: 0 6px; opacity: 0.7; }
    .meta .upd { margin-left: auto; padding-left: 12px; white-space: nowrap; }
    .fnote { margin-top: 2px; font-style: italic; }

    /* ---------------------------------------------- compact (phones) */
    @container ea (max-width: 600px) {
      .body { --ea-pad: 12px; --ea-cx: 8px; --ea-cy: 5px; --ea-fs: 13px; --ea-grp-h: 20px; }
      .title { font-size: 16px; }
      .row { gap: 8px; }
      .seg { display: flex; width: 100%; box-sizing: border-box; }
      .seg button { flex: 1; padding: 5px 4px; }
      .dates { width: 100%; }
      .dates label { flex: 1; min-width: 0; }
      .dates input { flex: 1; min-width: 0; width: 100%; }
      .chip { height: 26px; padding: 0 10px; font-size: 12px; }
      thead th { font-size: 11px; }
      thead .unit { display: block; }
      .live b { display: none; }
      td.wr { min-width: 26ch; }
      .live { margin-left: 6px; }
    }

    @keyframes ea-spin { to { transform: rotate(360deg); } }
    @keyframes ea-slide { to { left: 100%; } }
    @keyframes ea-pulse { 50% { opacity: 0.35; } }
    @keyframes ea-fade { 50% { opacity: 0.45; } }
    @media (prefers-reduced-motion: reduce) {
      .ibtn.busy svg, .live i, .skel .b { animation: none; }
      .progress::after { animation: none; left: 0; width: 100%; opacity: 0.6; }
    }
  `;

  const SHELL = `
    <ha-card>
      <div class="root"><div class="body">
        <div class="head">
          <div class="title"></div>
          <div class="actions">
            <button type="button" class="ibtn" data-action="refresh" title="Refresh" aria-label="Refresh">${svg(ICON.refresh)}</button>
            <button type="button" class="btn" data-action="csv" title="Download everything loaded (all columns) as CSV">${svg(ICON.download)}<span>CSV</span></button>
          </div>
        </div>
        <div class="controls">
          <div class="row usage-only">
            <div class="seg" role="group" aria-label="Resolution"></div>
            <div class="dates">
              <label>From <input type="date" class="from" aria-label="From date"></label>
              <span class="dash" aria-hidden="true">–</span>
              <label>To <input type="date" class="to" aria-label="To date"></label>
            </div>
          </div>
          <div class="chips presets usage-only" role="group" aria-label="Quick ranges"></div>
          <div class="chips groups" role="group" aria-label="Column groups"></div>
        </div>
        <div class="alert-slot"></div>
        <div class="area">
          <div class="progress" aria-hidden="true"></div>
          <div class="wrap" tabindex="0" role="region" aria-label="Data table"></div>
        </div>
        <div class="more"></div>
        <div class="footer"></div>
      </div></div>
    </ha-card>`;

  const SKELETON = (() => {
    const widths = [[34, 14, 12, 16, 10], [30, 12, 14, 12, 12], [36, 16, 10, 14, 10], [32, 10, 12, 16, 14]];
    let html = '<div class="skel" aria-label="Loading">';
    for (let i = 0; i < 8; i += 1) {
      const w = widths[i % widths.length];
      html += `<div class="r">${w.map((x, j) => `<span class="b" style="width:${x}%;animation-delay:${(i + j) * 60}ms"></span>`).join("")}</div>`;
    }
    return `${html}</div>`;
  })();

  // --------------------------------------------------------------------- card

  class ElectricityAnalyzerDataCard extends HTMLElement {
    static getStubConfig() {
      return { title: "Electricity data", dataset: "usage", period: "day" };
    }

    /** Visual editor (Home Assistant 2024.11+ builds a form from this; older versions ignore it). */
    static getConfigForm() {
      const labels = {
        title: "Title",
        dataset: "Dataset",
        period: "Initial resolution (usage)",
        range_days: "Initial range in days (blank = default for the resolution)",
        show_controls: "Show controls",
        max_height: "Maximum table height",
      };
      return {
        schema: [
          { name: "title", selector: { text: {} } },
          {
            name: "dataset",
            selector: {
              select: {
                mode: "dropdown",
                options: [
                  { value: "usage", label: "Usage tables" },
                  { value: "readings", label: "Meter reading log" },
                ],
              },
            },
          },
          {
            name: "period",
            selector: {
              select: {
                mode: "dropdown",
                options: Object.entries(PERIODS).map(([value, p]) => ({ value, label: p.label })),
              },
            },
          },
          { name: "range_days", selector: { number: { min: 1, max: 36500, mode: "box" } } },
          { name: "show_controls", selector: { boolean: {} } },
          {
            name: "max_height",
            selector: { number: { min: 120, max: 3000, step: 10, mode: "box", unit_of_measurement: "px" } },
          },
        ],
        computeLabel: (s) => labels[s.name] ?? s.name,
      };
    }

    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._config = null;
      this._hass = null;
      this._dataset = "usage";
      this._period = "day";
      this._rangeMode = { type: "default" };
      this._range = undefined; // {start, end} | null (everything) | undefined (not resolved yet)
      this._data = null;
      this._dataMsg = null;
      this._error = null;
      this._loading = null; // null | "user" | "refresh" | "background"
      this._sort = null;
      this._prefs = {};
      this._cfgCols = null;
      this._rowLimit = ROW_PAGE;
      this._seq = 0;
      this._timer = null;
      this._dateTimer = null;
      this._retryTimer = null;
      this._retryDelay = 0;
      this._fetchedOnce = false;
      this._lastFetchAt = 0;
      this._updatedAt = null;
      this._resetScroll = false;
      this._fmt = new Map();
      this._localeSig = "";
      this._meta = { numeric: new Set(), signed: new Set(), wrap: new Set(), status: new Set() };

      this.shadowRoot.innerHTML = `<style>${STYLES}</style>${SHELL}`;
      const $ = (s) => this.shadowRoot.querySelector(s);
      this._els = {
        head: $(".head"),
        title: $(".title"),
        actions: $(".actions"),
        refresh: $('[data-action="refresh"]'),
        csv: $('[data-action="csv"]'),
        controls: $(".controls"),
        usageRow: $(".row.usage-only"),
        seg: $(".seg"),
        from: $("input.from"),
        to: $("input.to"),
        presets: $(".presets"),
        groups: $(".groups"),
        alert: $(".alert-slot"),
        progress: $(".progress"),
        wrap: $(".wrap"),
        more: $(".more"),
        footer: $(".footer"),
      };
      this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
      this.shadowRoot.addEventListener("change", (ev) => this._onDateChange(ev));
      this._onVisibility = () => {
        if (!document.hidden && this._due()) this._fetch("background");
      };
    }

    // ------------------------------------------------------- card API

    setConfig(config) {
      const cfg = normalizeConfig(config);
      const prev = this._config;
      this._config = cfg;
      const reload =
        !prev || prev.dataset !== cfg.dataset || prev.period !== cfg.period || prev.range_days !== cfg.range_days;
      if (reload) {
        this._seq += 1; // drop any answer still in flight for the old config
        this._dataset = cfg.dataset;
        this._period = cfg.period;
        this._rangeMode = { type: "default" };
        this._range = undefined;
        this._data = null;
        this._dataMsg = null;
        this._error = null;
        this._loading = null;
        this._sort = null;
        this._rowLimit = ROW_PAGE;
        this._fetchedOnce = false;
      }
      this._cfgCols = cfg.columns;
      this._loadPrefs();
      this._els.wrap.style.maxHeight = `${cfg.max_height}px`;
      this._renderAll();
      if (reload && this._hass) this._fetch("user");
    }

    set hass(hass) {
      const prev = this._hass;
      this._hass = hass;
      if (!hass) return;
      this.toggleAttribute("dark", !!hass.themes?.darkMode);
      const loc = hass.locale || {};
      const sig = [loc.language, hass.language, loc.number_format, loc.time_format, loc.time_zone, hass.config?.time_zone].join("|");
      if (sig !== this._localeSig) {
        this._localeSig = sig;
        this._fmt.clear();
        if (prev && this._config) this._renderAll();
      }
      if (this._config && !this._fetchedOnce) this._fetch("user");
    }

    get hass() {
      return this._hass;
    }

    getCardSize() {
      const h = this._config?.max_height ?? 560;
      return Math.max(4, Math.ceil(h / 50) + (this._config?.show_controls === false ? 1 : 3));
    }

    getGridOptions() {
      return { columns: "full", min_rows: 4 };
    }

    connectedCallback() {
      if (!this._timer) this._timer = setInterval(() => this._tick(), REFRESH_MS);
      document.addEventListener("visibilitychange", this._onVisibility);
      if (this._due()) this._fetch("background");
    }

    disconnectedCallback() {
      clearInterval(this._timer);
      this._timer = null;
      clearTimeout(this._dateTimer);
      this._dateTimer = null;
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
      document.removeEventListener("visibilitychange", this._onVisibility);
    }

    // ------------------------------------------------------- fetching

    _due() {
      return (
        this._fetchedOnce &&
        !this._loading &&
        !!this._hass &&
        !!this._config &&
        Date.now() - this._lastFetchAt >= REFRESH_MS
      );
    }

    _tick() {
      if (document.hidden || this._loading || !this._hass || !this._config) return;
      this._fetch("background");
    }

    _today() {
      return todayIn(this._hass?.config?.time_zone);
    }

    _defaultDays(period) {
      const c = this._config;
      return period === c.period && c.range_days ? c.range_days : PERIODS[period].days;
    }

    /** Relative ranges (default / presets) are re-resolved on every fetch, so midnight rolls over. */
    _resolveRange() {
      const m = this._rangeMode;
      if (m.type === "custom") return m.range;
      const today = this._today();
      if (m.type === "preset") {
        const r = presetRange(this._period, m.id, today);
        if (r !== undefined) return r;
      }
      const [s, e] = rangeForDays(this._period, this._defaultDays(this._period), today);
      return { start: isoDay(s), end: isoDay(e) };
    }

    _request() {
      if (this._dataset === "readings") return { type: "electricity_analyzer/readings" };
      this._range = this._resolveRange();
      const msg = { type: "electricity_analyzer/table", period: this._period };
      if (this._range) {
        msg.start = this._range.start;
        msg.end = this._range.end;
      }
      return msg;
    }

    async _fetch(kind) {
      const hass = this._hass;
      if (!hass || !this._config) return;
      this._fetchedOnce = true;
      const msg = this._request();
      const seq = ++this._seq;
      this._loading = kind;
      this._keepFocus(() => {
        this._renderControls();
        this._renderStatus();
        if (!this._data) this._renderTable();
      });

      let res;
      let err = null;
      try {
        if (typeof hass.callWS !== "function") {
          throw { code: "no_connection", message: "No connection to Home Assistant." };
        }
        res = await withTimeout(hass.callWS(msg), WS_TIMEOUT_MS);
      } catch (e) {
        err = e ?? { message: "Unknown error." };
      }
      if (seq !== this._seq) return; // superseded by a newer request

      this._loading = null;
      this._lastFetchAt = Date.now();
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
      if (err) {
        this._error = describeError(err);
        const code = String(err.code ?? err.error?.code ?? "");
        if (TRANSIENT.has(code) && this.isConnected) {
          this._retryDelay = this._retryDelay ? Math.min(this._retryDelay * 2, REFRESH_MS) : RETRY_FIRST_MS;
          this._retryTimer = setTimeout(() => {
            this._retryTimer = null;
            if (!this._loading && this._hass && this._config && this.isConnected) this._fetch("background");
          }, this._retryDelay);
        }
        // Never show the old table under an error for a *different* question.
        if (kind === "user" && JSON.stringify(msg) !== JSON.stringify(this._dataMsg)) {
          this._data = null;
          this._dataMsg = null;
        }
      } else {
        this._retryDelay = 0;
        this._data = normalizeResponse(res);
        this._dataMsg = msg;
        this._error = null;
        this._updatedAt = new Date();
        this._analyze();
        if (this._sort && !this._data.columns.some((c) => c.key === this._sort.key)) this._sort = null;
        if (kind === "user") this._rowLimit = ROW_PAGE;
      }
      this._renderAll();
      if (this._resetScroll && !err) {
        this._resetScroll = false;
        this._els.wrap.scrollTop = 0;
        this._els.wrap.scrollLeft = 0;
      }
    }

    // --------------------------------------------------------- events

    _onClick(ev) {
      const el = ev.target instanceof Element ? ev.target.closest("[data-action]") : null;
      if (!el || el.disabled) return;
      const d = el.dataset;
      this._keepFocus(() => {
        switch (d.action) {
          case "refresh":
            this._fetch("refresh");
            break;
          case "csv":
            this._downloadCsv();
            break;
          case "period":
            this._setPeriod(d.period);
            break;
          case "preset":
            this._rangeMode = { type: "preset", id: d.preset };
            this._resetScroll = true;
            this._fetch("user");
            break;
          case "group":
            this._toggleGroup(d.group);
            break;
          case "groups-reset":
            this._prefs = {};
            this._savePrefs();
            this._renderControls();
            this._renderTable();
            break;
          case "sort":
            this._toggleSort(d.key);
            break;
          case "more":
            this._rowLimit += ROW_PAGE;
            this._renderTable();
            this._renderFooter();
            break;
          case "all-rows":
            this._rowLimit = Infinity;
            this._renderTable();
            this._renderFooter();
            break;
          default:
            break;
        }
      });
    }

    _onDateChange(ev) {
      if (ev.target !== this._els.from && ev.target !== this._els.to) return;
      clearTimeout(this._dateTimer);
      this._dateTimer = setTimeout(() => {
        this._dateTimer = null;
        this._applyDates();
      }, DATE_DEBOUNCE_MS);
    }

    _applyDates() {
      const fv = this._els.from.value;
      const tv = this._els.to.value;
      let f = parseDay(fv);
      let t = parseDay(tv);
      if ((fv && !f) || (tv && !t)) return;
      // Typing a year digit by digit passes through 0002, 0020, 0202: wait for a real one.
      if ((f && f.getUTCFullYear() < 1990) || (t && t.getUTCFullYear() < 1990)) return;
      let range = null;
      if (f || t) {
        if (!t) t = this._today();
        if (!f) [f] = rangeForDays(this._period, this._defaultDays(this._period), t);
        if (f > t) [f, t] = [t, f];
        range = { start: isoDay(f), end: isoDay(t) };
      }
      if (sameRange(range, this._range)) {
        this._renderControls(); // normalise what the inputs show (e.g. swapped dates)
        return;
      }
      this._rangeMode = range ? { type: "custom", range } : { type: "preset", id: "all" };
      this._resetScroll = true;
      this._fetch("user");
    }

    _setPeriod(period) {
      if (!PERIODS[period] || period === this._period) return;
      this._period = period;
      this._rangeMode = { type: "default" };
      this._sort = null;
      this._rowLimit = ROW_PAGE;
      this._resetScroll = true;
      this._loadPrefs();
      this._fetch("user");
    }

    _toggleSort(key) {
      if (!key) return;
      // Numbers start biggest-first; text and the time label start oldest / A-first.
      const first = this._meta.numeric.has(key) ? "desc" : "asc";
      if (!this._sort || this._sort.key !== key) this._sort = { key, dir: first };
      else if (this._sort.dir === first) this._sort = { key, dir: first === "asc" ? "desc" : "asc" };
      else this._sort = null; // third click: back to the order the backend delivered
      this._renderTable();
    }

    _toggleGroup(group) {
      const cols = this._orderedColumns().filter((c) => c.group === group);
      const visible = cols.some((c) => this._colVisible(c));
      this._prefs[group] = visible ? "off" : "all";
      this._savePrefs();
      this._renderControls();
      this._renderTable();
    }

    /** Re-renders replace buttons; put keyboard focus back on the equivalent one. */
    _keepFocus(fn) {
      const a = this.shadowRoot.activeElement;
      let sel = null;
      if (a && a.dataset && a.dataset.action) {
        sel = `[data-action="${CSS.escape(a.dataset.action)}"]`;
        for (const k of ["period", "preset", "group", "key"]) {
          if (a.dataset[k] !== undefined) sel += `[data-${k}="${CSS.escape(a.dataset[k])}"]`;
        }
      }
      fn();
      if (sel && !this.shadowRoot.activeElement) {
        const el = this.shadowRoot.querySelector(sel);
        if (el) el.focus({ preventScroll: true });
      }
    }

    // ---------------------------------------------------- preferences

    _prefsKey() {
      return `${STORE}:groups:${this._dataset}${this._dataset === "usage" ? `:${this._period}` : ""}`;
    }

    _loadPrefs() {
      this._prefs = {};
      try {
        const raw = window.localStorage.getItem(this._prefsKey());
        const obj = raw ? JSON.parse(raw) : null;
        if (obj && typeof obj === "object" && !Array.isArray(obj)) {
          for (const [g, v] of Object.entries(obj)) if (v === "off" || v === "all") this._prefs[g] = v;
        }
      } catch (_) {
        /* storage blocked or corrupt: defaults */
      }
    }

    _savePrefs() {
      try {
        if (Object.keys(this._prefs).length) {
          window.localStorage.setItem(this._prefsKey(), JSON.stringify(this._prefs));
        } else {
          window.localStorage.removeItem(this._prefsKey());
        }
      } catch (_) {
        /* storage blocked: choices last for this page view only */
      }
    }

    // ------------------------------------------------------- columns

    /** "When" columns first, then configured columns in config order, then the rest. */
    _orderedColumns() {
      const cols = this._data ? this._data.columns : [];
      if (!this._cfgCols) {
        return cols.filter((c) => c.group === "When").concat(cols.filter((c) => c.group !== "When"));
      }
      const byKey = new Map(cols.map((c) => [c.key, c]));
      const listed = this._cfgCols.map((k) => byKey.get(k)).filter(Boolean);
      const seen = new Set(listed.map((c) => c.key));
      const unlisted = cols.filter((c) => !seen.has(c.key));
      const when = (c) => c.group === "When";
      return [
        ...listed.filter(when),
        ...unlisted.filter(when),
        ...listed.filter((c) => !when(c)),
        ...unlisted.filter((c) => !when(c)),
      ];
    }

    _colVisible(c) {
      if (c.group === "When") return true;
      const pref = this._prefs[c.group];
      if (pref === "off") return false;
      if (pref === "all") return true;
      return !this._cfgCols || this._cfgCols.includes(c.key);
    }

    _groupInfo() {
      const cols = this._orderedColumns();
      const names = [];
      for (const c of cols) if (c.group !== "When" && !names.includes(c.group)) names.push(c.group);
      const rank = (g) => {
        const i = GROUP_ORDER.indexOf(g);
        return i < 0 ? GROUP_ORDER.length : i;
      };
      names.sort((a, b) => rank(a) - rank(b));
      return names.map((name) => {
        const members = cols.filter((c) => c.group === name);
        return {
          name,
          active: members.some((c) => this._colVisible(c)),
          labels: members.map((c) => c.label).join(", "),
        };
      });
    }

    _analyze() {
      const d = this._data;
      const meta = { numeric: new Set(), signed: new Set(), wrap: new Set(), status: new Set() };
      const firstKey = d.columns[0]?.key;
      for (const c of d.columns) {
        if (/(^|_)status$/i.test(c.key)) meta.status.add(c.key);
        const eps = 0.5 * 10 ** -(c.decimals ?? 2);
        let firstType = null;
        let neg = false;
        let long = false;
        for (const r of d.rows) {
          const v = r[c.key];
          if (isBlank(v)) continue;
          const t = typeof v;
          if (!firstType) firstType = t;
          if (t === "number" && v <= -eps) neg = true;
          else if (t === "string" && v.length > 40) long = true;
        }
        const tv = d.totals ? d.totals[c.key] : null;
        if (typeof tv === "number" && tv <= -eps) neg = true;
        const numeric = firstType ? firstType === "number" : c.decimals !== null;
        if (numeric && c.key !== "label") meta.numeric.add(c.key);
        if (numeric && (neg || SIGNED_KEY.test(c.key))) meta.signed.add(c.key);
        if (long && c.key !== firstKey) meta.wrap.add(c.key);
      }
      this._meta = meta;
    }

    _sortedRows() {
      const rows = this._data ? this._data.rows : [];
      if (!this._sort) return rows;
      const { key, dir } = this._sort;
      const mul = dir === "asc" ? 1 : -1;
      // The label is display text ("Wed 7 Oct 2026"); its row key is the sortable form.
      const byRowKey = key === "label" && rows.some((r) => !isBlank(r.key));
      // Other display-formatted times ("Wed 1 Oct 2026 09:00") don't sort as text, but the
      // backend delivers rows newest first, so delivery order is the chronological order.
      const col = this._data.columns.find((c) => c.key === key);
      const chrono =
        !byRowKey &&
        col?.group === "When" &&
        rows.some((r) => !isBlank(r[key])) &&
        rows.every((r) => isBlank(r[key]) || (typeof r[key] === "string" && /\d/.test(r[key]) && !ISO_DATETIME.test(r[key])));
      const get = byRowKey ? (r) => r.key : (r) => r[key];
      const coll = this._collator();
      return rows
        .map((r, i) => ({ r, i, v: chrono ? (isBlank(r[key]) ? null : rows.length - i) : get(r) }))
        .sort((a, b) => {
          const na = isBlank(a.v);
          const nb = isBlank(b.v);
          if (na || nb) return na === nb ? a.i - b.i : na ? 1 : -1; // blanks always last
          let c;
          if (typeof a.v === "number" && typeof b.v === "number") c = a.v - b.v;
          else c = coll.compare(String(a.v), String(b.v));
          return c ? c * mul : a.i - b.i;
        })
        .map((x) => x.r);
    }

    // ------------------------------------------------------ formatting

    _lang() {
      return this._hass?.locale?.language || this._hass?.language || navigator.language;
    }

    /** Honour Home Assistant's number-format profile setting the same way the frontend does. */
    _numberLocale() {
      const lang = this._lang();
      switch (this._hass?.locale?.number_format) {
        case "comma_decimal":
          return ["en-US", {}];
        case "decimal_comma":
          return ["de", {}];
        case "space_comma":
          return ["fr", {}];
        case "none":
          return [lang, { useGrouping: false }];
        case "system":
          return [undefined, {}];
        default:
          return [lang, {}];
      }
    }

    _cached(key, make) {
      let f = this._fmt.get(key);
      if (!f) {
        f = make();
        this._fmt.set(key, f);
      }
      return f;
    }

    _nf(decimals) {
      return this._cached(`n:${decimals ?? "auto"}`, () => {
        const [loc, extra] = this._numberLocale();
        const opts =
          decimals === null || decimals === undefined
            ? { maximumFractionDigits: 2, ...extra }
            : { minimumFractionDigits: decimals, maximumFractionDigits: decimals, ...extra };
        try {
          return new Intl.NumberFormat(loc, opts);
        } catch (_) {
          return new Intl.NumberFormat(undefined, opts);
        }
      });
    }

    _collator() {
      return this._cached("collator", () => {
        try {
          return new Intl.Collator(this._lang(), { numeric: true, sensitivity: "base" });
        } catch (_) {
          return new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
        }
      });
    }

    _hour12() {
      const tf = this._hass?.locale?.time_format;
      return tf === "12" ? { hour12: true } : tf === "24" ? { hour12: false } : {};
    }

    _dtf(key, opts) {
      return this._cached(`d:${key}`, () => {
        const lang = this._hass?.locale?.time_format === "system" ? undefined : this._lang();
        try {
          return new Intl.DateTimeFormat(lang, opts);
        } catch (_) {
          return new Intl.DateTimeFormat(undefined, opts);
        }
      });
    }

    _timeText(date) {
      return this._dtf("time", { hour: "2-digit", minute: "2-digit", ...this._hour12() }).format(date);
    }

    _isoDateTimeText(s) {
      if (!ISO_DATETIME.test(s)) return null;
      const d = new Date(s.replace(" ", "T"));
      if (Number.isNaN(d.getTime())) return null;
      const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/.test(s);
      const tz = zoned && this._hass?.locale?.time_zone === "server" ? this._hass?.config?.time_zone : undefined;
      return this._dtf(`dt:${tz || ""}`, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        ...this._hour12(),
        ...(tz ? { timeZone: tz } : {}),
      }).format(d);
    }

    _int(n) {
      return this._nf(0).format(n);
    }

    _numHtml(col, v) {
      if (!Number.isFinite(v)) return NIL;
      const d = col.decimals;
      const zero = Math.abs(v) < 0.5 * 10 ** -(d ?? 2);
      const neg = v < 0 && !zero;
      const sign = neg ? "−" : !zero && this._meta.signed.has(col.key) ? "+" : "";
      const cur = col.unit === "€" ? "€" : "";
      const txt = `${sign}${cur}${this._nf(d).format(zero ? 0 : Math.abs(v))}`;
      return neg ? `<span class="neg">${esc(txt)}</span>` : esc(txt);
    }

    _valueHtml(col, v) {
      if (isBlank(v)) return NIL;
      if (typeof v === "number") return this._numHtml(col, v);
      if (typeof v === "boolean") return v ? "Yes" : "No";
      if (typeof v === "object") return esc(JSON.stringify(v));
      const s = String(v);
      if (this._meta.status.has(col.key)) {
        const tone = STATUS_TONE[s.toLowerCase()];
        const text = s.replace(/_/g, " ");
        return `<span class="pill${tone ? ` t ${tone}` : ""}" title="${esc(s)}">${esc(text.charAt(0).toUpperCase() + text.slice(1))}</span>`;
      }
      const dt = this._isoDateTimeText(s);
      if (dt) return `<span title="${esc(s)}">${esc(dt)}</span>`;
      return esc(s);
    }

    /** The range actually shown (the backend may have chosen it, or left it open). */
    _effectiveRange() {
      const d = this._data;
      if (!d) return null;
      let start = d.start;
      let end = d.end;
      const keyToDay = (k) => {
        const s = String(k ?? "");
        if (/^\d{4}$/.test(s)) return `${s}-01-01`;
        if (/^\d{4}-\d{2}$/.test(s)) return `${s}-01`;
        return /^\d{4}-\d{2}-\d{2}/.test(s) ? s.slice(0, 10) : null;
      };
      if ((!start || !end) && d.rows.length) {
        const keys = d.rows.map((r) => keyToDay(r.key)).filter(Boolean).sort();
        if (!start && keys.length) start = keys[0];
        if (!end) end = isoDay(this._today());
      }
      return start && end ? { start, end } : null;
    }

    _rangeText() {
      if (this._dataset !== "usage") return "";
      const r = this._effectiveRange();
      if (!r) return "";
      const s = parseDay(r.start);
      const e = parseDay(r.end);
      if (!s || !e) return esc(`${r.start} – ${r.end}`);
      const p = this._data.period || this._period;
      const opts =
        p === "year"
          ? { year: "numeric" }
          : p === "month"
            ? { month: "short", year: "numeric" }
            : { day: "numeric", month: "short", year: "numeric" };
      const f = this._dtf(`range:${p}`, { ...opts, timeZone: "UTC" });
      if (f.format(s) === f.format(e)) return esc(f.format(s));
      try {
        return esc(f.formatRange(s, e));
      } catch (_) {
        return esc(`${f.format(s)} – ${f.format(e)}`);
      }
    }

    // ------------------------------------------------------- rendering

    _renderAll() {
      if (!this._config) return;
      this._keepFocus(() => {
        this._renderHead();
        this._renderControls();
        this._renderStatus();
        this._renderTable();
        this._renderFooter();
      });
    }

    _renderHead() {
      const c = this._config;
      this._els.title.textContent = c.title;
      this._els.title.hidden = !c.title;
      this._els.actions.hidden = !c.show_controls;
      this._els.head.hidden = !c.title && !c.show_controls;
    }

    _renderControls() {
      const c = this._config;
      if (!c) return;
      const els = this._els;
      els.controls.hidden = !c.show_controls;
      if (!c.show_controls) return;
      const usage = this._dataset === "usage";
      els.usageRow.hidden = !usage;
      els.presets.hidden = !usage;

      if (usage) {
        els.seg.innerHTML = Object.entries(PERIODS)
          .map(
            ([p, def]) =>
              `<button type="button" data-action="period" data-period="${p}" aria-pressed="${p === this._period}">${esc(def.label)}</button>`
          )
          .join("");

        const today = this._today();
        const current = this._range !== undefined ? this._range : this._resolveRange();
        const shown = current || this._effectiveRange() || { start: "", end: "" };
        const active = this.shadowRoot.activeElement;
        for (const [input, value] of [
          [els.from, shown.start],
          [els.to, shown.end],
        ]) {
          input.max = isoDay(today);
          if (input !== active && !this._dateTimer && input.value !== value) input.value = value;
        }

        els.presets.innerHTML = PRESETS[this._period]
          .map((p) => {
            const r = presetRange(this._period, p.id, today);
            const on = sameRange(r, current);
            return `<button type="button" class="chip" data-action="preset" data-preset="${esc(p.id)}" aria-pressed="${on}">${esc(p.label)}</button>`;
          })
          .join("");
      }

      const groups = this._groupInfo();
      els.groups.hidden = groups.length < 2;
      els.groups.innerHTML = groups.length < 2
        ? ""
        : `<span class="lead">Columns</span>${groups
            .map(
              (g) =>
                `<button type="button" class="chip" data-action="group" data-group="${esc(g.name)}" aria-pressed="${g.active}" title="${esc(`${g.active ? "Hide" : "Show"}: ${g.labels}`)}">${g.active ? svg(ICON.check) : ""}${esc(g.name)}</button>`
            )
            .join("")}${Object.keys(this._prefs).length ? '<button type="button" class="link" data-action="groups-reset" title="Forget column choices for this view">Reset</button>' : ""}`;
    }

    _renderStatus() {
      const els = this._els;
      const busy = !!this._loading;
      els.progress.classList.toggle("on", busy);
      els.refresh.classList.toggle("busy", busy);
      els.wrap.classList.toggle("dim", this._loading === "user" && !!this._data);
      els.wrap.setAttribute("aria-busy", String(busy));
      els.csv.disabled = !(this._data && this._data.rows.length);
      els.alert.innerHTML = this._error && this._data ? this._errorHtml(false) : "";
    }

    _errorHtml(big) {
      const e = this._error;
      const stale = !big && this._updatedAt ? ` Showing data from ${this._timeText(this._updatedAt)}.` : "";
      const code = e.code ? `<div class="ac">${esc(e.code)}${e.message ? `: ${esc(e.message)}` : ""}</div>` : "";
      return `<div class="alert${big ? " big" : ""}" role="alert">${svg(ICON.alert)}<div class="am"><div class="at">${esc(big ? e.title : `Couldn't refresh. ${e.title}`)}</div><div class="ad">${esc(`${e.detail || ""}${stale}`)}</div>${code}</div><button type="button" class="btn" data-action="refresh">Retry</button></div>`;
    }

    _emptyHtml() {
      const usage = this._dataset === "usage";
      const note = this._data?.note ? `<div class="es">${esc(this._data.note)}</div>` : "";
      const widen =
        usage && this._range !== null
          ? '<button type="button" class="btn" data-action="preset" data-preset="all">Show everything</button>'
          : "";
      const title = usage ? "No data for this range yet" : "No meter readings have been submitted yet";
      return `<div class="empty">${svg(ICON.table)}<div class="et">${title}</div>${note}${widen}</div>`;
    }

    _renderTable() {
      const els = this._els;
      const d = this._data;
      if (!d) {
        els.wrap.innerHTML = this._error && !this._loading ? this._errorHtml(true) : SKELETON;
        els.more.innerHTML = "";
        return;
      }
      if (!d.rows.length) {
        els.wrap.innerHTML = this._emptyHtml();
        els.more.innerHTML = "";
        return;
      }

      const cols = this._orderedColumns().filter((c) => this._colVisible(c));
      const all = this._sortedRows();
      const rows = all.slice(0, this._rowLimit);
      const meta = this._meta;

      // Group bands: runs of neighbouring columns from the same group.
      const runs = [];
      for (const c of cols) {
        const last = runs[runs.length - 1];
        if (last && last.group === c.group) last.span += 1;
        else runs.push({ group: c.group, span: 1 });
      }
      const grouped = runs.length > 1;
      if (grouped && runs[0].span > 1) {
        // The first column is sticky; a band cell spanning it and its neighbours would
        // stay pinned at full width and cover the bands scrolling under it.
        runs.splice(0, 1, { group: runs[0].group, span: 1 }, { group: runs[0].group, span: runs[0].span - 1, cont: true });
      }
      const cls = cols.map((c, i) => {
        const k = [];
        if (i === 0) k.push("sc");
        if (meta.numeric.has(c.key)) k.push("n");
        if (i > 0 && c.group !== cols[i - 1].group) k.push("gs");
        if (meta.wrap.has(c.key)) k.push("wr");
        return k.length ? ` class="${k.join(" ")}"` : "";
      });

      let html = `<table class="${grouped ? "grouped" : ""}"><thead>`;
      if (grouped) {
        html += `<tr class="grps">${runs
          .map((r, i) => {
            const k = i === 0 ? "sc" : r.cont ? "" : "gs";
            const text = r.group === "When" || r.cont ? "" : r.group;
            return `<th scope="colgroup" colspan="${r.span}"${k ? ` class="${k}"` : ""}>${esc(text)}</th>`;
          })
          .join("")}</tr>`;
      }
      html += `<tr class="cols">${cols.map((c, i) => this._thHtml(c, cls[i])).join("")}</tr></thead><tbody>`;

      for (const r of rows) {
        html += r.partial ? '<tr class="partial">' : "<tr>";
        for (let i = 0; i < cols.length; i += 1) {
          let cell = this._valueHtml(cols[i], r[cols[i].key]);
          if (i === 0 && r.partial) {
            cell += '<span class="live" title="Still in progress: this period is not complete yet"><i></i><b>in progress</b></span>';
          }
          html += `<td${cls[i]}>${cell}</td>`;
        }
        html += "</tr>";
      }
      html += "</tbody>";

      if (d.totals) {
        html += `<tfoot><tr>${cols
          .map((c, i) => {
            let v = d.totals[c.key];
            if (i === 0) v = isBlank(v) ? d.totals.label ?? "Total" : v;
            return `<td${cls[i]}>${isBlank(v) ? "" : i === 0 ? esc(v) : this._valueHtml(c, v)}</td>`;
          })
          .join("")}</tr></tfoot>`;
      }
      els.wrap.innerHTML = `${html}</table>`;

      const rest = all.length - rows.length;
      els.more.innerHTML =
        rest > 0
          ? `<span>${esc(this._int(rest))} more ${rest === 1 ? "row" : "rows"}</span><button type="button" class="btn" data-action="more">Show ${esc(this._int(Math.min(ROW_PAGE, rest)))} more</button><button type="button" class="btn" data-action="all-rows">Show all</button>`
          : "";
    }

    _thHtml(col, cls) {
      const dir = this._sort && this._sort.key === col.key ? this._sort.dir : null;
      const first = this._meta.numeric.has(col.key) ? "desc" : "asc";
      const icon = (dir || first) === "asc" ? ICON.up : ICON.down;
      const unit = col.unit && col.unit !== "€" ? ` <span class="unit">(${esc(col.unit)})</span>` : "";
      const name = `${col.label}${col.unit ? ` (${col.unit})` : ""}`;
      const tip = dir
        ? `${name}: sorted ${dir === "asc" ? "ascending" : "descending"}. Click to ${dir === first ? "reverse" : "restore the original order"}.`
        : `Sort by ${name}`;
      const aria = dir ? ` aria-sort="${dir === "asc" ? "ascending" : "descending"}"` : "";
      return `<th scope="col"${cls}${aria}><button type="button" class="sort" data-action="sort" data-key="${esc(col.key)}" title="${esc(tip)}"><span class="hl">${esc(col.label)}${unit}</span>${svg(icon, "ar")}</button></th>`;
    }

    _renderFooter() {
      const d = this._data;
      const items = [];
      if (d) {
        const n = d.rows.length;
        const noun = this._dataset === "readings" ? (n === 1 ? "entry" : "entries") : n === 1 ? "row" : "rows";
        const shown = Math.min(n, this._rowLimit);
        items.push(
          shown < n
            ? `Showing ${esc(this._int(shown))} of ${esc(this._int(n))} ${noun}`
            : `${esc(this._int(n))} ${noun}`
        );
        const range = this._rangeText();
        if (range) items.push(range);
      }
      const upd = this._updatedAt ? `Updated ${this._timeText(this._updatedAt)}` : "";
      // The empty state already shows the note, so it only goes here under real rows.
      const note = d && d.note && d.rows.length ? `<div class="fnote">${esc(d.note)}</div>` : "";
      this._els.footer.innerHTML =
        `<div class="meta">${items.map((x) => `<span class="it">${x}</span>`).join("")}${upd ? `<span class="upd">${esc(upd)}</span>` : ""}</div>${note}`;
      this._els.footer.hidden = !items.length && !upd && !note;
    }

    // ------------------------------------------------------------- CSV

    _downloadCsv() {
      const d = this._data;
      if (!d || !d.rows.length) return;
      const csv = buildCsv(d.columns, this._sortedRows(), d.totals);
      const msg = this._dataMsg || {};
      const name =
        this._dataset === "readings"
          ? `electricity-readings-${isoDay(this._today())}.csv`
          : `electricity-${msg.period || this._period}-${msg.start || "all"}-${msg.end || "all"}.csv`;
      // The BOM makes Excel read UTF-8 (so "€" survives); other tools ignore it.
      const blob = new Blob(["﻿", csv], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.rel = "noopener";
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    }
  }

  if (!customElements.get(TAG)) {
    customElements.define(TAG, ElectricityAnalyzerDataCard);
    console.info(`%c ELECTRICITY-ANALYZER-DATA-CARD %c ${VERSION} `, "color:#fff;background:#03a9f4;font-weight:600", "");
  }
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c && c.type === TAG)) {
    window.customCards.push({
      type: TAG,
      name: "Electricity Analyzer data",
      description:
        "Browse the Electricity Analyzer database: hourly, daily, monthly and yearly energy, cost, power, " +
        "power quality and ESB comparison, plus the meter-reading log. Sort, hide column groups, export CSV.",
      preview: false,
    });
  }
})();
