"""Админка: вход, дашборд, категории, события, вопросы, экспорт.

Доступ — сессия (SessionMiddleware) + CSRF-токен на каждый POST.
"""

import csv
import io
import json
import logging
import os
import re
import secrets
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import segno

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Request, UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from urllib.parse import parse_qsl, urlencode, urlsplit

import appearance
import backup
import category_access
import community_feed as community_recommendations
import db
import images
import metrics
import moderation
import notify
import places
import public_routes
import social_events
import settings as app_settings
import voting
import voting_events
from config import BASE_URL, SUPPORT_CONTACT, OAUTH_PROVIDERS, OAUTH_LABELS
from guests import gname
from helpers import (clean_text, new_link_token, normalize_period, now_iso, now_naive,
                     parse_birth_date, parse_dt_local, parse_links, pay_label, plural)
from fastapi.responses import JSONResponse
from ownership import get_owned_category, get_owned_date
from public_routes import (add_photos, copy_date_media_and_links, insert_date,
                           next_cat_pos, notify_admin, notify_user, parse_capacity, ranged_file,
                           save_links, VIDEO_TYPES)
from ratelimit import user_throttle
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
            "Голосование уже завершено: состав и порядок событий зафиксированы",
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
                f"Начало события должно быть позже дедлайна категории «{cat['name']}»",
            )


def actx(request: Request, conn, **extra) -> dict:
    user = request.state.user
    question_unread = conn.execute(
        "SELECT COUNT(*) FROM questions q WHERE q.answer IS NULL "
        "AND q.date_id IN (SELECT id FROM dates WHERE owner_id=?)",
        (user["id"],),
    ).fetchone()[0]
    review_waiting = extra.pop("review_waiting", None)
    if review_waiting is None:
        review_waiting = social_events.review_waiting_count(conn, int(user["id"]))
    ctx = {
        "request": request,
        "user": user,
        "question_unread": question_unread,
        "review_waiting": review_waiting,
        "unread": question_unread + review_waiting,
        "csrf": request.session.get("csrf", ""),
    }
    ctx.update(extra)
    return ctx


@router.post("/logout")
def logout(request: Request):
    """Выход только по POST с CSRF: logout по GET можно навязать ссылкой."""
    request.session.clear()
    return RedirectResponse("/", status_code=303)


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
    profile_sections = _profile_sections_context(
        conn, request.state.user["id"], request.state.user["id"],
        request.query_params,
    )
    return templates.TemplateResponse(
        request, "admin/profile.html",
        actx(request, conn, active="profile", oauth_providers=providers,
             me=request.state.user,
             first_category_id=first_category["id"] if first_category else None,
             profile_base_url="/admin/profile", public_events_label="Публичные события",
             profile_skin_suffix="", profile_embedded=True,
             profile_return_url=(
                 f"/admin/profile?tab={profile_sections['tab']}"
                 f"&page={profile_sections['page']}"
             ), **profile_sections))


@router.post("/profile")
def profile_save(request: Request,
                 display_name: str = Form(""),
                 birth_date: str = Form(""),
                 gender: str = Form(""),
                 cursor_effects: str | None = Form(None),
                 admin_skin: str | None = Form(None),
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
    skin = appearance.normalize_skin(
        admin_skin, default=request.state.user["admin_skin"]
    )
    if new_avatar:
        conn.execute(
            "UPDATE users SET display_name=?, birth_date=?, gender=?, avatar_path=?, "
            "cursor_effects=?, admin_skin=? WHERE id=?",
            (name, bdate, g, new_avatar, effects, skin, uid))
    else:
        conn.execute(
            "UPDATE users SET display_name=?, birth_date=?, gender=?, "
            "cursor_effects=?, admin_skin=? WHERE id=?",
            (name, bdate, g, effects, skin, uid))
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


@router.post("/profile/telegram/unlink")
def profile_telegram_unlink(request: Request, conn=Depends(get_db)):
    """Отвязывает Telegram-способ входа, не оставляя пользователя без доступа.

    bot_linked описывает разрешение уведомлений, а плитка в профиле — способ
    входа. Поэтому очищаем оба состояния, только если остаётся OAuth-привязка.
    """
    uid = int(request.state.user["id"])
    if request.state.user["telegram_id"] is None:
        return redir("/admin/profile", "Telegram уже отвязан")
    oauth_links = conn.execute(
        "SELECT COUNT(*) FROM oauth_accounts WHERE user_id=?", (uid,),
    ).fetchone()[0]
    if not oauth_links:
        return redir(
            "/admin/profile",
            "Нельзя отвязать единственный способ входа — сначала привяжи другой.",
        )
    # Незавершённый purpose-bound код не должен тут же молча вернуть Telegram,
    # который пользователь явно отвязал.
    conn.execute(
        "DELETE FROM login_codes WHERE user_id=? AND purpose='link'", (uid,),
    )
    conn.execute(
        "UPDATE users SET telegram_id=NULL, tg_username=NULL, bot_linked=0 "
        "WHERE id=?",
        (uid,),
    )
    conn.commit()
    return redir("/admin/profile", "Telegram отвязан")


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
def profile_avatar(filename: str, request: Request, w: int | None = None):
    """Отдаёт аватар текущего пользователя. Гейт: filename должен совпадать с
    его собственным avatar_path — чужой аватар по прямой ссылке не отдаём."""
    if not images.SAFE_FILENAME.match(filename) \
            or filename != request.state.user["avatar_path"]:
        raise HTTPException(404)
    try:
        path = images.responsive_image(filename, w)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=300"})


GNAME_SQL = "COALESCE(g.name, 'Человек #' || substr(COALESCE({t}, '??????'), 1, 6))"


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn=Depends(get_db)):
    uid = request.state.user["id"]
    stats = {
        "cats": conn.execute(
            "SELECT COUNT(*) FROM categories WHERE owner_id=?", (uid,)).fetchone()[0],
        "active": conn.execute(
            "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NULL",
            (uid,)).fetchone()[0],
        "archived": conn.execute(
            "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NOT NULL",
            (uid,)).fetchone()[0],
        # выборы считаем только по активным (не архивным) событиям владельца
        "bookings": conn.execute(
            "SELECT COUNT(*) FROM bookings b JOIN dates d ON d.id=b.date_id "
            "WHERE d.owner_id=? AND d.archived_at IS NULL", (uid,)).fetchone()[0],
        "unread_q": conn.execute(
            "SELECT COUNT(*) FROM questions q WHERE q.is_read=0 "
            "AND q.date_id IN (SELECT id FROM dates WHERE owner_id=?)",
            (uid,)).fetchone()[0],
    }

    # Блок «Поделиться»: категории владельца с включённой секретной ссылкой.
    # Для выбранной (или первой) рисуем QR прямо на сервере — инлайновый SVG,
    # под CSP не нужен ни внешний скрипт, ни data:-картинка.
    share_cats = conn.execute(
        "SELECT id, name, category_skin, link_token, og_title, og_desc, og_image, og_focus, "
        "use_default_preview, choice_mode, voting_deadline, voting_status "
        "FROM categories "
        "WHERE owner_id=? AND link_enabled=1 AND link_token IS NOT NULL "
        "AND operator_review_pending=0 "
        "ORDER BY created_at DESC", (uid,)).fetchall()
    sel = request.query_params.get("share")
    share = next((c for c in share_cats if str(c["id"]) == sel), None) \
        or (share_cats[0] if share_cats else None)
    share_url = qr_svg = None
    share_preview_revision = ""
    if share:
        share_url = f"{BASE_URL}/c/{share['link_token']}"
        qr_svg = _qr_svg(share_url, request.state.user["admin_skin"])
        share_sources = _category_collage_sources(conn, int(share["id"]))
        share_custom = share["og_image"] \
            if images.upload_image_exists(share["og_image"]) else None
        share_preview_revision = images.og_preview_revision(
            share_sources,
            appearance.normalize_skin(share["category_skin"]),
            custom_image=share_custom,
            custom_focus=share["og_focus"],
            use_default=bool(share["use_default_preview"]),
        )

    return templates.TemplateResponse(
        request, "admin/dashboard.html",
        actx(request, conn, active="dash", stats=stats,
             share_cats=share_cats, share=share, share_url=share_url, qr_svg=qr_svg,
             share_preview_revision=share_preview_revision))


def _qr_svg(data: str, skin: str = "friends") -> str:
    """QR-код ссылки как инлайновый SVG (без внешних запросов и без PIL).

    Основные модули и поисковые рамки окрашены в палитру выбранного оформления
    кабинета. Белая тихая зона остаётся частью скачиваемого SVG, поэтому код
    надёжно читается не только на странице, но и после сохранения в файл.
    """
    palette = {
        "friends": {
            "data": "#354483",
            "finder": "#176f6c",
            "detail": "#4b59a6",
        },
        "romantic": {
            "data": "#633743",
            "finder": "#a84f63",
            "detail": "#8f4a58",
        },
    }[skin if skin in ("friends", "romantic") else "friends"]
    buf = io.BytesIO()
    segno.make(data, error="m").save(
        buf, kind="svg", scale=4, border=4,
        dark=palette["data"], light="#ffffff",
        data_dark=palette["data"], finder_dark=palette["finder"],
        alignment_dark=palette["detail"], format_dark=palette["detail"],
        version_dark=palette["detail"], timing_dark=palette["detail"],
        separator="#ffffff", quiet_zone="#ffffff",
        svgclass="qr-svg", omitsize=True, xmldecl=False,
        title="QR-код ссылки date4you",
        desc="Откройте камеру телефона и наведите её на код")
    return buf.getvalue().decode("utf-8")


# ---------------------------------------------------------------------------
# Лента событий-комьюнити (на главной вместо «Последних действий»)
# ---------------------------------------------------------------------------
# Показываем чужие публичные активные события: карточка = фото + название +
# когда/место/комментарий + автор + действия добавления, жалобы и шаринга.
# Категорию и
# модификатор оплаты НЕ показываем (решение владельца). Бесконечный скролл —
# Рекомендованное окно использует непрозрачный курсор; после него сохраняется
# keyset-пагинация по id, чтобы старые события не исчезали из ленты.

def _community_cards_page(conn, viewer_id: int, cursor: object = None):
    ranked = community_recommendations.page(
        conn, viewer_id, cursor, now=now_naive(),
    )
    media = public_routes._batch_media(conn, [r["id"] for r in ranked.rows])
    cards = []
    for r in ranked.rows:
        d = public_routes.date_payload_from(r, media)
        d["owner_display"] = (r["owner_name"] or r["owner_username"]
                              or f"Человек #{r['owner_id']}")
        cards.append(d)
    return cards, ranked


def _community_cards(conn, viewer_id: int, cursor: object = None):
    """Совместимый helper для тестов и внутренних вызовов."""
    cards, ranked = _community_cards_page(conn, viewer_id, cursor)
    return cards, ranked.next_cursor


@router.get("/community", response_class=HTMLResponse)
def community_feed(request: Request, conn=Depends(get_db)):
    """HTML-фрагмент со следующей страницей ленты (для бесконечного скролла).
    Возвращает только карточки + маркер курсора — фронт дописывает их в ленту."""
    cards, ranked = _community_cards_page(
        conn, request.state.user["id"], request.query_params.get("cursor"),
    )
    metrics.observe_community_feed(
        mode=ranked.mode,
        candidate_count=ranked.candidate_count,
        returned_count=len(cards),
    )
    return templates.TemplateResponse(
        request, "admin/_community_cards.html",
        {"request": request, "cards": cards,
         "next_cursor": ranked.next_cursor})


@router.get("/community/date/{did}", response_class=HTMLResponse)
def community_widget(did: int, request: Request, conn=Depends(get_db)):
    """Мини-виджет одного события из ленты (открывается в модалке). Только
    публичное активное чужое событие; иначе 404."""
    r = conn.execute(
        "SELECT d.*, u.display_name AS owner_name, u.tg_username AS owner_username, "
        "u.avatar_path AS owner_avatar "
        "FROM dates d JOIN users u ON u.id=d.owner_id "
        "WHERE d.id=? AND d.is_public=1 AND d.is_draft=0 "
        "AND d.operator_review_pending=0 AND d.archived_at IS NULL",
        (did,)).fetchone()
    if not r:
        raise HTTPException(404, "Событие не найдено")

    return _event_widget_response(
        request, conn, r, viewer=request.state.user,
        widget_skin=request.state.user["admin_skin"],
    )


def _event_widget_response(request: Request, conn, row, *, viewer=None,
                           widget_skin: str | None = None):
    """Рисует общий встроенный виджет ленты и профилей."""
    did = int(row["id"])
    media = public_routes._batch_media(conn, [did])
    d = public_routes.date_payload_from(row, media)
    d["owner_display"] = (row["owner_name"] or row["owner_username"]
                          or f"Человек #{row['owner_id']}")
    is_mine = bool(
        viewer is not None
        and int(row["owner_id"]) == int(viewer["id"])
    )
    wanted_by_me = False
    want_action_available = False
    want = None
    if viewer is not None and not is_mine:
        want = conn.execute(
            "SELECT is_public FROM date_wants WHERE user_id=? AND date_id=?",
            (viewer["id"], did),
        ).fetchone()
        has_want = want is not None
        want_action_available = social_events.want_action_available(
            conn, did, int(viewer["id"]), now=now_naive(),
        )
        wanted_by_me = has_want and want_action_available
    return templates.TemplateResponse(
        request, "admin/_community_widget.html",
        {"request": request, "d": d, "me": viewer,
         "is_mine": is_mine, "wanted_by_me": wanted_by_me,
         "want_action_available": want_action_available,
         "admin_skin": appearance.normalize_skin(widget_skin)})


@router.get("/uploads/{filename}")
def admin_image(filename: str, request: Request, w: int | None = None,
                conn=Depends(get_db)):
    if not images.SAFE_FILENAME.match(filename):
        raise HTTPException(404)
    # файл виден, только если принадлежит владельцу: фото/видео события или
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
    ext = filename.rsplit(".", 1)[-1]
    if ext in VIDEO_TYPES:
        if w is not None:
            raise HTTPException(404)
        path = images.UPLOAD_DIR / filename
        if not path.exists():
            raise HTTPException(404)
        return ranged_file(path, VIDEO_TYPES[ext], request)
    try:
        path = images.responsive_image(filename, w)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=3600"})


# ----- Экспорт ---------------------------------------------------------------

def _require_operator(request: Request) -> None:
    """Операторские экспорт/импорт — только операторам, остальным отвечаем 404."""
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
    # гости — только те, кто фигурирует в событиях/голосах/вопросах владельца
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
    w.writerow(["id", "Название", "Место", "Начало", "Конец", "Оплата", "Модерация",
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
                             f'attachment; filename="date4you-events-{day}.csv"'})


def _account_media_names(data: dict, user) -> set[str]:
    """Исходные uploads, принадлежащие одному аккаунту и описанные export.json."""
    names = {user["avatar_path"]} if user["avatar_path"] else set()
    names.update(c["og_image"] for c in data["categories"] if c.get("og_image"))
    for date in data["dates"]:
        names.update(date["images"])
        names.update(date["videos"])
    return names


def _write_account_archive(path: str, data: dict, user) -> None:
    """Пишет переносимый ZIP без общей SQLite-базы и чужих файлов."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "export.json", json.dumps(data, ensure_ascii=False, indent=2),
        )
        for filename in sorted(_account_media_names(data, user)):
            # Имена обычно пришли из БД, но повреждённая/ручная запись не должна
            # превратить экспорт в чтение произвольного файла рядом с uploads.
            if (not images.SAFE_FILENAME.fullmatch(filename)
                    or Path(filename).name != filename):
                continue
            source = images.UPLOAD_DIR / filename
            if source.is_file() and not source.is_symlink():
                archive.write(source, arcname=f"uploads/{filename}")


def _write_platform_backup(path: str, database_snapshot: Path) -> None:
    """Пишет операторский ZIP: консистентный app.db и все исходные uploads."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(database_snapshot, arcname="app.db")
        for source in sorted(images.UPLOAD_DIR.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(images.UPLOAD_DIR)
            archive.write(source, arcname=(Path("uploads") / relative).as_posix())


def _zip_response(writer, filename: str) -> FileResponse:
    """Создаёт временный ZIP и гарантирует уборку при ошибке и после ответа."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    try:
        writer(tmp.name)
    except Exception:
        os.unlink(tmp.name)
        raise
    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=filename,
        headers={"Cache-Control": "private, no-store",
                 "X-Content-Type-Options": "nosniff"},
        background=BackgroundTask(os.unlink, tmp.name),
    )


@router.get("/export/account-archive")
def export_account_archive(request: Request, conn=Depends(get_db)):
    """export.json и только исходные медиа текущего аккаунта; общей БД нет."""
    user = request.state.user
    data = _full_dump(conn, user["id"])
    day = now_naive().strftime("%Y-%m-%d")
    return _zip_response(
        lambda path: _write_account_archive(path, data, user),
        f"date4you-account-{day}.zip",
    )


@router.get("/export/platform-backup")
def export_platform_backup(request: Request):
    """Операторский снимок всей SQLite-базы и всех исходных медиа из uploads."""
    _require_operator(request)
    database_snapshot = backup.make_backup()
    day = now_naive().strftime("%Y-%m-%d")
    return _zip_response(
        lambda path: _write_platform_backup(path, database_snapshot),
        f"date4you-platform-backup-{day}.zip",
    )


@router.get("/export/archive", include_in_schema=False)
def export_archive(request: Request):
    """Обратносуместимый URL прежнего операторского «полного архива»."""
    return export_platform_backup(request)


@router.post("/import/json")
async def import_json(request: Request, file: UploadFile = File(...),
                      conn=Depends(get_db)):
    """Импорт данных из нашего же export.json — ДОЗАПИСЬЮ к аккаунту оператора.

    Добавляет категории и события (с ссылками и привязкой фото/видео по именам
    файлов) как НОВЫЕ записи. Существующие данные не трогает и не дублирует
    осознанно — это аддитивный импорт. Голоса/вопросы/гостей не переносим:
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
        category_skin = appearance.normalize_skin(
            c.get("category_skin"), default=appearance.ROMANTIC
        )
        choice_mode = str(c.get("choice_mode") or "").strip()
        voting_deadline = str(c.get("voting_deadline") or "").strip()
        if choice_mode not in voting.CHOICE_MODES or not voting_deadline:
            raise HTTPException(
                400,
                f"У импортируемой категории «{name}» нет обязательного дедлайна",
            )
        try:
            datetime.fromisoformat(voting_deadline)
        except ValueError:
            raise HTTPException(400, f"У категории «{name}» некорректный дедлайн")
        token = new_link_token()
        cur = conn.execute(
            "INSERT INTO categories(owner_id, name, category_skin, description, link_token, "
            "link_enabled, moderate_proposals, choice_mode, "
            "voting_deadline, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uid, name, category_skin,
             clean_text(str(c.get("description") or ""), 1000, "Описание"),
             token, 1 if c.get("link_enabled", 1) else 0,
             1 if c.get("moderate_proposals") else 0,
             choice_mode, voting_deadline, now_iso()))
        if c.get("id") is not None:
            cat_map[c["id"]] = cur.lastrowid
        n_cats += 1
    # события
    for d in data["dates"]:
        if not isinstance(d, dict):
            continue
        name = clean_text(str(d.get("name") or ""), 200, "Название") or "Без названия"
        capacity_value = parse_capacity(d.get("capacity", 1))
        did = insert_date(
            conn, name=name, place=d.get("place"), starts=d.get("starts_at"),
            ends=d.get("ends_at"), comment=d.get("comment"),
            origin="admin", guest_token=None, owner_id=uid, actor_id=uid,
            draft=0,
            pay_split=1 if d.get("pay_split") else 0, place_url=d.get("place_url"),
            # Импортированный старый «неактивный» объект становится обычным
            # активным событием, но не публикуется без явного решения владельца.
            is_public=0 if d.get("is_draft") else d.get("is_public", 0),
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
    query = getattr(request, "query_params", {})
    return_to = "/operator/settings" \
        if query.get("return_to") == "operator" else "/admin/"
    return redir(return_to, f"Импортировано: {n_cats} категорий и {n_dates} событий")


# ----- Категории -----------------------------------------------------------

@router.get("/categories", response_class=HTMLResponse)
def categories_list(request: Request, conn=Depends(get_db)):
    query = _search_query(request.query_params.get("q", ""))
    cats = _categories_list_data(
        conn, int(request.state.user["id"]), query=query,
    )
    current_list_url = "/admin/categories"
    if query:
        current_list_url += f"?{urlencode({'q': query})}"
    return templates.TemplateResponse(
        request, "admin/categories.html",
        actx(
            request, conn, active="cats", cats=cats, q=query,
            current_list_url=current_list_url,
        ))


def _search_query(value: object, *, limit: int = 200) -> str:
    """Короткая поисковая строка без управляющих символов.

    Это не SQL-санитайзер: значения всё равно передаются только параметрами.
    Ограничение длины не даёт случайному URL превращать LIKE в дорогой запрос.
    """
    return " ".join(str(value or "").split())[:limit]


def _like_pattern(value: str) -> str:
    """Экранирует метасимволы LIKE, чтобы запрос искал буквальную строку."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _category_deadline_state(deadline: str | None, *, now: datetime) -> str:
    """Возвращает активность подборки строго по её обязательному дедлайну.

    Отсутствующая, timezone-aware или повреждённая дата не превращает подборку
    в бессрочную: это некорректное legacy-состояние, которое владелец должен
    явно исправить.
    """
    try:
        parsed = datetime.fromisoformat(str(deadline))
    except (TypeError, ValueError):
        return "missing"
    if parsed.tzinfo is not None:
        return "missing"
    return "active" if now < parsed else "expired"


def _categories_list_data(conn, owner_id: int, *, query: str = "") -> list[dict]:
    """Готовит карточки категорий двумя SELECT независимо от их количества."""
    where = "c.owner_id=?"
    params: list[object] = [owner_id]
    if query:
        where += (
            " AND (c.name LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(c.description, '') LIKE ? ESCAPE '\\' COLLATE NOCASE)"
        )
        pattern = _like_pattern(query)
        params.extend((pattern, pattern))
    rows = conn.execute(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM date_categories dc WHERE dc.category_id=c.id) AS dcount "
        f"FROM categories c WHERE {where} ORDER BY c.created_at DESC",
        params,
    ).fetchall()

    # Default и живая custom-картинка полностью определяют revision/has_og:
    # для них не нужны ни gallery SELECT, ни stat каждого фото события.
    custom_exists: dict[str, bool] = {}
    custom_images: dict[int, str | None] = {}
    auto_ids = []
    for row in rows:
        custom_image = None
        if not row["use_default_preview"] and row["og_image"]:
            filename = str(row["og_image"])
            if filename not in custom_exists:
                custom_exists[filename] = images.upload_image_exists(filename)
            if custom_exists[filename]:
                custom_image = filename
        custom_images[int(row["id"])] = custom_image
        if not row["use_default_preview"] and custom_image is None:
            auto_ids.append(int(row["id"]))
    batched_sources = public_routes.category_og_sources_batch(
        conn, auto_ids, include_focus=True, existing_only=True,
    )

    cats = []
    list_now = now_naive()
    for row in rows:
        category = dict(row)
        category_id = int(row["id"])
        sources = batched_sources.get(category_id, [])
        custom_image = custom_images[category_id]
        category["has_og"] = bool(
            row["use_default_preview"] or custom_image or sources
        )
        category["preview_revision"] = images.og_preview_revision(
            sources,
            appearance.normalize_skin(row["category_skin"]),
            custom_image=custom_image,
            custom_focus=row["og_focus"],
            use_default=bool(row["use_default_preview"]),
        )
        category["deadline_state"] = _category_deadline_state(
            row["voting_deadline"], now=list_now,
        )
        category["is_active"] = category["deadline_state"] == "active"
        cats.append(category)
    return cats


@router.get("/categories/new", response_class=HTMLResponse)
def category_new(request: Request, conn=Depends(get_db)):
    """Отдельный спокойный экран вместо перегруженной быстрой формы списка."""
    now = now_naive()
    # datetime-local не хранит секунды, а backend требует строго будущий срок.
    # Следующая минута исключает ложный доступный min, который уже прошёл.
    deadline_min = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    return templates.TemplateResponse(
        request, "admin/category_new.html",
        actx(
            request, conn, active="cats",
            deadline_min=deadline_min.strftime("%Y-%m-%dT%H:%M"),
            default_deadline=(now + timedelta(days=7)).replace(
                second=0, microsecond=0,
            ).strftime("%Y-%m-%dT%H:%M"),
        ),
    )


@router.post("/categories/create")
def category_create(request: Request, bg: BackgroundTasks, name: str = Form(...),
                    choice_mode: str = Form(""), voting_deadline: str = Form(""),
                    conn=Depends(get_db)):
    name = clean_text(name, 200, "Название", required=True)
    if not choice_mode or not voting_deadline:
        raise HTTPException(400, "Название, режим и дедлайн голосования обязательны")
    token = new_link_token()
    # Глобальная мягкая очередь и адресная премодерация независимы.
    # Вторая жёстко закрывает все публичные URL до одобрения.
    held = moderation.requires_operator_review(conn, request.state.user["id"])
    reviewed = 0 if (held or app_settings.is_on(
        conn, app_settings.MODERATE_CATEGORIES,
    )) else 1
    cursor = conn.execute(
        "INSERT INTO categories(owner_id, name, category_skin, link_token, link_enabled, "
        "moderate_proposals, is_reviewed, operator_review_pending, created_at) "
        "VALUES(?,?,?,?,1,0,?,?,?)",
        (request.state.user["id"], name, appearance.FRIENDS, token, reviewed,
         1 if held else 0, now_iso()))
    category_id = int(cursor.lastrowid)
    try:
        voting.configure_category(
            conn, category_id, request.state.user["id"],
            choice_mode, voting_deadline,
        )
    except voting.VotingError as exc:
        _raise_voting_http(exc)
    conn.commit()
    actor = request.state.user["display_name"] or request.state.user["tg_username"] or "—"
    notify_admin(bg, conn, request.state.user["id"], notify.card(
        "🆕 Создана категория",
        f"«{notify.esc(name)}»",
        f"Кто: {notify.esc(actor)}"))
    message = ("Подборка отправлена администратору на проверку"
               if held else "Категория создана — добавь название и описание")
    return redir(f"/admin/categories/{category_id}", message)


def _cat_or_404(conn, cid: int, user):
    """Категория, которой можно управлять. Владелец — только свою; админ
    (is_operator) — любую (пункт «админ правит чужое»)."""
    if user["is_operator"]:
        cat = conn.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
        if not cat:
            raise HTTPException(404, "Категория не найдена")
        return cat
    return get_owned_category(conn, cid, user["id"])


def _normalize_og_focus(value: str | None, *, required: bool = False) -> str | None:
    """Приводит точку фокуса OG-картинки к стабильному ``X% Y%``.

    Пустое значение допустимо в общей форме редактирования (старые клиенты могли
    не присылать поле), но отдельный endpoint перемещения фокуса требует его.
    """
    import re as _re

    raw = (value or "").strip()
    if not raw and not required:
        return None
    match = _re.fullmatch(r"(\d{1,3})%\s+(\d{1,3})%", raw)
    if not match or int(match.group(1)) > 100 or int(match.group(2)) > 100:
        raise HTTPException(400, "Некорректная точка фокуса")
    return f"{int(match.group(1))}% {int(match.group(2))}%"


@router.post("/categories/{cid}/clone")
def category_clone(cid: int, request: Request, conn=Depends(get_db)):
    """Копирует категорию, сохраняя ссылки на уже существующие события.

    У копии собственные настройки и секретная ссылка, но ``date_categories``
    указывает на те же события. Так фотографии и записи событий не дублируются,
    а правки события закономерно видны во всех категориях, где оно включено.
    Голоса и вопросы привязаны к категории и поэтому не копируются.
    """
    src = _cat_or_404(conn, cid, request.state.user)
    if not src["choice_mode"] or not src["voting_deadline"]:
        raise HTTPException(
            409,
            "Сначала задай исходной категории режим и обязательный дедлайн",
        )
    # Оператор может править чужую категорию. Копия остаётся у её владельца,
    # иначе общие события оказались бы привязаны к категории другого аккаунта.
    owner_id = int(src["owner_id"])
    suffix = " (копия)"
    name = src["name"][:200 - len(suffix)].rstrip() + suffix
    held = moderation.requires_operator_review(conn, request.state.user["id"])
    reviewed = 0 if (held or app_settings.is_on(
        conn, app_settings.MODERATE_CATEGORIES,
    )) else 1
    copied_files: list[str] = []
    try:
        # События теперь общие для исходной категории и копии. Поэтому нельзя
        # молча переносить старый дедлайн на неделю вперёд: будущая дата копии
        # делала уже завершённое общее событие временно недоступным для отзыва.
        # Сохраняем исходный срок; перед открытием новой ссылки редактор всё
        # равно потребует явно настроить актуальное голосование.
        copied_deadline = datetime.fromisoformat(
            src["voting_deadline"],
        ).replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(409, "У исходной категории некорректный дедлайн")

    try:
        # Собственная OG-картинка тоже должна быть независимым файлом. Если
        # исходник был удалён с диска, копия корректно откатится к авто-превью.
        og_image = images.copy_file(src["og_image"]) if src["og_image"] else None
        if og_image:
            copied_files.append(og_image)
        cursor = conn.execute(
            "INSERT INTO categories("
            "owner_id, name, category_skin, link_token, link_enabled, "
            "moderate_proposals, is_reviewed, operator_review_pending, "
            "description, og_title, og_desc, "
            "og_image, og_focus, use_default_preview, choice_mode, "
            "voting_deadline, voting_status, private_profiles, prevent_copying, "
            "pin_enabled, access_pin_hash, created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner_id, name, src["category_skin"], new_link_token(),
             0, src["moderate_proposals"], reviewed, 1 if held else 0,
             src["description"], src["og_title"], src["og_desc"], og_image,
             src["og_focus"] if og_image else None, src["use_default_preview"],
             src["choice_mode"],
             copied_deadline, voting.STATUS_UNCONFIGURED,
             src["private_profiles"], src["prevent_copying"],
             src["pin_enabled"], src["access_pin_hash"],
             now_iso()),
        )
        new_cid = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO date_categories(date_id, category_id, position) "
            "SELECT date_id, ?, position FROM date_categories WHERE category_id=?",
            (new_cid, cid),
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM date_categories WHERE category_id=?",
            (new_cid,),
        ).fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        for filename in copied_files:
            images.delete_file(filename)
        raise

    return redir(
        f"/admin/categories/{new_cid}",
        (("Подборка отправлена администратору на проверку; " if held else
          "Категория скопирована; ") + f"подключено {count} "
         f"{plural(count, 'существующее событие', 'существующих события', 'существующих событий')}. "
         "Проверь дедлайн и включи ссылку после правок"),
    )


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
    # «Прикрепить» — события того же владельца, что и категория (для админа,
    # правящего чужую категорию, это её владелец, а не сам админ).
    attachable = conn.execute(
        "SELECT id, name FROM dates WHERE owner_id=? AND archived_at IS NULL AND id NOT IN "
        "(SELECT date_id FROM date_categories WHERE category_id=?) ORDER BY created_at DESC",
        (cat["owner_id"], cid)).fetchall()
    # авто-превью ссылки: если своей картинки нет, показываем коллаж из фото
    # событий этой категории (тот же, что уйдёт в og:image). Здесь — только
    # флаг наличия; саму картинку отдаёт /admin/categories/{cid}/og-preview.
    custom_og_available = images.upload_image_exists(cat["og_image"])
    auto_files: list[images.OgSource] = []
    if not cat["use_default_preview"] and not custom_og_available:
        auto_files = _category_collage_sources(conn, cid)
    auto_og = bool(auto_files)
    preview_revision = images.og_preview_revision(
        auto_files,
        appearance.normalize_skin(cat["category_skin"]),
        custom_image=cat["og_image"] if custom_og_available else None,
        custom_focus=cat["og_focus"],
        use_default=bool(cat["use_default_preview"]),
    )
    state = voting.get_category_state(conn, cid)
    leader_ids = set(state.leader_date_ids)
    can_change_composition = not _category_voting_is_closed(cat)
    deadline_min = (now_naive() + timedelta(minutes=1)).replace(
        second=0, microsecond=0,
    ).strftime("%Y-%m-%dT%H:%M")
    is_operator = bool(request.state.user["is_operator"])
    editor_return_url = _safe_category_editor_return(
        request.query_params.get("return_to", ""), int(cat["owner_id"]),
        allow_operator=is_operator,
    )
    editor_self_url = _category_editor_url(
        cid, request.query_params.get("return_to", ""), int(cat["owner_id"]),
        allow_operator=is_operator,
    )
    operator_context = None
    if is_operator and int(cat["owner_id"]) != int(request.state.user["id"]):
        owner = conn.execute(
            "SELECT display_name, tg_username FROM users WHERE id=?",
            (cat["owner_id"],),
        ).fetchone()
        owner_name = (
            (owner["display_name"] or owner["tg_username"])
            if owner else None
        ) or f"#{cat['owner_id']}"
        operator_context = {"owner_name": owner_name}
    return templates.TemplateResponse(
        request, "admin/category_detail.html",
        actx(request, conn, active="cats", cat=cat, dates=dates, attachable=attachable,
             auto_og=auto_og, custom_og_available=custom_og_available,
             voting_state=state, leader_ids=leader_ids,
             can_change_composition=can_change_composition,
             deadline_min=deadline_min,
             preview_revision=preview_revision,
             editor_return_url=editor_return_url,
             editor_self_url=editor_self_url,
             operator_context=operator_context))


@router.post("/categories/{cid}/voting")
def category_voting_configure(cid: int, request: Request,
                              choice_mode: str = Form(...),
                              voting_deadline: str = Form(...),
                              conn=Depends(get_db)):
    """Явная настройка режима и ручного дедлайна голосования (время МСК)."""
    cat = _cat_or_404(conn, cid, request.state.user)
    # Снимок прошлого раунда и его сброс должны видеть одно состояние. Иначе
    # фоновое close_due могло закрыть категорию между SELECT и configure: новый
    # срок сохранялся бы, а только что созданные итоги оставались активными.
    conn.execute("UPDATE categories SET id=id WHERE id=?", (cid,))
    previous_state = voting.get_category_state(conn, cid)
    reopened = (
        previous_state.closed_at is not None
        or previous_state.status in voting.CLOSED_STATUSES
    )
    try:
        state = voting.configure_category(
            conn, cid, cat["owner_id"], choice_mode, voting_deadline,
        )
    except voting.VotingError as exc:
        _raise_voting_http(exc)
    if reopened:
        voting_events.cancel_pending_round_notifications(conn, cid)
    for row in conn.execute(
        "SELECT DISTINCT user_id FROM bookings WHERE category_id=? AND user_id IS NOT NULL",
        (cid,),
    ):
        voting_events.queue_deadline_reminder(conn, cid, int(row["user_id"]))
    for row in conn.execute(
        "SELECT date_id FROM date_categories WHERE category_id=?", (cid,),
    ).fetchall():
        social_events.reconcile_review_prompts_for_date(conn, int(row["date_id"]))
    conn.commit()
    message = (
        "Голосование возобновлено, новый дедлайн сохранён"
        if reopened else "Настройки голосования сохранены"
    )
    return _category_editor_redir(request, cid, int(cat["owner_id"]), message)


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
    if state.winner_date_id is not None:
        social_events.queue_review_prompts_for_date(conn, int(state.winner_date_id))
    conn.commit()
    return _category_editor_redir(
        request, cid, int(cat["owner_id"]), "Победитель выбран",
    )


def _category_collage_sources(conn, cid: int) -> list[images.OgSource]:
    """Общий с публичной страницей round-robin набор фото и точек фокуса."""
    return public_routes.category_og_sources(
        conn, cid, include_focus=True, existing_only=True,
    )


def _safe_operator_editor_return(raw: str, owner_id: int) -> str | None:
    """Возвращает только известный локальный URL операторской поверхности.

    Редакторы кабинета принимают ``return_to`` из query string. Оператору
    разрешены три конкретные точки возврата; схема, host, fragment и любые
    неизвестные параметры отбрасываются, чтобы редирект нельзя было превратить
    в open redirect.
    """
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    user_path = f"/operator/users/{owner_id}"
    if parsed.path == user_path:
        query = dict(parse_qsl(parsed.query, keep_blank_values=False))
        clean_user: list[tuple[str, str]] = []
        for key in ("events_page", "votes_page"):
            if query.get(key, "").isdigit() and int(query[key]) > 1:
                clean_user.append((key, str(int(query[key]))))
        return user_path + (f"?{urlencode(clean_user)}" if clean_user else "")
    if parsed.path == "/operator/review" and not parsed.query:
        return parsed.path
    query = dict(parse_qsl(parsed.query, keep_blank_values=False))
    clean: list[tuple[str, str]] = []
    if parsed.path == "/operator/categories":
        if query.get("link") in {"enabled", "disabled"}:
            clean.append(("link", query["link"]))
        if query.get("review") in {"pending", "operator", "soft", "approved"}:
            clean.append(("review", query["review"]))
        if query.get("q"):
            clean.append(("q", query["q"][:200]))
        if query.get("page", "").isdigit() and int(query["page"]) > 1:
            clean.append(("page", str(int(query["page"]))))
    elif parsed.path == "/operator/dates":
        if query.get("flt") in {
            "active", "review", "draft", "booked", "reported", "archived",
        }:
            clean.append(("flt", query["flt"]))
        if query.get("q"):
            clean.append(("q", query["q"][:200]))
        if query.get("page", "").isdigit() and int(query["page"]) > 1:
            clean.append(("page", str(int(query["page"]))))
    else:
        return None
    return parsed.path + (f"?{urlencode(clean)}" if clean else "")


def _safe_public_editor_return(raw: str, kind: str) -> str | None:
    """Разрешает контекстный возврат только на локальную секретную страницу."""
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    pattern = rf"/{re.escape(kind)}/[A-Za-z0-9_-]+"
    return parsed.path if re.fullmatch(pattern, parsed.path) else None


def _safe_category_editor_return(raw: str, owner_id: int,
                                 *, allow_operator: bool = False) -> str:
    """Безопасная точка возврата из редактора подборки."""
    public_target = _safe_public_editor_return(raw, "c")
    if public_target:
        return public_target
    if allow_operator:
        operator_target = _safe_operator_editor_return(raw, owner_id)
        if operator_target:
            return operator_target
    parsed = urlsplit(raw)
    if not parsed.scheme and not parsed.netloc and parsed.path == "/admin/categories":
        query = dict(parse_qsl(parsed.query, keep_blank_values=False))
        search = _search_query(query.get("q", ""))
        return parsed.path + (f"?{urlencode({'q': search})}" if search else "")
    return "/admin/categories"


def _category_editor_url(cid: int, return_to: str, owner_id: int,
                         *, allow_operator: bool = False,
                         fragment: str = "") -> str:
    target = _safe_category_editor_return(
        return_to, owner_id, allow_operator=allow_operator,
    )
    base = f"/admin/categories/{cid}"
    if target != "/admin/categories":
        base += f"?{urlencode({'return_to': target})}"
    if fragment:
        base += f"#{fragment}"
    return base


def _category_editor_redir(request: Request, cid: int, owner_id: int,
                           message: str, *, fragment: str = ""):
    query_params = getattr(request, "query_params", {})
    return redir(
        _category_editor_url(
            cid,
            query_params.get("return_to", ""),
            owner_id,
            allow_operator=bool(request.state.user["is_operator"]),
            fragment=fragment,
        ),
        message,
    )


def prewarm_date_collages(did: int) -> None:
    """Фоновая пере-сборка коллажей для всех категорий события. Зовём из
    BackgroundTasks после правки фото/категорий — чтобы список «Категории» и
    дашборд открывались по тёплому кэшу (иначе первый заход собирал N коллажей
    синхронно на единственном воркере — отсюда «долгая первая загрузка»)."""
    conn = db.connect()
    try:
        categories = conn.execute(
            "SELECT c.id, c.category_skin, c.og_image FROM categories c "
            "JOIN date_categories dc ON dc.category_id=c.id WHERE dc.date_id=? "
            "AND c.use_default_preview=0",
            (did,)).fetchall()
        for category in categories:
            if images.upload_image_exists(category["og_image"]):
                continue
            cid = category["id"]
            files = _category_collage_sources(conn, cid)
            if files:
                images.build_og_collage(
                    files, appearance.normalize_skin(category["category_skin"]))
    except Exception:
        log.exception(
            "Не удалось прогреть коллажи события",
            extra={"event": "date_collage_prewarm_failed", "outcome": "failure"},
        )
    finally:
        conn.close()


@router.get("/categories/{cid}/og-preview")
def category_og_preview(cid: int, request: Request, skin: str | None = None,
                        v: str | None = None,
                        conn=Depends(get_db)):
    """Коллаж-превью ссылки для редактора категории (когда своей картинки нет).
    Та же сборка, что и публичный og:image, но за owner-гейтом."""
    cat = _cat_or_404(conn, cid, request.state.user)
    preview_skin = appearance.normalize_skin(skin, default=cat["category_skin"])
    if cat["use_default_preview"]:
        return FileResponse(
            images.og_default_path(preview_skin),
            media_type="image/png",
            headers={"Cache-Control": "private, no-cache"},
        )
    if images.upload_image_exists(cat["og_image"]):
        # Редактор фокуса показывает защищённый raw upload, а этот endpoint —
        # итоговый link preview с тем же кропом и без накладываемых логотипов.
        cropped = images.build_og_crop(
            cat["og_image"], cat["og_focus"], preview_skin)
        if cropped:
            return FileResponse(cropped, media_type="image/webp",
                                headers={"Cache-Control": "private, no-cache"})
    collage = images.build_og_collage(
        _category_collage_sources(conn, cid), preview_skin)
    if not collage:
        return FileResponse(
            images.og_default_path(preview_skin),
            media_type="image/png",
            headers={"Cache-Control": "private, no-cache"},
        )
    return FileResponse(collage, media_type="image/webp",
                        headers={"Cache-Control": "private, no-cache"})


@router.post("/categories/{cid}/rename")
def category_rename(cid: int, request: Request, name: str = Form(...),
                    description: str = Form(""),
                    og_title: str = Form(""), og_desc: str = Form(""),
                    category_skin: str | None = Form(None),
                    og_image: UploadFile | None = File(None),
                    og_focus: Annotated[str | None, Form()] = None,
                    conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid, request.state.user)
    name = clean_text(name, 200, "Название", required=True)
    description = clean_text(description, 1000, "Описание")
    og_title = clean_text(og_title, 120, "Заголовок превью")
    og_desc = clean_text(og_desc, 200, "Описание превью")
    skin = appearance.normalize_skin(category_skin, default=cat["category_skin"])
    # Валидируем до записи файла: некорректный focus не должен оставлять orphan
    # в uploads. Поле приходит из того же редактора, поэтому новый файл и выбранный
    # пользователем кадр сохраняются одним submit.
    focus_value = _normalize_og_focus(og_focus)

    # картинка превью — опционально; новый файл сжимаем в WebP (как фото событий),
    # старый сносим только после успешной записи
    new_image = None
    if og_image is not None and (og_image.filename or "").strip():
        try:
            new_image = images.save_upload(og_image)
        except ValueError as e:
            raise HTTPException(400, str(e))

    old_image = cat["og_image"]
    if new_image:
        # Точка относится уже к новой картинке; при отсутствии поля остаётся
        # стандартный центр. Включённое fixed/default-превью отключаем, чтобы
        # пользователь сразу увидел только что выбранный custom-вариант.
        conn.execute(
            "UPDATE categories SET name=?, description=?, og_title=?, og_desc=?, "
            "category_skin=?, og_image=?, og_focus=?, use_default_preview=0 "
            "WHERE id=?",
            (name, description, og_title, og_desc, skin, new_image,
             focus_value, cid))
    elif og_focus is not None:
        # Существующую custom-картинку можно двигать и сохранить обычной кнопкой
        # даже если instant-save оказался недоступен. Для старых клиентов, которые
        # вообще не присылают og_focus, прежнее значение оставляем как есть.
        conn.execute(
            "UPDATE categories SET name=?, description=?, og_title=?, og_desc=?, "
            "category_skin=?, og_focus=? WHERE id=?",
            (name, description, og_title, og_desc, skin, focus_value, cid))
    else:
        conn.execute(
            "UPDATE categories SET name=?, description=?, og_title=?, og_desc=?, "
            "category_skin=? WHERE id=?",
            (name, description, og_title, og_desc, skin, cid))
    conn.commit()
    if new_image and old_image:          # старую картинку — только после коммита
        images.delete_file(old_image)
    return _category_editor_redir(
        request, cid, int(cat["owner_id"]), "Сохранено",
    )


@router.post("/categories/{cid}/privacy")
def category_privacy_save(
    cid: int,
    request: Request,
    private_profiles: str | None = Form(None),
    prevent_copying: str | None = Form(None),
    pin_enabled: str | None = Form(None),
    access_pin: str = Form(""),
    conn=Depends(get_db),
):
    """Сохраняет защиту гостевой подборки, не раскрывая текущий PIN.

    Пустое поле оставляет уже заданный хеш без изменений. При первом включении
    PIN обязателен; проверка формата и дорогое хеширование живут в общем модуле,
    которым пользуется и публичный экран разблокировки.
    """
    cat = _cat_or_404(conn, cid, request.state.user)
    enable_pin = pin_enabled == "1"
    raw_pin = access_pin
    pin_hash = cat["access_pin_hash"]
    if raw_pin:
        try:
            pin_hash = category_access.hash_pin(raw_pin)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    elif enable_pin and not pin_hash:
        raise HTTPException(400, "Введи PIN-код из 4 цифр")

    conn.execute(
        "UPDATE categories SET private_profiles=?, prevent_copying=?, "
        "pin_enabled=?, access_pin_hash=? WHERE id=?",
        (
            1 if private_profiles == "1" else 0,
            1 if prevent_copying == "1" else 0,
            1 if enable_pin else 0,
            pin_hash,
            cid,
        ),
    )
    conn.commit()
    return _category_editor_redir(
        request, cid, int(cat["owner_id"]), "Настройки приватности сохранены",
        fragment="categoryPrivacy",
    )


@router.post("/categories/{cid}/og_image/delete")
def category_og_image_delete(cid: int, request: Request, conn=Depends(get_db)):
    """Убрать свою картинку превью → вернуться к дефолту выбранного skin."""
    cat = _cat_or_404(conn, cid, request.state.user)
    old = cat["og_image"]
    conn.execute("UPDATE categories SET og_image=NULL WHERE id=?", (cid,))
    conn.commit()
    if old:
        images.delete_file(old)
    return _category_editor_redir(
        request, cid, int(cat["owner_id"]), "Картинка превью убрана",
    )


@router.post("/categories/{cid}/preview/reset")
def category_preview_reset(cid: int, request: Request, conn=Depends(get_db)):
    """Сбросить превью ссылки к стандартному виду: убрать свою картинку И текст
    (og_title/og_desc). Дальше превью — дефолтный текст + авто-коллаж из фото."""
    cat = _cat_or_404(conn, cid, request.state.user)
    old = cat["og_image"]
    conn.execute(
        "UPDATE categories SET og_image=NULL, og_title=NULL, og_desc=NULL, "
        "og_focus=NULL, use_default_preview=0 WHERE id=?",
        (cid,))
    conn.commit()
    if old:
        images.delete_file(old)
    return _category_editor_redir(
        request, cid, int(cat["owner_id"]),
        "Превью сброшено к стандартному",
    )


@router.post("/categories/{cid}/default_preview")
def category_default_preview(cid: int, request: Request, conn=Depends(get_db)):
    """Фиксирует фирменное превью либо возвращает динамический режим.

    Пользовательская картинка сохраняется на диске: после отключения режима она
    снова станет активной. Новые события не влияют на фиксированное превью.
    """
    cat = _cat_or_404(conn, cid, request.state.user)
    value = 0 if cat["use_default_preview"] else 1
    conn.execute(
        "UPDATE categories SET use_default_preview=? WHERE id=?", (value, cid),
    )
    conn.commit()
    return _category_editor_redir(
        request, cid, int(cat["owner_id"]),
        "Стандартное превью установлено" if value
        else "Стандартное превью отключено",
    )


@router.post("/categories/{cid}/og_focus")
def category_og_focus(cid: int, request: Request, focus: str = Form(...),
                      expected_image: Annotated[str, Form()] = "",
                      expected_focus: Annotated[str | None, Form()] = None,
                      conn=Depends(get_db)):
    """Точка фокуса своей картинки превью: «X% Y%» (0..100). Владелец двигает
    картинку в редакторе — og:image кропается по ней (WYSIWYG)."""
    cat = _cat_or_404(conn, cid, request.state.user)
    value = _normalize_og_focus(focus, required=True)
    if not images.upload_image_exists(cat["og_image"]):
        raise HTTPException(400, "У превью нет своей картинки")
    expected_value = _normalize_og_focus(expected_focus, required=True)
    if not expected_image or Path(expected_image).name != expected_image:
        raise HTTPException(409, "Превью уже изменилось — обнови страницу")
    # Compare-and-set не даёт старому instant-save перезаписать focus новой
    # картинки или более свежий обычный submit формы.
    updated = conn.execute(
        "UPDATE categories SET og_focus=? WHERE id=? AND og_image=? "
        "AND use_default_preview=0 "
        "AND COALESCE(og_focus, '50% 50%')=?",
        (value, cid, expected_image, expected_value),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise HTTPException(409, "Превью уже изменилось — обнови страницу")
    conn.commit()
    preview_revision = images.og_preview_revision(
        [], appearance.normalize_skin(cat["category_skin"]),
        custom_image=cat["og_image"], custom_focus=value,
    )
    return JSONResponse({
        "ok": True, "focus": value, "preview_revision": preview_revision,
    })


@router.post("/categories/{cid}/toggle")
def category_toggle(cid: int, request: Request, conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid, request.state.user)
    new_val = 0 if cat["link_enabled"] else 1
    conn.execute("UPDATE categories SET link_enabled=? WHERE id=?", (new_val, cid))
    conn.commit()
    return _category_editor_redir(
        request, cid, int(cat["owner_id"]),
        "Ссылка включена" if new_val else "Ссылка отключена",
    )


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
    return _category_editor_redir(
        request, cid, int(cat["owner_id"]),
        "Предложения гостей теперь попадают на модерацию в списке событий"
        if new_val else "Предложения гостей теперь публикуются сразу",
    )


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
    return _category_editor_redir(
        request, cid, int(cat["owner_id"]),
        "Новая ссылка сгенерирована. Старая больше не работает, все данные сохранены.",
    )


@router.post("/categories/{cid}/delete")
def category_delete(cid: int, request: Request, bg: BackgroundTasks, conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid, request.state.user)
    editor_return_url = _safe_category_editor_return(
        request.query_params.get("return_to", ""), int(cat["owner_id"]),
        allow_operator=bool(request.state.user["is_operator"]),
    )
    cat = _require_category_composition_mutable(conn, cat)
    affected = conn.execute(
        "SELECT DISTINCT d.id, d.name FROM dates d JOIN bookings b ON b.date_id=d.id "
        "WHERE b.category_id=?", (cid,),
    ).fetchall()
    affected_date_ids = [int(row["date_id"]) for row in conn.execute(
        "SELECT date_id FROM date_categories WHERE category_id=?", (cid,),
    ).fetchall()]
    voting_events.cancel_category_notifications(conn, cid)
    for d in affected:
        voting_events.queue_date_removed(
            conn, d["id"], d["name"], cid, cat["name"], None,
        )
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    for date_id in affected_date_ids:
        social_events.queue_review_prompts_for_date(conn, date_id)
    conn.commit()
    if cat["og_image"]:
        images.delete_file(cat["og_image"])
    actor = request.state.user["display_name"] or request.state.user["tg_username"] or "—"
    notify_admin(bg, conn, cat["owner_id"], notify.card(
        "🗑 Удалена категория",
        f"«{notify.esc(cat['name'])}»",
        f"Кто: {notify.esc(actor)}"))
    return redir(editor_return_url, "Подборка удалена (события остались)")


@router.post("/categories/{cid}/attach")
def category_attach(cid: int, request: Request, date_id: int = Form(...),
                    conn=Depends(get_db)):
    cat = _cat_or_404(conn, cid, request.state.user)
    cat = _require_category_composition_mutable(conn, cat)
    # привязать можно только событие ТОГО ЖЕ владельца, что и категория, —
    # иначе чужое утечёт в категорию (для админа контекст = владелец категории)
    date_row = get_owned_date(conn, date_id, cat["owner_id"])
    _validate_start_after_open_deadlines(conn, [cid], date_row["starts_at"])
    conn.execute(
        "INSERT OR IGNORE INTO date_categories(date_id, category_id, position) "
        "VALUES(?,?,?)", (date_id, cid, next_cat_pos(conn, cid)))
    # Привязка не является одобрением модерации: гостевой draft публикуется
    # только отдельным действием владельца.
    social_events.queue_review_prompts_for_date(conn, date_id)
    conn.commit()
    return _category_editor_redir(
        request, cid, int(cat["owner_id"]),
        "Событие добавлено в категорию",
        fragment="categoryDates",
    )


@router.post("/categories/{cid}/dates_reorder")
def category_dates_reorder(cid: int, request: Request, order: str = Form(...),
                           conn=Depends(get_db)):
    """Drag-and-drop порядок событий: order — id через запятую."""
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
    custom_image = cat["og_image"] \
        if images.upload_image_exists(cat["og_image"]) else None
    preview_revision = images.og_preview_revision(
        _category_collage_sources(conn, cid),
        appearance.normalize_skin(cat["category_skin"]),
        custom_image=custom_image, custom_focus=cat["og_focus"],
        use_default=bool(cat["use_default_preview"]),
    )
    return JSONResponse({"ok": True, "preview_revision": preview_revision})


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
    # Событие без категории остаётся активным в коллекции владельца. Категория
    # определяет участие в голосовании, а не жизненный статус события.
    social_events.queue_review_prompts_for_date(conn, date_id)
    conn.commit()
    return _category_editor_redir(
        request, cid, int(cat["owner_id"]),
        "Событие убрано из подборки", fragment="categoryDates",
    )


# ----- События -------------------------------------------------------------

VIEW_WHERE = {
    # Предложения — отдельный входящий поток. Они не смешиваются с личной
    # коллекцией владельца даже после архивации и всегда доступны в своей вкладке.
    "active": "d.archived_at IS NULL AND d.origin<>'guest'",
    "archived": "d.archived_at IS NOT NULL AND d.origin<>'guest'",
    "proposed": "d.origin='guest'",
}
FLT_WHERE = {
    "public": "d.is_public=1",
    "private": "d.is_public=0",
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
    # Старые сохранённые URL с f=guest ведём в новую самостоятельную вкладку.
    if qp.get("f") == "guest":
        view = "proposed"
    flt = qp.get("f") if qp.get("f") in FLT_WHERE else ""
    cat = qp.get("cat", "")
    query = _search_query(qp.get("q", ""))

    where = VIEW_WHERE[view]
    params: list = [request.state.user["id"]]
    where = "d.owner_id=? AND " + where
    if flt:
        where += " AND " + FLT_WHERE[flt]
    if cat.isdigit():
        where += " AND d.id IN (SELECT date_id FROM date_categories WHERE category_id=?)"
        params.append(int(cat))
    if query:
        pattern = _like_pattern(query)
        where += (
            " AND (d.name LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(d.place, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(d.comment, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR EXISTS (SELECT 1 FROM date_categories sdc "
            "JOIN categories sc ON sc.id=sdc.category_id "
            "WHERE sdc.date_id=d.id "
            "AND sc.name LIKE ? ESCAPE '\\' COLLATE NOCASE))"
        )
        params.extend((pattern, pattern, pattern, pattern))

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

    active_n = conn.execute(
        "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NULL "
        "AND origin<>'guest'",
        (request.state.user["id"],)).fetchone()[0]
    archived_n = conn.execute(
        "SELECT COUNT(*) FROM dates WHERE owner_id=? AND archived_at IS NOT NULL "
        "AND origin<>'guest'",
        (request.state.user["id"],)).fetchone()[0]
    proposed_n = conn.execute(
        "SELECT COUNT(*) FROM dates WHERE owner_id=? AND origin='guest'",
        (request.state.user["id"],)).fetchone()[0]

    keep = [("sort", sort)] if sort != "new" else []
    if flt:
        keep.append(("f", flt))
    if cat.isdigit():
        keep.append(("cat", cat))
    clear_search_url = f"/admin/dates?view={view}"
    if keep:
        clear_search_url += "&" + urlencode(keep)
    if query:
        keep.append(("q", query))
    qs_keep = ("&" + urlencode(keep)) if keep else ""
    current_list_url = f"/admin/dates?view={view}{qs_keep}"
    if page > 1:
        current_list_url += f"&page={page}"
    editor_return_query = urlencode({"return_to": current_list_url})

    # вид списка (карточки/таблица) — cookie, читается на сервере для SSR
    layout = request.cookies.get("layout")
    layout = layout if layout in ("cards", "list") else "cards"

    return templates.TemplateResponse(
        request, "admin/dates.html",
        actx(request, conn, active="dates", rows=rows, view=view, sort=sort,
             flt=flt, cat=cat, cats=_all_cats(conn, request.state.user["id"]),
             q=query, active_n=active_n, archived_n=archived_n,
             proposed_n=proposed_n,
             qs_keep=qs_keep, current_list_url=current_list_url,
             clear_search_url=clear_search_url,
             editor_return_query=editor_return_query,
             page=page, pages=pages, layout=layout))


def _all_cats(conn, uid: int):
    return conn.execute(
        "SELECT id, name, voting_status, voting_deadline, closed_at "
        "FROM categories WHERE owner_id=? ORDER BY created_at DESC",
        (uid,)).fetchall()


class DateQuotaExceeded(HTTPException):
    """Ожидаемый отказ, который endpoint превращает во flash с background."""


def enforce_date_quota(conn, user, bg: BackgroundTasks | None = None) -> None:
    """Отказ, если исчерпана квота лично созданных активных событий.

    Гостевые предложения принадлежат подборке владельца, но не расходуют его
    личную квоту. Архивные записи — история и тоже не считаются.
    """
    limit = user["date_limit"]
    used = public_routes.personal_date_quota_used(conn, int(user["id"]))
    if used >= limit:
        contact = (
            f"Чтобы увеличить лимит, напиши в поддержку {SUPPORT_CONTACT}."
            if SUPPORT_CONTACT
            else "Увеличить лимит можно через поддержку — контакт есть на странице «О проекте»."
        )
        if bg is not None:
            notify_admin(
                bg, conn, int(user["id"]),
                notify.card(
                    "⚠️ Достигнут лимит событий",
                    f"Использовано: {used} из {limit}",
                    f"Пользователь ID: {user['id']}",
                ),
            )
        raise DateQuotaExceeded(
            400,
            f"Достигнут лимит {limit} событий. {contact}",
        )


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
    форме со штатным value=1 = публичное; всё остальное = приватное."""
    return 1 if value == "1" else 0



@router.get("/dates/new", response_class=HTMLResponse)
def date_new_form(request: Request, conn=Depends(get_db)):
    checked = set()
    pre = request.query_params.get("category")
    if pre and pre.isdigit():
        checked.add(int(pre))
    cats = _all_cats(conn, request.state.user["id"])
    locked_cat_ids = {c["id"] for c in cats if _category_voting_is_closed(c)}
    checked.difference_update(locked_cat_ids)
    editor_return_url = _safe_date_editor_return(
        request.query_params.get("return_to", ""), int(request.state.user["id"]),
        allow_operator=bool(request.state.user["is_operator"]),
    )
    return templates.TemplateResponse(
        request, "admin/date_form.html",
        actx(request, conn, active="dates", date=None, photos=[], videos=[], links_text="",
             cats=cats, checked=checked, locked_cat_ids=locked_cat_ids,
             editor_return_url=editor_return_url,
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
    try:
        enforce_date_quota(conn, request.state.user, bg)  # общая квота аккаунта
    except DateQuotaExceeded as exc:
        # У HTTPException нет background hook. Возвращаем тот же понятный текст
        # flash-сообщением и прикрепляем очередь к ответу, чтобы уведомление
        # платформенному администратору действительно отправилось.
        response = redir("/admin/dates", str(exc.detail))
        response.background = bg
        return response
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
    # Категории управляют голосованием, а не активностью события. Публичность
    # отдельна и явно задаётся владельцем.
    date_id = insert_date(conn, name=name, place=place, starts=starts, ends=ends,
                          comment=comment, origin="admin", guest_token=None,
                          owner_id=uid, actor_id=uid,
                          draft=0,
                          pay_split=pay_value, place_url=place_url,
                          is_public=public_value, capacity=capacity_value)
    held = bool(conn.execute(
        "SELECT operator_review_pending FROM dates WHERE id=?", (date_id,),
    ).fetchone()[0])
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
        "🆕 Создано событие",
        f"«{notify.esc(name)}»",
        f"Кто: {notify.esc(actor)}"))
    return redir(
        _safe_date_editor_return(
            request.query_params.get("return_to", ""), int(uid),
            allow_operator=bool(request.state.user["is_operator"]),
        ),
        ("Событие отправлено администратору на проверку" if held else
         "Событие создано"),
    )


def add_videos(conn, date_id: int, files, existing: int) -> list[str]:
    """Сохраняет видео атомарно, вписывает в БД, возвращает имена файлов."""
    files = [f for f in files if f and f.filename]
    if not files:
        return []
    if existing + len(files) > images.MAX_VIDEOS:
        raise HTTPException(400, f"Максимум {images.MAX_VIDEOS} видео у одного события")
    try:
        saved = images.save_videos_batch(files)
    except ValueError as e:
        raise HTTPException(400, str(e))
    for fn in saved:
        conn.execute("INSERT INTO date_videos(date_id, filename) VALUES(?,?)",
                     (date_id, fn))
    return saved


def _date_or_404(conn, did: int, user):
    """Событие, которым можно управлять. Владелец — только своё; админ
    (is_operator) — любое (пункт «админ правит чужое»)."""
    if user["is_operator"]:
        d = conn.execute("SELECT * FROM dates WHERE id=?", (did,)).fetchone()
        if not d:
            raise HTTPException(404, "Событие не найдено")
        return d
    return get_owned_date(conn, did, user["id"])


def _require_date_not_in_closed_vote(conn, did: int, action: str) -> None:
    # Lock the date first: every competing category/vote mutation is a writer,
    # so the category list and deadline checks below remain a stable snapshot.
    if conn.execute("UPDATE dates SET id=id WHERE id=?", (did,)).rowcount == 0:
        raise HTTPException(404, "Событие не найдено")
    for cat in conn.execute(
        "SELECT c.* FROM categories c JOIN date_categories dc ON dc.category_id=c.id "
        "WHERE dc.date_id=?", (did,),
    ):
        if _category_voting_is_closed(cat):
            raise HTTPException(
                409,
                f"Нельзя {action}: голосование в категории «{cat['name']}» уже завершено",
            )


def _safe_date_editor_return(raw: str, owner_id: int,
                             *, allow_operator: bool = False) -> str:
    """Безопасный возврат из редактора в список или профиль.

    Значение приходит из query string, поэтому не переносим его в ``Location``
    как есть: сохраняем только известные параметры профильной страницы.
    """
    fallback = "/admin/dates"
    if not raw:
        return fallback
    public_target = _safe_public_editor_return(raw, "d")
    if public_target:
        return public_target
    if allow_operator:
        operator_target = _safe_operator_editor_return(raw, owner_id)
        if operator_target:
            return operator_target
    parsed = urlsplit(raw)
    allowed_paths = {"/admin/dates", "/admin/profile", f"/u/{owner_id}"}
    category_parts = parsed.path.strip("/").split("/")
    is_category_return = (
        len(category_parts) == 3
        and category_parts[:2] == ["admin", "categories"]
        and category_parts[2].isdigit()
    )
    if (parsed.scheme or parsed.netloc
            or (parsed.path not in allowed_paths and not is_category_return)):
        return fallback
    query = dict(parse_qsl(parsed.query, keep_blank_values=False))
    if is_category_return:
        target = parsed.path
        if allow_operator:
            nested = _safe_operator_editor_return(
                query.get("return_to", ""), owner_id,
            )
            if nested:
                target += f"?{urlencode({'return_to': nested})}"
        return target + ("#categoryDates" if parsed.fragment == "categoryDates" else "")
    if parsed.path == "/admin/dates":
        clean_dates = []
        if query.get("view") in VIEW_WHERE:
            clean_dates.append(("view", query["view"]))
        if query.get("sort") in SORT_ORDER:
            clean_dates.append(("sort", query["sort"]))
        if query.get("f") in FLT_WHERE:
            clean_dates.append(("f", query["f"]))
        if query.get("cat", "").isdigit():
            clean_dates.append(("cat", str(int(query["cat"]))))
        if query.get("q"):
            clean_dates.append(("q", _search_query(query["q"])))
        if query.get("page", "").isdigit() and int(query["page"]) > 1:
            clean_dates.append(("page", str(int(query["page"]))))
        return parsed.path + (f"?{urlencode(clean_dates)}" if clean_dates else "")
    if query.get("tab", "events") != "events":
        return fallback
    clean = [("tab", "events")]
    page = query.get("page", "")
    if page.isdigit() and int(page) > 1:
        clean.append(("page", str(int(page))))
    skin = query.get("skin", "")
    if parsed.path.startswith("/u/") and skin in appearance.VALID_SKINS:
        clean.append(("skin", skin))
    return f"{parsed.path}?{urlencode(clean)}#profileCollection"


def _date_editor_url(did: int, return_to: str, owner_id: int,
                     *, allow_operator: bool = False) -> str:
    target = _safe_date_editor_return(
        return_to, owner_id, allow_operator=allow_operator,
    )
    base = f"/admin/dates/{did}/edit"
    if target == "/admin/dates":
        return base
    return f"{base}?{urlencode({'return_to': target})}"


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
    editor_return_url = _safe_date_editor_return(
        request.query_params.get("return_to", ""), int(d["owner_id"]),
        allow_operator=bool(request.state.user["is_operator"]),
    )
    operator_context = None
    if (request.state.user["is_operator"]
            and int(d["owner_id"]) != int(request.state.user["id"])):
        owner = conn.execute(
            "SELECT display_name, tg_username FROM users WHERE id=?",
            (d["owner_id"],),
        ).fetchone()
        owner_name = (
            (owner["display_name"] or owner["tg_username"])
            if owner else None
        ) or f"#{d['owner_id']}"
        operator_context = {"owner_name": owner_name}
    return templates.TemplateResponse(
        request, "admin/date_form.html",
        actx(request, conn, active="dates", date=d, photos=photos, videos=videos,
             booked=booked,
             proposer=proposer,
             links_text="\n".join(r["url"] for r in link_rows),
             # категории показываем владельца события (админ может править чужое)
             cats=cats, checked=checked, locked_cat_ids=locked_cat_ids,
             editor_return_url=editor_return_url,
             operator_context=operator_context,
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
    # транзакции, что и запись. После блокировки перечитываем событие, чтобы
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

    # привязываем только категории владельца события (чужие id из формы молча
    # игнорируем). Для админа, правящего чужое, контекст = владелец события.
    own_cats = {r[0] for r in conn.execute(
        "SELECT id FROM categories WHERE owner_id=?", (d["owner_id"],))}
    categories = [c for c in categories if c in own_cats]
    requested = set(categories)
    current = {r[0] for r in conn.execute(
        "SELECT category_id FROM date_categories WHERE date_id=?", (did,))}

    # Закрывшаяся категория — зафиксированный снимок: её нельзя ни добавить к
    # событию, ни снять. Остальные категории синхронизируем точечно, не через
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
    # Только гостевое предложение, уже ожидающее модерации, сохраняет draft до
    # отдельного «Опубликовать». Обычное событие активно и без категории.
    is_draft = int(d["is_draft"]) if d["origin"] == "guest" else 0

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
    social_events.queue_review_prompts_for_date(conn, did)
    conn.commit()
    if needs_resolve:
        bg.add_task(places.resolve_into_db, did, place_url)
    bg.add_task(prewarm_date_collages, did)   # тёплый кэш коллажей для списка «Категории»
    return redir(
        _date_editor_url(
            did, request.query_params.get("return_to", ""), int(d["owner_id"]),
            allow_operator=bool(request.state.user["is_operator"]),
        ),
        "Сохранено",
    )


@router.post("/dates/{did}/publish")
def date_publish(did: int, request: Request, bg: BackgroundTasks,
                 next: str = Form("/admin/dates"), conn=Depends(get_db)):
    d = _date_or_404(conn, did, request.state.user)
    _require_date_not_in_closed_vote(conn, did, "изменить состав событий")
    d = _date_or_404(conn, did, request.state.user)
    # Инвариант: активно ⇔ есть хотя бы одна категория (иначе гости не видят).
    # Публиковать имеет смысл только событие с категорией — иначе оно снова
    # «активно» в списке, но невидимо гостям (частая путаница «не публикуется»).
    # Без категории уводим в редактор — там владелец её привяжет и сохранит.
    has_cat = conn.execute(
        "SELECT 1 FROM date_categories WHERE date_id=? LIMIT 1", (did,)).fetchone()
    if not has_cat:
        return redir(f"/admin/dates/{did}/edit",
                     "⚠ Добавь событие хотя бы в одну категорию — иначе гости его не увидят")
    category_ids = [int(r["category_id"]) for r in conn.execute(
        "SELECT category_id FROM date_categories WHERE date_id=?", (did,)
    )]
    _validate_start_after_open_deadlines(conn, category_ids, d["starts_at"])
    conn.execute("UPDATE dates SET is_draft=0 WHERE id=?", (did,))
    conn.commit()
    # Локальное одобрение владельца не обходит платформенную премодерацию.
    # Автора уведомляем о публикации только когда событие реально стало доступно.
    held = bool(d["operator_review_pending"])
    if not held and d["origin"] == "guest" and d["proposed_by"]:
        cat = conn.execute(
            "SELECT c.name, c.link_token FROM categories c "
            "JOIN date_categories dc ON dc.category_id=c.id "
            "WHERE dc.date_id=? LIMIT 1", (did,)).fetchone()
        notify_user(bg, conn, d["proposed_by"], notify.card(
            "✅ Твоё предложение опубликовано",
            f"«{notify.esc(d['name'])}»" + (f" · {notify.esc(cat['name'])}" if cat else ""),
            "Теперь его видят гости ♥"), preference="updates",
            action_url=f"{BASE_URL}/c/{cat['link_token']}" if cat else None,
            action_label="Открыть событие" if cat else None)
    return redir(
        next,
        ("Одобрено владельцем — событие ещё ждёт проверки администратора"
         if held else "Опубликовано — гости теперь видят это событие"),
    )


def _bulk_set_archived(conn, d, *, archived: bool) -> bool:
    """Меняет архивный статус с теми же побочными эффектами, что одиночная кнопка."""
    did = int(d["id"])
    if bool(d["archived_at"]) == archived:
        return False
    _require_date_not_in_closed_vote(
        conn, did,
        "перенести событие в архив" if archived else "вернуть событие из архива",
    )
    if not archived:
        category_ids = [int(r["category_id"]) for r in conn.execute(
            "SELECT category_id FROM date_categories WHERE date_id=?", (did,),
        )]
        _validate_start_after_open_deadlines(conn, category_ids, d["starts_at"])
        conn.execute("UPDATE dates SET archived_at=NULL WHERE id=?", (did,))
        return True

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
        conn.execute(
            "DELETE FROM bookings WHERE date_id=? AND category_id=?", (did, cat["id"]),
        )
        for user_id in affected_users:
            if not conn.execute(
                "SELECT 1 FROM bookings WHERE category_id=? AND user_id=? LIMIT 1",
                (cat["id"], user_id),
            ).fetchone():
                voting_events.cancel_deadline_reminder(conn, cat["id"], user_id)
    conn.execute("UPDATE dates SET archived_at=? WHERE id=?", (now_iso(), did))
    return True


def _bulk_delete_date(conn, d) -> list[str]:
    """Удаляет событие внутри общей транзакции; файлы возвращает для post-commit."""
    did = int(d["id"])
    _require_date_not_in_closed_vote(conn, did, "удалить событие")
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
        "SELECT filename FROM date_images WHERE date_id=? UNION ALL "
        "SELECT filename FROM date_videos WHERE date_id=?", (did, did),
    ).fetchall()]
    social_events.cancel_review_prompts_for_date(conn, did)
    conn.execute("DELETE FROM dates WHERE id=?", (did,))
    for category_id, user_ids in affected_by_cat.items():
        for user_id in user_ids:
            if not conn.execute(
                "SELECT 1 FROM bookings WHERE category_id=? AND user_id=? LIMIT 1",
                (category_id, user_id),
            ).fetchone():
                voting_events.cancel_deadline_reminder(conn, category_id, user_id)
    return files


@router.post("/dates/bulk")
def dates_bulk(request: Request, bg: BackgroundTasks,
               action: str = Form(...), date_ids: list[int] = Form(default=[]),
               next: str = Form("/admin/dates"), conn=Depends(get_db)):
    """Массовые действия desktop-списка с полной проверкой владения и lifecycle."""
    allowed = {"archive", "restore", "make_public", "make_private", "delete"}
    if action not in allowed:
        raise HTTPException(400, "Неизвестное массовое действие")
    ids = list(dict.fromkeys(int(did) for did in date_ids if int(did) > 0))
    if not ids:
        raise HTTPException(400, "Выбери хотя бы одно событие")
    if len(ids) > 100:
        raise HTTPException(400, "За один раз можно изменить не больше 100 событий")
    uid = int(request.state.user["id"])
    user_throttle("datebulk", uid, request)
    placeholders = ",".join("?" for _ in ids)
    owned = conn.execute(
        f"SELECT * FROM dates WHERE owner_id=? AND id IN ({placeholders})",
        (uid, *ids),
    ).fetchall()
    if len(owned) != len(ids):
        raise HTTPException(404, "Одно из событий не найдено")
    by_id = {int(row["id"]): row for row in owned}
    ordered = [by_id[did] for did in ids]
    changed = 0
    files: list[str] = []
    deleted_rows = []
    if action in {"archive", "restore"}:
        target = action == "archive"
        for row in ordered:
            changed += int(_bulk_set_archived(conn, row, archived=target))
    elif action in {"make_public", "make_private"}:
        value = 1 if action == "make_public" else 0
        for row in ordered:
            if int(row["is_public"]) == value:
                continue
            conn.execute("UPDATE dates SET is_public=? WHERE id=?", (value, row["id"]))
            changed += 1
    else:
        for row in ordered:
            files.extend(_bulk_delete_date(conn, row))
            deleted_rows.append(row)
            changed += 1
    conn.commit()
    for filename in files:
        images.delete_file(filename)
    actor = request.state.user["display_name"] or request.state.user["tg_username"] or "—"
    for row in deleted_rows:
        notify_admin(bg, conn, row["owner_id"], notify.card(
            "🗑 Удалено событие",
            f"«{notify.esc(row['name'])}»",
            f"Кто: {notify.esc(actor)}",
        ))
    labels = {
        "archive": "Перенесено в архив",
        "restore": "Возвращено из архива",
        "make_public": "Публичность включена",
        "make_private": "Публичность отключена",
        "delete": "Удалено",
    }
    target = _safe_date_editor_return(
        next, uid, allow_operator=bool(request.state.user["is_operator"]),
    )
    return redir(
        target,
        f"{labels[action]}: {changed} "
        f"{plural(changed, 'событие', 'события', 'событий')}",
    )


@router.post("/dates/{did}/archive")
def date_archive(did: int, request: Request, next: str = Form("/admin/dates"),
                  conn=Depends(get_db)):
    d = _date_or_404(conn, did, request.state.user)
    _require_date_not_in_closed_vote(conn, did, "перенести событие в архив")
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


@router.post("/dates/{did}/visibility")
def date_visibility(did: int, request: Request,
                    next: str = Form("/admin/dates"), conn=Depends(get_db)):
    """Явно переключает попадание события в публичную коллекцию и ленту.

    Для предложения на модерации флаг можно подготовить заранее, но is_draft
    всё равно не даст показать его гостям до отдельного одобрения.
    """
    d = _date_or_404(conn, did, request.state.user)
    value = 0 if int(d["is_public"]) else 1
    conn.execute("UPDATE dates SET is_public=? WHERE id=?", (value, did))
    conn.commit()
    return redir(next, "Событие теперь публичное" if value else
                 "Событие теперь непубличное")


@router.post("/dates/{did}/delete")
def date_delete(did: int, request: Request, bg: BackgroundTasks,
                next: str = Form("/admin/dates"), conn=Depends(get_db)):
    d = _date_or_404(conn, did, request.state.user)
    _require_date_not_in_closed_vote(conn, did, "удалить событие")
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
    social_events.cancel_review_prompts_for_date(conn, did)
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
        "🗑 Удалено событие",
        f"«{notify.esc(d['name'])}»",
        f"Кто: {notify.esc(actor)}"))
    return redir(next, "Событие удалено")


@router.post("/dates/{did}/clone")
def date_clone(did: int, request: Request, bg: BackgroundTasks,
               next: str = Form("/admin/dates"),
               conn=Depends(get_db)):
    """Дубль события: копируем запись, ссылки и файлы (с новыми именами на
    диске). Категории, голоса и вопросы НЕ переносим. Клон сразу активен в
    коллекции, но остаётся непубличным до явного решения владельца. Карта
    (place_url) уже распознана."""
    src = _date_or_404(conn, did, request.state.user)
    try:
        enforce_date_quota(conn, request.state.user, bg)
    except DateQuotaExceeded as exc:
        response = redir("/admin/dates", str(exc.detail))
        response.background = bg
        return response
    suffix = " (копия)"
    clone_name = src["name"][:200 - len(suffix)].rstrip() + suffix
    copied_files: list[str] = []
    try:
        new_id = insert_date(
            conn, name=clone_name, place=src["place"],
            starts=src["starts_at"], ends=src["ends_at"], comment=src["comment"],
            origin="admin", guest_token=None, draft=0,
            owner_id=request.state.user["id"], actor_id=request.state.user["id"],
            pay_split=src["pay_split"],
            place_url=src["place_url"], is_public=0,
            capacity=src["capacity"])
        held = bool(conn.execute(
            "SELECT operator_review_pending FROM dates WHERE id=?", (new_id,),
        ).fetchone()[0])

        # При ошибке внутри helper он чистит частичную копию сам. Полный список
        # сохраняем до commit, чтобы убрать файлы и при ошибке фиксации БД.
        copy_date_media_and_links(conn, did, new_id)
        copied_files = [row["filename"] for row in conn.execute(
            "SELECT filename FROM date_images WHERE date_id=? UNION ALL "
            "SELECT filename FROM date_videos WHERE date_id=?",
            (new_id, new_id),
        ).fetchall()]
        conn.commit()
    except Exception:
        conn.rollback()
        for filename in copied_files:
            images.delete_file(filename)
        raise
    return redir(
        _date_editor_url(
            new_id, next, int(request.state.user["id"]),
            allow_operator=bool(request.state.user["is_operator"]),
        ),
        ("Копия отправлена администратору на проверку" if held else
         "Событие скопировано и оставлено непубличным"),
    )


@router.post("/bookings/{bid}/delete")
def booking_delete(bid: int, request: Request, bg: BackgroundTasks,
                   next: str = Form("/admin/dates"), conn=Depends(get_db)):
    """Снять чужой выбор со события (например, по просьбе гостя)."""
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
    return redir(next, f"Голос снят — освободилось одно место ({row['nm']})")


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
    requested = request.query_params.get("f")
    f = "reviews" if requested == "reviews" else "unread"
    rows = []
    if f != "reviews":
        rows = conn.execute(
            f"SELECT q.*, d.name AS date_name, d.id AS did, c.name AS cat_name, "
            f"{GNAME_SQL.format(t='q.guest_token')} AS gname "
            f"FROM questions q JOIN dates d ON d.id=q.date_id "
            f"LEFT JOIN categories c ON c.id=q.category_id "
            f"LEFT JOIN guests g ON g.token=q.guest_token "
            f"WHERE q.date_id IN (SELECT id FROM dates WHERE owner_id=?) "
            f"AND q.answer IS NULL ORDER BY q.created_at DESC, q.id DESC",
            (request.state.user["id"],)
        ).fetchall()
    review_rows = social_events.review_waiting_rows(
        conn, int(request.state.user["id"]),
    ) if f == "reviews" else []
    return templates.TemplateResponse(
        request, "admin/questions.html",
        actx(request, conn, active="q", rows=rows, review_rows=review_rows, f=f,
             notif_settings=notify.get_preferences(conn, request.state.user["id"]),
             review_waiting=len(review_rows) if f == "reviews" else None))


@router.post("/questions/reviews/{date_id}/dismiss")
def review_waiting_dismiss(date_id: int, request: Request,
                           conn=Depends(get_db)):
    """Удаляет только напоминание, не событие и не право написать отзыв."""
    if not social_events.clear_review_waiting(
            conn, date_id, int(request.state.user["id"])):
        raise HTTPException(404, "Напоминание не найдено")
    conn.commit()
    return redir("/admin/questions?f=reviews", "Упоминание удалено")


@router.post("/questions/settings")
def question_notification_settings(
    request: Request,
    votes: str | None = Form(None),
    questions: str | None = Form(None),
    proposals: str | None = Form(None),
    updates: str | None = Form(None),
    reminders: str | None = Form(None),
    reviews: str | None = Form(None),
    conn=Depends(get_db),
):
    values = {
        "votes": votes == "1",
        "questions": questions == "1",
        "proposals": proposals == "1",
        "updates": updates == "1",
        "reminders": reminders == "1",
        "reviews": reviews == "1",
    }
    notify.save_preferences(conn, request.state.user["id"], values)
    conn.commit()
    return redir("/admin/questions", "Настройки уведомлений сохранены")


def _owned_question(conn, qid: int, uid: int):
    """Вопрос вместе с проверкой, что его событие принадлежит владельцу.
    Подтягиваем имена события/категории для уведомления автору."""
    return conn.execute(
        "SELECT q.*, d.name AS date_name, c.name AS cat_name, c.link_token "
        "FROM questions q JOIN dates d ON d.id=q.date_id "
        "LEFT JOIN categories c ON c.id=q.category_id "
        "WHERE q.id=? AND d.owner_id=?", (qid, uid)).fetchone()


def _notify_answer(bg, conn, q, answer: str) -> None:
    """Уведомляет автора вопроса (если залогинен и бот подключён) об ответе."""
    action_url = f"{BASE_URL}/c/{q['link_token']}" if q["link_token"] else None
    notify_user(bg, conn, q["user_id"], notify.card(
        "💬 Ответ на твой вопрос",
        f"«{notify.esc(q['date_name'])}»"
        + (f" · {notify.esc(q['cat_name'])}" if q["cat_name"] else ""),
        f"Вопрос: {notify.esc(q['text'])}",
        f"\nОтвет: {notify.esc(answer)}"), preference="updates",
        action_url=action_url,
        action_label="Открыть событие" if action_url else None)


@router.post("/questions/{qid}/accept_time")
def question_accept_time(qid: int, request: Request, bg: BackgroundTasks,
                         next: str = Form("/admin/questions"),
                         conn=Depends(get_db)):
    """Принять предложенное гостем время: применяем его к событию."""
    q = _owned_question(conn, qid, request.state.user["id"])
    if not q or not q["suggest_starts"]:
        raise HTTPException(404, "Это не предложение времени")
    # Сериализуем с настройкой категории и проверяем время по тому же правилу,
    # что редактор события. Так DB-защита превращается в понятную ошибку формы,
    # а не в необработанный IntegrityError/500.
    if conn.execute("UPDATE dates SET id=id WHERE id=?", (q["date_id"],)).rowcount == 0:
        raise HTTPException(404, "Событие не найдено")
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
    social_events.queue_review_prompts_for_date(conn, int(q["date_id"]))
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
# Публичная страница профиля /u/<id> доступна без регистрации. Изменения
# отзывов остаются защищены current_user и CSRF на конкретных POST-роутах.
# Решение владельца: показываем имя, фото, полную дату рождения и пол.
# (Privacy-пометка про полную ДР — в Политике конфиденциальности.)
# ---------------------------------------------------------------------------
user_router = APIRouter(prefix="/u")
PUBLIC_PROFILE_PAGE = 12


def _profile_viewer(request: Request, conn):
    """Необязательный viewer публичного профиля с готовым CSRF для действий."""
    me = public_routes.viewer(request, conn)
    if me is not None and "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(16)
    return me


def _profile_current_wants(conn, user_id: int, *, is_me: bool,
                           now, limit: int | None = None,
                           offset: int = 0):
    """Считает либо возвращает одну страницу актуальных wants целиком в SQL.

    ``COALESCE`` повторяет доменный приоритет: последний валидный дедлайн
    категории, затем ends_at, затем starts_at + 3 часа. Некорректные legacy
    timestamps дают NULL и переходят к следующему fallback, как ``_moment``.
    """
    visibility = ("" if is_me else
                  " AND w.is_public=1 AND d.is_public=1 AND d.is_draft=0")
    current = now.replace(tzinfo=None, microsecond=0).isoformat(sep="T")
    cte = (
        "WITH wants_with_expiry AS ("
        " SELECT d.id, d.owner_id, d.name, d.share_token, d.starts_at, "
        " d.ends_at, d.place, w.is_public AS want_public, "
        " w.updated_at AS marked_at, COALESCE("
        "  (SELECT MAX(strftime('%Y-%m-%dT%H:%M:%S', c.voting_deadline)) "
        "   FROM date_categories dc JOIN categories c ON c.id=dc.category_id "
        "   WHERE dc.date_id=d.id AND c.operator_review_pending=0), "
        "  strftime('%Y-%m-%dT%H:%M:%S', d.ends_at), "
        "  strftime('%Y-%m-%dT%H:%M:%S', d.starts_at, '+3 hours')"
        " ) AS expires_at "
        " FROM date_wants w JOIN dates d ON d.id=w.date_id "
        " WHERE w.user_id=? AND d.archived_at IS NULL "
        " AND d.operator_review_pending=0 "
        " AND NOT EXISTS (SELECT 1 FROM date_reviews r "
        "                 WHERE r.user_id=w.user_id AND r.date_id=w.date_id)"
        + visibility + ") "
    )
    current_filter = "WHERE expires_at IS NULL OR expires_at>?"
    if limit is None:
        return int(conn.execute(
            cte + "SELECT COUNT(*) FROM wants_with_expiry " + current_filter,
            (user_id, current),
        ).fetchone()[0])
    return conn.execute(
        cte + "SELECT id,owner_id,name,share_token,starts_at,ends_at,place,"
        "want_public,marked_at FROM wants_with_expiry " + current_filter +
        " ORDER BY marked_at DESC LIMIT ? OFFSET ?",
        (user_id, current, limit, offset),
    ).fetchall()


def _profile_sections_context(conn, user_id: int, viewer_id: int | None,
                              query_params) -> dict:
    """Данные трёх социальных вкладок профиля.

    Один загрузчик используется и в настройках собственного профиля, и на
    гостевой странице ``/u/<id>``. Так вкладки не расходятся по приватности и
    пагинации, хотя оболочка и подпись первой вкладки у них разные.
    """
    is_me = viewer_id is not None and int(user_id) == int(viewer_id)
    tab = query_params.get("tab", "events")
    if tab not in {"events", "want", "reviews"}:
        tab = "events"

    # Каждая вкладка пагинируется независимо. Секретные/черновые события не
    # раскрываются через чужую отметку или отзыв, даже если /d-ссылка известна
    # самому автору публикации.
    event_total = conn.execute(
        "SELECT COUNT(*) FROM dates "
        "WHERE owner_id=? AND is_public=1 AND is_draft=0 "
        "AND operator_review_pending=0 AND archived_at IS NULL",
        (user_id,)).fetchone()[0]
    # После явного дедлайна план исчезает из профиля; уже опубликованный отзыв
    # остаётся только в Reviews. Строку wants сохраняем как право на сам отзыв.
    want_now = now_naive()
    want_total = _profile_current_wants(
        conn, user_id, is_me=is_me, now=want_now,
    )
    review_visibility = " AND d.operator_review_pending=0"
    if not is_me:
        review_visibility += " AND r.is_public=1 AND d.is_public=1 AND d.is_draft=0"
    review_total = conn.execute(
        "SELECT COUNT(*) FROM date_reviews r JOIN dates d ON d.id=r.date_id "
        "WHERE r.user_id=?" + review_visibility,
        (user_id,),
    ).fetchone()[0]
    total = {"events": event_total, "want": want_total,
             "reviews": review_total}[tab]
    pages = max(1, -(-total // PUBLIC_PROFILE_PAGE))
    try:
        page = max(1, min(int(query_params.get("page", "1")), pages))
    except ValueError:
        page = 1
    offset = (page - 1) * PUBLIC_PROFILE_PAGE
    dates = []
    want_dates = []
    reviews = []
    if tab == "events":
        date_rows = conn.execute(
            "SELECT id, owner_id, name, share_token, starts_at, ends_at, place FROM dates "
            "WHERE owner_id=? AND is_public=1 AND is_draft=0 "
            "AND operator_review_pending=0 AND archived_at IS NULL "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, PUBLIC_PROFILE_PAGE, offset),
        ).fetchall()
        media = public_routes._batch_media(conn, [r["id"] for r in date_rows])
        dates = [public_routes.date_payload_from(r, media) for r in date_rows]
    elif tab == "want":
        rows = _profile_current_wants(
            conn, user_id, is_me=is_me, now=want_now,
            limit=PUBLIC_PROFILE_PAGE, offset=offset,
        )
        media = public_routes._batch_media(conn, [r["id"] for r in rows])
        want_dates = [public_routes.date_payload_from(r, media) for r in rows]
    else:
        rows = conn.execute(
            "SELECT r.id AS review_id, r.rating, r.text AS review_text, "
            "r.is_public AS review_public, r.updated_at AS review_updated_at, "
            "d.id, d.owner_id, d.name, d.share_token, d.starts_at, d.ends_at, d.place, "
            "d.is_public AS date_public, d.is_draft AS date_draft "
            "FROM date_reviews r JOIN dates d ON d.id=r.date_id "
            "WHERE r.user_id=?" + review_visibility +
            " ORDER BY r.updated_at DESC LIMIT ? OFFSET ?",
            (user_id, PUBLIC_PROFILE_PAGE, offset),
        ).fetchall()
        media = public_routes._batch_media(conn, [r["id"] for r in rows])
        for row in rows:
            item = dict(row)
            item["images"] = media["images"].get(row["id"], [])
            reviews.append(item)
    return {
        "dates": dates, "want_dates": want_dates, "reviews": reviews,
        "total": total, "event_total": event_total, "want_total": want_total,
        "review_total": review_total, "tab": tab, "page": page,
        "pages": pages, "is_me": is_me,
    }


@user_router.get("/{user_id}", response_class=HTMLResponse)
def public_profile(user_id: int, request: Request, conn=Depends(get_db)):
    u = conn.execute(
        "SELECT id, display_name, avatar_path, birth_date, gender, tg_username "
        "FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
    if not u:
        raise HTTPException(404, "Профиль не найден")
    me = _profile_viewer(request, conn)
    sections = _profile_sections_context(
        conn, user_id, int(me["id"]) if me else None, request.query_params,
    )
    # В обычном переходе профиль наследует оформление посетителя. Ссылки из
    # категории передают её skin явно, чтобы контекст категории сохранился.
    requested_skin = request.query_params.get("skin")
    profile_skin = appearance.normalize_skin(
        requested_skin,
        default=appearance.normalize_skin(
            me["admin_skin"] if me else appearance.ROMANTIC,
        ),
    )
    skin_suffix = f"&skin={profile_skin}" if requested_skin in appearance.VALID_SKINS else ""
    profile_base_url = f"/u/{user_id}"
    profile_return_url = (
        f"{profile_base_url}?tab={sections['tab']}&page={sections['page']}{skin_suffix}"
    )
    return templates.TemplateResponse(
        request, "public/profile.html",
        {"request": request, "u": u, "me": me,
         "csrf": request.session.get("csrf", ""),
         "profile_skin": profile_skin, "profile_base_url": profile_base_url,
         "profile_skin_suffix": skin_suffix,
         "public_events_label": "Коллекция событий",
         "profile_embedded": False, "profile_return_url": profile_return_url,
         **sections})


@user_router.get("/{user_id}/date/{did}/widget", response_class=HTMLResponse)
def public_profile_date_widget(user_id: int, did: int, request: Request,
                               conn=Depends(get_db)):
    """Встроенная карточка события, которое действительно видно в профиле.

    Отдельный профильный гейт нужен для отзывов старых/архивных встреч и для
    личных записей владельца: ослаблять публичный ``/admin/community``-виджет
    означало бы раскрывать любое событие простым перебором id.
    """
    if not conn.execute(
        "SELECT 1 FROM users WHERE id=? AND is_active=1", (user_id,),
    ).fetchone():
        raise HTTPException(404, "Профиль не найден")
    row = conn.execute(
        "SELECT d.*, u.display_name AS owner_name, u.tg_username AS owner_username, "
        "u.avatar_path AS owner_avatar "
        "FROM dates d JOIN users u ON u.id=d.owner_id WHERE d.id=?",
        (did,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Событие не найдено")
    if row["operator_review_pending"]:
        raise HTTPException(404, "Событие не найдено")

    me = _profile_viewer(request, conn)
    viewer_id = int(me["id"]) if me else None
    is_me = viewer_id is not None and int(user_id) == viewer_id
    date_public = bool(row["is_public"]) and not bool(row["is_draft"])
    visible = (
        int(row["owner_id"]) == int(user_id)
        and date_public
        and row["archived_at"] is None
    )

    want = conn.execute(
        "SELECT is_public FROM date_wants WHERE user_id=? AND date_id=?",
        (user_id, did),
    ).fetchone()
    if want and social_events.want_is_current(
            conn, did, user_id, now=now_naive()):
        visible = visible or is_me or (bool(want["is_public"]) and date_public)

    review = conn.execute(
        "SELECT is_public FROM date_reviews WHERE user_id=? AND date_id=?",
        (user_id, did),
    ).fetchone()
    if review:
        visible = visible or is_me or (bool(review["is_public"]) and date_public)

    if not visible:
        raise HTTPException(404, "Событие не найдено")
    return _event_widget_response(
        request, conn, row, viewer=me,
        widget_skin=request.query_params.get("skin"),
    )


@user_router.get("/{user_id}/reviews/{review_id}/widget", response_class=HTMLResponse)
def public_profile_review_widget(user_id: int, review_id: int, request: Request,
                                 conn=Depends(get_db)):
    """Отдельный виджет отзыва: не смешивает его с действиями события."""
    row = conn.execute(
        "SELECT r.id AS review_id, r.user_id, r.rating, r.text AS review_text, "
        "r.is_public AS review_public, "
        "d.id AS date_id, d.name, d.share_token, d.is_public AS date_public, "
        "d.is_draft AS date_draft, d.operator_review_pending, "
        "u.display_name, u.tg_username "
        "FROM date_reviews r JOIN dates d ON d.id=r.date_id "
        "JOIN users u ON u.id=r.user_id AND u.is_active=1 "
        "WHERE r.id=? AND r.user_id=?",
        (review_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Отзыв не найден")
    if row["operator_review_pending"]:
        raise HTTPException(404, "Отзыв не найден")
    me = _profile_viewer(request, conn)
    is_me = me is not None and int(user_id) == int(me["id"])
    shareable = bool(row["review_public"] and row["date_public"]
                     and not row["date_draft"])
    if not is_me and not shareable:
        raise HTTPException(404, "Отзыв не найден")
    review = dict(row)
    media = public_routes._batch_media(conn, [int(row["date_id"])])
    review["images"] = media["images"].get(int(row["date_id"]), [])
    return templates.TemplateResponse(
        request, "public/_profile_review_widget.html",
        {"request": request, "review": review, "me": me, "is_me": is_me,
         "shareable": shareable,
         "reviewer_display": row["display_name"] or row["tg_username"]
         or f"Человек #{user_id}",
         "profile_return_url": _safe_profile_return(
             request.query_params.get("next", ""), user_id,
         ),
         "review_share_url": (
             f"{BASE_URL}/d/{row['share_token']}/review/{review_id}"
         )})


def _owned_profile_review(conn, review_id: int, user_id: int):
    return conn.execute(
        "SELECT r.*, d.is_public AS date_public, d.is_draft AS date_draft "
        "FROM date_reviews r JOIN dates d ON d.id=r.date_id "
        "WHERE r.id=? AND r.user_id=?",
        (review_id, user_id),
    ).fetchone()


def _safe_profile_return(raw: str, user_id: int) -> str:
    """Разрешает возврат только в две оболочки текущего профиля."""
    fallback = f"/u/{user_id}?tab=reviews"
    if not raw:
        return fallback
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.path not in {
            "/admin/profile", f"/u/{user_id}"}:
        return fallback
    return raw


@user_router.post("/{user_id}/reviews/{review_id}/edit",
                  dependencies=[Depends(current_user)])
def public_profile_review_edit(user_id: int, review_id: int, request: Request,
                               rating: int = Form(...), text: str = Form(""),
                               next: str = Form(""),
                               conn=Depends(get_db)):
    if user_id != int(request.state.user["id"]):
        raise HTTPException(404, "Отзыв не найден")
    review = _owned_profile_review(conn, review_id, user_id)
    if not review:
        raise HTTPException(404, "Отзыв не найден")
    if not 1 <= rating <= 5:
        raise HTTPException(400, "Поставь оценку от 1 до 5")
    text = clean_text(text, 4000, "Текст отзыва")
    publish = 1 if review["date_public"] and not review["date_draft"] else 0
    conn.execute(
        "UPDATE date_reviews SET rating=?, text=?, is_public=?, updated_at=? "
        "WHERE id=? AND user_id=?",
        (rating, text or None, publish, now_iso(), review_id, user_id),
    )
    social_events.clear_review_waiting(conn, int(review["date_id"]), user_id)
    if publish:
        social_events.queue_review_received(conn, review_id)
    conn.commit()
    msg = "Отзыв обновлён" if publish else "Отзыв обновлён и оставлен скрытым"
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({
            "ok": True,
            "message": msg,
            "rating": rating,
            "text": text,
            "is_public": bool(publish),
        })
    return redir(_safe_profile_return(next, user_id), msg)


@user_router.post("/{user_id}/reviews/{review_id}/hide",
                  dependencies=[Depends(current_user)])
def public_profile_review_hide(user_id: int, review_id: int, request: Request,
                               next: str = Form(""),
                               conn=Depends(get_db)):
    if user_id != int(request.state.user["id"]):
        raise HTTPException(404, "Отзыв не найден")
    review = _owned_profile_review(conn, review_id, user_id)
    if not review:
        raise HTTPException(404, "Отзыв не найден")
    date_id = int(review["date_id"])
    social_events.cancel_review_received(conn, review_id, "review_deleted")
    conn.execute(
        "DELETE FROM date_reviews WHERE id=? AND user_id=?", (review_id, user_id),
    )
    social_events.mark_review_waiting(
        conn, date_id, user_id, "review_deleted", require_available=False,
    )
    conn.commit()
    return redir(_safe_profile_return(next, user_id),
                 "Отзыв удалён; событие перенесено в «Ждут отзыва»")


@user_router.get("/{user_id}/avatar")
def public_avatar(user_id: int, request: Request, w: int | None = None,
                  conn=Depends(get_db)):
    """Публичный аватар по id активного пользователя для страницы /u/<id>."""
    row = conn.execute(
        "SELECT avatar_path FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
    fn = row["avatar_path"] if row else None
    if not fn or not images.SAFE_FILENAME.match(fn):
        raise HTTPException(404)
    try:
        path = images.responsive_image(fn, w)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=300"})
