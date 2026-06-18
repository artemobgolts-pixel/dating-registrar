"""Конфигурация из переменных окружения.

Обязательные переменные проверяются прямо при импорте — лучше упасть
с понятной ошибкой при старте, чем молча работать со случайным ключом.
"""

import os

from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
DOMAIN = os.getenv("DOMAIN", "localhost")
BASE_URL = f"https://{DOMAIN}"
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").strip().lower() in ("1", "true", "yes")
# Мониторинг ошибок (опционально): если задан DSN и установлен sentry-sdk —
# необработанные исключения уходят в Sentry. Иначе минимум — лог + алёрт в TG.
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()

# --- Вход через Telegram-бота ---
# Бот один и тот же для уведомлений (notify.py) и для входа (deep-link ?start=код).
TG_BOT_USERNAME = os.getenv("TG_BOT_USERNAME", "").strip().lstrip("@")
# Секрет вебхука: Telegram шлёт его в заголовке X-Telegram-Bot-Api-Secret-Token.
# Без него вебхук принимать нельзя — иначе кто угодно «подтвердит» чужой код.
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET", "").strip()


def _parse_operator_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


# Операторы (суперадмины): их telegram_id. Пусто — операторов нет до ручного бэкофилла.
OPERATOR_TG_IDS = _parse_operator_ids(os.getenv("OPERATOR_TG_IDS", ""))
# Контакт поддержки (Telegram @username или ссылка) — для текста про расширение
# лимита и страницы /about. Без значения — текст без конкретного контакта.
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "").strip()
# Префикс __Host- жёстко привязывает cookie к хосту (требует Secure) — только в проде
GUEST_COOKIE = "__Host-bg" if COOKIE_SECURE else "bg"
LEGACY_GUEST_COOKIE = "bg"     # дореформенное имя: читаем, чтобы не терять гостей

if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY не задан. Создай .env из .env.example и впиши ключ: openssl rand -hex 32")
