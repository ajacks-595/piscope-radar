"""Daily DB backup — especially that secrets are stripped (parity with /api/export)."""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_redacted_sql_dump_strips_secrets(temp_db):
    # The shared dump helper must blank every SECRET_KEYS value while keeping ordinary data.
    from app.services import settings as settings_store
    settings_store.set_many({
        "cloud_api_key": "SECRET-CLOUD", "smtp_pass": "SECRET-PW",
        "fa_api_key": "SECRET-FA", "theme": "nord",
    })
    dump = settings_store.redacted_sql_dump(settings_store.DB_PATH)
    for secret in ("SECRET-CLOUD", "SECRET-PW", "SECRET-FA"):
        assert secret not in dump, f"{secret} leaked into dump"
    assert "nord" in dump          # non-secret settings survive the round-trip


def test_daily_backup_zip_omits_secrets(temp_db, tmp_path):
    # Regression for S1: the automated daily backup zip previously dumped the DB verbatim,
    # including plaintext API keys. It must now strip them like /api/export does.
    import asyncio
    import zipfile
    from app.services import settings as settings_store
    from app.services import backups as backups_store

    settings_store.set_many({"fa_api_key": "FA-SECRET-XYZ", "theme": "nord"})
    backups_store._LAST_BACKUP_DATE = None             # force a run this call
    settings_store.set_one("daily_backup_dir", str(tmp_path))

    # maybe_run_daily is async (iter 13: offloads the heavy dump to a thread).
    out = asyncio.run(backups_store.maybe_run_daily())
    assert out is not None
    with zipfile.ZipFile(out) as zf:
        sql = zf.read("piscope.sql").decode("utf-8")
    assert "FA-SECRET-XYZ" not in sql
    assert "nord" in sql
