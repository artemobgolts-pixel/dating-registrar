"""Фоновые задачи: архив, бэкап и доставка пользовательских уведомлений."""

import asyncio
import gzip
import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path

import backup
import db
import notify
import notification_outbox
import voting_events
from config import TG_BACKUP_CHAT_ID
from helpers import _parse, now_iso, now_naive

log = logging.getLogger("tasks")

# Лимит Bot API на размер файла, отправляемого ботом, — 50 МБ. С запасом.
TG_DOC_LIMIT = 49 * 1024 * 1024


async def notification_outbox_loop() -> None:
    """Отправляет наступившие сообщения, не блокируя event loop HTTP-запросами."""
    while True:
        try:
            stats = await asyncio.to_thread(notification_outbox.process_due)
            if stats.sent or stats.failed or stats.expired or stats.skipped:
                log.info(
                    "Telegram outbox: отправлено=%d, ошибок=%d, ждут привязки=%d, "
                    "просрочено=%d, отключено пользователем=%d",
                    stats.sent, stats.failed, stats.deferred, stats.expired, stats.skipped,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка обработки Telegram outbox")
        await asyncio.sleep(30)


async def voting_close_loop() -> None:
    """Фиксирует результаты вскоре после дедлайна, даже без открытия страницы."""
    while True:
        try:
            closed = await asyncio.to_thread(voting_events.close_due_once)
            if closed:
                log.info("Закрыто голосований по дедлайну: %d", closed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка закрытия голосований")
        await asyncio.sleep(30)


def autoarchive_once(conn: sqlite3.Connection | None = None, *,
                     category_id: int | None = None,
                     owner_id: int | None = None) -> int:
    """Переносит в архив события, чья дата прошла.

    Правило: если задан конец диапазона — истекает в этот момент;
    если задано только одно время — истекает в конце того же дня (23:59 МСК).
    Без даты/времени — только ручной перенос.

    category_id/owner_id оставлены для адресных служебных запусков. Обычные
    GET-запросы эту функцию не вызывают и не конкурируют за writer-lock:
    глобальный проход выполняется при старте и затем фоновым циклом.
    """
    own = conn is None
    if own:
        conn = db.connect()
    n = now_naive()
    archived = 0
    where = "d.archived_at IS NULL AND (d.starts_at IS NOT NULL OR d.ends_at IS NOT NULL)"
    params: list = []
    if category_id is not None:
        where += (" AND d.id IN (SELECT date_id FROM date_categories "
                  "WHERE category_id=?)")
        params.append(category_id)
    if owner_id is not None:
        where += " AND d.owner_id=?"
        params.append(owner_id)
    rows = conn.execute(
        f"SELECT d.id, d.starts_at, d.ends_at FROM dates d WHERE {where}", params
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
        # GET-страницы больше не пишут в SQLite. Частичный индекс v25 делает
        # короткий минутный проход дешёвым и оставляет статус почти мгновенным.
        await asyncio.sleep(60)
        try:
            n = autoarchive_once()
            if n:
                log.info("Авто-архив: перенесено событий — %d", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка авто-архивации")


def ship_backup_to_tg(snapshot: Path) -> bool:
    """Жмёт снимок базы gzip и шлёт документом в TG_BACKUP_CHAT_ID.

    Включается только если задан TG_BACKUP_CHAT_ID (осознанный opt-in: в базе
    ПДн посторонних). Снимок сжимается во временный .db.gz; gzip обычно даёт
    5–10× и держит файл под лимитом Bot API. Если даже сжатый превышает лимит —
    предупреждаем, не шлём (TG отвергнет, а облачный бэкап всё равно есть)."""
    if not TG_BACKUP_CHAT_ID:
        return False
    tmp = Path(tempfile.gettempdir()) / (snapshot.name + ".gz")
    try:
        with open(snapshot, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout)
        size = tmp.stat().st_size
        if size > TG_DOC_LIMIT:
            log.warning("Бэкап %s сжат до %.1f МБ — больше лимита TG (49 МБ), "
                        "в Telegram не отправлен (облачный бэкап остаётся)",
                        snapshot.name, size / 1024 / 1024)
            return False
        caption = (f"📦 Бэкап базы <code>{notify.esc(snapshot.name)}</code>\n"
                   f"{size / 1024 / 1024:.1f} МБ (gzip)")
        return notify.send_document(TG_BACKUP_CHAT_ID, tmp, caption=caption,
                                    filename=tmp.name)
    except Exception:
        log.exception("Ошибка отправки бэкапа в Telegram")
        return False
    finally:
        tmp.unlink(missing_ok=True)


async def backup_loop() -> None:
    """Держит свежий локальный снимок базы без внешней отправки.

    Telegram и облако обслуживает единственный серверный cron через
    scripts/backup.sh. Разделение исключает дубли: перезапуск контейнера больше
    не создаёт второе независимое расписание отправки.
    """
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            made = backup.make_backup_if_stale(hours=20)
            if made:
                log.info("Локальный авто-бэкап: %s", made)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка авто-бэкапа")
