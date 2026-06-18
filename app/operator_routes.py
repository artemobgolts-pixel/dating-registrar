"""Операторская админка (поверхность 3) — пульт управления платформой.

Отдельный префикс /operator/*, доступ строго при is_operator (гейт
current_operator: не-оператор получает 404, аноним → /login). Оператор вне
правил изоляции — видит и трогает данные ВСЕХ пользователей по праву роли.

Интерфейс намеренно простой/функциональный (без стекла и анимаций кабинета).
Первый срез: дашборд + управление пользователями. Жалобы — следующим срезом.
"""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

import images
from users import current_operator, get_user
from web import get_db, redir, templates

log = logging.getLogger("operator")

router = APIRouter(prefix="/operator", dependencies=[Depends(current_operator)])


def octx(request: Request, **extra) -> dict:
    ctx = {"request": request, "user": request.state.user,
           "csrf": request.session.get("csrf", "")}
    ctx.update(extra)
    return ctx


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn=Depends(get_db)):
    one = lambda sql, a=(): conn.execute(sql, a).fetchone()[0]
    stats = {
        "users": one("SELECT COUNT(*) FROM users WHERE telegram_id<>0"),
        "active": one("SELECT COUNT(*) FROM users WHERE telegram_id<>0 AND is_active=1"),
        "banned": one("SELECT COUNT(*) FROM users WHERE is_active=0"),
        "operators": one("SELECT COUNT(*) FROM users WHERE is_operator=1"),
        "cats": one("SELECT COUNT(*) FROM categories"),
        "dates": one("SELECT COUNT(*) FROM dates"),
        "bookings": one("SELECT COUNT(*) FROM bookings"),
    }
    recent = conn.execute(
        "SELECT id, display_name, tg_username, telegram_id, is_active, is_operator, "
        "created_at, last_login_at FROM users WHERE telegram_id<>0 "
        "ORDER BY created_at DESC LIMIT 10").fetchall()
    return templates.TemplateResponse(
        request, "operator/dashboard.html",
        octx(request, active="dash", stats=stats, recent=recent))


PAGE = 30


@router.get("/users", response_class=HTMLResponse)
def users_list(request: Request, q: str = "", page: int = 1, conn=Depends(get_db)):
    q = (q or "").strip()
    page = max(1, page)
    where, args = "WHERE telegram_id<>0", []
    if q:
        where += (" AND (display_name LIKE ? OR tg_username LIKE ? "
                  "OR CAST(telegram_id AS TEXT) LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
    total = conn.execute(f"SELECT COUNT(*) FROM users {where}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT u.id, u.display_name, u.tg_username, u.telegram_id, u.is_active, "
        f"u.is_operator, u.date_limit, u.created_at, "
        f"(SELECT COUNT(*) FROM dates d WHERE d.owner_id=u.id) AS n_dates, "
        f"(SELECT COUNT(*) FROM categories c WHERE c.owner_id=u.id) AS n_cats "
        f"FROM users u {where} ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
        args + [PAGE, (page - 1) * PAGE]).fetchall()
    pages = max(1, (total + PAGE - 1) // PAGE)
    return templates.TemplateResponse(
        request, "operator/users.html",
        octx(request, active="users", rows=rows, q=q, page=page, pages=pages,
             total=total))


def _target(conn, uid: int):
    """Пользователь-цель операторского действия. 404, если нет или это легаси."""
    u = get_user(conn, uid)
    if not u or u["telegram_id"] == 0:
        raise HTTPException(404)
    return u


@router.get("/users/{uid}", response_class=HTMLResponse)
def user_card(uid: int, request: Request, conn=Depends(get_db)):
    u = _target(conn, uid)
    cats = conn.execute(
        "SELECT id, name, link_enabled, link_token, "
        "(SELECT COUNT(*) FROM date_categories dc WHERE dc.category_id=categories.id) "
        "AS n FROM categories WHERE owner_id=? ORDER BY created_at DESC", (uid,)).fetchall()
    dates = conn.execute(
        "SELECT id, name, archived_at, is_draft, origin FROM dates "
        "WHERE owner_id=? ORDER BY created_at DESC LIMIT 100", (uid,)).fetchall()
    return templates.TemplateResponse(
        request, "operator/user_card.html",
        octx(request, active="users", u=u, cats=cats, dates=dates))


def _back(uid: int) -> str:
    return f"/operator/users/{uid}"


@router.post("/users/{uid}/ban")
def user_ban(uid: int, request: Request, conn=Depends(get_db)):
    u = _target(conn, uid)
    if u["id"] == request.state.user["id"]:
        raise HTTPException(400, "Нельзя забанить самого себя")
    new = 0 if u["is_active"] else 1
    conn.execute("UPDATE users SET is_active=? WHERE id=?", (new, uid))
    conn.commit()
    log.warning("operator %s set is_active=%s for user %s (%s)",
                request.state.user["id"], new, uid, u["tg_username"])
    return redir(_back(uid), "Пользователь разбанен" if new else "Пользователь забанен")


@router.post("/users/{uid}/quota")
def user_quota(uid: int, request: Request, date_limit: int = Form(...),
               conn=Depends(get_db)):
    _target(conn, uid)
    lim = max(0, min(10000, int(date_limit)))
    conn.execute("UPDATE users SET date_limit=? WHERE id=?", (lim, uid))
    conn.commit()
    return redir(_back(uid), f"Квота обновлена: {lim}")


@router.post("/users/{uid}/operator")
def user_operator(uid: int, request: Request, conn=Depends(get_db)):
    u = _target(conn, uid)
    if u["id"] == request.state.user["id"]:
        raise HTTPException(400, "Нельзя снять роль оператора с самого себя")
    new = 0 if u["is_operator"] else 1
    conn.execute("UPDATE users SET is_operator=? WHERE id=?", (new, uid))
    conn.commit()
    log.warning("operator %s set is_operator=%s for user %s",
                request.state.user["id"], new, uid)
    return redir(_back(uid), "Назначен оператором" if new else "Роль оператора снята")


@router.post("/users/{uid}/delete")
def user_delete(uid: int, request: Request, conn=Depends(get_db)):
    """Удаляет пользователя со всеми данными (каскад FK) и файлами с диска."""
    u = _target(conn, uid)
    if u["id"] == request.state.user["id"]:
        raise HTTPException(400, "Нельзя удалить самого себя")
    files = [r["filename"] for r in conn.execute(
        "SELECT di.filename FROM date_images di JOIN dates d ON d.id=di.date_id "
        "WHERE d.owner_id=?", (uid,))]
    files += [r["filename"] for r in conn.execute(
        "SELECT dv.filename FROM date_videos dv JOIN dates d ON d.id=dv.date_id "
        "WHERE d.owner_id=?", (uid,))]
    if u["avatar_path"]:
        files.append(u["avatar_path"])
    conn.execute("DELETE FROM users WHERE id=?", (uid,))   # FK CASCADE снесёт всё
    conn.commit()
    for fn in files:                                       # файлы — после коммита
        images.delete_file(fn)
    log.warning("operator %s DELETED user %s (%s), %d files",
                request.state.user["id"], uid, u["tg_username"], len(files))
    return redir("/operator/users", "Пользователь и все его данные удалены")



