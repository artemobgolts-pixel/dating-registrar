"""Уведомления админу в Telegram. Если токен/чат не заданы — тихо ничего не делает."""

import html
import logging
import os

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
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        # 200 от httpx не значит «доставлено»: Telegram отвечает 4xx/5xx
        # на битый chat_id, отозванный токен, слишком длинный текст и т.п.
        if r.status_code >= 400:
            log.warning("Telegram API %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Не удалось отправить уведомление в Telegram: %s", e)
