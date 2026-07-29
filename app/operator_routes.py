"""Операторская админка (поверхность 3) — пульт управления платформой.

Отдельный префикс /operator/*, доступ строго при is_operator (гейт
current_operator: не-оператор получает 404, аноним → /login). Оператор вне
правил изоляции — видит и трогает данные ВСЕХ пользователей по праву роли.

Интерфейс использует отдельную, более плотную версию дизайн-системы date4you:
единый операторский shell, адаптивные таблицы и спокойные стеклянные поверхности.
Он не импортирует глобальный CSS пользовательского кабинета, чтобы служебные
компоненты и сценарии модерации оставались изолированными.
"""

import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

import images
import settings as app_settings
import voting
import voting_events
from config import BASE_URL
from helpers import now_iso, now_naive
from users import current_operator, get_user
from web import get_db, redir, templates

log = logging.getLogger("operator")

router = APIRouter(prefix="/operator", dependencies=[Depends(current_operator)])


def _category_frozen(cat) -> bool:
    if cat["closed_at"] is not None or cat["voting_status"] in voting.CLOSED_STATUSES:
        return True
    if cat["voting_status"] == voting.STATUS_OPEN and cat["voting_deadline"]:
        try:
            return now_naive() >= datetime.fromisoformat(cat["voting_deadline"])
        except (TypeError, ValueError):
            return True
    return False


def _require_category_not_frozen(conn, cat) -> None:
    # Первый write в транзакции берёт SQLite RESERVED lock. После него
    # закрытие опроса/новый голос не смогут вклиниться между проверкой и
    # операторским DELETE/UPDATE.
    category_id = int(cat["id"])
    conn.execute("UPDATE categories SET id=id WHERE id=?", (category_id,))
    fresh = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if not fresh:
        raise HTTPException(404)
    if _category_frozen(fresh):
        raise HTTPException(409, "Голосование завершено — результат и голоса зафиксированы")


def _require_date_not_frozen(conn, date_id: int) -> None:
    # Блокируем writer'ов до чтения состава категорий: иначе событие могло
    # попасть в закрывающийся опрос сразу после SELECT.
    conn.execute("UPDATE dates SET id=id WHERE id=?", (date_id,))
    for cat in conn.execute(
        "SELECT c.* FROM categories c JOIN date_categories dc ON dc.category_id=c.id "
        "WHERE dc.date_id=?", (date_id,),
    ):
        _require_category_not_frozen(conn, cat)


def _validate_date_after_open_deadlines(conn, date_id: int,
                                        starts_at: str | None) -> None:
    if not starts_at:
        return
    try:
        starts = datetime.fromisoformat(starts_at)
    except (TypeError, ValueError):
        raise HTTPException(409, "У события некорректно задано время")
    for cat in conn.execute(
        "SELECT c.name, c.voting_deadline FROM categories c "
        "JOIN date_categories dc ON dc.category_id=c.id "
        "WHERE dc.date_id=? AND c.voting_status='open' "
        "AND c.voting_deadline IS NOT NULL", (date_id,),
    ):
        try:
            deadline = datetime.fromisoformat(cat["voting_deadline"])
        except (TypeError, ValueError):
            raise HTTPException(409, "У категории некорректно задан дедлайн")
        if starts <= deadline:
            raise HTTPException(
                409,
                f"Начало события должно быть позже дедлайна категории «{cat['name']}»",
            )


def _queue_date_removal_from_categories(conn, date_id: int) -> set[tuple[int, int]]:
    """Ставит уведомления всем авторизованным голосовавшим за событие.

    Возвращает пары ``(category_id, user_id)``: после фактического удаления
    голосов по ним нужно убрать дедлайн-напоминание, если других голосов в
    этой категории у пользователя не осталось.
    """
    d = conn.execute("SELECT name FROM dates WHERE id=?", (date_id,)).fetchone()
    if not d:
        return set()
    categories = conn.execute(
        "SELECT DISTINCT c.id, c.name, c.link_token FROM categories c "
        "JOIN bookings b ON b.category_id=c.id "
        "WHERE b.date_id=? ORDER BY c.id", (date_id,),
    ).fetchall()
    affected: set[tuple[int, int]] = set()
    for cat in categories:
        affected.update(
            (int(cat["id"]), int(row["user_id"]))
            for row in conn.execute(
                "SELECT DISTINCT user_id FROM bookings "
                "WHERE date_id=? AND category_id=? AND user_id IS NOT NULL",
                (date_id, cat["id"]),
            )
        )
        voting_events.queue_date_removed(
            conn, date_id, d["name"], int(cat["id"]), cat["name"],
            cat["link_token"],
        )
    return affected


def _cancel_empty_vote_deadlines(conn, affected: set[tuple[int, int]]) -> None:
    """Отменяет напоминание, только когда в категории не осталось голосов."""
    for category_id, user_id in affected:
        remaining = conn.execute(
            "SELECT 1 FROM bookings WHERE category_id=? AND user_id=? LIMIT 1",
            (category_id, user_id),
        ).fetchone()
        if not remaining:
            voting_events.cancel_deadline_reminder(conn, category_id, user_id)


def _queue_category_removal(conn, cat) -> None:
    """Отменяет устаревшие события категории и ставит notices по голосам."""
    category_id = int(cat["id"])
    voting_events.cancel_category_notifications(conn, category_id)
    dates = conn.execute(
        "SELECT DISTINCT d.id, d.name FROM dates d "
        "JOIN bookings b ON b.date_id=d.id WHERE b.category_id=? ORDER BY d.id",
        (category_id,),
    ).fetchall()
    for d in dates:
        voting_events.queue_date_removed(
            conn, int(d["id"]), d["name"], category_id, cat["name"],
            None,
        )


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
        "users": one("SELECT COUNT(*) FROM users WHERE COALESCE(telegram_id,-1)<>0"),
        "active": one("SELECT COUNT(*) FROM users WHERE COALESCE(telegram_id,-1)<>0 AND is_active=1"),
        "banned": one("SELECT COUNT(*) FROM users WHERE is_active=0"),
        "operators": one("SELECT COUNT(*) FROM users WHERE is_operator=1"),
        "cats": one("SELECT COUNT(*) FROM categories"),
        "dates": one("SELECT COUNT(*) FROM dates"),
        "bookings": one("SELECT COUNT(*) FROM bookings"),
        "reports": one("SELECT COUNT(*) FROM reports WHERE status='open'"),
        # очередь модерации: новые, ждущие проверки (мягкая очередь)
        "review_users": one(
            "SELECT COUNT(*) FROM users WHERE COALESCE(telegram_id,-1)<>0 AND is_reviewed=0"),
        "review_cats": one("SELECT COUNT(*) FROM categories WHERE is_reviewed=0"),
    }
    recent = conn.execute(
        "SELECT id, display_name, tg_username, telegram_id, is_active, is_operator, "
        "created_at, last_login_at FROM users WHERE COALESCE(telegram_id,-1)<>0 "
        "ORDER BY created_at DESC LIMIT 10").fetchall()
    return templates.TemplateResponse(
        request, "operator/dashboard.html",
        octx(request, active="dash", stats=stats, recent=recent))


PAGE = 30


@router.get("/users", response_class=HTMLResponse)
def users_list(request: Request, q: str = "", page: int = 1, conn=Depends(get_db)):
    q = (q or "").strip()
    page = max(1, page)
    where, args = "WHERE COALESCE(telegram_id,-1)<>0", []
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
    # Сериализуем удаление с любыми конкурентными изменениями пользователя и
    # только после блокировки записи собираем полный список файлов и связей.
    if conn.execute("UPDATE users SET id=id WHERE id=?", (uid,)).rowcount == 0:
        raise HTTPException(404)
    u = _target(conn, uid)
    files = [r["filename"] for r in conn.execute(
        "SELECT di.filename FROM date_images di JOIN dates d ON d.id=di.date_id "
        "WHERE d.owner_id=?", (uid,))]
    files += [r["filename"] for r in conn.execute(
        "SELECT dv.filename FROM date_videos dv JOIN dates d ON d.id=dv.date_id "
        "WHERE d.owner_id=?", (uid,))]
    files += [r["og_image"] for r in conn.execute(
        "SELECT og_image FROM categories WHERE owner_id=? AND og_image IS NOT NULL",
        (uid,))]
    if u["avatar_path"]:
        files.append(u["avatar_path"])

    for cat in conn.execute("SELECT * FROM categories WHERE owner_id=?", (uid,)).fetchall():
        _queue_category_removal(conn, cat)

    # Голоса в чужих категориях сохраняем, чтобы не пересчитывать зафиксированный
    # результат, но полностью отвязываем их и другие действия от предсказуемого
    # u<ID>. Один случайный псевдоним сохраняет связь нескольких выборов одного
    # анонимного участника, не раскрывая id удалённого аккаунта.
    old_guest = f"u{uid}"
    anon_guest = "deleted-" + secrets.token_urlsafe(18)
    identity_columns = (
        ("bookings", "guest_token"),
        ("questions", "guest_token"),
        ("dates", "guest_token"),
        ("reports", "reporter"),
    )
    for table, column in identity_columns:
        conn.execute(
            f"UPDATE {table} SET {column}=? WHERE {column}=?",
            (anon_guest, old_guest),
        )
    conn.execute("DELETE FROM guests WHERE token=?", (old_guest,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))   # FK CASCADE снесёт всё
    has_surviving_refs = any(conn.execute(
        f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (anon_guest,)
    ).fetchone() for table, column in identity_columns)
    if has_surviving_refs:
        conn.execute(
            "INSERT INTO guests(token, name, created_at) VALUES(?,?,?)",
            (anon_guest, "Удалённый участник", now_iso()),
        )
    conn.commit()
    for fn in files:                                       # файлы — после коммита
        images.delete_file(fn)
    log.warning("operator %s DELETED user %s (%s), %d files",
                request.state.user["id"], uid, u["tg_username"], len(files))
    return redir("/operator/users", "Пользователь и все его данные удалены")


# ---------- жалобы (очередь модерации) ----------

def _report_target(conn, r) -> dict:
    """Подтягивает объект жалобы (событие/категория) + владельца, если ещё жив."""
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
        _require_date_not_frozen(conn, tid)
        files = [x["filename"] for x in conn.execute(
            "SELECT filename FROM date_images WHERE date_id=?", (tid,))]
        files += [x["filename"] for x in conn.execute(
            "SELECT filename FROM date_videos WHERE date_id=?", (tid,))]
        affected = _queue_date_removal_from_categories(conn, tid)
        conn.execute("DELETE FROM dates WHERE id=?", (tid,))
        _cancel_empty_vote_deadlines(conn, affected)
    else:
        target_cat = _cat_or_404(conn, tid)
        _require_category_not_frozen(conn, target_cat)
        # События переживают удаление категории, поэтому их медиа трогать
        # нельзя. Удаляем только собственную картинку превью категории.
        files = [target_cat["og_image"]] if target_cat["og_image"] else []
        _queue_category_removal(conn, target_cat)
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
    """Удаляет категорию (связи date_categories — каскадом). События остаются
    у владельца, как и в кабинете."""
    cat = _cat_or_404(conn, cid)
    _require_category_not_frozen(conn, cat)
    _queue_category_removal(conn, cat)
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    conn.commit()
    if cat["og_image"]:
        images.delete_file(cat["og_image"])
    log.warning("operator %s deleted category %s", request.state.user["id"], cid)
    return redir("/operator/categories", "Категория удалена (события остались)")


# ---------- события (все, всех пользователей) ----------

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
    _require_date_not_frozen(conn, did)
    d = _date_or_404(conn, did)
    if d["archived_at"]:
        _validate_date_after_open_deadlines(conn, did, d["starts_at"])
        conn.execute("UPDATE dates SET archived_at=NULL WHERE id=?", (did,))
        msg = "Событие возвращено из архива"
    else:
        affected = _queue_date_removal_from_categories(conn, did)
        conn.execute("DELETE FROM bookings WHERE date_id=?", (did,))
        _cancel_empty_vote_deadlines(conn, affected)
        conn.execute("UPDATE dates SET archived_at=? WHERE id=?", (now_iso(), did))
        msg = "Событие отправлено в архив"
    conn.commit()
    return redir("/operator/dates", msg)


@router.post("/dates/{did}/delete")
def date_delete(did: int, request: Request, conn=Depends(get_db)):
    """Удаляет событие со всеми медиа (файлы с диска) и закрывает открытые
    жалобы на него."""
    _date_or_404(conn, did)
    _require_date_not_frozen(conn, did)
    files = [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_images WHERE date_id=?", (did,))]
    files += [r["filename"] for r in conn.execute(
        "SELECT filename FROM date_videos WHERE date_id=?", (did,))]
    affected = _queue_date_removal_from_categories(conn, did)
    conn.execute("DELETE FROM dates WHERE id=?", (did,))
    _cancel_empty_vote_deadlines(conn, affected)
    conn.execute(
        "UPDATE reports SET status='resolved', resolved_at=? "
        "WHERE target_type='date' AND target_id=? AND status='open'",
        (now_iso(), did))
    conn.commit()
    for fn in files:
        images.delete_file(fn)
    log.warning("operator %s deleted date %s, %d files",
                request.state.user["id"], did, len(files))
    return redir("/operator/dates", "Событие удалено")


# ---------- голоса / взаимодействия (обзор для разбора споров) ----------

@router.get("/bookings", response_class=HTMLResponse)
def bookings_list(request: Request, q: str = "", page: int = 1, conn=Depends(get_db)):
    q = (q or "").strip()
    page = max(1, page)
    where, args = "", []
    if q:
        where = ("WHERE g.name LIKE ? OR d.name LIKE ? OR c.name LIKE ? "
                 "OR u.display_name LIKE ?")
        like = f"%{q}%"
        args += [like, like, like, like]
    total = conn.execute(
        f"SELECT COUNT(*) FROM bookings b JOIN dates d ON d.id=b.date_id "
        f"JOIN categories c ON c.id=b.category_id JOIN users u ON u.id=c.owner_id "
        f"LEFT JOIN guests g ON g.token=b.guest_token {where}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT b.id, b.created_at, COALESCE(g.name, '—') AS guest, "
        f"d.name AS date_name, c.name AS cat_name, c.owner_id, "
        f"u.display_name AS owner FROM bookings b "
        f"JOIN dates d ON d.id=b.date_id JOIN categories c ON c.id=b.category_id "
        f"JOIN users u ON u.id=c.owner_id LEFT JOIN guests g ON g.token=b.guest_token "
        f"{where} ORDER BY b.created_at DESC LIMIT ? OFFSET ?",
        args + [PAGE, (page - 1) * PAGE]).fetchall()
    pages = max(1, (total + PAGE - 1) // PAGE)
    return templates.TemplateResponse(
        request, "operator/bookings.html",
        octx(request, active="bookings", rows=rows, q=q, page=page, pages=pages,
             total=total))


@router.post("/bookings/{bid}/delete")
def booking_delete(bid: int, request: Request, conn=Depends(get_db)):
    """Снять голос для разбора спорной ситуации и освободить одно место."""
    b = conn.execute(
        "SELECT c.*, b.id AS booking_id, b.user_id AS vote_user_id, "
        "b.category_id AS vote_category_id, d.name AS date_name "
        "FROM bookings b JOIN categories c ON c.id=b.category_id "
        "JOIN dates d ON d.id=b.date_id "
        "WHERE b.id=?", (bid,),
    ).fetchone()
    if not b:
        raise HTTPException(404)
    _require_category_not_frozen(conn, b)
    voting_events.queue_vote_removed_by_owner(
        conn, booking_id=int(b["booking_id"]), user_id=b["vote_user_id"],
        category_id=int(b["vote_category_id"]), category_name=b["name"],
        date_name=b["date_name"], category_token=b["link_token"],
    )
    conn.execute("DELETE FROM bookings WHERE id=?", (bid,))
    if b["vote_user_id"] is not None:
        _cancel_empty_vote_deadlines(
            conn, {(int(b["vote_category_id"]), int(b["vote_user_id"]))},
        )
    conn.commit()
    log.warning("operator %s deleted booking %s", request.state.user["id"], bid)
    return redir("/operator/bookings", "Голос снят — освободилось одно место")


# ---------- настройки модерации (глобальные флаги) ----------

@router.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse(
        request, "operator/settings.html",
        octx(request, active="settings",
             moderate_users=app_settings.is_on(conn, app_settings.MODERATE_USERS),
             moderate_categories=app_settings.is_on(conn, app_settings.MODERATE_CATEGORIES)))


@router.post("/settings")
def settings_save(request: Request,
                  moderate_users: str = Form(""),
                  moderate_categories: str = Form(""),
                  conn=Depends(get_db)):
    """Чекбоксы: присутствие значения = включено. По умолчанию модерация выкл."""
    app_settings.set_flag(conn, app_settings.MODERATE_USERS,
                          "1" if moderate_users else "0")
    app_settings.set_flag(conn, app_settings.MODERATE_CATEGORIES,
                          "1" if moderate_categories else "0")
    log.warning("operator %s updated moderation flags: users=%s cats=%s",
                request.state.user["id"], bool(moderate_users), bool(moderate_categories))
    return redir("/operator/settings", "Настройки сохранены")


# ---------- очередь модерации (новые пользователи и категории) ----------

@router.get("/review", response_class=HTMLResponse)
def review_queue(request: Request, conn=Depends(get_db)):
    users_q = conn.execute(
        "SELECT id, display_name, tg_username, telegram_id, created_at, "
        "(SELECT COUNT(*) FROM categories c WHERE c.owner_id=users.id) AS n_cats, "
        "(SELECT COUNT(*) FROM dates d WHERE d.owner_id=users.id) AS n_dates "
        "FROM users WHERE COALESCE(telegram_id,-1)<>0 AND is_reviewed=0 "
        "ORDER BY created_at DESC").fetchall()
    cats_q = conn.execute(
        "SELECT c.id, c.name, c.link_token, c.created_at, u.display_name AS owner, "
        "(SELECT COUNT(*) FROM date_categories dc WHERE dc.category_id=c.id) AS n "
        "FROM categories c JOIN users u ON u.id=c.owner_id "
        "WHERE c.is_reviewed=0 ORDER BY c.created_at DESC").fetchall()
    return templates.TemplateResponse(
        request, "operator/review.html",
        octx(request, active="review", users_q=users_q, cats_q=cats_q,
             base_url=BASE_URL))


@router.post("/review/user/{uid}/approve")
def review_user_approve(uid: int, request: Request, conn=Depends(get_db)):
    _target(conn, uid)
    conn.execute("UPDATE users SET is_reviewed=1 WHERE id=?", (uid,))
    conn.commit()
    log.warning("operator %s approved user %s", request.state.user["id"], uid)
    return redir("/operator/review", "Пользователь одобрен")


@router.post("/review/category/{cid}/approve")
def review_category_approve(cid: int, request: Request, conn=Depends(get_db)):
    _cat_or_404(conn, cid)
    conn.execute("UPDATE categories SET is_reviewed=1 WHERE id=?", (cid,))
    conn.commit()
    log.warning("operator %s approved category %s", request.state.user["id"], cid)
    return redir("/operator/review", "Категория одобрена")



