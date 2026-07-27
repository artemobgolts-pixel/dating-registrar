"""Публичная часть: главная, /health и страницы категорий /c/<токен>.

Гости представляются по имени, выбирают свидание (одно свидание — один
человек), предлагают свои идеи и задают вопросы.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Request, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse, Response,
                               StreamingResponse)

import auth_routes
import db
import guests as legacy_guests
import images
import notify
import places
import users
import voting
import voting_events
from config import (AUTHOR_PROJECTS, ABOUT_TEXT, BASE_URL, DOMAIN,
                    MSK, SUPPORT_CONTACT, support_link)
from helpers import (_parse, clean_text, fmt_gcal, fmt_when, new_link_token,
                     normalize_period, now_iso, parse_dt_local, parse_links)
from notify import esc
from ratelimit import guest_throttle
from tasks import autoarchive_once
from web import get_db, redir, templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Идентичность посетителя гостевой страницы
# ---------------------------------------------------------------------------
# Раньше гость представлялся только именем (cookie-токен). Теперь все действия
# (бронь, вопрос, предложение) требуют входа в аккаунт: аноним только смотрит.
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


def acting_user(request, conn):
    """Для POST-действий гостя: нужен вход. Нет валидной сессии → 401 с флагом
    need_login (фронт уводит на /login?next=…)."""
    u = viewer(request, conn)
    if not u:
        raise HTTPException(401, {"need_login": True,
                                  "msg": "Войди в аккаунт, чтобы продолжить ♥"})
    return u


def viewer_token(user) -> str | None:
    """Стабильный guest_token залогиненного посетителя."""
    return f"u{user['id']}" if user else None


def ensure_guest_name(conn, user) -> tuple[str, str]:
    """Заводит/обновляет строку guests для залогиненного посетителя (имя из
    профиля). Возвращает (guest_token, имя). Без commit — вызывающий коммитит
    вместе со своим действием."""
    token = viewer_token(user)
    name = ((user["display_name"] or "").strip()
            or (user["tg_username"] or "").strip()
            or f"Человек #{user['id']}")
    conn.execute(
        "INSERT INTO guests(token, name, created_at) VALUES(?,?,?) "
        "ON CONFLICT(token) DO UPDATE SET name=excluded.name",
        (token, name, now_iso()))
    return token, name


def claim_legacy_votes(conn, request: Request, user, category_id: int) -> int:
    """Безопасно привязывает старые cookie-голоса к текущему аккаунту.

    До v13 гость определялся только секретной HttpOnly-cookie, а в bookings
    оставался ``user_id=NULL``. Владение той же cookie — единственное доступное
    доказательство авторства. Переносим такие бюллетени лишь пока голосование
    можно менять; завершённый результат никогда не пересчитываем.
    """
    legacy = legacy_guests.get_guest(request)
    stable = viewer_token(user)
    # Идентификаторы аккаунтов всегда имеют предсказуемый формат ``u<ID>``.
    # После удаления аккаунта user_id обнуляется, но такой бюллетень нельзя
    # превращать в доступный для присвоения bearer-токен.
    if not legacy or legacy == stable or re.fullmatch(r"u\d+", legacy):
        return 0

    # Сериализуем перенос с голосами, настройкой и закрытием, затем перечитываем.
    if conn.execute("UPDATE categories SET id=id WHERE id=?", (category_id,)).rowcount == 0:
        return 0
    cat = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if not cat or cat["voting_status"] not in {
            voting.STATUS_UNCONFIGURED, voting.STATUS_OPEN}:
        return 0
    # Просмотр своей категории не должен превращать старый анонимный бюллетень
    # в голос владельца и обходить правило «создатель не считается участником».
    if int(cat["owner_id"]) == int(user["id"]):
        return 0
    if cat["voting_status"] == voting.STATUS_OPEN:
        try:
            if datetime.fromisoformat(cat["voting_deadline"]) <= \
                    datetime.now(MSK).replace(tzinfo=None, microsecond=0):
                return 0
        except (TypeError, ValueError):
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


def active_cat_or_410(conn, token: str):
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        raise HTTPException(410, "Ссылка больше не активна")
    return cat


def date_in_category(conn, category_id: int, date_id: int):
    """Опубликованное активное свидание в категории (для выбора/вопросов/ics)."""
    return conn.execute(
        "SELECT d.* FROM dates d JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE d.id=? AND dc.category_id=? AND d.archived_at IS NULL AND d.is_draft=0",
        (date_id, category_id),
    ).fetchone()


def notify_owner(bg, conn, owner_id: int, text: str) -> None:
    """Шлёт уведомление владельцу о событии на его свидании (выбор, вопрос,
    предложение...). chat_id резолвим СЕЙЧАС (conn ещё открыт), а сам сетевой
    вызов уводим в фон. Если бот владельцем не подключён — тихо ничего не шлём."""
    chat = notify.owner_chat_id(conn, owner_id)
    if chat is not None:
        bg.add_task(notify.send_to, chat, text)


def notify_user(bg, conn, user_id: int | None, text: str) -> None:
    """Шлёт уведомление произвольному пользователю (автору вопроса/предложения,
    гостю при снятии брони). Те же правила доставки, что и у владельца:
    бот подключён, активен, не легаси. None/нет бота — тихо пропускаем."""
    if user_id is None:
        return
    chat = notify.user_chat_id(conn, user_id)
    if chat is not None:
        bg.add_task(notify.send_to, chat, text)


def notify_admin(bg, conn, owner_id: int | None, text: str) -> None:
    """Глобальный поток администратора платформы (TG_CHAT_ID): он видит КАЖДОЕ
    действие всех пользователей. Дополняем готовую карточку строкой «Владелец»,
    чтобы было подписано, чьего кабинета касается событие. Сетевой вызов — в фон.
    Если TG_CHAT_ID не задан, notify.notify тихо ничего не делает."""
    oname = "—"
    if owner_id:
        owner = users.get_user(conn, owner_id)
        if owner:
            oname = owner["display_name"] or owner["tg_username"] or f"#{owner_id}"
    bg.add_task(notify.notify, text + f"\nВладелец: {esc(oname)}")


def own_proposal_or_403(conn, cat, date_id: int, guest: str | None):
    """Предложение гостя, которое он может править: только его собственное."""
    d = conn.execute(
        "SELECT d.* FROM dates d JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE d.id=? AND dc.category_id=? AND d.archived_at IS NULL",
        (date_id, cat["id"]),
    ).fetchone()
    if not d:
        raise HTTPException(404, "Свидание не найдено")
    if d["origin"] != "guest" or not guest or d["guest_token"] != guest:
        raise HTTPException(403, "Это не твоё предложение")
    return d


def _batch_media(conn, date_ids: list[int]) -> dict:
    """Разом грузит ссылки/фото/видео для набора свиданий (устраняет N+1 на
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
    (_batch_media) — без запросов на каждое свидание."""
    d = dict(row)
    d["links"] = media["links"].get(row["id"], [])
    d["images"] = media["images"].get(row["id"], [])
    d["videos"] = media["videos"].get(row["id"], [])
    return d


def insert_date(conn, *, name, place, starts, ends, comment, origin, guest_token,
                owner_id, draft=0, pay_split=0, place_url=None, proposed_by=None,
                is_public=1, capacity=1) -> int:
    # Каждое свидание получает стабильную секретную ссылку /d/<share_token>
    # сразу при создании — через неё им можно поделиться, чтобы другой
    # пользователь добавил копию себе. Генерим здесь, чтобы ВСЕ пути создания
    # (админ, клон, импорт, гостевое предложение) получили токен без правок.
    share_token = new_link_token()
    cur = conn.execute(
        "INSERT INTO dates(owner_id, name, place, place_url, starts_at, ends_at, comment, "
        "origin, guest_token, proposed_by, is_draft, pay_split, share_token, is_public, "
        "capacity, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (owner_id, name, place, place_url, starts, ends, comment, origin, guest_token,
         proposed_by, draft, pay_split, share_token, 1 if is_public else 0,
         capacity, now_iso()),
    )
    return cur.lastrowid


def parse_capacity(value) -> int:
    """Пустое значение означает обычное свидание на одного участника."""
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


def validate_candidate_start(cat, starts_at: str | None) -> None:
    if cat["voting_status"] != voting.STATUS_OPEN or not starts_at:
        return
    try:
        deadline = datetime.fromisoformat(cat["voting_deadline"])
        starts = datetime.fromisoformat(starts_at)
    except (TypeError, ValueError):
        raise HTTPException(400, "Некорректная дата голосования или свидания")
    if starts <= deadline:
        raise HTTPException(400, "Свидание должно начинаться после дедлайна голосования")


def next_cat_pos(conn, category_id: int) -> int:
    """Позиция для нового свидания в категории: в конец списка."""
    return conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM date_categories WHERE category_id=?",
        (category_id,)).fetchone()[0]


def save_links(conn, date_id: int, links: list[str]) -> None:
    conn.execute("DELETE FROM date_links WHERE date_id=?", (date_id,))
    for i, u in enumerate(links):
        conn.execute("INSERT INTO date_links(date_id, url, position) VALUES(?,?,?)",
                     (date_id, u, i))


def copy_date_media_and_links(conn, src_id: int, new_id: int) -> None:
    """Переносит на новое свидание ссылки и физические копии фото/видео исходного.

    Фото/видео — отдельные файлы с новыми именами (images.copy_file); фокус кадра
    сохраняем. Битые/пропавшие файлы просто пропускаем. При ошибке удаляем уже
    скопированные файлы и пробрасываем исключение — чтобы не оставлять сирот на
    диске. Категории/брони/вопросы НЕ трогаем: это решает вызывающий (клон копирует
    категории сам, «добавить себе» — нет). Коммит — на вызывающем.

    Используется и клонированием свидания админом, и «добавить себе» по /d/<токен>.
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
        raise HTTPException(400, f"Максимум {images.MAX_IMAGES} фото у одного свидания")
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


_FOCUS_RE = re.compile(r"\s*(\d{1,3})%\s+(\d{1,3})%\s*")


def _clean_focus(raw: str | None) -> str | None:
    """Валидирует зону кадра «X% Y%» (0..100). Иначе — None (центр по умолчанию)."""
    m = _FOCUS_RE.fullmatch(raw or "")
    if not m or int(m.group(1)) > 100 or int(m.group(2)) > 100:
        return None
    return f"{int(m.group(1))}% {int(m.group(2))}%"


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
def home():
    # Домен открывает сразу страницу входа/регистрации (а /login уже сам уводит
    # залогиненного в кабинет). Декоративный лендинг убран по просьбе владельца.
    return RedirectResponse("/login", status_code=307)


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
    return FileResponse("static/apple-touch-icon.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@router.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(
        request, "public/legal.html",
        {"doc": "terms", "support": SUPPORT_CONTACT, "domain": DOMAIN})


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(
        request, "public/legal.html",
        {"doc": "privacy", "support": SUPPORT_CONTACT, "domain": DOMAIN})


@router.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(
        request, "public/about.html",
        {"support": support_link(), "projects": AUTHOR_PROJECTS, "about_text": ABOUT_TEXT})


# ---------------------------------------------------------------------------
# Страница категории
# ---------------------------------------------------------------------------

@router.get("/c/{token}", response_class=HTMLResponse)
def public_category(token: str, request: Request, conn=Depends(get_db)):
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        return templates.TemplateResponse(request, "public/gone.html", status_code=404)

    autoarchive_once(conn, category_id=cat["id"])  # мгновенный статус «прошло», скан — только по этой категории
    vote_state = _category_voting_state(conn, cat["id"])
    # close_category меняет строку категории; шаблону нужен свежий статус.
    cat = cat_by_token(conn, token)

    # Залогиненный посетитель опознаётся стабильным токеном "u<id>"; аноним —
    # только смотрит (действия в JS ведут на /login). Имя берём из профиля.
    me = viewer(request, conn)
    new_guest = False
    if me:
        guest, guest_name = ensure_guest_name(conn, me)
        claim_legacy_votes(conn, request, me, cat["id"])
        conn.commit()
    else:
        guest = None
        guest_name = None
    owner = users.get_user(conn, cat["owner_id"])

    bookings: dict[int, list] = {}
    for b in _booking_rows(conn, cat["id"]):
        bookings.setdefault(b["date_id"], []).append(b)

    # Разом грузим все свидания категории (активные + мои-черновики и архив),
    # затем их медиа и мои вопросы одним батчем — иначе был N+1 (3+ запроса на
    # каждое свидание), из-за чего «первый заход» в большую категорию тормозил.
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
    # мои вопросы по всем свиданиям сразу (guest фиксирован в рамках запроса)
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
        d["booked_by_me"] = any(e["guest_token"] == guest for e in entries)
        others = [e["name"] for e in entries if e["guest_token"] != guest]
        d["booked_others_list"] = others         # инициалы-аватарки в карточке
        d["booked_others"] = ", ".join(others)   # для обновления DOM без reload
        parts = (["ты ♥"] if d["booked_by_me"] else []) + others
        d["booked_label"] = ", ".join(parts)
        d["participants"] = [dict(e) for e in entries]
        d["vote_count"] = len(entries)
        d["capacity"] = int(d.get("capacity") or 1)
        d["is_full"] = d["vote_count"] >= d["capacity"]
        d["is_winner"] = (vote_state.status == voting.STATUS_RESOLVED
                          and vote_state.winner_date_id == d["id"])
        d["is_loser"] = (vote_state.status == voting.STATUS_RESOLVED
                         and vote_state.winner_date_id != d["id"])
        d["is_tie_leader"] = (vote_state.status == voting.STATUS_TIE
                              and d["id"] in vote_state.leader_date_ids)
        mine_entry = next((e for e in entries if e["guest_token"] == guest), None)
        d["participation_withdrawn"] = bool(
            mine_entry and mine_entry["participation_withdrawn_at"])
        d["my_questions"] = my_q.get(d["id"], [])
        d["editable"] = (not past and d["origin"] == "guest"
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

    resp = templates.TemplateResponse(request, "public/category.html", {
        "cat": cat,
        "regular": dates,
        "past": past,
        "guest": guest, "guest_name": guest_name,
        "me": me, "owner": owner,
        "owner_name": owner_name,
        "viewer_is_owner": viewer_is_owner,
        "vote_state": vote_state,
        # есть ли картинка для og:image — своя или авто (первое фото свидания).
        # Если да, мета-тег ведёт на /c/<токен>/og-image, иначе на /static/og.png.
        "og_available": bool(cat["og_image"]) or any(d.get("images") for d in dates),
        # бот для вход-модалки (Telegram Login Widget). Анониму — кнопка «Войти»
        # открывает окно с этим виджетом прямо на гостевой.
        "bot": auth_routes.BOT_USERNAME,
        "oauth": auth_routes._oauth_buttons(),
        "widget_state": (auth_routes.issue_widget_state(request)
                         if auth_routes.BOT_USERNAME and not me else None),
        "token": token,
    })
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@router.get("/c/{token}/owner-avatar")
def public_owner_avatar(token: str, conn=Depends(get_db)):
    """Аватар владельца категории — для шапки гостевой страницы. Без логина:
    гость (в т.ч. анонимный) должен видеть фото автора. Отдаём только по активной
    ссылке и только аватар владельца этой категории (чужой файл не утечёт)."""
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        raise HTTPException(404)
    row = conn.execute(
        "SELECT avatar_path FROM users WHERE id=? AND is_active=1",
        (cat["owner_id"],)).fetchone()
    fn = row["avatar_path"] if row else None
    if not fn or not images.SAFE_FILENAME.match(fn):
        raise HTTPException(404)
    path = images.UPLOAD_DIR / fn
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/c/{token}/participant-avatar/{user_id}")
def public_participant_avatar(token: str, user_id: int, conn=Depends(get_db)):
    """Аватар участника виден только внутри активной гостевой категории.

    Идентификаторы/Telegram-контакты в HTML не выводятся; маршрут проверяет,
    что пользователь действительно голосовал именно в этой категории.
    """
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        raise HTTPException(404)
    if cat["voting_status"] in {
            voting.STATUS_UNCONFIGURED, voting.STATUS_OPEN}:
        winner_only = False
    elif cat["voting_status"] == voting.STATUS_RESOLVED:
        winner_only = True
    else:
        raise HTTPException(404)
    row = conn.execute(
        "SELECT u.avatar_path FROM users u JOIN bookings b ON b.user_id=u.id "
        "WHERE u.id=? AND u.is_active=1 AND b.category_id=? "
        "AND (?=0 OR b.date_id=?) LIMIT 1",
        (user_id, cat["id"], 1 if winner_only else 0, cat["winner_date_id"]),
    ).fetchone()
    fn = row["avatar_path"] if row else None
    if not fn or not images.SAFE_FILENAME.match(fn):
        raise HTTPException(404)
    path = images.UPLOAD_DIR / fn
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/c/{token}/og-image")
def public_og_image(token: str, conn=Depends(get_db)):
    """Картинка превью ссылки (og:image) — её запрашивают краулеры мессенджеров
    (Telegram, WhatsApp) без cookie, поэтому отдаём публично по активной ссылке.
    Своя og_image категории в приоритете; иначе — коллаж из фото её свиданий
    (сетка 2×2 или 4×2); если фото нет — 404 (шаблон укажет /static/og.png)."""
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        raise HTTPException(404)
    fn = cat["og_image"]
    if fn:
        if not images.SAFE_FILENAME.match(fn):
            raise HTTPException(404)
        cropped = images.build_og_crop(fn, cat["og_focus"])
        if not cropped:
            raise HTTPException(404)
        return FileResponse(cropped, media_type="image/webp",
                            headers={"Cache-Control": "public, max-age=3600"})
    # своей картинки нет — собираем коллаж из фото активных свиданий категории
    rows = conn.execute(
        "SELECT di.filename FROM date_categories dc "
        "JOIN dates d ON d.id=dc.date_id "
        "JOIN date_images di ON di.date_id=d.id "
        "WHERE dc.category_id=? AND d.archived_at IS NULL AND d.is_draft=0 "
        "ORDER BY dc.position ASC, di.position ASC, di.id ASC LIMIT 8",
        (cat["id"],)).fetchall()
    collage = images.build_og_collage([r["filename"] for r in rows])
    if not collage:
        raise HTTPException(404)
    return FileResponse(collage, media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=3600"})


@router.get("/c/{token}/image/{filename}")
def public_image(token: str, filename: str, request: Request, conn=Depends(get_db)):
    """Фото отдаются только по активной ссылке категории. Архивные свидания
    показываются с лентой «Архив», поэтому их фото доступны;
    черновики — только их автору."""
    if not images.SAFE_FILENAME.match(filename):
        raise HTTPException(404)
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        raise HTTPException(404)
    guest = viewer_token(viewer(request, conn)) or ""
    ok = conn.execute(
        "SELECT 1 FROM date_images di "
        "JOIN dates d ON d.id=di.date_id "
        "JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE di.filename=? AND dc.category_id=? "
        "AND (d.is_draft=0 OR d.guest_token=?)",
        (filename, cat["id"], guest),
    ).fetchone()
    path = images.UPLOAD_DIR / filename
    if not ok or not path.exists():
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
    ext = filename.rsplit(".", 1)[-1]
    if ext not in VIDEO_TYPES:
        raise HTTPException(404)
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        raise HTTPException(404)
    guest = viewer_token(viewer(request, conn)) or ""
    ok = conn.execute(
        "SELECT 1 FROM date_videos dv "
        "JOIN dates d ON d.id=dv.date_id "
        "JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE dv.filename=? AND dc.category_id=? "
        "AND (d.is_draft=0 OR d.guest_token=?)",
        (filename, cat["id"], guest),
    ).fetchone()
    path = images.UPLOAD_DIR / filename
    if not ok or not path.exists():
        raise HTTPException(404)
    return ranged_file(path, VIDEO_TYPES[ext], request)


# ---------------------------------------------------------------------------
# Поделиться отдельным свиданием: /d/<share_token>
# ---------------------------------------------------------------------------
# По стабильной секретной ссылке свидания любой залогиненный пользователь может
# добавить КОПИЮ свидания себе в коллекцию (новый owner_id = он сам). Так свидание
# переиспользуется в чужих категориях, не нарушая изоляцию: оригинал и копия —
# независимые записи разных владельцев. Аноним только смотрит превью (как и на
# гостевой странице категории), действие требует входа.

def date_by_share(conn, token: str):
    return conn.execute(
        "SELECT * FROM dates WHERE share_token=?", (token,)).fetchone()


def share_action_cat(conn, d):
    """Однозначный контекст голосования по share-ссылке.

    Одно свидание может состоять в нескольких независимых опросах. Без токена
    категории нельзя угадывать, куда положить голос, поэтому разрешаем его по
    /d только при ровно одной активной категории.
    """
    rows = conn.execute(
        "SELECT c.* FROM categories c JOIN date_categories dc ON dc.category_id=c.id "
        "WHERE dc.date_id=? AND c.link_enabled=1 ORDER BY c.id LIMIT 2",
        (d["id"],)).fetchall()
    return rows[0] if len(rows) == 1 else None


@router.get("/d/{token}", response_class=HTMLResponse)
def shared_date(token: str, request: Request, conn=Depends(get_db)):
    d = date_by_share(conn, token)
    if not d:
        return templates.TemplateResponse(request, "public/gone.html", status_code=404)
    me = viewer(request, conn)
    owner = users.get_user(conn, d["owner_id"])
    owner_name = (owner["display_name"] or owner["tg_username"] or "Автор") if owner else "Автор"
    is_mine = bool(me and me["id"] == d["owner_id"])

    payload = date_payload(conn, d)
    # Гостю (не автору) показываем полноценную карточку с действиями. Контекст
    # выбора существует только при ровно одной активной категории свидания.
    act_cat = share_action_cat(conn, d)
    vote_state = _category_voting_state(conn, act_cat["id"]) if act_cat else None
    guest = guest_name = None
    if me and not is_mine:
        guest, guest_name = ensure_guest_name(conn, me)
        if act_cat:
            claim_legacy_votes(conn, request, me, act_cat["id"])
        conn.commit()

    payload["pending"] = False
    payload["past"] = bool(d["archived_at"])
    payload["editable"] = False
    payload["booked_by_me"] = False
    payload["booked_others_list"] = []
    payload["booked_others"] = ""
    payload["my_questions"] = []
    payload["participants"] = []
    payload["vote_count"] = 0
    payload["capacity"] = int(d["capacity"] or 1)
    payload["is_full"] = False
    payload["is_winner"] = False
    payload["is_loser"] = False
    payload["participation_withdrawn"] = False
    if act_cat:
        entries = _booking_rows(conn, act_cat["id"])
        mine = [e for e in entries if e["date_id"] == d["id"] and e["guest_token"] == guest]
        date_entries = [e for e in entries if e["date_id"] == d["id"]]
        others = [e["name"] for e in date_entries if e["guest_token"] != guest]
        payload["booked_by_me"] = bool(mine)
        payload["booked_others_list"] = others
        payload["booked_others"] = ", ".join(others)
        payload["participants"] = [dict(e) for e in date_entries]
        payload["vote_count"] = len(date_entries)
        payload["is_full"] = payload["vote_count"] >= payload["capacity"]
        payload["is_winner"] = bool(
            vote_state and vote_state.status == voting.STATUS_RESOLVED
            and vote_state.winner_date_id == d["id"])
        payload["is_loser"] = bool(
            vote_state and vote_state.status == voting.STATUS_RESOLVED
            and vote_state.winner_date_id != d["id"])
        payload["participation_withdrawn"] = bool(
            mine and mine[0]["participation_withdrawn_at"])
    if guest:
        # Вопросы по share-ссылке видны и без однозначной категории голосования.
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
        "owner_name": owner_name,
        "is_mine": is_mine,
        "can_act": bool(act_cat),     # совместимость старой разметки
        "can_vote": bool(act_cat) and not is_mine,
        "can_cast_vote": (bool(act_cat) and not is_mine
                          and not payload["past"] and not bool(d["is_draft"])),
        "can_question": not is_mine,
        "vote_state": vote_state,
        "bot": auth_routes.BOT_USERNAME,
        "oauth": auth_routes._oauth_buttons(),
        "widget_state": (auth_routes.issue_widget_state(request)
                         if auth_routes.BOT_USERNAME and not me else None),
    })
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def _share_act_or_410(conn, token: str):
    """Свидание по share-токену + его категория-контекст для действий.
    410, если ссылки нет; 409-логику (нет категории) обрабатывает вызывающий."""
    d = date_by_share(conn, token)
    if not d:
        raise HTTPException(404, "Свидание не найдено")
    return d, share_action_cat(conn, d)


@router.get("/d/{token}/participant-avatar/{user_id}")
def shared_participant_avatar(token: str, user_id: int, conn=Depends(get_db)):
    d, cat = _share_act_or_410(conn, token)
    if not cat:
        raise HTTPException(404)
    if not (cat["voting_status"] in {
                voting.STATUS_UNCONFIGURED, voting.STATUS_OPEN}
            or (cat["voting_status"] == voting.STATUS_RESOLVED
                and cat["winner_date_id"] == d["id"])):
        raise HTTPException(404)
    row = conn.execute(
        "SELECT u.avatar_path FROM users u JOIN bookings b ON b.user_id=u.id "
        "WHERE u.id=? AND u.is_active=1 AND b.date_id=? AND b.category_id=? LIMIT 1",
        (user_id, d["id"], cat["id"]),
    ).fetchone()
    fn = row["avatar_path"] if row else None
    if not fn or not images.SAFE_FILENAME.match(fn):
        raise HTTPException(404)
    path = images.UPLOAD_DIR / fn
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.post("/d/{token}/book")
def shared_date_book(token: str, request: Request, bg: BackgroundTasks,
                     conn=Depends(get_db)):
    """Выбор по share-ссылке в единственной активной категории свидания."""
    d, cat = _share_act_or_410(conn, token)
    if not cat:
        raise HTTPException(400, "Это свидание пока нельзя выбрать")
    user = acting_user(request, conn)
    if user["id"] == d["owner_id"]:
        raise HTTPException(400, "Это твоё свидание")
    guest, name = ensure_guest_name(conn, user)
    claim_legacy_votes(conn, request, user, cat["id"])
    guest_throttle("book", guest, request)

    mine = conn.execute(
        "SELECT id FROM bookings WHERE date_id=? AND category_id=? AND guest_token=?",
        (d["id"], cat["id"], guest)).fetchone()
    try:
        if mine:
            result = voting.remove_vote(conn, cat["id"], d["id"], user["id"])
            booked = False
        else:
            result = voting.cast_vote(conn, cat["id"], d["id"], user["id"])
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
    notify_owner(bg, conn, d["owner_id"], card)
    notify_admin(bg, conn, d["owner_id"], card)
    conn.commit()
    return JSONResponse({"ok": True, "booked": booked, "name": name,
                         "choices": list(result.current_date_ids)})


@router.post("/d/{token}/withdraw")
def shared_date_withdraw(token: str, request: Request, bg: BackgroundTasks,
                         conn=Depends(get_db)):
    d, cat = _share_act_or_410(conn, token)
    if not cat:
        raise HTTPException(409, "У свидания нет однозначного контекста голосования")
    state = voting.get_category_state(conn, cat["id"])
    if state.status != voting.STATUS_RESOLVED or state.winner_date_id != d["id"]:
        raise HTTPException(409, "Отказаться можно только по ссылке победившего свидания")
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
    guest_throttle("withdraw", guest, request)
    try:
        result = voting.withdraw_participation(conn, cat["id"], user["id"])
    except voting.VotingError as exc:
        raise _voting_http_error(exc)
    voting_events.cancel_user_winner_reminders(conn, cat["id"], user["id"])
    if not result.already_withdrawn:
        voting_events.queue_participant_withdrawal(
            conn,
            booking_id=result.booking_id,
            owner_id=cat["owner_id"],
            participant_name=name,
            category_name=cat["name"],
            date_name=d["name"],
            category_id=cat["id"],
        )
    conn.commit()
    if not result.already_withdrawn:
        card = notify.card("↩️ Участник отказался от свидания",
                           f"«{esc(d['name'])}» · {esc(cat['name'])}",
                           f"Кто: {esc(name)}", "Итог голосования не изменился.")
        notify_admin(bg, conn, cat["owner_id"], card)
    return JSONResponse({"ok": True, "withdrawn": True,
                         "already_withdrawn": result.already_withdrawn})


@router.post("/d/{token}/question")
def shared_date_question(token: str, request: Request, bg: BackgroundTasks,
                         text: str = Form(...), conn=Depends(get_db)):
    """Вопрос по свиданию с share-ссылки. category_id берём из активной категории
    (колонка nullable — но контекст полезен автору)."""
    d, cat = _share_act_or_410(conn, token)
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
    guest_throttle("question", guest, request)
    text = clean_text(text, 2000, "Вопрос", required=True)
    conn.execute(
        "INSERT INTO questions(date_id, category_id, guest_token, user_id, text, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (d["id"], cat["id"] if cat else None, guest, user["id"], text, now_iso()))
    conn.commit()
    card = notify.card("❓ Новый вопрос", f"«{esc(d['name'])}»",
                       f"Кто: {esc(name)}", f"\n{esc(text)}",
                       f"\n{BASE_URL}/admin/questions")
    notify_owner(bg, conn, d["owner_id"], card)
    notify_admin(bg, conn, d["owner_id"], card)
    return JSONResponse({"ok": True})


@router.post("/d/{token}/suggest_time")
def shared_date_suggest(token: str, request: Request, bg: BackgroundTasks,
                        starts_at: str = Form(""), ends_at: str = Form(""),
                        conn=Depends(get_db)):
    """Гость предлагает время для свидания без даты по share-ссылке."""
    d, cat = _share_act_or_410(conn, token)
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
    guest_throttle("question", guest, request)
    if d["starts_at"]:
        raise HTTPException(400, "У этого свидания уже назначено время")
    starts, ends = normalize_period(parse_dt_local(starts_at), parse_dt_local(ends_at))
    if not starts:
        raise HTTPException(400, "Выбери хотя бы дату и время начала")
    text = "📅 Предлагаю назначить: " + fmt_when(starts, ends)
    conn.execute(
        "INSERT INTO questions(date_id, category_id, guest_token, user_id, text, "
        "suggest_starts, suggest_ends, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (d["id"], cat["id"] if cat else None, guest, user["id"], text, starts, ends, now_iso()))
    conn.commit()
    card = notify.card("📅 Предложено время", f"«{esc(d['name'])}»",
                       f"Кто: {esc(name)}", f"Когда: {fmt_when(starts, ends)} (мск)",
                       f"\n{BASE_URL}/admin/questions")
    notify_owner(bg, conn, d["owner_id"], card)
    notify_admin(bg, conn, d["owner_id"], card)
    return JSONResponse({"ok": True})


@router.get("/d/{token}/image/{filename}")
def shared_date_image(token: str, filename: str, conn=Depends(get_db)):
    """Фото свидания по share-ссылке. Отдаём только фото ЭТОГО свидания —
    чужой файл по прямой ссылке не утечёт."""
    if not images.SAFE_FILENAME.match(filename):
        raise HTTPException(404)
    d = date_by_share(conn, token)
    if not d:
        raise HTTPException(404)
    ok = conn.execute(
        "SELECT 1 FROM date_images WHERE date_id=? AND filename=?",
        (d["id"], filename)).fetchone()
    path = images.UPLOAD_DIR / filename
    if not ok or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=604800, immutable"})


@router.get("/d/{token}/video/{filename}")
def shared_date_video(token: str, filename: str, request: Request, conn=Depends(get_db)):
    """Видео свидания по share-ссылке — те же правила, что и у фото, + Range."""
    if not images.SAFE_FILENAME.match(filename):
        raise HTTPException(404)
    ext = filename.rsplit(".", 1)[-1]
    if ext not in VIDEO_TYPES:
        raise HTTPException(404)
    d = date_by_share(conn, token)
    if not d:
        raise HTTPException(404)
    ok = conn.execute(
        "SELECT 1 FROM date_videos WHERE date_id=? AND filename=?",
        (d["id"], filename)).fetchone()
    path = images.UPLOAD_DIR / filename
    if not ok or not path.exists():
        raise HTTPException(404)
    return ranged_file(path, VIDEO_TYPES[ext], request)


@router.post("/d/{token}/add")
def shared_date_add(token: str, request: Request, conn=Depends(get_db)):
    """Добавить копию свидания себе в коллекцию (нужен вход).

    Копия — отдельное активное свидание получателя со СВОИМ свежим share_token
    (его сгенерит insert_date). Категории/брони/вопросы не переносятся. Файлы
    фото/видео — физические копии (новые имена). Свою же ссылку добавлять незачем —
    отбиваем, чтобы не плодить дубли у себя."""
    d = date_by_share(conn, token)
    if not d:
        raise HTTPException(404, "Свидание не найдено")
    user = acting_user(request, conn)
    guest_throttle("dadd", viewer_token(user), request)
    if user["id"] == d["owner_id"]:
        raise HTTPException(400, "Это твоё свидание — оно уже в твоей коллекции")

    # квота получателя: активные (не архивные) свидания не должны превышать лимит
    used = conn.execute(
        "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NULL",
        (user["id"],)).fetchone()[0]
    if used >= user["date_limit"]:
        raise HTTPException(400, f"Достигнут лимит {user['date_limit']} свиданий — "
                                 "удали или заархивируй лишние и попробуй снова")

    new_id = insert_date(
        conn, name=d["name"], place=d["place"], starts=d["starts_at"], ends=d["ends_at"],
        comment=d["comment"], origin="admin", guest_token=None, owner_id=user["id"],
        draft=0, pay_split=d["pay_split"], place_url=d["place_url"],
        capacity=d["capacity"])
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
                 "Свидание добавлено в твою коллекцию ♥ Привяжи его к своим категориям")


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
def public_ics(token: str, date_id: int, conn=Depends(get_db)):
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        raise HTTPException(404)
    d = date_in_category(conn, cat["id"], date_id)
    if not d or not d["starts_at"]:
        raise HTTPException(404, "У этого свидания нет даты")
    return _ics_response(conn, d, f"date-{d['id']}-{cat['id']}@{DOMAIN}")


@router.get("/d/{token}/ics")
def shared_ics(token: str, conn=Depends(get_db)):
    """Календарь для свидания по share-ссылке."""
    d = date_by_share(conn, token)
    if not d or not d["starts_at"]:
        raise HTTPException(404, "У этого свидания нет даты")
    return _ics_response(conn, d, f"date-{d['id']}-share@{DOMAIN}")


def _ics_response(conn, d, uid: str) -> Response:
    """Собирает .ics-файл для одного свидания (общий код для /c и /d)."""
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
                             f'attachment; filename="date-{d["id"]}.ics"'})


# ---------------------------------------------------------------------------
# Действия гостя (требуют входа в аккаунт)
# ---------------------------------------------------------------------------

@router.post("/c/{token}/book")
def public_book(token: str, request: Request, bg: BackgroundTasks,
                date_id: int = Form(...), conn=Depends(get_db)):
    """Голос за вариант; режим single/multiple задаёт владелец категории."""
    cat = active_cat_or_410(conn, token)
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
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
        raise HTTPException(404, "Свидание не найдено")

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
    notify_owner(bg, conn, cat["owner_id"], card)
    notify_admin(bg, conn, cat["owner_id"], card)
    conn.commit()
    return JSONResponse({"ok": True, "booked": booked, "name": name,
                         "choices": list(result.current_date_ids)})


@router.post("/c/{token}/withdraw")
def public_withdraw(token: str, request: Request, bg: BackgroundTasks,
                    conn=Depends(get_db)):
    """Участник победившего варианта может отказаться без пересчёта итога."""
    cat = active_cat_or_410(conn, token)
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
    title = winner["name"] if winner else "Победившее свидание"
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
        card = notify.card("↩️ Участник отказался от свидания",
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
    """Гость предлагает время для свидания без даты.

    Хранится как обычный вопрос: появляется у админа во «Вопросах»
    с кнопками «Принять/Отказаться», автор видит его (и ответ) под карточкой.
    """
    cat = active_cat_or_410(conn, token)
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
    guest_throttle("question", guest, request)

    d = date_in_category(conn, cat["id"], date_id)
    if not d:
        raise HTTPException(404, "Свидание не найдено")
    if d["starts_at"]:
        raise HTTPException(400, "У этого свидания уже назначено время")
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
        f"Когда: {fmt_when(starts, ends)} (мск)",
        f"\n{BASE_URL}/admin/questions")
    notify_owner(bg, conn, cat["owner_id"], card)
    notify_admin(bg, conn, cat["owner_id"], card)
    return JSONResponse({"ok": True})


@router.post("/c/{token}/question")
def public_question(token: str, request: Request, bg: BackgroundTasks,
                    date_id: int = Form(...), text: str = Form(...),
                    conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token)
    user = acting_user(request, conn)
    guest, name = ensure_guest_name(conn, user)
    guest_throttle("question", guest, request)

    d = date_in_category(conn, cat["id"], date_id)
    if not d:
        raise HTTPException(404, "Свидание не найдено")
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
        f"\n{esc(text)}",
        f"\n{BASE_URL}/admin/questions")
    notify_owner(bg, conn, cat["owner_id"], card)
    notify_admin(bg, conn, cat["owner_id"], card)
    return JSONResponse({"ok": True})


@router.post("/c/{token}/propose")
def public_propose(token: str, request: Request, bg: BackgroundTasks,
                   name: str = Form(...), place: str = Form(""),
                   starts_at: str = Form(""), ends_at: str = Form(""),
                   links: str = Form(""), comment: str = Form(""),
                   pay: str = Form("0"),
                   capacity: str = Form("1"),
                   photos: list[UploadFile] = File(default=[], alias="images"),
                   video: UploadFile | None = File(None),
                   conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token)
    conn.execute("UPDATE categories SET id=id WHERE id=?", (cat["id"],))
    cat = active_cat_or_410(conn, token)
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
    if video and video.filename:
        try:
            vfn = images.save_video(video)
        except ValueError as e:
            conn.rollback()
            for fn in saved_photos:        # фото уже на диске — не оставляем сирот
                images.delete_file(fn)
            raise HTTPException(400, str(e))
        conn.execute("INSERT INTO date_videos(date_id, filename) VALUES(?,?)",
                     (date_id, vfn))
    conn.commit()
    if needs_resolve:
        bg.add_task(places.resolve_into_db, date_id, place_url)

    msg = notify.card(
        "💡 Новое предложение" + (" (на модерации)" if moderated else ""),
        f"«{esc(name)}» · {esc(cat['name'])}",
        f"От: {esc(author)}",
        f"📍 {esc(place)}" if place else "",
        f"🕐 {fmt_when(starts, ends)} (мск)" if fmt_when(starts, ends) else "",
        f"\n{BASE_URL}/admin/dates/{date_id}/edit")
    notify_owner(bg, conn, cat["owner_id"], msg)
    notify_admin(bg, conn, cat["owner_id"], msg)

    return JSONResponse({"ok": True, "id": date_id, "moderated": moderated})


@router.post("/c/{token}/propose/{date_id}/edit")
def public_propose_edit(token: str, date_id: int, request: Request, bg: BackgroundTasks,
                        name: str = Form(...), place: str = Form(""),
                        starts_at: str = Form(""), ends_at: str = Form(""),
                        links: str = Form(""), comment: str = Form(""),
                        keep_order: str = Form(""),
                        remove_image: list[int] = Form(default=[]),
                        remove_video: list[int] = Form(default=[]),
                        pay: str = Form("0"),
                        capacity: str = Form("1"),
                        photos: list[UploadFile] = File(default=[], alias="images"),
                        video: UploadFile | None = File(None),
                        conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token)
    ensure_category_editable(conn, cat)
    user = acting_user(request, conn)
    guest, author = ensure_guest_name(conn, user)
    d = own_proposal_or_403(conn, cat, date_id, guest)
    conn.execute("UPDATE dates SET id=id WHERE id=?", (date_id,))
    cat = active_cat_or_410(conn, token)
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
        raise HTTPException(400, f"Максимум {images.MAX_IMAGES} фото у одного свидания")
    try:
        saved = images.save_batch(new_files)
    except ValueError as e:
        raise HTTPException(400, str(e))

    for r in to_remove:
        conn.execute("DELETE FROM date_images WHERE id=?", (r["id"],))

    # порядок оставшихся фото: как расставил гость (drag-and-drop), затем новые
    remaining = [r["id"] for r in existing if r["id"] not in remove_set]
    wanted = [int(x) for x in keep_order.split(",") if x.strip().isdigit()]
    ordered = [i for i in wanted if i in remaining] + \
              [i for i in remaining if i not in wanted]
    pos = 0
    for iid in ordered:
        conn.execute("UPDATE date_images SET position=? WHERE id=?", (pos, iid))
        pos += 1
    for fn in saved:
        conn.execute("INSERT INTO date_images(date_id, filename, position) VALUES(?,?,?)",
                     (date_id, fn, pos))
        pos += 1

    # видео: новое заменяет старое; явное удаление — без замены
    vids = conn.execute(
        "SELECT id, filename FROM date_videos WHERE date_id=?", (date_id,)).fetchall()
    vid_remove = [v for v in vids if v["id"] in set(remove_video)]
    new_video_fn = None
    if video and video.filename:
        try:
            new_video_fn = images.save_video(video)
        except ValueError as e:
            for fn in saved:           # новые фото уже на диске — убираем сирот
                images.delete_file(fn)
            raise HTTPException(400, str(e))
        vid_remove = vids                      # замена: старые уходят
    for v in vid_remove:
        conn.execute("DELETE FROM date_videos WHERE id=?", (v["id"],))
    if new_video_fn:
        conn.execute("INSERT INTO date_videos(date_id, filename) VALUES(?,?)",
                     (date_id, new_video_fn))

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

    notify_owner(bg, conn, cat["owner_id"], notify.card(
        "✏️ Предложение изменено",
        f"«{esc(name)}» · {esc(cat['name'])}",
        f"Автор: {esc(author)}",
        f"\n{BASE_URL}/admin/dates/{date_id}/edit"))
    notify_admin(bg, conn, cat["owner_id"], notify.card(
        "✏️ Предложение изменено",
        f"«{esc(name)}» · {esc(cat['name'])}",
        f"Автор: {esc(author)}",
        f"\n{BASE_URL}/admin/dates/{date_id}/edit"))
    return JSONResponse({"ok": True})


@router.post("/c/{token}/propose/{date_id}/delete")
def public_propose_delete(token: str, date_id: int, request: Request, bg: BackgroundTasks,
                          conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token)
    ensure_category_editable(conn, cat)
    user = acting_user(request, conn)
    guest, author = ensure_guest_name(conn, user)
    d = own_proposal_or_403(conn, cat, date_id, guest)
    conn.execute("UPDATE dates SET id=id WHERE id=?", (date_id,))
    cat = active_cat_or_410(conn, token)
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

    notify_owner(bg, conn, cat["owner_id"], notify.card(
        "🗑 Предложение удалено",
        f"«{esc(d['name'])}» · {esc(cat['name'])}",
        f"Автор: {esc(author)}"))
    notify_admin(bg, conn, cat["owner_id"], notify.card(
        "🗑 Предложение удалено",
        f"«{esc(d['name'])}» · {esc(cat['name'])}",
        f"Автор: {esc(author)}"))
    return JSONResponse({"ok": True})


@router.post("/c/{token}/report")
def public_report(token: str, request: Request, bg: BackgroundTasks,
                  target_type: str = Form(...), target_id: int = Form(...),
                  reason: str = Form(""), conn=Depends(get_db)):
    """Жалоба гостя на свидание или саму категорию. Контент существует и виден
    по этой ссылке — иначе 404 (нельзя слать жалобы на чужой/скрытый id)."""
    cat = active_cat_or_410(conn, token)
    user = acting_user(request, conn)
    guest = viewer_token(user)
    guest_throttle("report", guest, request)

    if target_type not in ("date", "category"):
        raise HTTPException(400, "Некорректная цель жалобы")
    if target_type == "category":
        target_id = cat["id"]                 # жалоба на категорию — только на эту
        label = cat["name"]
    else:
        d = date_in_category(conn, cat["id"], target_id)
        if not d:
            raise HTTPException(404, "Свидание не найдено")
        label = d["name"]

    # Защита от дублей: один гость — одна открытая жалоба на объект.
    dup = conn.execute(
        "SELECT 1 FROM reports WHERE target_type=? AND target_id=? AND reporter=? "
        "AND status='open'", (target_type, target_id, guest)).fetchone()
    if not dup:
        reason = clean_text(reason, 1000, "Причина") or None
        conn.execute(
            "INSERT INTO reports(target_type, target_id, reporter, reason, "
            "status, created_at) VALUES(?,?,?,?,'open',?)",
            (target_type, target_id, guest, reason, now_iso()))
        conn.commit()
        bg.add_task(notify.notify, notify.card(
            "🚩 Жалоба",
            f"На {('категорию' if target_type=='category' else 'свидание')}: «{esc(label)}»",
            f"Категория: {esc(cat['name'])}",
            f"Причина: {esc(reason or 'без описания')}",
            f"\n{BASE_URL}/operator/reports"))
    return JSONResponse({"ok": True})
