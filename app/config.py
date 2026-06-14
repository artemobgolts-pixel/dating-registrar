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
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").strip().lower() in ("1", "true", "yes")
# Префикс __Host- жёстко привязывает cookie к хосту (требует Secure) — только в проде
GUEST_COOKIE = "__Host-bg" if COOKIE_SECURE else "bg"
LEGACY_GUEST_COOKIE = "bg"     # дореформенное имя: читаем, чтобы не терять гостей

if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY не задан. Создай .env из .env.example и впиши ключ: openssl rand -hex 32")
if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD не задан. Заполни логин и пароль админки в .env")
