"""Глобальные флаги платформы (модерация и т.п.) поверх таблицы settings.

Значения хранятся строками ('0'/'1'). Флаги нужны редко и читаются точечно
в горячих местах (вход, создание категории), поэтому без кэша — простой
SELECT по первичному ключу.
"""

# Ключи флагов. Модерация по умолчанию ВЫКЛючена (значение по умолчанию '0').
MODERATE_USERS = "moderate_users"            # новые пользователи попадают в очередь
MODERATE_CATEGORIES = "moderate_categories"  # новые категории попадают в очередь


def get_flag(conn, key: str, default: str = "0") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def is_on(conn, key: str) -> bool:
    return get_flag(conn, key) == "1"


def set_flag(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, "1" if value in ("1", "on", "true", "yes", True) else "0"))
    conn.commit()
