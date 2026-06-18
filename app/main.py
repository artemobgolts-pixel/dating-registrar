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
import secrets
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

import admin_routes
import auth_routes
import backup
import db
import notify
import operator_routes
import public_routes
import users
from config import COOKIE_SECURE, SECRET_KEY, SENTRY_DSN
from web import redir

# Реэкспорты: тестам и консоли удобно обращаться к main.<имя>,
# не зная внутреннюю раскладку по модулям.
import images                                              # noqa: F401
import places                                              # noqa: F401
from helpers import now_iso, now_naive                     # noqa: F401
from ratelimit import (_rates, client_ip,                  # noqa: F401
                       prune_rate_buckets, rates_gc_loop)
from tasks import autoarchive_loop, autoarchive_once, backup_loop  # noqa: F401

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
    try:
        autoarchive_once()
        backup.make_backup_if_stale(hours=20)
        auth_routes.setup_webhook()
    except Exception:
        log.exception("Ошибка при старте")
    # Чиним старые свидания, где ссылка на карты осела в поле place
    # (показывалась сырым URL и уходила в поиск Яндекса). В отдельном потоке —
    # внутри сетевые запросы к картам, не блокируем событийный цикл.
    async def _repair_places():
        try:
            n = await asyncio.to_thread(places.repair_legacy_places)
            if n:
                log.info("Починены ссылки-места у старых свиданий: распознано %d", n)
        except Exception:
            log.exception("Ошибка починки ссылок-мест")
    tasks = [asyncio.create_task(autoarchive_loop()),
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


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY,
                   session_cookie="__Host-admin_s" if COOKIE_SECURE else "admin_s",
                   max_age=60 * 60 * 24 * 30, same_site="lax", https_only=COOKIE_SECURE)


@app.middleware("http")
async def csp_headers(request: Request, call_next):
    """CSP c per-request nonce вместо script-src 'unsafe-inline'.

    Инлайновые СТИЛИ (style="...") остаются разрешёнными: на атрибуты
    nonce не распространяется, а риск инъекции стиля несравним со скриптом.
    """
    request.state.csp_nonce = secrets.token_urlsafe(16)
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{request.state.csp_nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    return resp

app.mount("/static", StaticFiles(directory="static"), name="static")
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
app.include_router(operator_routes.router)
