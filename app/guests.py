"""Чтение legacy guest cookie и имён для старых голосов и их переноса в аккаунт."""

from fastapi import Request

from config import GUEST_COOKIE, LEGACY_GUEST_COOKIE
from helpers import fmt_short


def get_guest(request: Request) -> str | None:
    return request.cookies.get(GUEST_COOKIE) or request.cookies.get(LEGACY_GUEST_COOKIE)


def get_guest_name(conn, guest: str | None) -> str | None:
    if not guest:
        return None
    row = conn.execute("SELECT name FROM guests WHERE token=?", (guest,)).fetchone()
    return row["name"] if row else None


def gname(conn, guest: str | None) -> str:
    """Имя для админки и Telegram (с фолбэком на короткий токен)."""
    return get_guest_name(conn, guest) or f"Человек #{fmt_short(guest)}"
