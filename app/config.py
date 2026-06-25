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
# Куда слать ежесуточный снимок базы документом в Telegram (gzip). Отдельная
# переменная, а не TG_CHAT_ID: в базе ПДн посторонних — отправка наружу должна
# быть осознанным opt-in. Пусто — снимки в TG не уходят (остаётся облако/диск).
# Заводи закрытый канал/«Избранное», не обычный диалог с уведомлениями.
TG_BACKUP_CHAT_ID = os.getenv("TG_BACKUP_CHAT_ID", "").strip()


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


def _parse_projects(raw: str) -> list[dict]:
    """Проекты автора для /about: формат «Название|https://url;Другой|https://...».
    Только СОБСТВЕННЫЕ проекты владельца, не сторонняя реклама. Кривые записи
    (без http-ссылки) тихо пропускаем, чтобы опечатка в .env не уронила страницу."""
    out: list[dict] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "|" not in part:
            continue
        name, _, url = part.partition("|")
        name, url = name.strip(), url.strip()
        if name and url.startswith(("https://", "http://")):
            out.append({"name": name, "url": url})
    return out


# Проекты автора (VPN и т.п.) — показываются на /about. См. формат в _parse_projects.
AUTHOR_PROJECTS = _parse_projects(os.getenv("AUTHOR_PROJECTS", ""))

# Кнопки в шапке кабинета и на гостевой странице подборки:
#   «Помощь» — связь с автором/поддержка, «VPN» — проект автора (сайт бесплатный).
# Пустое значение прячет соответствующую кнопку.
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/artiwayn").strip()
VPN_URL = os.getenv("VPN_URL", "https://lk.artik-vpn.site?campaign=date4you").strip()
# Короткий текст «о проекте» для /about (опционально).
ABOUT_TEXT = os.getenv("ABOUT_TEXT", "").strip()


def support_link() -> dict | None:
    """Контакт поддержки как {label, url} для ссылки. @username → t.me/username,
    готовая ссылка — как есть. None, если контакт не задан."""
    c = SUPPORT_CONTACT
    if not c:
        return None
    if c.startswith(("https://", "http://")):
        return {"label": c, "url": c}
    if c.startswith("@"):
        return {"label": c, "url": f"https://t.me/{c[1:]}"}
    return {"label": c, "url": f"https://t.me/{c}"}
# Префикс __Host- жёстко привязывает cookie к хосту (требует Secure) — только в проде
GUEST_COOKIE = "__Host-bg" if COOKIE_SECURE else "bg"
LEGACY_GUEST_COOKIE = "bg"     # дореформенное имя: читаем, чтобы не терять гостей

if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY не задан. Создай .env из .env.example и впиши ключ: openssl rand -hex 32")
