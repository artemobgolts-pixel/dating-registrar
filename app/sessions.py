"""Подписанная cookie содержит случайный ключ отзываемой серверной сессии."""

import hashlib
import re
import secrets
import time

from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

import db


SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def _id_hash(session_id: object) -> str | None:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return None
    return hashlib.sha256(session_id.encode("ascii")).hexdigest()


def _delete_current(conn, session: dict) -> None:
    digest = _id_hash(session.get("session_id"))
    if digest is not None:
        conn.execute("DELETE FROM auth_sessions WHERE id_hash=?", (digest,))


def issue_session(request, conn, user_id: int) -> None:
    """Ротирует текущую сессию, сохраняя login_next и незавершённые auth flows.

    Срок абсолютный: повторная подпись cookie при чтении не продлевает запись.
    В БД хранится только SHA-256 случайного токена, а не его cookie-значение.
    """
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    _delete_current(conn, request.session)
    conn.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
    conn.execute(
        "INSERT INTO auth_sessions(id_hash,user_id,expires_at) VALUES(?,?,?)",
        (_id_hash(token), int(user_id), now + SESSION_TTL_SECONDS),
    )
    conn.commit()
    request.session["user_id"] = int(user_id)
    request.session["session_id"] = token
    request.session["csrf"] = secrets.token_urlsafe(16)


def revoke_session(request, conn) -> None:
    """Отзывает текущий токен до очистки cookie, включая скопированный cookie."""
    _delete_current(conn, request.session)
    conn.commit()
    request.session.clear()


def revoke_user_sessions(conn, user_id: int) -> int:
    """Явный путь «выйти на всех устройствах» для деактивации/восстановления."""
    changed = conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
    conn.commit()
    return changed.rowcount


def _session_valid(user_id: object, session_id: object) -> bool:
    digest = _id_hash(session_id)
    if type(user_id) is not int or user_id <= 0 or digest is None:
        return False
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT 1 FROM auth_sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.id_hash=? AND s.user_id=? AND s.expires_at>? AND u.is_active=1",
            (digest, user_id, int(time.time())),
        ).fetchone() is not None
    finally:
        conn.close()


class SessionRevocationMiddleware:
    """Проверяет сессию до любого потребителя user_id, в том числе public routes.

    Должен находиться внутри SessionMiddleware: подпись cookie проверяется первой.
    Старые cookie без registry-токена требуют нового входа; регистрация по replay
    запрещена. Anonymous login/OAuth state без user_id остаётся доступным.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            session = scope["session"]
            if "user_id" in session or "session_id" in session:
                valid = await run_in_threadpool(
                    _session_valid, session.get("user_id"), session.get("session_id"),
                )
                if not valid:
                    session.clear()
        await self.app(scope, receive, send)
