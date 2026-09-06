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
import json
import logging
import secrets
import sqlite3
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import metrics
import users
from config import (BASE_URL, SUPPORT_CONTACT, TG_BOT_USERNAME, TG_MINI_APP_URL,
                    TG_WEBHOOK_SECRET, OAUTH_PROVIDERS, OAUTH_LABELS, OAUTH_META)
from helpers import now_iso, now_naive
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, quote, urlsplit
from ratelimit import client_ip, rate_ok
from web import get_db, templates

log = logging.getLogger("auth")
router = APIRouter()

TTL_SECONDS = 600          # код входа живёт 10 минут
MINIAPP_AUTH_TTL_SECONDS = 600
MINIAPP_MAX_INIT_DATA = 8192
START_VIDEO_PATH = Path(__file__).resolve().parent / "static" / "telegram-start-logo.mp4"

# Username бота для кнопки входа и deep-link. Берём из env (TG_BOT_USERNAME);
# если там пусто — определяем по токену через getMe при старте (resolve_bot_username),
# чтобы вход работал, даже когда владелец прописал только TG_BOT_TOKEN.
BOT_USERNAME = TG_BOT_USERNAME
_WIDGET_STATES_KEY = "tg_widget_states"
_AUTH_FLOWS_KEY = "tg_auth_flows"
_MINIAPP_NONCES_KEY = "tg_miniapp_nonces"


def _miniapp_url() -> str | None:
    """Возвращает только пригодный для Telegram HTTPS URL Mini App."""
    value = (TG_MINI_APP_URL or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return value


def _miniapp_button(label: str = "Открыть date4you") -> dict | None:
    miniapp_url = _miniapp_url()
    browser_url = (BASE_URL or "").strip().rstrip("/") + "/"
    browser = urlsplit(browser_url)
    if (browser.scheme != "https" or not browser.netloc
            or browser.username is not None or browser.password is not None):
        browser_url = ""
    if not miniapp_url and not browser_url:
        return None
    rows = []
    if miniapp_url:
        rows.append([{
            "text": label,
            "style": "primary",
            "web_app": {"url": miniapp_url},
        }])
    if browser_url:
        # Это намеренно обычная URL-кнопка, а не ``web_app``: Telegram передаст
        # ссылку системному браузеру на Desktop и телефоне. Отдельная строка не
        # сжимает длинные подписи на узком экране.
        rows.append([{
            "text": "Открыть в браузере",
            "url": browser_url,
        }])
    return {
        "inline_keyboard": rows,
    }


def _issue_miniapp_nonce(request: Request) -> str:
    """Одноразово связывает initData POST с загруженной нами boot-страницей."""
    nonce = secrets.token_urlsafe(24)
    values = request.session.get(_MINIAPP_NONCES_KEY, [])
    if not isinstance(values, list):
        values = []
    request.session[_MINIAPP_NONCES_KEY] = [
        *(str(value) for value in values[-3:]), nonce,
    ]
    return nonce


def _consume_miniapp_nonce(request: Request, supplied: object) -> bool:
    values = request.session.get(_MINIAPP_NONCES_KEY, [])
    if not isinstance(values, list) or not isinstance(supplied, str):
        return False
    matched = next((value for value in values
                    if isinstance(value, str)
                    and hmac.compare_digest(value, supplied)), None)
    if matched is None:
        return False
    remaining = [value for value in values if value != matched]
    if remaining:
        request.session[_MINIAPP_NONCES_KEY] = remaining
    else:
        request.session.pop(_MINIAPP_NONCES_KEY, None)
    return True


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
    (гостевая /c/…, событие /d/…, профиль /u/… или кабинет /admin…). Чужой/
    протокол-относительный URL отбрасываем (open redirect)."""
    raw = (raw or "").strip()
    if not raw or raw.startswith("//") or not raw.startswith("/"):
        return None
    if raw.startswith(("/c/", "/d/", "/u/", "/admin")):
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
        with metrics.track_dependency("telegram", "get_me") as observation:
            r = httpx.get(
                f"https://api.telegram.org/bot{notify.TOKEN}/getMe", timeout=10
            )
            if r.status_code != 200:
                observation.fail()
        if r.status_code != 200:
            log.warning(
                "Telegram getMe завершился ошибкой",
                extra={"event": "telegram_setup_failed", "provider": "telegram",
                       "operation": "get_me", "status_code": r.status_code,
                       "status_class": f"{r.status_code // 100}xx", "outcome": "failure"},
            )
            return
        username = ((r.json().get("result") or {}).get("username") or "").lstrip("@")
        if username:
            BOT_USERNAME = username
            log.info(
                "Username Telegram-бота определён по токену",
                extra={"event": "telegram_bot_username_resolved",
                       "provider": "telegram", "outcome": "success"},
            )
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
        with metrics.track_dependency("telegram", "set_webhook") as observation:
            r = httpx.post(
                f"https://api.telegram.org/bot{notify.TOKEN}/setWebhook",
                json={"url": f"{BASE_URL}/tg/webhook",
                      "secret_token": TG_WEBHOOK_SECRET,
                      "allowed_updates": ["message", "callback_query", "my_chat_member"]},
                timeout=10)
            if r.status_code >= 400:
                observation.fail()
        if r.status_code >= 400:
            log.warning(
                "Telegram setWebhook завершился ошибкой",
                extra={"event": "telegram_setup_failed", "provider": "telegram",
                       "operation": "set_webhook", "status_code": r.status_code,
                       "status_class": f"{r.status_code // 100}xx", "outcome": "failure"},
            )
        else:
            log.info("Telegram-вебхук входа зарегистрирован")
    except Exception:
        log.exception("Не удалось зарегистрировать Telegram-вебхук")


def setup_miniapp_menu() -> None:
    """Идемпотентно ставит Mini App основной кнопкой меню личного чата.

    Ту же точку входа бот присылает после /start. Глобальная menu button нужна,
    чтобы затем открывать кабинет одним нажатием и на телефоне, и в Desktop.
    """
    import notify
    url = _miniapp_url()
    if not (notify.TOKEN and url):
        return
    try:
        with metrics.track_dependency("telegram", "set_menu") as observation:
            r = httpx.post(
                f"https://api.telegram.org/bot{notify.TOKEN}/setChatMenuButton",
                json={
                    "menu_button": {
                        "type": "web_app",
                        "text": "Открыть date4you",
                        "web_app": {"url": url},
                    },
                },
                timeout=10,
            )
            if r.status_code >= 400:
                observation.fail()
        if r.status_code >= 400:
            log.warning(
                "Telegram setChatMenuButton завершился ошибкой",
                extra={"event": "telegram_setup_failed", "provider": "telegram",
                       "operation": "set_menu", "status_code": r.status_code,
                       "status_class": f"{r.status_code // 100}xx", "outcome": "failure"},
            )
        else:
            log.info("Кнопка меню Telegram Mini App зарегистрирована")
    except Exception:
        log.exception("Не удалось зарегистрировать кнопку Telegram Mini App")


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
        metrics.observe_auth(
            flow="telegram_widget", provider="telegram", result="rate_limited",
        )
        raise HTTPException(429, "Слишком много попыток входа. Подожди немного.")
    data = dict(request.query_params)
    if not data.get("id") or not _verify_widget(data):
        metrics.observe_auth(
            flow="telegram_widget", provider="telegram", result="invalid",
        )
        raise HTTPException(403, "Подпись Telegram не подтвердилась. Попробуй ещё раз.")
    if not _consume_widget_state(request, data.get("widget_state")):
        metrics.observe_auth(
            flow="telegram_widget", provider="telegram", result="expired",
        )
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
        metrics.observe_auth(
            flow="telegram_widget", provider="telegram", result="banned",
        )
        raise HTTPException(403, "Доступ закрыт. Напиши в поддержку.")
    request.session["user_id"] = uid
    request.session["csrf"] = secrets.token_urlsafe(16)
    metrics.observe_auth(
        flow="telegram_widget", provider="telegram", result="success",
    )
    return RedirectResponse(_post_login_redirect(request), status_code=303)


def verify_miniapp_init_data(raw: str, *, now: float | None = None) -> dict:
    """Проверяет сырые ``Telegram.WebApp.initData`` и возвращает user.

    Алгоритм ровно из документации Mini Apps: secret key — HMAC-SHA256 токена
    с ключом ``WebAppData``, затем HMAC data-check-string. ``initDataUnsafe`` с
    клиента здесь принципиально не принимается. Короткий TTL ограничивает replay.
    """
    import notify
    if not notify.TOKEN:
        raise HTTPException(503, "Telegram Mini App пока не настроен.")
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MINIAPP_MAX_INIT_DATA:
        raise HTTPException(400, "Некорректные данные Telegram.")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=64)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Некорректные данные Telegram.") from None
    values: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in values:
            raise HTTPException(400, "Некорректные данные Telegram.")
        values[key] = value
    received_hash = values.pop("hash", "")
    if len(received_hash) != 64:
        raise HTTPException(403, "Подпись Telegram не подтвердилась.")
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", notify.TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(403, "Подпись Telegram не подтвердилась.")
    try:
        auth_date = int(values.get("auth_date", ""))
    except (TypeError, ValueError):
        raise HTTPException(403, "Сессия Telegram устарела.") from None
    current = time.time() if now is None else now
    age = current - auth_date
    if age < -30 or age > MINIAPP_AUTH_TTL_SECONDS:
        raise HTTPException(403, "Сессия Telegram устарела. Открой Mini App заново.")
    try:
        person = json.loads(values.get("user", ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(400, "Telegram не передал профиль пользователя.") from None
    if not isinstance(person, dict) or person.get("is_bot") is True:
        raise HTTPException(400, "Telegram не передал профиль пользователя.")
    try:
        telegram_id = int(person.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Telegram не передал профиль пользователя.") from None
    if telegram_id <= 0 or telegram_id >= 2**63:
        raise HTTPException(400, "Некорректный Telegram ID.")
    person["id"] = telegram_id
    return person


@router.get("/tg/app", response_class=HTMLResponse)
def miniapp_page(request: Request):
    """Небольшой boot-screen: SDK получает initData, backend заводит сессию."""
    return templates.TemplateResponse(
        request,
        "auth/miniapp.html",
        {"bot": BOT_USERNAME, "miniapp_ready": bool(_miniapp_url()),
         "miniapp_nonce": _issue_miniapp_nonce(request)},
    )


@router.post("/auth/miniapp")
async def auth_miniapp(request: Request, conn=Depends(get_db)):
    """Безопасный автологин Mini App для Telegram mobile, Desktop и Web."""
    ip = client_ip(request)
    if not rate_ok(f"miniapp:{ip}", 30, 60):
        metrics.observe_auth(
            flow="miniapp", provider="telegram", result="rate_limited",
        )
        raise HTTPException(429, "Слишком много попыток. Открой Mini App заново.")
    content_length = request.headers.get("content-length")
    if not content_length or not content_length.isdigit():
        raise HTTPException(411, "Не указан размер запроса Mini App.")
    if int(content_length) > 16384:
        raise HTTPException(413, "Слишком большой запрос.")
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "Ожидался JSON-запрос Mini App.")
    expected_origin = f"{urlsplit(BASE_URL).scheme}://{urlsplit(BASE_URL).netloc}"
    if request.headers.get("origin", "").rstrip("/") != expected_origin:
        raise HTTPException(403, "Источник запроса Mini App не подтверждён.")
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(400, "Ожидались данные Telegram.") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Ожидались данные Telegram.")
    if not _consume_miniapp_nonce(request, payload.get("nonce")):
        metrics.observe_auth(
            flow="miniapp", provider="telegram", result="expired",
        )
        raise HTTPException(403, "Страница Mini App устарела. Открой её заново.")
    try:
        person = verify_miniapp_init_data(payload.get("init_data"))
    except HTTPException:
        metrics.observe_auth(
            flow="miniapp", provider="telegram", result="invalid",
        )
        raise
    # allows_write_to_pm покрыт подписью initData. Само открытие Mini App не
    # означает, что бот вправе писать: без этого флага bot_linked не выдаём.
    may_notify = person.get("allows_write_to_pm") is True
    uid = users.upsert_on_login(
        conn,
        person["id"],
        username=str(person.get("username") or "")[:64] or None,
        first_name=str(person.get("first_name") or "")[:128] or None,
        link_bot=may_notify,
    )
    user = users.get_user(conn, uid)
    if not user or not user["is_active"]:
        request.session.clear()
        metrics.observe_auth(
            flow="miniapp", provider="telegram", result="banned",
        )
        raise HTTPException(403, "Доступ закрыт. Напиши в поддержку.")
    nxt = _safe_next(str(payload.get("next") or "")) or "/admin/"
    request.session.clear()
    request.session["user_id"] = uid
    request.session["csrf"] = secrets.token_urlsafe(16)
    request.session["telegram_miniapp"] = True
    metrics.observe_auth(flow="miniapp", provider="telegram", result="success")
    return JSONResponse({
        "status": "ok",
        "redirect": nxt,
        "notifications_enabled": bool(user["bot_linked"] or may_notify),
    })


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
        metrics.observe_auth(
            flow="deep_link", provider="telegram", result="rate_limited",
        )
        raise HTTPException(429, "Слишком много попыток входа. Подожди немного.")
    if not BOT_USERNAME:
        metrics.observe_auth(
            flow="deep_link", provider="telegram", result="provider_error",
        )
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
        metrics.observe_auth(
            flow="deep_link", provider="telegram", result="invalid",
        )
        return JSONResponse({"status": "forbidden"}, status_code=403)
    row = conn.execute(
        "SELECT * FROM login_codes WHERE code=?", (code,)).fetchone()
    if not row:
        _consume_auth_flow(request, code)
        metrics.observe_auth(
            flow="deep_link", provider="telegram", result="expired",
        )
        return JSONResponse({"status": "expired"})
    if row["created_at"] < _code_cutoff():
        conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
        conn.commit()
        _consume_auth_flow(request, code)
        metrics.observe_auth(
            flow="deep_link", provider="telegram", result="expired",
        )
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
            metrics.observe_auth(
                flow="deep_link", provider="telegram", result="conflict",
            )
            return JSONResponse({"status": "conflict"})
        if row["status"] != "confirmed":
            return JSONResponse({"status": "pending"})
        user = users.get_user(conn, row["user_id"])
        if not user or not user["is_active"]:
            conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
            conn.commit()
            _consume_auth_flow(request, code)
            metrics.observe_auth(
                flow="deep_link", provider="telegram", result="banned",
            )
            return JSONResponse({"status": "banned"})
        conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
        conn.commit()
        _consume_auth_flow(request, code)
        # user_id в сессии намеренно не меняем: Telegram только привязан.
        if flow_nxt and request.session.get("login_next") == flow_nxt:
            request.session.pop("login_next", None)
        metrics.observe_auth(
            flow="deep_link", provider="telegram", result="success",
        )
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
        metrics.observe_auth(
            flow="deep_link", provider="telegram", result="banned",
        )
        return JSONResponse({"status": "banned"})
    conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
    conn.commit()
    _consume_auth_flow(request, code)
    request.session["user_id"] = user["id"]
    request.session["csrf"] = secrets.token_urlsafe(16)
    if flow_nxt and request.session.get("login_next") == flow_nxt:
        request.session.pop("login_next", None)
    metrics.observe_auth(
        flow="deep_link", provider="telegram", result="success",
    )
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
    trusted_operator = 1 if telegram_id in users.OPERATOR_TG_IDS else 0
    try:
        conn.execute(
            "UPDATE users SET telegram_id=?, tg_username=?, "
            "is_operator=CASE WHEN ? THEN 1 ELSE is_operator END, "
            "is_suspicious=CASE WHEN ? THEN 0 ELSE is_suspicious END, "
            "bot_linked=1, last_login_at=? WHERE id=?",
            (telegram_id, username, trusted_operator, trusted_operator,
             now_iso(), user_id))
    except sqlite3.IntegrityError:
        # Между SELECT и UPDATE другой запрос мог успеть занять telegram_id.
        return False, "telegram_in_use"
    return True, None


def _private_chat_from_user(message: dict, telegram_id: int) -> bool:
    """True только для настоящего личного диалога пользователя с ботом."""
    chat = message.get("chat") or {}
    try:
        return chat.get("type") == "private" and int(chat.get("id")) == telegram_id
    except (TypeError, ValueError):
        return False


async def _welcome_from_bot(conn, person: dict, message: dict, *, write_granted=False):
    """Создаёт аккаунт из личного /start или подтверждения write access."""
    import notify
    try:
        telegram_id = int(person.get("id"))
    except (TypeError, ValueError):
        return
    if telegram_id <= 0:
        return
    if not _private_chat_from_user(message, telegram_id):
        return
    existing = users.get_by_telegram(conn, telegram_id)
    if existing and not existing["is_active"]:
        await asyncio.to_thread(
            notify.send_to, telegram_id,
            "Доступ к date4you закрыт. Если это ошибка, напишите в поддержку.",
        )
        return
    users.upsert_on_login(
        conn,
        telegram_id,
        username=str(person.get("username") or "")[:64] or None,
        first_name=str(person.get("first_name") or "")[:128] or None,
        link_bot=True,
    )
    if write_granted:
        text = notify.card(
            "Уведомления включены",
            "Теперь бот сможет сообщать о голосованиях, событиях и ответах.",
        )
    elif existing:
        text = notify.card(
            "С возвращением в date4you",
            "Откройте кабинет — вход произойдёт автоматически через Telegram.",
        )
    else:
        text = notify.card(
            "Добро пожаловать в date4you",
            "Аккаунт создан. Откройте кабинет — вход произойдёт автоматически.",
            "Типы уведомлений можно выбрать в настройках кабинета.",
        )
    await asyncio.to_thread(
        notify.send_to if write_granted else notify.send_video_to,
        telegram_id,
        *((text,) if write_granted else (START_VIDEO_PATH, text)),
        reply_markup=_miniapp_button(),
    )


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

    membership = (update or {}).get("my_chat_member") or {}
    if membership:
        chat = membership.get("chat") or {}
        status = ((membership.get("new_chat_member") or {}).get("status") or "")
        try:
            telegram_id = int(chat.get("id"))
        except (TypeError, ValueError):
            telegram_id = 0
        if telegram_id and chat.get("type") == "private":
            if status in {"kicked", "left"}:
                conn.execute(
                    "UPDATE users SET bot_linked=0 WHERE telegram_id=?",
                    (telegram_id,),
                )
            elif status in {"member", "administrator"}:
                conn.execute(
                    "UPDATE users SET bot_linked=1 WHERE telegram_id=?",
                    (telegram_id,),
                )
            conn.commit()
        return JSONResponse({"ok": True})

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
    try:
        tg_id = int(tg_id)
    except (TypeError, ValueError):
        tg_id = 0

    # Telegram присылает это service message после requestWriteAccess(). Это
    # надёжнее JS-callback: вебхук подписан секретом и доказывает право доставки.
    if tg_id and "write_access_allowed" in msg:
        await _welcome_from_bot(conn, frm, msg, write_granted=True)
        return JSONResponse({"ok": True})

    # Бот реагирует только на команду /start от настоящего пользователя.
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
    if not tg_id or command != "/start":
        return JSONResponse({"ok": True})
    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    cutoff = _code_cutoff()
    row = conn.execute(
        "SELECT * FROM login_codes WHERE code=? AND created_at>=?", (code, cutoff)
    ).fetchone() if code else None
    if not row and code in ("", "app"):
        # Обычный /start и зарезервированный вход с browser-link — полноценная
        # регистрация. Произвольный/просроченный login code сюда не попадает.
        await _welcome_from_bot(conn, frm, msg)
        return JSONResponse({"ok": True})
    if not row or row["status"] != "pending":
        if _private_chat_from_user(msg, tg_id):
            import notify
            await asyncio.to_thread(
                notify.send_to,
                tg_id,
                notify.card(
                    "Ссылка входа устарела",
                    "Вернись на сайт и нажми «Войти через Telegram» ещё раз.",
                    "Для отдельной новой регистрации отправь /start без кода.",
                ),
            )
        return JSONResponse({"ok": True})
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
        metrics.observe_auth(flow="oauth", provider=provider, result="invalid")
        raise HTTPException(404, "Неизвестный провайдер входа")
    client_id, _secret = OAUTH_PROVIDERS[provider]
    label = OAUTH_LABELS.get(provider, provider)
    if not client_id:
        metrics.observe_auth(
            flow="oauth", provider=provider, result="provider_error",
        )
        raise HTTPException(503, f"Вход через {label} ещё не настроен")

    ip = client_ip(request)
    if not rate_ok(f"oauth:{ip}", 20, 600):
        metrics.observe_auth(
            flow="oauth", provider=provider, result="rate_limited",
        )
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
            dependency = f"oauth_{provider}"
            with metrics.track_dependency(dependency, "token_exchange") as observation:
                tok = cli.post(meta["token"], data=data,
                               headers={"Accept": "application/json"})
                if tok.status_code != 200:
                    observation.fail()
            if tok.status_code != 200:
                log.warning(
                    "OAuth: обмен кода завершился ошибкой",
                    extra={"event": "oauth_dependency_failed", "provider": provider,
                           "operation": "token_exchange", "status_code": tok.status_code,
                           "status_class": f"{tok.status_code // 100}xx",
                           "outcome": "failure"},
                )
                raise HTTPException(502, "Провайдер не выдал токен. Попробуй ещё раз.")
            access = tok.json().get("access_token")
            if not access:
                raise HTTPException(502, "Провайдер не выдал токен. Попробуй ещё раз.")
            with metrics.track_dependency(dependency, "userinfo") as observation:
                ui = cli.get(meta["userinfo"],
                             headers={"Authorization": f"Bearer {access}",
                                      "Accept": "application/json"})
                if ui.status_code != 200:
                    observation.fail()
            if ui.status_code != 200:
                log.warning(
                    "OAuth: userinfo завершился ошибкой",
                    extra={"event": "oauth_dependency_failed", "provider": provider,
                           "operation": "userinfo", "status_code": ui.status_code,
                           "status_class": f"{ui.status_code // 100}xx",
                           "outcome": "failure"},
                )
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
        metrics.observe_auth(flow="oauth", provider=provider, result="invalid")
        raise HTTPException(404, "Неизвестный провайдер входа")
    if not OAUTH_PROVIDERS[provider][0]:
        metrics.observe_auth(
            flow="oauth", provider=provider, result="provider_error",
        )
        raise HTTPException(503, "Провайдер не настроен")

    if request.query_params.get("error"):
        metrics.observe_auth(flow="oauth", provider=provider, result="cancelled")
        return RedirectResponse("/login?msg=" +
                                quote("Вход отменён."), status_code=303)
    state = request.query_params.get("state")
    saved = request.session.pop("oauth_state", None)
    is_link = request.session.pop("oauth_link", False)
    request.session.pop("oauth_provider", None)
    if not state or not saved or not secrets.compare_digest(state, saved):
        metrics.observe_auth(flow="oauth", provider=provider, result="expired")
        raise HTTPException(403, "Проверка state не прошла. Начни вход заново.")
    code = request.query_params.get("code")
    if not code:
        metrics.observe_auth(flow="oauth", provider=provider, result="invalid")
        raise HTTPException(400, "Провайдер не вернул код авторизации.")

    try:
        ident = _oauth_fetch_identity(provider, code)
    except HTTPException:
        metrics.observe_auth(
            flow="oauth", provider=provider, result="provider_error",
        )
        raise

    # режим привязки: пользователь уже вошёл — привязываем соцсеть к его аккаунту
    if is_link and request.session.get("user_id"):
        uid = request.session["user_id"]
        ok = users.link_oauth_account(conn, uid, provider, ident["uid"],
                                      email=ident["email"])
        metrics.observe_auth(
            flow="oauth", provider=provider,
            result="success" if ok else "conflict",
        )
        msg = ("Аккаунт привязан ♥" if ok else
               "Эта соцсеть уже привязана к другому профилю.")
        return RedirectResponse("/admin/profile?msg=" + quote(msg), status_code=303)

    # обычный вход/регистрация
    uid = users.upsert_oauth_login(conn, provider, ident["uid"],
                                   display_name=ident["name"], email=ident["email"])
    user = users.get_user(conn, uid)
    if not user or not user["is_active"]:
        metrics.observe_auth(flow="oauth", provider=provider, result="banned")
        raise HTTPException(403, "Доступ закрыт. Напиши в поддержку.")
    request.session["user_id"] = uid
    request.session["csrf"] = secrets.token_urlsafe(16)
    metrics.observe_auth(flow="oauth", provider=provider, result="success")
    return RedirectResponse(_post_login_redirect(request), status_code=303)
