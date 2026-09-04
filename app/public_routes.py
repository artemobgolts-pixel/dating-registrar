"""Публичная часть: главная, /health и страницы категорий /c/<токен>.

Участники представляются по имени, голосуют за события, предлагают свои идеи
и задают вопросы. За одно событие могут проголосовать несколько человек в
пределах настроенной вместимости.
"""

import json
import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from xml.sax.saxutils import escape as xml_escape

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Request, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse, Response,
                               StreamingResponse)

import auth_routes
import appearance
import category_access
import db
import guests as legacy_guests
import images
import notify
import places
import social_events
import users
import voting
import voting_events
from config import (AUTHOR_PROJECTS, ABOUT_TEXT, BASE_URL, DOMAIN,
                    MSK, support_link)
from helpers import (_parse, clean_text, fmt_gcal, fmt_when, new_link_token,
                     normalize_period, now_iso, now_naive, parse_dt_local,
                     parse_links)
from notify import esc
from ratelimit import guest_throttle
from web import get_db, redir, templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Идентичность посетителя гостевой страницы
# ---------------------------------------------------------------------------
# Раньше гость представлялся только именем (cookie-токен). Теперь все действия
# (голос, вопрос, предложение) требуют входа в аккаунт: аноним только смотрит.
# Залогиненному выдаём стабильный guest_token "u<id>" и держим его имя в guests —
# так весь код, завязанный на guest_token, работает без правок, а владелец видит
# реальное имя из профиля.

def viewer(request, conn):
    """Текущий залогиненный посетитель (строка users) или None для анонима."""
    uid = request.session.get("user_id")
    if not uid:
        return None
    return conn.execute(
        "SELECT * FROM users WHERE id=? AND is_active=1", (uid,)).fetchone()


def acting_user(request, conn, csrf: str = ""):
    """Для POST-действий гостя: нужен вход. Нет валидной сессии → 401 с флагом
    need_login (фронт уводит на /login?next=…). Валидная сессия сама по себе
    недостаточна: каждое изменение требует CSRF-токен этой сессии."""
    u = viewer(request, conn)
    if not u:
        raise HTTPException(401, {"need_login": True,
                                  "msg": "Войди в аккаунт, чтобы продолжить ♥"})
    users.require_csrf(request, csrf)
    return u


def report_identity(request: Request, conn, csrf: str = "") -> str:
    """Неперсональный идентификатор отправителя жалобы.

    Жалоба — единственное публичное POST-действие, доступное без аккаунта.
    Для вошедшего пользователя сохраняем прежнюю стабильную ссылку ``u<ID>``
    и обычную CSRF-защиту. Анониму выдаём случайный токен в подписанной
    HttpOnly-сессии: он нужен только для дедупликации и per-browser лимита,
    не содержит IP, fingerprint или другого персонального идентификатора.
    IP участвует лишь в существующем краткоживущем in-memory rate limit.
    """
    user = viewer(request, conn)
    if user:
        users.require_csrf(request, csrf)
        return viewer_token(user)

    users.require_same_origin(request)
    key = "anonymous_reporter"
    token = str(request.session.get(key) or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", token):
        token = secrets.token_urlsafe(24)
        request.session[key] = token
    return f"a:{token}"


def viewer_token(user) -> str | None:
    """Стабильный guest_token залогиненного посетителя."""
    return f"u{user['id']}" if user else None


def guest_identity(user) -> tuple[str, str]:
    """Возвращает стабильный токен и имя профиля без записи в БД.

    GET-страницы используют этот чистый вариант, чтобы обычный просмотр не
    конкурировал с голосами и другими записями за единственный writer-lock
    SQLite. Строка ``guests`` синхронизируется только вместе с POST-действием.
    """
    token = viewer_token(user)
    name = ((user["display_name"] or "").strip()
            or (user["tg_username"] or "").strip()
            or f"Человек #{user['id']}")
    return token, name


def ensure_guest_name(conn, user) -> tuple[str, str]:
    """Заводит/обновляет строку guests для залогиненного посетителя (имя из
    профиля). Возвращает (guest_token, имя). Без commit — вызывающий коммитит
    вместе со своим действием."""
    token, name = guest_identity(user)
    conn.execute(
        "INSERT INTO guests(token, name, created_at) VALUES(?,?,?) "
        "ON CONFLICT(token) DO UPDATE SET name=excluded.name",
        (token, name, now_iso()))
    return token, name


def legacy_guest_token(request: Request, user) -> str | None:
    """Старый непредсказуемый cookie-токен, которым владеет этот браузер.

    Читающий GET может учитывать такой бюллетень как «мой» без переноса строк.
    Предсказуемые ``u<ID>`` никогда не считаем bearer-доказательством.
    """
    legacy = legacy_guests.get_guest(request)
    stable = viewer_token(user)
    if not legacy or legacy == stable or re.fullmatch(r"u\d+", legacy):
        return None
    return legacy


def legacy_vote_date_ids(conn, request: Request, user,
                         category_id: int) -> tuple[int, ...]:
    """Варианты старого cookie-бюллетеня до его атомарного POST-переноса."""
    legacy = legacy_guest_token(request, user)
    if not legacy:
        return ()
    return tuple(
        int(row["date_id"])
        for row in conn.execute(
            "SELECT date_id FROM bookings "
            "WHERE category_id=? AND guest_token=? AND user_id IS NULL",
            (category_id, legacy),
        ).fetchall()
    )


def _legacy_claim_allowed(cat, user) -> bool:
    """Чистая проверка тех же условий, при которых POST переносит бюллетень."""
    if not cat or cat["voting_status"] not in {
            voting.STATUS_UNCONFIGURED, voting.STATUS_OPEN}:
        return False
    if int(cat["owner_id"]) == int(user["id"]):
        return False
    if cat["voting_status"] == voting.STATUS_OPEN:
        try:
            deadline = datetime.fromisoformat(cat["voting_deadline"])
        except (TypeError, ValueError):
            return False
        if deadline <= datetime.now(MSK).replace(tzinfo=None, microsecond=0):
            return False
    return True


def view_booking_rows(rows, request: Request, user, cat) -> list[dict]:
    """Read-only представление бюллетеней с виртуальным legacy-переносом.

    Оно повторяет результат ``claim_legacy_votes`` для UI, не меняя SQLite:
    single с уже существующим account-голосом скрывает старые cookie-строки,
    а совпавшие варианты в multiple не считаются дважды.
    """
    stable = viewer_token(user)
    result = [
        {**dict(row), "is_me": bool(stable and row["guest_token"] == stable)}
        for row in rows
    ]
    if not user:
        return result
    legacy = legacy_guest_token(request, user)
    if not legacy or int(cat["owner_id"]) == int(user["id"]):
        return result

    def eligible(row) -> bool:
        return row["guest_token"] == legacy and row["user_id"] is None
    if not _legacy_claim_allowed(cat, user):
        return result

    stable_dates = {
        int(row["date_id"]) for row in result
        if row["guest_token"] == stable
    }
    if cat["choice_mode"] == voting.CHOICE_SINGLE and stable_dates:
        return [row for row in result if not eligible(row)]

    _, profile_name = guest_identity(user)
    effective = []
    for row in result:
        if not eligible(row):
            effective.append(row)
            continue
        date_id = int(row["date_id"])
        if date_id in stable_dates:
            continue
        row.update({
            "guest_token": stable,
            "user_id": user["id"],
            "avatar_path": user["avatar_path"],
            "name": profile_name,
            "is_me": True,
        })
        stable_dates.add(date_id)
        effective.append(row)
    return effective


def claim_legacy_votes(conn, request: Request, user, category_id: int) -> int:
    """Безопасно привязывает старые cookie-голоса к текущему аккаунту.

    До v13 гость определялся только секретной HttpOnly-cookie, а в bookings
    оставался ``user_id=NULL``. Владение той же cookie — единственное доступное
    доказательство авторства. Переносим такие бюллетени лишь пока голосование
    можно менять; завершённый результат никогда не пересчитываем.
    """
    legacy = legacy_guest_token(request, user)
    stable = viewer_token(user)
    if not legacy:
        return 0

    # Сериализуем перенос с голосами, настройкой и закрытием, затем перечитываем.
    if conn.execute("UPDATE categories SET id=id WHERE id=?", (category_id,)).rowcount == 0:
        return 0
    cat = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if not _legacy_claim_allowed(cat, user):
        return 0

    legacy_rows = conn.execute(
        "SELECT id, date_id FROM bookings WHERE category_id=? "
        "AND guest_token=? AND user_id IS NULL ORDER BY id",
        (category_id, legacy),
    ).fetchall()
    if not legacy_rows:
        return 0
    stable_rows = {
        int(row["date_id"]): int(row["id"])
        for row in conn.execute(
            "SELECT id, date_id FROM bookings WHERE category_id=? AND guest_token=?",
            (category_id, stable),
        )
    }

    changed = 0
    if cat["choice_mode"] == voting.CHOICE_SINGLE and stable_rows:
        # A newer account choice wins; importing an older cookie choice would
        # violate the one-option rule.
        ids = [int(row["id"]) for row in legacy_rows]
        conn.executemany("DELETE FROM bookings WHERE id=?", ((bid,) for bid in ids))
        changed = len(ids)
    else:
        for row in legacy_rows:
            if int(row["date_id"]) in stable_rows:
                conn.execute("DELETE FROM bookings WHERE id=?", (row["id"],))
            else:
                conn.execute(
                    "UPDATE bookings SET guest_token=?, user_id=? WHERE id=?",
                    (stable, user["id"], row["id"]),
                )
            changed += 1
    if changed and cat["voting_status"] == voting.STATUS_OPEN:
        voting_events.queue_deadline_reminder(conn, category_id, int(user["id"]))
    return changed


# ---------------------------------------------------------------------------
# Общие выборки (используются и админкой)
# ---------------------------------------------------------------------------

def cat_by_token(conn, token: str):
    return conn.execute("SELECT * FROM categories WHERE link_token=?", (token,)).fetchone()


def category_og_sources(
        conn, category_id: int, *, include_focus: bool = False,
        existing_only: bool = False,
) -> list[str] | list[tuple[str, str | None]]:
    """До восьми фото для авто-превью, разнообразно по событиям.

    Сначала берём обложку каждого события в порядке категории, затем второе
    фото каждого события и так далее. Благодаря round-robin одно событие с
    большой галереей не вытесняет остальные. По умолчанию сохраняем прежний
    удобный ``list[str]`` API; генераторы запрашивают пары с точкой фокуса.
    """
    return category_og_sources_batch(
        conn, [category_id], include_focus=include_focus,
        existing_only=existing_only,
    ).get(int(category_id), [])


def category_og_sources_batch(
        conn, category_ids, *, include_focus: bool = False,
        existing_only: bool = False,
) -> dict[int, list[str] | list[tuple[str, str | None]]]:
    """Batch-версия round-robin источников для списка категорий."""
    ids = list(dict.fromkeys(int(category_id) for category_id in category_ids))
    if not ids:
        return {}
    galleries: dict[int, list[list[tuple[str, str | None]]]] = {
        category_id: [] for category_id in ids
    }
    by_date: dict[tuple[int, int], list[tuple[str, str | None]]] = {}
    exists_cache: dict[str, bool] = {}
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT dc.category_id, d.id AS date_id, di.filename, di.focus "
            "FROM date_categories dc JOIN dates d ON d.id=dc.date_id "
            "JOIN date_images di ON di.date_id=d.id "
            f"WHERE dc.category_id IN ({placeholders}) "
            "AND d.archived_at IS NULL AND d.is_draft=0 "
            "ORDER BY dc.category_id ASC, dc.position ASC, d.id ASC, "
            "di.position ASC, di.id ASC",
            tuple(batch),
        ).fetchall()
        for row in rows:
            filename = str(row["filename"] or "")
            if existing_only:
                if filename not in exists_cache:
                    exists_cache[filename] = images.upload_image_exists(filename)
                if not exists_cache[filename]:
                    continue
            category_id = int(row["category_id"])
            date_id = int(row["date_id"])
            key = (category_id, date_id)
            if key not in by_date:
                by_date[key] = []
                galleries[category_id].append(by_date[key])
            by_date[key].append((filename, row["focus"]))

    result: dict[int, list] = {}
    for category_id, category_galleries in galleries.items():
        selected: list[tuple[str, str | None]] = []
        level = 0
        while len(selected) < 8:
            added = False
            for gallery in category_galleries:
                if level < len(gallery):
                    selected.append(gallery[level])
                    added = True
                    if len(selected) == 8:
                        break
            if not added:
                break
            level += 1
        result[category_id] = (
            selected if include_focus
            else [filename for filename, _focus in selected]
        )
    return result


_PIN_GRANTS_KEY = "category_pin_grants"
_PIN_VISITOR_KEY = "category_pin_visitor"


def _pin_grant_fingerprint(cat) -> str:
    value = (
        f"{int(cat['id'])}:{cat['link_token'] or ''}:"
        f"{cat['access_pin_hash'] or ''}"
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def category_access_granted(conn, request: Request | None, cat) -> bool:
    """Владелец и сессия, успешно открывшая текущую версию PIN, имеют доступ.

    В cookie не кладём ни PIN, ни его хеш: только короткий fingerprint. При
    смене PIN сохранённый grant автоматически перестаёт совпадать.
    """
    if not bool(cat["pin_enabled"]):
        return True
    if request is None:
        return False
    current = viewer(request, conn)
    if current and int(current["id"]) == int(cat["owner_id"]):
        return True
    session = getattr(request, "session", {})
    grants = session.get(_PIN_GRANTS_KEY, {})
    return bool(
        isinstance(grants, dict)
        and grants.get(str(int(cat["id"]))) == _pin_grant_fingerprint(cat)
    )


def _remember_category_access(request: Request, cat) -> None:
    raw = request.session.get(_PIN_GRANTS_KEY, {})
    grants = dict(raw) if isinstance(raw, dict) else {}
    category_id = str(int(cat["id"]))
    grants.pop(category_id, None)
    grants[category_id] = _pin_grant_fingerprint(cat)
    # Не раздуваем подписанную cookie после просмотра множества подборок.
    request.session[_PIN_GRANTS_KEY] = dict(list(grants.items())[-12:])


def _pin_visitor(request: Request) -> str:
    token = str(request.session.get(_PIN_VISITOR_KEY) or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", token):
        token = secrets.token_urlsafe(24)
        request.session[_PIN_VISITOR_KEY] = token
    return token


def _pin_page(request: Request, cat, token: str, *, error: str | None = None,
              status_code: int = 200):
    response = templates.TemplateResponse(
        request,
        "public/pin.html",
        {
            "cat": {"name": cat["name"]},
            "token": token,
            "category_skin": appearance.normalize_skin(cat["category_skin"]),
            "csrf": request.session.get("csrf", ""),
            "error": error,
            "attempts_remaining": None,
        },
        status_code=status_code,
    )
    response.headers["X-Robots-Tag"] = "noindex"
    return response


def require_category_access(conn, request: Request | None, cat) -> None:
    if not category_access_granted(conn, request, cat):
        raise HTTPException(403, "Подборка защищена PIN-кодом")


def active_cat_or_410(conn, token: str, request: Request | None = None):
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        raise HTTPException(410, "Ссылка больше не активна")
    if request is not None:
        require_category_access(conn, request, cat)
    return cat


def date_in_category(conn, category_id: int, date_id: int):
    """Опубликованное активное событие в категории (для выбора/вопросов/ics)."""
    return conn.execute(
        "SELECT d.* FROM dates d JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE d.id=? AND dc.category_id=? AND d.archived_at IS NULL AND d.is_draft=0",
        (date_id, category_id),
    ).fetchone()


def notify_owner(bg, conn, owner_id: int, text: str, *,
                 preference: str = "updates", action_url: str | None = None,
                 action_label: str | None = None) -> None:
    """Шлёт владельцу уведомление о действии с его событием (голос, вопрос,
    предложение...). chat_id резолвим СЕЙЧАС (conn ещё открыт), а сам сетевой
    вызов уводим в фон. Если бот владельцем не подключён — тихо ничего не шлём."""
    chat = notify.owner_chat_id(conn, owner_id, preference)
    if chat is not None:
        bg.add_task(
            notify.send_to, chat, text,
            reply_markup=notify.action_markup(action_label, action_url),
        )


def notify_user(bg, conn, user_id: int | None, text: str, *,
                preference: str = "updates", action_url: str | None = None,
                action_label: str | None = None) -> None:
    """Шлёт уведомление произвольному пользователю (автору вопроса/предложения,
    гостю при снятии голоса). Те же правила доставки, что и у владельца:
    бот подключён, активен, не легаси. None/нет бота — тихо пропускаем."""
    if user_id is None:
        return
    chat = notify.user_chat_id(conn, user_id, preference)
    if chat is not None:
        bg.add_task(
            notify.send_to, chat, text,
            reply_markup=notify.action_markup(action_label, action_url),
        )


def notify_admin(bg, conn, owner_id: int | None, text: str, *,
                 action_url: str | None = None,
                 action_label: str | None = None) -> None:
    """Глобальный поток администратора платформы (TG_CHAT_ID): он видит КАЖДОЕ
    действие всех пользователей. Дополняем готовую карточку строкой «Владелец»,
    чтобы было подписано, чьего кабинета касается событие. Сетевой вызов — в фон.
    Если TG_CHAT_ID не задан, notify.notify тихо ничего не делает."""
    oname = "—"
    if owner_id:
        owner = users.get_user(conn, owner_id)
        if owner:
            oname = owner["display_name"] or owner["tg_username"] or f"#{owner_id}"
    bg.add_task(
        notify.notify, text + f"\nВладелец: {esc(oname)}",
        reply_markup=notify.action_markup(action_label, action_url),
    )


def own_proposal_or_403(conn, cat, date_id: int, guest: str | None):
    """Предложение гостя, которое он может править: только его собственное."""
    d = conn.execute(
        "SELECT d.* FROM dates d JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE d.id=? AND dc.category_id=? AND d.archived_at IS NULL",
        (date_id, cat["id"]),
    ).fetchone()
    if not d:
        raise HTTPException(404, "Событие не найдено")
    if d["origin"] != "guest" or not guest or d["guest_token"] != guest:
        raise HTTPException(403, "Это не твоё предложение")
    return d


def _batch_media(conn, date_ids: list[int]) -> dict:
    """Разом грузит ссылки/фото/видео для набора событий (устраняет N+1 на
    странице категории/ленте). Возвращает {'links': {did:[...]}, 'images':..,
    'videos':..} — упорядоченные списки sqlite3.Row на каждый date_id."""
    out = {"links": {}, "images": {}, "videos": {}}
    if not date_ids:
        return out
    ph = ",".join("?" * len(date_ids))
    args = tuple(date_ids)
    for key, table in (("links", "date_links"), ("images", "date_images"),
                       ("videos", "date_videos")):
        for r in conn.execute(
                f"SELECT * FROM {table} WHERE date_id IN ({ph}) ORDER BY position, id",
                args).fetchall():
            out[key].setdefault(r["date_id"], []).append(r)
    return out


def date_payload(conn, row) -> dict:
    d = dict(row)
    d["links"] = conn.execute(
        "SELECT * FROM date_links WHERE date_id=? ORDER BY position, id", (row["id"],)
    ).fetchall()
    d["images"] = conn.execute(
        "SELECT * FROM date_images WHERE date_id=? ORDER BY position, id", (row["id"],)
    ).fetchall()
    d["videos"] = conn.execute(
        "SELECT * FROM date_videos WHERE date_id=? ORDER BY position, id", (row["id"],)
    ).fetchall()
    return d


def date_payload_from(row, media: dict) -> dict:
    """Как date_payload, но берёт ссылки/фото/видео из заранее собранного батча
    (_batch_media) — без запросов на каждое событие."""
    d = dict(row)
    d["links"] = media["links"].get(row["id"], [])
    d["images"] = media["images"].get(row["id"], [])
    d["videos"] = media["videos"].get(row["id"], [])
    return d


def first_existing_og_image(rows):
    """Первое реально доступное фото по сохранённому position.

    База может пережить ручное удаление/неполный перенос upload-файла. Один
    пропавший первый кадр не должен скрывать остальные живые фото из link preview.
    """
    for row in rows or ():
        if images.upload_image_exists(row["filename"]):
            return row
    return None


def insert_date(conn, *, name, place, starts, ends, comment, origin, guest_token,
                owner_id, draft=0, pay_split=0, place_url=None, proposed_by=None,
                is_public=0, capacity=1, source_date_id=None) -> int:
    # Каждое событие получает стабильную секретную ссылку /d/<share_token>
    # сразу при создании — через неё им можно поделиться, чтобы другой
    # пользователь добавил копию себе. Генерим здесь, чтобы ВСЕ пути создания
    # (админ, клон, импорт, гостевое предложение) получили токен без правок.
    share_token = new_link_token()
    cur = conn.execute(
        "INSERT INTO dates(owner_id, name, place, place_url, starts_at, ends_at, comment, "
        "origin, guest_token, proposed_by, is_draft, pay_split, share_token, is_public, "
        "capacity, source_date_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (owner_id, name, place, place_url, starts, ends, comment, origin, guest_token,
         proposed_by, draft, pay_split, share_token, 1 if is_public else 0,
         capacity, source_date_id, now_iso()),
    )
    return cur.lastrowid


def personal_date_quota_used(conn, owner_id: int) -> int:
    """Активные события, созданные владельцем лично.

    Гостевые предложения и независимые пользовательские копии не расходуют
    квоту — у них есть явный provenance вместо ненадёжного сравнения названий.
    """
    return int(conn.execute(
        "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NULL "
        "AND origin='admin' AND source_date_id IS NULL",
        (owner_id,),
    ).fetchone()[0])


def parse_capacity(value) -> int:
    """Пустое значение означает обычное событие на одного участника."""
    if value in (None, ""):
        return 1
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "Количество участников должно быть числом от 1 до 100")
    if not voting.MIN_CAPACITY <= result <= voting.MAX_CAPACITY:
        raise HTTPException(400, "Количество участников должно быть от 1 до 100")
    return result


def parse_pay_split(value) -> int:
    """Единые варианты оплаты для основного и гостевого редакторов."""
    if value in (None, ""):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "Неизвестный вариант оплаты")
    if result not in (0, 1, 2, 3):
        raise HTTPException(400, "Неизвестный вариант оплаты")
    return result


def ensure_category_editable(conn, cat) -> None:
    """Состав и карточки опроса фиксируются после дедлайна/результата."""
    state = _category_voting_state(conn, cat["id"])
    if state.status in voting.CLOSED_STATUSES:
        raise HTTPException(409, "Голосование завершено — варианты уже зафиксированы")


def proposal_changes_open(state: voting.CategoryState, *, now: datetime | None = None) -> bool:
    """Read-only проверка для публичного UI предложения событий.

    Фоновое закрытие может отстать от дедлайна на несколько секунд. Поэтому
    кнопки создания/редактирования не должны оставаться доступными лишь из-за
    сохранённого ``status='open'`` и приводить пользователя к позднему 409.
    """
    if state.status == voting.STATUS_UNCONFIGURED:
        return True
    if state.status != voting.STATUS_OPEN or not state.voting_deadline:
        return False
    try:
        deadline = datetime.fromisoformat(state.voting_deadline)
    except (TypeError, ValueError):
        return False
    current = now or now_naive()
    if current.tzinfo is not None:
        current = current.astimezone(MSK).replace(tzinfo=None)
    if deadline.tzinfo is not None:
        deadline = deadline.astimezone(MSK).replace(tzinfo=None)
    return current < deadline


def validate_candidate_start(cat, starts_at: str | None) -> None:
    if cat["voting_status"] != voting.STATUS_OPEN or not starts_at:
        return
    try:
        deadline = datetime.fromisoformat(cat["voting_deadline"])
        starts = datetime.fromisoformat(starts_at)
    except (TypeError, ValueError):
        raise HTTPException(400, "Некорректная дата голосования или события")
    if starts <= deadline:
        raise HTTPException(400, "Событие должно начинаться после дедлайна голосования")


def next_cat_pos(conn, category_id: int) -> int:
    """Позиция для нового события в категории: в конец списка."""
    return conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM date_categories WHERE category_id=?",
        (category_id,)).fetchone()[0]


def save_links(conn, date_id: int, links: list[str]) -> None:
    conn.execute("DELETE FROM date_links WHERE date_id=?", (date_id,))
    for i, u in enumerate(links):
        conn.execute("INSERT INTO date_links(date_id, url, position) VALUES(?,?,?)",
                     (date_id, u, i))


def copy_date_media_and_links(conn, src_id: int, new_id: int) -> None:
    """Переносит на новое событие ссылки и физические копии фото/видео исходного.

    Фото/видео — отдельные файлы с новыми именами (images.copy_file); фокус кадра
    сохраняем. Битые/пропавшие файлы просто пропускаем. При ошибке удаляем уже
    скопированные файлы и пробрасываем исключение — чтобы не оставлять сирот на
    диске. Категории/голоса/вопросы НЕ трогаем: привязки при необходимости
    создаёт вызывающий. Коммит — на вызывающем.

    Используется и клонированием события админом, и «добавить себе» по /d/<токен>.
    """
    for r in conn.execute(
            "SELECT url, position FROM date_links WHERE date_id=? ORDER BY position, id",
            (src_id,)).fetchall():
        conn.execute("INSERT INTO date_links(date_id, url, position) VALUES(?,?,?)",
                     (new_id, r["url"], r["position"]))

    copied: list[str] = []
    try:
        for r in conn.execute(
                "SELECT filename, position, focus FROM date_images WHERE date_id=? "
                "ORDER BY position, id", (src_id,)).fetchall():
            fn = images.copy_file(r["filename"])
            if fn:
                copied.append(fn)
                conn.execute(
                    "INSERT INTO date_images(date_id, filename, position, focus) VALUES(?,?,?,?)",
                    (new_id, fn, r["position"], r["focus"]))
        for r in conn.execute(
                "SELECT filename, position FROM date_videos WHERE date_id=? "
                "ORDER BY position, id", (src_id,)).fetchall():
            fn = images.copy_file(r["filename"])
            if fn:
                copied.append(fn)
                conn.execute(
                    "INSERT INTO date_videos(date_id, filename, position) VALUES(?,?,?)",
                    (new_id, fn, r["position"]))
    except Exception:
        for fn in copied:               # не оставляем осиротевшие копии на диске
            images.delete_file(fn)
        raise


def _next_img_pos(conn, date_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(position), -1) FROM date_images WHERE date_id=?", (date_id,)
    ).fetchone()[0] + 1


def add_photos(conn, date_id: int, files: list[UploadFile], existing: int,
               focuses: list[str] | None = None) -> list[str]:
    files = [f for f in files if f and f.filename]
    if not files:
        return []
    if existing + len(files) > images.MAX_IMAGES:
        raise HTTPException(400, f"Максимум {images.MAX_IMAGES} фото у одного события")
    try:
        saved = images.save_batch(files)
    except ValueError as e:
        raise HTTPException(400, str(e))
    pos = _next_img_pos(conn, date_id)
    for i, fn in enumerate(saved):
        focus = _clean_focus(focuses[i]) if focuses and i < len(focuses) else None
        if focus:
            conn.execute(
                "INSERT INTO date_images(date_id, filename, position, focus) "
                "VALUES(?,?,?,?)", (date_id, fn, pos, focus))
        else:
            conn.execute(
                "INSERT INTO date_images(date_id, filename, position) VALUES(?,?,?)",
                (date_id, fn, pos))
        pos += 1
    return saved


def ordered_media_refs(raw: str, remaining_ids, new_count: int) -> list[tuple[str, int]]:
    """Разбирает совместимый порядок saved/new медиа одного события.

    Старый клиент отправлял ``1,2`` (только id сохранённых фото), новый —
    ``s1,n0,s2``. Любой чужой id и индекс вне текущей пачки игнорируются, затем
    все пропущенные собственные записи и новые файлы безопасно дописываются.
    """
    saved_ids = [int(value) for value in remaining_ids]
    allowed_saved = set(saved_ids)
    refs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    def ascii_int(value: str) -> int | None:
        # str.isdigit() принимает ² и другие Unicode-цифры, а очень длинная
        # строка падает на лимите int(). Protocol допускает только короткий
        # ASCII decimal id/index.
        if not 1 <= len(value) <= 20 or not value.isascii() or not value.isdigit():
            return None
        try:
            return int(value)
        except ValueError:
            return None

    for part in (raw or "").split(","):
        token = part.strip().lower()
        ref: tuple[str, int] | None = None
        legacy_id = ascii_int(token)
        saved_id = ascii_int(token[1:]) if token.startswith("s") else None
        new_index = ascii_int(token[1:]) if token.startswith("n") else None
        if legacy_id is not None:                   # legacy keep_order
            ref = ("saved", legacy_id)
        elif saved_id is not None:
            ref = ("saved", saved_id)
        elif new_index is not None:
            ref = ("new", new_index)
        if ref is None or ref in seen:
            continue
        if ref[0] == "saved" and ref[1] not in allowed_saved:
            continue
        if ref[0] == "new" and not 0 <= ref[1] < new_count:
            continue
        seen.add(ref)
        refs.append(ref)
    for media_id in saved_ids:
        ref = ("saved", media_id)
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
    for idx in range(new_count):
        ref = ("new", idx)
        if ref not in seen:
            refs.append(ref)
    return refs


_FOCUS_RE = re.compile(r"\s*(\d{1,3})%\s+(\d{1,3})%\s*")


def _clean_focus(raw: str | None) -> str | None:
    """Валидирует зону кадра «X% Y%» (0..100). Иначе — None (центр по умолчанию)."""
    m = _FOCUS_RE.fullmatch(raw or "")
    if not m or int(m.group(1)) > 100 or int(m.group(2)) > 100:
        return None
    return f"{int(m.group(1))}% {int(m.group(2))}%"


PUBLIC_ROSTER_LIMIT = 12


def _visible_participants(people: list[dict]) -> tuple[list[dict], int]:
    """Первые участники ростера, но текущий пользователь всегда видит себя."""
    visible = list(people[:PUBLIC_ROSTER_LIMIT])
    mine_index = next(
        (index for index, person in enumerate(people) if person.get("is_me")),
        None,
    )
    if mine_index is not None and mine_index >= PUBLIC_ROSTER_LIMIT:
        visible[-1:] = [people[mine_index]]
    return visible, max(0, len(people) - len(visible))


def _booking_rows(conn, category_id: int):
    return conn.execute(
        "SELECT b.id, b.date_id, b.guest_token, b.user_id, "
        "b.participation_withdrawn_at, u.avatar_path, "
        "COALESCE(NULLIF(u.display_name,''), NULLIF(u.tg_username,''), "
        "         NULLIF(g.name,''), 'Участник') AS name "
        "FROM bookings b LEFT JOIN guests g ON g.token=b.guest_token "
        "LEFT JOIN users u ON u.id=b.user_id AND u.is_active=1 "
        "WHERE b.category_id=? ORDER BY b.created_at",
        (category_id,),
    ).fetchall()


def _vote_card_updates(conn, category_id: int, date_ids, guest: str) -> list[dict]:
    """Компактный снимок изменившихся карточек для обновления без reload.

    Возвращаем только затронутые варианты и не более ``PUBLIC_ROSTER_LIMIT``
    участников на каждый: ответ остаётся маленьким даже у большого события.
    """
    ids = sorted({int(date_id) for date_id in date_ids})
    if not ids:
        return []
    category = conn.execute(
        "SELECT private_profiles, category_skin FROM categories WHERE id=?",
        (category_id,),
    ).fetchone()
    profiles_clickable = bool(category and not category["private_profiles"])
    category_skin = appearance.normalize_skin(
        category["category_skin"] if category else None,
    )
    ph = ",".join("?" * len(ids))
    capacities = {
        int(row["id"]): int(row["capacity"] or 1)
        for row in conn.execute(
            f"SELECT id, capacity FROM dates WHERE id IN ({ph})", tuple(ids)
        ).fetchall()
    }
    participants: dict[int, list[dict]] = {date_id: [] for date_id in ids}
    totals: dict[int, int] = {date_id: 0 for date_id in ids}
    mine: dict[int, bool] = {date_id: False for date_id in ids}
    for row in conn.execute(
            "SELECT b.date_id, b.guest_token, b.user_id, "
            "b.participation_withdrawn_at, u.avatar_path, "
            "COALESCE(NULLIF(u.display_name,''), NULLIF(u.tg_username,''), "
            "         NULLIF(g.name,''), 'Участник') AS name "
            "FROM bookings b LEFT JOIN guests g ON g.token=b.guest_token "
            "LEFT JOIN users u ON u.id=b.user_id AND u.is_active=1 "
            f"WHERE b.category_id=? AND b.date_id IN ({ph}) "
            "ORDER BY b.date_id, b.created_at, b.id",
            (category_id, *ids)).fetchall():
        date_id = int(row["date_id"])
        totals[date_id] += 1
        if row["guest_token"] == guest:
            mine[date_id] = True
        participant = {
            "name": row["name"],
            "user_id": row["user_id"],
            "has_avatar": bool(row["user_id"] and row["avatar_path"]),
            "is_me": row["guest_token"] == guest,
            "withdrawn": bool(row["participation_withdrawn_at"]),
        }
        participant["profile_url"] = (
            f"/u/{int(row['user_id'])}?skin={category_skin}"
            if row["user_id"] and profiles_clickable else None
        )
        participants[date_id].append(participant)

    updates = []
    for date_id in ids:
        capacity = capacities.get(date_id, 1)
        people, hidden_count = _visible_participants(participants[date_id])
        total = totals[date_id]
        updates.append({
            "date_id": date_id,
            "mine": mine[date_id],
            "vote_count": total,
            "capacity": capacity,
            "is_full": total >= capacity,
            "participants": people,
            "hidden_count": hidden_count,
            "profiles_clickable": profiles_clickable,
        })
    return updates


def _voting_http_error(exc: voting.VotingError) -> HTTPException:
    """Единый дружелюбный ответ доменных ошибок для гостевого fetch-UI."""
    return HTTPException(
        exc.status_code,
        {"msg": exc.message, "code": exc.code, "details": exc.details},
    )


def _category_voting_state(conn, category_id: int):
    """Лениво закрывает наступивший дедлайн и возвращает свежий снимок.

    Фоновый цикл остаётся основной сеткой безопасности, но такой вызов делает
    результат видимым сразу же при первом открытии страницы после дедлайна.
    """
    voting_events.close_due_once(conn, category_id=category_id)
    return voting.get_category_state(conn, category_id)


# ---------------------------------------------------------------------------
# Служебные страницы
# ---------------------------------------------------------------------------

@router.get("/health")
def health():
    # Не просто «процесс жив»: читаем базу и пробуем запись на диск —
    # ловим readonly-/data и залипшие блокировки.
    conn = db.connect()
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    finally:
        conn.close()
    (db.DATA_DIR / ".health").touch()
    return {"ok": True}


@router.get("/")
def home(request: Request):
    description = (
        "Создавай подборки событий с фотографиями, датами, местами и деталями. "
        "Делись ссылкой и выбирай следующую встречу вместе с друзьями."
    )
    structured_data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": f"{BASE_URL}/#website",
                "url": f"{BASE_URL}/",
                "name": "date4you",
                "description": description,
                "inLanguage": "ru-RU",
            },
            {
                "@type": "WebApplication",
                "@id": f"{BASE_URL}/#application",
                "url": f"{BASE_URL}/",
                "name": "date4you",
                "description": description,
                "applicationCategory": "LifestyleApplication",
                "operatingSystem": "Web",
                "inLanguage": "ru-RU",
            },
        ],
    }
    return templates.TemplateResponse(
        request,
        "public/home.html",
        {
            "description": description,
            "structured_data": structured_data,
            "support": support_link(),
        },
    )


@router.head("/", include_in_schema=False)
def home_head():
    # UptimeRobot и прочие мониторинги по умолчанию проверяют доступность
    # запросом HEAD. FastAPI не добавляет HEAD к GET-маршрутам сам, поэтому без
    # этого обработчика HEAD / отвечает 405 и мониторинг считает сайт упавшим.
    # Отдаём пустой 200 напрямую (а не редирект на /login, как делает GET): иначе
    # монитор пойдёт за 307 на /login — тоже GET-only — и снова получит 405.
    return Response(status_code=200)


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse("static/favicon-standard.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/robots.txt")
def robots():
    # Закрытые страницы несут meta noindex/X-Robots-Tag. Не блокируем их здесь:
    # иначе поисковый робот не сможет прочитать сам запрет индексации.
    return PlainTextResponse(
        f"User-agent: *\nAllow: /\n\nSitemap: {BASE_URL}/sitemap.xml\n"
    )


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    base = xml_escape(BASE_URL.rstrip("/"))
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{base}/</loc></url>\n"
        f"  <url><loc>{base}/about</loc></url>\n"
        "</urlset>\n"
    )
    return Response(content=content, media_type="application/xml")


_INFO_RETURN_RE = re.compile(
    r"^/(?:login|admin/?|c/[A-Za-z0-9_-]+|d/[A-Za-z0-9_-]+|"
    r"u/\d+(?:/reviews)?)$",
)


def _safe_info_return(request: Request) -> str:
    """Контекстный и безопасный «назад» для справочных страниц."""
    raw = request.query_params.get("return_to", "")
    if raw:
        from urllib.parse import urlsplit
        parsed = urlsplit(raw)
        if (not parsed.scheme and not parsed.netloc and not parsed.query
                and not parsed.fragment
                and (parsed.path == "/" or _INFO_RETURN_RE.fullmatch(parsed.path))):
            return parsed.path
    return "/admin/" if getattr(request.state, "user", None) else "/"


@router.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(
        request, "public/legal.html",
        {"doc": "terms", "support": support_link(), "domain": DOMAIN,
         "back_url": _safe_info_return(request)})


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(
        request, "public/legal.html",
        {"doc": "privacy", "support": support_link(), "domain": DOMAIN,
         "back_url": _safe_info_return(request)})


@router.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(
        request, "public/about.html",
        {"support": support_link(), "projects": AUTHOR_PROJECTS,
         "about_text": ABOUT_TEXT, "back_url": _safe_info_return(request)})


# ---------------------------------------------------------------------------
# Страница категории
# ---------------------------------------------------------------------------

@router.get("/c/{token}", response_class=HTMLResponse)
def public_category(token: str, request: Request, conn=Depends(get_db)):
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        return templates.TemplateResponse(request, "public/gone.html", status_code=404)
    if not category_access_granted(conn, request, cat):
        return _pin_page(request, cat, token)

    # Архивация и закрытие дедлайнов выполняются фоновыми циклами. Публичный GET
    # остаётся read-only и не захватывает writer-lock SQLite.
    vote_state = voting.get_category_state(conn, cat["id"])
    proposals_editable = proposal_changes_open(vote_state)
    voting_accepts_votes = (
        vote_state.status == voting.STATUS_OPEN
        and vote_state.closed_at is None
        and proposals_editable
    )

    # Залогиненный посетитель опознаётся стабильным токеном "u<id>"; аноним
    # может смотреть и отправить неперсональную жалобу, остальные действия в
    # JS ведут на /login. Имя вошедшего берём из профиля.
    me = viewer(request, conn)
    if me:
        guest, guest_name = guest_identity(me)
    else:
        guest = None
        guest_name = None
    owner = users.get_user(conn, cat["owner_id"])
    bookings: dict[int, list] = {}
    visible_bookings = view_booking_rows(
        _booking_rows(conn, cat["id"]), request, me, cat)
    for b in visible_bookings:
        bookings.setdefault(b["date_id"], []).append(b)

    # Разом грузим все события категории (активные + мои-черновики и архив),
    # затем их медиа и мои вопросы одним батчем — иначе был N+1 (3+ запроса на
    # каждое событие), из-за чего «первый заход» в большую категорию тормозил.
    rows = conn.execute(
        "SELECT d.* FROM dates d JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE dc.category_id=? AND d.archived_at IS NULL "
        "AND (d.is_draft=0 OR (d.origin='guest' AND d.guest_token=?)) "
        "ORDER BY dc.position ASC, (d.starts_at IS NULL) ASC, d.starts_at ASC, d.created_at ASC",
        (cat["id"], guest),
    ).fetchall()
    past_rows = conn.execute(
        "SELECT d.* FROM dates d JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE dc.category_id=? AND d.archived_at IS NOT NULL AND d.is_draft=0 "
        "ORDER BY COALESCE(d.ends_at, d.starts_at, d.created_at) DESC LIMIT 30",
        (cat["id"],),
    ).fetchall()
    all_ids = [r["id"] for r in rows] + [r["id"] for r in past_rows]
    media = _batch_media(conn, all_ids)
    # мои вопросы по всем событиям сразу (guest фиксирован в рамках запроса)
    my_q: dict[int, list] = {}
    if guest and all_ids:
        ph = ",".join("?" * len(all_ids))
        for q in conn.execute(
                f"SELECT date_id, text, answer FROM questions "
                f"WHERE date_id IN ({ph}) AND guest_token=? ORDER BY created_at",
                (*all_ids, guest)).fetchall():
            my_q.setdefault(q["date_id"], []).append(q)

    def enrich(row, past: bool = False) -> dict:
        d = date_payload_from(row, media)
        d["pending"] = bool(d["is_draft"])
        d["past"] = past
        entries = bookings.get(d["id"], [])
        d["booked_by_me"] = any(e["is_me"] for e in entries)
        others = [e["name"] for e in entries if not e["is_me"]]
        d["booked_others_list"] = others         # инициалы-аватарки в карточке
        d["booked_others"] = ", ".join(others)   # для обновления DOM без reload
        parts = (["ты"] if d["booked_by_me"] else []) + others
        d["booked_label"] = ", ".join(parts)
        participant_rows = [dict(e) for e in entries]
        d["participants"], d["participant_hidden_count"] = \
            _visible_participants(participant_rows)
        for participant in d["participants"]:
            participant["profile_url"] = (
                f"/u/{int(participant['user_id'])}?skin={appearance.normalize_skin(cat['category_skin'])}"
                if participant.get("user_id") and not cat["private_profiles"]
                else None
            )
        d["vote_count"] = len(entries)
        d["capacity"] = int(d.get("capacity") or 1)
        d["is_full"] = d["vote_count"] >= d["capacity"]
        d["is_winner"] = (vote_state.status == voting.STATUS_RESOLVED
                          and vote_state.winner_date_id == d["id"])
        d["is_loser"] = (vote_state.status == voting.STATUS_RESOLVED
                         and vote_state.winner_date_id != d["id"])
        d["is_tie_leader"] = (vote_state.status == voting.STATUS_TIE
                              and d["id"] in vote_state.leader_date_ids)
        mine_entry = next((e for e in entries if e["is_me"]), None)
        d["participation_withdrawn"] = bool(
            mine_entry and mine_entry["participation_withdrawn_at"])
        d["my_questions"] = my_q.get(d["id"], [])
        d["editable"] = (proposals_editable and not past and d["origin"] == "guest"
                         and d["guest_token"] == guest)
        if d["editable"]:
            d["meta_json"] = json.dumps({
                "id": d["id"], "name": d["name"], "place": d["place"] or "",
                "starts_at": d["starts_at"] or "", "ends_at": d["ends_at"] or "",
                "comment": d["comment"] or "",
                "links": "\n".join(l["url"] for l in d["links"]),
                "pay": d["pay_split"],
                "capacity": d["capacity"],
                "photos": [{"id": p["id"], "filename": p["filename"]} for p in d["images"]],
                "videos": [{"id": v["id"], "filename": v["filename"]} for v in d["videos"]],
            }, ensure_ascii=False).replace("</", "<\\/")
        if not past and not d["pending"] and d["starts_at"]:
            d["gcal"] = fmt_gcal(d["name"], d["starts_at"], d["ends_at"],
                                 d["place"], d["comment"],
                                 [l["url"] for l in d["links"]])
        return d

    dates = [enrich(r) for r in rows]
    past = [enrich(r, past=True) for r in past_rows]

    # Автор смотрит свою же страницу: не дублируем приветствие и не показываем
    # ему кнопки голосования. Сравниваем только стабильный user_id: тёзка автора
    # остаётся обычным участником.
    owner_name = (owner["display_name"] or owner["tg_username"] or "") if owner else ""
    viewer_is_owner = bool(me and owner and me["id"] == owner["id"])
    category_skin = appearance.normalize_skin(cat["category_skin"])
    og_files = category_og_sources(
        conn, int(cat["id"]), include_focus=True, existing_only=True,
    )
    custom_og_image = cat["og_image"] \
        if images.upload_image_exists(cat["og_image"]) else None
    preview_revision = images.og_preview_revision(
        og_files,
        category_skin,
        custom_image=custom_og_image,
        custom_focus=cat["og_focus"],
        use_default=bool(cat["use_default_preview"]),
    )

    resp = templates.TemplateResponse(request, "public/category.html", {
        "cat": cat,
        "category_skin": category_skin,
        "regular": dates,
        "past": past,
        "guest": guest, "guest_name": guest_name,
        "me": me, "owner": owner,
        "owner_name": owner_name,
        "viewer_is_owner": viewer_is_owner,
        "viewer_has_vote": any(bool(d["booked_by_me"]) for d in dates),
        "vote_state": vote_state,
        "voting_accepts_votes": voting_accepts_votes,
        "proposals_editable": proposals_editable,
        "max_photos": images.MAX_IMAGES,
        "max_videos": images.MAX_VIDEOS,
        # бот для вход-модалки (Telegram Login Widget). Анониму — кнопка «Войти»
        # открывает окно с этим виджетом прямо на гостевой.
        "bot": auth_routes.BOT_USERNAME,
        "oauth": auth_routes._oauth_buttons(),
        "widget_state": (auth_routes.issue_widget_state(request)
                         if auth_routes.BOT_USERNAME and not me else None),
        "token": token,
        # URL-ревизия нужна отдельно от дискового cache key:
        # Telegram и другие краулеры запоминают сам адрес og:image.
        "preview_revision": preview_revision,
        "csrf": request.session.get("csrf", ""),
    })
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@router.post("/c/{token}/unlock", response_class=HTMLResponse)
def public_category_unlock(token: str, request: Request,
                           pin: Annotated[str, Form()],
                           conn=Depends(get_db)):
    """Открывает защищённую подборку в этой подписанной browser-сессии."""
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        return templates.TemplateResponse(request, "public/gone.html", status_code=404)
    users.require_same_origin(request)
    if not cat["pin_enabled"]:
        return RedirectResponse(f"/c/{token}", status_code=303)
    if category_access_granted(conn, request, cat):
        return RedirectResponse(f"/c/{token}", status_code=303)
    guest_throttle("pin", _pin_visitor(request), request)
    if not category_access.verify_pin(pin, cat["access_pin_hash"] or ""):
        return _pin_page(
            request, cat, token,
            error="Неверный PIN-код. Проверь четыре цифры и попробуй снова.",
            status_code=403,
        )
    _remember_category_access(request, cat)
    return RedirectResponse(f"/c/{token}", status_code=303)


@router.get("/c/{token}/owner-avatar")
def public_owner_avatar(token: str, request: Request = None,
                        w: int | None = None, conn=Depends(get_db)):
    """Аватар владельца категории — для шапки гостевой страницы. Без логина:
    гость (в т.ч. анонимный) должен видеть фото автора. Отдаём только по активной
    ссылке и только аватар владельца этой категории (чужой файл не утечёт)."""
    cat = active_cat_or_410(conn, token, request)
    row = conn.execute(
        "SELECT u.avatar_path FROM categories c "
        "JOIN users u ON u.id=c.owner_id AND u.is_active=1 "
        "WHERE c.id=?",
        (cat["id"],)).fetchone()
    fn = row["avatar_path"] if row else None
    if not fn or not images.SAFE_FILENAME.match(fn):
        raise HTTPException(404)
    try:
        path = images.responsive_image(fn, w)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/c/{token}/participant-avatar/{user_id}")
def public_participant_avatar(token: str, user_id: int, w: int | None = None,
                              conn=Depends(get_db), request: Request = None):
    """Аватар участника виден только внутри активной гостевой категории.

    Идентификаторы/Telegram-контакты в HTML не выводятся; маршрут проверяет,
    что пользователь действительно голосовал именно в этой категории.
    """
    cat = active_cat_or_410(conn, token, request)
    row = conn.execute(
        "SELECT u.avatar_path FROM categories c "
        "JOIN bookings b ON b.category_id=c.id "
        "JOIN users u ON u.id=b.user_id AND u.is_active=1 "
        "WHERE c.id=? AND u.id=? "
        "LIMIT 1",
        (cat["id"], user_id),
    ).fetchone()
    fn = row["avatar_path"] if row else None
    if not fn or not images.SAFE_FILENAME.match(fn):
        raise HTTPException(404)
    try:
        path = images.responsive_image(fn, w)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/c/{token}/og-image")
def public_og_image(token: str, skin: str | None = None,
                    v: str | None = None, conn=Depends(get_db),
                    request: Request = None):
    """Картинка превью ссылки; для PIN-подборки тоже требуется browser-grant.

    Это намеренно отключает богатое превью у защищённой ссылки: иначе картинка
    раскрывала бы содержимое мессенджеру до ввода PIN. Своя og_image категории
    в приоритете; иначе — коллаж из фото её событий
    (сетка 2×2 или 4×2). Пользовательские фото отдаются без накладываемых лого;
    если фото нет, endpoint отдаёт единый приложенный default preview."""
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        raise HTTPException(404)
    require_category_access(conn, request, cat)
    preview_skin = appearance.normalize_skin(skin, default=cat["category_skin"])
    if cat["use_default_preview"]:
        return FileResponse(
            images.og_default_path(preview_skin),
            media_type="image/png",
            headers={"Cache-Control": "public, no-cache"},
        )
    fn = cat["og_image"] if images.upload_image_exists(cat["og_image"]) else None
    if fn:
        if not images.SAFE_FILENAME.match(fn):
            raise HTTPException(404)
        cropped = images.build_og_crop(fn, cat["og_focus"], preview_skin)
        if cropped:
            return FileResponse(cropped, media_type="image/webp",
                                headers={"Cache-Control": "public, no-cache"})
    # своей картинки нет — собираем коллаж из фото активных событий категории
    files = category_og_sources(
        conn, int(cat["id"]), include_focus=True, existing_only=True,
    )
    collage = images.build_og_collage(
        files, preview_skin)
    if not collage:
        return FileResponse(
            images.og_default_path(preview_skin),
            media_type="image/png",
            headers={"Cache-Control": "public, no-cache"},
        )
    return FileResponse(collage, media_type="image/webp",
                        headers={"Cache-Control": "public, no-cache"})


@router.get("/c/{token}/image/{filename}")
def public_image(token: str, filename: str, request: Request,
                 w: int | None = None, conn=Depends(get_db)):
    """Фото отдаются только по активной ссылке категории. Архивные события
    показываются с лентой «Архив», поэтому их фото доступны;
    черновики — только их автору."""
    if not images.SAFE_FILENAME.match(filename):
        raise HTTPException(404)
    active_cat_or_410(conn, token, request)
    uid = request.session.get("user_id") or 0
    ok = conn.execute(
        "SELECT 1 FROM date_images di "
        "JOIN dates d ON d.id=di.date_id "
        "JOIN date_categories dc ON dc.date_id=d.id "
        "JOIN categories c ON c.id=dc.category_id "
        "WHERE c.link_token=? AND c.link_enabled=1 AND di.filename=? "
        "AND (d.is_draft=0 OR (d.guest_token=('u' || CAST(? AS TEXT)) "
        "AND EXISTS(SELECT 1 FROM users u WHERE u.id=? AND u.is_active=1)))",
        (token, filename, uid, uid),
    ).fetchone()
    if not ok:
        raise HTTPException(404)
    try:
        path = images.responsive_image(filename, w)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    # 7 дней, а не год: секретную ссылку можно отключить или перегенерировать,
    # и не хочется, чтобы фото жили в чужих кэшах месяцами после этого
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=604800, immutable"})


VIDEO_TYPES = {"mp4": "video/mp4", "webm": "video/webm"}
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK = 256 * 1024


def ranged_file(path, media_type: str, request: Request):
    """FileResponse с поддержкой заголовка Range (206 Partial Content)."""
    size = path.stat().st_size
    base = {"Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=604800, immutable"}
    m = _RANGE.match(request.headers.get("range", ""))
    if not m:
        return FileResponse(path, media_type=media_type, headers=base)
    start = int(m.group(1) or 0)
    end = int(m.group(2)) if m.group(2) else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return Response(status_code=416,
                        headers={"Content-Range": f"bytes */{size}", **base})
    length = end - start + 1

    def chunks():
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                data = f.read(min(_CHUNK, left))
                if not data:
                    break
                left -= len(data)
                yield data

    return StreamingResponse(
        chunks(), status_code=206, media_type=media_type,
        headers={**base, "Content-Range": f"bytes {start}-{end}/{size}",
                 "Content-Length": str(length)})


@router.get("/c/{token}/video/{filename}")
def public_video(token: str, filename: str, request: Request, conn=Depends(get_db)):
    """Видео — по тем же правилам доступа, что и фото."""
    if not images.SAFE_FILENAME.match(filename):
        raise HTTPException(404)
    active_cat_or_410(conn, token, request)
    ext = filename.rsplit(".", 1)[-1]
    if ext not in VIDEO_TYPES:
        raise HTTPException(404)
    uid = request.session.get("user_id") or 0
    ok = conn.execute(
        "SELECT 1 FROM date_videos dv "
        "JOIN dates d ON d.id=dv.date_id "
        "JOIN date_categories dc ON dc.date_id=d.id "
        "JOIN categories c ON c.id=dc.category_id "
        "WHERE c.link_token=? AND c.link_enabled=1 AND dv.filename=? "
        "AND (d.is_draft=0 OR (d.guest_token=('u' || CAST(? AS TEXT)) "
        "AND EXISTS(SELECT 1 FROM users u WHERE u.id=? AND u.is_active=1)))",
        (token, filename, uid, uid),
    ).fetchone()
    path = images.UPLOAD_DIR / filename
    if not ok or not path.exists():
        raise HTTPException(404)
    return ranged_file(path, VIDEO_TYPES[ext], request)


# ---------------------------------------------------------------------------
# Поделиться отдельным событием: /d/<share_token>
# ---------------------------------------------------------------------------
# По стабильной секретной ссылке события любой залогиненный пользователь может
# добавить КОПИЮ события себе в коллекцию (новый owner_id = он сам). Так событие
# переиспользуется в чужих категориях, не нарушая изоляцию: оригинал и копия —
# независимые записи разных владельцев. Аноним только смотрит превью (как и на
# гостевой странице категории), действие требует входа.

def date_by_share(conn, token: str):
    return conn.execute(
        "SELECT * FROM dates WHERE share_token=?", (token,)).fetchone()


@router.get("/d/{token}", response_class=HTMLResponse)
def shared_date(token: str, request: Request, conn=Depends(get_db)):
    d = date_by_share(conn, token)
    if not d:
        return templates.TemplateResponse(request, "public/gone.html", status_code=404)
    me = viewer(request, conn)
    owner = users.get_user(conn, d["owner_id"])
    owner_name = (owner["display_name"] or owner["tg_username"] or "Автор") if owner else "Автор"
    is_mine = bool(me and me["id"] == d["owner_id"])
    # Без токена конкретной подборки нельзя доказать, что её владелец разрешил
    # копирование. Если событие есть хотя бы в одной защищённой подборке,
    # прямая ссылка остаётся страницей просмотра без действия «Сохранить».
    can_copy_event = not bool(conn.execute(
        "SELECT 1 FROM categories c "
        "JOIN date_categories dc ON dc.category_id=c.id "
        "WHERE dc.date_id=? AND c.link_enabled=1 AND c.prevent_copying=1 LIMIT 1",
        (d["id"],),
    ).fetchone())
    has_want = False
    wanted_by_me = False
    want_is_public = False
    want_visibility = None
    want_action_available = False
    my_review = None
    can_review = False
    if me and not is_mine:
        want = conn.execute(
            "SELECT is_public FROM date_wants WHERE user_id=? AND date_id=?",
            (me["id"], d["id"]),
        ).fetchone()
        has_want = want is not None
        want_is_public = bool(want and want["is_public"])
        want_visibility = (("public" if want_is_public else "private")
                           if want else None)
        my_review = conn.execute(
            "SELECT id, rating, text, is_public FROM date_reviews "
            "WHERE user_id=? AND date_id=?",
            (me["id"], d["id"]),
        ).fetchone()
        can_review = social_events.review_available(conn, d["id"], me["id"])
        want_action_available = social_events.want_action_available(
            conn, d["id"], int(me["id"]), now=now_naive(),
        )
        # Просроченная отметка больше не показывается как активная, но сама
        # связь нужна для доступа к форме уже доступного отзыва.
        wanted_by_me = has_want and want_action_available

    payload = date_payload(conn, d)
    # Прямая ссылка — самостоятельная карточка события. Она намеренно не
    # раскрывает, в какой подборке событие участвует, её голоса и результат.
    category_skin = appearance.FRIENDS
    first_og_image = first_existing_og_image(payload["images"])
    payload["og_preview_revision"] = images.og_preview_revision(
        [], category_skin,
        custom_image=first_og_image["filename"] if first_og_image else None,
        custom_focus=first_og_image["focus"] if first_og_image else None,
        use_default=not first_og_image,
    )
    guest = guest_name = None
    if me and not is_mine:
        guest, guest_name = guest_identity(me)

    payload["pending"] = False
    payload["past"] = bool(d["archived_at"])
    payload["editable"] = False
    payload["booked_by_me"] = False
    payload["booked_others_list"] = []
    payload["booked_others"] = ""
    payload["my_questions"] = []
    payload["participants"] = []
    payload["participant_hidden_count"] = 0
    payload["vote_count"] = 0
    payload["capacity"] = int(d["capacity"] or 1)
    payload["is_full"] = False
    payload["is_winner"] = False
    payload["is_loser"] = False
    payload["participation_withdrawn"] = False
    if guest:
        # Вопросы по share-ссылке принадлежат самому событию, не голосованию.
        payload["my_questions"] = conn.execute(
            "SELECT text, answer FROM questions WHERE date_id=? AND guest_token=? "
            "ORDER BY created_at", (d["id"], guest)).fetchall()
    if not payload["past"] and d["starts_at"]:
        payload["gcal"] = fmt_gcal(d["name"], d["starts_at"], d["ends_at"],
                                   d["place"], d["comment"],
                                   [l["url"] for l in payload["links"]])

    resp = templates.TemplateResponse(request, "public/share.html", {
        "d": payload,
        "token": token,
        "me": me,
        "guest_name": guest_name,
        "owner": owner,
        "owner_name": owner_name,
        "is_mine": is_mine,
        "has_want": has_want,
        "wanted_by_me": wanted_by_me,
        "want_is_public": want_is_public,
        "want_visibility": want_visibility,
        "can_publish_want": True,
        "can_copy_event": can_copy_event,
        "want_action_available": want_action_available,
        "my_review": my_review,
        "can_review": can_review,
        "csrf": request.session.get("csrf", ""),
        "can_act": False,
        "can_vote": False,
        "can_cast_vote": False,
        "can_question": not is_mine,
        "vote_state": None,
        "voting_accepts_votes": False,
        "category_skin": category_skin,
        "bot": auth_routes.BOT_USERNAME,
        "oauth": auth_routes._oauth_buttons(),
        "widget_state": (auth_routes.issue_widget_state(request)
                         if auth_routes.BOT_USERNAME and not me else None),
    })
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@router.get("/d/{token}/og-image")
def shared_date_og_image(token: str, skin: str | None = None,
                         v: str | None = None, conn=Depends(get_db)):
    """Единый link preview события и его публичных отзывов.

    Первый кадр сохраняет пользовательский focus и не получает накладываемых
    логотипов. Без доступного фото возвращаем единый default preview.
    """
    date_row = conn.execute(
        "SELECT id FROM dates WHERE share_token=?", (token,),
    ).fetchone()
    if not date_row:
        raise HTTPException(404)
    candidates = conn.execute(
        "SELECT filename, focus FROM date_images WHERE date_id=? "
        "ORDER BY position, id",
        (date_row["id"],),
    ).fetchall()
    row = first_existing_og_image(candidates)
    preview_skin = appearance.normalize_skin(skin, default=appearance.FRIENDS)
    if row:
        cropped = images.build_og_crop(
            row["filename"], row["focus"], preview_skin)
        if cropped:
            return FileResponse(
                cropped, media_type="image/webp",
                headers={"Cache-Control": "public, no-cache"},
            )
    return FileResponse(
        images.og_default_path(preview_skin),
        media_type="image/png",
        headers={"Cache-Control": "public, no-cache"},
    )


@router.get("/d/{token}/review/{review_id}", response_class=HTMLResponse)
def shared_profile_review(token: str, review_id: int, request: Request,
                          conn=Depends(get_db)):
    """Публичная share-ссылка ведёт на конкретный отзыв, не на событие."""
    row = conn.execute(
        "SELECT r.id AS review_id, r.user_id, r.rating, r.text AS review_text, "
        "r.is_public AS review_public, d.id AS date_id, d.name, d.share_token, "
        "d.owner_id AS event_owner_id, "
        "d.is_public AS date_public, d.is_draft AS date_draft, "
        "u.display_name, u.tg_username "
        "FROM date_reviews r JOIN dates d ON d.id=r.date_id "
        "JOIN users u ON u.id=r.user_id AND u.is_active=1 "
        "WHERE r.id=? AND d.share_token=? AND r.is_public=1 "
        "AND d.is_public=1 AND d.is_draft=0",
        (review_id, token),
    ).fetchone()
    if not row:
        return templates.TemplateResponse(request, "public/gone.html", status_code=404)
    review = dict(row)
    media = _batch_media(conn, [int(row["date_id"])])
    review["images"] = media["images"].get(int(row["date_id"]), [])
    me = viewer(request, conn)
    # Как и обычный публичный профиль, отзыв наследует оформление посетителя,
    # а не автора публикации. Для анонима стартовое оформление — стандартное.
    viewer_skin = appearance.normalize_skin(
        me["admin_skin"] if me else appearance.FRIENDS,
    )
    review_url = f"{BASE_URL}/d/{token}/review/{review_id}"
    first_og_image = first_existing_og_image(review["images"])
    review_og_revision = images.og_preview_revision(
        [], viewer_skin,
        custom_image=first_og_image["filename"] if first_og_image else None,
        custom_focus=first_og_image["focus"] if first_og_image else None,
        use_default=not first_og_image,
    )
    response = templates.TemplateResponse(
        request, "public/profile_review.html",
        {"request": request, "review": review, "is_me": False,
         "shareable": True,
         "reviewer_display": row["display_name"] or row["tg_username"]
         or f"Человек #{row['user_id']}",
         "category_skin": viewer_skin,
         "profile_return_url": "", "review_share_url": review_url,
         "review_og_revision": review_og_revision,
         "token": token, "me": me,
         "event_owner_id": int(row["event_owner_id"]),
         "csrf": request.session.get("csrf", ""),
         "bot": auth_routes.BOT_USERNAME,
         "oauth": auth_routes._oauth_buttons(),
         "widget_state": (auth_routes.issue_widget_state(request)
                          if auth_routes.BOT_USERNAME and not me else None)},
    )
    response.headers["X-Robots-Tag"] = "noindex"
    return response


@router.post("/d/{token}/want", dependencies=[Depends(users.current_user)])
def shared_date_want(token: str, request: Request,
                     visibility: Annotated[str | None, Form()] = None,
                     conn=Depends(get_db)):
    """Переключает публичную отметку «Хочу сходить».

    Старое поле ``visibility`` принимаем для совместимости кэшированных
    клиентов, но отметка теперь всегда публична. Приватное исходное событие
    всё равно не попадает в публичный профиль благодаря фильтру самого события.
    """
    d = date_by_share(conn, token)
    if not d:
        raise HTTPException(404, "Событие не найдено")
    user = request.state.user
    if int(user["id"]) == int(d["owner_id"]):
        raise HTTPException(400, "Это твоё событие")
    if visibility not in {None, "private", "public"}:
        raise HTTPException(400, "Видимость должна быть private или public")
    guest_throttle("want", viewer_token(user), request)
    existing = conn.execute(
        "SELECT is_public FROM date_wants WHERE user_id=? AND date_id=?",
        (user["id"], d["id"]),
    ).fetchone()
    want_public: bool | None
    if existing and visibility is not None:
        stamp = now_iso()
        conn.execute(
            "UPDATE date_wants SET is_public=1, updated_at=? "
            "WHERE user_id=? AND date_id=?",
            (stamp, user["id"], d["id"]),
        )
        msg = "Отметка видна в публичном профиле"
        wanted = True
        want_public = True
    elif existing:
        conn.execute(
            "DELETE FROM date_wants WHERE user_id=? AND date_id=?",
            (user["id"], d["id"]),
        )
        social_events.cancel_review_prompt(
            conn, d["id"], user["id"], "want_removed",
        )
        msg = "Убрано из «Хочу сходить»"
        wanted = False
        want_public = None
    else:
        if not social_events.want_action_available(
                conn, int(d["id"]), int(user["id"]), now=now_naive()):
            detail = "Дедлайн события уже прошёл или отзыв уже опубликован"
            if request.headers.get("x-requested-with") == "fetch":
                raise HTTPException(409, detail)
            return redir(f"/d/{token}", detail)
        stamp = now_iso()
        conn.execute(
            "INSERT INTO date_wants(user_id, date_id, is_public, created_at, updated_at) "
            "VALUES(?,?,?,?,?)",
            (user["id"], d["id"], 1, stamp, stamp),
        )
        # Отметка — это план, а не подтверждение посещения. Поэтому она не
        # создаёт Telegram-вопрос «Удалось ли сходить?».
        msg = "Добавлено в «Хочу сходить» и показано в публичном профиле"
        wanted = True
        want_public = True
    conn.commit()
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({
            "ok": True,
            "wanted": wanted,
            "want_is_public": bool(want_public),
            "want_visibility": (("public" if want_public else "private")
                                if want_public is not None else None),
            "message": msg,
        })
    return redir(f"/d/{token}", msg)


@router.post("/d/{token}/review", dependencies=[Depends(users.current_user)])
def shared_date_review(token: str, request: Request,
                       rating: int = Form(...), text: str = Form(""),
                       conn=Depends(get_db)):
    """Оценка 1–5 + необязательный текст; сохранение сразу публикует отзыв."""
    d = date_by_share(conn, token)
    if not d:
        raise HTTPException(404, "Событие не найдено")
    user = request.state.user
    if int(user["id"]) == int(d["owner_id"]):
        raise HTTPException(400, "Нельзя оставить отзыв о своём событии")
    guest_throttle("review", viewer_token(user), request)
    if not 1 <= rating <= 5:
        raise HTTPException(400, "Поставь оценку от 1 до 5")
    if not social_events.review_available(conn, d["id"], user["id"]):
        raise HTTPException(409, "Отзыв станет доступен после события")
    text = clean_text(text, 4000, "Текст отзыва")
    existing = conn.execute(
        "SELECT id FROM date_reviews WHERE user_id=? AND date_id=?",
        (user["id"], d["id"]),
    ).fetchone()
    stamp = now_iso()
    # Секретное или черновое событие нельзя раскрыть через профиль автора
    # отзыва. Сам отзыв сохраняется, но остаётся виден только владельцу.
    publish = 1 if d["is_public"] and not d["is_draft"] else 0
    conn.execute(
        """
        INSERT INTO date_reviews(
            user_id, date_id, rating, text, is_public, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(user_id, date_id) DO UPDATE SET
            rating=excluded.rating,
            text=excluded.text,
            is_public=excluded.is_public,
            updated_at=excluded.updated_at
        """,
        (user["id"], d["id"], rating, text or None, publish, stamp, stamp),
    )
    review = conn.execute(
        "SELECT id FROM date_reviews WHERE user_id=? AND date_id=?",
        (user["id"], d["id"]),
    ).fetchone()
    social_events.clear_review_waiting(conn, d["id"], user["id"])
    social_events.cancel_review_prompt(conn, d["id"], user["id"], "review_published")
    if not existing and publish:
        social_events.queue_review_received(conn, int(review["id"]))
    conn.commit()
    suffix = "" if publish else " Событие приватное, поэтому отзыв виден только тебе."
    return redir(
        f"/u/{user['id']}?tab=reviews#profileCollection",
        "Отзыв опубликован." + suffix,
    )


@router.post("/d/{token}/review/decline", dependencies=[Depends(users.current_user)])
def shared_date_review_decline(token: str, request: Request,
                               conn=Depends(get_db)):
    """Откладывает отзыв в центр «Ждут отзыва» без удаления события."""
    d = date_by_share(conn, token)
    if not d:
        raise HTTPException(404, "Событие не найдено")
    user = request.state.user
    if int(user["id"]) == int(d["owner_id"]):
        raise HTTPException(400, "Это твоё событие")
    guest_throttle("review-decline", viewer_token(user), request)
    if conn.execute(
        "SELECT 1 FROM date_reviews WHERE user_id=? AND date_id=?",
        (user["id"], d["id"]),
    ).fetchone():
        raise HTTPException(409, "Отзыв уже создан")
    if not social_events.mark_review_waiting(
            conn, d["id"], user["id"], "declined"):
        raise HTTPException(409, "Отзыв пока недоступен")
    social_events.cancel_review_prompt(conn, d["id"], user["id"], "review_declined")
    conn.commit()
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True, "message": "Событие добавлено в «Ждут отзыва»"})
    return redir(f"/d/{token}", "Событие добавлено в «Ждут отзыва»")


def _shared_event_or_404(conn, token: str):
    """Событие по прямой ссылке без неявного контекста подборки."""
    d = date_by_share(conn, token)
    if not d:
        raise HTTPException(404, "Событие не найдено")
    return d


@router.post("/d/{token}/question")
def shared_date_question(token: str, request: Request, bg: BackgroundTasks,
                         text: str = Form(...), conn=Depends(get_db)):
    """Вопрос по событию с прямой ссылки, независимо от подборок."""
    d = _shared_event_or_404(conn, token)
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
    guest_throttle("question", guest, request)
    text = clean_text(text, 2000, "Вопрос", required=True)
    conn.execute(
        "INSERT INTO questions(date_id, category_id, guest_token, user_id, text, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (d["id"], None, guest, user["id"], text, now_iso()))
    conn.commit()
    card = notify.card("❓ Новый вопрос", f"«{esc(d['name'])}»",
                       f"Кто: {esc(name)}", f"\n{esc(text)}")
    action_url = f"{BASE_URL}/admin/questions"
    notify_owner(bg, conn, d["owner_id"], card, preference="questions",
                 action_url=action_url, action_label="Ответить")
    notify_admin(bg, conn, d["owner_id"], card, action_url=action_url,
                 action_label="Открыть уведомления")
    return JSONResponse({"ok": True})


@router.post("/d/{token}/suggest_time")
def shared_date_suggest(token: str, request: Request, bg: BackgroundTasks,
                        starts_at: str = Form(""), ends_at: str = Form(""),
                        conn=Depends(get_db)):
    """Гость предлагает время для события без даты по share-ссылке."""
    d = _shared_event_or_404(conn, token)
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
    guest_throttle("question", guest, request)
    if d["starts_at"]:
        raise HTTPException(400, "У этого события уже назначено время")
    starts, ends = normalize_period(parse_dt_local(starts_at), parse_dt_local(ends_at))
    if not starts:
        raise HTTPException(400, "Выбери хотя бы дату и время начала")
    text = "📅 Предлагаю назначить: " + fmt_when(starts, ends)
    conn.execute(
        "INSERT INTO questions(date_id, category_id, guest_token, user_id, text, "
        "suggest_starts, suggest_ends, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (d["id"], None, guest, user["id"], text, starts, ends, now_iso()))
    conn.commit()
    card = notify.card("📅 Предложено время", f"«{esc(d['name'])}»",
                       f"Кто: {esc(name)}", f"Когда: {fmt_when(starts, ends)} (мск)")
    action_url = f"{BASE_URL}/admin/questions"
    notify_owner(bg, conn, d["owner_id"], card, preference="questions",
                 action_url=action_url, action_label="Выбрать ответ")
    notify_admin(bg, conn, d["owner_id"], card, action_url=action_url,
                 action_label="Открыть уведомления")
    return JSONResponse({"ok": True})


@router.get("/d/{token}/image/{filename}")
def shared_date_image(token: str, filename: str, w: int | None = None,
                      conn=Depends(get_db)):
    """Фото события по share-ссылке. Отдаём только фото ЭТОГО события —
    чужой файл по прямой ссылке не утечёт."""
    if not images.SAFE_FILENAME.match(filename):
        raise HTTPException(404)
    ok = conn.execute(
        "SELECT 1 FROM date_images di JOIN dates d ON d.id=di.date_id "
        "WHERE d.share_token=? AND di.filename=?",
        (token, filename)).fetchone()
    if not ok:
        raise HTTPException(404)
    try:
        path = images.responsive_image(filename, w)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=604800, immutable"})


@router.get("/d/{token}/video/{filename}")
def shared_date_video(token: str, filename: str, request: Request, conn=Depends(get_db)):
    """Видео события по share-ссылке — те же правила, что и у фото, + Range."""
    if not images.SAFE_FILENAME.match(filename):
        raise HTTPException(404)
    ext = filename.rsplit(".", 1)[-1]
    if ext not in VIDEO_TYPES:
        raise HTTPException(404)
    ok = conn.execute(
        "SELECT 1 FROM date_videos dv JOIN dates d ON d.id=dv.date_id "
        "WHERE d.share_token=? AND dv.filename=?",
        (token, filename)).fetchone()
    path = images.UPLOAD_DIR / filename
    if not ok or not path.exists():
        raise HTTPException(404)
    return ranged_file(path, VIDEO_TYPES[ext], request)


@router.post("/d/{token}/add")
def shared_date_add(token: str, request: Request, csrf: str = Form(""),
                    source_category_token: str = Form(""),
                    conn=Depends(get_db)):
    """Добавить копию события себе в коллекцию (нужен вход).

    Копия — отдельное активное событие получателя со СВОИМ свежим share_token
    (его сгенерит insert_date). Категории/голоса/вопросы не переносятся. Файлы
    фото/видео — физические копии (новые имена). Свою же ссылку добавлять незачем —
    отбиваем, чтобы не плодить дубли у себя."""
    d = date_by_share(conn, token)
    if not d:
        raise HTTPException(404, "Событие не найдено")
    user = acting_user(request, conn, csrf)
    guest_throttle("dadd", viewer_token(user), request)
    if user["id"] == d["owner_id"]:
        raise HTTPException(400, "Это твоё событие — оно уже в твоей коллекции")

    # Контекст подборки приходит только от её карточки. Проверяем и саму
    # привязку события, и настройку на сервере: скрытой кнопки недостаточно.
    source_category_token = (source_category_token or "").strip()
    if source_category_token:
        source_cat = active_cat_or_410(conn, source_category_token, request)
        linked = conn.execute(
            "SELECT 1 FROM date_categories WHERE category_id=? AND date_id=?",
            (source_cat["id"], d["id"]),
        ).fetchone()
        if not linked:
            raise HTTPException(404, "Событие не найдено в этой подборке")
        if source_cat["prevent_copying"]:
            raise HTTPException(403, "Владелец подборки запретил копирование событий")
    elif conn.execute(
        "SELECT 1 FROM categories c "
        "JOIN date_categories dc ON dc.category_id=c.id "
        "WHERE dc.date_id=? AND c.link_enabled=1 AND c.prevent_copying=1 LIMIT 1",
        (d["id"],),
    ).fetchone():
        # Иначе настройку можно было обойти ручным POST без hidden-поля.
        raise HTTPException(403, "Владелец запретил копирование этого события")

    source_id = int(d["source_date_id"] or d["id"])

    # Повторный тап/повтор fetch после сетевого таймаута возвращает уже созданную
    # копию. Это происходит до файловых операций и потому не плодит медиа.
    existing = conn.execute(
        "SELECT id FROM dates WHERE owner_id=? AND source_date_id=? "
        "ORDER BY id DESC LIMIT 1",
        (user["id"], source_id),
    ).fetchone()
    if existing:
        edit_url = f"/admin/dates/{int(existing['id'])}/edit"
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": True, "edit_url": edit_url,
                                 "already_added": True})
        return redir(edit_url, "Это событие уже есть в твоей коллекции")

    try:
        new_id = insert_date(
            conn, name=d["name"], place=d["place"], starts=d["starts_at"],
            ends=d["ends_at"], comment=d["comment"], origin="copy",
            guest_token=None, owner_id=user["id"], draft=0,
            pay_split=d["pay_split"], place_url=d["place_url"],
            capacity=d["capacity"], source_date_id=source_id,
        )
    except sqlite3.IntegrityError:
        # Уникальный индекс закрывает гонку двух одновременных тапов. После
        # освобождения writer-lock возвращаем уже созданную конкурентом копию.
        conn.rollback()
        existing = conn.execute(
            "SELECT id FROM dates WHERE owner_id=? AND source_date_id=? "
            "ORDER BY id DESC LIMIT 1",
            (user["id"], source_id),
        ).fetchone()
        if not existing:
            raise
        edit_url = f"/admin/dates/{int(existing['id'])}/edit"
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": True, "edit_url": edit_url,
                                 "already_added": True})
        return redir(edit_url, "Это событие уже есть в твоей коллекции")
    try:
        copy_date_media_and_links(conn, d["id"], new_id)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    edit_url = f"/admin/dates/{new_id}/edit"
    # fetch-запрос (виджет ленты комьюнити) хочет остаться на месте — отвечаем
    # JSON и показываем тост; обычный переход по ссылке /d/<token> — редиректом.
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True, "edit_url": edit_url})
    return redir(edit_url,
                 "Событие добавлено в твою коллекцию ♥ Привяжи его к своим категориям")


# ---------------------------------------------------------------------------
# Календарь (.ics)
# ---------------------------------------------------------------------------

def _ics_escape(s: str | None) -> str:
    return ((s or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\n").replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    # 36 символов ≈ 72 байта для кириллицы — укладываемся в лимит RFC (75)
    chunks = []
    while len(line) > 36:
        chunks.append(line[:36])
        line = line[36:]
    chunks.append(line)
    return "\r\n ".join(chunks)


@router.get("/c/{token}/ics/{date_id}")
def public_ics(token: str, date_id: int, request: Request,
               conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token, request)
    d = date_in_category(conn, cat["id"], date_id)
    if not d or not d["starts_at"]:
        raise HTTPException(404, "У этого события нет даты")
    return _ics_response(conn, d, f"date-{d['id']}-{cat['id']}@{DOMAIN}")


@router.get("/d/{token}/ics")
def shared_ics(token: str, conn=Depends(get_db)):
    """Календарь для события по share-ссылке."""
    d = date_by_share(conn, token)
    if not d or not d["starts_at"]:
        raise HTTPException(404, "У этого события нет даты")
    return _ics_response(conn, d, f"date-{d['id']}-share@{DOMAIN}")


def _ics_response(conn, d, uid: str) -> Response:
    """Собирает .ics-файл для одного события (общий код для /c и /d)."""
    start = _parse(d["starts_at"]).replace(tzinfo=MSK)
    end = (_parse(d["ends_at"]).replace(tzinfo=MSK) if d["ends_at"]
           else start + timedelta(hours=2))
    f = "%Y%m%dT%H%M%SZ"

    desc_parts = []
    if d["comment"]:
        desc_parts.append(d["comment"])
    desc_parts += [r["url"] for r in conn.execute(
        "SELECT url FROM date_links WHERE date_id=? ORDER BY position, id", (d["id"],))]

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//date4you//RU",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
        f"UID:{uid}",
        "DTSTAMP:" + datetime.now(timezone.utc).strftime(f),
        "DTSTART:" + start.astimezone(timezone.utc).strftime(f),
        "DTEND:" + end.astimezone(timezone.utc).strftime(f),
        "SUMMARY:" + _ics_escape(d["name"]),
    ]
    if d["place"]:
        lines.append("LOCATION:" + _ics_escape(d["place"]))
    if desc_parts:
        lines.append("DESCRIPTION:" + _ics_escape("\n".join(desc_parts)))
    lines += ["END:VEVENT", "END:VCALENDAR"]

    body = "\r\n".join(_ics_fold(l) for l in lines) + "\r\n"
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="event-{d["id"]}.ics"'})


# ---------------------------------------------------------------------------
# Действия гостя (требуют входа в аккаунт)
# ---------------------------------------------------------------------------

@router.post("/c/{token}/book")
def public_book(token: str, request: Request, bg: BackgroundTasks,
                date_id: int = Form(...), conn=Depends(get_db)):
    """Голос за вариант; режим single/multiple задаёт владелец категории."""
    cat = active_cat_or_410(conn, token, request)
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
    legacy_date_ids = legacy_vote_date_ids(conn, request, user, cat["id"])
    claim_legacy_votes(conn, request, user, cat["id"])
    guest_throttle("book", guest, request)

    mine = conn.execute(
        "SELECT id FROM bookings WHERE date_id=? AND category_id=? AND guest_token=?",
        (date_id, cat["id"], guest)).fetchone()
    if mine:
        # A pre-v22 ballot may belong to a date archived before voting became
        # configurable. It remains removable, but an archived date can never
        # be used to create a new ballot.
        d = conn.execute(
            "SELECT d.* FROM dates d JOIN date_categories dc ON dc.date_id=d.id "
            "WHERE d.id=? AND dc.category_id=? AND d.is_draft=0",
            (date_id, cat["id"]),
        ).fetchone()
    else:
        d = date_in_category(conn, cat["id"], date_id)
    if not d:
        raise HTTPException(404, "Событие не найдено")

    try:
        if mine:
            result = voting.remove_vote(conn, cat["id"], date_id, user["id"])
            booked = False
        else:
            result = voting.cast_vote(conn, cat["id"], date_id, user["id"])
            booked = True
    except voting.VotingError as exc:
        if exc.code == "voting_deadline_passed":
            voting_events.close_due_once(conn, category_id=cat["id"])
        raise _voting_http_error(exc)

    if booked:
        voting_events.queue_deadline_reminder(conn, cat["id"], user["id"])
    elif not result.current_date_ids:
        voting_events.cancel_deadline_reminder(conn, cat["id"], user["id"])

    card = notify.card(
        "💝 Новый голос" if booked else "🤍 Голос снят",
        f"«{esc(d['name'])}» · {esc(cat['name'])}", f"Кто: {esc(name)}")
    action_url = f"{BASE_URL}/admin/categories/{cat['id']}"
    notify_owner(bg, conn, cat["owner_id"], card, preference="votes",
                 action_url=action_url, action_label="Открыть категорию")
    notify_admin(bg, conn, cat["owner_id"], card, action_url=action_url,
                 action_label="Открыть категорию")
    affected = {date_id}
    affected.update(getattr(result, "removed_date_ids", ()))
    affected.update(legacy_date_ids)
    if legacy_date_ids:
        affected.update(result.current_date_ids)
    updates = _vote_card_updates(conn, cat["id"], affected, guest)
    conn.commit()
    return JSONResponse({"ok": True, "booked": booked, "name": name,
                         "choices": list(result.current_date_ids),
                         "updates": updates,
                         "voting_status": cat["voting_status"]})


@router.post("/c/{token}/withdraw")
def public_withdraw(token: str, request: Request, bg: BackgroundTasks,
                    conn=Depends(get_db)):
    """Участник победившего варианта может отказаться без пересчёта итога."""
    cat = active_cat_or_410(conn, token, request)
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
    guest_throttle("withdraw", guest, request)
    try:
        result = voting.withdraw_participation(conn, cat["id"], user["id"])
    except voting.VotingError as exc:
        raise _voting_http_error(exc)
    winner = conn.execute("SELECT name FROM dates WHERE id=?",
                          (result.winner_date_id,)).fetchone()
    voting_events.cancel_user_winner_reminders(conn, cat["id"], user["id"])
    title = winner["name"] if winner else "Победившее событие"
    if not result.already_withdrawn:
        voting_events.queue_participant_withdrawal(
            conn,
            booking_id=result.booking_id,
            owner_id=cat["owner_id"],
            participant_name=name,
            category_name=cat["name"],
            date_name=title,
            category_id=cat["id"],
        )
    conn.commit()
    if not result.already_withdrawn:
        card = notify.card("↩️ Участник отказался от события",
                           f"«{esc(title)}» · {esc(cat['name'])}",
                           f"Кто: {esc(name)}",
                           "Итог голосования не изменился.")
        notify_admin(bg, conn, cat["owner_id"], card)
    return JSONResponse({"ok": True, "withdrawn": True,
                         "already_withdrawn": result.already_withdrawn})


@router.post("/c/{token}/suggest_time")
def public_suggest_time(token: str, request: Request, bg: BackgroundTasks,
                        date_id: int = Form(...),
                        starts_at: str = Form(""), ends_at: str = Form(""),
                        conn=Depends(get_db)):
    """Гость предлагает время для события без даты.

    Хранится как обычный вопрос: появляется у админа во «Вопросах»
    с кнопками «Принять/Отказаться», автор видит его (и ответ) под карточкой.
    """
    cat = active_cat_or_410(conn, token, request)
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
    guest_throttle("question", guest, request)

    d = date_in_category(conn, cat["id"], date_id)
    if not d:
        raise HTTPException(404, "Событие не найдено")
    if d["starts_at"]:
        raise HTTPException(400, "У этого события уже назначено время")
    starts, ends = normalize_period(parse_dt_local(starts_at), parse_dt_local(ends_at))
    if not starts:
        raise HTTPException(400, "Выбери хотя бы дату и время начала")

    text = "📅 Предлагаю назначить: " + fmt_when(starts, ends)
    conn.execute(
        "INSERT INTO questions(date_id, category_id, guest_token, user_id, text, "
        "suggest_starts, suggest_ends, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (date_id, cat["id"], guest, user["id"], text, starts, ends, now_iso()))
    conn.commit()
    card = notify.card(
        "📅 Предложено время",
        f"«{esc(d['name'])}» · {esc(cat['name'])}",
        f"Кто: {esc(name)}",
        f"Когда: {fmt_when(starts, ends)} (мск)")
    action_url = f"{BASE_URL}/admin/questions"
    notify_owner(bg, conn, cat["owner_id"], card, preference="questions",
                 action_url=action_url, action_label="Выбрать ответ")
    notify_admin(bg, conn, cat["owner_id"], card, action_url=action_url,
                 action_label="Открыть уведомления")
    return JSONResponse({"ok": True})


@router.post("/c/{token}/question")
def public_question(token: str, request: Request, bg: BackgroundTasks,
                    date_id: int = Form(...), text: str = Form(...),
                    conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token, request)
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
    guest_throttle("question", guest, request)

    d = date_in_category(conn, cat["id"], date_id)
    if not d:
        raise HTTPException(404, "Событие не найдено")
    text = clean_text(text, 2000, "Вопрос", required=True)

    conn.execute(
        "INSERT INTO questions(date_id, category_id, guest_token, user_id, text, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (date_id, cat["id"], guest, user["id"], text, now_iso()),
    )
    conn.commit()
    card = notify.card(
        "❓ Новый вопрос",
        f"«{esc(d['name'])}» · {esc(cat['name'])}",
        f"От: {esc(name)}",
        f"\n{esc(text)}")
    action_url = f"{BASE_URL}/admin/questions"
    notify_owner(bg, conn, cat["owner_id"], card, preference="questions",
                 action_url=action_url, action_label="Ответить")
    notify_admin(bg, conn, cat["owner_id"], card, action_url=action_url,
                 action_label="Открыть уведомления")
    return JSONResponse({"ok": True})


@router.post("/c/{token}/propose")
def public_propose(token: str, request: Request, bg: BackgroundTasks,
                   name: str = Form(...), place: str = Form(""),
                   starts_at: str = Form(""), ends_at: str = Form(""),
                   links: str = Form(""), comment: str = Form(""),
                   pay: str = Form("0"),
                   capacity: str = Form("1"),
                   photos: list[UploadFile] = File(default=[], alias="images"),
                   videos: list[UploadFile] = File(default=[], alias="videos"),
                   video: UploadFile | None = File(None),  # legacy singular client
                   conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token, request)
    conn.execute("UPDATE categories SET id=id WHERE id=?", (cat["id"],))
    cat = active_cat_or_410(conn, token, request)
    ensure_category_editable(conn, cat)
    user = acting_user(request, conn)
    guest, author = ensure_guest_name(conn, user)
    guest_throttle("prop", guest, request)

    name = clean_text(name, 200, "Название", required=True)
    place, place_url, needs_resolve = places.split_place(clean_text(place, 500, "Место"))
    comment = clean_text(comment, 2000, "Комментарий")
    starts, ends = normalize_period(parse_dt_local(starts_at), parse_dt_local(ends_at))
    validate_candidate_start(cat, starts)
    link_list = parse_links(links)
    capacity_value = parse_capacity(capacity)
    pay_value = parse_pay_split(pay)
    video_files = [f for f in videos if f and f.filename]
    if video and video.filename:
        video_files.append(video)
    if len(video_files) > images.MAX_VIDEOS:
        raise HTTPException(400, f"Максимум {images.MAX_VIDEOS} видео у одного события")

    moderated = bool(cat["moderate_proposals"])
    date_id = insert_date(conn, name=name, place=place, starts=starts, ends=ends,
                          comment=comment, origin="guest", guest_token=guest,
                          owner_id=cat["owner_id"],   # предложение принадлежит владельцу категории
                          proposed_by=user["id"],     # но автор — гость (для уведомления о публикации)
                          draft=1 if moderated else 0,
                          pay_split=pay_value, place_url=place_url,
                          capacity=capacity_value)
    conn.execute(
        "INSERT INTO date_categories(date_id, category_id, position) VALUES(?,?,?)",
        (date_id, cat["id"], next_cat_pos(conn, cat["id"])))
    save_links(conn, date_id, link_list)
    saved_photos = add_photos(conn, date_id, photos, existing=0)
    try:
        saved_videos = images.save_videos_batch(video_files)
    except ValueError as e:
        conn.rollback()
        for fn in saved_photos:        # фото уже на диске — не оставляем сирот
            images.delete_file(fn)
        raise HTTPException(400, str(e))
    for position, filename in enumerate(saved_videos):
        conn.execute(
            "INSERT INTO date_videos(date_id, filename, position) VALUES(?,?,?)",
            (date_id, filename, position),
        )
    conn.commit()
    if needs_resolve:
        bg.add_task(places.resolve_into_db, date_id, place_url)

    msg = notify.card(
        "💡 Новое предложение" + (" (на модерации)" if moderated else ""),
        f"«{esc(name)}» · {esc(cat['name'])}",
        f"От: {esc(author)}",
        f"📍 {esc(place)}" if place else "",
        f"🕐 {fmt_when(starts, ends)} (мск)" if fmt_when(starts, ends) else "")
    action_url = f"{BASE_URL}/admin/dates/{date_id}/edit"
    notify_owner(bg, conn, cat["owner_id"], msg, preference="proposals",
                 action_url=action_url, action_label="Открыть предложение")
    notify_admin(bg, conn, cat["owner_id"], msg, action_url=action_url,
                 action_label="Открыть предложение")

    return JSONResponse({"ok": True, "id": date_id, "moderated": moderated})


@router.post("/c/{token}/propose/{date_id}/edit")
def public_propose_edit(token: str, date_id: int, request: Request, bg: BackgroundTasks,
                        name: str = Form(...), place: str = Form(""),
                        starts_at: str = Form(""), ends_at: str = Form(""),
                        links: str = Form(""), comment: str = Form(""),
                        keep_order: str = Form(""),
                        keep_video_order: str = Form(""),
                        remove_image: list[int] = Form(default=[]),
                        remove_video: list[int] = Form(default=[]),
                        pay: str = Form("0"),
                        capacity: str = Form("1"),
                        photos: list[UploadFile] = File(default=[], alias="images"),
                        videos: list[UploadFile] = File(default=[], alias="videos"),
                        video: UploadFile | None = File(None),  # legacy singular client
                        conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token, request)
    ensure_category_editable(conn, cat)
    user = acting_user(request, conn)
    guest, author = ensure_guest_name(conn, user)
    d = own_proposal_or_403(conn, cat, date_id, guest)
    conn.execute("UPDATE dates SET id=id WHERE id=?", (date_id,))
    cat = active_cat_or_410(conn, token, request)
    ensure_category_editable(conn, cat)
    d = own_proposal_or_403(conn, cat, date_id, guest)
    for linked_cat in conn.execute(
        "SELECT c.* FROM categories c JOIN date_categories dc ON dc.category_id=c.id "
        "WHERE dc.date_id=?", (date_id,),
    ):
        ensure_category_editable(conn, linked_cat)
    guest_throttle("prop", guest, request)

    name = clean_text(name, 200, "Название", required=True)
    place, place_url, needs_resolve = places.place_on_edit(
        clean_text(place, 500, "Место"), d)
    comment = clean_text(comment, 2000, "Комментарий")
    starts, ends = normalize_period(parse_dt_local(starts_at), parse_dt_local(ends_at))
    for linked_cat in conn.execute(
        "SELECT c.* FROM categories c JOIN date_categories dc ON dc.category_id=c.id "
        "WHERE dc.date_id=?", (date_id,),
    ):
        validate_candidate_start(linked_cat, starts)
    link_list = parse_links(links)
    capacity_value = parse_capacity(capacity)
    pay_value = parse_pay_split(pay)

    # Проверяем capacity до записи новых файлов на диск. При отказе (например,
    # лимит ниже уже набранного) редактор не оставит осиротевшие фото/видео.
    try:
        voting.set_date_capacity(conn, date_id, d["owner_id"], capacity_value)
    except voting.VotingError as exc:
        raise _voting_http_error(exc)

    changed_labels: list[str] = []
    if name != d["name"]:
        changed_labels.append("название")
    if place != (d["place"] or ""):
        changed_labels.append("место")
    if starts != d["starts_at"] or ends != d["ends_at"]:
        changed_labels.append("дата или время")
    if pay_value != int(d["pay_split"] or 0):
        changed_labels.append("условия оплаты")
    if capacity_value != int(d["capacity"] or 1):
        changed_labels.append("количество участников")

    existing = conn.execute(
        "SELECT id, filename FROM date_images WHERE date_id=? ORDER BY position, id",
        (date_id,)).fetchall()
    remove_set = set(remove_image)
    to_remove = [r for r in existing if r["id"] in remove_set]
    keep_count = len(existing) - len(to_remove)
    new_files = [f for f in photos if f and f.filename]
    if keep_count + len(new_files) > images.MAX_IMAGES:
        raise HTTPException(400, f"Максимум {images.MAX_IMAGES} фото у одного события")

    vids = conn.execute(
        "SELECT id, filename FROM date_videos WHERE date_id=? ORDER BY position, id",
        (date_id,),
    ).fetchall()
    video_remove_set = set(remove_video)
    # Старый кэшированный клиент посылал singular `video` и ожидал замену.
    # Новый `videos` добавляет/сортирует до общего лимита, не удаляя остальные.
    legacy_replace = bool(video and video.filename and
                          not any(f and f.filename for f in videos))
    if legacy_replace:
        video_remove_set.update(int(v["id"]) for v in vids)
    vid_remove = [v for v in vids if v["id"] in video_remove_set]
    remaining_vids = [v["id"] for v in vids if v["id"] not in video_remove_set]
    new_video_files = [f for f in videos if f and f.filename]
    if video and video.filename:
        new_video_files.append(video)
    if len(remaining_vids) + len(new_video_files) > images.MAX_VIDEOS:
        raise HTTPException(400, f"Максимум {images.MAX_VIDEOS} видео у одного события")

    # Разбираем недоверенные order-токены до записи upload-файлов. Даже
    # намеренно испорченный multipart не должен оставить сироты на диске.
    remaining = [r["id"] for r in existing if r["id"] not in remove_set]
    photo_order = ordered_media_refs(keep_order, remaining, len(new_files))
    video_order = ordered_media_refs(
        keep_video_order, remaining_vids, len(new_video_files))

    saved: list[str] = []
    saved_videos: list[str] = []
    try:
        saved = images.save_batch(new_files)
        saved_videos = images.save_videos_batch(new_video_files)
    except ValueError as e:
        for filename in (*saved, *saved_videos):
            images.delete_file(filename)
        raise HTTPException(400, str(e))

    for r in to_remove:
        conn.execute("DELETE FROM date_images WHERE id=?", (r["id"],))

    # Порядок объединяет сохранённые и новые элементы. Bare id остаются
    # совместимы со старым keep_order; s<ID>/n<index> задаёт точное чередование.
    for position, (kind, value) in enumerate(photo_order):
        if kind == "saved":
            conn.execute(
                "UPDATE date_images SET position=? WHERE id=? AND date_id=?",
                (position, value, date_id),
            )
        else:
            conn.execute(
                "INSERT INTO date_images(date_id, filename, position) VALUES(?,?,?)",
                (date_id, saved[value], position),
            )

    for v in vid_remove:
        conn.execute("DELETE FROM date_videos WHERE id=?", (v["id"],))
    for position, (kind, value) in enumerate(video_order):
        if kind == "saved":
            conn.execute(
                "UPDATE date_videos SET position=? WHERE id=? AND date_id=?",
                (position, value, date_id),
            )
        else:
            conn.execute(
                "INSERT INTO date_videos(date_id, filename, position) VALUES(?,?,?)",
                (date_id, saved_videos[value], position),
            )

    conn.execute(
        "UPDATE dates SET name=?, place=?, place_url=?, starts_at=?, ends_at=?, "
        "comment=?, pay_split=? WHERE id=?",
        (name, place, place_url, starts, ends, comment, pay_value, date_id))
    save_links(conn, date_id, link_list)
    voting_events.queue_date_changed(conn, date_id, changed_labels)
    conn.commit()
    if needs_resolve:
        bg.add_task(places.resolve_into_db, date_id, place_url)
    for r in to_remove:               # файлы — только после коммита
        images.delete_file(r["filename"])
    for v in vid_remove:
        images.delete_file(v["filename"])

    changed_card = notify.card(
        "✏️ Предложение изменено",
        f"«{esc(name)}» · {esc(cat['name'])}",
        f"Автор: {esc(author)}")
    action_url = f"{BASE_URL}/admin/dates/{date_id}/edit"
    notify_owner(bg, conn, cat["owner_id"], changed_card, preference="proposals",
                 action_url=action_url, action_label="Открыть предложение")
    notify_admin(bg, conn, cat["owner_id"], changed_card, action_url=action_url,
                 action_label="Открыть предложение")
    return JSONResponse({"ok": True})


@router.post("/c/{token}/propose/{date_id}/delete")
def public_propose_delete(token: str, date_id: int, request: Request, bg: BackgroundTasks,
                          conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token, request)
    ensure_category_editable(conn, cat)
    user = acting_user(request, conn)
    guest, author = ensure_guest_name(conn, user)
    d = own_proposal_or_403(conn, cat, date_id, guest)
    conn.execute("UPDATE dates SET id=id WHERE id=?", (date_id,))
    cat = active_cat_or_410(conn, token, request)
    ensure_category_editable(conn, cat)
    d = own_proposal_or_403(conn, cat, date_id, guest)
    guest_throttle("prop", guest, request)

    linked_cats = conn.execute(
        "SELECT c.* FROM categories c JOIN date_categories dc ON dc.category_id=c.id "
        "WHERE dc.date_id=?", (date_id,),
    ).fetchall()
    affected_by_cat: dict[int, list[int]] = {}
    for linked_cat in linked_cats:
        ensure_category_editable(conn, linked_cat)
    for linked_cat in linked_cats:
        affected_by_cat[int(linked_cat["id"])] = [
            int(r["user_id"]) for r in conn.execute(
                "SELECT DISTINCT user_id FROM bookings WHERE date_id=? AND category_id=? "
                "AND user_id IS NOT NULL", (date_id, linked_cat["id"]),
            )
        ]
        voting_events.queue_date_removed(
            conn, date_id, d["name"], linked_cat["id"], linked_cat["name"],
            linked_cat["link_token"],
        )

    files = [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_images WHERE date_id=?", (date_id,))]
    files += [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_videos WHERE date_id=?", (date_id,))]
    conn.execute("DELETE FROM dates WHERE id=?", (date_id,))
    for category_id, user_ids in affected_by_cat.items():
        for user_id in user_ids:
            if not conn.execute(
                "SELECT 1 FROM bookings WHERE category_id=? AND user_id=? LIMIT 1",
                (category_id, user_id),
            ).fetchone():
                voting_events.cancel_deadline_reminder(conn, category_id, user_id)
    conn.commit()
    for fn in files:
        images.delete_file(fn)

    deleted_card = notify.card(
        "🗑 Предложение удалено",
        f"«{esc(d['name'])}» · {esc(cat['name'])}",
        f"Автор: {esc(author)}")
    action_url = f"{BASE_URL}/admin/categories/{cat['id']}"
    notify_owner(bg, conn, cat["owner_id"], deleted_card, preference="proposals",
                 action_url=action_url, action_label="Открыть категорию")
    notify_admin(bg, conn, cat["owner_id"], deleted_card, action_url=action_url,
                 action_label="Открыть категорию")
    return JSONResponse({"ok": True})


@router.post("/c/{token}/report")
def public_report(token: str, request: Request, bg: BackgroundTasks,
                  target_type: str = Form(...), target_id: int = Form(...),
                  reason: str = Form(""), csrf: str = Form(""),
                  conn=Depends(get_db)):
    """Жалоба без обязательного входа на событие или саму подборку.

    Контент существует и виден по этой ссылке — иначе 404 (нельзя слать
    жалобы на чужой/скрытый id). Для анонима в БД попадает только случайный
    per-browser токен, не IP или fingerprint.
    """
    cat = active_cat_or_410(conn, token, request)
    reporter = report_identity(request, conn, csrf)
    guest_throttle("report", reporter, request)

    if target_type not in ("date", "category"):
        raise HTTPException(400, "Некорректная цель жалобы")
    if target_type == "category":
        target_id = cat["id"]                 # жалоба на категорию — только на эту
        label = cat["name"]
    else:
        d = date_in_category(conn, cat["id"], target_id)
        if not d:
            raise HTTPException(404, "Событие не найдено")
        label = d["name"]

    # Защита от дублей: один гость — одна открытая жалоба на объект.
    dup = conn.execute(
        "SELECT 1 FROM reports WHERE target_type=? AND target_id=? AND reporter=? "
        "AND status='open'", (target_type, target_id, reporter)).fetchone()
    if not dup:
        reason = clean_text(reason, 1000, "Причина") or None
        conn.execute(
            "INSERT INTO reports(target_type, target_id, reporter, reason, "
            "status, created_at) VALUES(?,?,?,?,'open',?)",
            (target_type, target_id, reporter, reason, now_iso()))
        conn.commit()
        bg.add_task(notify.notify, notify.card(
            "🚩 Жалоба",
            f"На {('категорию' if target_type=='category' else 'событие')}: «{esc(label)}»",
            f"Категория: {esc(cat['name'])}",
            f"Причина: {esc(reason or 'без описания')}",
            f"\n{BASE_URL}/operator/reports"))
    return JSONResponse({"ok": True})


@router.post("/d/{token}/report")
def shared_date_report(token: str, request: Request, bg: BackgroundTasks,
                       target_type: str = Form("date"), target_id: int = Form(0),
                       reason: str = Form(""), csrf: str = Form(""),
                       conn=Depends(get_db)):
    """Жалоба со страницы события или его публичного отзыва.

    Операторская модель жалоб знает события и категории, поэтому кнопка отзыва
    намеренно указывает на исходное событие и не подменяет id типом ``review``.
    """
    d = date_by_share(conn, token)
    if not d:
        raise HTTPException(404, "Событие не найдено")
    reporter = report_identity(request, conn, csrf)
    guest_throttle("report", reporter, request)
    if target_type != "date" or (target_id and int(target_id) != int(d["id"])):
        raise HTTPException(400, "Некорректная цель жалобы")
    duplicate = conn.execute(
        "SELECT 1 FROM reports WHERE target_type='date' AND target_id=? "
        "AND reporter=? AND status='open'", (d["id"], reporter),
    ).fetchone()
    reason = clean_text(reason, 1000, "Причина") or None
    if not duplicate:
        conn.execute(
            "INSERT INTO reports(target_type,target_id,reporter,reason,status,created_at) "
            "VALUES('date',?,?,?,'open',?)",
            (d["id"], reporter, reason, now_iso()),
        )
        conn.commit()
        bg.add_task(notify.notify, notify.card(
            "🚩 Жалоба",
            f"На событие: «{esc(d['name'])}»",
            "Источник: публичная ссылка или отзыв",
            f"Причина: {esc(reason or 'без описания')}",
            f"\n{BASE_URL}/operator/reports",
        ))
    return JSONResponse({"ok": True})
