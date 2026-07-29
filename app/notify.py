"""Уведомления админу в Telegram. Если токен/чат не заданы — тихо ничего не делает."""

import html
import logging
import os
import time

import httpx

log = logging.getLogger("notify")

TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
CHAT = os.getenv("TG_CHAT_ID", "").strip()


def esc(s: str | None) -> str:
    return html.escape(s or "")


def notify(text: str) -> None:
    """Отправляет сообщение боту. Вызывается из BackgroundTasks — ответ юзеру не ждёт."""
    if not TOKEN or not CHAT:
        return
    send_to(CHAT, text)


def owner_chat_id(conn, owner_id: int) -> int | None:
    """telegram_id владельца, КОМУ можно слать уведомления о действиях с его
    событием. None — если бот не подключён (вошёл только через виджет), забанен
    или это служебный легаси-владелец (telegram_id=0). Резолвить ОБЯЗАТЕЛЬНО
    внутри запроса: в BackgroundTask соединение уже закрыто."""
    row = conn.execute(
        "SELECT telegram_id FROM users "
        "WHERE id=? AND bot_linked=1 AND is_active=1 AND telegram_id<>0",
        (owner_id,)).fetchone()
    return row["telegram_id"] if row else None


# user_chat_id — то же правило, что и owner_chat_id (бот подключён, активен,
# не легаси). Отдельное имя для читаемости в местах «уведомить автора действия».
user_chat_id = owner_chat_id


def card(title: str, *lines: str) -> str:
    """Собирает аккуратное многострочное уведомление: жирный заголовок,
    затем строки (пустые/None пропускаются). Текст внутри строк должен быть
    уже экранирован через esc(), где это нужно."""
    body = "\n".join(l for l in lines if l)
    head = f"<b>{title}</b>"
    return f"{head}\n{body}" if body else head


def send_to(chat_id: int | str, text: str, *, reply_markup: dict | None = None) -> bool:
    """Шлёт сообщение конкретному chat_id (напр. тому, кто подтвердил вход).

    В отличие от notify() не требует CHAT — нужен только TOKEN. Ошибки не валят
    вызвавшего: вход не должен падать из-за недоставленного предупреждения.
    Возвращает True только после успешного ответа Telegram — очередь использует
    результат, чтобы не помечать недоставленное сообщение отправленным.
    """
    if not TOKEN:
        return False
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = httpx.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        if r.status_code >= 400:
            log.warning("Telegram API %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("Не удалось отправить сообщение в Telegram: %s", e)
        return False


def answer_callback(query_id: str, text: str = "") -> bool:
    """Закрывает индикатор inline-кнопки Telegram; ошибка не ломает вход."""
    if not TOKEN or not query_id:
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
            json={"callback_query_id": query_id, "text": text[:200]},
            timeout=10,
        )
        return r.status_code < 400
    except Exception as e:
        log.warning("Не удалось ответить на callback Telegram: %s", e)
        return False


def send_document(chat_id: int | str, path, caption: str | None = None,
                  filename: str | None = None) -> bool:
    """Шлёт файл документом конкретному chat_id (напр. снимок базы для бэкапа).

    Как и send_to, требует только TOKEN (не CHAT). Ошибки не валят вызвавшего:
    недоставленный бэкап логируем, но фоновую задачу не роняем. Возвращает True
    при успешной отправке. Блокирующий httpx — звать из потока, не из event loop.
    """
    if not TOKEN:
        return False
    path = str(path)
    try:
        with open(path, "rb") as f:
            data: dict[str, str] = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            r = httpx.post(
                f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                data=data,
                files={"document": (filename or os.path.basename(path), f)},
                timeout=60,
            )
        if r.status_code >= 400:
            log.warning("Telegram sendDocument %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("Не удалось отправить документ в Telegram: %s", e)
        return False


# Алёрты о сбоях (500-е) оператору. Дедупликация по тексту, чтобы всплеск
# одинаковых ошибок не превратился в флуд: одинаковый алёрт — не чаще раза в окно.
_ALERT_WINDOW = 300          # секунд
_alert_seen: dict[str, float] = {}


def alert(text: str) -> None:
    """Шлёт алёрт о сбое (тот же бот/чат, что и уведомления), с троттлингом.

    Блокирующий httpx.post — вызывать из потока/боновой задачи, не из event loop.
    """
    if not TOKEN or not CHAT:
        return
    now = time.monotonic()
    # чистим протухшие записи окна и проверяем дедуп
    for k, t in list(_alert_seen.items()):
        if now - t > _ALERT_WINDOW:
            _alert_seen.pop(k, None)
    if now - _alert_seen.get(text, -_ALERT_WINDOW) < _ALERT_WINDOW:
        return
    _alert_seen[text] = now
    notify(text)
