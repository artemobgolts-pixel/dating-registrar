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
import social_events
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
    """Переносит в архив завершённые события.

    Правило: если задан конец диапазона — истекает в этот момент;
    если задано только одно время — истекает в конце того же дня (23:59 МСК).
    Событие без времени также завершается, когда оно стало победителем
    голосования и реальный (не отказавшийся) участник победившего варианта
    оставил обзор. Одной отметки «Хочу сходить» или обзора постороннего для
    архивации недостаточно.

    category_id/owner_id оставлены для адресных служебных запусков. Обычные
    GET-запросы эту функцию не вызывают и не конкурируют за writer-lock:
    глобальный проход выполняется при старте и затем фоновым циклом.
    """
    own = conn is None
    if own:
        conn = db.connect()
    n = now_naive()
    archive_ids: set[int] = set()
    scope = ""
    scope_params: list = []
    if category_id is not None:
        scope += (" AND EXISTS (SELECT 1 FROM date_categories scoped_dc "
                  "WHERE scoped_dc.date_id=d.id AND scoped_dc.category_id=?)")
        scope_params.append(category_id)
    if owner_id is not None:
        scope += " AND d.owner_id=?"
        scope_params.append(owner_id)

    # Отдельные partial indexes превращают глобальный минутный проход из
    # сканирования всех активных событий в адресные range-seek только по уже
    # наступившим границам. Повторная проверка через _parse ниже намеренно
    # остаётся: легаси-строки с некорректным ISO-временем не архивируются.
    start_cutoff = min(
        n.replace(hour=0, minute=0, second=0, microsecond=0),
        n - social_events.DEFAULT_EVENT_DURATION,
    )
    rows = conn.execute(
        "SELECT d.id, d.starts_at, d.ends_at FROM dates d "
        "INDEXED BY idx_dates_autoarchive_ends_due "
        "WHERE d.archived_at IS NULL AND d.ends_at IS NOT NULL "
        f"AND d.ends_at<?{scope}",
        (n.isoformat(sep="T"), *scope_params),
    ).fetchall()
    rows += conn.execute(
        "SELECT d.id, d.starts_at, d.ends_at FROM dates d "
        "INDEXED BY idx_dates_autoarchive_starts_due "
        "WHERE d.archived_at IS NULL AND d.ends_at IS NULL "
        "AND d.starts_at IS NOT NULL "
        f"AND d.starts_at<?{scope}",
        (start_cutoff.isoformat(sep="T"), *scope_params),
    ).fetchall()
    # Невалидный legacy ends_at раньше (и по-прежнему) означает fallback к
    # starts_at. Строки, лексически меньшие now, уже попали в первый seek;
    # оставшуюся половину берём по due-start partial index, а _parse отличает
    # реальный будущий конец от невалидного значения без полного скана.
    rows += conn.execute(
        "SELECT d.id, d.starts_at, d.ends_at FROM dates d "
        "INDEXED BY idx_dates_autoarchive_end_fallback_start "
        "WHERE d.archived_at IS NULL AND d.ends_at IS NOT NULL "
        "AND d.starts_at IS NOT NULL AND d.starts_at<? AND d.ends_at>=?"
        f"{scope}",
        (start_cutoff.isoformat(sep="T"), n.isoformat(sep="T"), *scope_params),
    ).fetchall()
    for r in rows:
        end = _parse(r["ends_at"])
        if end is None:
            start = _parse(r["starts_at"])
            if start is None:
                continue
            # Поздний старт не должен архивироваться раньше принятой в обзорах
            # длительности «начало + 3 часа». Для обычного времени сохраняем
            # прежнее правило конца календарного дня.
            end = max(
                start.replace(hour=23, minute=59, second=59),
                start + social_events.DEFAULT_EVENT_DURATION,
            )
        if end < n:
            archive_ids.add(int(r["id"]))

    # У недатированного победителя нет календарного конца, но опубликованный
    # обзор участника однозначно подтверждает, что встреча состоялась. Связываем
    # review с booking того же пользователя и той же resolved-категории: иначе
    # любой человек с прямой /d-ссылкой и отметкой want мог бы заархивировать
    # чужое событие своим обзором.
    completed_where = (
        "c.voting_status='resolved' AND c.winner_date_id IS NOT NULL "
        "AND d.archived_at IS NULL "
        "AND EXISTS ("
        " SELECT 1 FROM bookings b "
        " JOIN date_reviews r ON r.date_id=b.date_id AND r.user_id=b.user_id "
        " WHERE b.category_id=c.id AND b.date_id=c.winner_date_id "
        " AND b.user_id IS NOT NULL AND b.participation_withdrawn_at IS NULL"
        ") "
        # Одна строка dates может участвовать сразу в нескольких категориях.
        # Не прячем победителя глобально, пока где-то ещё идёт голосование или
        # владелец не разрешил ничью: иначе вариант исчезнет из второго опроса.
        "AND NOT EXISTS ("
        " SELECT 1 FROM date_categories pending_dc "
        " JOIN categories pending_c ON pending_c.id=pending_dc.category_id "
        " WHERE pending_dc.date_id=d.id "
        " AND pending_c.voting_status IN ('open','tie')"
        ")"
    )
    completed_params: list = []
    if category_id is not None:
        completed_where += " AND c.id=?"
        completed_params.append(category_id)
    if owner_id is not None:
        completed_where += " AND d.owner_id=?"
        completed_params.append(owner_id)
    archive_ids.update(int(row["id"]) for row in conn.execute(
        "SELECT DISTINCT d.id FROM categories c "
        "JOIN dates d ON d.id=c.winner_date_id "
        f"WHERE {completed_where}",
        completed_params,
    ).fetchall())

    archived = 0
    if archive_ids:
        stamp = now_iso()
        for date_id in archive_ids:
            cursor = conn.execute(
                "UPDATE dates SET archived_at=? WHERE id=? AND archived_at IS NULL",
                (stamp, date_id),
            )
            changed = max(0, cursor.rowcount)
            archived += changed
            if not changed:
                continue
            # Переход в архив — надёжная точка для вопроса «Удалось сходить?».
            # Outbox дедуплицирован по user/date, а review_queue имеет составной
            # PK, поэтому повторный минутный цикл не создаст дублей.
            social_events.queue_archive_review_fanout(conn, date_id, now=n)
    if archived:
        conn.commit()
    if own:
        conn.close()
    return archived


async def autoarchive_loop() -> None:
    while True:
        # GET-страницы больше не пишут в SQLite. Due-индексы v32 делают
        # короткий минутный проход адресным и оставляют статус почти мгновенным.
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
