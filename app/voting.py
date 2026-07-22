"""Доменная логика голосования по свиданиям.

Модуль не зависит от FastAPI и не делает ``commit``: роут может атомарно
сохранить результат вместе с другими изменениями и только затем отправить
уведомления. Ошибки имеют стабильный машинный ``code`` и рекомендуемый HTTP-
статус, чтобы публичная и административная ручки показывали одинаковый ответ.

Термины:
* строка ``bookings`` — неизменяемый после ``closed_at`` голос (ballot);
* ``participation_withdrawn_at`` — отказ победившего участника после результата,
  он не удаляет голос и не пересчитывает победителя;
* ``dates.capacity`` применяется отдельно к каждой паре свидание/категория.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


MSK = ZoneInfo("Europe/Moscow")


CHOICE_SINGLE = "single"
CHOICE_MULTIPLE = "multiple"
CHOICE_MODES = frozenset({CHOICE_SINGLE, CHOICE_MULTIPLE})

STATUS_UNCONFIGURED = "unconfigured"
STATUS_OPEN = "open"
STATUS_TIE = "tie"
STATUS_RESOLVED = "resolved"
STATUS_NO_WINNER = "no_winner"
FINAL_STATUSES = frozenset({STATUS_RESOLVED, STATUS_NO_WINNER})
CLOSED_STATUSES = frozenset({STATUS_TIE, *FINAL_STATUSES})

MIN_CAPACITY = 1
MAX_CAPACITY = 100


class VotingError(Exception):
    """Ожидаемая доменная ошибка со стабильным кодом для HTTP/API слоя."""

    def __init__(self, code: str, message: str, *, status_code: int = 409,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class CategoryState:
    category_id: int
    status: str
    choice_mode: str | None
    voting_deadline: str | None
    closed_at: str | None
    resolved_at: str | None
    winner_date_id: int | None
    vote_counts: dict[int, int]
    total_votes: int
    leader_date_ids: tuple[int, ...]
    active_winner_participants: int


@dataclass(frozen=True)
class VoteResult:
    category_id: int
    date_id: int
    booking_id: int
    created: bool
    removed_date_ids: tuple[int, ...]
    current_date_ids: tuple[int, ...]
    date_votes: int
    capacity: int


@dataclass(frozen=True)
class RemoveVoteResult:
    category_id: int
    date_id: int
    removed: bool
    current_date_ids: tuple[int, ...]


@dataclass(frozen=True)
class WithdrawalResult:
    category_id: int
    winner_date_id: int
    booking_id: int
    withdrawn_at: str
    already_withdrawn: bool


@dataclass(frozen=True)
class CapacityResult:
    date_id: int
    capacity: int


def _fail(code: str, message: str, *, status_code: int = 409, **details: Any):
    raise VotingError(code, message, status_code=status_code, details=details)


def _parse_moment(value: str | datetime | None, field: str) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        raw = (value or "").strip()
        if not raw:
            _fail(f"{field}_required", "Укажи дату и время дедлайна",
                  status_code=400)
        try:
            moment = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            _fail(f"invalid_{field}", "Неверный формат даты и времени",
                  status_code=400)
    # Во всём проекте дата/время хранится как локальное наивное время МСК.
    # Не принимаем offset здесь, иначе строка перестанет быть совместима с
    # существующими starts_at/now_iso.
    if moment.tzinfo is not None:
        _fail(f"invalid_{field}", "Используй локальное время без часового пояса",
              status_code=400)
    return moment.replace(microsecond=0)


def _now(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(MSK).replace(tzinfo=None, microsecond=0)
    return _parse_moment(value, "now")


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat(sep="T")


def _participant_token(user_id: int) -> str:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        _fail("login_required", "Войди в аккаунт, чтобы проголосовать",
              status_code=401)
    if uid <= 0:
        _fail("login_required", "Войди в аккаунт, чтобы проголосовать",
              status_code=401)
    return f"u{uid}"


def _category(conn: sqlite3.Connection, category_id: int):
    row = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if not row:
        _fail("category_not_found", "Категория не найдена", status_code=404)
    return row


def _owned_category(conn: sqlite3.Connection, category_id: int, owner_id: int):
    row = conn.execute(
        "SELECT * FROM categories WHERE id=? AND owner_id=?", (category_id, owner_id)
    ).fetchone()
    if not row:
        # Не раскрываем существование чужой категории.
        _fail("category_not_found", "Категория не найдена", status_code=404)
    return row


def _lock_category(conn: sqlite3.Connection, category_id: int,
                   owner_id: int | None = None):
    """Берёт блокировку записи SQLite до проверки изменения голосования.

    Холостой UPDATE намеренный: SQLite сериализует писателей, поэтому следующие
    проверки видят последнюю сохранённую категорию и остаются валидными до
    commit или rollback вызывающей транзакции.
    """
    if owner_id is None:
        cursor = conn.execute(
            "UPDATE categories SET id=id WHERE id=?", (category_id,),
        )
    else:
        cursor = conn.execute(
            "UPDATE categories SET id=id WHERE id=? AND owner_id=?",
            (category_id, owner_id),
        )
    if cursor.rowcount == 0:
        _fail("category_not_found", "Категория не найдена", status_code=404)
    return _category(conn, category_id)


def _candidate(conn: sqlite3.Connection, category_id: int, date_id: int):
    row = conn.execute(
        "SELECT d.* FROM dates d "
        "JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE d.id=? AND dc.category_id=? "
        "AND d.archived_at IS NULL AND d.is_draft=0",
        (date_id, category_id),
    ).fetchone()
    if not row:
        _fail("date_not_available", "Свидание недоступно в этой категории",
              status_code=404)
    return row


def _counts(conn: sqlite3.Connection, category_id: int) -> dict[int, int]:
    """Счётчики включают отказавшихся: их бюллетень остаётся в результате."""
    counts = {
        int(row["date_id"]): int(row["n"])
        for row in conn.execute(
            "SELECT date_id, COUNT(*) AS n FROM bookings "
            "WHERE category_id=? GROUP BY date_id", (category_id,)
        )
    }
    # Нулевые варианты полезны UI и обязательны для корректного no_winner.
    for row in conn.execute(
        "SELECT date_id FROM date_categories WHERE category_id=?", (category_id,)
    ):
        counts.setdefault(int(row["date_id"]), 0)
    return dict(sorted(counts.items()))


def _leaders(counts: dict[int, int]) -> tuple[int, ...]:
    top = max(counts.values(), default=0)
    if top <= 0:
        return ()
    return tuple(date_id for date_id, count in counts.items() if count == top)


def _current_choices(conn: sqlite3.Connection, category_id: int,
                     guest_token: str) -> tuple[int, ...]:
    return tuple(int(row["date_id"]) for row in conn.execute(
        "SELECT date_id FROM bookings WHERE category_id=? AND guest_token=? "
        "ORDER BY created_at, id", (category_id, guest_token)
    ))


def get_category_state(conn: sqlite3.Connection, category_id: int) -> CategoryState:
    """Текущее сохранённое состояние без автоматического закрытия по времени."""
    cat = _category(conn, category_id)
    counts = _counts(conn, category_id)
    winner_id = cat["winner_date_id"]
    active = 0
    if winner_id is not None:
        active = int(conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE category_id=? AND date_id=? "
            "AND participation_withdrawn_at IS NULL", (category_id, winner_id)
        ).fetchone()[0])
    return CategoryState(
        category_id=int(cat["id"]),
        status=cat["voting_status"],
        choice_mode=cat["choice_mode"],
        voting_deadline=cat["voting_deadline"],
        closed_at=cat["closed_at"],
        resolved_at=cat["resolved_at"],
        winner_date_id=int(winner_id) if winner_id is not None else None,
        vote_counts=counts,
        total_votes=sum(counts.values()),
        leader_date_ids=_leaders(counts),
        active_winner_participants=active,
    )


def configure_category(conn: sqlite3.Connection, category_id: int, owner_id: int,
                       choice_mode: str, voting_deadline: str | datetime, *,
                       now: str | datetime | None = None) -> CategoryState:
    """Явно включает голосование; дедлайн по умолчанию не подставляется.

    Повторная настройка открытой категории допустима до старого дедлайна. Смена
    multiple → single запрещена, если у кого-то уже больше одного голоса.
    """
    cat = _lock_category(conn, category_id, owner_id)
    mode = (choice_mode or "").strip().lower()
    if mode not in CHOICE_MODES:
        _fail("invalid_choice_mode", "Режим должен быть single или multiple",
              status_code=400, choice_mode=choice_mode)

    current = _now(now)
    deadline = _parse_moment(voting_deadline, "deadline")
    if deadline <= current:
        _fail("deadline_not_future", "Дедлайн должен быть в будущем",
              status_code=400, voting_deadline=_iso(deadline))

    status = cat["voting_status"]
    if cat["closed_at"] is not None or status in CLOSED_STATUSES:
        _fail("voting_already_closed", "Завершённое голосование нельзя перенастроить")
    if status == STATUS_OPEN and cat["voting_deadline"]:
        old_deadline = _parse_moment(cat["voting_deadline"], "deadline")
        if old_deadline <= current:
            _fail("voting_deadline_passed", "Дедлайн уже наступил")
    if status not in {STATUS_UNCONFIGURED, STATUS_OPEN}:
        _fail("invalid_voting_state", "Некорректное состояние голосования",
              status_code=500, voting_status=status)

    # Дедлайн обязан быть строго раньше начала каждого активного датированного
    # кандидата. Свидания без starts_at ограничение не создают.
    for date_row in conn.execute(
        "SELECT d.id, d.name, d.starts_at FROM dates d "
        "JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE dc.category_id=? AND d.archived_at IS NULL AND d.is_draft=0 "
        "AND d.starts_at IS NOT NULL ORDER BY d.starts_at, d.id", (category_id,)
    ):
        try:
            starts = _parse_moment(date_row["starts_at"], "starts_at")
        except VotingError as exc:
            _fail("invalid_candidate_start", "У свидания некорректно задано время",
                  status_code=409, date_id=int(date_row["id"]), cause=exc.code)
        if deadline >= starts:
            _fail(
                "deadline_not_before_start",
                "Дедлайн должен быть раньше начала всех свиданий",
                status_code=400,
                date_id=int(date_row["id"]),
                date_name=date_row["name"],
                starts_at=date_row["starts_at"],
            )

    if mode == CHOICE_SINGLE:
        incompatible = conn.execute(
            "SELECT guest_token, COUNT(*) AS n FROM bookings WHERE category_id=? "
            "GROUP BY guest_token HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 1",
            (category_id,),
        ).fetchone()
        if incompatible:
            _fail(
                "existing_votes_incompatible",
                "Участник уже выбрал несколько вариантов; используй режим multiple",
                guest_token=incompatible["guest_token"],
                votes=int(incompatible["n"]),
            )

    conn.execute(
        "UPDATE categories SET choice_mode=?, voting_deadline=?, "
        "voting_status='open', closed_at=NULL, resolved_at=NULL, winner_date_id=NULL "
        "WHERE id=?",
        (mode, _iso(deadline), category_id),
    )
    return get_category_state(conn, category_id)


def _ensure_open(cat, current: datetime) -> None:
    status = cat["voting_status"]
    if status == STATUS_UNCONFIGURED:
        _fail("voting_unconfigured", "Владелец ещё не настроил голосование")
    if status != STATUS_OPEN or cat["closed_at"] is not None:
        _fail("voting_closed", "Голосование уже завершено")
    if cat["choice_mode"] not in CHOICE_MODES or not cat["voting_deadline"]:
        _fail("invalid_voting_state", "Голосование настроено не полностью",
              status_code=500)
    deadline = _parse_moment(cat["voting_deadline"], "deadline")
    if current >= deadline:
        _fail("voting_deadline_passed", "Время голосования закончилось")


def _ensure_not_owner(cat, user_id: int) -> str:
    guest_token = _participant_token(user_id)
    if int(cat["owner_id"]) == int(user_id):
        _fail("owner_cannot_vote", "Владелец категории не участвует в голосовании",
              status_code=403)
    return guest_token


def _check_candidate_deadline(cat, candidate) -> None:
    if candidate["starts_at"]:
        starts = _parse_moment(candidate["starts_at"], "starts_at")
        deadline = _parse_moment(cat["voting_deadline"], "deadline")
        if deadline >= starts:
            _fail("candidate_before_deadline",
                  "Это свидание начинается не позже дедлайна голосования",
                  date_id=int(candidate["id"]))


def _map_insert_error(exc: sqlite3.IntegrityError) -> None:
    raw = str(exc)
    if "booking_capacity_reached" in raw:
        _fail("capacity_reached", "На это свидание уже набрался максимум участников")
    if "single_choice_only" in raw:
        _fail("single_choice_only", "В этой категории можно выбрать только один вариант")
    if "voting_closed" in raw:
        _fail("voting_closed", "Голосование уже завершено")
    if "bookings.date_id, bookings.category_id, bookings.guest_token" in raw:
        _fail("already_voted", "Этот вариант уже выбран")
    raise exc


def cast_vote(conn: sqlite3.Connection, category_id: int, date_id: int,
              user_id: int, *, now: str | datetime | None = None) -> VoteResult:
    """Добавляет голос; в single-режиме атомарно переносит прежний выбор.

    Сначала проверяется вместимость нового варианта. Если он заполнен, старый
    single-голос остаётся на месте. Повтор по тому же варианту идемпотентен.
    """
    cat = _lock_category(conn, category_id)
    current = _now(now)
    _ensure_open(cat, current)
    guest_token = _ensure_not_owner(cat, user_id)
    candidate = _candidate(conn, category_id, date_id)
    _check_candidate_deadline(cat, candidate)

    existing = conn.execute(
        "SELECT id FROM bookings WHERE date_id=? AND category_id=? AND guest_token=?",
        (date_id, category_id, guest_token),
    ).fetchone()
    if existing:
        current_ids = _current_choices(conn, category_id, guest_token)
        date_votes = int(conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE date_id=? AND category_id=?",
            (date_id, category_id),
        ).fetchone()[0])
        return VoteResult(category_id, date_id, int(existing["id"]), False, (),
                          current_ids, date_votes, int(candidate["capacity"]))

    date_votes = int(conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE date_id=? AND category_id=?",
        (date_id, category_id),
    ).fetchone()[0])
    capacity = int(candidate["capacity"])
    if date_votes >= capacity:
        _fail("capacity_reached", "На это свидание уже набрался максимум участников",
              date_id=date_id, capacity=capacity)

    prior = _current_choices(conn, category_id, guest_token)
    removed = prior if cat["choice_mode"] == CHOICE_SINGLE else ()

    # SAVEPOINT сохраняет старый single-голос при любой ошибке новой вставки и
    # работает как внутри внешней транзакции роута, так и в autocommit.
    conn.execute("SAVEPOINT voting_cast_vote")
    try:
        if removed:
            conn.execute(
                "DELETE FROM bookings WHERE category_id=? AND guest_token=?",
                (category_id, guest_token),
            )
        cursor = conn.execute(
            "INSERT INTO bookings(date_id, category_id, guest_token, user_id, created_at) "
            "VALUES(?,?,?,?,?)",
            (date_id, category_id, guest_token, user_id, _iso(current)),
        )
        conn.execute("RELEASE SAVEPOINT voting_cast_vote")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK TO SAVEPOINT voting_cast_vote")
        conn.execute("RELEASE SAVEPOINT voting_cast_vote")
        _map_insert_error(exc)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT voting_cast_vote")
        conn.execute("RELEASE SAVEPOINT voting_cast_vote")
        raise

    current_ids = _current_choices(conn, category_id, guest_token)
    date_votes = int(conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE date_id=? AND category_id=?",
        (date_id, category_id),
    ).fetchone()[0])
    return VoteResult(category_id, date_id, int(cursor.lastrowid), True, removed,
                      current_ids, date_votes, capacity)


def remove_vote(conn: sqlite3.Connection, category_id: int, date_id: int,
                user_id: int, *, now: str | datetime | None = None) -> RemoveVoteResult:
    """Снимает свой голос пока категория открыта или ещё не настроена.

    ``unconfigured`` нужен для безопасной миграции: старые бюллетени остаются
    видимыми и снимаемыми, хотя новые голоса до явной настройки запрещены.
    """
    cat = _lock_category(conn, category_id)
    if cat["voting_status"] != STATUS_UNCONFIGURED:
        current = _now(now)
        _ensure_open(cat, current)
    guest_token = _ensure_not_owner(cat, user_id)
    cursor = conn.execute(
        "DELETE FROM bookings WHERE category_id=? AND date_id=? AND guest_token=?",
        (category_id, date_id, guest_token),
    )
    return RemoveVoteResult(
        category_id=category_id,
        date_id=date_id,
        removed=cursor.rowcount > 0,
        current_date_ids=_current_choices(conn, category_id, guest_token),
    )


def close_category(conn: sqlite3.Connection, category_id: int, *,
                   now: str | datetime | None = None) -> CategoryState:
    """После дедлайна замораживает голоса и вычисляет детерминированный исход.

    0 голосов → ``no_winner``; один лидер → ``resolved``; несколько лидеров →
    ``tie`` до ручного выбора владельца. Повторный вызов идемпотентен.
    """
    cat = _lock_category(conn, category_id)
    if cat["voting_status"] in CLOSED_STATUSES:
        return get_category_state(conn, category_id)
    if cat["voting_status"] == STATUS_UNCONFIGURED:
        _fail("voting_unconfigured", "Владелец ещё не настроил голосование")
    if cat["voting_status"] != STATUS_OPEN or cat["closed_at"] is not None:
        _fail("invalid_voting_state", "Некорректное состояние голосования",
              status_code=500, voting_status=cat["voting_status"])

    current = _now(now)
    deadline = _parse_moment(cat["voting_deadline"], "deadline")
    if current < deadline:
        _fail("deadline_not_reached", "Дедлайн голосования ещё не наступил",
              voting_deadline=cat["voting_deadline"])
    timestamp = _iso(current)

    # Сначала ставим физическую границу closed_at: триггеры тут же блокируют
    # INSERT/DELETE бюллетеней, затем считаем уже зафиксированный снимок.
    cursor = conn.execute(
        "UPDATE categories SET closed_at=? WHERE id=? AND voting_status='open' "
        "AND closed_at IS NULL", (timestamp, category_id)
    )
    if cursor.rowcount == 0:
        return get_category_state(conn, category_id)

    counts = _counts(conn, category_id)
    leaders = _leaders(counts)
    if not leaders:
        conn.execute(
            "UPDATE categories SET voting_status='no_winner', winner_date_id=NULL, "
            "resolved_at=? WHERE id=?", (timestamp, category_id)
        )
    elif len(leaders) == 1:
        conn.execute(
            "UPDATE categories SET voting_status='resolved', winner_date_id=?, "
            "resolved_at=? WHERE id=?", (leaders[0], timestamp, category_id)
        )
    else:
        conn.execute(
            "UPDATE categories SET voting_status='tie', winner_date_id=NULL, "
            "resolved_at=NULL WHERE id=?", (category_id,)
        )
    return get_category_state(conn, category_id)


def resolve_tie(conn: sqlite3.Connection, category_id: int, owner_id: int,
                winner_date_id: int, *,
                now: str | datetime | None = None) -> CategoryState:
    """Владелец вручную выбирает победителя только из фактических лидеров."""
    cat = _lock_category(conn, category_id, owner_id)
    if cat["voting_status"] != STATUS_TIE or cat["closed_at"] is None:
        _fail("tie_not_pending", "В этой категории нет ничьей для разрешения")
    counts = _counts(conn, category_id)
    leaders = _leaders(counts)
    if int(winner_date_id) not in leaders:
        _fail("winner_not_leader", "Победителя можно выбрать только среди лидеров",
              status_code=400, leader_date_ids=list(leaders))
    timestamp = _iso(_now(now))
    conn.execute(
        "UPDATE categories SET voting_status='resolved', winner_date_id=?, "
        "resolved_at=? WHERE id=? AND voting_status='tie'",
        (winner_date_id, timestamp, category_id),
    )
    return get_category_state(conn, category_id)


def withdraw_participation(conn: sqlite3.Connection, category_id: int,
                           user_id: int, *,
                           now: str | datetime | None = None) -> WithdrawalResult:
    """Отмечает отказ участника победившего свидания, не меняя его голос."""
    cat = _lock_category(conn, category_id)
    guest_token = _ensure_not_owner(cat, user_id)
    if cat["voting_status"] != STATUS_RESOLVED or cat["winner_date_id"] is None:
        _fail("winner_not_resolved", "Победитель категории ещё не определён")
    winner_id = int(cat["winner_date_id"])
    booking = conn.execute(
        "SELECT id, participation_withdrawn_at FROM bookings "
        "WHERE category_id=? AND date_id=? "
        "AND (user_id=? OR (user_id IS NULL AND guest_token=?)) LIMIT 1",
        (category_id, winner_id, user_id, guest_token),
    ).fetchone()
    if not booking:
        _fail("not_winner_participant", "Ты не участвуешь в победившем свидании",
              status_code=403)
    if booking["participation_withdrawn_at"]:
        return WithdrawalResult(category_id, winner_id, int(booking["id"]),
                                booking["participation_withdrawn_at"], True)

    timestamp = _iso(_now(now))
    conn.execute(
        "UPDATE bookings SET participation_withdrawn_at=? "
        "WHERE id=? AND participation_withdrawn_at IS NULL",
        (timestamp, booking["id"]),
    )
    return WithdrawalResult(category_id, winner_id, int(booking["id"]),
                            timestamp, False)


def set_date_capacity(conn: sqlite3.Connection, date_id: int, owner_id: int,
                      capacity: int) -> CapacityResult:
    """Меняет вместимость, не позволяя обрезать уже набранные категории."""
    try:
        value = int(capacity)
    except (TypeError, ValueError):
        _fail("invalid_capacity", "Вместимость должна быть целым числом от 1 до 100",
              status_code=400)
    if isinstance(capacity, bool) or not MIN_CAPACITY <= value <= MAX_CAPACITY:
        _fail("invalid_capacity", "Вместимость должна быть целым числом от 1 до 100",
              status_code=400, min=MIN_CAPACITY, max=MAX_CAPACITY)
    cursor = conn.execute(
        "UPDATE dates SET id=id WHERE id=? AND owner_id=?", (date_id, owner_id)
    )
    if cursor.rowcount == 0:
        _fail("date_not_found", "Свидание не найдено", status_code=404)
    try:
        conn.execute("UPDATE dates SET capacity=? WHERE id=?", (value, date_id))
    except sqlite3.IntegrityError as exc:
        if "capacity_below_existing_votes" in str(exc):
            max_row = conn.execute(
                "SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM bookings "
                "WHERE date_id=? GROUP BY category_id)", (date_id,)
            ).fetchone()
            _fail("capacity_below_existing_votes",
                  "Вместимость меньше уже набранного количества участников",
                  minimum=int(max_row[0] or 1))
        raise
    return CapacityResult(date_id, value)
