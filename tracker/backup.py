"""Automatic backups of everyone's data to a folder you choose (e.g. in OneDrive).

What's copied: every database (yours, each person's, and the sign-in registry), using SQLite's
own backup so a copy is consistent even while the app is running. Not copied: the downloaded
statement files (everything in them is already in the databases), Gmail sign-in tokens, and PDF
passwords (those stay in Windows Credential Manager).

Each backup is a dated folder, `Expenses-YYYYMMDD-HHMMSS`; the newest few are kept. The folder
and the schedule are the owner's settings, kept in the owner's database. Logs never record the
folder path (it contains your Windows user name).
"""

import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from tracker import db, profiles

log = logging.getLogger(__name__)

EVERY_DAYS = 7
KEEP = 8
PREFIX = "Expenses-"
README = """Expenses app backup ({when})

These are copies of the app's databases: expenses.db (the owner), profiles.db (who can sign in)
and profiles/<name>/expenses.db (everyone else).

To restore: stop the app, copy these files back into the app's data folder (same layout),
and start the app again. Keep this folder private: it holds everyone's transactions.
"""


def _owner():
    """The owner's settings, whoever is signed in."""
    return db.use_path(db.get_default_db_path())


def settings() -> dict:
    with _owner(), db.session() as conn:
        folder = db.get_setting(conn, "backup:folder")
        last = float(db.get_setting(conn, "backup:last", "0") or 0)
    return {
        "folder": folder,
        "last": datetime.fromtimestamp(last).isoformat(timespec="minutes") if last else None,
        "next": datetime.fromtimestamp(last + EVERY_DAYS * 86400).isoformat(timespec="minutes") if folder and last else None,
        "every_days": EVERY_DAYS,
        "keep": KEEP,
        "suggestion": suggestion(),
    }


def suggestion() -> str | None:
    one_drive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    return str(Path(one_drive) / "Expenses backups") if one_drive and Path(one_drive).is_dir() else None


def set_folder(folder: str | None) -> dict:
    folder = (folder or "").strip().strip('"')
    if folder:
        path = Path(folder).expanduser()
        if not path.is_absolute():
            raise ValueError("Give the full path of a folder, e.g. C:\\Users\\you\\OneDrive\\Expenses backups")
        data = profiles.base_dir().resolve()
        if path.resolve() == data or data in path.resolve().parents:
            raise ValueError("Choose a folder outside the app's own data folder.")
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise ValueError(f"Can't write to that folder ({exc.strerror or exc}).") from exc
        folder = str(path)
    with _owner(), db.session() as conn:
        db.set_setting(conn, "backup:folder", folder)
    log.info("Backups %s", "turned on" if folder else "turned off")
    return settings()


def _databases() -> list[tuple[Path, Path]]:
    """(source, path inside the backup) for every database there is."""
    base = profiles.base_dir()
    out = [(db.get_default_db_path(), Path("expenses.db"))]
    if profiles.registry_path().exists():
        out.append((profiles.registry_path(), Path("profiles.db")))
    for p in profiles.list_profiles():
        if not p.is_owner and p.db_path.exists():
            out.append((p.db_path, p.db_path.relative_to(base)))
    return [(src, rel) for src, rel in out if src.exists()]


def run(folder: str | None = None) -> dict:
    """Back up now. Returns what was copied; raises ValueError when there's no folder."""
    folder = folder or settings()["folder"]
    if not folder:
        raise ValueError("Choose a backup folder first.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target, n = Path(folder) / f"{PREFIX}{stamp}", 2
    while target.exists():  # two backups in the same second
        target, n = Path(folder) / f"{PREFIX}{stamp}-{n}", n + 1
    target.mkdir(parents=True)
    size = 0
    copied = _databases()
    for src, rel in copied:
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(src) as source, sqlite3.connect(dest) as copy:
            source.backup(copy)
        size += dest.stat().st_size
    (target / "README.txt").write_text(README.format(when=datetime.now().strftime("%d %b %Y %H:%M")), encoding="utf-8")
    removed = _prune(Path(folder))
    with _owner(), db.session() as conn:
        db.set_setting(conn, "backup:last", str(time.time()))
    log.info("Backup done: %d databases, %d KB, %d older backups removed", len(copied), size // 1024, removed)
    return {"databases": len(copied), "bytes": size, "folder_name": target.name, "removed": removed, **settings()}


def _prune(folder: Path) -> int:
    backups = sorted((p for p in folder.glob(f"{PREFIX}*") if p.is_dir()), key=lambda p: p.name, reverse=True)
    for old in backups[KEEP:]:
        shutil.rmtree(old, ignore_errors=True)
    return max(len(backups) - KEEP, 0)


def due(now: float | None = None) -> bool:
    with _owner(), db.session() as conn:
        folder = db.get_setting(conn, "backup:folder")
        last = float(db.get_setting(conn, "backup:last", "0") or 0)
    return bool(folder) and (now or time.time()) - last >= EVERY_DAYS * 86400


def start_scheduler(check_every: float = 3600) -> threading.Thread:
    """Checks once an hour (and at start-up) whether a backup is due. Runs as the owner, outside any request."""

    def loop():
        while True:
            try:
                with profiles.use(None):
                    if due():
                        run()
            except Exception:  # a full disk or a missing OneDrive folder must not stop the app
                log.exception("Scheduled backup failed")
            time.sleep(check_every)

    thread = threading.Thread(target=loop, name="backups", daemon=True)
    thread.start()
    return thread
