"""Гости: cookie-токен и имя."""

from fastapi import HTTPException, Request

from config import COOKIE_SECURE, GUEST_COOKIE, LEGACY_GUEST_COOKIE
from helpers import fmt_short


def get_guest(request: Request) -> str | None:
    return request.cookies.get(GUEST_COOKIE) or request.cookies.get(LEGACY_GUEST_COOKIE)


def set_guest_cookie(resp, token: str) -> None:
    resp.set_cookie(GUEST_COOKIE, token, max_age=60 * 60 * 24 * 365,
                    httponly=True, samesite="lax", secure=COOKIE_SECURE)


def get_guest_name(conn, guest: str | None) -> str | None:
    if not guest:
        return None
    row = conn.execute("SELECT name FROM guests WHERE token=?", (guest,)).fetchone()
    return row["name"] if row else None


def require_name(conn, guest: str | None) -> str:
    """Любое действие гостя требует имени — иначе фронт покажет диалог «представься»."""
    name = get_guest_name(conn, guest)
    if not name:
        raise HTTPException(412, {"need_name": True,
                                  "msg": "Сначала скажи, как тебя зовут ♥"})
    return name


def gname(conn, guest: str | None) -> str:
    """Имя для админки и Telegram (с фолбэком на короткий токен)."""
    return get_guest_name(conn, guest) or f"Человек #{fmt_short(guest)}"
