"""The integration's own database: every hour, kept forever, readable by a human.

WHY THIS EXISTS WHEN THE RECORDER ALREADY KEEPS STATISTICS

The recorder keeps hourly statistics per entity. What it cannot keep is anything
that needs the two clamps *together* and the tariff: the day/night split, the
rest-of-house figure, the cost of each hour at the rate in force at the time, the
estimated meter registers, and ESB's own figure for the same hour. This store holds
exactly that, one row per hour, and is the single source for the dashboard's tables,
the derived long-term statistics, the forecasts and the meter-reading estimate.

READABLE BY DESIGN

The previous version of this project kept a cache nobody could see. This one is
meant to be read: column names say what they hold and in which unit, every row
carries its local hour and local date as text next to the UTC epoch, and the views
`v_daily` and `v_monthly` give the natural rollups to anyone who opens the file in a
SQLite browser. Inside Home Assistant the same data is served to the dashboard's
data card over the websocket API, so opening the file is never required.

Deliberately synchronous and free of Home Assistant imports: sqlite3 blocks, so the
integration calls these methods from an executor thread.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .tariff import IRELAND, Tariff
from .weather import COLUMNS as WEATHER_COLUMNS

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1
UTC = timezone.utc

# Every per-hour figure, in table order. Grouped and labelled for the data card in
# `columns.py`; listed here once so the insert, the select and the CSV agree.
HOURLY_FIELDS: tuple[str, ...] = (
    "band",
    "house_kwh",
    "meter_kwh",
    "meter_factor",
    "hall_kwh",
    "hall_factor",
    "rest_kwh",
    "unit_rate_eur",
    "house_cost_eur",
    "hall_cost_eur",
    "rest_cost_eur",
    "fixed_cost_eur",
    "house_avg_w",
    "house_min_w",
    "house_max_w",
    "hall_avg_w",
    "hall_min_w",
    "hall_max_w",
    "house_avg_va",
    "hall_avg_va",
    "house_pf",
    "house_pf_min",
    "hall_pf",
    "hall_pf_min",
    "voltage_avg",
    "voltage_min",
    "voltage_max",
    "hall_voltage_avg",
    "hall_voltage_min",
    "hall_voltage_max",
    "house_current_avg",
    "house_current_max",
    "hall_current_avg",
    "hall_current_max",
    "frequency_avg",
    "hall_frequency_avg",
    "house_returned_kwh",
    "hall_over_house",
    "tariff_from",
    "source",
    "meter_source",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per UTC hour. hour_utc is the epoch of the hour's START.
CREATE TABLE IF NOT EXISTS hourly (
    hour_utc           INTEGER PRIMARY KEY,
    local_hour         TEXT NOT NULL,     -- 'YYYY-MM-DD HH:00' Europe/Dublin (+ ' (2)' on the repeated autumn hour)
    local_date         TEXT NOT NULL,     -- 'YYYY-MM-DD' Europe/Dublin
    band               TEXT NOT NULL,     -- 'day' | 'night' (meter register this hour lands in)
    house_kwh          REAL,              -- full-house clamp (the TOTAL), as measured
    meter_kwh          REAL,              -- house_kwh x meter_factor: what the utility meter will have counted
    meter_factor       REAL,              -- meter kWh per clamp kWh, learned from real meter readings
    hall_kwh           REAL,              -- hall clamp (inside the total), as measured
    hall_factor        REAL,              -- correction keeping hall <= house (hallfix.py); corrected hall = hall_kwh x hall_factor
    rest_kwh           REAL,              -- max(0, house - corrected hall)
    unit_rate_eur      REAL,              -- €/kWh incl VAT in force this hour
    house_cost_eur     REAL,              -- meter_kwh * unit rate (the bill is the meter's, not the clamp's)
    hall_cost_eur      REAL,
    rest_cost_eur      REAL,
    fixed_cost_eur     REAL,              -- this hour's share of standing charge + PSO
    house_avg_w        REAL, house_min_w REAL, house_max_w REAL,
    hall_avg_w         REAL, hall_min_w  REAL, hall_max_w  REAL,
    house_avg_va       REAL,
    hall_avg_va        REAL,
    house_pf           REAL, house_pf_min REAL,
    hall_pf            REAL, hall_pf_min  REAL,
    voltage_avg        REAL, voltage_min REAL, voltage_max REAL,             -- full-house clamp
    hall_voltage_avg   REAL, hall_voltage_min REAL, hall_voltage_max REAL,
    house_current_avg  REAL, house_current_max REAL,
    hall_current_avg   REAL, hall_current_max  REAL,
    frequency_avg      REAL,
    hall_frequency_avg REAL,
    house_returned_kwh REAL,
    hall_over_house    INTEGER NOT NULL DEFAULT 0,  -- 1 when the hall clamp read more than the house clamp
    tariff_from        TEXT,              -- valid_from of the tariff used to price this hour
    source             TEXT NOT NULL DEFAULT 'shelly',
    meter_source       TEXT,              -- what meter_kwh (and so the money) rests on: 'esb' = ESB's own data (final), else the clamp x factor (provisional)
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hourly_local_date ON hourly(local_date);

-- ESB Networks half-hourly intervals (start instant), kWh.
CREATE TABLE IF NOT EXISTS esb_intervals (
    start_utc   INTEGER PRIMARY KEY,
    local_start TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    band        TEXT NOT NULL,
    kwh         REAL NOT NULL,
    source      TEXT NOT NULL DEFAULT 'esb'
);
CREATE INDEX IF NOT EXISTS esb_local_date ON esb_intervals(local_date);

-- Register readings reported by ESB (billed reads), one per register per date.
CREATE TABLE IF NOT EXISTS esb_register_reads (
    read_date TEXT NOT NULL,
    register  TEXT NOT NULL,   -- 'day' | 'night'
    reading   REAL NOT NULL,
    kind      TEXT,
    PRIMARY KEY (read_date, register)
);

-- Dated tariff periods, every money figure INCLUDING VAT.
CREATE TABLE IF NOT EXISTS tariffs (
    valid_from          TEXT PRIMARY KEY,
    supplier            TEXT,
    plan                TEXT,
    day_rate_eur        REAL NOT NULL,
    night_rate_eur      REAL NOT NULL,
    standing_eur_year   REAL NOT NULL,
    pso_eur_year        REAL NOT NULL,
    night_start_hour    INTEGER NOT NULL,
    night_end_hour      INTEGER NOT NULL,
    night_clock         TEXT NOT NULL
);

-- A real reading of the meter, and when it was taken. The newest is the anchor the
-- running estimate is built on; older ones are kept as a record.
CREATE TABLE IF NOT EXISTS meter_anchors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT NOT NULL,     -- ISO 8601 UTC
    day_kwh     REAL NOT NULL,
    night_kwh   REAL NOT NULL,
    origin      TEXT NOT NULL,     -- setup | manual | submitted | sse
    created_at  TEXT NOT NULL
);

-- Every reading prepared or sent to the supplier, and what happened.
CREATE TABLE IF NOT EXISTS meter_submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    read_at     TEXT NOT NULL,
    day_kwh     INTEGER NOT NULL,
    night_kwh   INTEGER NOT NULL,
    origin      TEXT NOT NULL,     -- estimate | manual
    mode        TEXT NOT NULL,     -- dry_run | live
    status      TEXT NOT NULL,     -- dry_run | submitted | failed | blocked
    message     TEXT,
    payload     TEXT,
    response    TEXT
);

-- Hourly weather from Open-Meteo. `source` is 'archive' (observed/reanalysis) or
-- 'forecast'; an archive value always replaces a forecast one for the same hour.
CREATE TABLE IF NOT EXISTS weather_hourly (
    hour_utc    INTEGER PRIMARY KEY,
    local_hour  TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    source      TEXT NOT NULL,
    temperature REAL,\n    apparent_temperature REAL,\n    humidity REAL,\n    dew_point REAL,\n    wind_speed REAL,\n    wind_gusts REAL,\n    wind_direction REAL,\n    precipitation REAL,\n    rain REAL,\n    snowfall REAL,\n    snow_depth REAL,\n    cloud_cover REAL,\n    solar_radiation REAL,\n    sunshine REAL,\n    pressure REAL,\n    soil_temperature REAL
);
CREATE INDEX IF NOT EXISTS weather_local_date ON weather_hourly(local_date);

-- Every model's predictions, made at the start of each day for the week ahead, so
-- they can be scored against what happened and the ensemble's weights tuned.
CREATE TABLE IF NOT EXISTS forecast_log (
    made_on   TEXT NOT NULL,
    target    TEXT NOT NULL,
    model     TEXT NOT NULL,
    kwh       REAL NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'live',   -- live | backtest
    PRIMARY KEY (made_on, target, model)
);

-- The learned "what drives usage" weights, one row per feature per night, so how
-- they move over time can be charted.
CREATE TABLE IF NOT EXISTS learned_weights (
    fitted_on TEXT NOT NULL,
    model     TEXT NOT NULL,
    feature   TEXT NOT NULL,
    weight    REAL NOT NULL,
    PRIMARY KEY (fitted_on, model, feature)
);

-- Minute-by-minute aggregates of the clamps' (about once a second) readings, kept for good.
CREATE TABLE IF NOT EXISTS minutely (
    minute_utc   INTEGER PRIMARY KEY,
    local_time   TEXT NOT NULL,
    house_w REAL, house_w_min REAL, house_w_max REAL,
    hall_w REAL, hall_w_max REAL,
    rest_w REAL,
    house_pf REAL, hall_pf REAL,
    voltage REAL, voltage_min REAL, voltage_max REAL,
    house_wh REAL, hall_wh REAL,
    samples INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    source     TEXT PRIMARY KEY,
    cookies    TEXT NOT NULL,
    saved_at   TEXT NOT NULL,
    ok_at      TEXT
);

CREATE TABLE IF NOT EXISTS fetch_state (
    source        TEXT PRIMARY KEY,
    last_attempt  TEXT,
    last_success  TEXT,
    failures      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);

DROP VIEW IF EXISTS v_daily;
DROP VIEW IF EXISTS v_monthly;
DROP VIEW IF EXISTS v_esb_daily;

-- Daily rollup for anyone browsing the file. PF over a period is total real power
-- over total apparent power, not an average of hourly ratios.
CREATE VIEW v_daily AS
SELECT
    local_date,
    COUNT(*)                                             AS hours,
    ROUND(SUM(house_kwh), 3)                             AS house_kwh,
    ROUND(SUM(meter_kwh), 3)                             AS meter_kwh,
    ROUND(SUM(CASE WHEN band = 'day'   THEN house_kwh END), 3) AS day_kwh,
    ROUND(SUM(CASE WHEN band = 'night' THEN house_kwh END), 3) AS night_kwh,
    ROUND(SUM(hall_kwh * COALESCE(hall_factor, 1)), 3)   AS hall_kwh,
    ROUND(SUM(rest_kwh), 3)                              AS rest_kwh,
    ROUND(SUM(house_cost_eur), 4)                        AS energy_cost_eur,
    ROUND(SUM(fixed_cost_eur), 4)                        AS fixed_cost_eur,
    ROUND(SUM(house_cost_eur) + SUM(fixed_cost_eur), 4)  AS total_cost_eur,
    ROUND(SUM(hall_cost_eur), 4)                         AS hall_cost_eur,
    ROUND(MAX(house_max_w), 0)                           AS house_peak_w,
    ROUND(MIN(house_min_w), 0)                           AS house_min_w,
    ROUND(SUM(house_avg_w) / NULLIF(SUM(house_avg_va), 0), 3) AS house_pf,
    ROUND(SUM(hall_avg_w)  / NULLIF(SUM(hall_avg_va), 0), 3)  AS hall_pf,
    ROUND(MIN(voltage_min), 1)                           AS voltage_min,
    ROUND(AVG(voltage_avg), 1)                           AS voltage_avg,
    ROUND(MAX(voltage_max), 1)                           AS voltage_max,
    SUM(hall_over_house)                                 AS hall_over_house_hours
FROM hourly
GROUP BY local_date;

CREATE VIEW v_monthly AS
SELECT
    substr(local_date, 1, 7)                             AS month,
    COUNT(DISTINCT local_date)                           AS days,
    ROUND(SUM(house_kwh), 2)                             AS house_kwh,
    ROUND(SUM(meter_kwh), 2)                             AS meter_kwh,
    ROUND(SUM(CASE WHEN band = 'day'   THEN house_kwh END), 2) AS day_kwh,
    ROUND(SUM(CASE WHEN band = 'night' THEN house_kwh END), 2) AS night_kwh,
    ROUND(SUM(hall_kwh * COALESCE(hall_factor, 1)), 2)   AS hall_kwh,
    ROUND(SUM(rest_kwh), 2)                              AS rest_kwh,
    ROUND(SUM(house_cost_eur) + SUM(fixed_cost_eur), 2)  AS total_cost_eur,
    ROUND(SUM(hall_cost_eur), 2)                         AS hall_cost_eur,
    ROUND(SUM(house_avg_w) / NULLIF(SUM(house_avg_va), 0), 3) AS house_pf
FROM hourly
GROUP BY substr(local_date, 1, 7);

CREATE VIEW v_esb_daily AS
SELECT
    local_date,
    COUNT(*)                                             AS intervals,
    ROUND(SUM(kwh), 3)                                   AS esb_kwh,
    ROUND(SUM(CASE WHEN band = 'day'   THEN kwh END), 3) AS esb_day_kwh,
    ROUND(SUM(CASE WHEN band = 'night' THEN kwh END), 3) AS esb_night_kwh
FROM esb_intervals
GROUP BY local_date;
"""


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _epoch(instant: datetime) -> int:
    return int(instant.astimezone(UTC).timestamp())


def _from_epoch(value: int) -> datetime:
    return datetime.fromtimestamp(int(value), UTC)


def local_hour_label(hour_start: datetime) -> str:
    """'YYYY-MM-DD HH:00' local, marking the second pass of the autumn repeat hour."""
    local = hour_start.astimezone(IRELAND)
    label = local.strftime("%Y-%m-%d %H:00")
    # The previous UTC hour having the same local label means this is the second
    # pass through the autumn repeat (01:00 happens twice).
    earlier = (hour_start - timedelta(hours=1)).astimezone(IRELAND)
    if earlier.strftime("%Y-%m-%d %H") == local.strftime("%Y-%m-%d %H"):
        label += " (2)"
    return label


@dataclass
class FetchState:
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    failures: int = 0
    last_error: str | None = None

    def is_due(self, interval: timedelta, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if self.failures:
            # Exponential backoff that survives a restart, capped at a day.
            wait = min(interval * (2 ** min(self.failures - 1, 5)), timedelta(days=1))
            return self.last_attempt is None or now - self.last_attempt >= wait
        return self.last_success is None or now - self.last_success >= interval

    def retry_at(self, wait: timedelta) -> datetime | None:
        """After a failure: when the next attempt may be made (fixed wait, survives a restart)."""
        if not self.failures or self.last_attempt is None:
            return None
        return self.last_attempt + wait

    def waiting(self, wait: timedelta, *, now: datetime | None = None) -> bool:
        """Inside the wait after a failure: nothing may be tried."""
        at = self.retry_at(wait)
        return at is not None and (now or datetime.now(UTC)) < at


class Store:
    """SQLite store. Thread-safe; call from an executor."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # Durability over speed: write-ahead log plus a full fsync on every commit, so a
        # power cut can lose at most the transaction in flight, never a committed hour.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        with self._tx() as c:
            c.executescript(_SCHEMA.split("DROP VIEW IF EXISTS v_daily;")[0])
            have = {row[1] for row in c.execute("PRAGMA table_info(hourly)")}
            for column in (
                "meter_kwh", "meter_factor", "hall_factor", "hall_voltage_avg", "hall_voltage_min",
                "hall_voltage_max", "house_current_avg", "hall_current_avg", "hall_frequency_avg",
            ):
                if column not in have:
                    c.execute(f"ALTER TABLE hourly ADD COLUMN {column} REAL")
            if "meter_source" not in have:  # 1.0.2: existing rows read as provisional (NULL)
                c.execute("ALTER TABLE hourly ADD COLUMN meter_source TEXT")
            c.executescript("DROP VIEW IF EXISTS v_daily;" + _SCHEMA.split("DROP VIEW IF EXISTS v_daily;")[1])
            c.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as exc:
                # Not fatal: the WAL is checkpointed on the next open instead.
                LOG.debug("WAL checkpoint on close skipped: %s", exc)
            self._db.close()

    # ---------------------------------------------------------- robustness

    def integrity_ok(self) -> tuple[bool, str]:
        """SQLite's own structural check of every page and index."""
        with self._lock:
            result = self._db.execute("PRAGMA quick_check").fetchall()
        text = "; ".join(str(r[0]) for r in result)
        return text == "ok", text

    def backup_to(self, target: str | Path) -> int:
        """A consistent copy taken while the database is in use (SQLite online backup)."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".partial")
        with self._lock:
            dest = sqlite3.connect(str(tmp))
            try:
                self._db.backup(dest)
                ok = dest.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            finally:
                dest.close()
        if not ok:
            tmp.unlink(missing_ok=True)
            raise sqlite3.DatabaseError("backup failed its own integrity check")
        tmp.replace(target)
        return target.stat().st_size

    def export_csv(self, folder: str | Path, tables: Iterable[str]) -> dict[str, int]:
        """Every row of the given tables to CSV files, one per table. Returns row counts."""
        import csv

        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        for table in tables:
            with self._lock:
                cur = self._db.execute(f"SELECT * FROM {table}")  # noqa: S608 - fixed table names
                columns = [d[0] for d in cur.description]
                rows = cur.fetchall()
            with open(folder / f"{table}.csv", "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(columns)
                writer.writerows(rows)
            counts[table] = len(rows)
        return counts

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, tuple(params)).fetchall()]

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        rows = self._rows("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self._tx() as c:
            if value is None:
                c.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                c.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_meta(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.set_meta(key, json.dumps(value, separators=(",", ":"), default=str))

    # ---------------------------------------------------------------- hourly

    def upsert_hours(self, rows: list[dict[str, Any]]) -> int:
        """Insert or replace whole hours. Each row needs `hour_utc` (aware datetime)."""
        if not rows:
            return 0
        now = _now_iso()
        cols = ("hour_utc", "local_hour", "local_date", *HOURLY_FIELDS, "updated_at")
        sql = (
            f"INSERT OR REPLACE INTO hourly ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})"
        )
        values = []
        for row in rows:
            start: datetime = row["hour_utc"]
            local = start.astimezone(IRELAND)
            values.append(
                (
                    _epoch(start),
                    local_hour_label(start),
                    local.date().isoformat(),
                    *(row.get(f) for f in HOURLY_FIELDS),
                    now,
                )
            )
        with self._tx() as c:
            c.executemany(sql, values)
        return len(values)

    def hours(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Hours with start in [start, end), oldest first. `hour_utc` as a datetime."""
        sql = "SELECT * FROM hourly"
        where, params = [], []
        if start is not None:
            where.append("hour_utc >= ?")
            params.append(_epoch(start))
        if end is not None:
            where.append("hour_utc < ?")
            params.append(_epoch(end))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY hour_utc"
        rows = self._rows(sql, params)
        for r in rows:
            r["hour_utc"] = _from_epoch(r["hour_utc"])
        return rows

    def hour_span(self) -> tuple[datetime | None, datetime | None]:
        rows = self._rows("SELECT MIN(hour_utc) AS a, MAX(hour_utc) AS b FROM hourly")
        if not rows or rows[0]["a"] is None:
            return None, None
        return _from_epoch(rows[0]["a"]), _from_epoch(rows[0]["b"])

    def first_hour_from(self, *, not_source: str) -> datetime | None:
        """The first stored hour that did NOT come from `not_source` (e.g. the clamp's first)."""
        rows = self._rows("SELECT MIN(hour_utc) AS a FROM hourly WHERE source != ?", (not_source,))
        return _from_epoch(rows[0]["a"]) if rows and rows[0]["a"] is not None else None

    def hour_count(self) -> int:
        return int(self._rows("SELECT COUNT(*) AS n FROM hourly")[0]["n"])

    def daily(self, start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM v_daily"
        where, params = [], []
        if start:
            where.append("local_date >= ?")
            params.append(start.isoformat())
        if end:
            where.append("local_date <= ?")
            params.append(end.isoformat())
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self._rows(sql + " ORDER BY local_date", params)

    def daily_kwh(self) -> dict[date, float]:
        """Complete-ish days of house kWh, for the forecaster."""
        return {
            date.fromisoformat(r["local_date"]): float(r["house_kwh"] or 0.0)
            for r in self._rows(
                "SELECT local_date, SUM(house_kwh) AS house_kwh, COUNT(*) AS n "
                "FROM hourly GROUP BY local_date HAVING n >= 23"
            )
        }

    # ------------------------------------------------------------------- ESB

    def upsert_esb_intervals(self, rows: list[tuple[datetime, float, str]], source: str) -> int:
        """(start_utc, kwh, band) rows. Re-delivered intervals overwrite: ESB revises."""
        values = []
        for start, kwh, band in rows:
            local = start.astimezone(IRELAND)
            values.append(
                (
                    _epoch(start),
                    local.strftime("%Y-%m-%d %H:%M"),
                    local.date().isoformat(),
                    band,
                    float(kwh),
                    source,
                )
            )
        if not values:
            return 0
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO esb_intervals "
                "(start_utc, local_start, local_date, band, kwh, source) VALUES (?,?,?,?,?,?)",
                values,
            )
        return len(values)

    def esb_intervals(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> list[tuple[datetime, float, str]]:
        sql = "SELECT start_utc, kwh, band FROM esb_intervals"
        where, params = [], []
        if start is not None:
            where.append("start_utc >= ?")
            params.append(_epoch(start))
        if end is not None:
            where.append("start_utc < ?")
            params.append(_epoch(end))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY start_utc"
        return [
            (_from_epoch(r["start_utc"]), float(r["kwh"]), r["band"])
            for r in self._rows(sql, params)
        ]

    def esb_span(self) -> tuple[datetime | None, datetime | None]:
        rows = self._rows("SELECT MIN(start_utc) AS a, MAX(start_utc) AS b FROM esb_intervals")
        if not rows or rows[0]["a"] is None:
            return None, None
        # The end is the END of the last interval.
        return _from_epoch(rows[0]["a"]), _from_epoch(rows[0]["b"]) + timedelta(minutes=30)

    def esb_daily(self, start: date | None = None, end: date | None = None) -> dict[str, dict]:
        sql = "SELECT * FROM v_esb_daily"
        where, params = [], []
        if start:
            where.append("local_date >= ?")
            params.append(start.isoformat())
        if end:
            where.append("local_date <= ?")
            params.append(end.isoformat())
        if where:
            sql += " WHERE " + " AND ".join(where)
        return {r["local_date"]: r for r in self._rows(sql, params)}

    def clear_esb(self, source: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM esb_intervals WHERE source = ?", (source,))

    def upsert_register_reads(self, reads: list[tuple[date, str, float, str]]) -> int:
        if not reads:
            return 0
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO esb_register_reads (read_date, register, reading, kind) "
                "VALUES (?,?,?,?)",
                [(d.isoformat(), reg, float(v), kind) for d, reg, v, kind in reads],
            )
        return len(reads)

    def register_reads(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM esb_register_reads ORDER BY read_date, register")

    # --------------------------------------------------------------- tariffs

    def save_tariff(self, t: Tariff) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO tariffs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    t.valid_from.isoformat(),
                    t.supplier,
                    t.plan,
                    t.day_rate,
                    t.night_rate,
                    t.standing_per_year,
                    t.pso_per_year,
                    t.window.start_hour,
                    t.window.end_hour,
                    t.window.clock,
                ),
            )

    def tariffs(self) -> list[Tariff]:
        out = []
        for r in self._rows("SELECT * FROM tariffs ORDER BY valid_from"):
            out.append(
                Tariff.from_dict(
                    {
                        "valid_from": r["valid_from"],
                        "supplier": r["supplier"],
                        "plan": r["plan"],
                        "day_rate": r["day_rate_eur"],
                        "night_rate": r["night_rate_eur"],
                        "standing_per_year": r["standing_eur_year"],
                        "pso_per_year": r["pso_eur_year"],
                        "window": {
                            "start_hour": r["night_start_hour"],
                            "end_hour": r["night_end_hour"],
                            "clock": r["night_clock"],
                        },
                    }
                )
            )
        return out

    def delete_tariff(self, valid_from: date) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM tariffs WHERE valid_from = ?", (valid_from.isoformat(),))

    def replace_tariff(self, old_valid_from: date, t: Tariff) -> bool:
        """Move the period stored at `old_valid_from` onto `t` (a corrected date or levy).

        One transaction, so there is never a moment with neither period stored. Refuses
        (returns False) when there is nothing at `old_valid_from` or when another period
        already starts on `t.valid_from`, so it can never overwrite a different tariff.
        """
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM tariffs WHERE valid_from = ?", (old_valid_from.isoformat(),)).fetchone():
                return False
            if t.valid_from != old_valid_from and c.execute(
                "SELECT 1 FROM tariffs WHERE valid_from = ?", (t.valid_from.isoformat(),)
            ).fetchone():
                return False
            c.execute("DELETE FROM tariffs WHERE valid_from = ?", (old_valid_from.isoformat(),))
            c.execute(
                "INSERT INTO tariffs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    t.valid_from.isoformat(), t.supplier, t.plan, t.day_rate, t.night_rate,
                    t.standing_per_year, t.pso_per_year, t.window.start_hour, t.window.end_hour, t.window.clock,
                ),
            )
        return True

    # ---------------------------------------------------------- meter anchors

    def add_anchor(self, taken_at: datetime, day_kwh: float, night_kwh: float, origin: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO meter_anchors (taken_at, day_kwh, night_kwh, origin, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    taken_at.astimezone(UTC).replace(microsecond=0).isoformat(),
                    float(day_kwh),
                    float(night_kwh),
                    origin,
                    _now_iso(),
                ),
            )

    def anchors(self) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM meter_anchors ORDER BY taken_at, id")
        for r in rows:
            r["taken_at"] = datetime.fromisoformat(r["taken_at"])
        return rows

    def latest_anchor(self) -> dict[str, Any] | None:
        rows = self.anchors()
        return rows[-1] if rows else None

    # ------------------------------------------------------ meter submissions

    def log_submission(self, record: dict[str, Any]) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO meter_submissions (created_at, read_at, day_kwh, night_kwh, origin, "
                "mode, status, message, payload, response) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    _now_iso(),
                    record["read_at"],
                    int(record["day_kwh"]),
                    int(record["night_kwh"]),
                    record.get("origin", "estimate"),
                    record["mode"],
                    record["status"],
                    record.get("message"),
                    json.dumps(record.get("payload")) if record.get("payload") is not None else None,
                    json.dumps(record.get("response")) if record.get("response") is not None else None,
                ),
            )
            return int(cur.lastrowid)

    def submissions(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM meter_submissions ORDER BY id DESC LIMIT ?", (int(limit),)
        )

    def last_submission(self, *, status: str | None = None) -> dict[str, Any] | None:
        if status:
            rows = self._rows(
                "SELECT * FROM meter_submissions WHERE status = ? ORDER BY id DESC LIMIT 1",
                (status,),
            )
        else:
            rows = self._rows("SELECT * FROM meter_submissions ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    # ------------------------------------------------------- portal sessions

    def save_session(self, source: str, cookies: dict[str, str]) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO sessions (source, cookies, saved_at) VALUES (?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET cookies = excluded.cookies, "
                "saved_at = excluded.saved_at",
                (source, json.dumps(cookies), _now_iso()),
            )

    def load_session(self, source: str) -> dict[str, str]:
        rows = self._rows("SELECT cookies FROM sessions WHERE source = ?", (source,))
        if not rows:
            return {}
        try:
            data = json.loads(rows[0]["cookies"])
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def clear_session(self, source: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM sessions WHERE source = ?", (source,))

    def fetch_state(self, source: str) -> FetchState:
        rows = self._rows("SELECT * FROM fetch_state WHERE source = ?", (source,))
        if not rows:
            return FetchState()
        r = rows[0]
        return FetchState(
            last_attempt=datetime.fromisoformat(r["last_attempt"]) if r["last_attempt"] else None,
            last_success=datetime.fromisoformat(r["last_success"]) if r["last_success"] else None,
            failures=int(r["failures"] or 0),
            last_error=r["last_error"],
        )

    def mark_attempt(self, source: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO fetch_state (source, last_attempt) VALUES (?, ?) "
                "ON CONFLICT(source) DO UPDATE SET last_attempt = excluded.last_attempt",
                (source, _now_iso()),
            )

    def mark_success(self, source: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO fetch_state (source, last_attempt, last_success, failures, last_error) "
                "VALUES (?, ?, ?, 0, NULL) ON CONFLICT(source) DO UPDATE SET "
                "last_success = excluded.last_success, failures = 0, last_error = NULL",
                (source, _now_iso(), _now_iso()),
            )

    def reset_failures(self, source: str) -> None:
        """Forget a failure streak (new credentials): the next attempt need not wait."""
        with self._tx() as c:
            c.execute("UPDATE fetch_state SET failures = 0, last_error = NULL WHERE source = ?", (source,))

    def mark_failure(self, source: str, error: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO fetch_state (source, last_attempt, failures, last_error) "
                "VALUES (?, ?, 1, ?) ON CONFLICT(source) DO UPDATE SET "
                "failures = fetch_state.failures + 1, last_error = excluded.last_error",
                (source, _now_iso(), error[:500]),
            )

    # -------------------------------------------------------------- weather

    def upsert_weather(self, observations, source: str) -> int:
        """Archive rows always win; forecast rows never overwrite an archive hour."""
        values = []
        for obs in observations:
            local = obs.hour.astimezone(IRELAND)
            values.append(
                (
                    _epoch(obs.hour),
                    local_hour_label(obs.hour),
                    local.date().isoformat(),
                    source,
                    *(obs.values.get(c) for c in WEATHER_COLUMNS),
                )
            )
        if not values:
            return 0
        cols = ("hour_utc", "local_hour", "local_date", "source", *WEATHER_COLUMNS)
        placeholders = ", ".join("?" for _ in cols)
        if source == "archive":
            sql = f"INSERT OR REPLACE INTO weather_hourly ({', '.join(cols)}) VALUES ({placeholders})"
        else:
            sql = (
                f"INSERT INTO weather_hourly ({', '.join(cols)}) VALUES ({placeholders}) "
                "ON CONFLICT(hour_utc) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in ("local_hour", "local_date", "source", *WEATHER_COLUMNS))
                + " WHERE weather_hourly.source = 'forecast'"
            )
        with self._tx() as c:
            c.executemany(sql, values)
        return len(values)

    def clear_weather(self) -> int:
        """Every weather hour, for when the location moves."""
        with self._tx() as c:
            return c.execute("DELETE FROM weather_hourly").rowcount

    def weather(self, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM weather_hourly"
        where, params = [], []
        if start is not None:
            where.append("hour_utc >= ?")
            params.append(_epoch(start))
        if end is not None:
            where.append("hour_utc < ?")
            params.append(_epoch(end))
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._rows(sql + " ORDER BY hour_utc", params)
        for r in rows:
            r["hour_utc"] = _from_epoch(r["hour_utc"])
        return rows

    def weather_archive_days(self) -> dict[str, int]:
        """Archive hours held per local date."""
        return {
            r["local_date"]: int(r["n"])
            for r in self._rows(
                "SELECT local_date, COUNT(*) AS n FROM weather_hourly WHERE source = 'archive' GROUP BY local_date"
            )
        }

    # --------------------------------------------------------- forecast log

    def log_forecasts(self, rows: list[dict[str, Any]], kind: str = "live") -> int:
        if not rows:
            return 0
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO forecast_log (made_on, target, model, kwh, kind) VALUES (?,?,?,?,?)",
                [(r["made_on"].isoformat(), r["target"].isoformat(), r["model"], float(r["kwh"]), kind) for r in rows],
            )
        return len(rows)

    def forecast_log(self, since: date) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM forecast_log WHERE target >= ? ORDER BY target, model", (since.isoformat(),)
        )
        for r in rows:
            r["made_on"] = date.fromisoformat(r["made_on"])
            r["target"] = date.fromisoformat(r["target"])
        return rows

    def logged_on(self, made_on: date) -> bool:
        rows = self._rows(
            "SELECT 1 FROM forecast_log WHERE made_on = ? AND kind = 'live' LIMIT 1", (made_on.isoformat(),)
        )
        return bool(rows)

    def save_weights(self, fitted_on: date, model: str, weights: dict[str, float]) -> None:
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO learned_weights (fitted_on, model, feature, weight) VALUES (?,?,?,?)",
                [(fitted_on.isoformat(), model, k, float(v)) for k, v in weights.items()],
            )

    def weight_history(self, model: str, since: date) -> dict[str, list[tuple[str, float]]]:
        out: dict[str, list[tuple[str, float]]] = {}
        for r in self._rows(
            "SELECT fitted_on, feature, weight FROM learned_weights WHERE model = ? AND fitted_on >= ? ORDER BY fitted_on",
            (model, since.isoformat()),
        ):
            out.setdefault(r["feature"], []).append((r["fitted_on"], round(float(r["weight"]), 4)))
        return out

    # -------------------------------------------------------------- minutes

    MINUTE_FIELDS = (
        "house_w", "house_w_min", "house_w_max", "hall_w", "hall_w_max", "rest_w", "house_pf",
        "hall_pf", "voltage", "voltage_min", "voltage_max", "house_wh", "hall_wh", "samples",
    )

    def upsert_minutes(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        cols = ("minute_utc", "local_time", *self.MINUTE_FIELDS)
        with self._tx() as c:
            c.executemany(
                f"INSERT OR REPLACE INTO minutely ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [
                    (
                        _epoch(r["minute_utc"]),
                        r["minute_utc"].astimezone(IRELAND).strftime("%Y-%m-%d %H:%M"),
                        *(r.get(f) for f in self.MINUTE_FIELDS),
                    )
                    for r in rows
                ],
            )
        return len(rows)

    def minutes(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM minutely WHERE minute_utc >= ? AND minute_utc < ? ORDER BY minute_utc",
            (_epoch(start), _epoch(end)),
        )
        for r in rows:
            r["minute_utc"] = _from_epoch(r["minute_utc"])
        return rows

    # ----------------------------------------------------------------- gaps

    def missing_hours(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Contiguous runs of hours in [start, end) with no stored row."""
        have = {
            r["hour_utc"]
            for r in self._rows(
                "SELECT hour_utc FROM hourly WHERE hour_utc >= ? AND hour_utc < ?", (_epoch(start), _epoch(end))
            )
        }
        runs: list[tuple[datetime, datetime]] = []
        t = start
        while t < end:
            if _epoch(t) not in have:
                if runs and runs[-1][1] == t:
                    runs[-1] = (runs[-1][0], t + timedelta(hours=1))
                else:
                    runs.append((t, t + timedelta(hours=1)))
            t += timedelta(hours=1)
        return runs

    # ----------------------------------------------------------------- misc

    def stats(self) -> dict[str, Any]:
        first, last = self.hour_span()
        esb_first, esb_last = self.esb_span()
        return {
            "path": str(self.path),
            "hours": self.hour_count(),
            "first_hour": first.isoformat() if first else None,
            "last_hour": last.isoformat() if last else None,
            # Where the clamp's own measurements begin (earlier hours are ESB's meter data).
            "clamp_first_hour": (lambda t: t.isoformat() if t else None)(self.first_hour_from(not_source="esb")),
            "esb_hours": int(self._rows("SELECT COUNT(*) AS n FROM hourly WHERE source = 'esb'")[0]["n"]),
            # Priced on ESB's own data (final) versus on the clamp until ESB publishes them.
            "final_hours": int(self._rows("SELECT COUNT(*) AS n FROM hourly WHERE meter_source = 'esb'")[0]["n"]),
            # Hours before the first tariff period: kWh kept, no price (see tariff.py).
            "unpriced_hours": int(
                self._rows("SELECT COUNT(*) AS n FROM hourly WHERE house_cost_eur IS NULL")[0]["n"]
            ),
            "esb_intervals": int(self._rows("SELECT COUNT(*) AS n FROM esb_intervals")[0]["n"]),
            "esb_first": esb_first.isoformat() if esb_first else None,
            "esb_last": esb_last.isoformat() if esb_last else None,
            "tariffs": len(self.tariffs()),
            "anchors": len(self.anchors()),
            "submissions": int(
                self._rows("SELECT COUNT(*) AS n FROM meter_submissions")[0]["n"]
            ),
            "weather_hours": int(self._rows("SELECT COUNT(*) AS n FROM weather_hourly")[0]["n"]),
            "minutes": int(self._rows("SELECT COUNT(*) AS n FROM minutely")[0]["n"]),
            "forecast_log_rows": int(self._rows("SELECT COUNT(*) AS n FROM forecast_log")[0]["n"]),
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }
