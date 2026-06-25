"""Общая веб-инфраструктура: шаблоны, подключение к базе, редиректы."""

import os
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import db
from config import BASE_URL, SUPPORT_URL, VPN_URL
from helpers import (fmt_host, fmt_short, fmt_ts, fmt_when, fmt_ymaps,
                     pay_label, placename, plural, rich)

_STATIC_DIR = "static"


def asset(name: str) -> str:
    """Ссылка на статику с версией по mtime файла: /static/admin.css?v=<mtime>.

    Статика отдаётся с Cache-Control: max-age=3600 без версионирования имён, из-за
    чего правки CSS/JS доезжали до пользователя только через час (или после
    жёсткой перезагрузки). Версия в query меняется при каждом изменении файла и
    автоматически инвалидирует кэш — на деплое новые стили применяются сразу.
    """
    try:
        ver = int(os.path.getmtime(os.path.join(_STATIC_DIR, name)))
    except OSError:
        ver = 0
    return f"/static/{name}?v={ver}"


def _template_globals(request: Request) -> dict:
    return {"csp_nonce": getattr(request.state, "csp_nonce", "")}


templates = Jinja2Templates(directory="templates",
                            context_processors=[_template_globals])
templates.env.globals["asset"] = asset
templates.env.filters["when"] = fmt_when
templates.env.filters["ts"] = fmt_ts
templates.env.filters["short"] = fmt_short
templates.env.filters["host"] = fmt_host
templates.env.filters["ymaps"] = fmt_ymaps
templates.env.filters["placename"] = placename
templates.env.filters["rich"] = rich
templates.env.filters["plural"] = plural
templates.env.filters["pay_label"] = pay_label
templates.env.globals["pay_label"] = pay_label
templates.env.globals["BASE_URL"] = BASE_URL
templates.env.globals["SUPPORT_URL"] = SUPPORT_URL
templates.env.globals["VPN_URL"] = VPN_URL


def get_db():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def redir(url: str, msg: str | None = None) -> RedirectResponse:
    """303-редирект с flash-сообщением.

    Принимает только локальные пути: значение часто приходит из поля next
    формы, и без проверки это классический open redirect. «//evil.com» —
    протокол-относительный URL, тоже наружу.
    """
    if not url.startswith("/") or url.startswith("//"):
        url = "/admin/"
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)
