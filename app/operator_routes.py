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
from config import BASE_URL
from helpers import now_iso
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
        "reports": one("SELECT COUNT(*) FROM reports WHERE status='open'"),
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


# ---------- жалобы (очередь модерации) ----------

def _report_target(conn, r) -> dict:
    """Подтягивает объект жалобы (свидание/категория) + владельца, если ещё жив."""
    if r["target_type"] == "category":
        row = conn.execute(
            "SELECT c.name, c.owner_id, u.display_name AS owner, c.link_token "
            "FROM categories c LEFT JOIN users u ON u.id=c.owner_id "
            "WHERE c.id=?", (r["target_id"],)).fetchone()
    else:
        row = conn.execute(
            "SELECT d.name, d.owner_id, u.display_name AS owner FROM dates d "
            "LEFT JOIN users u ON u.id=d.owner_id WHERE d.id=?",
            (r["target_id"],)).fetchone()
    return dict(row) if row else {}


@router.get("/reports", response_class=HTMLResponse)
def reports_list(request: Request, status: str = "open", conn=Depends(get_db)):
    if status not in ("open", "resolved", "dismissed", "all"):
        status = "open"
    where = "" if status == "all" else "WHERE status=?"
    args = () if status == "all" else (status,)
    rows = conn.execute(
        f"SELECT * FROM reports {where} ORDER BY created_at DESC LIMIT 200",
        args).fetchall()
    items = [{"r": r, "t": _report_target(conn, r)} for r in rows]
    return templates.TemplateResponse(
        request, "operator/reports.html",
        octx(request, active="reports", items=items, status=status))


def _get_report(conn, rid: int):
    r = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404)
    return r


@router.post("/reports/{rid}/resolve")
def report_resolve(rid: int, request: Request, action: str = Form("resolved"),
                   conn=Depends(get_db)):
    """Закрыть жалобу без удаления контента: обработана или отклонена."""
    _get_report(conn, rid)
    st = "dismissed" if action == "dismiss" else "resolved"
    conn.execute("UPDATE reports SET status=?, resolved_at=? WHERE id=?",
                 (st, now_iso(), rid))
    conn.commit()
    return redir("/operator/reports", "Жалоба отклонена" if st == "dismissed"
                 else "Жалоба помечена обработанной")


@router.post("/reports/{rid}/takedown")
def report_takedown(rid: int, request: Request, conn=Depends(get_db)):
    """Удалить контент по жалобе (takedown) и закрыть жалобу. Удаляет также
    ВСЕ прочие открытые жалобы на тот же объект — он больше не существует."""
    r = _get_report(conn, rid)
    tt, tid = r["target_type"], r["target_id"]
    files = []
    if tt == "date":
        files = [x["filename"] for x in conn.execute(
            "SELECT filename FROM date_images WHERE date_id=?", (tid,))]
        files += [x["filename"] for x in conn.execute(
            "SELECT filename FROM date_videos WHERE date_id=?", (tid,))]
        conn.execute("DELETE FROM dates WHERE id=?", (tid,))
    else:
        files = [x["filename"] for x in conn.execute(
            "SELECT di.filename FROM date_images di JOIN dates d ON d.id=di.date_id "
            "JOIN date_categories dc ON dc.date_id=d.id WHERE dc.category_id=?", (tid,))]
        conn.execute("DELETE FROM categories WHERE id=?", (tid,))
    conn.execute(
        "UPDATE reports SET status='resolved', resolved_at=? "
        "WHERE target_type=? AND target_id=? AND status='open'",
        (now_iso(), tt, tid))
    conn.commit()
    for fn in files:
        images.delete_file(fn)
    log.warning("operator %s TAKEDOWN %s %s (report %s), %d files",
                request.state.user["id"], tt, tid, rid, len(files))
    return redir("/operator/reports", "Контент удалён, жалоба закрыта")


# ---------- категории (все, всех пользователей) ----------

@router.get("/categories", response_class=HTMLResponse)
def cats_list(request: Request, q: str = "", page: int = 1, conn=Depends(get_db)):
    q = (q or "").strip()
    page = max(1, page)
    where, args = "", []
    if q:
        where = ("WHERE c.name LIKE ? OR u.display_name LIKE ? "
                 "OR u.tg_username LIKE ?")
        like = f"%{q}%"
        args += [like, like, like]
    total = conn.execute(
        f"SELECT COUNT(*) FROM categories c JOIN users u ON u.id=c.owner_id {where}",
        args).fetchone()[0]
    rows = conn.execute(
        f"SELECT c.id, c.name, c.link_enabled, c.link_token, c.owner_id, "
        f"u.display_name AS owner, "
        f"(SELECT COUNT(*) FROM date_categories dc WHERE dc.category_id=c.id) AS n "
        f"FROM categories c JOIN users u ON u.id=c.owner_id {where} "
        f"ORDER BY c.created_at DESC LIMIT ? OFFSET ?",
        args + [PAGE, (page - 1) * PAGE]).fetchall()
    pages = max(1, (total + PAGE - 1) // PAGE)
    return templates.TemplateResponse(
        request, "operator/categories.html",
        octx(request, active="cats", rows=rows, q=q, page=page, pages=pages,
             total=total, base_url=BASE_URL))


def _cat_or_404(conn, cid: int):
    c = conn.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404)
    return c


@router.post("/categories/{cid}/toggle")
def cat_toggle(cid: int, request: Request, conn=Depends(get_db)):
    c = _cat_or_404(conn, cid)
    new = 0 if c["link_enabled"] else 1
    conn.execute("UPDATE categories SET link_enabled=? WHERE id=?", (new, cid))
    conn.commit()
    log.warning("operator %s toggled link_enabled=%s for category %s",
                request.state.user["id"], new, cid)
    return redir("/operator/categories",
                 "Ссылка включена" if new else "Ссылка выключена")


@router.post("/categories/{cid}/delete")
def cat_delete(cid: int, request: Request, conn=Depends(get_db)):
    """Удаляет категорию (связи date_categories — каскадом). Свидания остаются
    у владельца, как и в кабинете."""
    _cat_or_404(conn, cid)
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    conn.commit()
    log.warning("operator %s deleted category %s", request.state.user["id"], cid)
    return redir("/operator/categories", "Категория удалена (свидания остались)")


# ---------- свидания (все, всех пользователей) ----------

@router.get("/dates", response_class=HTMLResponse)
def dates_list(request: Request, q: str = "", flt: str = "", page: int = 1,
               conn=Depends(get_db)):
    q = (q or "").strip()
    page = max(1, page)
    conds, args = [], []
    if q:
        conds.append("(d.name LIKE ? OR u.display_name LIKE ? OR u.tg_username LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
    if flt == "draft":
        conds.append("d.is_draft=1")
    elif flt == "booked":
        conds.append("EXISTS(SELECT 1 FROM bookings b WHERE b.date_id=d.id)")
    elif flt == "reported":
        conds.append("EXISTS(SELECT 1 FROM reports r WHERE r.target_type='date' "
                     "AND r.target_id=d.id AND r.status='open')")
    elif flt == "archived":
        conds.append("d.archived_at IS NOT NULL")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM dates d JOIN users u ON u.id=d.owner_id {where}",
        args).fetchone()[0]
    rows = conn.execute(
        f"SELECT d.id, d.name, d.is_draft, d.origin, d.archived_at, d.owner_id, "
        f"u.display_name AS owner, "
        f"(SELECT COUNT(*) FROM bookings b WHERE b.date_id=d.id) AS books, "
        f"(SELECT COUNT(*) FROM reports r WHERE r.target_type='date' "
        f" AND r.target_id=d.id AND r.status='open') AS reports "
        f"FROM dates d JOIN users u ON u.id=d.owner_id {where} "
        f"ORDER BY d.created_at DESC LIMIT ? OFFSET ?",
        args + [PAGE, (page - 1) * PAGE]).fetchall()
    pages = max(1, (total + PAGE - 1) // PAGE)
    return templates.TemplateResponse(
        request, "operator/dates.html",
        octx(request, active="dates", rows=rows, q=q, flt=flt, page=page,
             pages=pages, total=total))


def _date_or_404(conn, did: int):
    d = conn.execute("SELECT * FROM dates WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404)
    return d


@router.post("/dates/{did}/archive")
def date_archive(did: int, request: Request, conn=Depends(get_db)):
    d = _date_or_404(conn, did)
    if d["archived_at"]:
        conn.execute("UPDATE dates SET archived_at=NULL WHERE id=?", (did,))
        msg = "Свидание возвращено из архива"
    else:
        conn.execute("UPDATE dates SET archived_at=? WHERE id=?", (now_iso(), did))
        msg = "Свидание отправлено в архив"
    conn.commit()
    return redir("/operator/dates", msg)


@router.post("/dates/{did}/delete")
def date_delete(did: int, request: Request, conn=Depends(get_db)):
    """Удаляет свидание со всеми медиа (файлы с диска) и закрывает открытые
    жалобы на него."""
    _date_or_404(conn, did)
    files = [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_images WHERE date_id=?", (did,))]
    files += [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_videos WHERE date_id=?", (did,))]
    conn.execute("DELETE FROM dates WHERE id=?", (did,))
    conn.execute(
        "UPDATE reports SET status='resolved', resolved_at=? "
        "WHERE target_type='date' AND target_id=? AND status='open'",
        (now_iso(), did))
    conn.commit()
    for fn in files:
        images.delete_file(fn)
    log.warning("operator %s deleted date %s, %d files",
                request.state.user["id"], did, len(files))
    return redir("/operator/dates", "Свидание удалено")



