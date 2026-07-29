"""Owner-гейт мультитенантной изоляции.

ГЛАВНЫЙ инвариант продукта: пользователь видит и трогает ТОЛЬКО свои данные.
Все ручки кабинета достают корневые сущности (categories, dates) исключительно
через эти хелперы — НЕ инлайновым `WHERE id=?`. Дочерние сущности
(date_links/date_images/date_videos/date_categories/bookings/questions)
принадлежат транзитивно — их владелец проверяется через JOIN к родителю.

Почему 404, а не 403: 403 («это не твоё») подтверждает, что объект с таким id
существует — утечка. 404 не отличает «нет объекта» от «не твой».
"""

from fastapi import HTTPException


def get_owned_category(conn, cid: int, user_id: int):
    """Категория cid, если она принадлежит user_id. Иначе 404."""
    cat = conn.execute(
        "SELECT * FROM categories WHERE id=? AND owner_id=?", (cid, user_id)
    ).fetchone()
    if not cat:
        raise HTTPException(404, "Категория не найдена")
    return cat


def get_owned_date(conn, did: int, user_id: int):
    """Событие did, если оно принадлежит user_id. Иначе 404."""
    d = conn.execute(
        "SELECT * FROM dates WHERE id=? AND owner_id=?", (did, user_id)
    ).fetchone()
    if not d:
        raise HTTPException(404, "Событие не найдено")
    return d


def owned_date_ids(conn, user_id: int) -> set[int]:
    """id всех событий пользователя — для пакетных проверок (реордер, attach)."""
    return {r[0] for r in conn.execute(
        "SELECT id FROM dates WHERE owner_id=?", (user_id,))}
