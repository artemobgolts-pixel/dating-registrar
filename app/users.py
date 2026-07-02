"""Пользователи: вход через Telegram, апсерт, роли, зависимость current_user.

Вход — без пароля: бот подтверждает одноразовый код (см. auth_routes), а здесь
по telegram_id заводится/обновляется запись users и кладётся в сессию user_id.
"""

import secrets

from fastapi import Depends, HTTPException, Request

import settings as app_settings
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
                    first_name: str | None = None, link_bot: bool = False) -> int:
    """Заводит/обновляет пользователя после входа. Возвращает id.

    link_bot=True (вход через бота, deeplink) — выставляет bot_linked=1: бот
    запущен, уведомления можно слать. link_bot=False (вход через виджет) — флаг
    не трогает: у новых остаётся 0, пока не подключат бота отдельно.
    """
    is_op = telegram_id in OPERATOR_TG_IDS
    row = get_by_telegram(conn, telegram_id)

    if row:
        conn.execute(
            "UPDATE users SET tg_username=?, is_operator=?, "
            "bot_linked=CASE WHEN ? THEN 1 ELSE bot_linked END, "
            "last_login_at=? WHERE id=?",
            (username, 1 if (is_op or row["is_operator"]) else 0,
             1 if link_bot else 0, now_iso(), row["id"]))
        conn.commit()
        return row["id"]

    cur = conn.execute(
        "INSERT INTO users(telegram_id, tg_username, display_name, is_operator, "
        "is_reviewed, bot_linked, created_at, last_login_at) VALUES(?,?,?,?,?,?,?,?)",
        (telegram_id, username, first_name or username, 1 if is_op else 0,
         # операторы — всегда одобрены; остальным is_reviewed=0, только если
         # включена модерация пользователей (мягкая очередь у админа).
         0 if (not is_op and app_settings.is_on(conn, app_settings.MODERATE_USERS)) else 1,
         1 if link_bot else 0, now_iso(), now_iso()))
    conn.commit()
    return cur.lastrowid


def upsert_oauth_login(conn, provider: str, provider_uid: str, *,
                       display_name: str | None = None,
                       email: str | None = None) -> int:
    """Вход/регистрация через OAuth. Возвращает user_id.

    Если привязка (provider, provider_uid) уже есть — логиним её пользователя.
    Иначе заводим НОВЫЙ аккаунт без telegram_id (standalone-OAuth) и создаём
    привязку. Имя берём из профиля провайдера. Роль оператора OAuth не выдаёт
    (операторы — только через OPERATOR_TG_IDS)."""
    link = conn.execute(
        "SELECT user_id FROM oauth_accounts WHERE provider=? AND provider_uid=?",
        (provider, provider_uid)).fetchone()
    if link:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?",
                     (now_iso(), link["user_id"]))
        conn.commit()
        return link["user_id"]

    reviewed = 0 if app_settings.is_on(conn, app_settings.MODERATE_USERS) else 1
    cur = conn.execute(
        "INSERT INTO users(telegram_id, display_name, is_reviewed, created_at, "
        "last_login_at) VALUES(NULL,?,?,?,?)",
        (display_name or f"{provider}-{provider_uid[:6]}", reviewed,
         now_iso(), now_iso()))
    uid = cur.lastrowid
    conn.execute(
        "INSERT INTO oauth_accounts(provider, provider_uid, user_id, email, created_at) "
        "VALUES(?,?,?,?,?)", (provider, provider_uid, uid, email, now_iso()))
    conn.commit()
    return uid


def link_oauth_account(conn, user_id: int, provider: str, provider_uid: str,
                       email: str | None = None) -> bool:
    """Привязать OAuth-аккаунт к существующему пользователю (из профиля).
    False, если эта соцсеть уже привязана к ДРУГОМУ аккаунту."""
    existing = conn.execute(
        "SELECT user_id FROM oauth_accounts WHERE provider=? AND provider_uid=?",
        (provider, provider_uid)).fetchone()
    if existing:
        return existing["user_id"] == user_id
    conn.execute(
        "INSERT INTO oauth_accounts(provider, provider_uid, user_id, email, created_at) "
        "VALUES(?,?,?,?,?)", (provider, provider_uid, user_id, email, now_iso()))
    conn.commit()
    return True


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
    # env — источник правды для роли оператора: если telegram_id попал в
    # OPERATOR_TG_IDS уже после входа, выдаём роль на лету (без перелогина).
    # Не снимаем роль у назначенных через операторскую панель — только выдаём.
    if (user["telegram_id"] in OPERATOR_TG_IDS and not user["is_operator"]
            and user["telegram_id"] != 0):
        conn.execute("UPDATE users SET is_operator=1 WHERE id=?", (uid,))
        conn.commit()
        user = get_user(conn, uid)
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


async def current_operator(request: Request, conn=Depends(get_db)):
    """Зависимость операторской админки: всё из current_user + роль is_operator.

    Не-оператор (или аноним) не должен даже знать, что /operator существует,
    поэтому отдаём 404, а не 403. Аноним по-прежнему уходит на /login (NeedLogin).
    """
    user = await current_user(request, conn)
    if not user["is_operator"]:
        raise HTTPException(404)
    return user
