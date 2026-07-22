"""Закрытие опросов и продуктовые Telegram-уведомления о голосовании."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

import db
import notification_outbox as outbox
import notify
import voting
from config import BASE_URL
from helpers import fmt_when, now_naive


log = logging.getLogger("voting_events")
RESULT_TTL = timedelta(days=30)


def _as_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None, microsecond=0)
    except (TypeError, ValueError):
        return None


def _date(conn: sqlite3.Connection, date_id: int | None):
    if date_id is None:
        return None
    return conn.execute("SELECT * FROM dates WHERE id=?", (date_id,)).fetchone()


def _voters(conn: sqlite3.Connection, category_id: int):
    return conn.execute(
        "SELECT b.user_id, b.date_id, b.participation_withdrawn_at, "
        "COALESCE(NULLIF(u.display_name,''), NULLIF(u.tg_username,''), 'Участник') AS name "
        "FROM bookings b LEFT JOIN users u ON u.id=b.user_id "
        "WHERE b.category_id=? AND b.user_id IS NOT NULL ORDER BY b.id",
        (category_id,),
    ).fetchall()


def queue_deadline_reminder(conn: sqlite3.Connection, category_id: int,
                            user_id: int, *, now: datetime | None = None) -> None:
    """Ставит одному проголосовавшему напоминание за два часа до дедлайна."""
    current = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    cat = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if not cat or cat["voting_status"] != voting.STATUS_OPEN:
        cancel_deadline_reminder(conn, category_id, user_id)
        return
    deadline = _as_dt(cat["voting_deadline"])
    if not deadline:
        cancel_deadline_reminder(conn, category_id, user_id)
        return
    send_at = deadline - timedelta(hours=2)
    if send_at < current:
        cancel_deadline_reminder(conn, category_id, user_id)
        return
    cancel_deadline_reminder(conn, category_id, user_id)
    text = notify.card(
        "⏳ До конца голосования осталось 2 часа",
        f"«{notify.esc(cat['name'])}»",
        "Можно проверить или изменить свой выбор.",
        f"\n{BASE_URL}/c/{cat['link_token']}",
    )
    outbox.enqueue(
        conn, user_id=user_id, kind="voting_deadline",
        event_key=(f"category:{category_id}:deadline:{deadline.isoformat()}:"
                   f"user:{user_id}"),
        text=text, send_at=send_at, expires_at=deadline,
    )


def cancel_deadline_reminder(conn: sqlite3.Connection, category_id: int,
                             user_id: int) -> int:
    stamp = now_naive().replace(microsecond=0).isoformat(sep="T")
    cur = conn.execute(
        "UPDATE notification_outbox SET cancelled_at=?, claimed_at=NULL, "
        "last_error='deadline_changed_or_no_votes', updated_at=? "
        "WHERE sent_at IS NULL AND cancelled_at IS NULL AND kind='voting_deadline' "
        "AND event_key LIKE ?",
        (stamp, stamp,
         f"category:{int(category_id)}:deadline:%:user:{int(user_id)}"),
    )
    return max(cur.rowcount, 0)


def cancel_category_notifications(conn: sqlite3.Connection, category_id: int,
                                  reason: str = "category_removed") -> int:
    category_id = int(category_id)
    total = outbox.cancel(
        conn, event_prefix=f"category:{int(category_id)}:", reason=reason,
    )
    # Часть уведомлений (изменение/удаление отдельного свидания или голоса)
    # имеет date:/booking:-ключ. При удалении категории их также нужно погасить,
    # иначе пользователь получит уже недействующую гостевую ссылку.
    cat = conn.execute(
        "SELECT link_token FROM categories WHERE id=?", (category_id,),
    ).fetchone()
    token = cat["link_token"] if cat else None
    stamp = now_naive().replace(microsecond=0).isoformat(sep="T")
    key_fragment = f":category:{category_id}:"
    if token:
        cur = conn.execute(
            "UPDATE notification_outbox SET cancelled_at=?, claimed_at=NULL, "
            "last_error=?, updated_at=? "
            "WHERE sent_at IS NULL AND cancelled_at IS NULL "
            "AND (instr(event_key, ?) > 0 OR instr(text, ?) > 0)",
            (stamp, reason, stamp, key_fragment, f"{BASE_URL}/c/{token}"),
        )
    else:
        cur = conn.execute(
            "UPDATE notification_outbox SET cancelled_at=?, claimed_at=NULL, "
            "last_error=?, updated_at=? "
            "WHERE sent_at IS NULL AND cancelled_at IS NULL "
            "AND instr(event_key, ?) > 0",
            (stamp, reason, stamp, key_fragment),
        )
    return total + max(cur.rowcount, 0)


def queue_category_outcome(conn: sqlite3.Connection, state: voting.CategoryState,
                           *, now: datetime | None = None) -> None:
    """Дедуплицированно ставит владельцу и всем голосовавшим текущий итог."""
    current = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    cat = conn.execute("SELECT * FROM categories WHERE id=?",
                       (state.category_id,)).fetchone()
    if not cat or state.status not in voting.CLOSED_STATUSES:
        return
    expiry = current + RESULT_TTL
    owner_id = int(cat["owner_id"])
    admin_url = f"{BASE_URL}/admin/categories/{state.category_id}"
    public_url = f"{BASE_URL}/c/{cat['link_token']}"

    winner = _date(conn, state.winner_date_id)
    if state.status == voting.STATUS_NO_WINNER:
        owner_text = notify.card(
            "🤍 Никто не выбрал",
            f"«{notify.esc(cat['name'])}»",
            "Категория завершена без победителя.", f"\n{admin_url}")
    elif state.status == voting.STATUS_TIE:
        leaders = [_date(conn, did) for did in state.leader_date_ids]
        names = ", ".join(f"«{notify.esc(d['name'])}»" for d in leaders if d)
        owner_text = notify.card(
            "⚖️ В голосовании ничья",
            f"«{notify.esc(cat['name'])}»",
            f"Лидируют: {names}", "Выбери победителя вручную:", f"\n{admin_url}")
    else:
        winner_name = notify.esc(winner["name"] if winner else "Свидание")
        count = state.vote_counts.get(state.winner_date_id or -1, 0)
        owner_text = notify.card(
            "🏆 Победитель определён",
            f"«{notify.esc(cat['name'])}»",
            f"«{winner_name}» · {count} голос(а)", f"\n{admin_url}")

    outbox.enqueue(
        conn, user_id=owner_id, kind=f"voting_{state.status}",
        event_key=f"category:{state.category_id}:status:{state.status}:owner:{owner_id}",
        text=owner_text, expires_at=expiry,
    )

    voter_rows = _voters(conn, state.category_id)
    by_user: dict[int, list] = {}
    for row in voter_rows:
        by_user.setdefault(int(row["user_id"]), []).append(row)

    for user_id, choices in by_user.items():
        chose_winner = any(r["date_id"] == state.winner_date_id for r in choices)
        if state.status == voting.STATUS_TIE:
            title = "⚖️ Голосование завершилось ничьёй"
            detail = "Организатор выбирает победителя среди лидеров. Мы пришлём итог."
        elif state.status == voting.STATUS_RESOLVED and chose_winner:
            title = "💝 Ваш вариант победил"
            detail = "Вы участвуете в победившем свидании."
        elif state.status == voting.STATUS_RESOLVED:
            title = "Голосование завершено"
            detail = f"Победил вариант «{notify.esc(winner['name'] if winner else 'Свидание')}»."
        else:
            title = "Голосование завершено"
            detail = "До дедлайна никто не сделал выбор."
        text = notify.card(title, f"«{notify.esc(cat['name'])}»", detail,
                           f"\n{public_url}")
        outbox.enqueue(
            conn, user_id=user_id, kind=f"voting_{state.status}",
            event_key=(f"category:{state.category_id}:status:{state.status}:"
                       f"user:{user_id}"), text=text, expires_at=expiry,
        )

    # Напоминания получают только участники победившего варианта. Количество
    # может быть меньше capacity — это не меняет победителя.
    if state.status == voting.STATUS_RESOLVED and winner:
        starts = _as_dt(winner["starts_at"])
        if starts:
            for row in voter_rows:
                if row["date_id"] != state.winner_date_id or row["participation_withdrawn_at"]:
                    continue
                user_id = int(row["user_id"])
                for hours in (24, 2):
                    send_at = starts - timedelta(hours=hours)
                    if send_at < current:
                        continue
                    # Суточное напоминание протухает к двухчасовому порогу, чтобы
                    # после поздней привязки Telegram или простоя воркера оба
                    # сообщения не пришли одновременно перед самым событием.
                    expires_at = (starts - timedelta(hours=2)
                                  if hours == 24 else starts)
                    text = notify.card(
                        f"🔔 Свидание через {hours} ч" if hours != 24 else "🔔 Свидание завтра",
                        f"«{notify.esc(winner['name'])}»",
                        f"Когда: {fmt_when(winner['starts_at'], winner['ends_at'])} (мск)",
                        f"📍 {notify.esc(winner['place'])}" if winner["place"] else "",
                        f"\n{public_url}",
                    )
                    outbox.enqueue(
                        conn, user_id=user_id, kind="winner_reminder",
                        event_key=(f"category:{state.category_id}:date:{winner['id']}:"
                                   f"reminder:{hours}h:at:{starts.isoformat()}:user:{user_id}"),
                        text=text, send_at=send_at, expires_at=expires_at,
                    )


def close_due_once(conn: sqlite3.Connection | None = None, *,
                   category_id: int | None = None,
                   now: datetime | None = None) -> int:
    """Закрывает наступившие опросы, ставит итоги и коммитит одной транзакцией."""
    own = conn is None
    if own:
        conn = db.connect()
    assert conn is not None
    current = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    where = "voting_status='open' AND closed_at IS NULL AND voting_deadline<=?"
    args: list[object] = [current.isoformat(sep="T")]
    if category_id is not None:
        where += " AND id=?"
        args.append(category_id)
    rows = conn.execute(f"SELECT id FROM categories WHERE {where}", args).fetchall()
    count = 0
    try:
        for row in rows:
            try:
                state = voting.close_category(conn, int(row["id"]), now=current)
                queue_category_outcome(conn, state, now=current)
                count += 1
            except voting.VotingError:
                log.exception("Не удалось закрыть категорию id=%s", row["id"])
        if count:
            conn.commit()
        return count
    finally:
        if own:
            conn.close()


def cancel_date_reminders(conn: sqlite3.Connection, date_id: int,
                          reason: str = "date_changed",
                          category_id: int | None = None) -> int:
    stamp = now_naive().replace(microsecond=0).isoformat(sep="T")
    pattern = (f"category:{int(category_id)}:date:{int(date_id)}:reminder:%"
               if category_id is not None
               else f"%:date:{int(date_id)}:reminder:%")
    cur = conn.execute(
        "UPDATE notification_outbox SET cancelled_at=?, claimed_at=NULL, "
        "last_error=?, updated_at=? WHERE sent_at IS NULL AND cancelled_at IS NULL "
        "AND kind='winner_reminder' AND event_key LIKE ?",
        (stamp, reason, stamp, pattern),
    )
    return max(cur.rowcount, 0)


def cancel_user_winner_reminders(conn: sqlite3.Connection, category_id: int,
                                 user_id: int) -> int:
    stamp = now_naive().replace(microsecond=0).isoformat(sep="T")
    cur = conn.execute(
        "UPDATE notification_outbox SET cancelled_at=?, claimed_at=NULL, "
        "last_error='participant_withdrew', updated_at=? "
        "WHERE sent_at IS NULL AND cancelled_at IS NULL AND kind='winner_reminder' "
        "AND event_key LIKE ?",
        (stamp, stamp,
         f"category:{int(category_id)}:%:reminder:%:user:{int(user_id)}"),
    )
    return max(cur.rowcount, 0)


def queue_date_changed(conn: sqlite3.Connection, date_id: int,
                       changed_labels: list[str], *, now: datetime | None = None) -> int:
    """Уведомляет релевантных голосовавших и перестраивает напоминания."""
    if not changed_labels:
        return 0
    current = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    d = _date(conn, date_id)
    if not d:
        return 0
    rows = conn.execute(
        "SELECT DISTINCT b.user_id, c.id AS category_id, c.name AS category_name, "
        "c.link_token, c.voting_status, c.winner_date_id, b.participation_withdrawn_at "
        "FROM bookings b JOIN categories c ON c.id=b.category_id "
        "WHERE b.date_id=? AND b.user_id IS NOT NULL",
        (date_id,),
    ).fetchall()
    queued = 0
    stamp_key = current.strftime("%Y%m%dT%H%M%S")
    resolved_categories: set[int] = set()
    for row in rows:
        # После результата изменения получают только реальные участники
        # победителя; проигравшие варианты больше ни на кого не влияют.
        if row["voting_status"] == voting.STATUS_RESOLVED:
            if row["winner_date_id"] != date_id or row["participation_withdrawn_at"]:
                continue
            resolved_categories.add(int(row["category_id"]))
        text = notify.card(
            "✏️ Свидание изменилось",
            f"«{notify.esc(d['name'])}» · {notify.esc(row['category_name'])}",
            "Изменено: " + notify.esc(", ".join(changed_labels)) + ".",
            f"Когда: {fmt_when(d['starts_at'], d['ends_at'])} (мск)" if d["starts_at"] else "",
            f"📍 {notify.esc(d['place'])}" if d["place"] else "",
            f"\n{BASE_URL}/c/{row['link_token']}",
        )
        outbox.enqueue(
            conn, user_id=int(row["user_id"]), kind="date_changed",
            event_key=(f"date:{date_id}:changed:{stamp_key}:category:"
                       f"{row['category_id']}:user:{row['user_id']}"),
            text=text, expires_at=current + RESULT_TTL,
        )
        queued += 1

    cancel_date_reminders(conn, date_id, reason="date_changed")
    for category_id in resolved_categories:
        queue_category_outcome(conn, voting.get_category_state(conn, category_id),
                               now=current)
    return queued


def queue_date_removed(conn: sqlite3.Connection, date_id: int, date_name: str,
                       category_id: int, category_name: str,
                       category_token: str | None, *, now: datetime | None = None) -> int:
    """Ставит уведомления до удаления голосов/варианта из открытой категории."""
    current = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    bookings = conn.execute(
        "SELECT id, user_id FROM bookings WHERE date_id=? AND category_id=? "
        "AND user_id IS NOT NULL", (date_id, category_id)
    ).fetchall()
    for booking in bookings:
        user_id = int(booking["user_id"])
        text = notify.card(
            "🗑 Вариант удалён из голосования",
            f"«{notify.esc(date_name)}» · {notify.esc(category_name)}",
            "Ваш голос за этот вариант снят. Можно выбрать другой.",
            f"\n{BASE_URL}/c/{category_token}" if category_token else "",
        )
        outbox.enqueue(
            conn, user_id=user_id, kind="date_removed",
            event_key=(f"booking:{int(booking['id'])}:date_removed:user:{user_id}"),
            text=text, expires_at=current + RESULT_TTL,
        )
    cancel_date_reminders(conn, date_id, reason="date_removed",
                          category_id=category_id)
    return len(bookings)


def queue_vote_removed_by_owner(conn: sqlite3.Connection, *, booking_id: int,
                                user_id: int | None, category_id: int,
                                category_name: str, date_name: str,
                                category_token: str | None) -> None:
    if user_id is None:
        return
    text = notify.card(
        "ℹ️ Организатор снял ваш голос",
        f"«{notify.esc(date_name)}» · {notify.esc(category_name)}",
        "Если голосование ещё идёт, можно выбрать другой вариант.",
        f"\n{BASE_URL}/c/{category_token}" if category_token else "",
    )
    outbox.enqueue(
        conn, user_id=int(user_id), kind="vote_removed",
        event_key=f"booking:{int(booking_id)}:removed:user:{int(user_id)}",
        text=text, expires_at=now_naive() + RESULT_TTL,
    )


def queue_participant_withdrawal(conn: sqlite3.Connection, *, booking_id: int,
                                 owner_id: int, participant_name: str,
                                 category_name: str, date_name: str,
                                 category_id: int) -> None:
    text = notify.card(
        "↩️ Участник отказался от свидания",
        f"«{notify.esc(date_name)}» · {notify.esc(category_name)}",
        f"Кто: {notify.esc(participant_name)}",
        "Итог голосования не изменился.",
        f"\n{BASE_URL}/admin/categories/{int(category_id)}",
    )
    outbox.enqueue(
        conn, user_id=int(owner_id), kind="participant_withdrawal",
        event_key=f"booking:{int(booking_id)}:participant_withdrawal",
        text=text, expires_at=now_naive() + RESULT_TTL,
    )
