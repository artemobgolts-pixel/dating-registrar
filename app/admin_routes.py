"""Админка: вход, дашборд, категории, свидания, вопросы, экспорт.

Доступ — сессия (SessionMiddleware) + CSRF-токен на каждый POST.
"""

import csv
import io
import json
import logging
import os
import tempfile
import zipfile
from datetime import datetime

import segno

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Request, UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from urllib.parse import urlencode

import backup
import db
import images
import notify
import places
import public_routes
import settings as app_settings
import voting
import voting_events
from config import BASE_URL, SUPPORT_CONTACT, OAUTH_PROVIDERS, OAUTH_LABELS
from guests import gname
from helpers import (clean_text, new_link_token, normalize_period, now_iso, now_naive,
                     parse_birth_date, parse_dt_local, parse_links, pay_label)
from fastapi.responses import JSONResponse
from ownership import get_owned_category, get_owned_date
from public_routes import (add_photos, copy_date_media_and_links, insert_date,
                           next_cat_pos, notify_admin, notify_user, parse_capacity, ranged_file,
                           save_links, VIDEO_TYPES)
from ratelimit import user_throttle
from tasks import autoarchive_once
from users import current_user
from web import get_db, redir, templates


# ---------------------------------------------------------------------------
# Доступ к кабинету
# ---------------------------------------------------------------------------
# Вход (через Telegram-бота) живёт в auth_routes. Здесь — только защищённая
# часть: каждый запрос проходит через current_user (сессия + активный юзер +
# CSRF на POST) и видит данные ТОЛЬКО своего владельца.

router = APIRouter(prefix="/admin", dependencies=[Depends(current_user)])
log = logging.getLogger("admin")


def _raise_voting_http(exc: voting.VotingError) -> None:
    """Переводит ожидаемую доменную ошибку голосования в понятный HTTP-ответ."""
    raise HTTPException(exc.status_code, exc.message) from exc


def _category_voting_is_closed(cat) -> bool:
    """Считает состав зафиксированным и при уже наступившем дедлайне.

    Фоновое закрытие может выполниться на несколько секунд позже дедлайна, но
    это окно не должно позволять владельцу менять набор кандидатов.
    """
    if cat["closed_at"] is not None or cat["voting_status"] in voting.CLOSED_STATUSES:
        return True
    deadline = cat["voting_deadline"]
    if cat["voting_status"] == voting.STATUS_OPEN and deadline:
        try:
            return now_naive() >= datetime.fromisoformat(deadline)
        except (TypeError, ValueError):
            return True
    return False


def _require_category_composition_mutable(conn, cat):
    """Сериализует изменение состава с голосами и закрытием по дедлайну."""
    cursor = conn.execute("UPDATE categories SET id=id WHERE id=?", (cat["id"],))
    if cursor.rowcount == 0:
        raise HTTPException(404, "Категория не найдена")
    fresh = conn.execute("SELECT * FROM categories WHERE id=?", (cat["id"],)).fetchone()
    if _category_voting_is_closed(fresh):
        raise HTTPException(
            409,
            "Голосование уже завершено: состав и порядок свиданий зафиксированы",
        )
    return fresh


def _validate_start_after_open_deadlines(conn, category_ids: list[int] | set[int],
                                         starts_at: str | None) -> None:
    """Не даёт поставить старт не позже дедлайна привязанного голосования."""
    if not starts_at or not category_ids:
        return
    try:
        starts = datetime.fromisoformat(starts_at)
    except (TypeError, ValueError):
        raise HTTPException(400, "Неверный формат даты/времени")
    placeholders = ",".join("?" * len(category_ids))
    for cat in conn.execute(
        f"SELECT id, name, voting_deadline FROM categories "
        f"WHERE id IN ({placeholders}) AND voting_status='open' "
        "AND voting_deadline IS NOT NULL",
        tuple(category_ids),
    ):
        try:
            deadline = datetime.fromisoformat(cat["voting_deadline"])
        except (TypeError, ValueError):
            raise HTTPException(409, f"У категории «{cat['name']}» некорректный дедлайн")
        if starts <= deadline:
            raise HTTPException(
                400,
                f"Начало свидания должно быть позже дедлайна категории «{cat['name']}»",
            )


def actx(request: Request, conn, **extra) -> dict:
    user = request.state.user
    ctx = {
        "request": request,
        "user": user,
        "unread": conn.execute(
            "SELECT COUNT(*) FROM questions q JOIN dates d ON d.id=q.date_id "
            "WHERE d.owner_id=? AND q.is_read=0", (user["id"],)).fetchone()[0],
        "csrf": request.session.get("csrf", ""),
    }
    ctx.update(extra)
    return ctx


@router.post("/logout")
def logout(request: Request):
    """Выход только по POST с CSRF: logout по GET можно навязать ссылкой."""
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/profile", response_class=HTMLResponse)
def profile_form(request: Request, conn=Depends(get_db)):
    # какие соцсети уже привязаны — чтобы показать статус в блоке привязки
    linked = {r["provider"] for r in conn.execute(
        "SELECT provider FROM oauth_accounts WHERE user_id=?",
        (request.state.user["id"],))}
    providers = [{"slug": s, "label": lbl, "linked": s in linked,
                  "enabled": bool(OAUTH_PROVIDERS[s][0])}
                 for s, lbl in OAUTH_LABELS.items()]
    first_category = conn.execute(
        "SELECT id FROM categories WHERE owner_id=? ORDER BY created_at LIMIT 1",
        (request.state.user["id"],),
    ).fetchone()
    return templates.TemplateResponse(
        request, "admin/profile.html",
        actx(request, conn, active="profile", oauth_providers=providers,
             first_category_id=first_category["id"] if first_category else None))


@router.post("/profile")
def profile_save(request: Request,
                 display_name: str = Form(""),
                 birth_date: str = Form(""),
                 gender: str = Form(""),
                 cursor_effects: str | None = Form(None),
                 avatar: UploadFile | None = File(None),
                 conn=Depends(get_db)):
    """Сохраняет профиль владельца. Аватар — опционально; старый файл сносим
    только после успешной записи нового имени в базу."""
    uid = request.state.user["id"]
    name = clean_text(display_name, 80, "Имя", required=True)
    bdate = parse_birth_date(birth_date)
    g = gender if gender in ("m", "f") else None

    new_avatar = None
    if avatar is not None and (avatar.filename or "").strip():
        try:
            new_avatar = images.save_upload(avatar)
        except ValueError as e:
            raise HTTPException(400, str(e))

    old_avatar = request.state.user["avatar_path"]
    effects = 1 if cursor_effects is not None else 0
    if new_avatar:
        conn.execute(
            "UPDATE users SET display_name=?, birth_date=?, gender=?, avatar_path=?, "
            "cursor_effects=? WHERE id=?", (name, bdate, g, new_avatar, effects, uid))
    else:
        conn.execute(
            "UPDATE users SET display_name=?, birth_date=?, gender=?, cursor_effects=? "
            "WHERE id=?", (name, bdate, g, effects, uid))
    conn.commit()
    if new_avatar and old_avatar:        # старый аватар — только после коммита
        images.delete_file(old_avatar)
    return redir("/admin/profile", "Профиль сохранён ♥")


@router.post("/profile/oauth/{provider}/unlink")
def profile_oauth_unlink(provider: str, request: Request, conn=Depends(get_db)):
    """Отвязать соцсеть от аккаунта. Не даём отвязать последний способ входа
    у OAuth-аккаунта без Telegram — иначе человек потеряет доступ к аккаунту."""
    uid = request.state.user["id"]
    if provider not in OAUTH_LABELS:
        raise HTTPException(404)
    has_tg = request.state.user["telegram_id"] is not None
    links = conn.execute(
        "SELECT COUNT(*) FROM oauth_accounts WHERE user_id=?", (uid,)).fetchone()[0]
    if not has_tg and links <= 1:
        return redir("/admin/profile",
                     "Нельзя отвязать единственный способ входа — сначала привяжи другой.")
    conn.execute("DELETE FROM oauth_accounts WHERE user_id=? AND provider=?",
                 (uid, provider))
    conn.commit()
    return redir("/admin/profile", f"{OAUTH_LABELS[provider]} отвязан")


@router.post("/profile/avatar/delete")
def profile_avatar_delete(request: Request, conn=Depends(get_db)):
    uid = request.state.user["id"]
    old = request.state.user["avatar_path"]
    conn.execute("UPDATE users SET avatar_path=NULL WHERE id=?", (uid,))
    conn.commit()
    if old:
        images.delete_file(old)
    return redir("/admin/profile", "Фото профиля удалено")


@router.get("/avatar/{filename}")
def profile_avatar(filename: str, request: Request):
    """Отдаёт аватар текущего пользователя. Гейт: filename должен совпадать с
    его собственным avatar_path — чужой аватар по прямой ссылке не отдаём."""
    if not images.SAFE_FILENAME.match(filename) \
            or filename != request.state.user["avatar_path"]:
        raise HTTPException(404)
    path = images.UPLOAD_DIR / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=300"})


GNAME_SQL = "COALESCE(g.name, 'Человек #' || substr(COALESCE({t}, '??????'), 1, 6))"


# Сколько карточек комьюнити-ленты отдаём за одну «страницу» бесконечного скролла.
COMMUNITY_PAGE = 12


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn=Depends(get_db)):
    autoarchive_once(conn, owner_id=request.state.user["id"])
    uid = request.state.user["id"]
    stats = {
        "cats": conn.execute(
            "SELECT COUNT(*) FROM categories WHERE owner_id=?", (uid,)).fetchone()[0],
        "active": conn.execute(
            "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NULL "
            "AND is_draft=0", (uid,)).fetchone()[0],
        "drafts": conn.execute(
            "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NULL "
            "AND is_draft=1", (uid,)).fetchone()[0],
        "archived": conn.execute(
            "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NOT NULL",
            (uid,)).fetchone()[0],
        # выборы считаем только по активным (не архивным) свиданиям владельца
        "bookings": conn.execute(
            "SELECT COUNT(*) FROM bookings b JOIN dates d ON d.id=b.date_id "
            "WHERE d.owner_id=? AND d.archived_at IS NULL", (uid,)).fetchone()[0],
        "unread_q": conn.execute(
            "SELECT COUNT(*) FROM questions q JOIN dates d ON d.id=q.date_id "
            "WHERE d.owner_id=? AND q.is_read=0", (uid,)).fetchone()[0],
    }

    # Блок «Поделиться»: категории владельца с включённой секретной ссылкой.
    # Для выбранной (или первой) рисуем QR прямо на сервере — инлайновый SVG,
    # под CSP не нужен ни внешний скрипт, ни data:-картинка.
    share_cats = conn.execute(
        "SELECT id, name, link_token, og_title, og_desc, og_image FROM categories "
        "WHERE owner_id=? AND link_enabled=1 AND link_token IS NOT NULL "
        "ORDER BY created_at DESC", (uid,)).fetchall()
    sel = request.query_params.get("share")
    share = next((c for c in share_cats if str(c["id"]) == sel), None) \
        or (share_cats[0] if share_cats else None)
    share_url = qr_svg = None
    share_has_og = False
    if share:
        share_url = f"{BASE_URL}/c/{share['link_token']}"
        qr_svg = _qr_svg(share_url)
        # превью ссылки: своя картинка ИЛИ коллаж из фото активных свиданий
        share_has_og = bool(share["og_image"]) or conn.execute(
            "SELECT 1 FROM date_categories dc JOIN dates d ON d.id=dc.date_id "
            "JOIN date_images di ON di.date_id=d.id "
            "WHERE dc.category_id=? AND d.archived_at IS NULL AND d.is_draft=0 LIMIT 1",
            (share["id"],)).fetchone() is not None

    return templates.TemplateResponse(
        request, "admin/dashboard.html",
        actx(request, conn, active="dash", stats=stats,
             share_cats=share_cats, share=share, share_url=share_url, qr_svg=qr_svg,
             share_has_og=share_has_og))


def _qr_svg(data: str) -> str:
    """QR-код ссылки как инлайновый SVG (без внешних запросов и без PIL).

    Прозрачный фон, цвет — роза палитры; omitsize сохраняет viewBox, поэтому
    SVG масштабируется под контейнер. Под нашу CSP инлайновый SVG безопасен.
    """
    buf = io.BytesIO()
    segno.make(data, error="m").save(
        buf, kind="svg", scale=4, border=2,
        dark="#8f4a58", svgclass=None, omitsize=True, xmldecl=False)
    return buf.getvalue().decode("utf-8")


# ---------------------------------------------------------------------------
# Лента свиданий-комьюнити (на главной вместо «Последних действий»)
# ---------------------------------------------------------------------------
# Показываем чужие публичные активные свидания: карточка = фото + название +
# когда/место/комментарий + пилюля владельца (→ его профиль). Категорию и
# модификатор оплаты НЕ показываем (решение владельца). Бесконечный скролл —
# keyset-пагинацией по d.id DESC (курсор = id последней карточки).

def _community_cards(conn, viewer_id: int, cursor: int | None):
    """Возвращает (dates, next_cursor) для ленты. dates — свидания с медиа и
    именем/аватаром владельца. Курсор — id, строго меньше которого берём дальше."""
    where = ("d.is_public=1 AND d.is_draft=0 AND d.archived_at IS NULL "
             "AND d.owner_id<>?")
    params: list = [viewer_id]
    if cursor:
        where += " AND d.id<?"
        params.append(cursor)
    rows = conn.execute(
        f"SELECT d.*, u.display_name AS owner_name, u.tg_username AS owner_username, "
        f"u.avatar_path AS owner_avatar "
        f"FROM dates d JOIN users u ON u.id=d.owner_id "
        f"WHERE {where} ORDER BY d.id DESC LIMIT ?",
        (*params, COMMUNITY_PAGE + 1)).fetchall()
    has_more = len(rows) > COMMUNITY_PAGE
    rows = rows[:COMMUNITY_PAGE]
    media = public_routes._batch_media(conn, [r["id"] for r in rows])
    cards = []
    for r in rows:
        d = public_routes.date_payload_from(r, media)
        d["owner_display"] = (r["owner_name"] or r["owner_username"]
                              or f"Человек #{r['owner_id']}")
        cards.append(d)
    next_cursor = rows[-1]["id"] if (rows and has_more) else None
    return cards, next_cursor


@router.get("/community", response_class=HTMLResponse)
def community_feed(request: Request, conn=Depends(get_db)):
    """HTML-фрагмент со следующей страницей ленты (для бесконечного скролла).
    Возвращает только карточки + маркер курсора — фронт дописывает их в ленту."""
    cur = request.query_params.get("cursor")
    cursor = int(cur) if cur and cur.isdigit() else None
    cards, next_cursor = _community_cards(conn, request.state.user["id"], cursor)
    return templates.TemplateResponse(
        request, "admin/_community_cards.html",
        {"request": request, "cards": cards, "next_cursor": next_cursor})


@router.get("/community/date/{did}", response_class=HTMLResponse)
def community_widget(did: int, request: Request, conn=Depends(get_db)):
    """Мини-виджет одного свидания из ленты (открывается в модалке). Только
    публичное активное чужое свидание; иначе 404."""
    r = conn.execute(
        "SELECT d.*, u.display_name AS owner_name, u.tg_username AS owner_username, "
        "u.avatar_path AS owner_avatar "
        "FROM dates d JOIN users u ON u.id=d.owner_id "
        "WHERE d.id=? AND d.is_public=1 AND d.is_draft=0 AND d.archived_at IS NULL",
        (did,)).fetchone()
    if not r:
        raise HTTPException(404, "Свидание не найдено")
    media = public_routes._batch_media(conn, [did])
    d = public_routes.date_payload_from(r, media)
    d["owner_display"] = (r["owner_name"] or r["owner_username"]
                          or f"Человек #{r['owner_id']}")
    return templates.TemplateResponse(
        request, "admin/_community_widget.html",
        {"request": request, "d": d, "is_mine": r["owner_id"] == request.state.user["id"]})


@router.get("/uploads/{filename}")
def admin_image(filename: str, request: Request, conn=Depends(get_db)):
    if not images.SAFE_FILENAME.match(filename):
        raise HTTPException(404)
    # файл виден, только если принадлежит владельцу: фото/видео свидания или
    # картинка превью его категории. Оператор (правит чужое) видит любой файл.
    uid = request.state.user["id"]
    if request.state.user["is_operator"]:
        owns = True
    else:
        owns = conn.execute(
            "SELECT 1 FROM date_images di JOIN dates d ON d.id=di.date_id "
            "WHERE di.filename=? AND d.owner_id=? "
            "UNION ALL "
            "SELECT 1 FROM date_videos dv JOIN dates d ON d.id=dv.date_id "
            "WHERE dv.filename=? AND d.owner_id=? "
            "UNION ALL "
            "SELECT 1 FROM categories WHERE og_image=? AND owner_id=? LIMIT 1",
            (filename, uid, filename, uid, filename, uid)).fetchone()
    if not owns:
        raise HTTPException(404)
    path = images.UPLOAD_DIR / filename
    if not path.exists():
        raise HTTPException(404)
    ext = filename.rsplit(".", 1)[-1]
    if ext in VIDEO_TYPES:
        return ranged_file(path, VIDEO_TYPES[ext], request)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=3600"})


# ----- Экспорт ---------------------------------------------------------------

def _require_operator(request: Request) -> None:
    """Экспорт/импорт данных — только операторам. Обычный пользователь даже не
    должен знать о ручках: 404, как и на всей операторской поверхности."""
    if not request.state.user["is_operator"]:
        raise HTTPException(404)


def _full_dump(conn, uid: int) -> dict:
    cats = [dict(r) for r in conn.execute(
        "SELECT * FROM categories WHERE owner_id=? ORDER BY id", (uid,))]
    out_dates = []
    seen_tokens: set[str] = set()
    for r in conn.execute(
            "SELECT * FROM dates WHERE owner_id=? ORDER BY id", (uid,)).fetchall():
        d = dict(r)
        d["links"] = [x["url"] for x in conn.execute(
            "SELECT url FROM date_links WHERE date_id=? ORDER BY position, id", (r["id"],))]
        d["images"] = [x["filename"] for x in conn.execute(
            "SELECT filename FROM date_images WHERE date_id=? ORDER BY position, id", (r["id"],))]
        d["videos"] = [x["filename"] for x in conn.execute(
            "SELECT filename FROM date_videos WHERE date_id=? ORDER BY position, id", (r["id"],))]
        d["categories"] = [x["category_id"] for x in conn.execute(
            "SELECT category_id FROM date_categories WHERE date_id=?", (r["id"],))]
        d["booked_by"] = [x[0] for x in conn.execute(
            "SELECT COALESCE(g.name, b.guest_token) FROM bookings b "
            "LEFT JOIN guests g ON g.token=b.guest_token WHERE b.date_id=?", (r["id"],))]
        if r["guest_token"]:
            seen_tokens.add(r["guest_token"])
        out_dates.append(d)
    # гости — только те, кто фигурирует в свиданиях/бронях/вопросах владельца
    for r in conn.execute(
            "SELECT DISTINCT b.guest_token FROM bookings b JOIN dates d ON d.id=b.date_id "
            "WHERE d.owner_id=? AND b.guest_token IS NOT NULL", (uid,)):
        seen_tokens.add(r[0])
    guests = []
    if seen_tokens:
        ph = ",".join("?" * len(seen_tokens))
        guests = [dict(r) for r in conn.execute(
            f"SELECT * FROM guests WHERE token IN ({ph}) ORDER BY created_at",
            tuple(seen_tokens))]
    questions = [dict(r) for r in conn.execute(
        "SELECT q.* FROM questions q JOIN dates d ON d.id=q.date_id "
        "WHERE d.owner_id=? ORDER BY q.id", (uid,))]
    return {"exported_at": now_iso(), "categories": cats, "guests": guests,
            "dates": out_dates, "questions": questions}


@router.get("/export/json")
def export_json(request: Request, conn=Depends(get_db)):
    _require_operator(request)
    data = _full_dump(conn, request.state.user["id"])
    day = now_naive().strftime("%Y-%m-%d")
    return Response(json.dumps(data, ensure_ascii=False, indent=2),
                    media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="date4you-export-{day}.json"'})


@router.get("/export/csv")
def export_csv(request: Request, conn=Depends(get_db)):
    _require_operator(request)
    data = _full_dump(conn, request.state.user["id"])
    cat_names = {c["id"]: c["name"] for c in data["categories"]}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "Название", "Место", "Начало", "Конец", "Оплата", "Неактивно",
                "Архив", "Источник", "Выборы", "Кто выбрал", "Категории", "Ссылки"])
    for d in data["dates"]:
        w.writerow([
            d["id"], d["name"], d["place"] or "", d["starts_at"] or "", d["ends_at"] or "",
            pay_label(d["pay_split"]).replace("💸 ", ""),
            "да" if d["is_draft"] else "",
            "да" if d["archived_at"] else "",
            "гость" if d["origin"] == "guest" else "админ",
            len(d["booked_by"]),
            ", ".join(d["booked_by"]),
            ", ".join(cat_names.get(c, "?") for c in d["categories"]),
            " ".join(d["links"]),
        ])
    day = now_naive().strftime("%Y-%m-%d")
    return Response("\ufeff" + buf.getvalue(),       # BOM — чтобы Excel понял UTF-8
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="date4you-dates-{day}.csv"'})


@router.get("/export/archive")
def export_archive(request: Request, conn=Depends(get_db)):
    """Архив владельца: его данные (export.json) + только его фото/видео.

    Полный снимок базы (app.db) кладём ТОЛЬКО оператору — он содержит данные
    всех арендаторов. Обычный пользователь получает выгрузку строго своих данных.
    """
    _require_operator(request)
    user = request.state.user
    data = _full_dump(conn, user["id"])
    own_files = set()
    for d in data["dates"]:
        own_files.update(d["images"])
        own_files.update(d["videos"])
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("export.json", json.dumps(data, ensure_ascii=False, indent=2))
        if user["is_operator"]:
            z.write(backup.make_backup(), arcname="app.db")
        for fn in sorted(own_files):
            p = images.UPLOAD_DIR / fn
            if p.exists():
                z.write(p, arcname=f"uploads/{fn}")
    day = now_naive().strftime("%Y-%m-%d")
    return FileResponse(tmp.name, media_type="application/zip",
                        filename=f"date4you-export-{day}.zip",
                        background=BackgroundTask(os.unlink, tmp.name))


@router.post("/import/json")
async def import_json(request: Request, file: UploadFile = File(...),
                      conn=Depends(get_db)):
    """Импорт данных из нашего же export.json — ДОЗАПИСЬЮ к аккаунту оператора.

    Добавляет категории и свидания (с ссылками и привязкой фото/видео по именам
    файлов) как НОВЫЕ записи. Существующие данные не трогает и не дублирует
    осознанно — это аддитивный импорт. Брони/вопросы/гостей не переносим:
    это контент получателей, привязанный к их браузерам. Категориям выдаём
    свежие секретные ссылки. Файлы фото/видео должны уже лежать в uploads/
    (например, распакованы из архива) — отсутствующие записи просто пропускаем.
    """
    _require_operator(request)
    uid = request.state.user["id"]
    raw = await file.read()
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(400, "Файл слишком большой (макс 50 МБ)")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Не удалось разобрать JSON — нужен наш export.json")
    if not isinstance(data, dict) or not isinstance(data.get("dates"), list):
        raise HTTPException(400, "Это не похоже на наш export.json")

    cat_map: dict[int, int] = {}            # старый id категории → новый
    n_cats = n_dates = 0
    # категории
    for c in data.get("categories", []):
        if not isinstance(c, dict):
            continue
        name = clean_text(str(c.get("name") or ""), 200, "Название") or "Без названия"
        token = new_link_token()
        cur = conn.execute(
            "INSERT INTO categories(owner_id, name, description, link_token, "
            "link_enabled, moderate_proposals, created_at) VALUES(?,?,?,?,?,?,?)",
            (uid, name, clean_text(str(c.get("description") or ""), 1000, "Описание"),
             token, 1 if c.get("link_enabled", 1) else 0,
             1 if c.get("moderate_proposals") else 0, now_iso()))
        if c.get("id") is not None:
            cat_map[c["id"]] = cur.lastrowid
        n_cats += 1
    # свидания
    for d in data["dates"]:
        if not isinstance(d, dict):
            continue
        name = clean_text(str(d.get("name") or ""), 200, "Название") or "Без названия"
        capacity_value = parse_capacity(d.get("capacity", 1))
        did = insert_date(
            conn, name=name, place=d.get("place"), starts=d.get("starts_at"),
            ends=d.get("ends_at"), comment=d.get("comment"),
            origin="admin", guest_token=None, owner_id=uid,
            draft=1 if d.get("is_draft") else 0,
            pay_split=1 if d.get("pay_split") else 0, place_url=d.get("place_url"),
            capacity=capacity_value)
        if d.get("archived_at"):
            conn.execute("UPDATE dates SET archived_at=? WHERE id=?",
                         (now_iso(), did))
        # ссылки
        links = [u for u in (d.get("links") or []) if isinstance(u, str)]
        save_links(conn, did, links[:20])
        # привязка к импортированным категориям
        for old_cid in d.get("categories", []):
            new_cid = cat_map.get(old_cid)
            if new_cid:
                conn.execute(
                    "INSERT OR IGNORE INTO date_categories(date_id, category_id, position) "
                    "VALUES(?,?,?)", (did, new_cid, next_cat_pos(conn, new_cid)))
        # фото/видео — по именам файлов, если они уже есть на диске
        for pos, fn in enumerate(d.get("images") or []):
            if isinstance(fn, str) and images.SAFE_FILENAME.match(fn) \
                    and (images.UPLOAD_DIR / fn).exists():
                conn.execute(
                    "INSERT INTO date_images(date_id, filename, position) VALUES(?,?,?)",
                    (did, fn, pos))
        for pos, fn in enumerate(d.get("videos") or []):
            if isinstance(fn, str) and images.SAFE_FILENAME.match(fn) \
                    and (images.UPLOAD_DIR / fn).exists():
                conn.execute(
                    "INSERT INTO date_videos(date_id, filename, position) VALUES(?,?,?)",
                    (did, fn, pos))
        n_dates += 1
    conn.commit()
    return redir("/admin/", f"Импортировано: {n_cats} категорий и {n_dates} свиданий")


# ----- Категории -----------------------------------------------------------

@router.get("/categories", response_class=HTMLResponse)
def categories_list(request: Request, conn=Depends(get_db)):
    cats = conn.execute(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM date_categories dc WHERE dc.category_id=c.id) AS dcount, "
        # есть ли превью ссылки: своя картинка ИЛИ хотя бы одно фото активного
        # свидания категории (тогда соберётся коллаж). Для миниатюры в списке.
        "(c.og_image IS NOT NULL OR EXISTS("
        "  SELECT 1 FROM date_categories dc JOIN dates d ON d.id=dc.date_id "
        "  JOIN date_images di ON di.date_id=d.id "
        "  WHERE dc.category_id=c.id AND d.archived_at IS NULL AND d.is_draft=0)) AS has_og "
        "FROM categories c WHERE c.owner_id=? ORDER BY c.created_at DESC",
        (request.state.user["id"],)
    ).fetchall()
    return templates.TemplateResponse(
        request, "admin/categories.html", actx(request, conn, active="cats", cats=cats))


@router.post("/categories/create")
def category_create(request: Request, bg: BackgroundTasks, name: str = Form(...),
                    conn=Depends(get_db)):
    name = clean_text(name, 200, "Название", required=True)
    token = new_link_token()
    # мягкая очередь: при включённой модерации категорий новая помечается
    # is_reviewed=0 (ссылка работает сразу, админ просто видит её в очереди).
    reviewed = 0 if app_settings.is_on(conn, app_settings.MODERATE_CATEGORIES) else 1
    cursor = conn.execute(
        "INSERT INTO categories(owner_id, name, link_token, link_enabled, "
        "moderate_proposals, is_reviewed, created_at) VALUES(?,?,?,1,0,?,?)",
        (request.state.user["id"], name, token, reviewed, now_iso()))
    category_id = int(cursor.lastrowid)
    conn.commit()
    actor = request.state.user["display_name"] or request.state.user["tg_username"] or "—"
    notify_admin(bg, conn, request.state.user["id"], notify.card(
        "🆕 Создана категория",
        f"«{notify.esc(name)}»",
        f"Кто: {notify.esc(actor)}"))
    return redir(f"/admin/categories/{category_id}", "Категория создана — добавь название и описание")


def _cat_or_404(conn, cid: int, user):
    """Категория, которой можно управлять. Владелец — только свою; админ
    (is_operator) — любую (пункт «админ правит чужое»)."""
    if user["is_operator"]:
        cat = conn.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
        if not cat:
            raise HTTPException(404, "Категория не найдена")
        return cat
    return get_owned_category(conn, cid, user["id"])


@router.get("/categories/{cid}", response_class=HTMLResponse)
def category_detail(cid: int, request: Request, conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid, request.state.user)
    voting_events.close_due_once(conn, category_id=cid)
    cat = _cat_or_404(conn, cid, request.state.user)
    dates = conn.execute(
        "SELECT d.*, "
        "(SELECT COUNT(*) FROM bookings b WHERE b.date_id=d.id AND b.category_id=?) AS books, "
        "(SELECT GROUP_CONCAT(COALESCE(u.display_name, u.tg_username, g.name, "
        "'#' || substr(b.guest_token,1,6)), ', ') "
        " FROM bookings b LEFT JOIN users u ON u.id=b.user_id "
        " LEFT JOIN guests g ON g.token=b.guest_token "
        " WHERE b.date_id=d.id AND b.category_id=?) AS booked_names "
        "FROM dates d JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE dc.category_id=? "
        "ORDER BY (d.archived_at IS NOT NULL) ASC, dc.position ASC, d.created_at DESC",
        (cid, cid, cid)).fetchall()
    # «Прикрепить» — свидания того же владельца, что и категория (для админа,
    # правящего чужую категорию, это её владелец, а не сам админ).
    attachable = conn.execute(
        "SELECT id, name FROM dates WHERE owner_id=? AND archived_at IS NULL AND id NOT IN "
        "(SELECT date_id FROM date_categories WHERE category_id=?) ORDER BY created_at DESC",
        (cat["owner_id"], cid)).fetchall()
    # авто-превью ссылки: если своей картинки нет, показываем коллаж из фото
    # свиданий этой категории (тот же, что уйдёт в og:image). Здесь — только
    # флаг наличия; саму картинку отдаёт /admin/categories/{cid}/og-preview.
    auto_og = False
    if not cat["og_image"]:
        auto_og = conn.execute(
            "SELECT 1 FROM date_categories dc "
            "JOIN dates d ON d.id=dc.date_id "
            "JOIN date_images di ON di.date_id=d.id "
            "WHERE dc.category_id=? AND d.archived_at IS NULL AND d.is_draft=0 LIMIT 1",
            (cid,)).fetchone() is not None
    state = voting.get_category_state(conn, cid)
    leader_ids = set(state.leader_date_ids)
    can_change_composition = not _category_voting_is_closed(cat)
    return templates.TemplateResponse(
        request, "admin/category_detail.html",
        actx(request, conn, active="cats", cat=cat, dates=dates, attachable=attachable,
             auto_og=auto_og, voting_state=state, leader_ids=leader_ids,
             can_change_composition=can_change_composition))


@router.post("/categories/{cid}/voting")
def category_voting_configure(cid: int, request: Request,
                              choice_mode: str = Form(...),
                              voting_deadline: str = Form(...),
                              conn=Depends(get_db)):
    """Явная настройка режима и ручного дедлайна голосования (время МСК)."""
    cat = _cat_or_404(conn, cid, request.state.user)
    try:
        state = voting.configure_category(
            conn, cid, cat["owner_id"], choice_mode, voting_deadline,
        )
    except voting.VotingError as exc:
        _raise_voting_http(exc)
    for row in conn.execute(
        "SELECT DISTINCT user_id FROM bookings WHERE category_id=? AND user_id IS NOT NULL",
        (cid,),
    ):
        voting_events.queue_deadline_reminder(conn, cid, int(row["user_id"]))
    conn.commit()
    return redir(f"/admin/categories/{cid}", "Настройки голосования сохранены")


@router.post("/categories/{cid}/voting/resolve")
def category_voting_resolve_tie(cid: int, request: Request,
                                winner_date_id: int = Form(...),
                                conn=Depends(get_db)):
    """При ничьей владелец вручную выбирает одного из фактических лидеров."""
    cat = _cat_or_404(conn, cid, request.state.user)
    try:
        state = voting.resolve_tie(conn, cid, cat["owner_id"], winner_date_id)
    except voting.VotingError as exc:
        _raise_voting_http(exc)
    voting_events.queue_category_outcome(conn, state)
    conn.commit()
    return redir(f"/admin/categories/{cid}", "Победитель выбран")


def _category_collage_sources(conn, cid: int) -> list[str]:
    """Имена фото для коллажа-превью категории (активные, не-черновики), до 8."""
    return [r["filename"] for r in conn.execute(
        "SELECT di.filename FROM date_categories dc "
        "JOIN dates d ON d.id=dc.date_id "
        "JOIN date_images di ON di.date_id=d.id "
        "WHERE dc.category_id=? AND d.archived_at IS NULL AND d.is_draft=0 "
        "ORDER BY dc.position ASC, di.position ASC, di.id ASC LIMIT 8", (cid,))]


def prewarm_date_collages(did: int) -> None:
    """Фоновая пере-сборка коллажей для всех категорий свидания. Зовём из
    BackgroundTasks после правки фото/категорий — чтобы список «Категории» и
    дашборд открывались по тёплому кэшу (иначе первый заход собирал N коллажей
    синхронно на единственном воркере — отсюда «долгая первая загрузка»)."""
    conn = db.connect()
    try:
        cids = [r["category_id"] for r in conn.execute(
            "SELECT category_id FROM date_categories WHERE date_id=?", (did,))]
        for cid in cids:
            files = _category_collage_sources(conn, cid)
            if files:
                images.build_og_collage(files)     # идемпотентно: строит, если нет в кэше
    except Exception:
        log.exception("prewarm_date_collages did=%s", did)
    finally:
        conn.close()


@router.get("/categories/{cid}/og-preview")
def category_og_preview(cid: int, request: Request, conn=Depends(get_db)):
    """Коллаж-превью ссылки для редактора категории (когда своей картинки нет).
    Та же сборка, что и публичный og:image, но за owner-гейтом."""
    cat = _cat_or_404(conn, cid, request.state.user)
    if cat["og_image"]:
        # своя картинка — кропаем в 1200×630 по точке фокуса (как уйдёт в og:image)
        if not images.SAFE_FILENAME.match(cat["og_image"]):
            raise HTTPException(404)
        cropped = images.build_og_crop(cat["og_image"], cat["og_focus"])
        if not cropped:
            raise HTTPException(404)
        return FileResponse(cropped, media_type="image/webp",
                            headers={"Cache-Control": "private, max-age=300"})
    rows = conn.execute(
        "SELECT di.filename FROM date_categories dc "
        "JOIN dates d ON d.id=dc.date_id "
        "JOIN date_images di ON di.date_id=d.id "
        "WHERE dc.category_id=? AND d.archived_at IS NULL AND d.is_draft=0 "
        "ORDER BY dc.position ASC, di.position ASC, di.id ASC LIMIT 8",
        (cid,)).fetchall()
    collage = images.build_og_collage([r["filename"] for r in rows])
    if not collage:
        raise HTTPException(404)
    return FileResponse(collage, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=300"})


@router.post("/categories/{cid}/rename")
def category_rename(cid: int, request: Request, name: str = Form(...),
                    description: str = Form(""),
                    og_title: str = Form(""), og_desc: str = Form(""),
                    og_image: UploadFile | None = File(None),
                    conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid, request.state.user)
    name = clean_text(name, 200, "Название", required=True)
    description = clean_text(description, 1000, "Описание")
    og_title = clean_text(og_title, 120, "Заголовок превью")
    og_desc = clean_text(og_desc, 200, "Описание превью")

    # картинка превью — опционально; новый файл сжимаем в WebP (как фото свиданий),
    # старый сносим только после успешной записи
    new_image = None
    if og_image is not None and (og_image.filename or "").strip():
        try:
            new_image = images.save_upload(og_image)
        except ValueError as e:
            raise HTTPException(400, str(e))

    old_image = cat["og_image"]
    if new_image:
        # новая картинка — фокус к центру (старая точка к ней не относится)
        conn.execute(
            "UPDATE categories SET name=?, description=?, og_title=?, og_desc=?, og_image=?, og_focus=NULL WHERE id=?",
            (name, description, og_title, og_desc, new_image, cid))
    else:
        conn.execute(
            "UPDATE categories SET name=?, description=?, og_title=?, og_desc=? WHERE id=?",
            (name, description, og_title, og_desc, cid))
    conn.commit()
    if new_image and old_image:          # старую картинку — только после коммита
        images.delete_file(old_image)
    return redir(f"/admin/categories/{cid}", "Сохранено")


@router.post("/categories/{cid}/og_image/delete")
def category_og_image_delete(cid: int, request: Request, conn=Depends(get_db)):
    """Убрать свою картинку превью → вернуться к дефолтной /static/og.png."""
    cat = _cat_or_404(conn, cid, request.state.user)
    old = cat["og_image"]
    conn.execute("UPDATE categories SET og_image=NULL WHERE id=?", (cid,))
    conn.commit()
    if old:
        images.delete_file(old)
    return redir(f"/admin/categories/{cid}", "Картинка превью убрана")


@router.post("/categories/{cid}/preview/reset")
def category_preview_reset(cid: int, request: Request, conn=Depends(get_db)):
    """Сбросить превью ссылки к стандартному виду: убрать свою картинку И текст
    (og_title/og_desc). Дальше превью — дефолтный текст + авто-коллаж из фото."""
    cat = _cat_or_404(conn, cid, request.state.user)
    old = cat["og_image"]
    conn.execute(
        "UPDATE categories SET og_image=NULL, og_title=NULL, og_desc=NULL, og_focus=NULL WHERE id=?",
        (cid,))
    conn.commit()
    if old:
        images.delete_file(old)
    return redir(f"/admin/categories/{cid}", "Превью сброшено к стандартному")


@router.post("/categories/{cid}/og_focus")
def category_og_focus(cid: int, request: Request, focus: str = Form(...),
                      conn=Depends(get_db)):
    """Точка фокуса своей картинки превью: «X% Y%» (0..100). Владелец двигает
    картинку в редакторе — og:image кропается по ней (WYSIWYG)."""
    import re as _re
    cat = _cat_or_404(conn, cid, request.state.user)
    m = _re.fullmatch(r"\s*(\d{1,3})%\s+(\d{1,3})%\s*", focus or "")
    if not m or int(m.group(1)) > 100 or int(m.group(2)) > 100:
        raise HTTPException(400, "Некорректная точка фокуса")
    if not cat["og_image"]:
        raise HTTPException(400, "У превью нет своей картинки")
    value = f"{int(m.group(1))}% {int(m.group(2))}%"
    conn.execute("UPDATE categories SET og_focus=? WHERE id=?", (value, cid))
    conn.commit()
    return JSONResponse({"ok": True, "focus": value})


@router.post("/categories/{cid}/toggle")
def category_toggle(cid: int, request: Request, conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid, request.state.user)
    new_val = 0 if cat["link_enabled"] else 1
    conn.execute("UPDATE categories SET link_enabled=? WHERE id=?", (new_val, cid))
    conn.commit()
    return redir(f"/admin/categories/{cid}",
                 "Ссылка включена" if new_val else "Ссылка отключена")


@router.post("/categories/{cid}/moderation")
def category_moderation(cid: int, request: Request, conn=Depends(get_db)):
    # Режим модерации предложений — решение платформы, а не владельца категории:
    # переключать может только оператор (обычному пользователю — 404, как на всей
    # операторской поверхности).
    if not request.state.user["is_operator"]:
        raise HTTPException(404)
    cat = _cat_or_404(conn, cid, request.state.user)
    new_val = 0 if cat["moderate_proposals"] else 1
    conn.execute("UPDATE categories SET moderate_proposals=? WHERE id=?", (new_val, cid))
    conn.commit()
    return redir(f"/admin/categories/{cid}",
                 "Предложения гостей теперь попадают на модерацию (вкладка «Неактивные»)"
                 if new_val else "Предложения гостей теперь публикуются сразу")


@router.post("/categories/{cid}/regenerate")
def category_regenerate(cid: int, request: Request, conn=Depends(get_db)):
    _cat_or_404(conn, cid, request.state.user)
    # Сначала берём блокировку записи, затем заново читаем текущий токен. При двух
    # одновременных регенерациях второй запрос заменит в очереди ссылку первого,
    # а не устаревшую ссылку, которую видел до ожидания блокировки.
    if conn.execute("UPDATE categories SET id=id WHERE id=?", (cid,)).rowcount == 0:
        raise HTTPException(404, "Категория не найдена")
    cat = _cat_or_404(conn, cid, request.state.user)
    old_token = cat["link_token"]
    token = new_link_token()
    conn.execute("UPDATE categories SET link_token=?, link_enabled=1 WHERE id=?", (token, cid))

    # Обновляем каждое неотправленное сообщение с точным секретным URL, включая
    # уже наступившие и сообщения с иным event_key. Снять claim безопасно:
    # воркер повторно проверяет уникальную аренду непосредственно перед отправкой.
    if old_token:
        old_url = f"{BASE_URL}/c/{old_token}"
        new_url = f"{BASE_URL}/c/{token}"
        stamp = now_iso()
        conn.execute(
            "UPDATE notification_outbox SET text=REPLACE(text, ?, ?), "
            "claimed_at=NULL, updated_at=? "
            "WHERE sent_at IS NULL AND cancelled_at IS NULL AND instr(text, ?) > 0",
            (old_url, new_url, stamp, old_url),
        )

    # После получения блокировки перечитываем статус: фоновый обработчик мог
    # успеть завершить голосование до начала этой транзакции.
    cat = conn.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()

    # Пересобираем уведомления в той же транзакции, чтобы нигде не осталась
    # очередь со старой, уже недействующей гостевой ссылкой.
    if cat["voting_status"] == voting.STATUS_OPEN:
        voters = conn.execute(
            "SELECT DISTINCT user_id FROM bookings "
            "WHERE category_id=? AND user_id IS NOT NULL",
            (cid,),
        ).fetchall()
        for voter in voters:
            voting_events.queue_deadline_reminder(conn, cid, int(voter["user_id"]))
    elif cat["voting_status"] in voting.CLOSED_STATUSES:
        voting_events.queue_category_outcome(conn, voting.get_category_state(conn, cid))
    conn.commit()
    return redir(f"/admin/categories/{cid}",
                 "Новая ссылка сгенерирована. Старая больше не работает, все данные сохранены.")


@router.post("/categories/{cid}/delete")
def category_delete(cid: int, request: Request, bg: BackgroundTasks, conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid, request.state.user)
    cat = _require_category_composition_mutable(conn, cat)
    affected = conn.execute(
        "SELECT DISTINCT d.id, d.name FROM dates d JOIN bookings b ON b.date_id=d.id "
        "WHERE b.category_id=?", (cid,),
    ).fetchall()
    voting_events.cancel_category_notifications(conn, cid)
    for d in affected:
        voting_events.queue_date_removed(
            conn, d["id"], d["name"], cid, cat["name"], None,
        )
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    conn.commit()
    if cat["og_image"]:
        images.delete_file(cat["og_image"])
    actor = request.state.user["display_name"] or request.state.user["tg_username"] or "—"
    notify_admin(bg, conn, cat["owner_id"], notify.card(
        "🗑 Удалена категория",
        f"«{notify.esc(cat['name'])}»",
        f"Кто: {notify.esc(actor)}"))
    return redir("/admin/categories", "Категория удалена (свидания остались)")


@router.post("/categories/{cid}/attach")
def category_attach(cid: int, request: Request, date_id: int = Form(...),
                    conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid, request.state.user)
    cat = _require_category_composition_mutable(conn, cat)
    # привязать можно только свидание ТОГО ЖЕ владельца, что и категория, —
    # иначе чужое утечёт в категорию (для админа контекст = владелец категории)
    date_row = get_owned_date(conn, date_id, cat["owner_id"])
    _validate_start_after_open_deadlines(conn, [cid], date_row["starts_at"])
    conn.execute(
        "INSERT OR IGNORE INTO date_categories(date_id, category_id, position) "
        "VALUES(?,?,?)", (date_id, cid, next_cat_pos(conn, cid)))
    # попав хотя бы в одну категорию, свидание становится активным
    conn.execute("UPDATE dates SET is_draft=0 WHERE id=?", (date_id,))
    conn.commit()
    return redir(f"/admin/categories/{cid}", "Свидание добавлено в категорию")


@router.post("/categories/{cid}/dates_reorder")
def category_dates_reorder(cid: int, request: Request, order: str = Form(...),
                           conn=Depends(get_db)):
    """Drag-and-drop порядок свиданий: order — id через запятую."""
    cat = _cat_or_404(conn, cid, request.state.user)
    cat = _require_category_composition_mutable(conn, cat)
    ids_db = [r[0] for r in conn.execute(
        "SELECT date_id FROM date_categories WHERE category_id=?", (cid,))]
    try:
        ids = [int(x) for x in order.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "Некорректный порядок")
    if sorted(ids) != sorted(ids_db):
        raise HTTPException(400, "Некорректный порядок")
    for pos, did in enumerate(ids):
        conn.execute(
            "UPDATE date_categories SET position=? WHERE category_id=? AND date_id=?",
            (pos, cid, did))
    conn.commit()
    return JSONResponse({"ok": True})


@router.post("/categories/{cid}/detach")
def category_detach(cid: int, request: Request, date_id: int = Form(...),
                    conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid, request.state.user)
    cat = _require_category_composition_mutable(conn, cat)
    d = conn.execute("SELECT name FROM dates WHERE id=?", (date_id,)).fetchone()
    affected_users = [int(r["user_id"]) for r in conn.execute(
        "SELECT DISTINCT user_id FROM bookings WHERE date_id=? AND category_id=? "
        "AND user_id IS NOT NULL", (date_id, cid),
    )]
    if d:
        voting_events.queue_date_removed(
            conn, date_id, d["name"], cid, cat["name"], cat["link_token"],
        )
    conn.execute("DELETE FROM bookings WHERE date_id=? AND category_id=?", (date_id, cid))
    conn.execute("DELETE FROM date_categories WHERE date_id=? AND category_id=?", (date_id, cid))
    for user_id in affected_users:
        still_voting = conn.execute(
            "SELECT 1 FROM bookings WHERE category_id=? AND user_id=? LIMIT 1",
            (cid, user_id),
        ).fetchone()
        if not still_voting:
            voting_events.cancel_deadline_reminder(conn, cid, user_id)
    # если это была последняя категория свидания — оно становится неактивным
    still = conn.execute(
        "SELECT 1 FROM date_categories WHERE date_id=? LIMIT 1", (date_id,)).fetchone()
    if not still:
        conn.execute("UPDATE dates SET is_draft=1 WHERE id=?", (date_id,))
    conn.commit()
    return redir(f"/admin/categories/{cid}", "Свидание убрано из категории")


# ----- Свидания -------------------------------------------------------------

VIEW_WHERE = {
    "active": "d.archived_at IS NULL AND d.is_draft=0",
    "drafts": "d.archived_at IS NULL AND d.is_draft=1",
    "archived": "d.archived_at IS NOT NULL",
}
FLT_WHERE = {
    "guest": "d.origin='guest'",
    "booked": "EXISTS (SELECT 1 FROM bookings b WHERE b.date_id=d.id)",
    "nodate": "d.starts_at IS NULL AND d.ends_at IS NULL",
}
SORT_ORDER = {
    # d.id DESC — тай-брейк: created_at имеет точность до секунды
    "new": "d.created_at DESC, d.id DESC",
    "when": "(d.starts_at IS NULL) ASC, d.starts_at ASC, d.created_at DESC, d.id DESC",
    "book": "books DESC, d.created_at DESC, d.id DESC",
}
PER_PAGE = 30


@router.get("/dates", response_class=HTMLResponse)
def dates_list(request: Request, conn=Depends(get_db)):
    qp = request.query_params
    view = qp.get("view") if qp.get("view") in VIEW_WHERE else "active"
    sort = qp.get("sort") if qp.get("sort") in SORT_ORDER else "new"
    flt = qp.get("f") if qp.get("f") in FLT_WHERE else ""
    cat = qp.get("cat", "")

    # Само-исцеление инварианта «активно ⇔ есть категория». Историческая причина
    # «старое неактивное свидание не достаётся из Неактивных»: у него уже была
    # привязка в date_categories, но is_draft остался 1 (легаси-баг/недокат).
    # Приводим к инварианту: НЕархивное свидание владельца с ≥1 категорией и
    # is_draft=1 → активным. ВАЖНО: не трогаем гостевые предложения (origin='guest')
    # — они законно ждут модерации на вкладке «Неактивные», их публикует «Одобрить».
    conn.execute(
        "UPDATE dates SET is_draft=0 WHERE owner_id=? AND is_draft=1 "
        "AND origin<>'guest' AND archived_at IS NULL "
        "AND id IN (SELECT date_id FROM date_categories)", (request.state.user["id"],))
    conn.commit()

    where = VIEW_WHERE[view]
    params: list = [request.state.user["id"]]
    where = "d.owner_id=? AND " + where
    if flt:
        where += " AND " + FLT_WHERE[flt]
    if cat.isdigit():
        where += " AND d.id IN (SELECT date_id FROM date_categories WHERE category_id=?)"
        params.append(int(cat))

    total = conn.execute(
        f"SELECT COUNT(*) FROM dates d WHERE {where}", params).fetchone()[0]
    pages = max(1, -(-total // PER_PAGE))
    try:
        page = max(1, min(int(qp.get("page", "1")), pages))
    except ValueError:
        page = 1

    rows = conn.execute(
        f"SELECT d.*, "
        f"(SELECT COUNT(*) FROM bookings b WHERE b.date_id=d.id) AS books, "
        f"(SELECT GROUP_CONCAT(COALESCE(g.name, '#' || substr(b.guest_token,1,6)), ', ') "
        f" FROM bookings b LEFT JOIN guests g ON g.token=b.guest_token "
        f" WHERE b.date_id=d.id) AS booked_names, "
        f"(SELECT filename FROM date_images di WHERE di.date_id=d.id "
        f" ORDER BY di.position, di.id LIMIT 1) AS cover, "
        f"(SELECT focus FROM date_images di WHERE di.date_id=d.id "
        f" ORDER BY di.position, di.id LIMIT 1) AS cover_focus, "
        f"(SELECT GROUP_CONCAT(c.name, ', ') FROM date_categories dc "
        f" JOIN categories c ON c.id=dc.category_id WHERE dc.date_id=d.id) AS cats "
        f"FROM dates d WHERE {where} ORDER BY {SORT_ORDER[sort]} "
        f"LIMIT {PER_PAGE} OFFSET {(page - 1) * PER_PAGE}",
        params).fetchall()

    drafts_n = conn.execute(
        "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NULL AND is_draft=1",
        (request.state.user["id"],)).fetchone()[0]
    active_n = conn.execute(
        "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NULL AND is_draft=0",
        (request.state.user["id"],)).fetchone()[0]
    archived_n = conn.execute(
        "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NOT NULL",
        (request.state.user["id"],)).fetchone()[0]

    keep = [("sort", sort)] if sort != "new" else []
    if flt:
        keep.append(("f", flt))
    if cat.isdigit():
        keep.append(("cat", cat))
    qs_keep = ("&" + urlencode(keep)) if keep else ""

    # вид списка (карточки/таблица) — cookie, читается на сервере для SSR
    layout = request.cookies.get("layout")
    layout = layout if layout in ("cards", "list") else "cards"

    return templates.TemplateResponse(
        request, "admin/dates.html",
        actx(request, conn, active="dates", rows=rows, view=view, sort=sort,
             flt=flt, cat=cat, cats=_all_cats(conn, request.state.user["id"]),
             drafts_n=drafts_n, active_n=active_n, archived_n=archived_n,
             qs_keep=qs_keep, page=page, pages=pages, layout=layout))


def _all_cats(conn, uid: int):
    return conn.execute(
        "SELECT id, name, voting_status, voting_deadline, closed_at "
        "FROM categories WHERE owner_id=? ORDER BY created_at DESC",
        (uid,)).fetchall()


def enforce_date_quota(conn, user) -> None:
    """Отказ, если у пользователя уже date_limit активных (не архивных) свиданий.
    Архивные не считаем — это «история», она не растёт бесконтрольно."""
    limit = user["date_limit"]
    used = conn.execute(
        "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NULL",
        (user["id"],)).fetchone()[0]
    if used >= limit:
        contact = f" Напиши в поддержку {SUPPORT_CONTACT}" if SUPPORT_CONTACT \
                  else " Контакт поддержки — на странице «О проекте»."
        raise HTTPException(400, f"Достигнут лимит {limit} свиданий.{contact}")


def parse_pay(value) -> int:
    """Вариант оплаты из формы → 0..3 (0 не указано, 1 — 50/50, 2 — я плачу,
    3 — ты оплатишь). Любое неизвестное значение трактуем как 0."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    return v if v in (1, 2, 3) else 0


def parse_public(value) -> int:
    """Тумблер «Публичное/Приватное» из формы → 1/0. Чекбокс: присутствует в
    форме (любое значение) = публичное (1); отсутствует = приватное (0)."""
    return 0 if value is None else 1



@router.get("/dates/new", response_class=HTMLResponse)
def date_new_form(request: Request, conn=Depends(get_db)):
    checked = set()
    pre = request.query_params.get("category")
    if pre and pre.isdigit():
        checked.add(int(pre))
    cats = _all_cats(conn, request.state.user["id"])
    locked_cat_ids = {c["id"] for c in cats if _category_voting_is_closed(c)}
    checked.difference_update(locked_cat_ids)
    return templates.TemplateResponse(
        request, "admin/date_form.html",
        actx(request, conn, active="dates", date=None, photos=[], videos=[], links_text="",
             cats=cats, checked=checked, locked_cat_ids=locked_cat_ids,
             slots=images.MAX_IMAGES))


@router.post("/dates/new")
def date_create(request: Request, bg: BackgroundTasks,
                name: str = Form(...), place: str = Form(""),
                starts_at: str = Form(""), ends_at: str = Form(""),
                links: str = Form(""), comment: str = Form(""),
                capacity: str = Form("1"),
                pay: str | None = Form(None),
                is_public: str | None = Form(None),
                categories: list[int] = Form(default=[]),
                photos: list[UploadFile] = File(default=[], alias="images"),
                videos: list[UploadFile] = File(default=[], alias="videos"),
                image_focuses: str = Form(""),
                conn=Depends(get_db)):
    uid = request.state.user["id"]
    user_throttle("datecreate", uid, request)        # анти-всплеск
    enforce_date_quota(conn, request.state.user)      # общая квота аккаунта
    name = clean_text(name, 200, "Название", required=True)
    place, place_url, needs_resolve = places.split_place(clean_text(place, 500, "Место"))
    comment = clean_text(comment, 2000, "Комментарий")
    starts, ends = normalize_period(parse_dt_local(starts_at), parse_dt_local(ends_at))
    link_list = parse_links(links)
    pay_value = parse_pay(pay)
    public_value = parse_public(is_public)

    # привязываем только СВОИ категории (чужие id из формы молча игнорируем)
    own_cats = {r[0] for r in conn.execute(
        "SELECT id FROM categories WHERE owner_id=?", (uid,))}
    attach = [c for c in categories if c in own_cats]
    for cid in attach:
        cat = conn.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
        _require_category_composition_mutable(conn, cat)
    _validate_start_after_open_deadlines(conn, attach, starts)
    capacity_value = parse_capacity(capacity)
    # свидание без категорий неактивно автоматически (гости его не видят)
    date_id = insert_date(conn, name=name, place=place, starts=starts, ends=ends,
                          comment=comment, origin="admin", guest_token=None,
                          owner_id=uid,
                          draft=0 if attach else 1,
                          pay_split=pay_value, place_url=place_url,
                          is_public=public_value, capacity=capacity_value)
    for cid in attach:
        conn.execute(
            "INSERT OR IGNORE INTO date_categories(date_id, category_id, position) "
            "VALUES(?,?,?)", (date_id, cid, next_cat_pos(conn, cid)))
    save_links(conn, date_id, link_list)
    saved_files: list[str] = []
    try:
        saved_files += add_photos(conn, date_id, photos, existing=0,
                                  focuses=image_focuses.split(",") if image_focuses else None)
        saved_files += add_videos(conn, date_id, videos, existing=0)
    except Exception:
        # фото уже на диске, а видео битое (или наоборот) — не оставляем сирот
        for fn in saved_files:
            images.delete_file(fn)
        raise
    conn.commit()
    if needs_resolve:
        bg.add_task(places.resolve_into_db, date_id, place_url)
    bg.add_task(prewarm_date_collages, date_id)   # тёплый кэш коллажей для списка «Категории»
    actor = request.state.user["display_name"] or request.state.user["tg_username"] or "—"
    notify_admin(bg, conn, uid, notify.card(
        "🆕 Создано свидание",
        f"«{notify.esc(name)}»",
        f"Кто: {notify.esc(actor)}"))
    return redir("/admin/dates", "Свидание создано")


def add_videos(conn, date_id: int, files, existing: int) -> list[str]:
    """Сохраняет видео атомарно, вписывает в БД, возвращает имена файлов."""
    files = [f for f in files if f and f.filename]
    if not files:
        return []
    if existing + len(files) > images.MAX_VIDEOS:
        raise HTTPException(400, f"Максимум {images.MAX_VIDEOS} видео у одного свидания")
    try:
        saved = images.save_videos_batch(files)
    except ValueError as e:
        raise HTTPException(400, str(e))
    for fn in saved:
        conn.execute("INSERT INTO date_videos(date_id, filename) VALUES(?,?)",
                     (date_id, fn))
    return saved


def _date_or_404(conn, did: int, user):
    """Свидание, которым можно управлять. Владелец — только своё; админ
    (is_operator) — любое (пункт «админ правит чужое»)."""
    if user["is_operator"]:
        d = conn.execute("SELECT * FROM dates WHERE id=?", (did,)).fetchone()
        if not d:
            raise HTTPException(404, "Свидание не найдено")
        return d
    return get_owned_date(conn, did, user["id"])


def _require_date_not_in_closed_vote(conn, did: int, action: str) -> None:
    # Lock the date first: every competing category/vote mutation is a writer,
    # so the category list and deadline checks below remain a stable snapshot.
    if conn.execute("UPDATE dates SET id=id WHERE id=?", (did,)).rowcount == 0:
        raise HTTPException(404, "Свидание не найдено")
    for cat in conn.execute(
        "SELECT c.* FROM categories c JOIN date_categories dc ON dc.category_id=c.id "
        "WHERE dc.date_id=?", (did,),
    ):
        if _category_voting_is_closed(cat):
            raise HTTPException(
                409,
                f"Нельзя {action}: голосование в категории «{cat['name']}» уже завершено",
            )


@router.get("/dates/{did}/edit", response_class=HTMLResponse)
def date_edit_form(did: int, request: Request, conn=Depends(get_db)):
    d = _date_or_404(conn, did, request.state.user)
    photos = conn.execute(
        "SELECT * FROM date_images WHERE date_id=? ORDER BY position, id", (did,)).fetchall()
    link_rows = conn.execute(
        "SELECT url FROM date_links WHERE date_id=? ORDER BY position, id", (did,)).fetchall()
    checked = {r[0] for r in conn.execute(
        "SELECT category_id FROM date_categories WHERE date_id=?", (did,))}
    videos = conn.execute(
        "SELECT * FROM date_videos WHERE date_id=? ORDER BY position, id", (did,)).fetchall()
    booked = conn.execute(
        "SELECT b.id AS bid, "
        "COALESCE(u.display_name, u.tg_username, g.name, "
        "'#' || substr(b.guest_token,1,6)) AS name, c.name AS cat "
        "FROM bookings b LEFT JOIN users u ON u.id=b.user_id "
        "LEFT JOIN guests g ON g.token=b.guest_token "
        "JOIN categories c ON c.id=b.category_id WHERE b.date_id=? ORDER BY b.created_at",
        (did,)).fetchall()
    proposer = gname(conn, d["guest_token"]) if d["origin"] == "guest" else None
    cats = _all_cats(conn, d["owner_id"])
    locked_cat_ids = {c["id"] for c in cats if _category_voting_is_closed(c)}
    return templates.TemplateResponse(
        request, "admin/date_form.html",
        actx(request, conn, active="dates", date=d, photos=photos, videos=videos,
             booked=booked,
             proposer=proposer,
             links_text="\n".join(r["url"] for r in link_rows),
             # категории показываем владельца свидания (админ может править чужое)
             cats=cats, checked=checked, locked_cat_ids=locked_cat_ids,
             slots=images.MAX_IMAGES - len(photos)))


@router.post("/dates/{did}/edit")
def date_update(did: int, request: Request, bg: BackgroundTasks, name: str = Form(...),
                place: str = Form(""),
                starts_at: str = Form(""), ends_at: str = Form(""),
                links: str = Form(""), comment: str = Form(""),
                capacity: str = Form("1"),
                pay: str | None = Form(None),
                is_public: str | None = Form(None),
                categories: list[int] = Form(default=[]),
                photos: list[UploadFile] = File(default=[], alias="images"),
                videos: list[UploadFile] = File(default=[], alias="videos"),
                image_focuses: str = Form(""),
                conn=Depends(get_db)):
    d = _date_or_404(conn, did, request.state.user)
    # Проверяем расписание, вместимость и категории в той же сериализованной
    # транзакции, что и запись. После блокировки перечитываем свидание, чтобы
    # параллельная настройка категории не сделала снимок устаревшим.
    conn.execute("UPDATE dates SET id=id WHERE id=?", (did,))
    d = _date_or_404(conn, did, request.state.user)
    user_throttle("dateedit", request.state.user["id"], request)
    name = clean_text(name, 200, "Название", required=True)
    place, place_url, needs_resolve = places.place_on_edit(
        clean_text(place, 500, "Место"), d)
    comment = clean_text(comment, 2000, "Комментарий")
    starts, ends = normalize_period(parse_dt_local(starts_at), parse_dt_local(ends_at))
    link_list = parse_links(links)
    pay_value = parse_pay(pay)
    public_value = parse_public(is_public)

    # привязываем только категории владельца свидания (чужие id из формы молча
    # игнорируем). Для админа, правящего чужое, контекст = владелец свидания.
    own_cats = {r[0] for r in conn.execute(
        "SELECT id FROM categories WHERE owner_id=?", (d["owner_id"],))}
    categories = [c for c in categories if c in own_cats]
    requested = set(categories)
    current = {r[0] for r in conn.execute(
        "SELECT category_id FROM date_categories WHERE date_id=?", (did,))}

    # Закрывшаяся категория — зафиксированный снимок: её нельзя ни добавить к
    # свиданию, ни снять. Остальные категории синхронизируем точечно, не через
    # DELETE/INSERT всех строк, чтобы не затронуть уже установленный порядок.
    for cid in current ^ requested:
        cat = conn.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
        if cat:
            _require_category_composition_mutable(conn, cat)
    _validate_start_after_open_deadlines(conn, requested, starts)

    # После результата вместимость тоже остаётся частью зафиксированного
    # варианта. До закрытия сервис разрешит изменение, но не ниже числа голосов
    # в любой отдельно взятой категории.
    capacity_value = parse_capacity(capacity)
    if capacity_value != int(d["capacity"]):
        for cid in current:
            cat = conn.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
            if cat and _category_voting_is_closed(cat):
                raise HTTPException(
                    409,
                    "Нельзя изменить количество участников после завершения голосования",
                )
    try:
        voting.set_date_capacity(
            conn, did, d["owner_id"], capacity_value,
        )
    except voting.VotingError as exc:
        _raise_voting_http(exc)
    # свидание без категорий неактивно автоматически (гости его не видят)
    is_draft = 0 if categories else 1

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

    conn.execute(
        "UPDATE dates SET name=?, place=?, place_url=?, starts_at=?, ends_at=?, "
        "comment=?, is_draft=?, pay_split=?, is_public=? WHERE id=?",
        (name, place, place_url, starts, ends, comment,
         is_draft, pay_value, public_value, did))

    # Синхронизируем только фактическую разницу; голоса снятых открытых
    # категорий удаляются, а неизменённые строки и их порядок сохраняются.
    for cid in current - requested:
        affected_users = [int(r["user_id"]) for r in conn.execute(
            "SELECT DISTINCT user_id FROM bookings WHERE date_id=? AND category_id=? "
            "AND user_id IS NOT NULL", (did, cid),
        )]
        removed_cat = conn.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
        if removed_cat:
            voting_events.queue_date_removed(
                conn, did, d["name"], cid, removed_cat["name"],
                removed_cat["link_token"],
            )
        conn.execute("DELETE FROM bookings WHERE date_id=? AND category_id=?", (did, cid))
        conn.execute("DELETE FROM date_categories WHERE date_id=? AND category_id=?", (did, cid))
        for user_id in affected_users:
            if not conn.execute(
                "SELECT 1 FROM bookings WHERE category_id=? AND user_id=? LIMIT 1",
                (cid, user_id),
            ).fetchone():
                voting_events.cancel_deadline_reminder(conn, cid, user_id)
    for cid in requested - current:
        conn.execute(
            "INSERT INTO date_categories(date_id, category_id, position) VALUES(?,?,?)",
            (did, cid, next_cat_pos(conn, cid)),
        )

    save_links(conn, did, link_list)
    existing = conn.execute(
        "SELECT COUNT(*) FROM date_images WHERE date_id=?", (did,)).fetchone()[0]
    vexisting = conn.execute(
        "SELECT COUNT(*) FROM date_videos WHERE date_id=?", (did,)).fetchone()[0]
    saved_files: list[str] = []
    try:
        saved_files += add_photos(conn, did, photos, existing=existing,
                                  focuses=image_focuses.split(",") if image_focuses else None)
        saved_files += add_videos(conn, did, videos, existing=vexisting)
    except Exception:
        for fn in saved_files:
            images.delete_file(fn)
        raise
    voting_events.queue_date_changed(conn, did, changed_labels)
    conn.commit()
    if needs_resolve:
        bg.add_task(places.resolve_into_db, did, place_url)
    bg.add_task(prewarm_date_collages, did)   # тёплый кэш коллажей для списка «Категории»
    return redir(f"/admin/dates/{did}/edit", "Сохранено")


@router.post("/dates/{did}/publish")
def date_publish(did: int, request: Request, bg: BackgroundTasks,
                 next: str = Form("/admin/dates"), conn=Depends(get_db)):
    d = _date_or_404(conn, did, request.state.user)
    _require_date_not_in_closed_vote(conn, did, "изменить состав свиданий")
    d = _date_or_404(conn, did, request.state.user)
    # Инвариант: активно ⇔ есть хотя бы одна категория (иначе гости не видят).
    # Публиковать имеет смысл только свидание с категорией — иначе оно снова
    # «активно» в списке, но невидимо гостям (частая путаница «не публикуется»).
    # Без категории уводим в редактор — там владелец её привяжет и сохранит.
    has_cat = conn.execute(
        "SELECT 1 FROM date_categories WHERE date_id=? LIMIT 1", (did,)).fetchone()
    if not has_cat:
        return redir(f"/admin/dates/{did}/edit",
                     "⚠ Добавь свидание хотя бы в одну категорию — иначе гости его не увидят")
    category_ids = [int(r["category_id"]) for r in conn.execute(
        "SELECT category_id FROM date_categories WHERE date_id=?", (did,)
    )]
    _validate_start_after_open_deadlines(conn, category_ids, d["starts_at"])
    conn.execute("UPDATE dates SET is_draft=0 WHERE id=?", (did,))
    conn.commit()
    # если это было предложение гостя — уведомим автора о публикации
    if d["origin"] == "guest" and d["proposed_by"]:
        cat = conn.execute(
            "SELECT c.name FROM categories c JOIN date_categories dc ON dc.category_id=c.id "
            "WHERE dc.date_id=? LIMIT 1", (did,)).fetchone()
        notify_user(bg, conn, d["proposed_by"], notify.card(
            "✅ Твоё предложение опубликовано",
            f"«{notify.esc(d['name'])}»" + (f" · {notify.esc(cat['name'])}" if cat else ""),
            "Теперь его видят гости ♥"))
    return redir(next, "Опубликовано — гости теперь видят это свидание")


@router.post("/dates/{did}/archive")
def date_archive(did: int, request: Request, next: str = Form("/admin/dates"),
                 conn=Depends(get_db)):
    d = _date_or_404(conn, did, request.state.user)
    _require_date_not_in_closed_vote(conn, did, "перенести свидание в архив")
    d = _date_or_404(conn, did, request.state.user)
    if d["archived_at"]:
        category_ids = [int(r["category_id"]) for r in conn.execute(
            "SELECT category_id FROM date_categories WHERE date_id=?", (did,)
        )]
        _validate_start_after_open_deadlines(conn, category_ids, d["starts_at"])
        conn.execute("UPDATE dates SET archived_at=NULL WHERE id=?", (did,))
        msg = "Возвращено из архива"
    else:
        for cat in conn.execute(
            "SELECT c.* FROM categories c JOIN date_categories dc ON dc.category_id=c.id "
            "WHERE dc.date_id=?", (did,),
        ).fetchall():
            affected_users = [int(r["user_id"]) for r in conn.execute(
                "SELECT DISTINCT user_id FROM bookings WHERE date_id=? AND category_id=? "
                "AND user_id IS NOT NULL", (did, cat["id"]),
            )]
            voting_events.queue_date_removed(
                conn, did, d["name"], cat["id"], cat["name"], cat["link_token"],
            )
            conn.execute("DELETE FROM bookings WHERE date_id=? AND category_id=?",
                         (did, cat["id"]))
            for user_id in affected_users:
                if not conn.execute(
                    "SELECT 1 FROM bookings WHERE category_id=? AND user_id=? LIMIT 1",
                    (cat["id"], user_id),
                ).fetchone():
                    voting_events.cancel_deadline_reminder(conn, cat["id"], user_id)
        conn.execute("UPDATE dates SET archived_at=? WHERE id=?", (now_iso(), did))
        msg = "Перенесено в архив"
    conn.commit()
    return redir(next, msg)


@router.post("/dates/{did}/delete")
def date_delete(did: int, request: Request, bg: BackgroundTasks, conn=Depends(get_db)):
    d = _date_or_404(conn, did, request.state.user)
    _require_date_not_in_closed_vote(conn, did, "удалить свидание")
    affected_by_cat: dict[int, list[int]] = {}
    for cat in conn.execute(
        "SELECT c.* FROM categories c JOIN date_categories dc ON dc.category_id=c.id "
        "WHERE dc.date_id=?", (did,),
    ).fetchall():
        affected_by_cat[int(cat["id"])] = [int(r["user_id"]) for r in conn.execute(
            "SELECT DISTINCT user_id FROM bookings WHERE date_id=? AND category_id=? "
            "AND user_id IS NOT NULL", (did, cat["id"]),
        )]
        voting_events.queue_date_removed(
            conn, did, d["name"], cat["id"], cat["name"], cat["link_token"],
        )
    files = [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_images WHERE date_id=?", (did,))]
    files += [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_videos WHERE date_id=?", (did,))]
    conn.execute("DELETE FROM dates WHERE id=?", (did,))
    for category_id, user_ids in affected_by_cat.items():
        for user_id in user_ids:
            if not conn.execute(
                "SELECT 1 FROM bookings WHERE category_id=? AND user_id=? LIMIT 1",
                (category_id, user_id),
            ).fetchone():
                voting_events.cancel_deadline_reminder(conn, category_id, user_id)
    conn.commit()
    for fn in files:                  # файлы — только после коммита
        images.delete_file(fn)
    actor = request.state.user["display_name"] or request.state.user["tg_username"] or "—"
    notify_admin(bg, conn, d["owner_id"], notify.card(
        "🗑 Удалено свидание",
        f"«{notify.esc(d['name'])}»",
        f"Кто: {notify.esc(actor)}"))
    return redir("/admin/dates", "Свидание удалено")


@router.post("/dates/{did}/clone")
def date_clone(did: int, request: Request, next: str = Form("/admin/dates"),
               conn=Depends(get_db)):
    """Дубль свидания: копируем запись, ссылки и файлы (с новыми именами на
    диске). Категории, брони и вопросы НЕ переносим — клон это свежее
    предложение без категории, поэтому он неактивен (гости не видят дубль),
    пока владелец не добавит его в категорию. Карта (place_url) уже распознана."""
    src = _date_or_404(conn, did, request.state.user)
    new_id = insert_date(
        conn, name=f"{src['name']} (копия)", place=src["place"],
        starts=src["starts_at"], ends=src["ends_at"], comment=src["comment"],
        origin="admin", guest_token=None, draft=1, owner_id=request.state.user["id"],
        pay_split=src["pay_split"], place_url=src["place_url"],
        capacity=src["capacity"])

    # ссылки и физические копии фото/видео — общий хелпер (его же зовёт «добавить
    # себе» по share-ссылке). При ошибке он сам подчистит осиротевшие копии.
    copy_date_media_and_links(conn, did, new_id)
    conn.commit()
    return redir(f"/admin/dates/{new_id}/edit",
                 "Свидание скопировано — оно неактивно, добавь его в категорию")


@router.post("/bookings/{bid}/delete")
def booking_delete(bid: int, request: Request, bg: BackgroundTasks,
                   next: str = Form("/admin/dates"), conn=Depends(get_db)):
    """Снять чужой выбор со свидания (например, по просьбе гостя)."""
    row = conn.execute(
        "SELECT b.id, b.user_id, b.category_id, d.name AS dname, c.name AS cname, "
        "c.link_token, "
        "COALESCE(g.name, 'человек') AS nm FROM bookings b "
        "JOIN dates d ON d.id=b.date_id "
        "JOIN categories c ON c.id=b.category_id "
        "LEFT JOIN guests g ON g.token=b.guest_token "
        "WHERE b.id=? AND d.owner_id=?", (bid, request.state.user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Выбор не найден")
    cat = conn.execute("SELECT * FROM categories WHERE id=(SELECT category_id FROM bookings WHERE id=?)",
                       (bid,)).fetchone()
    if cat:
        _require_category_composition_mutable(conn, cat)
    voting_events.queue_vote_removed_by_owner(
        conn, booking_id=row["id"], user_id=row["user_id"],
        category_id=row["category_id"], category_name=row["cname"],
        date_name=row["dname"], category_token=row["link_token"],
    )
    conn.execute("DELETE FROM bookings WHERE id=?", (bid,))
    if row["user_id"] is not None and not conn.execute(
        "SELECT 1 FROM bookings WHERE category_id=? AND user_id=? LIMIT 1",
        (row["category_id"], row["user_id"]),
    ).fetchone():
        voting_events.cancel_deadline_reminder(
            conn, row["category_id"], int(row["user_id"]),
        )
    conn.commit()
    return redir(next, f"Выбор снят — свидание снова свободно ({row['nm']})")


@router.post("/dates/{did}/videos/{vid}/delete")
def date_video_delete(did: int, vid: int, request: Request, conn=Depends(get_db)):
    _date_or_404(conn, did, request.state.user)
    row = conn.execute(
        "SELECT * FROM date_videos WHERE id=? AND date_id=?", (vid, did)).fetchone()
    if row:
        conn.execute("DELETE FROM date_videos WHERE id=?", (vid,))
        conn.commit()
        images.delete_file(row["filename"])
    return redir(f"/admin/dates/{did}/edit", "Видео удалено")


@router.post("/dates/{did}/images/{img_id}/delete")
def date_image_delete(did: int, img_id: int, request: Request, conn=Depends(get_db)):
    _date_or_404(conn, did, request.state.user)
    row = conn.execute(
        "SELECT * FROM date_images WHERE id=? AND date_id=?", (img_id, did)).fetchone()
    if row:
        conn.execute("DELETE FROM date_images WHERE id=?", (img_id,))
        conn.commit()
        images.delete_file(row["filename"])
    return redir(f"/admin/dates/{did}/edit", "Фото удалено")


@router.post("/dates/{did}/images/reorder")
def date_images_reorder(did: int, request: Request, order: str = Form(...),
                        conn=Depends(get_db)):
    """Drag-and-drop порядок фото: order — id через запятую, первое = обложка."""
    _date_or_404(conn, did, request.state.user)
    ids_db = [r["id"] for r in conn.execute(
        "SELECT id FROM date_images WHERE date_id=?", (did,))]
    try:
        ids = [int(x) for x in order.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "Некорректный порядок фото")
    if sorted(ids) != sorted(ids_db):
        raise HTTPException(400, "Некорректный порядок фото")
    for pos, iid in enumerate(ids):
        conn.execute("UPDATE date_images SET position=? WHERE id=?", (pos, iid))
    conn.commit()
    return JSONResponse({"ok": True})


@router.post("/dates/{did}/images/{img_id}/focus")
def date_image_focus(did: int, img_id: int, request: Request, focus: str = Form(...),
                     conn=Depends(get_db)):
    """Точка фокуса фото для обрезки в карточке: «X% Y%» (X,Y 0..100)."""
    import re as _re
    _date_or_404(conn, did, request.state.user)
    m = _re.fullmatch(r"\s*(\d{1,3})%\s+(\d{1,3})%\s*", focus or "")
    if not m or int(m.group(1)) > 100 or int(m.group(2)) > 100:
        raise HTTPException(400, "Некорректная точка фокуса")
    value = f"{int(m.group(1))}% {int(m.group(2))}%"
    row = conn.execute(
        "SELECT 1 FROM date_images WHERE id=? AND date_id=?", (img_id, did)).fetchone()
    if not row:
        raise HTTPException(404, "Фото не найдено")
    conn.execute("UPDATE date_images SET focus=? WHERE id=?", (value, img_id))
    conn.commit()
    return JSONResponse({"ok": True, "focus": value})


# ----- Вопросы ---------------------------------------------------------------

@router.get("/questions", response_class=HTMLResponse)
def questions_list(request: Request, conn=Depends(get_db)):
    f = request.query_params.get("f", "unread")
    where = "d.owner_id=?" + ("" if f == "all" else " AND q.is_read=0")
    rows = conn.execute(
        f"SELECT q.*, d.name AS date_name, d.id AS did, c.name AS cat_name, "
        f"{GNAME_SQL.format(t='q.guest_token')} AS gname "
        f"FROM questions q JOIN dates d ON d.id=q.date_id "
        f"LEFT JOIN categories c ON c.id=q.category_id "
        f"LEFT JOIN guests g ON g.token=q.guest_token "
        f"WHERE {where} ORDER BY q.created_at DESC",
        (request.state.user["id"],)
    ).fetchall()
    return templates.TemplateResponse(
        request, "admin/questions.html", actx(request, conn, active="q", rows=rows, f=f))


def _owned_question(conn, qid: int, uid: int):
    """Вопрос вместе с проверкой, что его свидание принадлежит владельцу.
    Подтягиваем имена свидания/категории для уведомления автору."""
    return conn.execute(
        "SELECT q.*, d.name AS date_name, c.name AS cat_name "
        "FROM questions q JOIN dates d ON d.id=q.date_id "
        "LEFT JOIN categories c ON c.id=q.category_id "
        "WHERE q.id=? AND d.owner_id=?", (qid, uid)).fetchone()


def _notify_answer(bg, conn, q, answer: str) -> None:
    """Уведомляет автора вопроса (если залогинен и бот подключён) об ответе."""
    notify_user(bg, conn, q["user_id"], notify.card(
        "💬 Ответ на твой вопрос",
        f"«{notify.esc(q['date_name'])}»"
        + (f" · {notify.esc(q['cat_name'])}" if q["cat_name"] else ""),
        f"Вопрос: {notify.esc(q['text'])}",
        f"\nОтвет: {notify.esc(answer)}"))


@router.post("/questions/{qid}/accept_time")
def question_accept_time(qid: int, request: Request, bg: BackgroundTasks,
                         next: str = Form("/admin/questions"),
                         conn=Depends(get_db)):
    """Принять предложенное гостем время: применяем его к свиданию."""
    q = _owned_question(conn, qid, request.state.user["id"])
    if not q or not q["suggest_starts"]:
        raise HTTPException(404, "Это не предложение времени")
    # Сериализуем с настройкой категории и проверяем время по тому же правилу,
    # что редактор свидания. Так DB-защита превращается в понятную ошибку формы,
    # а не в необработанный IntegrityError/500.
    if conn.execute("UPDATE dates SET id=id WHERE id=?", (q["date_id"],)).rowcount == 0:
        raise HTTPException(404, "Свидание не найдено")
    q = _owned_question(conn, qid, request.state.user["id"])
    if not q or not q["suggest_starts"]:
        raise HTTPException(404, "Это не предложение времени")
    category_ids = [int(r["category_id"]) for r in conn.execute(
        "SELECT category_id FROM date_categories WHERE date_id=?", (q["date_id"],)
    )]
    _validate_start_after_open_deadlines(conn, category_ids, q["suggest_starts"])
    old_period = conn.execute(
        "SELECT starts_at, ends_at FROM dates WHERE id=?", (q["date_id"],)
    ).fetchone()
    answer = "✅ Принято! Время назначено ♥"
    conn.execute("UPDATE dates SET starts_at=?, ends_at=? WHERE id=?",
                 (q["suggest_starts"], q["suggest_ends"], q["date_id"]))
    if (old_period["starts_at"], old_period["ends_at"]) != \
            (q["suggest_starts"], q["suggest_ends"]):
        voting_events.queue_date_changed(conn, q["date_id"], ["дата или время"])
    conn.execute(
        "UPDATE questions SET answer=?, answered_at=?, is_read=1 WHERE id=?",
        (answer, now_iso(), qid))
    conn.commit()
    _notify_answer(bg, conn, q, answer)
    return redir(next, "Время назначено ♥")


@router.post("/questions/{qid}/decline_time")
def question_decline_time(qid: int, request: Request, bg: BackgroundTasks,
                          next: str = Form("/admin/questions"),
                          conn=Depends(get_db)):
    """Вежливо отказаться от предложенного времени (автор увидит ответ)."""
    q = _owned_question(conn, qid, request.state.user["id"])
    if not q or not q["suggest_starts"]:
        raise HTTPException(404, "Это не предложение времени")
    answer = "🥺 Это время не получится — предложи, пожалуйста, другое"
    conn.execute(
        "UPDATE questions SET answer=?, answered_at=?, is_read=1 WHERE id=?",
        (answer, now_iso(), qid))
    conn.commit()
    _notify_answer(bg, conn, q, answer)
    return redir(next, "Отказ отправлен — автор увидит его на странице")


@router.post("/questions/{qid}/answer")
def question_answer(qid: int, request: Request, bg: BackgroundTasks,
                    text: str = Form(""),
                    next: str = Form("/admin/questions"), conn=Depends(get_db)):
    q = _owned_question(conn, qid, request.state.user["id"])
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    text = clean_text(text, 2000, "Ответ")
    if text:
        conn.execute("UPDATE questions SET answer=?, answered_at=?, is_read=1 WHERE id=?",
                     (text, now_iso(), qid))
        conn.commit()
        _notify_answer(bg, conn, q, text)
        msg = "Ответ сохранён — автор вопроса увидит его на странице категории"
    else:
        conn.execute("UPDATE questions SET answer=NULL, answered_at=NULL WHERE id=?", (qid,))
        conn.commit()
        msg = "Ответ удалён"
    return redir(next, msg)


@router.post("/questions/{qid}/toggle")
def question_toggle(qid: int, request: Request, next: str = Form("/admin/questions"),
                    conn=Depends(get_db)):
    q = _owned_question(conn, qid, request.state.user["id"])
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    conn.execute("UPDATE questions SET is_read=? WHERE id=?",
                 (0 if q["is_read"] else 1, qid))
    conn.commit()
    return redir(next)


@router.post("/questions/{qid}/delete")
def question_delete(qid: int, request: Request, next: str = Form("/admin/questions"),
                    conn=Depends(get_db)):
    q = _owned_question(conn, qid, request.state.user["id"])
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    conn.execute("DELETE FROM questions WHERE id=?", (qid,))
    conn.commit()
    return redir(next, "Вопрос удалён")


# ---------------------------------------------------------------------------
# Публичная страница профиля /u/<id> — доступна ЛЮБОМУ залогиненному.
# Незалогиненный сюда не попадёт (current_user → NeedLogin → /login).
# Решение владельца: показываем имя, фото, полную дату рождения и пол.
# (Privacy-пометка про полную ДР — в Политике конфиденциальности.)
# ---------------------------------------------------------------------------
user_router = APIRouter(prefix="/u", dependencies=[Depends(current_user)])


@user_router.get("/{user_id}", response_class=HTMLResponse)
def public_profile(user_id: int, request: Request, conn=Depends(get_db)):
    u = conn.execute(
        "SELECT id, display_name, avatar_path, birth_date, gender, tg_username "
        "FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
    if not u:
        raise HTTPException(404, "Профиль не найден")
    # Публичные активные свидания пользователя — то же, что видно в общей ленте.
    # Каждое ведёт на свою страницу-шаринг /d/<share_token>.
    date_rows = conn.execute(
        "SELECT id, name, share_token, starts_at, ends_at, place FROM dates "
        "WHERE owner_id=? AND is_public=1 AND is_draft=0 AND archived_at IS NULL "
        "ORDER BY id DESC", (user_id,)).fetchall()
    media = public_routes._batch_media(conn, [r["id"] for r in date_rows])
    dates = [public_routes.date_payload_from(r, media) for r in date_rows]
    return templates.TemplateResponse(
        request, "public/profile.html",
        {"request": request, "u": u, "dates": dates,
         "is_me": u["id"] == request.state.user["id"]})


@user_router.get("/{user_id}/avatar")
def public_avatar(user_id: int, request: Request, conn=Depends(get_db)):
    """Аватар по id пользователя — для страницы /u/<id>. Гейт логина уже на
    роутере; отдаём только активным пользователям."""
    row = conn.execute(
        "SELECT avatar_path FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
    fn = row["avatar_path"] if row else None
    if not fn or not images.SAFE_FILENAME.match(fn):
        raise HTTPException(404)
    path = images.UPLOAD_DIR / fn
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=300"})
