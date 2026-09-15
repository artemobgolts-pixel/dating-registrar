"""Единая граница положительных ID перед передачей значений SQLite."""

from typing import Annotated

from fastapi import HTTPException
from pydantic import AfterValidator


MAX_OBJECT_ID = (1 << 63) - 1


def require_object_id(value: int | str) -> int:
    """Принимает int и сырой path-параметр ранней проверки загрузки."""
    if isinstance(value, str):
        digits = value.strip().removeprefix("+")
        if not digits.isascii() or not digits.isdecimal():
            raise HTTPException(404, "Некорректный идентификатор")
        digits = digits.lstrip("0")
        # Не преобразуем произвольно длинное число даже при снятом лимите int().
        if not digits or len(digits) > 19:
            raise HTTPException(404, "Некорректный идентификатор")
        value = int(digits)
    if type(value) is not int or not 0 < value <= MAX_OBJECT_ID:
        raise HTTPException(404, "Некорректный идентификатор")
    return value


# Одинаковая проверка для path/form и каждого элемента списка ID.
ObjectId = Annotated[int, AfterValidator(require_object_id)]
