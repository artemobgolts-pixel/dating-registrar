"""Atomically published SQLite-only snapshots, safe with a live WAL database.

These 14 local snapshots do not promise media recovery. Use recovery.py with
all writers stopped for a complete DB + uploads recovery point. Telegram
delivery remains a separate, explicitly configured operation in tasks.py.
"""

from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "app.db"
BACKUP_DIR = DATA_DIR / "backups"
KEEP = 14
# Legacy final-looking files may be incomplete. They cannot suppress a retry.
_PUBLISHED = re.compile(r"app-\d{8}-\d{6}-[0-9a-f]{32}\.db\Z")


def _sync_file(path: Path) -> None:
    # Windows FlushFileBuffers requires a handle opened for writing.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    # Directory fsync is supported by the Linux production filesystem; Windows
    # has no equivalent through os.open. File fsync and atomic rename still run.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_snapshot(source: Path, destination: Path) -> None:
    """Write and validate an unpublished snapshot; never create a missing source."""
    deadline = time.monotonic() + 60

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup exceeded 60 seconds")

    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.05)
            # A standalone snapshot must not depend on external WAL/SHM files.
            dst.execute("PRAGMA journal_mode=DELETE")
            if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("Backup failed SQLite integrity_check")
    _sync_file(destination)


def _published_backups() -> list[Path]:
    return sorted((path for path in BACKUP_DIR.glob("app-*.db")
                   if _PUBLISHED.fullmatch(path.name) and path.is_file()
                   and not path.is_symlink()), key=lambda path: (path.stat().st_mtime_ns, path.name))


def make_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / (
        f"app-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}.db")
    fd, temporary = tempfile.mkstemp(prefix=".app-", suffix=".partial", dir=BACKUP_DIR)
    os.close(fd)
    partial = Path(temporary)
    try:
        _write_snapshot(DB_PATH, partial)
        os.replace(partial, dest)
        _sync_directory(BACKUP_DIR)
    finally:
        # SQLite can leave sidecars after an interrupted copy, too. These names
        # belong to this invocation; other writers' partials are never touched.
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(partial) + suffix).unlink(missing_ok=True)
    # Rotate only successfully published snapshots, and only after publication.
    # Legacy snapshots are preserved for operator review, never trusted as fresh.
    previous = [path for path in _published_backups() if path != dest]
    for old in previous[:max(0, len(previous) - max(KEEP - 1, 0))]:
        old.unlink(missing_ok=True)
    return dest


def make_backup_if_stale(hours: int = 20) -> Path | None:
    """Only a snapshot from the atomic publisher can count as fresh."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = _published_backups()
    if files:
        newest = max(f.stat().st_mtime for f in files)
        if time.time() - newest < hours * 3600:
            return None
    return make_backup()


if __name__ == "__main__":
    print(f"Бэкап готов: {make_backup()}")
