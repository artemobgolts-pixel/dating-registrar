"""date4you — точка входа приложения.

Здесь только сборка: приложение, middleware, обработчики ошибок,
фоновые задачи и подключение роутеров. Логика лежит в модулях:

    config.py        — переменные окружения (fail-fast при импорте)
    helpers.py       — время, форматирование, валидация форм
    guests.py        — cookie-токен гостя и его имя
    ratelimit.py     — анти-спам публичных ручек и троттлинг входа
    tasks.py         — авто-архив и авто-бэкап
    web.py           — шаблоны, get_db, безопасный redir
    public_routes.py — всё, что видят гости (/, /health, /c/<токен>/...)
    admin_routes.py  — вход и админка (/admin/...)
"""

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

import admin_routes
import auth_routes
import backup
import db
import notify
import operator_routes
import public_routes
import users
import voting_events
from config import COOKIE_SECURE, DOMAIN, SECRET_KEY, SENTRY_DSN
from web import redir

# Реэкспорты: тестам и консоли удобно обращаться к main.<имя>,
# не зная внутреннюю раскладку по модулям.
import images                                              # noqa: F401
import places                                              # noqa: F401
from helpers import now_iso, now_naive                     # noqa: F401
from ratelimit import (_rates, client_ip,                  # noqa: F401
                       prune_rate_buckets, rates_gc_loop)
from tasks import (autoarchive_loop, autoarchive_once, backup_loop,
                   notification_outbox_loop, voting_close_loop)     # noqa: F401

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# Мониторинг ошибок (опционально). Sentry подключается, только если задан
# SENTRY_DSN и установлен пакет sentry-sdk (`pip install sentry-sdk`). Без него
# работает минимальный рубеж: обработчик ниже логирует 500-е и шлёт алёрт в TG.
if SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.0,
                        send_default_pii=False)
        log.info("Sentry подключён")
    except Exception:
        log.warning("SENTRY_DSN задан, но sentry-sdk не установлен — "
                    "ошибки идут только в лог и в Telegram-алёрт")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Preserve the ballot snapshot first after downtime. Every valid event
    # starts after its poll deadline, so overdue polls must be resolved before
    # the corresponding dates are auto-archived.
    try:
        voting_events.close_due_once()
    except Exception:
        log.exception("Ошибка закрытия голосований при старте")
    # Независимые startup-шаги не должны каскадно отключать Telegram: например,
    # ошибка локального backup не мешает повторно зарегистрировать webhook/menu.
    for label, startup in (
        ("автоархивации", autoarchive_once),
        ("локального бэкапа", lambda: backup.make_backup_if_stale(hours=20)),
        ("определения Telegram username", auth_routes.resolve_bot_username),
        ("регистрации Telegram webhook", auth_routes.setup_webhook),
        ("регистрации Telegram Mini App menu", auth_routes.setup_miniapp_menu),
    ):
        try:
            startup()
        except Exception:
            log.exception("Ошибка %s при старте", label)
    # Чиним старые события, где ссылка на карты осела в поле place
    # (показывалась сырым URL и уходила в поиск Яндекса). В отдельном потоке —
    # внутри сетевые запросы к картам, не блокируем событийный цикл.
    async def _repair_places():
        try:
            n = await asyncio.to_thread(places.repair_legacy_places)
            if n:
                log.info("Починены ссылки-места у старых событий: распознано %d", n)
        except Exception:
            log.exception("Ошибка починки ссылок-мест")
    tasks = [asyncio.create_task(notification_outbox_loop()),
             asyncio.create_task(voting_close_loop()),
             asyncio.create_task(autoarchive_loop()),
             asyncio.create_task(backup_loop()),
             asyncio.create_task(rates_gc_loop()),
             asyncio.create_task(_repair_places())]
    yield
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass


def session_same_site(cookie_secure: bool) -> str:
    """Web Telegram embeds Mini Apps cross-site; its session needs None+Secure."""
    return "none" if cookie_secure else "lax"


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY,
                   session_cookie="__Host-admin_s" if COOKIE_SECURE else "admin_s",
                   max_age=60 * 60 * 24 * 30,
                   # web.telegram.org может открыть Mini App cross-site iframe.
                   # None допустим только вместе с Secure; локальный HTTP — Lax.
                   same_site=session_same_site(COOKIE_SECURE),
                   https_only=COOKIE_SECURE)
_domain_host = urlsplit(f"//{DOMAIN}").hostname or DOMAIN.partition(":")[0]
_trusted_hosts = {_domain_host, "localhost", "127.0.0.1", "testserver"}
if _domain_host and not _domain_host.startswith("www."):
    _trusted_hosts.add(f"www.{_domain_host}")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(_trusted_hosts),
                   www_redirect=False)


class CachedStatic(StaticFiles):
    """StaticFiles + Cache-Control. Файлы с версией-хэшем в query (?v=…, их даёт
    web.asset) контентно-адресуемы — кэшируем их навсегда (immutable, год). Без
    версии: шрифты/иконки/картинки — надолго, CSS/JS — на час с ревалидацией."""

    LONG = {".woff2", ".woff", ".ttf", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".svg"}

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        ext = os.path.splitext(path)[1].lower()
        if resp.status_code == 200:
            qs = scope.get("query_string", b"")
            if b"v=" in qs:
                # имя версионировано по содержимому → можно кэшировать максимально
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            elif ext in self.LONG:
                resp.headers["Cache-Control"] = "public, max-age=2592000"   # 30 дней
            else:
                resp.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
        return resp


@app.middleware("http")
async def csp_headers(request: Request, call_next):
    """CSP c per-request nonce вместо script-src 'unsafe-inline'.

    Инлайновые СТИЛИ (style="...") остаются разрешёнными: на атрибуты
    nonce не распространяется, а риск инъекции стиля несравним со скриптом.
    """
    request.state.csp_nonce = secrets.token_urlsafe(16)
    p = request.url.path
    resp = await call_next(request)
    # Telegram Web/Desktop может держать Mini App во frame/webview. Разрешение
    # выдаётся только boot-route и сессии, которая успешно прошла initData auth.
    # TrustedHost может отклонить запрос раньше SessionMiddleware. В таком
    # ответе ``request.session`` недоступен, поэтому читаем уже созданный scope
    # без AssertionError и сохраняем обычную защиту от framing.
    miniapp = p == "/tg/app" or bool(
        request.scope.get("session", {}).get("telegram_miniapp")
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    if not miniapp:
        resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # Старый браузерный XSS-фильтр сам создавал обходы; современная защита — CSP.
    resp.headers["X-XSS-Protection"] = "0"
    if COOKIE_SECURE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if resp.headers.get("content-type", "").startswith("text/html"):
        # В HTML есть персональные данные и секретные токены гостевых ссылок:
        # браузер и промежуточные кэши не должны сохранять такие страницы.
        resp.headers["Cache-Control"] = "no-store"
        # Telegram Login Widget (внешний скрипт + iframe oauth.telegram.org)
        # грузится на странице входа /login И на гостевых ссылках /c/<токен>
        # и /d/<токен> (вход-модалка прямо со страницы подборки/события).
        # Послабление CSP — ровно на этих HTML-страницах, не на всём сайте.
        if miniapp:
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{request.state.csp_nonce}' "
                "https://telegram.org https://oauth.telegram.org; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob: https://t.me; font-src 'self'; "
                "connect-src 'self'; frame-src https://oauth.telegram.org; "
                "frame-ancestors https://telegram.org https://*.telegram.org; "
                "base-uri 'self'; form-action 'self'")
        elif p == "/login" or p.startswith("/c/") or p.startswith("/d/"):
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{request.state.csp_nonce}' "
                "https://telegram.org https://oauth.telegram.org; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob: https://t.me; font-src 'self'; "
                "connect-src 'self'; "
                "frame-src https://oauth.telegram.org; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        else:
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{request.state.csp_nonce}'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    return resp

app.mount("/static", CachedStatic(directory="static"), name="static")
# /uploads наружу не монтируется: фото отдаются только через
# /c/<токен>/image/<файл> с проверкой категории и /admin/uploads/<файл> для админа.


# Дружелюбные ошибки: POST-формы админки при ошибке (неверная дата, лишние
# фото, просроченный CSRF...) получают flash-сообщение вместо JSON-простыни.
@app.exception_handler(StarletteHTTPException)
async def friendly_http_exc(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith(("/admin", "/operator")) and request.method == "POST" \
            and not isinstance(exc.detail, dict):
        sp = urlsplit(request.headers.get("referer", ""))
        if sp.path.startswith(("/admin", "/operator")):
            q = [(k, v) for k, v in parse_qsl(sp.query) if k != "msg"]
            back = sp.path + (f"?{urlencode(q)}" if q else "")
        else:
            back = "/admin/"
        return redir(back, f"⚠ {exc.detail}")
    return await http_exception_handler(request, exc)


@app.exception_handler(users.NeedLogin)
def _need_login(request: Request, exc: users.NeedLogin):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Последний рубеж: необработанное исключение = 500. Логируем с трейсом и
    шлём алёрт оператору в Telegram (в потоке — httpx.post блокирующий).

    Не перехватывает HTTPException/NeedLogin: для них есть свои обработчики.
    Если подключён Sentry, исключение уже ушло туда через его интеграцию.
    """
    log.exception("Необработанная ошибка на %s %s", request.method, request.url.path)
    try:
        msg = (f"🔥 500 на <code>{request.method} {request.url.path}</code>\n"
               f"<b>{type(exc).__name__}</b>: {notify.esc(str(exc)[:300])}")
        asyncio.create_task(asyncio.to_thread(notify.alert, msg))
    except Exception:
        log.exception("Не удалось отправить алёрт о сбое")
    return PlainTextResponse("Что-то пошло не так. Мы уже разбираемся.",
                             status_code=500)


app.include_router(public_routes.router)
app.include_router(auth_routes.router)
app.include_router(admin_routes.router)
app.include_router(admin_routes.user_router)
app.include_router(operator_routes.router)
