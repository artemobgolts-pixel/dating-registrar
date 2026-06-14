"""Распознавание ссылок на карты в поле «Место».

Если вместо адреса вставили ссылку на карты (Яндекс/Google/2ГИС),
достаём человекочитаемое название из <title> страницы: на карточке будет имя
места, а клик поведёт по исходной ссылке.

Безопасность: автозапрос (resolve_name) ходит ТОЛЬКО на доверённые домены
картографических сервисов. Это закрывает SSRF — иначе гость мог бы вставить
ссылку на внутренний адрес (169.254.169.254, localhost, 10.0.0.0/8…) и
заставить сервер сходить туда. Ссылка любого вида всё равно сохраняется и
открывается у гостя в браузере — мы лишь не ходим по ней сами.
"""

import html
import ipaddress
import logging
import re
import socket
from urllib.parse import urlsplit

import httpx

import db

log = logging.getLogger("places")

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
# хвосты вида « — Яндекс Карты», «| Google Maps» и т.п.
_TAIL = re.compile(
    r"\s*[—–|-]\s*(Яндекс[\s\u00a0]*Карты|Google\s*Maps|2ГИС).*$", re.I)

# Доверённые домены карт: автозапрос идём только сюда (плюс поддомены).
ALLOWED_MAP_HOSTS = (
    "yandex.ru", "yandex.com", "ya.ru", "maps.yandex.ru",
    "google.com", "google.ru", "maps.google.com", "goo.gl", "maps.app.goo.gl",
    "2gis.ru", "2gis.com", "go.2gis.com",
)


def is_link(s: str | None) -> bool:
    return bool(s) and s.startswith(("http://", "https://"))


def _host_allowed(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in ALLOWED_MAP_HOSTS)


def _resolves_to_public_ip(host: str) -> bool:
    """True только если ВСЕ адреса хоста — публичные (не loopback/private/link-local).

    Вторая линия обороны на случай DNS-rebinding: даже разрешённый домен
    не должен указывать на внутренний адрес.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def resolve_name(url: str) -> str | None:
    """Возвращает название из <title>, но ТОЛЬКО для доверённых доменов карт."""
    host = urlsplit(url).hostname or ""
    if not _host_allowed(host):
        log.info("Ссылка не на карты (%s) — название не запрашиваем", host)
        return None
    if not _resolves_to_public_ip(host):
        log.warning("Хост %s резолвится во внутренний адрес — пропускаем", host)
        return None
    try:
        r = httpx.get(url, follow_redirects=True, timeout=5,
                      headers={"User-Agent": "Mozilla/5.0 (date4you)"})
        m = _TITLE.search(r.text[:30000])
        if not m:
            return None
        t = html.unescape(m.group(1)).strip()
        t = _TAIL.sub("", t).strip(" \u00a0—–|-")
        return (t[:200] or None)
    except Exception as e:
        log.warning("Не удалось получить название места по ссылке: %s", e)
        return None


def process_place(place: str | None) -> tuple[str | None, str | None]:
    """(имя_для_карточки, ссылка | None). Ссылку распознаём по префиксу http.

    СИНХРОННЫЙ вариант (ходит в сеть): оставлен для тестов и обратной
    совместимости. В роутах используем split_place + resolve_into_db,
    чтобы не блокировать сохранение свидания на запросе к картам.
    """
    if place and is_link(place):
        return (resolve_name(place) or "Место на карте"), place
    return place, None


def split_place(place: str | None) -> tuple[str | None, str | None, bool]:
    """Мгновенно, без сети: (имя_для_показа, ссылка | None, нужен_резолвинг).

    Если в «Место» вставили ссылку — показываем плейсхолдер «Место на карте»
    сразу, а настоящее название подтягиваем фоном (resolve_into_db).
    """
    if place and is_link(place):
        return "Место на карте", place, True
    return place, None, False


def place_on_edit(place: str | None, existing) -> tuple[str | None, str | None, bool]:
    """То же для правки, но бережёт уже распознанную ссылку.

    Форма правки подставляет в «Место» человекочитаемое ИМЯ (а не ссылку).
    Если поле не трогали — оставляем прежние place/place_url, иначе:
      • вставили новую ссылку → плейсхолдер + фон-резолвинг;
      • вписали обычный адрес → как есть, ссылку сбрасываем.
    """
    old_place = existing["place"] if existing is not None else None
    old_url = existing["place_url"] if existing is not None else None
    if is_link(place):
        return "Место на карте", place, True
    if old_url and place == old_place:           # поле не меняли — не теряем ссылку
        return old_place, old_url, False
    return place, None, False


def resolve_into_db(date_id: int, url: str) -> None:
    """Фон: тянет название из <title> и дописывает в запись свидания.

    Своё короткоживущее соединение (фоновая задача вне жизненного цикла
    запроса). UPDATE сверяется с place_url — если место успели поменять,
    устаревший резолвинг ничего не перезапишет.
    """
    name = resolve_name(url)
    if not name:
        return
    conn = db.connect()
    try:
        conn.execute("UPDATE dates SET place=? WHERE id=? AND place_url=?",
                     (name, date_id, url))
        conn.commit()
    finally:
        conn.close()


def repair_legacy_places() -> int:
    """Чинит старые свидания, где ссылка осела прямо в поле place.

    До фонового резолвинга ссылка на карты сохранялась в place, а place_url
    оставался пустым — на гостевой такая «ссылка-как-адрес» уходила в поиск
    Яндекса (?text=https://…) и показывалась сырым URL. Переносим ссылку в
    place_url, ставим плейсхолдер в place и ставим запросы названий в очередь.

    Идемпотентно: повторный прогон ничего не находит. Возвращает число
    распознанных названий (или 0). Сетевые запросы — синхронно, но только
    на старте и только по доверённым доменам карт (resolve_name).
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, place FROM dates "
            "WHERE place_url IS NULL AND (place LIKE 'http://%' OR place LIKE 'https://%')"
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE dates SET place_url=?, place=? WHERE id=?",
                (r["place"], "Место на карте", r["id"]))
        conn.commit()
        resolved = 0
        for r in rows:
            name = resolve_name(r["place"])
            if name:
                conn.execute("UPDATE dates SET place=? WHERE id=? AND place_url=?",
                             (name, r["id"], r["place"]))
                resolved += 1
        if resolved:
            conn.commit()
        return resolved
    finally:
        conn.close()
