"""Надёжная очередь пользовательских Telegram-уведомлений.

Роуты кладут сообщения в ``notification_outbox`` в той же транзакции, что и
продуктовое событие. Фоновый обработчик отправляет их позже. Telegram chat_id
не фиксируется в очереди: он определяется по ``user_id`` перед каждой попыткой,
так что ожидающее сообщение можно доставить после поздней привязки бота.

Гарантия доставки — at-least-once. Уникальный ``event_key`` не даёт поставить
одно логическое событие дважды; короткая аренда ``claimed_at`` защищает от
параллельных обработчиков. Редкий сбой процесса между ответом Telegram и записью
``sent_at`` теоретически может привести к повтору — у Bot API нет ключа
идемпотентности для sendMessage.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

import db
import notify
from helpers import now_naive


log = logging.getLogger("notification_outbox")

Sender = Callable[[int | str, str], bool]
CLAIM_LEASE = timedelta(minutes=5)
MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class ProcessStats:
    """Итог одного прохода обработчика очереди."""

    claimed: int = 0
    sent: int = 0
    failed: int = 0
    deferred: int = 0
    expired: int = 0


def _dt(value: datetime | str | None, *, default: datetime | None = None) -> datetime:
    if value is None:
        if default is None:
            raise ValueError("datetime value is required")
        return default.replace(tzinfo=None, microsecond=0)
    if isinstance(value, datetime):
        # В приложении все persisted timestamps — naive МСК. Для aware-значений
        # сохраняем показанное локальное время: публичный API этого модуля
        # используется уже после нормализации времени формой/доменным слоем.
        return value.replace(tzinfo=None, microsecond=0)
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None, microsecond=0)
    except (TypeError, ValueError) as exc:
        raise ValueError("timestamp must be an ISO datetime") from exc


def _iso(value: datetime | str | None, *, default: datetime | None = None) -> str:
    return _dt(value, default=default).isoformat(sep="T")


def enqueue(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    kind: str,
    event_key: str,
    text: str,
    send_at: datetime | str | None = None,
    expires_at: datetime | str | None = None,
    now: datetime | str | None = None,
) -> int:
    """Ставит/обновляет одно логическое уведомление и возвращает его id.

    Повторный ``event_key`` обновляет ещё не отправленную запись (это удобно
    для переноса напоминания). Уже отправленная запись неизменна и тем самым
    дедуплицирует повторный вызов. Функция не делает commit: вызывающий может
    атомарно сохранить событие и его уведомление одной транзакцией.
    """
    kind = (kind or "").strip()
    event_key = (event_key or "").strip()
    text = (text or "").strip()
    if not kind:
        raise ValueError("kind is required")
    if not event_key:
        raise ValueError("event_key is required")
    if not text:
        raise ValueError("text is required")

    now_dt = _dt(now, default=now_naive())
    created = _iso(now_dt)
    due = _iso(send_at, default=now_dt)
    expiry = _iso(expires_at) if expires_at is not None else None
    conn.execute(
        """
        INSERT INTO notification_outbox(
            user_id, kind, event_key, text, send_at, expires_at,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(event_key) DO UPDATE SET
            user_id=excluded.user_id,
            kind=excluded.kind,
            text=excluded.text,
            send_at=excluded.send_at,
            expires_at=excluded.expires_at,
            cancelled_at=NULL,
            claimed_at=NULL,
            attempts=0,
            last_error=NULL,
            updated_at=excluded.updated_at
        WHERE notification_outbox.sent_at IS NULL
        """,
        (user_id, kind, event_key, text, due, expiry, created, created),
    )
    row = conn.execute(
        "SELECT id FROM notification_outbox WHERE event_key=?", (event_key,)
    ).fetchone()
    if row is None:  # pragma: no cover — INSERT/UNIQUE гарантируют строку
        raise RuntimeError("notification was not queued")
    return int(row["id"] if isinstance(row, sqlite3.Row) else row[0])


def cancel(
    conn: sqlite3.Connection,
    *,
    event_key: str | None = None,
    event_prefix: str | None = None,
    user_id: int | None = None,
    kind: str | None = None,
    reason: str = "cancelled",
    now: datetime | str | None = None,
) -> int:
    """Отменяет подходящие неотправленные сообщения и возвращает их число.

    Нужен хотя бы один селектор — случайно отменить всю очередь нельзя.
    ``event_prefix`` сравнивается как буквальный префикс, а не LIKE-шаблон.
    """
    if event_key is None and event_prefix is None and user_id is None and kind is None:
        raise ValueError("at least one cancellation selector is required")
    where = ["sent_at IS NULL", "cancelled_at IS NULL"]
    args: list[object] = []
    if event_key is not None:
        where.append("event_key=?")
        args.append(event_key)
    if event_prefix is not None:
        escaped = event_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("event_key LIKE ? ESCAPE '\\'")
        args.append(escaped + "%")
    if user_id is not None:
        where.append("user_id=?")
        args.append(user_id)
    if kind is not None:
        where.append("kind=?")
        args.append(kind)
    stamp = _iso(now, default=now_naive())
    cur = conn.execute(
        f"UPDATE notification_outbox SET cancelled_at=?, claimed_at=NULL, "
        f"last_error=?, updated_at=? WHERE {' AND '.join(where)}",
        (stamp, (reason or "cancelled")[:MAX_ERROR_LENGTH], stamp, *args),
    )
    return max(cur.rowcount, 0)


def _retry_at(now: datetime, attempts_after_failure: int) -> str:
    # 30s, 60s, 120s … до часа. expires_at остаётся абсолютным рубежом.
    delay = min(3600, 30 * (2 ** min(max(attempts_after_failure - 1, 0), 7)))
    return _iso(now + timedelta(seconds=delay))


def _claim_token(now: datetime) -> str:
    """Возвращает сравнимый по времени и уникальный маркер аренды.

    ``claimed_at`` участвует и в проверке давности аренды, и в проверке её
    владельца. Одной точности до секунды недостаточно: два обработчика могут
    получить одинаковый ``now`` и принять чужой claim за свой. Длинная
    десятичная дробная часть сохраняет хронологическую сортировку ISO-строки и
    одновременно служит уникальным идентификатором конкретного claim.
    """
    return f"{_iso(now)}.{uuid.uuid4().int:039d}"


def process_due(
    conn: sqlite3.Connection | None = None,
    *,
    now: datetime | str | None = None,
    limit: int = 50,
    sender: Sender | None = None,
) -> ProcessStats:
    """Обрабатывает одну пачку наступивших уведомлений синхронно.

    Переданный ``conn`` должен быть выделенным соединением: обработчик делает
    commit после claim и после результата каждой сетевой попытки. В фоне вызов
    запускается через ``asyncio.to_thread``.
    """
    if limit < 1:
        return ProcessStats()
    own = conn is None
    if own:
        conn = db.connect()
    assert conn is not None
    sender = sender or notify.send_to
    now_dt = _dt(now, default=now_naive())
    stamp = _iso(now_dt)
    lease_before = _iso(now_dt - CLAIM_LEASE)
    expired = claimed = sent = failed = deferred = 0
    try:
        cur = conn.execute(
            """
            UPDATE notification_outbox
            SET cancelled_at=?, claimed_at=NULL, last_error='expired', updated_at=?
            WHERE sent_at IS NULL AND cancelled_at IS NULL
              AND expires_at IS NOT NULL AND expires_at<=?
            """,
            (stamp, stamp, stamp),
        )
        expired = max(cur.rowcount, 0)
        conn.commit()

        rows = conn.execute(
            """
            SELECT id FROM notification_outbox
            WHERE sent_at IS NULL AND cancelled_at IS NULL AND send_at<=?
              AND (expires_at IS NULL OR expires_at>?)
              AND (claimed_at IS NULL OR claimed_at<=?)
            ORDER BY send_at, id
            LIMIT ?
            """,
            (stamp, stamp, lease_before, limit),
        ).fetchall()

        for candidate in rows:
            notification_id = int(candidate["id"] if isinstance(candidate, sqlite3.Row)
                                  else candidate[0])
            claim_token = _claim_token(now_dt)
            cur = conn.execute(
                """
                UPDATE notification_outbox SET claimed_at=?, updated_at=?
                WHERE id=? AND sent_at IS NULL AND cancelled_at IS NULL
                  AND send_at<=? AND (expires_at IS NULL OR expires_at>?)
                  AND (claimed_at IS NULL OR claimed_at<=?)
                """,
                (claim_token, stamp, notification_id, stamp, stamp, lease_before),
            )
            conn.commit()
            if cur.rowcount != 1:
                continue
            claimed += 1

            row = conn.execute(
                """
                SELECT o.id, o.user_id, o.text, o.attempts, u.telegram_id
                FROM notification_outbox o
                LEFT JOIN users u ON u.id=o.user_id AND u.bot_linked=1
                    AND u.is_active=1 AND u.telegram_id IS NOT NULL
                    AND u.telegram_id<>0
                WHERE o.id=? AND o.claimed_at=?
                  AND o.sent_at IS NULL AND o.cancelled_at IS NULL
                """,
                (notification_id, claim_token),
            ).fetchone()
            if row is None:
                continue
            chat_id = row["telegram_id"]
            if chat_id is None:
                # Это не ошибка доставки и не попытка: оставляем сообщение due,
                # чтобы следующий проход увидел свежую привязку Telegram. При
                # этом сдвигаем его за новые due-события: иначе первые LIMIT
                # непривязанных пользователей навсегда блокировали бы очередь.
                cur = conn.execute(
                    "UPDATE notification_outbox SET claimed_at=NULL, "
                    "last_error='telegram_not_linked', send_at=?, updated_at=? "
                    "WHERE id=? AND claimed_at=? AND sent_at IS NULL "
                    "AND cancelled_at IS NULL",
                    (_iso(now_dt + timedelta(seconds=30)), stamp,
                     notification_id, claim_token),
                )
                conn.commit()
                if cur.rowcount == 1:
                    deferred += 1
                continue

            error: str | None = None
            try:
                delivered = sender(chat_id, row["text"]) is True
                if not delivered:
                    error = "telegram_send_failed"
            except Exception as exc:  # sender может быть пользовательским в тесте
                delivered = False
                error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
                log.warning("Ошибка отправки outbox id=%s: %s", notification_id, exc)

            attempts = int(row["attempts"]) + 1
            if delivered:
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET sent_at=?, claimed_at=NULL, attempts=?, last_error=NULL,
                        updated_at=?
                    WHERE id=? AND claimed_at=? AND sent_at IS NULL
                      AND cancelled_at IS NULL
                    """,
                    (stamp, attempts, stamp, notification_id, claim_token),
                )
                sent += 1
            else:
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET claimed_at=NULL, attempts=?, last_error=?, send_at=?,
                        updated_at=?
                    WHERE id=? AND claimed_at=? AND sent_at IS NULL
                      AND cancelled_at IS NULL
                    """,
                    (attempts, error, _retry_at(now_dt, attempts), stamp,
                     notification_id, claim_token),
                )
                failed += 1
            conn.commit()
    finally:
        if own:
            conn.close()
    return ProcessStats(claimed=claimed, sent=sent, failed=failed,
                        deferred=deferred, expired=expired)
