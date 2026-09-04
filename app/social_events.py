"""«Хочу сходить», отзывы и связанные отложенные уведомления.

Коллекция создаёт отдельную копию события, а эта подсистема всегда хранит
связь с исходной строкой ``dates``. Один пользователь получает не больше одного
review-prompt на событие, даже если оно состоит в нескольких категориях.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import notification_outbox
import notify
from config import BASE_URL
from helpers import now_naive


REVIEW_PROMPT_TTL = timedelta(days=30)
UNDATED_DEADLINE_GRACE = timedelta(days=1)
DEFAULT_EVENT_DURATION = timedelta(hours=3)


def _moment(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None, microsecond=0)
    except (TypeError, ValueError):
        return None


def _review_due_from_values(starts_at: str | None, ends_at: str | None,
                            created_at: str | None,
                            deadline_values) -> datetime | None:
    deadlines = [parsed for parsed in map(_moment, deadline_values) if parsed is not None]
    latest_deadline = max(deadlines) if deadlines else None
    event_end = _moment(ends_at)
    if event_end is None:
        start = _moment(starts_at)
        event_end = start + DEFAULT_EVENT_DURATION if start else None
    if event_end is None:
        if latest_deadline is not None:
            return latest_deadline
        # Категории после v28 всегда имеют дедлайн. Последний fallback нужен
        # для старой /d-ссылки события вообще без категории: новый пользователь
        # всё равно не должен навсегда остаться без формы отзыва.
        base = _moment(created_at)
        return base + UNDATED_DEADLINE_GRACE if base else None
    return max(event_end, latest_deadline) if latest_deadline else event_end


def _want_expires_from_values(starts_at: str | None, ends_at: str | None,
                              deadline_values) -> datetime | None:
    """Явный срок показа плана в «Хочу сходить».

    Если событие участвует в голосовании, пользовательский дедлайн является
    источником правды: после самого позднего из связанных дедлайнов план больше
    не показывается, даже когда сама встреча назначена на будущее. Для события
    без категории используем его окончание (либо старт + обычные три часа), а
    полностью недатированный план без дедлайна оставляем без автоистечения.
    """
    deadlines = [parsed for parsed in map(_moment, deadline_values) if parsed is not None]
    if deadlines:
        return max(deadlines)
    event_end = _moment(ends_at)
    if event_end is not None:
        return event_end
    start = _moment(starts_at)
    return start + DEFAULT_EVENT_DURATION if start else None


def review_due(conn: sqlite3.Connection, date_id: int) -> datetime | None:
    """Момент, после которого уместно спрашивать «Удалось сходить?».

    Дедлайн категории в этой модели завершает голосование *до* события, поэтому
    сам по себе не означает, что встреча уже прошла. Для датированного события
    ждём ``ends_at`` либо ``starts_at + 3 часа`` и берём максимум с самым поздним
    дедлайном всех категорий. Если времени у события нет, обязательный дедлайн
    сам становится моментом prompt. Суточный fallback остаётся только у старой
    отдельной /d-ссылки без даты и без связанной подборки.
    """
    context = _review_event_context(conn, date_id)
    return context[1] if context else None


def _review_event_context(conn: sqlite3.Connection, date_id: int):
    """Загружает инварианты события и вычисляет due одним SQL-запросом."""
    rows = conn.execute(
        "SELECT d.name, d.share_token, d.starts_at, d.ends_at, d.created_at, "
        "c.voting_deadline FROM dates d "
        "LEFT JOIN date_categories dc ON dc.date_id=d.id "
        "LEFT JOIN categories c ON c.id=dc.category_id "
        "WHERE d.id=?",
        (date_id,),
    ).fetchall()
    if not rows:
        return None
    event = rows[0]
    due = _review_due_from_values(
        event["starts_at"], event["ends_at"], event["created_at"],
        (row["voting_deadline"] for row in rows if row["voting_deadline"]),
    )
    return event, due


def prompt_key(date_id: int, user_id: int) -> str:
    return f"review:date:{int(date_id)}:user:{int(user_id)}:prompt"


def archive_prompt_key(date_id: int, user_id: int) -> str:
    return f"review:date:{int(date_id)}:user:{int(user_id)}:archived"


def review_user_ids(conn: sqlite3.Connection, date_id: int) -> list[int]:
    """Пользователи, которым событие действительно доступно для отзыва.

    Отметка «Хочу сходить» даёт право независимо от результата голосования;
    бронь — только участнику победившего варианта, который не отказался.
    """
    return [int(row["user_id"]) for row in conn.execute(
        "SELECT user_id FROM date_wants WHERE date_id=? "
        "UNION "
        "SELECT b.user_id FROM bookings b JOIN categories c ON c.id=b.category_id "
        "WHERE b.date_id=? AND b.user_id IS NOT NULL "
        "AND b.participation_withdrawn_at IS NULL "
        "AND c.voting_status='resolved' AND c.winner_date_id=b.date_id",
        (date_id, date_id),
    ).fetchall()]


def review_notification_user_ids(conn: sqlite3.Connection, date_id: int) -> list[int]:
    """Получатели Telegram-вопроса об итогах посещения.

    Простое «Хочу сходить» не подтверждает участие и потому даёт право вручную
    оставить отзыв после события, но никогда не инициирует уведомление.
    """
    return [int(row["user_id"]) for row in conn.execute(
        "SELECT DISTINCT b.user_id FROM bookings b "
        "JOIN categories c ON c.id=b.category_id "
        "WHERE b.date_id=? AND b.user_id IS NOT NULL "
        "AND b.participation_withdrawn_at IS NULL "
        "AND c.voting_status='resolved' AND c.winner_date_id=b.date_id",
        (date_id,),
    ).fetchall()]


def mark_review_waiting(conn: sqlite3.Connection, date_id: int, user_id: int,
                        reason: str = "due", *, now: datetime | None = None,
                        require_available: bool = True) -> bool:
    """Добавляет событие в пользовательскую очередь «Ждут отзыва»."""
    if reason not in {"due", "declined", "review_deleted"}:
        raise ValueError("Некорректная причина ожидания отзыва")
    current = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    if require_available and not review_available(
            conn, date_id, user_id, now=current):
        return False
    if not require_available and not conn.execute(
        "SELECT 1 FROM dates WHERE id=?", (date_id,),
    ).fetchone():
        return False
    if conn.execute(
        "SELECT 1 FROM date_reviews WHERE user_id=? AND date_id=?",
        (user_id, date_id),
    ).fetchone():
        return False
    stamp = current.isoformat(sep="T")
    conn.execute(
        "INSERT INTO review_queue(user_id,date_id,reason,created_at,updated_at) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id,date_id) DO UPDATE SET "
        "reason=excluded.reason, updated_at=excluded.updated_at, dismissed_at=NULL",
        (user_id, date_id, reason, stamp, stamp),
    )
    return True


def clear_review_waiting(conn: sqlite3.Connection, date_id: int,
                         user_id: int) -> int:
    current = now_naive().replace(tzinfo=None, microsecond=0)
    stamp = current.isoformat(sep="T")
    row = conn.execute(
        "SELECT dismissed_at FROM review_queue WHERE user_id=? AND date_id=?",
        (user_id, date_id),
    ).fetchone()
    if row:
        if row["dismissed_at"] is not None:
            return 0
        cur = conn.execute(
            "UPDATE review_queue SET dismissed_at=? "
            "WHERE user_id=? AND date_id=? AND dismissed_at IS NULL",
            (stamp, user_id, date_id),
        )
        return max(cur.rowcount, 0)

    if conn.execute(
        "SELECT 1 FROM date_reviews WHERE user_id=? AND date_id=?",
        (user_id, date_id),
    ).fetchone() or not review_available(conn, date_id, user_id, now=current):
        return 0
    conn.execute(
        "INSERT INTO review_queue("
        "user_id,date_id,reason,created_at,updated_at,dismissed_at"
        ") VALUES(?,?,?,?,?,?)",
        (user_id, date_id, "due", stamp, stamp, stamp),
    )
    return 1


def cancel_review_prompt(conn: sqlite3.Connection, date_id: int, user_id: int,
                         reason: str = "review_not_needed") -> int:
    return notification_outbox.cancel(
        conn,
        event_prefix=f"review:date:{int(date_id)}:user:{int(user_id)}:",
        kind="review_prompt",
        reason=reason,
    )


def queue_review_prompt(conn: sqlite3.Connection, date_id: int,
                        user_id: int) -> int | None:
    """Создаёт/переносит prompt только подтверждённому участнику-победителю."""
    if not _review_notification_entitled(conn, date_id, user_id):
        cancel_review_prompt(conn, date_id, user_id, "review_not_available")
        return None
    row = conn.execute(
        "SELECT name, share_token FROM dates WHERE id=?",
        (date_id,),
    ).fetchone()
    if not row:
        cancel_review_prompt(conn, date_id, user_id, "event_removed")
        return None
    if conn.execute(
        "SELECT 1 FROM date_reviews WHERE user_id=? AND date_id=?",
        (user_id, date_id),
    ).fetchone():
        cancel_review_prompt(conn, date_id, user_id, "review_exists")
        return None
    due = review_due(conn, date_id)
    if due is None or not row["share_token"]:
        cancel_review_prompt(conn, date_id, user_id, "event_has_no_review_due")
        return None
    now = now_naive()
    # Если отметку поставили по очень старой ссылке, исторический due уже вышел
    # за TTL. Даём prompt сейчас, а не создаём заведомо просроченную запись.
    if due + REVIEW_PROMPT_TTL <= now:
        due = now
    text = notify.card(
        "⭐ Удалось сходить?",
        f"«{notify.esc(row['name'])}»",
        "Поставь оценку — текст можно добавить по желанию.",
    )
    return notification_outbox.enqueue(
        conn,
        user_id=user_id,
        kind="review_prompt",
        event_key=prompt_key(date_id, user_id),
        text=text,
        action_url=f"{BASE_URL}/d/{row['share_token']}#review",
        action_label="Оставить отзыв",
        send_at=due,
        expires_at=due + REVIEW_PROMPT_TTL,
    )


def queue_archive_review_prompt(conn: sqlite3.Connection, date_id: int,
                                user_id: int, *, now: datetime | None = None) -> int | None:
    """Ставит отдельное уведомление именно после перехода события в архив.

    Обычный prompt мог быть доставлен сразу после времени встречи. Отдельный
    idempotency-key гарантирует уведомление подтверждённому участнику после
    архивации и не размножается на повторных фоновых проходах.
    """
    current = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    if not _review_notification_entitled(conn, date_id, user_id):
        return None
    # Не отправляем кнопку раньше, чем по ней действительно откроется форма.
    # Это особенно важно для поздних starts-only событий и старых импортов.
    if not review_available(conn, date_id, user_id, now=current):
        return None
    if conn.execute(
        "SELECT 1 FROM date_reviews WHERE user_id=? AND date_id=?",
        (user_id, date_id),
    ).fetchone():
        return None
    row = conn.execute(
        "SELECT name, share_token FROM dates WHERE id=?", (date_id,),
    ).fetchone()
    if not row or not row["share_token"]:
        return None
    return notification_outbox.enqueue(
        conn,
        user_id=user_id,
        kind="review_prompt",
        event_key=archive_prompt_key(date_id, user_id),
        text=notify.card(
            "⭐ Событие завершилось",
            f"«{notify.esc(row['name'])}»",
            "Удалось сходить? Если да — оставь отзыв.",
        ),
        action_url=f"{BASE_URL}/d/{row['share_token']}#review",
        action_label="Оставить отзыв",
        send_at=current,
        expires_at=current + REVIEW_PROMPT_TTL,
        now=current,
    )


def _cancel_prompt_users(conn: sqlite3.Connection, date_id: int, user_ids,
                         reason: str, *, now: datetime | None = None) -> int:
    return notification_outbox.cancel_user_prefixes(
        conn,
        {
            int(user_id): f"review:date:{int(date_id)}:user:{int(user_id)}:"
            for user_id in user_ids
        },
        kind="review_prompt", reason=reason, now=now,
    )


def _queue_review_prompt_fanout(conn: sqlite3.Connection, date_id: int,
                                entitlement: dict[int, bool]) -> int:
    """Общий batch-path queue/reconcile без запросов на каждого пользователя."""
    if not entitlement:
        return 0
    current = now_naive().replace(tzinfo=None, microsecond=0)
    entitled = {user_id for user_id, value in entitlement.items() if value}
    unavailable = set(entitlement) - entitled
    if unavailable:
        _cancel_prompt_users(
            conn, date_id, unavailable, "review_not_available", now=current,
        )
    if not entitled:
        return 0

    context = _review_event_context(conn, date_id)
    if context is None:
        _cancel_prompt_users(conn, date_id, entitled, "event_removed", now=current)
        return 0
    event, due = context
    reviewed = {
        int(row["user_id"])
        for row in conn.execute(
            "SELECT user_id FROM date_reviews WHERE date_id=?",
            (date_id,),
        ).fetchall()
        if int(row["user_id"]) in entitled
    }
    if reviewed:
        _cancel_prompt_users(conn, date_id, reviewed, "review_exists", now=current)
    recipients = entitled - reviewed
    if due is None or not event["share_token"]:
        _cancel_prompt_users(
            conn, date_id, recipients, "event_has_no_review_due", now=current,
        )
        return 0

    # Старые due не создают сразу просроченные строки — то же правило, что у
    # одиночного queue_review_prompt, но вычисленное один раз на весь fan-out.
    if due + REVIEW_PROMPT_TTL <= current:
        due = current
    text = notify.card(
        "⭐ Удалось сходить?",
        f"«{notify.esc(event['name'])}»",
        "Поставь оценку — текст можно добавить по желанию.",
    )
    notifications = [
        {
            "user_id": user_id,
            "kind": "review_prompt",
            "event_key": prompt_key(date_id, user_id),
            "text": text,
            "action_url": f"{BASE_URL}/d/{event['share_token']}#review",
            "action_label": "Оставить отзыв",
            "send_at": due,
            "expires_at": due + REVIEW_PROMPT_TTL,
            "now": current,
        }
        for user_id in sorted(recipients)
    ]
    return notification_outbox.enqueue_many(conn, notifications)


def queue_review_prompts_for_date(conn: sqlite3.Connection, date_id: int) -> int:
    """Пересчитывает prompt подтверждённых участников-победителей."""
    return _queue_review_prompt_fanout(
        conn, date_id,
        {user_id: True for user_id in review_notification_user_ids(conn, date_id)},
    )


def reconcile_review_prompts_for_date(conn: sqlite3.Connection, date_id: int) -> int:
    """Пересчитывает prompts и для пользователей, потерявших право на отзыв.

    Обычный fan-out идёт только по текущим получателям. При повторном открытии
    голосования бывший победитель перестаёт давать право на отзыв, поэтому его
    участники уже не попадают в :func:`review_user_ids` и старый отложенный
    prompt иначе остаётся активным. Берём объединение всех отметок «Хочу» и
    всех бюллетеней события: ``queue_review_prompt`` либо перенесёт актуальную
    запись, либо отменит её, если других оснований для отзыва больше нет.
    """
    entitlement = {
        int(row["user_id"]): bool(row["entitled"])
        for row in conn.execute(
            "SELECT candidates.user_id, MAX(candidates.entitled) AS entitled FROM ("
            # Прежние версии могли поставить want-пользователю prompt. Держим
            # его в наборе как не имеющего notification-права, чтобы отменить.
            " SELECT user_id, 0 AS entitled FROM date_wants WHERE date_id=? "
            " UNION ALL "
            " SELECT b.user_id, CASE WHEN b.participation_withdrawn_at IS NULL "
            "  AND c.voting_status='resolved' AND c.winner_date_id=b.date_id "
            "  THEN 1 ELSE 0 END AS entitled "
            " FROM bookings b JOIN categories c ON c.id=b.category_id "
            " WHERE b.date_id=? AND b.user_id IS NOT NULL"
            ") candidates GROUP BY candidates.user_id",
            (date_id, date_id),
        ).fetchall()
    }
    return _queue_review_prompt_fanout(conn, date_id, entitlement)


def queue_archive_review_fanout(conn: sqlite3.Connection, date_id: int, *,
                                now: datetime | None = None) -> tuple[int, int]:
    """Пакетно ставит archive-prompts и строки «Ждут отзыва».

    Возвращает ``(notifications, waiting_rows)``. Внутренние SELECT-инварианты
    выполняются один раз на событие, а обе записи fan-out пишутся пачками.
    """
    current = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    user_ids = review_user_ids(conn, date_id)
    if not user_ids:
        return 0, 0
    context = _review_event_context(conn, date_id)
    if context is None:
        return 0, 0
    event, due = context
    reviewed = {
        int(row["user_id"])
        for row in conn.execute(
            "SELECT user_id FROM date_reviews WHERE date_id=?",
            (date_id,),
        ).fetchall()
    }
    queue_states = {
        int(row["user_id"]): row["dismissed_at"]
        for row in conn.execute(
            "SELECT user_id,dismissed_at FROM review_queue WHERE date_id=?",
            (date_id,),
        ).fetchall()
    }
    already_waiting = {
        user_id for user_id, dismissed_at in queue_states.items()
        if dismissed_at is None
    }
    dismissed = {
        user_id for user_id, dismissed_at in queue_states.items()
        if dismissed_at is not None
    }
    eligible = [
        user_id for user_id in user_ids
        if user_id not in reviewed and user_id not in dismissed
        and (user_id in already_waiting or (due is not None and current >= due))
    ]
    if not eligible:
        return 0, 0

    stamp = current.isoformat(sep="T")
    for start in range(0, len(eligible), 150):
        batch = eligible[start:start + 150]
        placeholders = ",".join("(?,?,?,?,?)" for _ in batch)
        conn.execute(
            "INSERT INTO review_queue(user_id,date_id,reason,created_at,updated_at) "
            f"VALUES {placeholders} ON CONFLICT(user_id,date_id) DO UPDATE SET "
            "reason=excluded.reason, updated_at=excluded.updated_at "
            "WHERE review_queue.dismissed_at IS NULL",
            tuple(
                value
                for user_id in batch
                for value in (user_id, date_id, "due", stamp, stamp)
            ),
        )

    notification_users = set(review_notification_user_ids(conn, date_id))
    notification_eligible = [
        user_id for user_id in eligible if user_id in notification_users
    ]
    notifications = []
    if event["share_token"]:
        text = notify.card(
            "⭐ Событие завершилось",
            f"«{notify.esc(event['name'])}»",
            "Удалось сходить? Если да — оставь отзыв.",
        )
        notifications = [
            {
                "user_id": user_id,
                "kind": "review_prompt",
                "event_key": archive_prompt_key(date_id, user_id),
                "text": text,
                "action_url": f"{BASE_URL}/d/{event['share_token']}#review",
                "action_label": "Оставить отзыв",
                "send_at": current,
                "expires_at": current + REVIEW_PROMPT_TTL,
                "now": current,
            }
            for user_id in notification_eligible
        ]
    return notification_outbox.enqueue_many(conn, notifications), len(eligible)


def cancel_review_prompts_for_date(conn: sqlite3.Connection, date_id: int,
                                   reason: str = "event_removed") -> int:
    cancelled = notification_outbox.cancel(
        conn,
        event_prefix=f"review:date:{int(date_id)}:user:",
        kind="review_prompt",
        reason=reason,
    )
    for row in conn.execute(
        "SELECT id FROM date_reviews WHERE date_id=?", (date_id,),
    ).fetchall():
        cancelled += cancel_review_received(conn, int(row["id"]), reason)
    return cancelled


def _review_entitled(conn: sqlite3.Connection, date_id: int, user_id: int) -> bool:
    if conn.execute(
        "SELECT 1 FROM date_wants WHERE user_id=? AND date_id=?",
        (user_id, date_id),
    ).fetchone():
        return True
    return conn.execute(
        "SELECT 1 FROM bookings b JOIN categories c ON c.id=b.category_id "
        "WHERE b.user_id=? AND b.date_id=? "
        "AND b.participation_withdrawn_at IS NULL "
        "AND c.voting_status='resolved' AND c.winner_date_id=b.date_id LIMIT 1",
        (user_id, date_id),
    ).fetchone() is not None


def _review_notification_entitled(conn: sqlite3.Connection, date_id: int,
                                  user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM bookings b JOIN categories c ON c.id=b.category_id "
        "WHERE b.user_id=? AND b.date_id=? "
        "AND b.participation_withdrawn_at IS NULL "
        "AND c.voting_status='resolved' AND c.winner_date_id=b.date_id LIMIT 1",
        (user_id, date_id),
    ).fetchone() is not None


def review_available(conn: sqlite3.Connection, date_id: int, user_id: int,
                     *, now: datetime | None = None) -> bool:
    # Строка очереди создаётся только после уже наступившего due. Она сохраняет
    # право вернуться к отзыву, даже если затем пользователь снял «Хочу
    # сходить»/отказался от участия. Для review_deleted это также доказательство,
    # что отзыв об этом событии ранее действительно существовал.
    if conn.execute(
        "SELECT 1 FROM review_queue WHERE user_id=? AND date_id=?",
        (user_id, date_id),
    ).fetchone():
        return True
    if not _review_entitled(conn, date_id, user_id):
        return False
    due = review_due(conn, date_id)
    return due is not None and (now or now_naive()) >= due


def review_waiting_rows(conn: sqlite3.Connection, user_id: int, *,
                        now: datetime | None = None) -> list[dict]:
    """Возвращает полную пользовательскую очередь «Ждут отзыва».

    Явные строки ``review_queue`` сохраняют причины «отложен»/«удалён» и право
    вернуться после снятого выбора. Остальные выбранные события вычисляются на
    чтении: отметка «Хочу сходить» либо бронь победителя появляется сразу после
    ``review_due``, даже если фоновая автоархивация ещё не материализовала её.
    Tombstone не даёт явно удалённому упоминанию появиться снова.
    """
    current = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    candidates = conn.execute(
        "WITH entitled_dates(date_id) AS ("
        " SELECT date_id FROM date_wants WHERE user_id=?"
        " UNION "
        " SELECT b.date_id FROM bookings b "
        " JOIN categories winner ON winner.id=b.category_id "
        " WHERE b.user_id=? AND b.participation_withdrawn_at IS NULL "
        " AND winner.voting_status='resolved' "
        " AND winner.winner_date_id=b.date_id"
        " UNION "
        " SELECT date_id FROM review_queue "
        " WHERE user_id=? AND dismissed_at IS NULL"
        ") "
        "SELECT d.id AS date_id, d.name AS date_name, d.share_token, "
        "d.starts_at, d.ends_at, d.created_at AS event_created_at, "
        "rq.reason, rq.created_at AS queue_created_at, "
        "rq.updated_at AS queue_updated_at, rq.dismissed_at, "
        "MAX(CASE WHEN datetime(c.voting_deadline) IS NOT NULL "
        "THEN c.voting_deadline END) AS latest_deadline "
        # CROSS JOIN фиксирует маленький user-scoped CTE внешним циклом:
        # SQLite не должен выбирать глобальный scan dates вместо PK lookup.
        "FROM entitled_dates e CROSS JOIN dates d ON d.id=e.date_id "
        "LEFT JOIN review_queue rq ON rq.user_id=? AND rq.date_id=d.id "
        "LEFT JOIN date_reviews dr ON dr.user_id=? AND dr.date_id=d.id "
        "LEFT JOIN date_categories dc ON dc.date_id=d.id "
        "LEFT JOIN categories c ON c.id=dc.category_id "
        "WHERE dr.id IS NULL AND rq.dismissed_at IS NULL "
        "GROUP BY d.id",
        (user_id, user_id, user_id, user_id, user_id),
    ).fetchall()

    result: list[dict] = []
    for row in candidates:
        due = _review_due_from_values(
            row["starts_at"], row["ends_at"], row["event_created_at"],
            (row["latest_deadline"],),
        )
        if row["reason"] is None:
            # Старый standalone /d без даты сохраняет право открыть форму через
            # сутки, но сам по себе не является «прошедшим» событием. В
            # вычисляемую очередь он попадёт только с реальным временем либо
            # дедлайном подборки — ровно теми сигналами, что событие завершилось.
            has_completion_signal = bool(
                row["starts_at"] or row["ends_at"] or row["latest_deadline"]
            )
            if not has_completion_signal or due is None or current < due:
                continue
        stamp = row["queue_updated_at"] or due.isoformat(sep="T")
        result.append({
            "user_id": user_id,
            "date_id": int(row["date_id"]),
            "reason": row["reason"] or "due",
            "created_at": row["queue_created_at"] or stamp,
            "updated_at": stamp,
            "date_name": row["date_name"],
            "share_token": row["share_token"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
        })
    result.sort(key=lambda item: item["updated_at"], reverse=True)
    return result


def review_waiting_count(conn: sqlite3.Connection, user_id: int, *,
                         now: datetime | None = None) -> int:
    return len(review_waiting_rows(conn, user_id, now=now))


def current_want_date_ids(conn: sqlite3.Connection, user_id: int, date_ids,
                          *, now: datetime | None = None) -> set[int]:
    """Пакетно фильтрует планы профиля без N+1 запросов.

    Возвращает только неархивные события без уже созданного отзыва и до явного
    дедлайна (либо до окончания отдельного события, у которого категории нет).
    Данные событий, дедлайны категорий и существующие отзывы загружаются двумя
    запросами на пачку, а сама временная семантика общая с :func:`review_due`.
    """
    ids = list(dict.fromkeys(int(date_id) for date_id in date_ids))
    if not ids:
        return set()
    current = now or now_naive()
    visible: set[int] = set()
    batch_size = 400  # с запасом ниже стандартного SQLite limit переменных
    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]
        placeholders = ",".join("?" for _ in batch)
        want_states = conn.execute(
            f"SELECT w.date_id, r.id AS review_id FROM date_wants w "
            f"LEFT JOIN date_reviews r ON r.user_id=w.user_id AND r.date_id=w.date_id "
            f"WHERE w.user_id=? AND w.date_id IN ({placeholders})",
            (user_id, *batch),
        ).fetchall()
        wanted = {int(row["date_id"]) for row in want_states}
        reviewed = {
            int(row["date_id"]) for row in want_states if row["review_id"] is not None
        }
        grouped: dict[int, dict] = {}
        for row in conn.execute(
            f"SELECT d.id, d.starts_at, d.ends_at, d.archived_at, "
            f"c.voting_deadline FROM dates d "
            f"LEFT JOIN date_categories dc ON dc.date_id=d.id "
            f"LEFT JOIN categories c ON c.id=dc.category_id "
            f"WHERE d.id IN ({placeholders})",
            tuple(batch),
        ):
            date_id = int(row["id"])
            item = grouped.setdefault(date_id, {
                "starts_at": row["starts_at"],
                "ends_at": row["ends_at"],
                "archived_at": row["archived_at"],
                "deadlines": [],
            })
            if row["voting_deadline"]:
                item["deadlines"].append(row["voting_deadline"])
        for date_id, item in grouped.items():
            if (date_id not in wanted or date_id in reviewed
                    or item["archived_at"] is not None):
                continue
            expires = _want_expires_from_values(
                item["starts_at"], item["ends_at"], item["deadlines"],
            )
            if expires is None or current < expires:
                visible.add(date_id)
    return visible


def want_action_available(conn: sqlite3.Connection, date_id: int, user_id: int,
                          *, now: datetime | None = None) -> bool:
    """Можно ли сейчас добавить событие в «Хочу сходить».

    Уже опубликованный отзыв и наступивший дедлайн закрывают действие. Строку
    ``date_wants`` при этом не удаляем физически: она остаётся доказательством
    права показать форму отзыва после самой встречи.
    """
    if conn.execute(
        "SELECT 1 FROM date_reviews WHERE user_id=? AND date_id=?",
        (user_id, date_id),
    ).fetchone():
        return False
    rows = conn.execute(
        "SELECT d.starts_at, d.ends_at, d.archived_at, c.voting_deadline FROM dates d "
        "LEFT JOIN date_categories dc ON dc.date_id=d.id "
        "LEFT JOIN categories c ON c.id=dc.category_id "
        "WHERE d.id=?",
        (date_id,),
    ).fetchall()
    if not rows:
        return False
    if rows[0]["archived_at"] is not None:
        return False
    expires = _want_expires_from_values(
        rows[0]["starts_at"], rows[0]["ends_at"],
        (row["voting_deadline"] for row in rows if row["voting_deadline"]),
    )
    return expires is None or (now or now_naive()) < expires


def want_is_current(conn: sqlite3.Connection, date_id: int, user_id: int,
                    *, now: datetime | None = None) -> bool:
    """Можно ли ещё показывать событие во вкладке «Хочу сходить».

    После явного ``voting_deadline`` план исчезает из профиля; после появления
    отзыва живёт только во вкладке «Отзывы». Сама строка ``date_wants``
    сохраняется: она подтверждает право оставить отзыв по прямой ссылке и не
    ломает уже поставленный prompt.
    """
    return int(date_id) in current_want_date_ids(
        conn, user_id, [date_id], now=now,
    )


def queue_review_received(conn: sqlite3.Connection, review_id: int) -> int | None:
    """Уведомляет автора исходного события один раз после первой публикации."""
    row = conn.execute(
        "SELECT r.user_id, d.owner_id, d.name, "
        "COALESCE(u.display_name, u.tg_username, 'Пользователь') AS reviewer "
        "FROM date_reviews r JOIN dates d ON d.id=r.date_id "
        "JOIN users u ON u.id=r.user_id WHERE r.id=?",
        (review_id,),
    ).fetchone()
    if not row or int(row["owner_id"]) == int(row["user_id"]):
        return None
    return notification_outbox.enqueue(
        conn,
        user_id=int(row["owner_id"]),
        kind="review_received",
        event_key=f"review:{int(review_id)}:owner:{int(row['owner_id'])}:published",
        text=notify.card(
            "⭐ Новый отзыв о твоём событии",
            f"«{notify.esc(row['name'])}»",
            f"Автор: {notify.esc(row['reviewer'])}",
        ),
        action_url=f"{BASE_URL}/u/{int(row['user_id'])}?tab=reviews#review-{int(review_id)}",
        action_label="Посмотреть отзыв",
    )


def cancel_review_received(conn: sqlite3.Connection, review_id: int,
                           reason: str = "review_hidden") -> int:
    return notification_outbox.cancel(
        conn,
        event_prefix=f"review:{int(review_id)}:owner:",
        kind="review_received",
        reason=reason,
    )
