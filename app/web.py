"""Общая веб-инфраструктура: шаблоны, подключение к базе, редиректы."""

import os
import hashlib
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import db
from config import BASE_URL, SUPPORT_URL, VPN_URL
from helpers import (fmt_host, fmt_short, fmt_ts, fmt_when, fmt_ymaps,
                     pay_label, placename, plural, rich)

_STATIC_DIR = "static"
_ASSET_VER: dict[str, tuple[float, str]] = {}    # name → (mtime, content-hash)


def asset(name: str) -> str:
    """Ссылка на статику с версией по ХЭШУ СОДЕРЖИМОГО: /static/admin.css?v=<hash>.

    Версия меняется ровно тогда, когда меняется содержимое файла (а не время
    сборки/коммита, как у mtime) — поэтому после деплоя без правок файла ссылка
    не меняется и кэш браузера переиспользуется. При наличии ?v=<hash> статика
    отдаётся с immutable-кэшем на год (см. CachedStatic). Хэш считаем один раз и
    кэшируем в процессе, пересчитывая только при изменении mtime файла.
    """
    path = os.path.join(_STATIC_DIR, name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return f"/static/{name}"
    cached = _ASSET_VER.get(name)
    if cached and cached[0] == mtime:
        return f"/static/{name}?v={cached[1]}"
    try:
        with open(path, "rb") as f:
            digest = hashlib.sha1(f.read()).hexdigest()[:12]
    except OSError:
        return f"/static/{name}"
    _ASSET_VER[name] = (mtime, digest)
    return f"/static/{name}?v={digest}"


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
