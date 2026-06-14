"""Лимиты: вход админки + анти-спам публичных ручек. Всё в памяти процесса."""

import asyncio
import logging
import time

from fastapi import HTTPException, Request

log = logging.getLogger("ratelimit")

_rates: dict[str, list[float]] = {}


def rate_ok(bucket: str, limit: int, window: int) -> bool:
    now = time.time()
    arr = [t for t in _rates.get(bucket, []) if now - t < window]
    if len(arr) >= limit:
        _rates[bucket] = arr
        return False
    arr.append(now)
    _rates[bucket] = arr
    return True


# kind: (лимит на гостя, лимит на IP, окно в секундах)
RATE_RULES = {
    "book": (30, 120, 60),        # 30 действий с выбором в минуту
    "question": (10, 30, 600),    # 10 вопросов за 10 минут
    "prop": (5, 15, 600),         # 5 предложений (и правок) за 10 минут
    "name": (10, 30, 600),        # смена имени
}


def client_ip(request: Request) -> str:
    """Реальный IP клиента для лимитов.

    Берём X-Real-IP, который Caddy ПЕРЕЗАПИСЫВАЕТ значением remote_host, —
    в отличие от X-Forwarded-For его нельзя подделать заголовком клиента.
    Без прокси (локальная разработка, тесты) — обычный адрес соединения.
    """
    return request.headers.get("x-real-ip") or \
        (request.client.host if request.client else "?")


def guest_throttle(kind: str, guest: str, request: Request) -> None:
    per_guest, per_ip, window = RATE_RULES[kind]
    ip = client_ip(request)
    if not rate_ok(f"{kind}:g:{guest}", per_guest, window) or \
       not rate_ok(f"{kind}:i:{ip}", per_ip, window):
        raise HTTPException(429, "Слишком много действий подряд — передохни минутку ♥")


_login_fails: dict[str, list[float]] = {}


def _throttle_ok(ip: str) -> bool:
    now = time.time()
    arr = [t for t in _login_fails.get(ip, []) if now - t < 900]
    _login_fails[ip] = arr
    return len(arr) < 10


def _register_fail(ip: str) -> None:
    _login_fails.setdefault(ip, []).append(time.time())


def prune_rate_buckets() -> None:
    """Сносит пустые вёдра лимитов: иначе ключи копятся месяцами."""
    now = time.time()
    for store, window in ((_rates, 600), (_login_fails, 900)):
        for k in list(store):
            arr = [t for t in store[k] if now - t < window]
            if arr:
                store[k] = arr
            else:
                store.pop(k, None)


async def rates_gc_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            prune_rate_buckets()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка чистки лимитов")
