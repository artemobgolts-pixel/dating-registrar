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


def send_to(chat_id: int | str, text: str) -> None:
    """Шлёт сообщение конкретному chat_id (напр. тому, кто подтвердил вход).

    В отличие от notify() не требует CHAT — нужен только TOKEN. Ошибки не валят
    вызвавшего: вход не должен падать из-за недоставленного предупреждения.
    """
    if not TOKEN:
        return
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code >= 400:
            log.warning("Telegram API %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Не удалось отправить сообщение в Telegram: %s", e)


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
