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
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

import images
import social_events
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


def _detach_date_from_frozen_categories(conn, date_id: int) -> list[str]:
    """Убирает held-событие из уже зафиксированных голосований перед выпуском.

    Пока событие удерживается платформой, публичные пути не позволяют за него
    голосовать. Поэтому отсоединение не меняет легитимный результат закрытого
    опроса, зато событие не остаётся навсегда без пути к одобрению.
    """
    conn.execute("UPDATE dates SET id=id WHERE id=?", (date_id,))
    frozen = [
        cat for cat in conn.execute(
            "SELECT c.* FROM categories c "
            "JOIN date_categories dc ON dc.category_id=c.id "
            "WHERE dc.date_id=? ORDER BY c.id",
            (date_id,),
        ).fetchall()
        if _category_frozen(cat)
    ]
    if not frozen:
        return []
    category_ids = [int(cat["id"]) for cat in frozen]
    placeholders = ",".join("?" for _ in category_ids)
    if conn.execute(
        f"SELECT 1 FROM bookings WHERE date_id=? "
        f"AND category_id IN ({placeholders}) LIMIT 1",
        [date_id, *category_ids],
    ).fetchone():
        raise HTTPException(
            409,
            "Нельзя выпустить событие: в завершённом голосовании уже есть голоса",
        )
    conn.execute(
        f"DELETE FROM date_categories WHERE date_id=? "
        f"AND category_id IN ({placeholders})",
        [date_id, *category_ids],
    )
    return [str(cat["name"]) for cat in frozen]


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
        "review_cats": one(
            "SELECT COUNT(*) FROM categories "
            "WHERE is_reviewed=0 OR operator_review_pending=1"),
        "review_dates": one(
            "SELECT COUNT(*) FROM dates WHERE operator_review_pending=1"),
        "suspicious": one(
            "SELECT COUNT(*) FROM users WHERE is_suspicious=1"),
    }
    recent = conn.execute(
        "SELECT id, display_name, tg_username, telegram_id, is_active, is_operator, "
        "is_suspicious, "
        "created_at, last_login_at FROM users WHERE COALESCE(telegram_id,-1)<>0 "
        "ORDER BY created_at DESC LIMIT 10").fetchall()
    return templates.TemplateResponse(
        request, "operator/dashboard.html",
        octx(request, active="dash", stats=stats, recent=recent))


PAGE = 30
USER_CARD_PAGE = 10


def _search_query(value: object, *, limit: int = 200) -> str:
    """Нормализует поисковую строку и ограничивает стоимость LIKE-запроса."""
    return " ".join(str(value or "").split())[:limit]


def _like_pattern(value: str) -> str:
    """Ищет введённые ``%`` и ``_`` буквально, а не как маски SQLite LIKE."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _page_number(page: int, total: int, per_page: int = PAGE) -> tuple[int, int]:
    pages = max(1, (total + per_page - 1) // per_page)
    return max(1, min(page, pages)), pages


@router.get("/users", response_class=HTMLResponse)
def users_list(request: Request, q: str = "", state: str = "", role: str = "",
               risk: str = "", page: int = 1, conn=Depends(get_db)):
    q = _search_query(q)
    state = state if state in {"active", "blocked"} else ""
    role = role if role in {"operator", "member"} else ""
    risk = risk if risk in {"suspicious", "regular"} else ""
    where, args = "WHERE COALESCE(telegram_id,-1)<>0", []
    if state == "active":
        where += " AND is_active=1"
    elif state == "blocked":
        where += " AND is_active=0"
    if role == "operator":
        where += " AND is_operator=1"
    elif role == "member":
        where += " AND is_operator=0"
    if risk == "suspicious":
        where += " AND is_suspicious=1"
    elif risk == "regular":
        where += " AND is_suspicious=0"
    if q:
        where += (" AND (display_name LIKE ? ESCAPE '\\' COLLATE NOCASE "
                  "OR tg_username LIKE ? ESCAPE '\\' COLLATE NOCASE "
                  "OR CAST(telegram_id AS TEXT) LIKE ? ESCAPE '\\')")
        like = _like_pattern(q)
        args += [like, like, like]
    total = conn.execute(f"SELECT COUNT(*) FROM users {where}", args).fetchone()[0]
    page, pages = _page_number(page, total)
    rows = conn.execute(
        f"SELECT u.id, u.display_name, u.tg_username, u.telegram_id, u.is_active, "
        f"u.is_operator, u.is_suspicious, u.date_limit, u.created_at, "
        f"(SELECT COUNT(*) FROM dates d WHERE d.owner_id=u.id) AS n_dates, "
        f"(SELECT COUNT(*) FROM categories c WHERE c.owner_id=u.id) AS n_cats "
        f"FROM users u {where} ORDER BY u.created_at DESC, u.id DESC LIMIT ? OFFSET ?",
        args + [PAGE, (page - 1) * PAGE]).fetchall()
    return templates.TemplateResponse(
        request, "operator/users.html",
        octx(request, active="users", rows=rows, q=q, state=state, role=role,
             risk=risk,
             page=page, pages=pages, total=total))


def _target(conn, uid: int):
    """Пользователь-цель операторского действия. 404, если нет или это легаси."""
    u = get_user(conn, uid)
    if not u or u["telegram_id"] == 0:
        raise HTTPException(404)
    return u


@router.get("/users/{uid}", response_class=HTMLResponse)
def user_card(uid: int, request: Request, events_page: int = 1,
              votes_page: int = 1, conn=Depends(get_db)):
    u = _target(conn, uid)
    cats = conn.execute(
        "SELECT id, name, link_enabled, link_token, operator_review_pending, "
        "(SELECT COUNT(*) FROM date_categories dc WHERE dc.category_id=categories.id) "
        "AS n FROM categories WHERE owner_id=? ORDER BY created_at DESC, id DESC",
        (uid,),
    ).fetchall()
    dates_total = int(conn.execute(
        "SELECT COUNT(*) FROM dates WHERE owner_id=?", (uid,),
    ).fetchone()[0])
    events_page, events_pages = _page_number(
        events_page, dates_total, USER_CARD_PAGE,
    )
    dates = conn.execute(
        "SELECT id, name, archived_at, is_draft, operator_review_pending, origin "
        "FROM dates "
        "WHERE owner_id=? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (uid, USER_CARD_PAGE, (events_page - 1) * USER_CARD_PAGE),
    ).fetchall()
    votes_total = int(conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE user_id=?", (uid,),
    ).fetchone()[0])
    votes_page, votes_pages = _page_number(votes_page, votes_total, USER_CARD_PAGE)
    votes = conn.execute(
        "SELECT b.id, b.created_at, b.participation_withdrawn_at, "
        "d.id AS date_id, d.name AS date_name, c.id AS category_id, "
        "c.name AS category_name, c.owner_id, "
        "COALESCE(owner.display_name, owner.tg_username, 'Без имени') AS owner_name "
        "FROM bookings b JOIN dates d ON d.id=b.date_id "
        "JOIN categories c ON c.id=b.category_id "
        "JOIN users owner ON owner.id=c.owner_id "
        "WHERE b.user_id=? ORDER BY b.created_at DESC, b.id DESC LIMIT ? OFFSET ?",
        (uid, USER_CARD_PAGE, (votes_page - 1) * USER_CARD_PAGE),
    ).fetchall()
    card_query = urlencode([
        *([("events_page", str(events_page))] if events_page > 1 else []),
        *([("votes_page", str(votes_page))] if votes_page > 1 else []),
    ])
    card_return_to = f"/operator/users/{uid}" + (f"?{card_query}" if card_query else "")
    return templates.TemplateResponse(
        request, "operator/user_card.html",
        octx(
            request, active="users", u=u, cats=cats, dates=dates, votes=votes,
            cats_total=len(cats), dates_total=dates_total, votes_total=votes_total,
            events_page=events_page, events_pages=events_pages,
            votes_page=votes_page, votes_pages=votes_pages,
            card_return_to=card_return_to,
        ))


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
    log.warning(
        "Оператор изменил доступ пользователя",
        extra={"event": "operator_user_access_changed",
               "outcome": "enabled" if new else "disabled"},
    )
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
    # Сериализуем выдачу роли с переключением suspicious: две параллельные
    # формы не должны оставить взаимоисключающие флаги одновременно.
    if conn.execute("UPDATE users SET id=id WHERE id=?", (uid,)).rowcount == 0:
        raise HTTPException(404)
    u = _target(conn, uid)
    if u["id"] == request.state.user["id"]:
        raise HTTPException(400, "Нельзя снять роль оператора с самого себя")
    new = 0 if u["is_operator"] else 1
    if new and u["is_suspicious"]:
        raise HTTPException(
            409,
            "Сначала снимите пометку «Подозрительный», затем выдайте роль администратора",
        )
    conn.execute("UPDATE users SET is_operator=? WHERE id=?", (new, uid))
    conn.commit()
    log.warning(
        "Оператор изменил роль пользователя",
        extra={"event": "operator_role_changed",
               "outcome": "granted" if new else "revoked"},
    )
    return redir(_back(uid), "Назначен оператором" if new else "Роль оператора снята")


@router.post("/users/{uid}/suspicious")
def user_suspicious(uid: int, request: Request, enabled: int = Form(...),
                    conn=Depends(get_db)):
    """Идемпотентно включает адресную премодерацию будущего контента."""
    if enabled not in (0, 1):
        raise HTTPException(400, "Некорректное значение пометки")
    if conn.execute("UPDATE users SET id=id WHERE id=?", (uid,)).rowcount == 0:
        raise HTTPException(404)
    u = _target(conn, uid)
    if enabled and u["is_operator"]:
        raise HTTPException(409, "Администратора нельзя пометить подозрительным")
    previous = int(u["is_suspicious"])
    conn.execute("UPDATE users SET is_suspicious=? WHERE id=?", (enabled, uid))
    conn.commit()
    log.warning(
        "Оператор изменил режим премодерации пользователя",
        extra={
            "event": "operator_user_suspicious_changed",
            "target_id": uid,
            "outcome": "enabled" if enabled else "disabled",
        },
    )
    if previous == enabled:
        message = ("Премодерация уже включена" if enabled
                   else "Премодерация уже выключена")
    else:
        message = ("Новый контент пользователя будет ждать проверки" if enabled else
                   "Премодерация будущего контента выключена")
    return redir(_back(uid), message)


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
    log.warning(
        "Оператор удалил пользователя",
        extra={"event": "operator_user_deleted", "count": len(files),
               "outcome": "success"},
    )
    return redir("/operator/users", "Пользователь и все его данные удалены")


# ---------- жалобы (очередь модерации) ----------

@router.get("/reports", response_class=HTMLResponse)
def reports_list(request: Request, status: str = "open", target: str = "",
                 q: str = "", page: int = 1, conn=Depends(get_db)):
    if status not in ("open", "resolved", "dismissed", "all"):
        status = "open"
    target_filter = target if target in {"date", "category"} else ""
    q = _search_query(q)
    conds, args = [], []
    if status != "all":
        conds.append("r.status=?")
        args.append(status)
    if target_filter:
        conds.append("r.target_type=?")
        args.append(target_filter)
    if q:
        pattern = _like_pattern(q)
        conds.append(
            "(COALESCE(r.reason, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(c.name, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(d.name, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(cu.display_name, cu.tg_username, '') LIKE ? "
            "ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(du.display_name, du.tg_username, '') LIKE ? "
            "ESCAPE '\\' COLLATE NOCASE)"
        )
        args.extend([pattern] * 5)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    joins = (
        "FROM reports r "
        "LEFT JOIN categories c ON r.target_type='category' AND c.id=r.target_id "
        "LEFT JOIN users cu ON cu.id=c.owner_id "
        "LEFT JOIN dates d ON r.target_type<>'category' AND d.id=r.target_id "
        "LEFT JOIN users du ON du.id=d.owner_id "
    )
    total = int(conn.execute(
        f"SELECT COUNT(*) {joins}{where}", args,
    ).fetchone()[0])
    page, pages = _page_number(page, total)
    rows = conn.execute(
        "SELECT r.*, "
        "CASE WHEN r.target_type='category' THEN c.name ELSE d.name END AS target_name, "
        "CASE WHEN r.target_type='category' THEN c.owner_id ELSE d.owner_id END "
        " AS target_owner_id, "
        "CASE WHEN r.target_type='category' THEN cu.display_name ELSE du.display_name END "
        " AS target_owner, c.link_token AS target_link_token "
        f"{joins}{where} "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?",
        args + [PAGE, (page - 1) * PAGE]).fetchall()
    items = []
    for row in rows:
        target = {}
        if row["target_name"] is not None:
            target = {
                "name": row["target_name"],
                "owner_id": row["target_owner_id"],
                "owner": row["target_owner"],
            }
            if row["target_type"] == "category":
                target["link_token"] = row["target_link_token"]
        items.append({"r": row, "t": target})
    return templates.TemplateResponse(
        request, "operator/reports.html",
        octx(request, active="reports", items=items, status=status,
             target=target_filter,
             q=q, page=page, pages=pages, total=total))


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
        social_events.cancel_review_prompts_for_date(conn, tid)
        conn.execute("DELETE FROM dates WHERE id=?", (tid,))
        _cancel_empty_vote_deadlines(conn, affected)
    else:
        target_cat = _cat_or_404(conn, tid)
        _require_category_not_frozen(conn, target_cat)
        # События переживают удаление категории, поэтому их медиа трогать
        # нельзя. Удаляем только собственную картинку превью категории.
        files = [target_cat["og_image"]] if target_cat["og_image"] else []
        affected_dates = [int(row["date_id"]) for row in conn.execute(
            "SELECT date_id FROM date_categories WHERE category_id=?", (tid,),
        ).fetchall()]
        _queue_category_removal(conn, target_cat)
        conn.execute("DELETE FROM categories WHERE id=?", (tid,))
        for date_id in affected_dates:
            social_events.queue_review_prompts_for_date(conn, date_id)
    conn.execute(
        "UPDATE reports SET status='resolved', resolved_at=? "
        "WHERE target_type=? AND target_id=? AND status='open'",
        (now_iso(), tt, tid))
    conn.commit()
    for fn in files:
        images.delete_file(fn)
    log.warning(
        "Оператор удалил контент по жалобе",
        extra={"event": "operator_report_takedown", "operation": tt,
               "count": len(files), "outcome": "success"},
    )
    return redir("/operator/reports", "Контент удалён, жалоба закрыта")


# ---------- категории (все, всех пользователей) ----------

@router.get("/categories", response_class=HTMLResponse)
def cats_list(request: Request, q: str = "", link: str = "", review: str = "",
              page: int = 1, conn=Depends(get_db)):
    q = _search_query(q)
    link = link if link in {"enabled", "disabled"} else ""
    review = review if review in {"pending", "operator", "soft", "approved"} else ""
    conds, args = [], []
    if q:
        conds.append(
            "(c.name LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR u.display_name LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR u.tg_username LIKE ? ESCAPE '\\' COLLATE NOCASE)"
        )
        like = _like_pattern(q)
        args += [like, like, like]
    if link == "enabled":
        conds.append("c.link_enabled=1")
    elif link == "disabled":
        conds.append("c.link_enabled=0")
    if review == "pending":
        conds.append("(c.is_reviewed=0 OR c.operator_review_pending=1)")
    elif review == "operator":
        conds.append("c.operator_review_pending=1")
    elif review == "soft":
        conds.append("c.is_reviewed=0 AND c.operator_review_pending=0")
    elif review == "approved":
        conds.append("c.is_reviewed=1 AND c.operator_review_pending=0")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM categories c JOIN users u ON u.id=c.owner_id {where}",
        args).fetchone()[0]
    page, pages = _page_number(page, total)
    rows = conn.execute(
        f"SELECT c.id, c.name, c.link_enabled, c.link_token, c.is_reviewed, "
        f"c.operator_review_pending, "
        f"c.owner_id, "
        f"u.display_name AS owner, "
        f"(SELECT COUNT(*) FROM date_categories dc WHERE dc.category_id=c.id) AS n "
        f"FROM categories c JOIN users u ON u.id=c.owner_id {where} "
        f"ORDER BY c.created_at DESC, c.id DESC LIMIT ? OFFSET ?",
        args + [PAGE, (page - 1) * PAGE]).fetchall()
    return templates.TemplateResponse(
        request, "operator/categories.html",
        octx(request, active="cats", rows=rows, q=q, link=link, review=review,
             page=page, pages=pages, total=total, base_url=BASE_URL,
             editor_return_to=(
                  "/operator/categories"
                  + ("?" + urlencode([
                      *([("link", link)] if link else []),
                      *([("review", review)] if review else []),
                      *([("q", q)] if q else []),
                      *([("page", str(page))] if page > 1 else []),
                  ]) if link or review or q or page > 1 else "")
             )))


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
    log.warning(
        "Оператор изменил доступность ссылки",
        extra={"event": "operator_category_link_changed",
               "outcome": "enabled" if new else "disabled"},
    )
    return redir("/operator/categories",
                 "Ссылка включена" if new else "Ссылка выключена")


@router.post("/categories/{cid}/delete")
def cat_delete(cid: int, request: Request, conn=Depends(get_db)):
    """Удаляет категорию (связи date_categories — каскадом). События остаются
    у владельца, как и в кабинете."""
    cat = _cat_or_404(conn, cid)
    _require_category_not_frozen(conn, cat)
    affected_dates = [int(row["date_id"]) for row in conn.execute(
        "SELECT date_id FROM date_categories WHERE category_id=?", (cid,),
    ).fetchall()]
    _queue_category_removal(conn, cat)
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    for date_id in affected_dates:
        social_events.queue_review_prompts_for_date(conn, date_id)
    conn.commit()
    if cat["og_image"]:
        images.delete_file(cat["og_image"])
    log.warning(
        "Оператор удалил категорию",
        extra={"event": "operator_category_deleted", "outcome": "success"},
    )
    return redir("/operator/categories", "Категория удалена (события остались)")


# ---------- события (все, всех пользователей) ----------

@router.get("/dates", response_class=HTMLResponse)
def dates_list(request: Request, q: str = "", flt: str = "", page: int = 1,
               conn=Depends(get_db)):
    q = _search_query(q)
    flt = flt if flt in {
        "active", "review", "draft", "booked", "reported", "archived",
    } else ""
    conds, args = [], []
    reported_join = ""
    report_count_sql = (
        "(SELECT COUNT(*) FROM reports r "
        "INDEXED BY idx_reports_open_date_target "
        "WHERE r.target_type='date' AND r.target_id=d.id AND r.status='open')"
    )
    if q:
        conds.append(
            "(d.name LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR u.display_name LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR u.tg_username LIKE ? ESCAPE '\\' COLLATE NOCASE)"
        )
        like = _like_pattern(q)
        args += [like, like, like]
    if flt == "active":
        conds.append(
            "d.archived_at IS NULL AND d.is_draft=0 "
            "AND d.operator_review_pending=0"
        )
    elif flt == "review":
        conds.append("d.operator_review_pending=1")
    elif flt == "draft":
        conds.append("d.is_draft=1")
    elif flt == "booked":
        conds.append("EXISTS(SELECT 1 FROM bookings b WHERE b.date_id=d.id)")
    elif flt == "reported":
        reported_join = (
            " JOIN (SELECT target_id, COUNT(*) AS reports FROM reports "
            " INDEXED BY idx_reports_open_date_target "
            " WHERE target_type='date' AND status='open' GROUP BY target_id) rr "
            " ON rr.target_id=d.id"
        )
        report_count_sql = "rr.reports"
    elif flt == "archived":
        conds.append("d.archived_at IS NOT NULL")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM dates d JOIN users u ON u.id=d.owner_id"
        f"{reported_join} {where}",
        args).fetchone()[0]
    page, pages = _page_number(page, total)
    rows = conn.execute(
        f"SELECT d.id, d.name, d.is_draft, d.operator_review_pending, "
        f"d.origin, d.archived_at, d.owner_id, "
        f"u.display_name AS owner, "
        f"(SELECT COUNT(*) FROM bookings b WHERE b.date_id=d.id) AS books, "
        f"{report_count_sql} AS reports "
        f"FROM dates d JOIN users u ON u.id=d.owner_id{reported_join} {where} "
        f"ORDER BY d.created_at DESC, d.id DESC LIMIT ? OFFSET ?",
        args + [PAGE, (page - 1) * PAGE]).fetchall()
    return templates.TemplateResponse(
        request, "operator/dates.html",
        octx(request, active="dates", rows=rows, q=q, flt=flt, page=page,
             pages=pages, total=total,
             editor_return_to=(
                 "/operator/dates"
                 + ("?" + urlencode([
                     *( [("flt", flt)] if flt else [] ),
                     *( [("q", q)] if q else [] ),
                     *( [("page", str(page))] if page > 1 else [] ),
                 ]) if flt or q or page > 1 else "")
             )))


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
    social_events.cancel_review_prompts_for_date(conn, did)
    conn.execute("DELETE FROM dates WHERE id=?", (did,))
    _cancel_empty_vote_deadlines(conn, affected)
    conn.execute(
        "UPDATE reports SET status='resolved', resolved_at=? "
        "WHERE target_type='date' AND target_id=? AND status='open'",
        (now_iso(), did))
    conn.commit()
    for fn in files:
        images.delete_file(fn)
    log.warning(
        "Оператор удалил событие",
        extra={"event": "operator_date_deleted", "count": len(files),
               "outcome": "success"},
    )
    return redir("/operator/dates", "Событие удалено")


# ---------- голоса / взаимодействия (обзор для разбора споров) ----------

@router.get("/bookings", response_class=HTMLResponse)
def bookings_list(request: Request, q: str = "", kind: str = "", state: str = "",
                  voter_id: int | None = None, page: int = 1,
                  conn=Depends(get_db)):
    q = _search_query(q)
    kind = kind if kind in {"account", "legacy"} else ""
    state = state if state in {"active", "withdrawn"} else ""
    voter_id = voter_id if voter_id is not None and voter_id > 0 else None
    conds, args = [], []
    if q:
        conds.append(
            "(COALESCE(vu.display_name, vu.tg_username, g.name, '') LIKE ? "
            "ESCAPE '\\' COLLATE NOCASE "
            "OR d.name LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR c.name LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(u.display_name, u.tg_username, '') LIKE ? "
            "ESCAPE '\\' COLLATE NOCASE)"
        )
        like = _like_pattern(q)
        args.extend([like] * 4)
    if kind == "account":
        conds.append("b.user_id IS NOT NULL")
    elif kind == "legacy":
        conds.append("b.user_id IS NULL")
    if state == "active":
        conds.append("b.participation_withdrawn_at IS NULL")
    elif state == "withdrawn":
        conds.append("b.participation_withdrawn_at IS NOT NULL")
    if voter_id is not None:
        conds.append("b.user_id=?")
        args.append(voter_id)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    joins = (
        "FROM bookings b JOIN dates d ON d.id=b.date_id "
        "JOIN categories c ON c.id=b.category_id JOIN users u ON u.id=c.owner_id "
        "LEFT JOIN users vu ON vu.id=b.user_id "
        "LEFT JOIN guests g ON g.token=b.guest_token "
    )
    total = conn.execute(
        f"SELECT COUNT(*) {joins}{where}", args).fetchone()[0]
    page, pages = _page_number(page, total)
    rows = conn.execute(
        f"SELECT b.id, b.created_at, b.user_id, b.participation_withdrawn_at, "
        f"COALESCE(vu.display_name, vu.tg_username, g.name, '—') AS guest, "
        f"d.name AS date_name, c.name AS cat_name, c.owner_id, "
        f"u.display_name AS owner {joins}"
        f"{where} ORDER BY b.created_at DESC, b.id DESC LIMIT ? OFFSET ?",
        args + [PAGE, (page - 1) * PAGE]).fetchall()
    return templates.TemplateResponse(
        request, "operator/bookings.html",
        octx(request, active="bookings", rows=rows, q=q, kind=kind, state=state,
             voter_id=voter_id, page=page, pages=pages, total=total))


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
    log.warning(
        "Оператор удалил голос",
        extra={"event": "operator_booking_deleted", "outcome": "success"},
    )
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
    moderation_outcome = (
        f"users_{'on' if moderate_users else 'off'}_"
        f"categories_{'on' if moderate_categories else 'off'}"
    )
    log.warning(
        "Оператор изменил настройки модерации",
        extra={"event": "operator_moderation_settings_changed",
               "outcome": moderation_outcome},
    )
    return redir("/operator/settings", "Настройки сохранены")


# ---------- очередь модерации ----------

@router.get("/review", response_class=HTMLResponse)
def review_queue(request: Request, users_page: int = 1,
                 categories_page: int = 1, dates_page: int = 1,
                 conn=Depends(get_db)):
    users_total = int(conn.execute(
        "SELECT COUNT(*) FROM users "
        "WHERE COALESCE(telegram_id,-1)<>0 AND is_reviewed=0",
    ).fetchone()[0])
    users_page, users_pages = _page_number(users_page, users_total, PAGE)
    users_q = conn.execute(
        "SELECT id, display_name, tg_username, telegram_id, created_at, "
        "(SELECT COUNT(*) FROM categories c WHERE c.owner_id=users.id) AS n_cats, "
        "(SELECT COUNT(*) FROM dates d WHERE d.owner_id=users.id) AS n_dates "
        "FROM users WHERE COALESCE(telegram_id,-1)<>0 AND is_reviewed=0 "
        "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (PAGE, (users_page - 1) * PAGE),
    ).fetchall()
    categories_total = int(conn.execute(
        "SELECT COUNT(*) FROM categories "
        "WHERE is_reviewed=0 OR operator_review_pending=1",
    ).fetchone()[0])
    categories_page, categories_pages = _page_number(
        categories_page, categories_total, PAGE,
    )
    cats_q = conn.execute(
        "SELECT c.id, c.name, c.link_token, c.link_enabled, c.is_reviewed, "
        "c.operator_review_pending, c.created_at, c.owner_id, "
        "COALESCE(u.display_name, u.tg_username, 'Без имени') AS owner, "
        "(SELECT COUNT(*) FROM date_categories dc WHERE dc.category_id=c.id) AS n "
        "FROM categories c JOIN users u ON u.id=c.owner_id "
        "WHERE c.is_reviewed=0 OR c.operator_review_pending=1 "
        "ORDER BY c.created_at DESC, c.id DESC LIMIT ? OFFSET ?",
        (PAGE, (categories_page - 1) * PAGE),
    ).fetchall()
    dates_total = int(conn.execute(
        "SELECT COUNT(*) FROM dates WHERE operator_review_pending=1",
    ).fetchone()[0])
    dates_page, dates_pages = _page_number(dates_page, dates_total, PAGE)
    dates_q = conn.execute(
        "SELECT d.id, d.name, d.created_at, d.owner_id, d.origin, d.is_draft, "
        "COALESCE(owner.display_name, owner.tg_username, 'Без имени') AS owner, "
        "COALESCE(author.display_name, author.tg_username, "
        "owner.display_name, owner.tg_username, 'Без имени') AS author, "
        "(SELECT GROUP_CONCAT(c.name, ', ') FROM date_categories dc "
        " JOIN categories c ON c.id=dc.category_id WHERE dc.date_id=d.id) AS categories "
        "FROM dates d JOIN users owner ON owner.id=d.owner_id "
        "LEFT JOIN users author ON author.id=d.proposed_by "
        "WHERE d.operator_review_pending=1 "
        "ORDER BY d.created_at DESC, d.id DESC LIMIT ? OFFSET ?",
        (PAGE, (dates_page - 1) * PAGE),
    ).fetchall()
    return templates.TemplateResponse(
        request, "operator/review.html",
        octx(request, active="review", users_q=users_q, cats_q=cats_q,
             dates_q=dates_q, users_total=users_total, users_page=users_page,
             users_pages=users_pages, categories_total=categories_total,
             categories_page=categories_page, categories_pages=categories_pages,
             dates_total=dates_total, dates_page=dates_page,
             dates_pages=dates_pages,
             base_url=BASE_URL))


@router.post("/review/user/{uid}/approve")
def review_user_approve(uid: int, request: Request, conn=Depends(get_db)):
    _target(conn, uid)
    conn.execute("UPDATE users SET is_reviewed=1 WHERE id=?", (uid,))
    conn.commit()
    log.warning(
        "Оператор одобрил пользователя",
        extra={"event": "operator_user_approved", "outcome": "success"},
    )
    return redir("/operator/review", "Пользователь одобрен")


@router.post("/review/category/{cid}/approve")
def review_category_approve(cid: int, request: Request, conn=Depends(get_db)):
    if conn.execute("UPDATE categories SET id=id WHERE id=?", (cid,)).rowcount == 0:
        raise HTTPException(404)
    _cat_or_404(conn, cid)
    conn.execute(
        "UPDATE categories SET is_reviewed=1, operator_review_pending=0 "
        "WHERE id=?",
        (cid,),
    )
    conn.commit()
    log.warning(
        "Оператор одобрил категорию",
        extra={"event": "operator_category_approved", "target_id": cid,
               "outcome": "success"},
    )
    return redir("/operator/review", "Подборка одобрена и снята с проверки")


@router.post("/review/date/{did}/approve")
def review_date_approve(did: int, request: Request, conn=Depends(get_db)):
    d = _date_or_404(conn, did)
    if not d["operator_review_pending"]:
        return redir("/operator/review", "Событие уже проверено")
    detached_categories = _detach_date_from_frozen_categories(conn, did)
    d = _date_or_404(conn, did)
    _validate_date_after_open_deadlines(conn, did, d["starts_at"])
    conn.execute(
        "UPDATE dates SET operator_review_pending=0 WHERE id=?", (did,),
    )
    conn.commit()
    log.warning(
        "Оператор одобрил событие",
        extra={"event": "operator_date_approved", "target_id": did,
               "outcome": "success"},
    )
    suffix = "; осталось одобрение владельца" if d["is_draft"] else ""
    if detached_categories:
        suffix += (
            f"; исключено из завершённых подборок: {len(detached_categories)}"
        )
    return redir("/operator/review", f"Событие одобрено{suffix}")


@router.post("/review/category/{cid}/reject")
def review_category_reject(cid: int, request: Request, conn=Depends(get_db)):
    if conn.execute("UPDATE categories SET id=id WHERE id=?", (cid,)).rowcount == 0:
        raise HTTPException(404)
    cat = _cat_or_404(conn, cid)
    if not cat["operator_review_pending"]:
        raise HTTPException(409, "Отклонять здесь можно только удержанную подборку")
    affected_dates = [int(row["date_id"]) for row in conn.execute(
        "SELECT date_id FROM date_categories WHERE category_id=?", (cid,),
    ).fetchall()]
    _queue_category_removal(conn, cat)
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    for date_id in affected_dates:
        social_events.queue_review_prompts_for_date(conn, date_id)
    conn.commit()
    if cat["og_image"]:
        images.delete_file(cat["og_image"])
    log.warning(
        "Оператор отклонил подборку на премодерации",
        extra={"event": "operator_category_rejected", "target_id": cid,
               "outcome": "success"},
    )
    return redir("/operator/review", "Подборка отклонена и удалена")


@router.post("/review/date/{did}/reject")
def review_date_reject(did: int, request: Request, conn=Depends(get_db)):
    if conn.execute("UPDATE dates SET id=id WHERE id=?", (did,)).rowcount == 0:
        raise HTTPException(404)
    d = _date_or_404(conn, did)
    if not d["operator_review_pending"]:
        raise HTTPException(409, "Отклонять здесь можно только удержанное событие")
    files = [row["filename"] for row in conn.execute(
        "SELECT filename FROM date_images WHERE date_id=?", (did,),
    )]
    files += [row["filename"] for row in conn.execute(
        "SELECT filename FROM date_videos WHERE date_id=?", (did,),
    )]
    affected = _queue_date_removal_from_categories(conn, did)
    social_events.cancel_review_prompts_for_date(conn, did)
    conn.execute("DELETE FROM dates WHERE id=?", (did,))
    _cancel_empty_vote_deadlines(conn, affected)
    conn.execute(
        "UPDATE reports SET status='resolved', resolved_at=? "
        "WHERE target_type='date' AND target_id=? AND status='open'",
        (now_iso(), did),
    )
    conn.commit()
    for filename in files:
        images.delete_file(filename)
    log.warning(
        "Оператор отклонил событие на премодерации",
        extra={"event": "operator_date_rejected", "target_id": did,
               "count": len(files), "outcome": "success"},
    )
    return redir("/operator/review", "Событие отклонено и удалено")
