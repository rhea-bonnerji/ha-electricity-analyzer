"""Keep the database safe: check it on every start, back it up every night.

NO HOME ASSISTANT IMPORTS.

    <config>/electricity_analyzer/electricity.db          the database (WAL, full fsync)
    <config>/electricity_analyzer/backups/daily-*.db      the last 7 nights
    <config>/electricity_analyzer/backups/weekly-*.db     the last 4 Sundays
    <config>/electricity_analyzer/backups/monthly-*.db    the last 12 firsts-of-the-month
    <config>/electricity_analyzer/exports/*.csv           on request (export service)

The whole folder is inside Home Assistant's config directory, so every ordinary Home
Assistant backup carries the database and its backups along with it.

On start the database is checked with SQLite's own integrity check. A damaged file is
moved aside (never deleted: it is evidence), the newest backup that passes its own check
is restored in its place, and the caller is told so a Repairs item can say what happened.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import date
from pathlib import Path

KEEP = {"daily": 7, "weekly": 4, "monthly": 12}


def backup_dir(db_path: Path) -> Path:
    return db_path.parent / "backups"


def backups(db_path: Path) -> list[Path]:
    folder = backup_dir(db_path)
    if not folder.exists():
        return []
    return sorted(folder.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)


def check_file(path: Path) -> bool | None:
    """True if healthy, False if SQLite says the file is damaged, None if it could not tell.

    "Could not tell" (the file is locked or busy, a read-only connection cannot replay a
    write-ahead log left by a power cut, a transient I/O error) is NOT damage. Treating
    it as damage would move a good database aside and roll it back to last night's
    backup, losing everything since; that is far worse than trying again.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return None
    except sqlite3.DatabaseError:
        # "file is not a database", "database disk image is malformed": real damage.
        return False


def recover(db_path: Path) -> tuple[str, str | None]:
    """Make sure `db_path` is healthy. Returns (outcome, detail).

    outcome: "ok" | "new" | "unchecked" | "restored" | "unrecoverable"
    """
    if not db_path.exists():
        return "new", None
    verdict = check_file(db_path)
    if verdict is None:
        # A read-write connection can replay a hot WAL that a read-only one cannot.
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                verdict = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            return "unchecked", f"integrity check could not run ({exc}); the database was left untouched"
        except sqlite3.DatabaseError:
            verdict = False
    if verdict:
        return "ok", None
    stamp = date.today().isoformat()
    damaged = db_path.with_name(f"{db_path.stem}.damaged-{stamp}{db_path.suffix}")
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if src.exists():
            src.replace(Path(str(damaged) + suffix))
    for candidate in backups(db_path):
        if check_file(candidate):
            shutil.copy2(candidate, db_path)
            return "restored", f"restored {candidate.name}; the damaged file was kept as {damaged.name}"
    return "unrecoverable", f"no healthy backup; the damaged file was kept as {damaged.name}; starting fresh"


def rotate(db_path: Path, today: date) -> list[Path]:
    """Which backups to write tonight (daily always; weekly on Sundays; monthly on the 1st),
    and prune the old ones. Returns the target paths."""
    folder = backup_dir(db_path)
    folder.mkdir(parents=True, exist_ok=True)
    targets = [folder / f"daily-{today.isoformat()}.db"]
    if today.weekday() == 6:
        targets.append(folder / f"weekly-{today.isoformat()}.db")
    if today.day == 1:
        targets.append(folder / f"monthly-{today.isoformat()}.db")
    for kind, keep in KEEP.items():
        existing = sorted(folder.glob(f"{kind}-*.db"), reverse=True)
        for old in existing[keep - 1 if any(t.name.startswith(kind) for t in targets) else keep:]:
            old.unlink(missing_ok=True)
    return targets
