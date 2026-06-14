"""Фоновые задачи: авто-архив просроченных свиданий и ежесуточный бэкап."""

import asyncio
import logging
import sqlite3

import backup
import db
from helpers import _parse, now_iso, now_naive

log = logging.getLogger("tasks")


def autoarchive_once(conn: sqlite3.Connection | None = None) -> int:
    """Переносит в архив свидания, чья дата прошла.

    Правило: если задан конец диапазона — истекает в этот момент;
    если задано только одно время — истекает в конце того же дня (23:59 МСК).
    Без даты/времени — только ручной перенос.
    """
    own = conn is None
    if own:
        conn = db.connect()
    n = now_naive()
    archived = 0
    rows = conn.execute(
        "SELECT id, starts_at, ends_at FROM dates "
        "WHERE archived_at IS NULL AND (starts_at IS NOT NULL OR ends_at IS NOT NULL)"
    ).fetchall()
    for r in rows:
        end = _parse(r["ends_at"])
        if end is None:
            start = _parse(r["starts_at"])
            if start is None:
                continue
            end = start.replace(hour=23, minute=59, second=59)
        if end < n:
            conn.execute("UPDATE dates SET archived_at=? WHERE id=?", (now_iso(), r["id"]))
            archived += 1
    if archived:
        conn.commit()
    if own:
        conn.close()
    return archived


async def autoarchive_loop() -> None:
    while True:
        await asyncio.sleep(600)
        try:
            n = autoarchive_once()
            if n:
                log.info("Авто-архив: перенесено свиданий — %d", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка авто-архивации")


async def backup_loop() -> None:
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            made = backup.make_backup_if_stale(hours=20)
            if made:
                log.info("Авто-бэкап: %s", made)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка авто-бэкапа")
