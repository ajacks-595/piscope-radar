"""Daily snapshot of the SQLite store to a user-configured directory.

When `daily_backup_dir` is set, the feed loop calls `maybe_run_daily()` once per cycle. The
backup itself only fires when the day rolls over, and we keep the last 14 zip files on disk.
"""
from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import settings as settings_store


log = logging.getLogger("piscope.backups")

KEEP_BACKUPS = 14
_LAST_BACKUP_DATE: Optional[str] = None


def _backup_path(dir_path: Path) -> Path:
    return dir_path / f"piscope-{datetime.now(timezone.utc).strftime('%Y%m%d')}.zip"


def _write_backup(dir_path: Path) -> Optional[Path]:
    db_path = Path(settings_store.DB_PATH)
    if not db_path.exists():
        return None
    dir_path.mkdir(parents=True, exist_ok=True)
    target = _backup_path(dir_path)
    # Online backup + secret-stripping (shared with /api/export): the daily zip lands on
    # disk and may be synced/emailed, so it must NOT carry plaintext API keys / SMTP pass /
    # provider tokens. redacted_sql_dump blanks them in a throwaway in-memory copy.
    dump = settings_store.redacted_sql_dump(db_path)
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("piscope.sql", dump)
        zf.writestr("created_at.txt", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
    target.write_bytes(bio.getvalue())
    return target


def _prune(dir_path: Path) -> int:
    backups = sorted(dir_path.glob("piscope-*.zip"))
    extra = max(0, len(backups) - KEEP_BACKUPS)
    for old in backups[:extra]:
        try:
            old.unlink()
        except OSError:
            pass
    return extra


def maybe_run_daily() -> Optional[str]:
    """Run a backup if the date has rolled over and a target dir is configured."""
    global _LAST_BACKUP_DATE
    target_dir = (settings_store.get("daily_backup_dir") or "").strip()
    if not target_dir:
        return None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _LAST_BACKUP_DATE == today:
        return None
    try:
        dir_path = Path(os.path.expanduser(target_dir))
        out = _write_backup(dir_path)
        _prune(dir_path)
        _LAST_BACKUP_DATE = today
        log.info("Daily backup written to %s", out)
        return str(out) if out else None
    except Exception as exc:
        log.warning("Daily backup failed: %s", exc)
        return None
