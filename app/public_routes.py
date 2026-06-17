"""Публичная часть: главная, /health и страницы категорий /c/<токен>.

Гости представляются по имени, выбирают свидание (одно свидание — один
человек), предлагают свои идеи и задают вопросы.
"""

import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Request, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, Response, StreamingResponse)

import db
import images
import notify
import places
from config import BASE_URL, DOMAIN, GUEST_COOKIE, LEGACY_GUEST_COOKIE, MSK
from guests import get_guest, get_guest_name, require_name, set_guest_cookie
from helpers import (_parse, clean_text, fmt_gcal, fmt_when, normalize_period,
                     now_iso, parse_dt_local, parse_links)
from notify import esc
from ratelimit import guest_throttle
from tasks import autoarchive_once
from web import get_db, templates

router = APIRouter()


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


def insert_date(conn, *, name, place, starts, ends, comment, origin, guest_token,
                draft=0, pay_split=0, place_url=None) -> int:
    cur = conn.execute(
        "INSERT INTO dates(name, place, place_url, starts_at, ends_at, comment, origin, "
        "guest_token, is_draft, pay_split, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (name, place, place_url, starts, ends, comment, origin, guest_token,
         draft, pay_split, now_iso()),
    )
    return cur.lastrowid


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
        "SELECT b.date_id, b.guest_token, COALESCE(g.name, 'гость') AS name "
        "FROM bookings b LEFT JOIN guests g ON g.token = b.guest_token "
        "WHERE b.category_id=? ORDER BY b.created_at",
        (category_id,),
    ).fetchall()


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


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "public/home.html")


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse("static/apple-touch-icon.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


# ---------------------------------------------------------------------------
# Страница категории
# ---------------------------------------------------------------------------

@router.get("/c/{token}", response_class=HTMLResponse)
def public_category(token: str, request: Request, conn=Depends(get_db)):
    cat = cat_by_token(conn, token)
    if not cat or not cat["link_enabled"]:
        return templates.TemplateResponse(request, "public/gone.html", status_code=404)

    autoarchive_once(conn)  # статус «прошло» проставляется мгновенно, без ожидания цикла

    guest = request.cookies.get(GUEST_COOKIE)
    new_guest = False                      # нужно ли (пере)выставить cookie
    if not guest:
        guest = request.cookies.get(LEGACY_GUEST_COOKIE)
        new_guest = guest is not None      # тихо переезжаем на __Host-имя
    if not guest:
        guest = secrets.token_urlsafe(16)
        new_guest = True
    guest_name = get_guest_name(conn, guest)

    bookings: dict[int, list] = {}
    for b in _booking_rows(conn, cat["id"]):
        bookings.setdefault(b["date_id"], []).append(b)

    def enrich(row, past: bool = False) -> dict:
        d = date_payload(conn, row)
        d["pending"] = bool(d["is_draft"])
        d["past"] = past
        entries = bookings.get(d["id"], [])
        d["booked_by_me"] = any(e["guest_token"] == guest for e in entries)
        others = [e["name"] for e in entries if e["guest_token"] != guest]
        d["booked_others_list"] = others         # инициалы-аватарки в карточке
        d["booked_others"] = ", ".join(others)   # для обновления DOM без reload
        parts = (["ты ♥"] if d["booked_by_me"] else []) + others
        d["booked_label"] = ", ".join(parts)
        d["my_questions"] = conn.execute(
            "SELECT text, answer FROM questions WHERE date_id=? AND guest_token=? "
            "ORDER BY created_at", (d["id"], guest)).fetchall()
        d["editable"] = (not past and d["origin"] == "guest"
                         and d["guest_token"] == guest)
        if d["editable"]:
            d["meta_json"] = json.dumps({
                "id": d["id"], "name": d["name"], "place": d["place"] or "",
                "starts_at": d["starts_at"] or "", "ends_at": d["ends_at"] or "",
                "comment": d["comment"] or "",
                "links": "\n".join(l["url"] for l in d["links"]),
                "pay": d["pay_split"],
                "photos": [{"id": p["id"], "filename": p["filename"]} for p in d["images"]],
                "videos": [{"id": v["id"], "filename": v["filename"]} for v in d["videos"]],
            }, ensure_ascii=False).replace("</", "<\\/")
        if not past and not d["pending"] and d["starts_at"]:
            d["gcal"] = fmt_gcal(d["name"], d["starts_at"], d["ends_at"],
                                 d["place"], d["comment"],
                                 [l["url"] for l in d["links"]])
        return d

    rows = conn.execute(
        "SELECT d.* FROM dates d JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE dc.category_id=? AND d.archived_at IS NULL "
        "AND (d.is_draft=0 OR (d.origin='guest' AND d.guest_token=?)) "
        "ORDER BY dc.position ASC, (d.starts_at IS NULL) ASC, d.starts_at ASC, d.created_at ASC",
        (cat["id"], guest),
    ).fetchall()
    dates = [enrich(r) for r in rows]

    past_rows = conn.execute(
        "SELECT d.* FROM dates d JOIN date_categories dc ON dc.date_id=d.id "
        "WHERE dc.category_id=? AND d.archived_at IS NOT NULL AND d.is_draft=0 "
        "ORDER BY COALESCE(d.ends_at, d.starts_at, d.created_at) DESC LIMIT 30",
        (cat["id"],),
    ).fetchall()
    past = [enrich(r, past=True) for r in past_rows]

    resp = templates.TemplateResponse(request, "public/category.html", {
        "cat": cat,
        "regular": dates,
        "past": past,
        "guest": guest, "guest_name": guest_name,
        "token": token,
    })
    resp.headers["X-Robots-Tag"] = "noindex"
    if new_guest:
        set_guest_cookie(resp, guest)
    return resp


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
    guest = get_guest(request) or ""
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
    guest = get_guest(request) or ""
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

    start = _parse(d["starts_at"]).replace(tzinfo=MSK)
    end = (_parse(d["ends_at"]).replace(tzinfo=MSK) if d["ends_at"]
           else start + timedelta(hours=2))
    f = "%Y%m%dT%H%M%SZ"

    desc_parts = []
    if d["comment"]:
        desc_parts.append(d["comment"])
    desc_parts += [r["url"] for r in conn.execute(
        "SELECT url FROM date_links WHERE date_id=? ORDER BY position, id", (date_id,))]

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//date4you//RU",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
        f"UID:date-{d['id']}-{cat['id']}@{DOMAIN}",
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
                             f'attachment; filename="date-{date_id}.ics"'})


# ---------------------------------------------------------------------------
# Действия гостя
# ---------------------------------------------------------------------------

@router.post("/c/{token}/name")
def public_name(token: str, request: Request, name: str = Form(...),
                conn=Depends(get_db)):
    """Гость представляется (или меняет имя)."""
    active_cat_or_410(conn, token)
    guest = get_guest(request)
    new_guest = False
    if not guest:
        guest = secrets.token_urlsafe(16)
        new_guest = True
    guest_throttle("name", guest, request)
    name = clean_text(name, 40, "Имя", required=True)
    conn.execute(
        "INSERT INTO guests(token, name, created_at) VALUES(?,?,?) "
        "ON CONFLICT(token) DO UPDATE SET name=excluded.name",
        (guest, name, now_iso()))
    conn.commit()
    resp = JSONResponse({"ok": True, "name": name})
    if new_guest:
        set_guest_cookie(resp, guest)
    return resp


@router.post("/c/{token}/book")
def public_book(token: str, request: Request, bg: BackgroundTasks,
                date_id: int = Form(...), conn=Depends(get_db)):
    """Выбор свидания: у гостя ровно один на категорию.

    Тап по своему выбору — отмена; тап по другому свиданию — перенос.
    """
    cat = active_cat_or_410(conn, token)
    guest = get_guest(request)
    name = require_name(conn, guest)
    guest_throttle("book", guest, request)

    d = date_in_category(conn, cat["id"], date_id)
    if not d:
        raise HTTPException(404, "Свидание не найдено")

    mine = conn.execute(
        "SELECT id FROM bookings WHERE date_id=? AND category_id=? AND guest_token=?",
        (date_id, cat["id"], guest)).fetchone()

    if mine:
        conn.execute("DELETE FROM bookings WHERE id=?", (mine["id"],))
        booked = False
        bg.add_task(notify.notify,
                    f"🤍 {esc(name)} отменил(а) выбор «{esc(d['name'])}» "
                    f"в категории «{esc(cat['name'])}»")
    else:
        # одно свидание может выбрать только один человек,
        # а вот гость может выбрать сколько угодно свиданий
        holder = conn.execute(
            "SELECT b.guest_token, COALESCE(g.name, 'кто-то') AS nm "
            "FROM bookings b LEFT JOIN guests g ON g.token=b.guest_token "
            "WHERE b.date_id=?", (date_id,)).fetchone()
        if holder and holder["guest_token"] != guest:
            raise HTTPException(409, f"Уже выбрано: {holder['nm']} ♥")
        try:
            conn.execute(
                "INSERT INTO bookings(date_id, category_id, guest_token, created_at) "
                "VALUES(?,?,?,?)",
                (date_id, cat["id"], guest, now_iso()))
        except sqlite3.IntegrityError:
            # гонка: кто-то выбрал это свидание между проверкой и записью
            conn.rollback()
            raise HTTPException(409, "Только что заняли это свидание — обнови страницу ♥")
        booked = True
        bg.add_task(notify.notify,
                    f"💝 {esc(name)} выбрал(а) «{esc(d['name'])}» "
                    f"в категории «{esc(cat['name'])}»")
    conn.commit()
    return JSONResponse({"ok": True, "booked": booked, "name": name})


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
    guest = get_guest(request)
    name = require_name(conn, guest)
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
        "INSERT INTO questions(date_id, category_id, guest_token, text, "
        "suggest_starts, suggest_ends, created_at) VALUES(?,?,?,?,?,?,?)",
        (date_id, cat["id"], guest, text, starts, ends, now_iso()))
    conn.commit()
    bg.add_task(notify.notify,
                f"📅 {esc(name)} предлагает время для «{esc(d['name'])}» "
                f"({esc(cat['name'])}):\n{fmt_when(starts, ends)}"
                f"\n\n{BASE_URL}/admin/questions")
    return JSONResponse({"ok": True})


@router.post("/c/{token}/question")
def public_question(token: str, request: Request, bg: BackgroundTasks,
                    date_id: int = Form(...), text: str = Form(...),
                    conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token)
    guest = get_guest(request)
    name = require_name(conn, guest)
    guest_throttle("question", guest, request)

    d = date_in_category(conn, cat["id"], date_id)
    if not d:
        raise HTTPException(404, "Свидание не найдено")
    text = clean_text(text, 2000, "Вопрос", required=True)

    conn.execute(
        "INSERT INTO questions(date_id, category_id, guest_token, text, created_at) "
        "VALUES(?,?,?,?,?)",
        (date_id, cat["id"], guest, text, now_iso()),
    )
    conn.commit()
    bg.add_task(notify.notify,
                f"❓ {esc(name)} — вопрос к «{esc(d['name'])}» "
                f"({esc(cat['name'])}):\n{esc(text)}\n\n{BASE_URL}/admin/questions")
    return JSONResponse({"ok": True})


@router.post("/c/{token}/propose")
def public_propose(token: str, request: Request, bg: BackgroundTasks,
                   name: str = Form(...), place: str = Form(""),
                   starts_at: str = Form(""), ends_at: str = Form(""),
                   links: str = Form(""), comment: str = Form(""),
                   pay: str | None = Form(None),
                   photos: list[UploadFile] = File(default=[], alias="images"),
                   video: UploadFile | None = File(None),
                   conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token)
    guest = get_guest(request)
    author = require_name(conn, guest)
    guest_throttle("prop", guest, request)

    name = clean_text(name, 200, "Название", required=True)
    place, place_url, needs_resolve = places.split_place(clean_text(place, 500, "Место"))
    comment = clean_text(comment, 2000, "Комментарий")
    starts, ends = normalize_period(parse_dt_local(starts_at), parse_dt_local(ends_at))
    link_list = parse_links(links)

    moderated = bool(cat["moderate_proposals"])
    date_id = insert_date(conn, name=name, place=place, starts=starts, ends=ends,
                          comment=comment, origin="guest", guest_token=guest,
                          draft=1 if moderated else 0,
                          pay_split=1 if pay else 0, place_url=place_url)
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

    msg = (f"💡 {esc(author)} предложил(а) свидание в «{esc(cat['name'])}»"
           f"{' — ждёт модерации' if moderated else ''}:\n«{esc(name)}»")
    if place:
        msg += f"\n📍 {esc(place)}"
    when = fmt_when(starts, ends)
    if when:
        msg += f"\n🕐 {when} (мск)"
    msg += f"\n\n{BASE_URL}/admin/dates/{date_id}/edit"
    bg.add_task(notify.notify, msg)

    return JSONResponse({"ok": True, "id": date_id, "moderated": moderated})


@router.post("/c/{token}/propose/{date_id}/edit")
def public_propose_edit(token: str, date_id: int, request: Request, bg: BackgroundTasks,
                        name: str = Form(...), place: str = Form(""),
                        starts_at: str = Form(""), ends_at: str = Form(""),
                        links: str = Form(""), comment: str = Form(""),
                        keep_order: str = Form(""),
                        remove_image: list[int] = Form(default=[]),
                        remove_video: list[int] = Form(default=[]),
                        pay: str | None = Form(None),
                        photos: list[UploadFile] = File(default=[], alias="images"),
                        video: UploadFile | None = File(None),
                        conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token)
    guest = get_guest(request)
    author = require_name(conn, guest)
    d = own_proposal_or_403(conn, cat, date_id, guest)
    guest_throttle("prop", guest, request)

    name = clean_text(name, 200, "Название", required=True)
    place, place_url, needs_resolve = places.place_on_edit(
        clean_text(place, 500, "Место"), d)
    comment = clean_text(comment, 2000, "Комментарий")
    starts, ends = normalize_period(parse_dt_local(starts_at), parse_dt_local(ends_at))
    link_list = parse_links(links)

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
        (name, place, place_url, starts, ends, comment, 1 if pay else 0, date_id))
    save_links(conn, date_id, link_list)
    conn.commit()
    if needs_resolve:
        bg.add_task(places.resolve_into_db, date_id, place_url)
    for r in to_remove:               # файлы — только после коммита
        images.delete_file(r["filename"])
    for v in vid_remove:
        images.delete_file(v["filename"])

    bg.add_task(notify.notify,
                f"✏️ {esc(author)} изменил(а) своё предложение "
                f"«{esc(name)}» ({esc(cat['name'])})\n{BASE_URL}/admin/dates/{date_id}/edit")
    return JSONResponse({"ok": True})


@router.post("/c/{token}/propose/{date_id}/delete")
def public_propose_delete(token: str, date_id: int, request: Request, bg: BackgroundTasks,
                          conn=Depends(get_db)):
    cat = active_cat_or_410(conn, token)
    guest = get_guest(request)
    author = require_name(conn, guest)
    d = own_proposal_or_403(conn, cat, date_id, guest)
    guest_throttle("prop", guest, request)

    files = [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_images WHERE date_id=?", (date_id,))]
    files += [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_videos WHERE date_id=?", (date_id,))]
    conn.execute("DELETE FROM dates WHERE id=?", (date_id,))
    conn.commit()
    for fn in files:
        images.delete_file(fn)

    bg.add_task(notify.notify,
                f"🗑 {esc(author)} удалил(а) своё предложение "
                f"«{esc(d['name'])}» ({esc(cat['name'])})")
    return JSONResponse({"ok": True})
