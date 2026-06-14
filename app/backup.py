"""Консистентные бэкапы базы (через sqlite backup API — безопасно при WAL).

Снимки складываются в /data/backups/app-ГГГГММДД-ЧЧММСС.db, хранятся
последние 14. Приложение само делает снимок раз в сутки; вручную:

    docker compose exec app python backup.py
"""

import os
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "app.db"
BACKUP_DIR = DATA_DIR / "backups"
KEEP = 14


def make_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"app-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    # держим только последние KEEP снимков
    files = sorted(BACKUP_DIR.glob("app-*.db"))
    for old in files[:-KEEP]:
        old.unlink(missing_ok=True)
    return dest


def make_backup_if_stale(hours: int = 20) -> Path | None:
    """Делает снимок, если свежего (моложе `hours` часов) ещё нет."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = list(BACKUP_DIR.glob("app-*.db"))
    if files:
        newest = max(f.stat().st_mtime for f in files)
        if time.time() - newest < hours * 3600:
            return None
    return make_backup()


if __name__ == "__main__":
    print(f"Бэкап готов: {make_backup()}")
