"""Backup zips: build them (/api/export + the daily backup) and restore them (/api/import).

Both directions stream, because the production Pi is a Raspberry Pi 3 with ~1 GB of
RAM and no swap:

* Export writes the secret-redacted SQL dump line by line into the zip entry, instead
  of materialising the whole dump string (54 MB on the live DB) and then a second
  in-memory copy of the zip.
* Restore decompresses the dump through a byte-counting reader and executes it one
  statement at a time. The old path did ``zf.read()`` (no uncompressed-size bound —
  a 20 MB zip could expand to gigabytes, 2026-11-08 P2-2) and then ``.decode()`` (a
  single non-ASCII character makes CPython store the whole string at 4 bytes/char).

A backup file is fully attacker-controlled on this no-auth box, so restore is
defensive in depth (2026-11-08 P1-1 / P2-2):

1. Size caps: zip entry count, declared AND actual decompressed size, per statement.
2. The dump runs under a whitelist authorizer: it may only create tables/indexes and
   insert/delete literal rows. No ATTACH/DETACH (file-write primitive), no triggers
   or views, no SELECT (so no recursive-CTE CPU bombs), no SQL functions
   (``randomblob``/``zeroblob`` memory bombs), no PRAGMA but ``foreign_keys``.
3. The result is a *scratch* DB that is never used directly. Its rows are copied,
   column by column, into a fresh DB built from the canonical schema
   (settings.ensure_schema), so a dump that redefines a table (say, ``settings``
   without its PRIMARY KEY, which breaks every upsert) cannot wedge the service.
4. Every restored settings row is re-validated exactly like a live write; invalid
   rows are dropped so the key falls back to its default. A dump persisting
   ``poll_interval = Infinity`` used to 500 every settings read and kill the feed.
"""
from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import time
import zipfile
import zlib
from contextlib import closing
from pathlib import Path
from typing import IO, Any, Iterator, Optional

from . import settings as settings_store


log = logging.getLogger("piscope.restore")


# Sized from the live Pi (2026-11-08): ~5 months of analytics = 50 MB of SQL in a
# 16 MB zip, restored in 65 s at 64 MB peak RSS. A full 365-day ledger projects to
# ~130 MB / ~40 MB zip, so the caps (upload: api.MAX_IMPORT_UPLOAD_BYTES = 64 MiB)
# leave ~2x headroom. Both directions stream, so this bounds disk + CPU time (a
# few minutes on the Pi 3 at the cap), not RAM. Raise both caps together if
# analytics_retention_days is set to multiple years.
MAX_SQL_BYTES = 256 * 1024 * 1024
# The largest single statement in a real dump is one feed_snapshots row (a
# zlib-compressed snapshot, hex-encoded: well under 1 MB).
MAX_STATEMENT_CHARS = 8 * 1024 * 1024
# A real backup holds 3 entries (dump + timestamp + README).
MAX_ZIP_ENTRIES = 16
SQL_ENTRY_NAMES = ("piscope.sql", "skywatch.sql")   # skywatch = pre-rename backups


class RestoreError(ValueError):
    """The backup was refused. The message is safe to show the user."""


# --- Export ---------------------------------------------------------------------


def iter_redacted_dump(db_path: Path) -> Iterator[str]:
    """Yield a full SQL dump of the DB (one statement per item) with every
    SECRET_KEYS value blanked.

    Shared by /api/export AND the daily backup so neither writes plaintext API
    keys / SMTP password / provider tokens into a zip that may be emailed or
    cloud-stored, and so the two can never drift. Runs against a throwaway
    in-memory copy via SQLite's online backup, so the live DB is never modified."""
    with closing(sqlite3.connect(db_path)) as src, closing(sqlite3.connect(":memory:")) as mem:
        src.backup(mem)
        secret_keys = sorted(settings_store.SECRET_KEYS)
        if secret_keys:
            # Values are JSON-encoded in the settings table; '""' is an empty JSON string.
            placeholders = ",".join("?" * len(secret_keys))
            mem.execute(
                # bandit B608 false positive: only ? placeholders / our own schema's table+column names are interpolated
                f"UPDATE settings SET value = '\"\"' WHERE key IN ({placeholders})",  # nosec B608
                tuple(secret_keys),
            )
            mem.commit()
        yield from mem.iterdump()


def write_backup_zip(db_path: Path, dest: IO[bytes], extra_files: dict[str, str]) -> None:
    """Stream a redacted dump of `db_path` into a zip written to `dest`.

    Lines are batched into ~1 MB writes so deflate sees reasonable chunks
    without ever holding the whole dump in memory."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        with zf.open("piscope.sql", "w") as entry:
            chunk: list[bytes] = []
            size = 0
            for stmt in iter_redacted_dump(db_path):
                b = (stmt + "\n").encode("utf-8")
                chunk.append(b)
                size += len(b)
                if size >= 1 << 20:
                    entry.write(b"".join(chunk))
                    chunk, size = [], 0
            if chunk:
                entry.write(b"".join(chunk))
        for name, text in extra_files.items():
            zf.writestr(name, text)


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n"


# --- Restore: reading the dump ----------------------------------------------------


class _CappedReader(io.RawIOBase):
    """Read-only wrapper that raises RestoreError once more than `cap` bytes have
    come out of `raw`. The zip's declared file_size is attacker-controlled, so the
    real decompressed byte count is what gets enforced."""

    def __init__(self, raw: IO[bytes], cap: int) -> None:
        self._raw = raw
        self._cap = cap
        self.count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self._raw.read(len(b))
        n = len(data)
        self.count += n
        if self.count > self._cap:
            raise RestoreError(f"backup SQL is larger than {self._cap // (1024 * 1024)} MB uncompressed")
        b[:n] = data
        return n


def _select_sql_entry(zf: zipfile.ZipFile) -> zipfile.ZipInfo:
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise RestoreError(f"zip has {len(infos)} entries; a PiScope backup has only a few")
    by_name = {i.filename: i for i in infos}
    info = next((by_name[n] for n in SQL_ENTRY_NAMES if n in by_name), None)
    if info is None:
        raise RestoreError("Zip is missing piscope.sql / skywatch.sql")
    # Cheap early refusal on the declared size; _CappedReader enforces the real one.
    if info.file_size > MAX_SQL_BYTES:
        raise RestoreError(f"{info.filename} is {info.file_size // (1024 * 1024)} MB uncompressed; "
                           f"the limit is {MAX_SQL_BYTES // (1024 * 1024)} MB")
    return info


def iter_statements(text: IO[str], *, max_statement: int = MAX_STATEMENT_CHARS) -> Iterator[str]:
    """Split a dump into single statements without holding more than one in memory.

    A statement ends at a line ending in ';' with an even number of single quotes
    seen so far in it. That is exact for sqlite3.iterdump() output (identifiers are
    double-quoted and never contain a quote; '' escapes keep parity), and O(n) —
    unlike re-running sqlite3.complete_statement on a growing buffer. A crafted
    dump that fools the splitter only yields fragments that then fail to execute;
    every fragment still runs under the authorizer."""
    parts: list[str] = []
    size = 0
    quotes = 0
    while True:
        line = text.readline(max_statement + 1)
        if not line:
            break
        size += len(line)
        if size > max_statement:
            raise RestoreError("backup contains an oversized SQL statement")
        parts.append(line)
        quotes += line.count("'")
        if quotes % 2 == 0 and line.rstrip().endswith(";"):
            yield "".join(parts)
            parts, size, quotes = [], 0, 0
    if "".join(parts).strip():
        raise RestoreError("backup SQL ends in the middle of a statement")


# --- Restore: executing the dump into a scratch DB ---------------------------------

# Everything sqlite3.iterdump() output needs, measured by logging the authorizer
# while restoring a real export: CREATE TABLE / CREATE INDEX (the latter also fires
# REINDEX), INSERT, and READ (index builds read the indexed columns). The schema
# bookkeeping UPDATEs only sqlite_master, and the only DELETE is the dump clearing
# sqlite_sequence before re-seeding it — so UPDATE/DELETE are allowed on those
# internal tables alone (an `UPDATE t SET a = a || a` loop could otherwise double a
# value until the Pi runs out of memory). Anything else is denied, which fails that
# statement and so the whole restore, with a 400.
_RESTORE_ALLOWED_ACTIONS = frozenset({
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_READ,
})
_SCHEMA_TABLES = frozenset({"sqlite_master", "sqlite_schema"})


# Per-connection SQLite limits for the restore. A single value can't legitimately
# exceed the statement cap. LENGTH is load-bearing, not just belt and braces: SQLite
# evaluates a column `DEFAULT (expr)` WITHOUT consulting the authorizer's FUNCTION
# hook, so `DEFAULT (zeroblob(1e8))` slips past it and only this cap (or the
# expression-default refusal below, which also covers Python < 3.11) stops it.
_RESTORE_LIMITS = (
    ("SQLITE_LIMIT_LENGTH", MAX_STATEMENT_CHARS),
    ("SQLITE_LIMIT_SQL_LENGTH", MAX_STATEMENT_CHARS + 1024),
)


def _apply_limits(conn: sqlite3.Connection) -> None:
    """sqlite3.Connection.setlimit exists from Python 3.11 (the Pi's version); on
    older interpreters the size caps above still bound every statement."""
    setlimit = getattr(conn, "setlimit", None)
    if setlimit is None:
        return
    for name, value in _RESTORE_LIMITS:
        category = getattr(sqlite3, name, None)
        if category is not None:
            setlimit(category, value)


# A real dump's CREATE TABLE statements come from our own schema, whose defaults are
# all literals (`DEFAULT 0`). An expression default — `DEFAULT (`…`)` — is evaluated
# outside the authorizer's FUNCTION hook (see above), so refuse it outright.
_EXPR_DEFAULT_RE = re.compile(r"\bDEFAULT\s*\(", re.IGNORECASE)
_CREATE_RE = re.compile(r"^\s*CREATE\b", re.IGNORECASE)

# Transaction control in the dump is skipped: restore runs the whole dump inside
# one transaction of its own (one fsync instead of one per row on the SD card).
_TXN_RE = re.compile(r"^\s*(BEGIN|COMMIT|END)(\s+(DEFERRED|IMMEDIATE|EXCLUSIVE))?(\s+TRANSACTION)?\s*;\s*$",
                     re.IGNORECASE)


def _restore_authorizer(action: int, arg1: Optional[str], arg2: Optional[str],
                        db_name: Optional[str], source: Optional[str]) -> int:
    if action in _RESTORE_ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_UPDATE and arg1 in _SCHEMA_TABLES:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_DELETE and arg1 == "sqlite_sequence":
        return sqlite3.SQLITE_OK
    # Newer iterdump() emits "PRAGMA foreign_keys=OFF;" when the source has FK
    # violations. Harmless; every other PRAGMA (writable_schema!) is refused.
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() == "foreign_keys":
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _execute_dump(text: IO[str], scratch_path: Path) -> int:
    """Run the dump into a brand-new scratch DB under the restore authorizer.
    Returns the number of statements executed."""
    scratch_path.unlink(missing_ok=True)
    n = 0
    with closing(sqlite3.connect(scratch_path, isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode = OFF")   # throwaway file: no rollback journal needed
        conn.execute("PRAGMA synchronous = OFF")
        _apply_limits(conn)
        conn.execute("BEGIN")
        conn.set_authorizer(_restore_authorizer)
        try:
            for stmt in iter_statements(text):
                if _TXN_RE.match(stmt):
                    continue
                if _CREATE_RE.match(stmt) and _EXPR_DEFAULT_RE.search(stmt):
                    raise RestoreError(f"backup SQL statement {n + 1} uses an expression "
                                       "DEFAULT, which real backups never contain")
                try:
                    conn.execute(stmt)
                except sqlite3.Error as exc:
                    raise RestoreError(f"backup SQL statement {n + 1} failed: {exc}") from exc
                n += 1
        finally:
            conn.set_authorizer(None)
        conn.execute("COMMIT")
    return n


# --- Restore: copying into the canonical schema ------------------------------------


def _normalise_settings(conn: sqlite3.Connection) -> list[str]:
    """Re-validate every settings row the way a live write is validated. Unknown
    keys are deleted; rows that fail validation are deleted (so the key falls
    back to its default); valid rows are rewritten in canonical encoding. The
    webhooks/views blobs get the same per-entry cleaning their own endpoints
    apply. Returns the keys that were reset."""
    from . import webhooks as webhooks_service   # local: webhooks imports _http → settings

    reset: list[str] = []
    for key, raw in conn.execute("SELECT key, value FROM settings").fetchall():
        if key not in settings_store.DEFAULTS:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            continue
        try:
            value = settings_store._validated(key, settings_store._coerce(key, raw))
            if key == "webhooks_json":
                value = json.dumps(webhooks_service.clean_endpoints(json.loads(value)))
            elif key == "saved_views_json":
                value = json.dumps(settings_store.clean_saved_views(json.loads(value)))
            encoded = json.dumps(value, allow_nan=False)
            if len(encoded) > settings_store._MAX_VALUE_BYTES:
                raise ValueError("oversized")
        except settings_store._REJECT_ERRORS:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            reset.append(key)
            continue
        if encoded != raw:
            conn.execute("UPDATE settings SET value = ? WHERE key = ?", (encoded, key))
    return sorted(reset)


def _copy_into_canonical(scratch_path: Path, final_path: Path) -> dict[str, Any]:
    """Build `final_path` from the canonical schema and copy the scratch DB's rows
    into it, table by table, matching columns by name. Tables the canonical schema
    doesn't know are dropped; rows violating its constraints are skipped."""
    final_path.unlink(missing_ok=True)
    copied: dict[str, int] = {}
    with closing(sqlite3.connect(final_path)) as conn:
        _apply_limits(conn)
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")   # must precede the first table
        settings_store.ensure_schema(conn)
        canonical = [r[0] for r in conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        conn.execute("ATTACH DATABASE ? AS src", (str(scratch_path),))
        try:
            src_tables = {r[0] for r in conn.execute(
                "SELECT name FROM src.sqlite_master WHERE type = 'table'")}
            for table in canonical:   # names come from OUR schema — safe to interpolate
                if table not in src_tables:
                    continue
                src_info = conn.execute(f'PRAGMA src.table_xinfo("{table}")').fetchall()
                # hidden != 0 → generated / hidden column. A VIRTUAL generated column
                # is computed on read, i.e. on THIS (un-authorized) connection, so a
                # dump could smuggle e.g. randomblob() past the authorizer through it.
                if any(r[6] for r in src_info):
                    raise RestoreError(f"backup table {table!r} uses generated columns")
                src_cols = {r[1] for r in src_info}
                cols = [r[1] for r in conn.execute(f'PRAGMA main.table_info("{table}")')
                        if r[1] in src_cols]
                if not cols:
                    continue
                col_list = ", ".join(f'"{c}"' for c in cols)
                cur = conn.execute(
                    # bandit B608 false positive: only ? placeholders / our own schema's table+column names are interpolated
                    f'INSERT OR IGNORE INTO main."{table}" ({col_list}) '  # nosec B608
                    f'SELECT {col_list} FROM src."{table}"')
                copied[table] = cur.rowcount
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE src")
        reset = _normalise_settings(conn)
        # A restore lands at user_version 0, exactly like the old raw restore did, so
        # init_db() re-runs the (idempotent) migrations — the v4 one deliberately
        # treats a restored backup as an existing install.
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RestoreError(f"restored database failed integrity check: {check}")
    return {"rows": copied, "settings_reset": reset}


def restore_zip(source: "bytes | IO[bytes]", final_path: Path) -> dict[str, Any]:
    """Validate a backup zip and build a ready-to-swap DB at `final_path`.

    `source` is the zip as bytes or a seekable binary file (the import endpoint
    passes the upload's disk-spooled temp file, so the zip never sits in RAM).
    Synchronous and heavy: run it in a worker thread. Raises RestoreError (or
    sqlite3.Error) on refusal; never touches the live DB."""
    scratch_path = final_path.with_name(final_path.name + ".scratch")
    t0 = time.monotonic()
    fileobj = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    try:
        try:
            zf = zipfile.ZipFile(fileobj)
        except (zipfile.BadZipFile, ValueError, OSError):
            raise RestoreError("Not a valid zip file") from None
        with zf:
            info = _select_sql_entry(zf)
            try:
                with zf.open(info) as raw:
                    capped = _CappedReader(raw, MAX_SQL_BYTES)
                    text = io.TextIOWrapper(io.BufferedReader(capped, 1 << 16),
                                            encoding="utf-8", errors="replace", newline="")
                    statements = _execute_dump(text, scratch_path)
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
                # Corrupt / truncated deflate stream, CRC mismatch, or an unsupported
                # compression method (NotImplementedError) / encrypted entry.
                raise RestoreError(f"could not read {info.filename}: {exc}") from None
            except RuntimeError as exc:   # zipfile: "File ... is encrypted, password required"
                if isinstance(exc, RecursionError):
                    raise
                raise RestoreError(f"could not read {info.filename}: {exc}") from None
        report = _copy_into_canonical(scratch_path, final_path)
    except BaseException:
        final_path.unlink(missing_ok=True)
        raise
    finally:
        scratch_path.unlink(missing_ok=True)
    report["statements"] = statements
    report["seconds"] = round(time.monotonic() - t0, 2)
    return report
