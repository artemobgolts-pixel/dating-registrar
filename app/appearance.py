"""Общие значения визуальных оформлений date4you.

Светлая/тёмная тема остаётся настройкой браузера. ``skin`` отвечает только за
характер интерфейса: стандартный или сохранённый авторский романтический.
"""

FRIENDS = "friends"
ROMANTIC = "romantic"
VALID_SKINS = frozenset({FRIENDS, ROMANTIC})


def normalize_skin(value: object, *, default: str = FRIENDS) -> str:
    """Возвращает только разрешённое значение, безопасное для ``data-skin``."""
    if default not in VALID_SKINS:
        raise ValueError("unknown default skin")
    return value if isinstance(value, str) and value in VALID_SKINS else default
