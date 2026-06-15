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
import places
from config import ADMIN_PASSWORD, ADMIN_USERNAME, BASE_URL
from guests import gname
from helpers import clean_text, normalize_period, now_iso, now_naive, parse_dt_local, parse_links
from fastapi.responses import JSONResponse
from public_routes import (add_photos, insert_date, next_cat_pos, ranged_file,
                           save_links, VIDEO_TYPES)
from ratelimit import _login_fails, _register_fail, _throttle_ok, client_ip
from tasks import autoarchive_once
from web import get_db, redir, templates


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------

class NeedLogin(Exception):
    pass


async def require_admin(request: Request):
    if not request.session.get("admin"):
        raise NeedLogin()
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(16)
    if request.method == "POST":
        form = await request.form()
        token = str(form.get("csrf") or "")
        good = request.session.get("csrf") or ""
        if not (token and secrets.compare_digest(token, good)):
            raise HTTPException(403, "Сессия устарела — обнови страницу и попробуй ещё раз")


auth_router = APIRouter()       # /admin/login — без require_admin


@auth_router.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("admin"):
        return RedirectResponse("/admin/", status_code=303)
    return templates.TemplateResponse(request, "admin/login.html", {"error": None})


@auth_router.post("/admin/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(""), password: str = Form("")):
    ip = client_ip(request)
    if not _throttle_ok(ip):
        return templates.TemplateResponse(
            request, "admin/login.html",
            {"error": "Слишком много попыток. Подожди 15 минут."},
            status_code=429)

    ok = secrets.compare_digest(username.encode(), ADMIN_USERNAME.encode()) and \
        secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode())

    if ok:
        request.session["admin"] = True
        request.session["csrf"] = secrets.token_urlsafe(16)
        _login_fails.pop(ip, None)
        return RedirectResponse("/admin/", status_code=303)

    _register_fail(ip)
    return templates.TemplateResponse(
        request, "admin/login.html",
        {"error": "Неверный логин или пароль"},
        status_code=401)


# ---------------------------------------------------------------------------
# Защищённая часть
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def actx(request: Request, conn, **extra) -> dict:
    ctx = {
        "request": request,
        "unread": conn.execute("SELECT COUNT(*) FROM questions WHERE is_read=0").fetchone()[0],
        "csrf": request.session.get("csrf", ""),
    }
    ctx.update(extra)
    return ctx


@router.post("/logout")
def logout(request: Request):
    """Выход только по POST с CSRF: logout по GET можно навязать ссылкой."""
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


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
  UNION ALL
    SELECT 'question', q.created_at,
           {GNAME_SQL.format(t='q.guest_token')},
           d.id, d.name, IFNULL(c.name, '—'), q.text
    FROM questions q
    JOIN dates d ON d.id = q.date_id
    LEFT JOIN categories c ON c.id = q.category_id
    LEFT JOIN guests g ON g.token = q.guest_token
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
    WHERE d.origin = 'guest'
)
ORDER BY created_at DESC
LIMIT 50
"""


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn=Depends(get_db)):
    autoarchive_once(conn)
    stats = {
        "cats": conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0],
        "active": conn.execute(
            "SELECT COUNT(*) FROM dates WHERE archived_at IS NULL AND is_draft=0").fetchone()[0],
        "drafts": conn.execute(
            "SELECT COUNT(*) FROM dates WHERE archived_at IS NULL AND is_draft=1").fetchone()[0],
        "archived": conn.execute(
            "SELECT COUNT(*) FROM dates WHERE archived_at IS NOT NULL").fetchone()[0],
        # выборы считаем только по активным (не архивным) свиданиям
        "bookings": conn.execute(
            "SELECT COUNT(*) FROM bookings b JOIN dates d ON d.id=b.date_id "
            "WHERE d.archived_at IS NULL").fetchone()[0],
        "unread_q": conn.execute(
            "SELECT COUNT(*) FROM questions WHERE is_read=0").fetchone()[0],
    }
    feed = conn.execute(FEED_SQL).fetchall()

    # Блок «Поделиться»: категории с включённой секретной ссылкой.
    # Для выбранной (или первой) рисуем QR прямо на сервере — инлайновый SVG,
    # под CSP не нужен ни внешний скрипт, ни data:-картинка.
    share_cats = conn.execute(
        "SELECT id, name, link_token FROM categories "
        "WHERE link_enabled=1 AND link_token IS NOT NULL ORDER BY created_at DESC"
    ).fetchall()
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
def admin_image(filename: str, request: Request):
    if not images.SAFE_FILENAME.match(filename):
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

def _full_dump(conn) -> dict:
    cats = [dict(r) for r in conn.execute("SELECT * FROM categories ORDER BY id")]
    guests = [dict(r) for r in conn.execute("SELECT * FROM guests ORDER BY created_at")]
    out_dates = []
    for r in conn.execute("SELECT * FROM dates ORDER BY id").fetchall():
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
        out_dates.append(d)
    questions = [dict(r) for r in conn.execute("SELECT * FROM questions ORDER BY id")]
    return {"exported_at": now_iso(), "categories": cats, "guests": guests,
            "dates": out_dates, "questions": questions}


@router.get("/export/json")
def export_json(conn=Depends(get_db)):
    data = _full_dump(conn)
    day = now_naive().strftime("%Y-%m-%d")
    return Response(json.dumps(data, ensure_ascii=False, indent=2),
                    media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="date4you-export-{day}.json"'})


@router.get("/export/csv")
def export_csv(conn=Depends(get_db)):
    data = _full_dump(conn)
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
def export_archive(conn=Depends(get_db)):
    """Полный архив: консистентный снимок базы + все фото + export.json."""
    data = _full_dump(conn)
    snap = backup.make_backup()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(snap, arcname="app.db")
        z.writestr("export.json", json.dumps(data, ensure_ascii=False, indent=2))
        for pat in ("*.webp", "*.mp4", "*.webm"):
            for p in sorted(images.UPLOAD_DIR.glob(pat)):
                z.write(p, arcname=f"uploads/{p.name}")
    day = now_naive().strftime("%Y-%m-%d")
    return FileResponse(tmp.name, media_type="application/zip",
                        filename=f"date4you-export-{day}.zip",
                        background=BackgroundTask(os.unlink, tmp.name))


# ----- Категории -----------------------------------------------------------

@router.get("/categories", response_class=HTMLResponse)
def categories_list(request: Request, conn=Depends(get_db)):
    cats = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM date_categories dc WHERE dc.category_id=c.id) AS dcount "
        "FROM categories c ORDER BY c.created_at DESC"
    ).fetchall()
    return templates.TemplateResponse(
        request, "admin/categories.html", actx(request, conn, active="cats", cats=cats))


@router.post("/categories/create")
def category_create(name: str = Form(...), conn=Depends(get_db)):
    name = clean_text(name, 200, "Название", required=True)
    token = secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO categories(name, link_token, link_enabled, created_at) VALUES(?,?,1,?)",
        (name, token, now_iso()))
    conn.commit()
    return redir("/admin/categories", "Категория создана")


def _cat_or_404(conn, cid: int):
    cat = conn.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
    if not cat:
        raise HTTPException(404, "Категория не найдена")
    return cat


@router.get("/categories/{cid}", response_class=HTMLResponse)
def category_detail(cid: int, request: Request, conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid)
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
    attachable = conn.execute(
        "SELECT id, name FROM dates WHERE archived_at IS NULL AND id NOT IN "
        "(SELECT date_id FROM date_categories WHERE category_id=?) ORDER BY created_at DESC",
        (cid,)).fetchall()
    return templates.TemplateResponse(
        request, "admin/category_detail.html",
        actx(request, conn, active="cats", cat=cat, dates=dates, attachable=attachable))


@router.post("/categories/{cid}/rename")
def category_rename(cid: int, name: str = Form(...), description: str = Form(""),
                    conn=Depends(get_db)):
    _cat_or_404(conn, cid)
    name = clean_text(name, 200, "Название", required=True)
    description = clean_text(description, 1000, "Описание")
    conn.execute("UPDATE categories SET name=?, description=? WHERE id=?",
                 (name, description, cid))
    conn.commit()
    return redir(f"/admin/categories/{cid}", "Сохранено")


@router.post("/categories/{cid}/toggle")
def category_toggle(cid: int, conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid)
    new_val = 0 if cat["link_enabled"] else 1
    conn.execute("UPDATE categories SET link_enabled=? WHERE id=?", (new_val, cid))
    conn.commit()
    return redir(f"/admin/categories/{cid}",
                 "Ссылка включена" if new_val else "Ссылка отключена")


@router.post("/categories/{cid}/moderation")
def category_moderation(cid: int, conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid)
    new_val = 0 if cat["moderate_proposals"] else 1
    conn.execute("UPDATE categories SET moderate_proposals=? WHERE id=?", (new_val, cid))
    conn.commit()
    return redir(f"/admin/categories/{cid}",
                 "Предложения гостей теперь попадают на модерацию (вкладка «Черновики»)"
                 if new_val else "Предложения гостей теперь публикуются сразу")


@router.post("/categories/{cid}/regenerate")
def category_regenerate(cid: int, conn=Depends(get_db)):
    _cat_or_404(conn, cid)
    token = secrets.token_urlsafe(24)
    conn.execute("UPDATE categories SET link_token=?, link_enabled=1 WHERE id=?", (token, cid))
    conn.commit()
    return redir(f"/admin/categories/{cid}",
                 "Новая ссылка сгенерирована. Старая больше не работает, все данные сохранены.")


@router.post("/categories/{cid}/delete")
def category_delete(cid: int, conn=Depends(get_db)):
    _cat_or_404(conn, cid)
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    conn.commit()
    return redir("/admin/categories", "Категория удалена (свидания остались)")


@router.post("/categories/{cid}/attach")
def category_attach(cid: int, date_id: int = Form(...), conn=Depends(get_db)):
    _cat_or_404(conn, cid)
    if not conn.execute("SELECT 1 FROM dates WHERE id=?", (date_id,)).fetchone():
        raise HTTPException(404, "Свидание не найдено")
    conn.execute(
        "INSERT OR IGNORE INTO date_categories(date_id, category_id, position) "
        "VALUES(?,?,?)", (date_id, cid, next_cat_pos(conn, cid)))
    conn.commit()
    return redir(f"/admin/categories/{cid}", "Свидание добавлено в категорию")


@router.post("/categories/{cid}/dates_reorder")
def category_dates_reorder(cid: int, order: str = Form(...), conn=Depends(get_db)):
    """Drag-and-drop порядок свиданий: order — id через запятую."""
    _cat_or_404(conn, cid)
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
def category_detach(cid: int, date_id: int = Form(...), conn=Depends(get_db)):
    _cat_or_404(conn, cid)
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
    params: list = []
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
        "SELECT COUNT(*) FROM dates WHERE archived_at IS NULL AND is_draft=1").fetchone()[0]

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
             flt=flt, cat=cat, cats=_all_cats(conn), drafts_n=drafts_n,
             qs_keep=qs_keep, page=page, pages=pages, layout=layout))


def _all_cats(conn):
    return conn.execute("SELECT id, name FROM categories ORDER BY created_at DESC").fetchall()


@router.get("/dates/new", response_class=HTMLResponse)
def date_new_form(request: Request, conn=Depends(get_db)):
    checked = set()
    pre = request.query_params.get("category")
    if pre and pre.isdigit():
        checked.add(int(pre))
    return templates.TemplateResponse(
        request, "admin/date_form.html",
        actx(request, conn, active="dates", date=None, photos=[], videos=[], links_text="",
             cats=_all_cats(conn), checked=checked, slots=images.MAX_IMAGES))


@router.post("/dates/new")
def date_create(bg: BackgroundTasks,
                name: str = Form(...), place: str = Form(""),
                starts_at: str = Form(""), ends_at: str = Form(""),
                links: str = Form(""), comment: str = Form(""),
                draft: str | None = Form(None), pay: str | None = Form(None),
                categories: list[int] = Form(default=[]),
                photos: list[UploadFile] = File(default=[], alias="images"),
                videos: list[UploadFile] = File(default=[], alias="videos"),
                image_focuses: str = Form(""),
                conn=Depends(get_db)):
    name = clean_text(name, 200, "Название", required=True)
    place, place_url, needs_resolve = places.split_place(clean_text(place, 500, "Место"))
    comment = clean_text(comment, 2000, "Комментарий")
    starts, ends = normalize_period(parse_dt_local(starts_at), parse_dt_local(ends_at))
    link_list = parse_links(links)

    date_id = insert_date(conn, name=name, place=place, starts=starts, ends=ends,
                          comment=comment, origin="admin", guest_token=None,
                          draft=1 if draft else 0,
                          pay_split=1 if pay else 0, place_url=place_url)
    for cid in categories:
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


def _date_or_404(conn, did: int):
    d = conn.execute("SELECT * FROM dates WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "Свидание не найдено")
    return d


@router.get("/dates/{did}/edit", response_class=HTMLResponse)
def date_edit_form(did: int, request: Request, conn=Depends(get_db)):
    d = _date_or_404(conn, did)
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
             cats=_all_cats(conn), checked=checked,
             slots=images.MAX_IMAGES - len(photos)))


@router.post("/dates/{did}/edit")
def date_update(did: int, bg: BackgroundTasks, name: str = Form(...),
                place: str = Form(""),
                starts_at: str = Form(""), ends_at: str = Form(""),
                links: str = Form(""), comment: str = Form(""),
                draft: str | None = Form(None), pay: str | None = Form(None),
                categories: list[int] = Form(default=[]),
                photos: list[UploadFile] = File(default=[], alias="images"),
                videos: list[UploadFile] = File(default=[], alias="videos"),
                image_focuses: str = Form(""),
                conn=Depends(get_db)):
    d = _date_or_404(conn, did)
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
def date_publish(did: int, next: str = Form("/admin/dates"), conn=Depends(get_db)):
    _date_or_404(conn, did)
    conn.execute("UPDATE dates SET is_draft=0 WHERE id=?", (did,))
    conn.commit()
    return redir(next, "Опубликовано — гости теперь видят это свидание")


@router.post("/dates/{did}/archive")
def date_archive(did: int, next: str = Form("/admin/dates"), conn=Depends(get_db)):
    d = _date_or_404(conn, did)
    if d["archived_at"]:
        conn.execute("UPDATE dates SET archived_at=NULL WHERE id=?", (did,))
        msg = "Возвращено из архива"
    else:
        conn.execute("UPDATE dates SET archived_at=? WHERE id=?", (now_iso(), did))
        msg = "Перенесено в архив"
    conn.commit()
    return redir(next, msg)


@router.post("/dates/{did}/delete")
def date_delete(did: int, conn=Depends(get_db)):
    _date_or_404(conn, did)
    files = [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_images WHERE date_id=?", (did,))]
    files += [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_videos WHERE date_id=?", (did,))]
    conn.execute("DELETE FROM dates WHERE id=?", (did,))
    conn.commit()
    for fn in files:                  # файлы — только после коммита
        images.delete_file(fn)
    return redir("/admin/dates", "Свидание удалено")


@router.post("/dates/{did}/clone")
def date_clone(did: int, next: str = Form("/admin/dates"), conn=Depends(get_db)):
    """Дубль свидания: копируем запись, ссылки, категории и файлы (с новыми
    именами на диске). Брони и вопросы НЕ переносим — клон это свежее
    предложение. Клон создаётся черновиком, чтобы гости не увидели дубль
    раньше времени. Карта (place_url) уже распознана — резолвить не нужно."""
    src = _date_or_404(conn, did)
    new_id = insert_date(
        conn, name=f"{src['name']} (копия)", place=src["place"],
        starts=src["starts_at"], ends=src["ends_at"], comment=src["comment"],
        origin="admin", guest_token=None, draft=1,
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
def booking_delete(bid: int, next: str = Form("/admin/dates"), conn=Depends(get_db)):
    """Снять чужой выбор со свидания (например, по просьбе гостя)."""
    row = conn.execute(
        "SELECT b.id, COALESCE(g.name, 'человек') AS nm FROM bookings b "
        "LEFT JOIN guests g ON g.token=b.guest_token WHERE b.id=?", (bid,)).fetchone()
    if not row:
        raise HTTPException(404, "Выбор не найден")
    conn.execute("DELETE FROM bookings WHERE id=?", (bid,))
    conn.commit()
    return redir(next, f"Выбор снят — свидание снова свободно ({row['nm']})")


@router.post("/dates/{did}/videos/{vid}/delete")
def date_video_delete(did: int, vid: int, conn=Depends(get_db)):
    row = conn.execute(
        "SELECT * FROM date_videos WHERE id=? AND date_id=?", (vid, did)).fetchone()
    if row:
        conn.execute("DELETE FROM date_videos WHERE id=?", (vid,))
        conn.commit()
        images.delete_file(row["filename"])
    return redir(f"/admin/dates/{did}/edit", "Видео удалено")


@router.post("/dates/{did}/images/{img_id}/delete")
def date_image_delete(did: int, img_id: int, conn=Depends(get_db)):
    row = conn.execute(
        "SELECT * FROM date_images WHERE id=? AND date_id=?", (img_id, did)).fetchone()
    if row:
        conn.execute("DELETE FROM date_images WHERE id=?", (img_id,))
        conn.commit()
        images.delete_file(row["filename"])
    return redir(f"/admin/dates/{did}/edit", "Фото удалено")


@router.post("/dates/{did}/images/reorder")
def date_images_reorder(did: int, order: str = Form(...), conn=Depends(get_db)):
    """Drag-and-drop порядок фото: order — id через запятую, первое = обложка."""
    from fastapi.responses import JSONResponse
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
def date_image_focus(did: int, img_id: int, focus: str = Form(...), conn=Depends(get_db)):
    """Точка фокуса фото для обрезки в карточке: «X% Y%» (X,Y 0..100)."""
    import re as _re
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
    where = "" if f == "all" else "WHERE q.is_read=0"
    rows = conn.execute(
        f"SELECT q.*, d.name AS date_name, d.id AS did, c.name AS cat_name, "
        f"{GNAME_SQL.format(t='q.guest_token')} AS gname "
        f"FROM questions q JOIN dates d ON d.id=q.date_id "
        f"LEFT JOIN categories c ON c.id=q.category_id "
        f"LEFT JOIN guests g ON g.token=q.guest_token "
        f"{where} ORDER BY q.created_at DESC"
    ).fetchall()
    return templates.TemplateResponse(
        request, "admin/questions.html", actx(request, conn, active="q", rows=rows, f=f))


@router.post("/questions/{qid}/accept_time")
def question_accept_time(qid: int, next: str = Form("/admin/questions"),
                         conn=Depends(get_db)):
    """Принять предложенное гостем время: применяем его к свиданию."""
    q = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if not q or not q["suggest_starts"]:
        raise HTTPException(404, "Это не предложение времени")
    if not conn.execute("SELECT 1 FROM dates WHERE id=?", (q["date_id"],)).fetchone():
        raise HTTPException(404, "Свидание уже удалено")
    conn.execute("UPDATE dates SET starts_at=?, ends_at=? WHERE id=?",
                 (q["suggest_starts"], q["suggest_ends"], q["date_id"]))
    conn.execute(
        "UPDATE questions SET answer=?, answered_at=?, is_read=1 WHERE id=?",
        ("✅ Принято! Время назначено ♥", now_iso(), qid))
    conn.commit()
    return redir(next, "Время назначено ♥")


@router.post("/questions/{qid}/decline_time")
def question_decline_time(qid: int, next: str = Form("/admin/questions"),
                          conn=Depends(get_db)):
    """Вежливо отказаться от предложенного времени (автор увидит ответ)."""
    q = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if not q or not q["suggest_starts"]:
        raise HTTPException(404, "Это не предложение времени")
    conn.execute(
        "UPDATE questions SET answer=?, answered_at=?, is_read=1 WHERE id=?",
        ("🥺 Это время не получится — предложи, пожалуйста, другое",
         now_iso(), qid))
    conn.commit()
    return redir(next, "Отказ отправлен — автор увидит его на странице")


@router.post("/questions/{qid}/answer")
def question_answer(qid: int, text: str = Form(""),
                    next: str = Form("/admin/questions"), conn=Depends(get_db)):
    q = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    text = clean_text(text, 2000, "Ответ")
    if text:
        conn.execute("UPDATE questions SET answer=?, answered_at=?, is_read=1 WHERE id=?",
                     (text, now_iso(), qid))
        msg = "Ответ сохранён — автор вопроса увидит его на странице категории"
    else:
        conn.execute("UPDATE questions SET answer=NULL, answered_at=NULL WHERE id=?", (qid,))
        msg = "Ответ удалён"
    conn.commit()
    return redir(next, msg)


@router.post("/questions/{qid}/toggle")
def question_toggle(qid: int, next: str = Form("/admin/questions"), conn=Depends(get_db)):
    q = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    conn.execute("UPDATE questions SET is_read=? WHERE id=?",
                 (0 if q["is_read"] else 1, qid))
    conn.commit()
    return redir(next)


@router.post("/questions/{qid}/delete")
def question_delete(qid: int, next: str = Form("/admin/questions"), conn=Depends(get_db)):
    conn.execute("DELETE FROM questions WHERE id=?", (qid,))
    conn.commit()
    return redir(next, "Вопрос удалён")
