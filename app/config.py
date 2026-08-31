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
# Среда/релиз попадают в JSON-логи и Sentry. APP_RELEASE обычно равен
# git SHA, который передаёт система деплоя.
APP_ENV = os.getenv(
    "APP_ENV", "production" if COOKIE_SECURE else "development"
).strip() or "production"
APP_RELEASE = os.getenv("APP_RELEASE", "unknown").strip() or "unknown"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"


def _sample_rate(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, value))


# Мониторинг ошибок: при DSN необработанные исключения уходят в Sentry.
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
SENTRY_TRACES_SAMPLE_RATE = _sample_rate("SENTRY_TRACES_SAMPLE_RATE", 0.05)

# --- Вход через Telegram-бота ---
# Бот один и тот же для уведомлений (notify.py) и для входа (deep-link ?start=код).
TG_BOT_USERNAME = os.getenv("TG_BOT_USERNAME", "").strip().lstrip("@")
# Секрет вебхука: Telegram шлёт его в заголовке X-Telegram-Bot-Api-Secret-Token.
# Без него вебхук принимать нельзя — иначе кто угодно «подтвердит» чужой код.
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET", "").strip()
# HTTPS-точка входа Mini App. По умолчанию приложение живёт на том же домене,
# что и сайт; отдельная переменная полезна для тестового бота/стейджинга.
TG_MINI_APP_URL = (os.getenv("TG_MINI_APP_URL", "").strip()
                   or f"{BASE_URL}/tg/app")

# --- OAuth-провайдеры (заготовки) ---
# Пока только каркас: владелец добавит реальные client_id/secret и redirect-URI
# в настройках каждого сервиса позже. Пусто = провайдер не настроен → кнопка
# входа отвечает 503 «ещё не настроен». Реальный обмен кода — отдельным этапом.
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
YANDEX_CLIENT_ID = os.getenv("YANDEX_CLIENT_ID", "").strip()
YANDEX_CLIENT_SECRET = os.getenv("YANDEX_CLIENT_SECRET", "").strip()

# Провайдер → (client_id, client_secret). Пустой client_id = провайдер не
# настроен: кнопка входа показывается, но /auth/<provider> вернёт 503.
OAUTH_PROVIDERS = {
    "discord": (DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET),
    "google": (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET),
    "yandex": (YANDEX_CLIENT_ID, YANDEX_CLIENT_SECRET),
}
OAUTH_LABELS = {"discord": "Discord", "google": "Google", "yandex": "Яндекс"}

# Эндпоинты и параметры каждого провайдера. authorize/token/userinfo — стандартные
# OAuth2-URL; scope — минимум для получения стабильного id и имени. uid_field и
# name_fields говорят, как достать их из ответа userinfo (разные у провайдеров).
OAUTH_META = {
    "discord": {
        "authorize": "https://discord.com/oauth2/authorize",
        "token": "https://discord.com/api/oauth2/token",
        "userinfo": "https://discord.com/api/users/@me",
        "scope": "identify email",
        "uid_field": "id",
        "name_fields": ("global_name", "username"),
        "email_field": "email",
    },
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "userinfo": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
        "uid_field": "sub",
        "name_fields": ("name", "given_name", "email"),
        "email_field": "email",
    },
    "yandex": {
        "authorize": "https://oauth.yandex.ru/authorize",
        "token": "https://oauth.yandex.ru/token",
        "userinfo": "https://login.yandex.ru/info",
        "scope": "login:info login:email",
        "uid_field": "id",
        "name_fields": ("display_name", "real_name", "login"),
        "email_field": "default_email",
    },
}
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
