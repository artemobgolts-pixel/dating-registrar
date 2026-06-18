"""Вход через Telegram-бота (без пароля).

Поток:
  1. /login — страница с кнопкой «Войти через Telegram».
  2. POST /auth/start — сервер генерит одноразовый код, кладёт в login_codes
     (status=pending) и отдаёт deep-link https://t.me/<бот>?start=<код>.
  3. Человек жмёт Start в боте → Telegram шлёт боту апдейт на /tg/webhook
     (с секретным заголовком). Обработчик помечает код confirmed и запоминает,
     какой telegram_id его подтвердил.
  4. Страница /login поллит GET /auth/poll?code=… — как только код confirmed,
     заводим/обновляем пользователя, кладём user_id в сессию и редиректим в кабинет.

Коды живут TTL_SECONDS; протухшие чистятся лениво при обращении.
"""

import hashlib
import hmac
import logging
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import users
from config import BASE_URL, TG_BOT_USERNAME, TG_WEBHOOK_SECRET
from helpers import now_iso, now_naive
from datetime import timedelta
from ratelimit import client_ip, rate_ok
from web import get_db, templates

log = logging.getLogger("auth")
router = APIRouter()

TTL_SECONDS = 600          # код входа живёт 10 минут


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
                  "allowed_updates": ["message"]},
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
    cutoff = (now_naive() - timedelta(seconds=TTL_SECONDS)).isoformat(sep="T")
    conn.execute("DELETE FROM login_codes WHERE created_at < ?", (cutoff,))
    conn.commit()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, conn=Depends(get_db)):
    if request.session.get("user_id") and users.get_user(conn, request.session["user_id"]):
        return RedirectResponse("/admin/", status_code=303)
    return templates.TemplateResponse(
        request, "auth/login.html", {"bot": TG_BOT_USERNAME})


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
    pairs = sorted(f"{k}={v}" for k, v in data.items() if k != "hash")
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
    tg_id = int(data["id"])
    uid = users.upsert_on_login(conn, tg_id, username=data.get("username"),
                                first_name=data.get("first_name"), link_bot=False)
    user = users.get_user(conn, uid)
    if not user["is_active"]:
        raise HTTPException(403, "Доступ закрыт. Напиши в поддержку.")
    request.session["user_id"] = uid
    request.session["csrf"] = secrets.token_urlsafe(16)
    return RedirectResponse("/admin/", status_code=303)


@router.post("/auth/start")
def auth_start(request: Request, conn=Depends(get_db)):
    """Создаёт одноразовый код и возвращает deep-link на бота."""
    ip = client_ip(request)
    # анти-спам: не больше 10 кодов с одного IP за 10 минут
    if not rate_ok(f"authstart:{ip}", 10, 600):
        raise HTTPException(429, "Слишком много попыток входа. Подожди немного.")
    if not TG_BOT_USERNAME:
        raise HTTPException(503, "Вход через Telegram не настроен (TG_BOT_USERNAME).")
    _gc_codes(conn)
    code = secrets.token_urlsafe(12)
    conn.execute(
        "INSERT INTO login_codes(code, status, created_at) VALUES(?, 'pending', ?)",
        (code, now_iso()))
    conn.commit()
    return JSONResponse({"code": code,
                         "url": f"https://t.me/{TG_BOT_USERNAME}?start={code}"})


@router.get("/auth/poll")
def auth_poll(request: Request, code: str, conn=Depends(get_db)):
    """Поллинг страницей входа. Как только код confirmed — логиним и редиректим.

    Вебхук (отдельный запрос от Telegram, без сессии браузера) проставил коду
    telegram_id. Здесь по нему резолвим пользователя и кладём user_id в ЭТУ сессию.
    """
    row = conn.execute(
        "SELECT * FROM login_codes WHERE code=?", (code,)).fetchone()
    if not row:
        return JSONResponse({"status": "expired"})
    if row["status"] != "confirmed" or not row["telegram_id"]:
        return JSONResponse({"status": "pending"})
    user = users.get_by_telegram(conn, row["telegram_id"])
    if not user:
        return JSONResponse({"status": "pending"})
    if not user["is_active"]:
        conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
        conn.commit()
        return JSONResponse({"status": "banned"})
    conn.execute("DELETE FROM login_codes WHERE code=?", (code,))
    conn.commit()
    request.session["user_id"] = user["id"]
    request.session["csrf"] = secrets.token_urlsafe(16)
    return JSONResponse({"status": "ok", "redirect": "/admin/"})


@router.post("/tg/webhook")
async def tg_webhook(request: Request, conn=Depends(get_db)):
    """Приём апдейтов от Telegram. Интересует только /start <код> в личке.

    Защита: секретный заголовок X-Telegram-Bot-Api-Secret-Token (его знает
    только Telegram, которому мы сами отдали секрет при setWebhook). Без
    совпадения — 403, иначе кто угодно подтвердит чужой код.
    """
    hdr = request.headers.get("x-telegram-bot-api-secret-token") or ""
    if not TG_WEBHOOK_SECRET or not secrets.compare_digest(hdr, TG_WEBHOOK_SECRET):
        raise HTTPException(403, "forbidden")
    update = await request.json()
    msg = (update or {}).get("message") or {}
    text = (msg.get("text") or "").strip()
    frm = msg.get("from") or {}
    tg_id = frm.get("id")
    # Бот реагирует только на «/start <код>» от настоящего пользователя.
    if not tg_id or not text.startswith("/start"):
        return JSONResponse({"ok": True})
    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    row = conn.execute("SELECT * FROM login_codes WHERE code=?", (code,)).fetchone() \
        if code else None
    if not row or row["status"] == "confirmed":
        return JSONResponse({"ok": True})
    # Заводим/обновляем пользователя и помечаем код подтверждённым.
    users.upsert_on_login(conn, int(tg_id),
                          username=frm.get("username"),
                          first_name=frm.get("first_name"),
                          link_bot=True)
    conn.execute("UPDATE login_codes SET status='confirmed', telegram_id=? WHERE code=?",
                 (int(tg_id), code))
    conn.commit()
    # Смягчение фишинга/session-fixation: подтвердивший видит, КУДА и КАК он
    # входит. Если он не начинал вход на сайте — это сигнал, что кто-то пытается
    # войти под ним (попросил нажать Start по чужой ссылке). Сообщение шлём тому,
    # кто нажал Start (его chat_id == tg_id в личке), а не оператору.
    import notify
    notify.send_to(
        int(tg_id),
        "🔓 Вы подтвердили вход в кабинет <b>" + notify.esc(BASE_URL) + "</b>.\n\n"
        "Если вы <b>не</b> открывали страницу входа сами — <b>не закрывайте это "
        "и никому не пересылайте</b>: кто-то мог попросить вас нажать Start, "
        "чтобы войти под вашим именем. Просто проигнорируйте — без вашей страницы "
        "входа сессия не откроется.")
    return JSONResponse({"ok": True})
