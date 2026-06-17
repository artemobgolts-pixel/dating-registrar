"""Пользователи: вход через Telegram, апсерт, роли, зависимость current_user.

Вход — без пароля: бот подтверждает одноразовый код (см. auth_routes), а здесь
по telegram_id заводится/обновляется запись users и кладётся в сессию user_id.
"""

import secrets

from fastapi import Depends, HTTPException, Request

from config import OPERATOR_TG_IDS
from helpers import now_iso
from web import get_db


class NeedLogin(Exception):
    """Нет валидной сессии — middleware/обработчик редиректит на /login."""


def get_user(conn, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_by_telegram(conn, telegram_id: int):
    return conn.execute(
        "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()


def upsert_on_login(conn, telegram_id: int, *, username: str | None = None,
                    first_name: str | None = None) -> int:
    """Заводит/обновляет пользователя после подтверждения кода ботом. Возвращает id.

    «Забор» легаси-владельца: если входящий telegram_id — оператор, а реальной
    записи под ним ещё нет, но есть служебный легаси-владелец (telegram_id=0),
    переписываем ему telegram_id — и все легаси-данные становятся его данными.
    """
    is_op = telegram_id in OPERATOR_TG_IDS
    row = get_by_telegram(conn, telegram_id)
    if not row and is_op:
        legacy = get_by_telegram(conn, 0)
        if legacy:
            conn.execute(
                "UPDATE users SET telegram_id=?, tg_username=?, is_operator=1, "
                "last_login_at=? WHERE id=?",
                (telegram_id, username, now_iso(), legacy["id"]))
            conn.commit()
            return legacy["id"]

    if row:
        conn.execute(
            "UPDATE users SET tg_username=?, is_operator=?, last_login_at=? WHERE id=?",
            (username, 1 if (is_op or row["is_operator"]) else 0, now_iso(), row["id"]))
        conn.commit()
        return row["id"]

    cur = conn.execute(
        "INSERT INTO users(telegram_id, tg_username, display_name, is_operator, "
        "created_at, last_login_at) VALUES(?,?,?,?,?,?)",
        (telegram_id, username, first_name or username, 1 if is_op else 0,
         now_iso(), now_iso()))
    conn.commit()
    return cur.lastrowid


async def current_user(request: Request, conn=Depends(get_db)):
    """Зависимость кабинета: валидная сессия + активный юзер + CSRF на POST.

    Возвращает строку users. 404-объектов отсюда не бывает — только NeedLogin
    (нет/протухла сессия, забанен) и 403 (CSRF). Заменяет прежний require_admin.
    """
    uid = request.session.get("user_id")
    if not uid:
        raise NeedLogin()
    user = get_user(conn, uid)
    if not user or not user["is_active"]:
        request.session.clear()
        raise NeedLogin()
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(16)
    if request.method == "POST":
        form = await request.form()
        token = str(form.get("csrf") or "")
        good = request.session.get("csrf") or ""
        if not (token and secrets.compare_digest(token, good)):
            raise HTTPException(403, "Сессия устарела — обнови страницу и попробуй ещё раз")
    request.state.user = user
    return user
