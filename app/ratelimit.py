"""Лимиты: вход админки + анти-спам публичных ручек. Всё в памяти процесса."""

import asyncio
import logging
import time

from fastapi import HTTPException, Request

import metrics

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
    "withdraw": (10, 40, 600),    # отказ от участия после итогов голосования
    "question": (10, 30, 600),    # 10 вопросов за 10 минут
    "prop": (5, 15, 600),         # 5 предложений (и правок) за 10 минут
    "dadd": (20, 60, 600),        # 20 «добавить себе» по share-ссылке за 10 минут
    "want": (30, 120, 60),        # отметка «Хочу сходить» — такой же toggle, как выбор
    "review": (10, 30, 600),      # публикация/правка обзора
    "name": (10, 30, 600),        # смена имени
    "report": (5, 20, 600),       # 5 жалоб за 10 минут (анти-спам очереди)
}

# Лимиты на залогиненного создателя (ключ — user_id). Помимо общей квоты
# date_limit (всего событий на аккаунт) — это защита от всплесков:
# kind: (лимит на пользователя, окно в секундах)
USER_RATE_RULES = {
    "datecreate": (40, 3600),     # 40 событий в час (квоту 30 это не отменяет)
    "dateedit": (120, 3600),      # 120 правок событий в час
}

# Самое длинное окно среди всех правил — горизонт, на котором ведро ещё «живо».
# GC не должен чистить записи раньше: иначе часовой лимит сбрасывался бы каждые
# 10 минут. + запас.
_MAX_WINDOW = max([w for *_, w in RATE_RULES.values()]
                  + [w for _, w in USER_RATE_RULES.values()])


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


def user_throttle(kind: str, user_id: int, request: Request) -> None:
    """Лимит на залогиненного пользователя (анти-всплеск). Дополнительно бьёт
    по IP — чтобы один человек не плодил аккаунты ради обхода."""
    per_user, window = USER_RATE_RULES[kind]
    ip = client_ip(request)
    if not rate_ok(f"{kind}:u:{user_id}", per_user, window) or \
       not rate_ok(f"{kind}:i:{ip}", per_user * 3, window):
        raise HTTPException(429, "Слишком много действий подряд — передохни минутку.")


def prune_rate_buckets() -> None:
    """Сносит пустые вёдра лимитов: иначе ключи копятся месяцами."""
    now = time.time()
    for k in list(_rates):
        arr = [t for t in _rates[k] if now - t < _MAX_WINDOW]
        if arr:
            _rates[k] = arr
        else:
            _rates.pop(k, None)


async def rates_gc_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            with metrics.track_background_job("rate_limit_gc"):
                prune_rate_buckets()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка чистки лимитов")
