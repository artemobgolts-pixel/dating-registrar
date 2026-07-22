"""Вход через Telegram-бота (без пароля).

Поток:
  1. /login — страница с кнопкой «Войти через Telegram».
  2. POST /auth/start — сервер генерит одноразовый код, кладёт в login_codes
     (status=pending) и отдаёт deep-link https://t.me/<бот>?start=<код>.
  3. Человек жмёт Start, затем явно подтверждает вход inline-кнопкой в боте.
     Telegram шлёт апдейты на /tg/webhook с секретным заголовком; обработчик
     атомарно связывает код с telegram_id и только после кнопки подтверждает его.
  4. Страница /login поллит GET /auth/poll?code=… — как только код confirmed,
     заводим/обновляем пользователя, кладём user_id в сессию и редиректим в кабинет.

Коды живут TTL_SECONDS; протухшие чистятся лениво при обращении.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import sqlite3
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import users
from config import (BASE_URL, SUPPORT_CONTACT, TG_BOT_USERNAME, TG_WEBHOOK_SECRET,
                    OAUTH_PROVIDERS, OAUTH_LABELS, OAUTH_META)
from helpers import now_iso, now_naive
from datetime import timedelta
from urllib.parse import urlencode, quote
from ratelimit import client_ip, rate_ok
from web import get_db, templates

log = logging.getLogger("auth")
router = APIRouter()

TTL_SECONDS = 600          # код входа живёт 10 минут

# Username бота для кнопки входа и deep-link. Берём из env (TG_BOT_USERNAME);
# если там пусто — определяем по токену через getMe при старте (resolve_bot_username),
# чтобы вход работал, даже когда владелец прописал только TG_BOT_TOKEN.
BOT_USERNAME = TG_BOT_USERNAME
_WIDGET_STATES_KEY = "tg_widget_states"
_AUTH_FLOWS_KEY = "tg_auth_flows"


def issue_widget_state(request: Request) -> str:
    """Создаёт одноразовый nonce Telegram Widget, привязанный к сессии.

    Небольшой список позволяет независимо работать нескольким открытым вкладкам.
    SessionMiddleware подписывает значения и хранит их в HttpOnly-cookie.
    """
    values = request.session.get(_WIDGET_STATES_KEY, [])
    if not isinstance(values, list):
        values = []
    values = [v for v in values if isinstance(v, str) and 20 <= len(v) <= 128]
    state = secrets.token_urlsafe(24)
    request.session[_WIDGET_STATES_KEY] = (values + [state])[-5:]
    return state


def _consume_widget_state(request: Request, candidate: str | None) -> bool:
    """Однократно погашает nonce Widget, привязанный к браузерной сессии."""
    values = request.session.get(_WIDGET_STATES_KEY, [])
    if not candidate or not isinstance(values, list):
        return False
    match = next((v for v in values if isinstance(v, str)
                  and secrets.compare_digest(v, candidate)), None)
    if match is None:
        return False
    remaining = [v for v in values if v != match]
    if remaining:
        request.session[_WIDGET_STATES_KEY] = remaining
    else:
        request.session.pop(_WIDGET_STATES_KEY, None)
    return True


def _remember_auth_flow(request: Request, code: str, return_to: str | None) -> None:
    """Привязывает deep-link код к выпустившей его браузерной сессии."""
    flows = request.session.get(_AUTH_FLOWS_KEY, {})
    if not isinstance(flows, dict):
        flows = {}
    clean = {k: v for k, v in flows.items()
             if isinstance(k, str) and isinstance(v, str)}
    clean[code] = return_to or ""
    # Антиспам допускает десять попыток за окно — столько же держим в cookie.
    request.session[_AUTH_FLOWS_KEY] = dict(list(clean.items())[-10:])


def _auth_flow(request: Request, code: str) -> tuple[bool, str | None]:
    flows = request.session.get(_AUTH_FLOWS_KEY, {})
    if not isinstance(flows, dict) or code not in flows:
        return False, None
    return True, _safe_next(flows.get(code))


def _consume_auth_flow(request: Request, code: str) -> None:
    flows = request.session.get(_AUTH_FLOWS_KEY, {})
    if not isinstance(flows, dict) or code not in flows:
        return
    flows = dict(flows)
    flows.pop(code, None)
    if flows:
        request.session[_AUTH_FLOWS_KEY] = flows
    else:
        request.session.pop(_AUTH_FLOWS_KEY, None)


def _safe_next(raw: str | None) -> str | None:
    """Безопасный возврат после входа: только локальные пути в наши разделы
    (гостевая ссылка /c/…, share-ссылка свидания /d/… или кабинет /admin…). Чужой/
    протокол-относительный URL отбрасываем (open redirect)."""
    raw = (raw or "").strip()
    if not raw or raw.startswith("//") or not raw.startswith("/"):
        return None
    if raw.startswith(("/c/", "/d/", "/admin")):
        return raw
    return None


def _post_login_redirect(request) -> str:
    """Куда уводить после успешного входа: сохранённый безопасный next или кабинет."""
    nxt = _safe_next(request.session.pop("login_next", None))
    return nxt or "/admin/"


def resolve_bot_username() -> None:
    """Узнаёт @username бота по токену, если TG_BOT_USERNAME не задан.

    Так вход работает, даже когда в .env есть только TG_BOT_TOKEN: раньше при
    пустом username страница входа писала «Вход через Telegram пока не настроен».
    Идемпотентно и без сети, если username уже известен или токена нет.
    """
    global BOT_USERNAME
    import notify
    if BOT_USERNAME or not notify.TOKEN:
        return
    try:
        r = httpx.get(f"https://api.telegram.org/bot{notify.TOKEN}/getMe", timeout=10)
        if r.status_code != 200:
            log.warning("getMe вернул %s: %s", r.status_code, r.text[:200])
            return
        username = ((r.json().get("result") or {}).get("username") or "").lstrip("@")
        if username:
            BOT_USERNAME = username
            log.info("Username бота определён по токену: @%s", username)
        else:
            log.warning("getMe не вернул username бота")
    except Exception:
        log.exception("Не удалось определить username бота (getMe)")


def setup_webhook() -> None:
    """Регистрирует вебхук входа в Telegram при старте (если всё настроено).

    Идемпотентно: setWebhook можно звать сколько угодно. Без токена/секрета/домена
    тихо пропускаем (локальная разработка, тесты). Бот тот же, что для уведомлений.
    """
    import notify
    if not (notify.TOKEN and TG_WEBHOOK_SECRET and BASE_URL.startswith("https://")):
        return
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{notify.TOKEN}/setWebhook",
            json={"url": f"{BASE_URL}/tg/webhook",
                  "secret_token": TG_WEBHOOK_SECRET,
                  "allowed_updates": ["message", "callback_query"]},
            timeout=10)
        if r.status_code >= 400:
            log.warning("setWebhook вернул %s: %s", r.status_code, r.text[:200])
        else:
            log.info("Telegram-вебхук входа зарегистрирован")
    except Exception:
        log.exception("Не удалось зарегистрировать Telegram-вебхук")


def _gc_codes(conn) -> None:
    """Сносит протухшие коды (ленивая чистка — отдельный loop не нужен).

    Сравниваем ISO-строки напрямую: created_at пишется через now_iso() (МСК,
    фиксированная ширина YYYY-MM-DDTHH:MM:SS), поэтому лексикографический порядок
    совпадает с хронологическим. Никакого mktime — он читал бы МСК-метку в TZ
    сервера (в Docker UTC) и раздувал реальный TTL до ~3 часов.
    """
    cutoff = _code_cutoff()
    conn.execute("DELETE FROM login_codes WHERE created_at < ?", (cutoff,))
    conn.commit()


def _code_cutoff() -> str:
    """Нижняя граница свежего кода в том же локальном ISO-формате, что БД."""
    return (now_naive() - timedelta(seconds=TTL_SECONDS)).isoformat(sep="T")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, conn=Depends(get_db)):
    # Запоминаем, куда вернуть после входа (например, на гостевую ссылку, с
    # которой пользователь нажал «Войти»). Только безопасные локальные пути.
    nxt = _safe_next(request.query_params.get("next"))
    if "next" in request.query_params:
        request.session.pop("login_next", None)
        if nxt:
            request.session["login_next"] = nxt
    elif "msg" not in request.query_params:
        # Обычный новый заход не должен наследовать адрес от старой/оборванной
        # попытки. При возврате с ошибкой OAuth (?msg=...) оставляем его для retry.
        request.session.pop("login_next", None)
    if request.session.get("user_id") and users.get_user(conn, request.session["user_id"]):
        return RedirectResponse(_post_login_redirect(request), status_code=303)
    return templates.TemplateResponse(
        request, "auth/login.html",
        {"bot": BOT_USERNAME, "oauth": _oauth_buttons(), "next_url": nxt,
         "widget_state": issue_widget_state(request) if BOT_USERNAME else None})


def _oauth_buttons() -> list[dict]:
    """Список провайдеров OAuth для шаблонов входа: {slug, label, enabled}.
    enabled=False (нет client_id) — кнопка показывается, но помечена «скоро»."""
    return [{"slug": slug, "label": OAUTH_LABELS.get(slug, slug),
             "enabled": bool(cid)}
            for slug, (cid, _s) in OAUTH_PROVIDERS.items()]


def _verify_widget(data: dict) -> bool:
    """Проверяет подпись Telegram Login Widget (https://core.telegram.org/widgets/login).

    secret = SHA256(bot_token); HMAC-SHA256 по строке "k=v\\n..." (ключи отсортированы,
    кроме hash). Совпадение hash доказывает, что данные пришли от Telegram и не
    подделаны. Плюс проверяем свежесть auth_date (не старше 1 дня) от replay.
    """
    import notify
    if not notify.TOKEN:
        return False
    got = data.get("hash") or ""
    # return_to и widget_state задаём мы в data-auth-url до перехода в Telegram.
    # Telegram их не подписывает, поэтому они не входят в check-string:
    # return_to отдельно проходит allow-list, а state сверяется с HttpOnly-
    # сессией и одноразово погашается в колбэке.
    pairs = sorted(f"{k}={v}" for k, v in data.items()
                   if k not in ("hash", "return_to", "widget_state"))
    secret = hashlib.sha256(notify.TOKEN.encode()).digest()
    calc = hmac.new(secret, "\n".join(pairs).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return False
    try:
        if time.time() - int(data.get("auth_date", 0)) > 86400:
            return False
    except (TypeError, ValueError):
        return False
    return True


@router.get("/auth/widget")
def auth_widget(request: Request, conn=Depends(get_db)):
    """Колбэк Telegram Login Widget (redirect-режим). Telegram редиректит сюда
    с полями id/first_name/username/auth_date/hash в query. Проверяем подпись,
    логиним. Бот при этом НЕ запускается — bot_linked не выставляем."""
    ip = client_ip(request)
    if not rate_ok(f"widget:{ip}", 10, 600):
        raise HTTPException(429, "Слишком много попыток входа. Подожди немного.")
    data = dict(request.query_params)
    if not data.get("id") or not _verify_widget(data):
        raise HTTPException(403, "Подпись Telegram не подтвердилась. Попробуй ещё раз.")
    if not _consume_widget_state(request, data.get("widget_state")):
        raise HTTPException(403, "Сессия входа устарела. Открой страницу входа заново.")
    # return_to не является полем Telegram и потому не покрыт HMAC. Разрешаем
    # только локальные разделы; внешний/протокол-относительный адрес отбрасываем.
    if "return_to" in data:
        request.session.pop("login_next", None)
        nxt = _safe_next(data.get("return_to"))
        if nxt:
            request.session["login_next"] = nxt
    tg_id = int(data["id"])
    uid = users.upsert_on_login(conn, tg_id, username=data.get("username"),
                                first_name=data.get("first_name"), link_bot=False)
    user = users.get_user(conn, uid)
    if not user["is_active"]:
        raise HTTPException(403, "Доступ закрыт. Напиши в поддержку.")
    request.session["user_id"] = uid
    request.session["csrf"] = secrets.token_urlsafe(16)
    return RedirectResponse(_post_login_redirect(request), status_code=303)


@router.post("/auth/start")
def auth_start(request: Request, conn=Depends(get_db)):
    """Создаёт одноразовый код и возвращает deep-link на бота.

    Для анонима это код входа. Для уже открытой сессии — purpose-bound код
    подключения Telegram к тому же user_id: подтверждение такого кода никогда
    не должно переключать браузер на другой аккаунт.
    """
    ip = client_ip(request)
    # анти-спам: не больше 10 кодов с одного IP за 10 минут
    if not rate_ok(f"authstart:{ip}", 10, 600):
        raise HTTPException(429, "Слишком много попыток входа. Подожди немного.")
    if not BOT_USERNAME:
        raise HTTPException(503, "Вход через Telegram не настроен (TG_BOT_USERNAME).")
    # Публичные баннеры запускают вход на месте. Запоминаем только проверенный
    # локальный адрес возврата и затем привязываем его к конкретному коду.
    explicit_nxt = None
    if "return_to" in request.query_params:
        request.session.pop("login_next", None)
        explicit_nxt = _safe_next(request.query_params.get("return_to"))
        if explicit_nxt:
            request.session["login_next"] = explicit_nxt
    _gc_codes(conn)
    code = secrets.token_urlsafe(12)
    session_uid = request.session.get("user_id")
    target = users.get_user(conn, session_uid) if session_uid else None
    link_uid = target["id"] if target and target["is_active"] else None
    flow_nxt = (explicit_nxt if link_uid else
                (explicit_nxt or _safe_next(request.session.get("login_next"))))
    _remember_auth_flow(request, code, flow_nxt)
    conn.execute(
        "INSERT INTO login_codes(code, status, created_at, purpose, user_id) "
        "VALUES(?, 'pending', ?, ?, ?)",
        (code, now_iso(), "link" if link_uid else "login", link_uid))
    conn.commit()
    return JSONResponse({"code": code,
                         "url": f"https://t.me/{BOT_USERNAME}?start={code}"})


@router.get("/auth/poll")
def auth_poll(request: Request, code: str, conn=Depends(get_db)):
    """Поллинг страницей входа. Как только код confirmed — логиним и редиректим.

    Вебхук (отдельный запрос от Telegram, без сессии браузера) проставил коду
    telegram_id. Здесь по нему резолвим пользователя и кладём user_id в ЭТУ сессию.
    """
    known_flow, flow_nxt = _auth_flow(request, code)
    if not known_flow:
        return JSONResponse({"status": "forbidden"}, status_code=403)
    row = conn.execute(
        "SELECT * FROM login_codes WHERE code=?", (code,)).fetchone()
    if not row:
        _consume_auth_flow(request, code)
        return JSONResponse({"status": "expired"})
    if row["created_at"] < _code_cutoff():
        conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
        conn.commit()
        _consume_auth_flow(request, code)
        return JSONResponse({"status": "expired"})
    if row["purpose"] == "link":
        # Код жёстко привязан к сессии, которая его выпустила. Даже успешно
        # подтверждённый link-код нельзя использовать для входа в ином браузере.
        if request.session.get("user_id") != row["user_id"]:
            return JSONResponse({"status": "forbidden"}, status_code=403)
        if row["status"] == "conflict":
            conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
            conn.commit()
            _consume_auth_flow(request, code)
            return JSONResponse({"status": "conflict"})
        if row["status"] != "confirmed":
            return JSONResponse({"status": "pending"})
        user = users.get_user(conn, row["user_id"])
        if not user or not user["is_active"]:
            conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
            conn.commit()
            _consume_auth_flow(request, code)
            return JSONResponse({"status": "banned"})
        conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
        conn.commit()
        _consume_auth_flow(request, code)
        # user_id в сессии намеренно не меняем: Telegram только привязан.
        if flow_nxt and request.session.get("login_next") == flow_nxt:
            request.session.pop("login_next", None)
        return JSONResponse({"status": "ok", "linked": True,
                             "redirect": flow_nxt or "/admin/profile"})
    if row["status"] != "confirmed" or not row["telegram_id"]:
        return JSONResponse({"status": "pending"})
    user = users.get_by_telegram(conn, row["telegram_id"])
    if not user:
        return JSONResponse({"status": "pending"})
    if not user["is_active"]:
        conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
        conn.commit()
        _consume_auth_flow(request, code)
        return JSONResponse({"status": "banned"})
    conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
    conn.commit()
    _consume_auth_flow(request, code)
    request.session["user_id"] = user["id"]
    request.session["csrf"] = secrets.token_urlsafe(16)
    if flow_nxt and request.session.get("login_next") == flow_nxt:
        request.session.pop("login_next", None)
    return JSONResponse({"status": "ok", "redirect": flow_nxt or "/admin/"})


def _link_telegram(conn, user_id: int, telegram_id: int,
                   username: str | None) -> tuple[bool, str | None]:
    """Привязывает Telegram к заданному аккаунту без автоматического слияния.

    Повторная привязка того же Telegram к тому же пользователю идемпотентна.
    Если telegram_id уже принадлежит другому user_id, возвращаем конфликт и не
    меняем ни один аккаунт.
    """
    target = users.get_user(conn, user_id)
    if not target or not target["is_active"]:
        return False, "account_unavailable"
    if target["telegram_id"] is not None and target["telegram_id"] != telegram_id:
        return False, "telegram_mismatch"
    existing = users.get_by_telegram(conn, telegram_id)
    if existing and existing["id"] != user_id:
        return False, "telegram_in_use"
    try:
        conn.execute(
            "UPDATE users SET telegram_id=?, tg_username=?, "
            "is_operator=CASE WHEN ? THEN 1 ELSE is_operator END, "
            "bot_linked=1, last_login_at=? WHERE id=?",
            (telegram_id, username, 1 if telegram_id in users.OPERATOR_TG_IDS else 0,
             now_iso(), user_id))
    except sqlite3.IntegrityError:
        # Между SELECT и UPDATE другой запрос мог успеть занять telegram_id.
        return False, "telegram_in_use"
    return True, None


@router.post("/tg/webhook")
async def tg_webhook(request: Request, conn=Depends(get_db)):
    """Приём /start и явного подтверждения inline-кнопкой в личке бота.

    Защита: секретный заголовок X-Telegram-Bot-Api-Secret-Token (его знает
    только Telegram, которому мы сами отдали секрет при setWebhook). Без
    совпадения — 403, иначе кто угодно подтвердит чужой код.
    """
    hdr = request.headers.get("x-telegram-bot-api-secret-token") or ""
    if not TG_WEBHOOK_SECRET or not secrets.compare_digest(hdr, TG_WEBHOOK_SECRET):
        raise HTTPException(403, "forbidden")
    update = await request.json()
    import notify

    callback = (update or {}).get("callback_query") or {}
    if callback:
        data = (callback.get("data") or "").strip()
        frm = callback.get("from") or {}
        tg_id = frm.get("id")
        action = code = ""
        if data.startswith("auth_confirm:"):
            action, code = "confirm", data.split(":", 1)[1]
        elif data.startswith("auth_cancel:"):
            action, code = "cancel", data.split(":", 1)[1]
        try:
            tg_id = int(tg_id)
        except (TypeError, ValueError):
            tg_id = 0
        cutoff = _code_cutoff()
        row = conn.execute(
            "SELECT * FROM login_codes WHERE code=? AND created_at>=?", (code, cutoff)
        ).fetchone() if code else None
        if (not row or row["status"] != "awaiting_confirmation" or not tg_id
                or int(row["telegram_id"] or 0) != tg_id):
            await asyncio.to_thread(
                notify.answer_callback, callback.get("id") or "", "Код уже не действует")
            return JSONResponse({"ok": True})

        if action == "cancel":
            deleted = conn.execute(
                "DELETE FROM login_codes WHERE code=? "
                "AND status='awaiting_confirmation' AND telegram_id=? AND created_at>=?",
                (code, tg_id, cutoff),
            )
            if deleted.rowcount != 1:
                conn.rollback()
                await asyncio.to_thread(
                    notify.answer_callback, callback.get("id") or "",
                    "Код уже не действует")
                return JSONResponse({"ok": True})
            conn.commit()
            await asyncio.gather(
                asyncio.to_thread(
                    notify.answer_callback, callback.get("id") or "", "Вход отменён"),
                asyncio.to_thread(
                    notify.send_to, tg_id,
                    "Вход отменён. Аккаунт и сессия не изменены."),
            )
            return JSONResponse({"ok": True})

        # Забираем код условным UPDATE. Это сериализует повторный callback и
        # гонку «Подтвердить / Отмена»: побочные действия выполнит только один.
        claimed = conn.execute(
            "UPDATE login_codes SET status='processing' WHERE code=? "
            "AND status='awaiting_confirmation' AND telegram_id=? AND created_at>=?",
            (code, tg_id, cutoff),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            await asyncio.to_thread(
                notify.answer_callback, callback.get("id") or "", "Код уже не действует")
            return JSONResponse({"ok": True})

        if row["purpose"] == "link":
            ok, error = _link_telegram(
                conn, row["user_id"], tg_id, frm.get("username"))
            conn.execute(
                "UPDATE login_codes SET status=?, error=? WHERE code=?",
                ("confirmed" if ok else "conflict", error, code),
            )
            if ok:
                message = ("🔔 Telegram подключён к вашему аккаунту <b>" +
                           notify.esc(BASE_URL) + "</b>. Уведомления включены.")
            else:
                message = ("⚠️ Этот Telegram уже связан с другим аккаунтом <b>" +
                           notify.esc(BASE_URL) + "</b>. Аккаунты не объединялись.")
        else:
            users.upsert_on_login(
                conn, tg_id, username=frm.get("username"),
                first_name=frm.get("first_name"), link_bot=True, commit=False)
            conn.execute(
                "UPDATE login_codes SET status='confirmed' WHERE code=?", (code,))
            message = (
                "🔓 Вход в кабинет <b>" + notify.esc(BASE_URL) +
                "</b> подтверждён. Вернитесь в браузер.\n\n"
                "Если это были не вы, сразу сообщите в поддержку: <b>" +
                notify.esc(SUPPORT_CONTACT or "контакт указан на сайте") + "</b>.")
        conn.commit()
        await asyncio.gather(
            asyncio.to_thread(
                notify.answer_callback, callback.get("id") or "", "Подтверждено"),
            asyncio.to_thread(notify.send_to, tg_id, message),
        )
        return JSONResponse({"ok": True})

    msg = (update or {}).get("message") or {}
    text = (msg.get("text") or "").strip()
    frm = msg.get("from") or {}
    tg_id = frm.get("id")
    # Бот реагирует только на «/start <код>» от настоящего пользователя.
    if not tg_id or not text.startswith("/start"):
        return JSONResponse({"ok": True})
    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    cutoff = _code_cutoff()
    row = conn.execute(
        "SELECT * FROM login_codes WHERE code=? AND created_at>=?", (code, cutoff)
    ).fetchone() if code else None
    if not row or row["status"] != "pending":
        return JSONResponse({"ok": True})
    tg_id = int(tg_id)
    claimed = conn.execute(
        "UPDATE login_codes SET status='awaiting_confirmation', telegram_id=? "
        "WHERE code=? AND status='pending' AND created_at>=?",
        (tg_id, code, cutoff),
    )
    if claimed.rowcount != 1:
        conn.rollback()
        return JSONResponse({"ok": True})
    conn.commit()
    if row["purpose"] == "link":
        title = "Подключить Telegram-уведомления к открытому аккаунту?"
    else:
        title = "Войти в кабинет date4you в открытом браузере?"
    prompt = (
        "🔐 <b>" + title + "</b>\n\n"
        "Подтверждайте только если вы сами только что начали это действие на <b>" +
        notify.esc(BASE_URL) + "</b>. Никому не пересылайте ссылку или код.")
    delivered = await asyncio.to_thread(
        notify.send_to, tg_id, prompt,
        reply_markup={"inline_keyboard": [[
            {"text": "Подтвердить", "callback_data": f"auth_confirm:{code}"},
            {"text": "Отмена", "callback_data": f"auth_cancel:{code}"},
        ]]},
    )
    # При реальном сбое отправки разрешаем повторный /start. В тестовой/локальной
    # среде без TG_BOT_TOKEN прямые webhook-вызовы остаются доступными для smoke.
    if notify.TOKEN and not delivered:
        conn.execute(
            "UPDATE login_codes SET status='pending', telegram_id=NULL "
            "WHERE code=? AND status='awaiting_confirmation' AND telegram_id=?",
            (code, tg_id),
        )
        conn.commit()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# OAuth-провайдеры: Discord / Google / Yandex
# ---------------------------------------------------------------------------
# Полный Authorization Code Flow. Регистрация — standalone: OAuth заводит
# отдельный аккаунт без telegram_id (см. users.upsert_oauth_login). Если человек
# уже вошёл (есть сессия) и жмёт «Привязать» в профиле — режим link: привязываем
# соцсеть к текущему аккаунту, не создавая новый.
#
# Провайдер не настроен (нет client_id) → 503, но кнопка на входе видна. state
# кладём в сессию (CSRF-защита колбэка). redirect_uri обязан совпадать с тем,
# что вписан в настройках приложения провайдера: <BASE_URL>/auth/<provider>/callback.

def _oauth_redirect_uri(provider: str) -> str:
    return f"{BASE_URL}/auth/{provider}/callback"


@router.get("/auth/{provider}")
def oauth_start(provider: str, request: Request):
    """Старт OAuth: редирект на страницу авторизации провайдера. Параметр
    ?link=1 запоминает, что это привязка к текущему аккаунту (из профиля)."""
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(404, "Неизвестный провайдер входа")
    client_id, _secret = OAUTH_PROVIDERS[provider]
    label = OAUTH_LABELS.get(provider, provider)
    if not client_id:
        raise HTTPException(503, f"Вход через {label} ещё не настроен")

    ip = client_ip(request)
    if not rate_ok(f"oauth:{ip}", 20, 600):
        raise HTTPException(429, "Слишком много попыток входа. Подожди немного.")

    meta = OAUTH_META[provider]
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    request.session["oauth_provider"] = provider
    # режим привязки — только если пользователь уже вошёл
    request.session["oauth_link"] = bool(
        request.query_params.get("link") and request.session.get("user_id"))
    # куда вернуться после входа (для обычного логина)
    nxt = _safe_next(request.query_params.get("next"))
    if nxt:
        request.session["login_next"] = nxt

    params = {
        "client_id": client_id,
        "redirect_uri": _oauth_redirect_uri(provider),
        "response_type": "code",
        "scope": meta["scope"],
        "state": state,
    }
    if provider == "google":
        params["access_type"] = "online"
        params["prompt"] = "select_account"
    url = meta["authorize"] + "?" + urlencode(params)
    return RedirectResponse(url, status_code=303)


def _oauth_fetch_identity(provider: str, code: str) -> dict:
    """Меняет authorization code на access token и тянет профиль пользователя.
    Возвращает {'uid','name','email'} или бросает HTTPException при сбое."""
    client_id, client_secret = OAUTH_PROVIDERS[provider]
    meta = OAUTH_META[provider]
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _oauth_redirect_uri(provider),
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        with httpx.Client(timeout=10) as cli:
            tok = cli.post(meta["token"], data=data,
                           headers={"Accept": "application/json"})
            if tok.status_code != 200:
                log.warning("OAuth %s: обмен кода не удался: %s %s",
                            provider, tok.status_code, tok.text[:200])
                raise HTTPException(502, "Провайдер не выдал токен. Попробуй ещё раз.")
            access = tok.json().get("access_token")
            if not access:
                raise HTTPException(502, "Провайдер не выдал токен. Попробуй ещё раз.")
            ui = cli.get(meta["userinfo"],
                         headers={"Authorization": f"Bearer {access}",
                                  "Accept": "application/json"})
            if ui.status_code != 200:
                log.warning("OAuth %s: userinfo не удался: %s", provider, ui.status_code)
                raise HTTPException(502, "Не удалось получить профиль. Попробуй ещё раз.")
            info = ui.json()
    except httpx.HTTPError:
        log.exception("OAuth %s: сетевая ошибка обмена", provider)
        raise HTTPException(502, "Провайдер недоступен. Попробуй позже.")

    uid = info.get(meta["uid_field"])
    if not uid:
        raise HTTPException(502, "Провайдер не вернул идентификатор пользователя.")
    name = next((info[f] for f in meta["name_fields"] if info.get(f)), None)
    email = info.get(meta["email_field"])
    return {"uid": str(uid), "name": name, "email": email}


@router.get("/auth/{provider}/callback")
def oauth_callback(provider: str, request: Request, conn=Depends(get_db)):
    """Колбэк провайдера: сверяем state, меняем code на профиль, логиним/привязываем."""
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(404, "Неизвестный провайдер входа")
    if not OAUTH_PROVIDERS[provider][0]:
        raise HTTPException(503, "Провайдер не настроен")

    if request.query_params.get("error"):
        return RedirectResponse("/login?msg=" +
                                quote("Вход отменён."), status_code=303)
    state = request.query_params.get("state")
    saved = request.session.pop("oauth_state", None)
    is_link = request.session.pop("oauth_link", False)
    request.session.pop("oauth_provider", None)
    if not state or not saved or not secrets.compare_digest(state, saved):
        raise HTTPException(403, "Проверка state не прошла. Начни вход заново.")
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(400, "Провайдер не вернул код авторизации.")

    ident = _oauth_fetch_identity(provider, code)

    # режим привязки: пользователь уже вошёл — привязываем соцсеть к его аккаунту
    if is_link and request.session.get("user_id"):
        uid = request.session["user_id"]
        ok = users.link_oauth_account(conn, uid, provider, ident["uid"],
                                      email=ident["email"])
        msg = ("Аккаунт привязан ♥" if ok else
               "Эта соцсеть уже привязана к другому профилю.")
        return RedirectResponse("/admin/profile?msg=" + quote(msg), status_code=303)

    # обычный вход/регистрация
    uid = users.upsert_oauth_login(conn, provider, ident["uid"],
                                   display_name=ident["name"], email=ident["email"])
    user = users.get_user(conn, uid)
    if not user or not user["is_active"]:
        raise HTTPException(403, "Доступ закрыт. Напиши в поддержку.")
    request.session["user_id"] = uid
    request.session["csrf"] = secrets.token_urlsafe(16)
    return RedirectResponse(_post_login_redirect(request), status_code=303)
