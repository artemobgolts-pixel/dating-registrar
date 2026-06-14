"""Общая веб-инфраструктура: шаблоны, подключение к базе, редиректы."""

from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import db
from config import BASE_URL
from helpers import (fmt_host, fmt_short, fmt_ts, fmt_when, fmt_ymaps,
                     placename, plural, rich)


def _template_globals(request: Request) -> dict:
    return {"csp_nonce": getattr(request.state, "csp_nonce", "")}


templates = Jinja2Templates(directory="templates",
                            context_processors=[_template_globals])
templates.env.filters["when"] = fmt_when
templates.env.filters["ts"] = fmt_ts
templates.env.filters["short"] = fmt_short
templates.env.filters["host"] = fmt_host
templates.env.filters["ymaps"] = fmt_ymaps
templates.env.filters["placename"] = placename
templates.env.filters["rich"] = rich
templates.env.filters["plural"] = plural
templates.env.globals["BASE_URL"] = BASE_URL


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
