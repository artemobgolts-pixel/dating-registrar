"""Админка: вход, дашборд, категории, свидания, вопросы, экспорт.

Доступ — сессия (SessionMiddleware) + CSRF-токен на каждый POST.
"""

import csv
import io
import json
import os
import secrets
import tempfile
import zipfile

import segno

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Request, UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from urllib.parse import urlencode

import backup
import images
import notify
import places
import settings as app_settings
from config import BASE_URL, SUPPORT_CONTACT
from guests import gname
from helpers import (clean_text, normalize_period, now_iso, now_naive,
                     parse_birth_date, parse_dt_local, parse_links)
from fastapi.responses import JSONResponse
from ownership import get_owned_category, get_owned_date
from public_routes import (add_photos, insert_date, next_cat_pos, notify_admin,
                           notify_user, ranged_file, save_links, VIDEO_TYPES)
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
    return templates.TemplateResponse(
        request, "admin/profile.html", actx(request, conn, active="profile"))


@router.post("/profile")
def profile_save(request: Request,
                 display_name: str = Form(""),
                 birth_date: str = Form(""),
                 gender: str = Form(""),
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
    if new_avatar:
        conn.execute(
            "UPDATE users SET display_name=?, birth_date=?, gender=?, avatar_path=? "
            "WHERE id=?", (name, bdate, g, new_avatar, uid))
    else:
        conn.execute(
            "UPDATE users SET display_name=?, birth_date=?, gender=? WHERE id=?",
            (name, bdate, g, uid))
    conn.commit()
    if new_avatar and old_avatar:        # старый аватар — только после коммита
        images.delete_file(old_avatar)
    return redir("/admin/profile", "Профиль сохранён ♥")


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

FEED_SQL = f"""
SELECT * FROM (
    SELECT 'book' AS kind, b.created_at AS created_at,
           {GNAME_SQL.format(t='b.guest_token')} AS gname,
           d.id AS date_id, d.name AS date_name, c.name AS cat_name, NULL AS text
    FROM bookings b
    JOIN dates d ON d.id = b.date_id
    JOIN categories c ON c.id = b.category_id
    LEFT JOIN guests g ON g.token = b.guest_token
    WHERE d.owner_id = :uid
  UNION ALL
    SELECT 'question', q.created_at,
           {GNAME_SQL.format(t='q.guest_token')},
           d.id, d.name, IFNULL(c.name, '—'), q.text
    FROM questions q
    JOIN dates d ON d.id = q.date_id
    LEFT JOIN categories c ON c.id = q.category_id
    LEFT JOIN guests g ON g.token = q.guest_token
    WHERE d.owner_id = :uid
  UNION ALL
    SELECT 'proposal', d.created_at,
           {GNAME_SQL.format(t='d.guest_token')},
           d.id, d.name,
           IFNULL((SELECT c2.name FROM date_categories dc2
                   JOIN categories c2 ON c2.id = dc2.category_id
                   WHERE dc2.date_id = d.id LIMIT 1), '—'),
           NULL
    FROM dates d
    LEFT JOIN guests g ON g.token = d.guest_token
    WHERE d.origin = 'guest' AND d.owner_id = :uid
)
ORDER BY created_at DESC
LIMIT 50
"""


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn=Depends(get_db)):
    autoarchive_once(conn)
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
    feed = conn.execute(FEED_SQL, {"uid": uid}).fetchall()

    # Блок «Поделиться»: категории владельца с включённой секретной ссылкой.
    # Для выбранной (или первой) рисуем QR прямо на сервере — инлайновый SVG,
    # под CSP не нужен ни внешний скрипт, ни data:-картинка.
    share_cats = conn.execute(
        "SELECT id, name, link_token, og_title, og_desc FROM categories "
        "WHERE owner_id=? AND link_enabled=1 AND link_token IS NOT NULL "
        "ORDER BY created_at DESC", (uid,)).fetchall()
    sel = request.query_params.get("share")
    share = next((c for c in share_cats if str(c["id"]) == sel), None) \
        or (share_cats[0] if share_cats else None)
    share_url = qr_svg = None
    if share:
        share_url = f"{BASE_URL}/c/{share['link_token']}"
        qr_svg = _qr_svg(share_url)

    return templates.TemplateResponse(
        request, "admin/dashboard.html",
        actx(request, conn, active="dash", stats=stats, feed=feed,
             share_cats=share_cats, share=share, share_url=share_url, qr_svg=qr_svg))


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


@router.get("/uploads/{filename}")
def admin_image(filename: str, request: Request, conn=Depends(get_db)):
    if not images.SAFE_FILENAME.match(filename):
        raise HTTPException(404)
    # файл виден, только если принадлежит свиданию владельца (через фото или видео)
    uid = request.state.user["id"]
    owns = conn.execute(
        "SELECT 1 FROM date_images di JOIN dates d ON d.id=di.date_id "
        "WHERE di.filename=? AND d.owner_id=? "
        "UNION ALL "
        "SELECT 1 FROM date_videos dv JOIN dates d ON d.id=dv.date_id "
        "WHERE dv.filename=? AND d.owner_id=? LIMIT 1",
        (filename, uid, filename, uid)).fetchone()
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
    w.writerow(["id", "Название", "Место", "Начало", "Конец", "50/50", "Черновик",
                "Архив", "Источник", "Выборы", "Кто выбрал", "Категории", "Ссылки"])
    for d in data["dates"]:
        w.writerow([
            d["id"], d["name"], d["place"] or "", d["starts_at"] or "", d["ends_at"] or "",
            "да" if d["pay_split"] else "",
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
        token = secrets.token_urlsafe(24)
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
        did = insert_date(
            conn, name=name, place=d.get("place"), starts=d.get("starts_at"),
            ends=d.get("ends_at"), comment=d.get("comment"),
            origin="admin", guest_token=None, owner_id=uid,
            draft=1 if d.get("is_draft") else 0,
            pay_split=1 if d.get("pay_split") else 0, place_url=d.get("place_url"))
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
        "SELECT c.*, (SELECT COUNT(*) FROM date_categories dc WHERE dc.category_id=c.id) AS dcount "
        "FROM categories c WHERE c.owner_id=? ORDER BY c.created_at DESC",
        (request.state.user["id"],)
    ).fetchall()
    return templates.TemplateResponse(
        request, "admin/categories.html", actx(request, conn, active="cats", cats=cats))


@router.post("/categories/create")
def category_create(request: Request, bg: BackgroundTasks, name: str = Form(...),
                    conn=Depends(get_db)):
    name = clean_text(name, 200, "Название", required=True)
    token = secrets.token_urlsafe(24)
    # мягкая очередь: при включённой модерации категорий новая помечается
    # is_reviewed=0 (ссылка работает сразу, админ просто видит её в очереди).
    reviewed = 0 if app_settings.is_on(conn, app_settings.MODERATE_CATEGORIES) else 1
    conn.execute(
        "INSERT INTO categories(owner_id, name, link_token, link_enabled, "
        "moderate_proposals, is_reviewed, created_at) VALUES(?,?,?,1,1,?,?)",
        (request.state.user["id"], name, token, reviewed, now_iso()))
    conn.commit()
    actor = request.state.user["display_name"] or request.state.user["tg_username"] or "—"
    notify_admin(bg, conn, request.state.user["id"], notify.card(
        "🆕 Создана категория",
        f"«{notify.esc(name)}»",
        f"Кто: {notify.esc(actor)}"))
    return redir("/admin/categories", "Категория создана")


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
    dates = conn.execute(
        "SELECT d.*, "
        "(SELECT COUNT(*) FROM bookings b WHERE b.date_id=d.id AND b.category_id=?) AS books, "
        "(SELECT GROUP_CONCAT(COALESCE(g.name, '#' || substr(b.guest_token,1,6)), ', ') "
        " FROM bookings b LEFT JOIN guests g ON g.token=b.guest_token "
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
    return templates.TemplateResponse(
        request, "admin/category_detail.html",
        actx(request, conn, active="cats", cat=cat, dates=dates, attachable=attachable))


@router.post("/categories/{cid}/rename")
def category_rename(cid: int, request: Request, name: str = Form(...),
                    description: str = Form(""),
                    og_title: str = Form(""), og_desc: str = Form(""),
                    conn=Depends(get_db)):
    _cat_or_404(conn, cid, request.state.user)
    name = clean_text(name, 200, "Название", required=True)
    description = clean_text(description, 1000, "Описание")
    og_title = clean_text(og_title, 120, "Заголовок превью")
    og_desc = clean_text(og_desc, 200, "Описание превью")
    conn.execute("UPDATE categories SET name=?, description=?, og_title=?, og_desc=? WHERE id=?",
                 (name, description, og_title, og_desc, cid))
    conn.commit()
    return redir(f"/admin/categories/{cid}", "Сохранено")


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
                 "Предложения гостей теперь попадают на модерацию (вкладка «Черновики»)"
                 if new_val else "Предложения гостей теперь публикуются сразу")


@router.post("/categories/{cid}/regenerate")
def category_regenerate(cid: int, request: Request, conn=Depends(get_db)):
    _cat_or_404(conn, cid, request.state.user)
    token = secrets.token_urlsafe(24)
    conn.execute("UPDATE categories SET link_token=?, link_enabled=1 WHERE id=?", (token, cid))
    conn.commit()
    return redir(f"/admin/categories/{cid}",
                 "Новая ссылка сгенерирована. Старая больше не работает, все данные сохранены.")


@router.post("/categories/{cid}/delete")
def category_delete(cid: int, request: Request, bg: BackgroundTasks, conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid, request.state.user)
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    conn.commit()
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
    # привязать можно только свидание ТОГО ЖЕ владельца, что и категория, —
    # иначе чужое утечёт в категорию (для админа контекст = владелец категории)
    get_owned_date(conn, date_id, cat["owner_id"])
    conn.execute(
        "INSERT OR IGNORE INTO date_categories(date_id, category_id, position) "
        "VALUES(?,?,?)", (date_id, cid, next_cat_pos(conn, cid)))
    conn.commit()
    return redir(f"/admin/categories/{cid}", "Свидание добавлено в категорию")


@router.post("/categories/{cid}/dates_reorder")
def category_dates_reorder(cid: int, request: Request, order: str = Form(...),
                           conn=Depends(get_db)):
    """Drag-and-drop порядок свиданий: order — id через запятую."""
    _cat_or_404(conn, cid, request.state.user)
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
    _cat_or_404(conn, cid, request.state.user)
    conn.execute("DELETE FROM date_categories WHERE date_id=? AND category_id=?", (date_id, cid))
    conn.execute("DELETE FROM bookings WHERE date_id=? AND category_id=?", (date_id, cid))
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
             flt=flt, cat=cat, cats=_all_cats(conn, request.state.user["id"]), drafts_n=drafts_n,
             qs_keep=qs_keep, page=page, pages=pages, layout=layout))


def _all_cats(conn, uid: int):
    return conn.execute(
        "SELECT id, name FROM categories WHERE owner_id=? ORDER BY created_at DESC",
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


@router.get("/dates/new", response_class=HTMLResponse)
def date_new_form(request: Request, conn=Depends(get_db)):
    checked = set()
    pre = request.query_params.get("category")
    if pre and pre.isdigit():
        checked.add(int(pre))
    return templates.TemplateResponse(
        request, "admin/date_form.html",
        actx(request, conn, active="dates", date=None, photos=[], videos=[], links_text="",
             cats=_all_cats(conn, request.state.user["id"]), checked=checked, slots=images.MAX_IMAGES))


@router.post("/dates/new")
def date_create(request: Request, bg: BackgroundTasks,
                name: str = Form(...), place: str = Form(""),
                starts_at: str = Form(""), ends_at: str = Form(""),
                links: str = Form(""), comment: str = Form(""),
                draft: str | None = Form(None), pay: str | None = Form(None),
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

    date_id = insert_date(conn, name=name, place=place, starts=starts, ends=ends,
                          comment=comment, origin="admin", guest_token=None,
                          owner_id=uid,
                          draft=1 if draft else 0,
                          pay_split=1 if pay else 0, place_url=place_url)
    # привязываем только СВОИ категории (чужие id из формы молча игнорируем)
    own_cats = {r[0] for r in conn.execute(
        "SELECT id FROM categories WHERE owner_id=?", (uid,))}
    for cid in categories:
        if cid in own_cats:
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
        "COALESCE(g.name, '#' || substr(b.guest_token,1,6)) AS name, c.name AS cat "
        "FROM bookings b LEFT JOIN guests g ON g.token=b.guest_token "
        "JOIN categories c ON c.id=b.category_id WHERE b.date_id=? ORDER BY b.created_at",
        (did,)).fetchall()
    proposer = gname(conn, d["guest_token"]) if d["origin"] == "guest" else None
    return templates.TemplateResponse(
        request, "admin/date_form.html",
        actx(request, conn, active="dates", date=d, photos=photos, videos=videos,
             booked=booked,
             proposer=proposer,
             links_text="\n".join(r["url"] for r in link_rows),
             # категории показываем владельца свидания (админ может править чужое)
             cats=_all_cats(conn, d["owner_id"]), checked=checked,
             slots=images.MAX_IMAGES - len(photos)))


@router.post("/dates/{did}/edit")
def date_update(did: int, request: Request, bg: BackgroundTasks, name: str = Form(...),
                place: str = Form(""),
                starts_at: str = Form(""), ends_at: str = Form(""),
                links: str = Form(""), comment: str = Form(""),
                draft: str | None = Form(None), pay: str | None = Form(None),
                categories: list[int] = Form(default=[]),
                photos: list[UploadFile] = File(default=[], alias="images"),
                videos: list[UploadFile] = File(default=[], alias="videos"),
                image_focuses: str = Form(""),
                conn=Depends(get_db)):
    d = _date_or_404(conn, did, request.state.user)
    user_throttle("dateedit", request.state.user["id"], request)
    name = clean_text(name, 200, "Название", required=True)
    place, place_url, needs_resolve = places.place_on_edit(
        clean_text(place, 500, "Место"), d)
    comment = clean_text(comment, 2000, "Комментарий")
    starts, ends = normalize_period(parse_dt_local(starts_at), parse_dt_local(ends_at))
    link_list = parse_links(links)

    conn.execute(
        "UPDATE dates SET name=?, place=?, place_url=?, starts_at=?, ends_at=?, "
        "comment=?, is_draft=?, pay_split=? WHERE id=?",
        (name, place, place_url, starts, ends, comment,
         1 if draft else 0, 1 if pay else 0, did))

    # привязываем только категории владельца свидания (чужие id из формы молча
    # игнорируем). Для админа, правящего чужое, контекст = владелец свидания.
    own_cats = {r[0] for r in conn.execute(
        "SELECT id FROM categories WHERE owner_id=?", (d["owner_id"],))}
    categories = [c for c in categories if c in own_cats]
    # Синхронизируем категории; выборы по отвязанным категориям удаляем
    conn.execute("DELETE FROM date_categories WHERE date_id=?", (did,))
    if categories:
        for cid in categories:
            conn.execute(
                "INSERT OR IGNORE INTO date_categories(date_id, category_id, position) "
                "VALUES(?,?,?)", (did, cid, next_cat_pos(conn, cid)))
        placeholders = ",".join("?" * len(categories))
        conn.execute(
            f"DELETE FROM bookings WHERE date_id=? AND category_id NOT IN ({placeholders})",
            (did, *categories))
    else:
        conn.execute("DELETE FROM bookings WHERE date_id=?", (did,))

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
    conn.commit()
    if needs_resolve:
        bg.add_task(places.resolve_into_db, did, place_url)
    return redir(f"/admin/dates/{did}/edit", "Сохранено")


@router.post("/dates/{did}/publish")
def date_publish(did: int, request: Request, bg: BackgroundTasks,
                 next: str = Form("/admin/dates"), conn=Depends(get_db)):
    d = _date_or_404(conn, did, request.state.user)
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
    if d["archived_at"]:
        conn.execute("UPDATE dates SET archived_at=NULL WHERE id=?", (did,))
        msg = "Возвращено из архива"
    else:
        conn.execute("UPDATE dates SET archived_at=? WHERE id=?", (now_iso(), did))
        msg = "Перенесено в архив"
    conn.commit()
    return redir(next, msg)


@router.post("/dates/{did}/delete")
def date_delete(did: int, request: Request, bg: BackgroundTasks, conn=Depends(get_db)):
    d = _date_or_404(conn, did, request.state.user)
    files = [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_images WHERE date_id=?", (did,))]
    files += [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_videos WHERE date_id=?", (did,))]
    conn.execute("DELETE FROM dates WHERE id=?", (did,))
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
    """Дубль свидания: копируем запись, ссылки, категории и файлы (с новыми
    именами на диске). Брони и вопросы НЕ переносим — клон это свежее
    предложение. Клон создаётся черновиком, чтобы гости не увидели дубль
    раньше времени. Карта (place_url) уже распознана — резолвить не нужно."""
    src = _date_or_404(conn, did, request.state.user)
    new_id = insert_date(
        conn, name=f"{src['name']} (копия)", place=src["place"],
        starts=src["starts_at"], ends=src["ends_at"], comment=src["comment"],
        origin="admin", guest_token=None, draft=1, owner_id=request.state.user["id"],
        pay_split=src["pay_split"], place_url=src["place_url"])

    # категории — в конец каждого списка
    for cid in [r[0] for r in conn.execute(
            "SELECT category_id FROM date_categories WHERE date_id=?", (did,))]:
        conn.execute(
            "INSERT OR IGNORE INTO date_categories(date_id, category_id, position) "
            "VALUES(?,?,?)", (new_id, cid, next_cat_pos(conn, cid)))

    # ссылки
    for r in conn.execute(
            "SELECT url, position FROM date_links WHERE date_id=? ORDER BY position, id",
            (did,)).fetchall():
        conn.execute("INSERT INTO date_links(date_id, url, position) VALUES(?,?,?)",
                     (new_id, r["url"], r["position"]))

    # фото и видео — физические копии файлов; битые/пропавшие просто пропускаем
    copied: list[str] = []
    try:
        for r in conn.execute(
                "SELECT filename, position, focus FROM date_images WHERE date_id=? "
                "ORDER BY position, id", (did,)).fetchall():
            fn = images.copy_file(r["filename"])
            if fn:
                copied.append(fn)
                conn.execute(
                    "INSERT INTO date_images(date_id, filename, position, focus) VALUES(?,?,?,?)",
                    (new_id, fn, r["position"], r["focus"]))
        for r in conn.execute(
                "SELECT filename, position FROM date_videos WHERE date_id=? "
                "ORDER BY position, id", (did,)).fetchall():
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
    conn.commit()
    return redir(f"/admin/dates/{new_id}/edit",
                 "Свидание скопировано — это черновик, проверь и опубликуй")


@router.post("/bookings/{bid}/delete")
def booking_delete(bid: int, request: Request, bg: BackgroundTasks,
                   next: str = Form("/admin/dates"), conn=Depends(get_db)):
    """Снять чужой выбор со свидания (например, по просьбе гостя)."""
    row = conn.execute(
        "SELECT b.id, b.user_id, d.name AS dname, c.name AS cname, "
        "COALESCE(g.name, 'человек') AS nm FROM bookings b "
        "JOIN dates d ON d.id=b.date_id "
        "JOIN categories c ON c.id=b.category_id "
        "LEFT JOIN guests g ON g.token=b.guest_token "
        "WHERE b.id=? AND d.owner_id=?", (bid, request.state.user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Выбор не найден")
    conn.execute("DELETE FROM bookings WHERE id=?", (bid,))
    conn.commit()
    notify_user(bg, conn, row["user_id"], notify.card(
        "ℹ️ Твой выбор снят",
        f"«{notify.esc(row['dname'])}» · {notify.esc(row['cname'])}",
        "Свидание снова свободно — можно выбрать другое ♥"))
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
    answer = "✅ Принято! Время назначено ♥"
    conn.execute("UPDATE dates SET starts_at=?, ends_at=? WHERE id=?",
                 (q["suggest_starts"], q["suggest_ends"], q["date_id"]))
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
    return templates.TemplateResponse(
        request, "public/profile.html",
        {"request": request, "u": u, "is_me": u["id"] == request.state.user["id"]})


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
