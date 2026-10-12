"""Copies of the whole notebook (the SQLite file), so a dead disk or a deleted file loses nothing.

- One automatic copy a day: debt-2026-10-12.db (made when the program starts, then checked every hour).
- A copy by hand from the page: debt-2026-10-12_153000-manual.db
- Old copies are removed: the last KEEP_DAILY daily ones and KEEP_MANUAL by-hand ones stay.

The copy uses SQLite's own backup, so it is safe while the program is running.
To restore: close the program, copy a backup over debt.db, start it again (see README).
"""

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEEP_DAILY = 30
KEEP_MANUAL = 20
MANUAL_TAG = "manual"
NAME_RE = re.compile(r"^debt-\d{4}-\d{2}-\d{2}(?:_\d{4}(?:\d{2})?-" + MANUAL_TAG + r")?\.db$")


def backup_dir() -> Path:
    """BACKUP_DIR from .env (e.g. a USB stick or a Google Drive folder), else backups/ in the project."""
    return Path(os.getenv("BACKUP_DIR") or ROOT / "backups")


def _copy(conn: sqlite3.Connection, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    target = sqlite3.connect(str(tmp))
    try:
        conn.backup(target)
    finally:
        target.close()
    os.replace(tmp, dest)  # a half-written copy never looks like a good one
    return dest


def make_backup(conn, folder: Path | None = None, manual: bool = True, now: datetime | None = None) -> Path:
    folder = folder or backup_dir()
    now = now or datetime.now()
    if manual:
        name = f"debt-{now:%Y-%m-%d_%H%M%S}-{MANUAL_TAG}.db"
    else:
        name = f"debt-{now:%Y-%m-%d}.db"
    path = _copy(conn, folder / name)
    prune(folder)
    return path


def ensure_daily(conn, folder: Path | None = None, now: datetime | None = None) -> Path | None:
    """Today's automatic copy, if it is not there yet. Returns the new file or None."""
    folder = folder or backup_dir()
    now = now or datetime.now()
    if (folder / f"debt-{now:%Y-%m-%d}.db").exists():
        return None
    return make_backup(conn, folder, manual=False, now=now)


def list_backups(folder: Path | None = None) -> list[dict]:
    """Newest first."""
    folder = folder or backup_dir()
    if not folder.is_dir():
        return []
    items = []
    for p in folder.iterdir():
        if p.is_file() and NAME_RE.match(p.name):
            st = p.stat()
            items.append({"name": p.name, "size": st.st_size, "manual": MANUAL_TAG in p.name,
                          "created_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    items.sort(key=lambda b: b["name"], reverse=True)  # the date is in the name
    return items


def prune(folder: Path) -> None:
    items = list_backups(folder)
    daily = [b for b in items if not b["manual"]]
    manual = [b for b in items if b["manual"]]
    for b in daily[KEEP_DAILY:] + manual[KEEP_MANUAL:]:
        (folder / b["name"]).unlink(missing_ok=True)


def backup_path(name: str, folder: Path | None = None) -> Path | None:
    """The file for a name from list_backups, or None (no paths, no other files)."""
    if not NAME_RE.match(name or ""):
        return None
    path = (folder or backup_dir()) / name
    return path if path.is_file() else None
