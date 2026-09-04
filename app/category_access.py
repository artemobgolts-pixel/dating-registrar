"""Безопасное хранение четырёхзначного PIN-кода подборки.

В базе лежит только salted PBKDF2-хеш.  Формат намеренно версионирован, чтобы
параметры можно было повышать без хранения исходного PIN-кода.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets


_PIN_RE = re.compile(r"[0-9]{4}")
_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 310_000
_SALT_BYTES = 16


def validate_pin(pin: str) -> bool:
    """Только четыре ASCII-цифры; Unicode-цифры в протокол не принимаются."""
    return bool(_PIN_RE.fullmatch(pin or ""))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_pin(pin: str) -> str:
    """Возвращает salted PBKDF2-хеш; неверный PIN отвергает до записи в БД."""
    if not validate_pin(pin):
        raise ValueError("PIN-код должен состоять ровно из 4 цифр")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", pin.encode("ascii"), salt, _ITERATIONS,
    )
    return f"{_ALGORITHM}${_ITERATIONS}${_encode(salt)}${_encode(digest)}"


def verify_pin(pin: str, encoded: str) -> bool:
    """Проверяет PIN без исключений и со сравнением за постоянное время."""
    if not validate_pin(pin) or not encoded:
        return False
    try:
        algorithm, rounds, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        iterations = int(rounds)
        # Не даём повреждённой строке заставить процесс считать произвольно
        # дорогой PBKDF2. Нижняя граница не мешает будущей ротации параметров.
        if not 100_000 <= iterations <= 1_000_000:
            return False
        salt = _decode(salt_text)
        expected = _decode(digest_text)
        if not 8 <= len(salt) <= 64 or len(expected) != 32:
            return False
    except (ValueError, TypeError, binascii.Error):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", pin.encode("ascii"), salt, iterations,
    )
    return hmac.compare_digest(actual, expected)
