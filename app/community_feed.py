"""Ранжирование ленты публичных событий без внешней рекомендательной системы.

Первая страница строится по ограниченному окну свежих событий: это сохраняет
предсказуемую стоимость запроса, но позволяет учитывать близость даты,
популярность, качество карточки и историю действий текущего пользователя.
После окна лента продолжает работать в хронологическом режиме, поэтому старые
события не пропадают из бесконечного скролла.

Курсор не является авторизационным артефактом. Тем не менее он разбирается по
строгому формату и имеет ограниченные числовые поля: произвольный ввод из URL
не попадает ни в SQL, ни в неограниченный OFFSET.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

import community_search


PAGE_SIZE = 12
RANKING_POOL_SIZE = 240
SEARCH_POOL_SIZE = community_search.SEARCH_POOL_SIZE

_RANKED_CURSOR_RE = re.compile(
    r"^r1\.(?P<stamp>\d{14})\.(?P<max_id>[1-9]\d*)\.(?P<offset>\d{1,3})$"
)
_CHRONO_CURSOR_RE = re.compile(
    r"^c1\.(?P<before_id>[1-9]\d*)(?:\.(?P<last_owner>[1-9]\d*))?$"
)
_SEARCH_CURSOR_RE = re.compile(
    r"^s1\.(?P<stamp>\d{14})\.(?P<max_id>[1-9]\d*)\."
    r"(?P<offset>\d{1,4})\.(?P<signature>[0-9a-f]{10})$"
)


@dataclass(frozen=True)
class FeedPage:
    rows: list[sqlite3.Row]
    next_cursor: str | None
    mode: str
    candidate_count: int


def _stamp(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y%m%d%H%M%S")


def _ranked_cursor(as_of: datetime, max_id: int, offset: int) -> str:
    return f"r1.{_stamp(as_of)}.{max_id}.{offset}"


def _chrono_cursor(before_id: int, last_owner: int | None = None) -> str:
    suffix = f".{last_owner}" if last_owner else ""
    return f"c1.{before_id}{suffix}"


def _search_signature(query: community_search.SearchQuery) -> str:
    return hashlib.blake2s(
        query.normalized.encode("utf-8"), digest_size=5,
    ).hexdigest()


def _search_cursor(as_of: datetime, max_id: int, offset: int,
                   query: community_search.SearchQuery) -> str:
    return f"s1.{_stamp(as_of)}.{max_id}.{offset}.{_search_signature(query)}"


def _parse_search_cursor(raw: object, query: community_search.SearchQuery):
    value = str(raw or "").strip()
    match = _SEARCH_CURSOR_RE.fullmatch(value)
    if not match or not hmac.compare_digest(
        match.group("signature"), _search_signature(query),
    ):
        return None
    try:
        as_of = datetime.strptime(match.group("stamp"), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    offset = int(match.group("offset"))
    max_id = int(match.group("max_id"))
    if offset > SEARCH_POOL_SIZE or max_id > 9_223_372_036_854_775_807:
        return None
    return as_of, max_id, offset


def _parse_cursor(raw: object):
    if isinstance(raw, int) and raw > 0:
        return "chronological", (raw, None)
    value = str(raw or "").strip()
    if not value:
        return "fresh", None
    # Старые числовые курсоры продолжают работать после выкладки новой версии.
    if value.isdigit() and int(value) > 0:
        return "chronological", (int(value), None)
    match = _CHRONO_CURSOR_RE.fullmatch(value)
    if match:
        last_owner = match.group("last_owner")
        return "chronological", (
            int(match.group("before_id")),
            int(last_owner) if last_owner else None,
        )
    match = _RANKED_CURSOR_RE.fullmatch(value)
    if not match:
        return "fresh", None
    try:
        as_of = datetime.strptime(match.group("stamp"), "%Y%m%d%H%M%S")
    except ValueError:
        return "fresh", None
    max_id = int(match.group("max_id"))
    offset = int(match.group("offset"))
    if offset > RANKING_POOL_SIZE:
        return "fresh", None
    return "ranked", (as_of, max_id, offset)


def _candidate_rows(
    conn: sqlite3.Connection,
    viewer_id: int,
    as_of: datetime,
    *,
    limit: int,
    max_id: int | None = None,
    before_id: int | None = None,
) -> list[sqlite3.Row]:
    where = [
        "d.is_public=1",
        "d.is_draft=0",
        "d.operator_review_pending=0",
        "d.archived_at IS NULL",
        "d.owner_id<>?",
        # Autoarchive запускается раз в минуту. Не показываем уже начавшееся
        # событие даже в коротком окне между стартом и фоновым проходом.
        "(d.starts_at IS NULL OR datetime(d.starts_at) IS NULL "
        "OR datetime(d.starts_at)>datetime(?))",
        "NOT EXISTS ("
        "SELECT 1 FROM dates copied WHERE copied.owner_id=? "
        "AND copied.origin='copy' "
        "AND copied.source_date_id=COALESCE(d.source_date_id,d.id))",
    ]
    params: list[object] = [
        int(viewer_id), as_of.isoformat(sep="T", timespec="seconds"), int(viewer_id),
    ]
    if max_id is not None:
        where.append("d.id<=?")
        params.append(int(max_id))
    if before_id is not None:
        where.append("d.id<?")
        params.append(int(before_id))
    params.append(max(1, int(limit)))
    return conn.execute(
        "SELECT d.*, u.display_name AS owner_name, "
        "u.tg_username AS owner_username, u.avatar_path AS owner_avatar, "
        "EXISTS(SELECT 1 FROM date_images di WHERE di.date_id=d.id) AS has_image "
        "FROM dates d JOIN users u ON u.id=d.owner_id "
        f"WHERE {' AND '.join(where)} ORDER BY d.id DESC LIMIT ?",
        tuple(params),
    ).fetchall()


def _counts_for_ids(
    conn: sqlite3.Connection,
    sql_prefix: str,
    ids: list[int],
    params: tuple[object, ...],
) -> dict[int, int]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    return {
        int(row["item_id"]): int(row["total"])
        for row in conn.execute(
            f"{sql_prefix} ({placeholders}) GROUP BY item_id",
            (*params, *ids),
        )
    }


def _popularity(
    conn: sqlite3.Connection, viewer_id: int, rows: list[sqlite3.Row],
) -> tuple[dict[int, int], dict[int, int]]:
    date_ids = [int(row["id"]) for row in rows]
    root_ids = list(dict.fromkeys(
        int(row["source_date_id"] or row["id"]) for row in rows
    ))
    wants = _counts_for_ids(
        conn,
        "SELECT date_id AS item_id, COUNT(*) AS total FROM date_wants "
        "WHERE user_id<>? AND date_id IN",
        date_ids,
        (int(viewer_id),),
    )
    copies = _counts_for_ids(
        conn,
        "SELECT source_date_id AS item_id, COUNT(*) AS total FROM dates "
        "WHERE owner_id<>? AND origin='copy' AND source_date_id IN",
        root_ids,
        (int(viewer_id),),
    )
    return wants, copies


def _author_affinity(conn: sqlite3.Connection, viewer_id: int) -> dict[int, int]:
    """Вес прошлых осмысленных действий пользователя по авторам событий.

    Низкая оценка уменьшает affinity, высокая увеличивает. Несколько голосов за
    одно событие в разных подборках считаются одним действием.
    """
    rows = conn.execute(
        "SELECT owner_id, SUM(points) AS points FROM ("
        " SELECT d.owner_id, 3 AS points FROM date_wants w "
        " JOIN dates d ON d.id=w.date_id WHERE w.user_id=? "
        " UNION ALL "
        " SELECT source.owner_id, 5 AS points FROM dates copied "
        " JOIN dates source ON source.id=copied.source_date_id "
        " WHERE copied.owner_id=? AND copied.origin='copy' "
        " UNION ALL "
        " SELECT d.owner_id, 4 AS points FROM ("
        "  SELECT DISTINCT date_id FROM bookings "
        "  WHERE user_id=? AND participation_withdrawn_at IS NULL"
        " ) chosen JOIN dates d ON d.id=chosen.date_id "
        " UNION ALL "
        " SELECT d.owner_id, (r.rating-3)*2 AS points FROM date_reviews r "
        " JOIN dates d ON d.id=r.date_id WHERE r.user_id=?"
        ") interactions GROUP BY owner_id",
        (viewer_id, viewer_id, viewer_id, viewer_id),
    ).fetchall()
    return {
        int(row["owner_id"]): int(row["points"])
        for row in rows
        if int(row["points"] or 0) != 0
    }


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _event_score(
    row: sqlite3.Row,
    *,
    as_of: datetime,
    wants: int,
    copies: int,
    affinity: int,
) -> int:
    starts_at = _parse_datetime(row["starts_at"])
    if starts_at is None:
        proximity = 6
    else:
        days = max(0.0, (starts_at - as_of).total_seconds() / 86_400)
        if days <= 7:
            proximity = 36
        elif days <= 30:
            proximity = 28
        elif days <= 90:
            proximity = 16
        elif days <= 180:
            proximity = 8
        else:
            proximity = 2

    created_at = _parse_datetime(row["created_at"])
    freshness = 0
    if created_at is not None:
        age_days = max(0.0, (as_of - created_at).total_seconds() / 86_400)
        if age_days <= 3:
            freshness = 18
        elif age_days <= 14:
            freshness = 12
        elif age_days <= 60:
            freshness = 6

    quality = 12 if row["has_image"] else 0
    quality += 4 if str(row["place"] or "").strip() else 0
    quality += 4 if str(row["comment"] or "").strip() else 0
    quality += 4 if starts_at is not None else 0

    # Ограничения не дают одному вирусному событию навсегда вытеснить новые.
    popularity = min(max(0, wants), 8) * 4 + min(max(0, copies), 8) * 5
    personal = min(30, max(-12, affinity))
    return proximity + freshness + quality + popularity + personal


def _diversify(
    rows: list[sqlite3.Row], previous_owner: int | None = None,
) -> list[sqlite3.Row]:
    """Сохраняет рейтинг, выбирая ближайшего другого автора между повторами."""
    remaining = list(rows)
    result: list[sqlite3.Row] = []
    while remaining:
        pick = 0
        last_owner = int(result[-1]["owner_id"]) if result else previous_owner
        if last_owner and int(remaining[0]["owner_id"]) == last_owner:
            pick = next(
                (index for index, row in enumerate(remaining)
                 if int(row["owner_id"]) != last_owner),
                0,
            )
        result.append(remaining.pop(pick))
    return result


def _ranked_rows(
    conn: sqlite3.Connection,
    viewer_id: int,
    rows: list[sqlite3.Row],
    as_of: datetime,
) -> tuple[list[sqlite3.Row], str]:
    wants, copies = _popularity(conn, viewer_id, rows)
    affinity = _author_affinity(conn, viewer_id)
    personalized = any(int(row["owner_id"]) in affinity for row in rows)

    def key(row: sqlite3.Row) -> tuple[int, int]:
        date_id = int(row["id"])
        root_id = int(row["source_date_id"] or date_id)
        return (
            _event_score(
                row,
                as_of=as_of,
                wants=wants.get(date_id, 0),
                copies=copies.get(root_id, 0),
                affinity=affinity.get(int(row["owner_id"]), 0),
            ),
            date_id,
        )

    ranked = sorted(rows, key=key, reverse=True)
    return _diversify(ranked), "personalized" if personalized else "general"


def _links_for_rows(
    conn: sqlite3.Connection, rows: list[sqlite3.Row],
) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    ids = [int(row["id"]) for row in rows]
    # Старые сборки SQLite ограничивали запрос 999 параметрами. Небольшие чанки
    # сохраняют поиск переносимым и не меняют число запросов на обычной ленте.
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        placeholders = ",".join("?" for _ in chunk)
        for link in conn.execute(
            "SELECT date_id, url FROM date_links "
            f"WHERE date_id IN ({placeholders}) ORDER BY date_id, position, id",
            tuple(chunk),
        ):
            result.setdefault(int(link["date_id"]), []).append(str(link["url"]))
    return result


def _search_page(
    conn: sqlite3.Connection,
    viewer_id: int,
    cursor: object,
    query: community_search.SearchQuery,
    current: datetime,
    page_size: int,
    pool_size: int,
) -> FeedPage:
    parsed_cursor = _parse_search_cursor(cursor, query)
    if parsed_cursor:
        as_of, max_id, offset = parsed_cursor
    else:
        as_of, max_id, offset = current, None, 0

    candidates = _candidate_rows(
        conn, viewer_id, as_of, limit=pool_size + 1, max_id=max_id,
    )
    if not candidates:
        return FeedPage([], None, "search", 0)
    if max_id is None:
        max_id = int(candidates[0]["id"])
    pool = candidates[:pool_size]
    links = _links_for_rows(conn, pool)
    scored = [
        (row, community_search.score_event(
            row, links.get(int(row["id"]), []), query,
        ))
        for row in pool
    ]
    matches = [(row, score) for row, score in scored if score > 0]
    if not matches:
        return FeedPage([], None, "search", len(pool))

    match_rows = [row for row, _ in matches]
    wants, copies = _popularity(conn, viewer_id, match_rows)
    affinity = _author_affinity(conn, viewer_id)

    def key(item: tuple[sqlite3.Row, int]) -> tuple[int, int, int]:
        row, search_score = item
        date_id = int(row["id"])
        root_id = int(row["source_date_id"] or date_id)
        recommendation_score = _event_score(
            row,
            as_of=as_of,
            wants=wants.get(date_id, 0),
            copies=copies.get(root_id, 0),
            affinity=affinity.get(int(row["owner_id"]), 0),
        )
        return search_score, recommendation_score, date_id

    ordered = sorted(matches, key=key, reverse=True)
    # Разводим одинаково релевантные карточки разных авторов, но никогда не
    # меняем местами разные уровни текстового совпадения.
    ranked: list[sqlite3.Row] = []
    previous_owner: int | None = None
    index = 0
    while index < len(ordered):
        score = ordered[index][1]
        end = index + 1
        while end < len(ordered) and ordered[end][1] == score:
            end += 1
        group = _diversify(
            [row for row, _ in ordered[index:end]], previous_owner,
        )
        ranked.extend(group)
        previous_owner = int(group[-1]["owner_id"])
        index = end

    if offset >= len(ranked):
        return FeedPage([], None, "search", len(pool))
    page_rows = ranked[offset:offset + page_size]
    next_offset = offset + len(page_rows)
    next_cursor = _search_cursor(as_of, max_id, next_offset, query) \
        if next_offset < len(ranked) else None
    return FeedPage(page_rows, next_cursor, "search", len(pool))


def _chronological_page(
    conn: sqlite3.Connection,
    viewer_id: int,
    before_id: int,
    as_of: datetime,
    page_size: int,
    previous_owner: int | None = None,
) -> FeedPage:
    rows = _candidate_rows(
        conn, viewer_id, as_of, limit=page_size + 1, before_id=before_id,
    )
    has_more = len(rows) > page_size
    page_rows = rows[:page_size]
    diversified = _diversify(page_rows, previous_owner)
    next_cursor = _chrono_cursor(
        int(page_rows[-1]["id"]), int(diversified[-1]["owner_id"]),
    ) if page_rows and has_more else None
    return FeedPage(
        rows=diversified,
        next_cursor=next_cursor,
        mode="chronological",
        candidate_count=len(rows),
    )


def page(
    conn: sqlite3.Connection,
    viewer_id: int,
    cursor: object = None,
    *,
    now: datetime | None = None,
    page_size: int = PAGE_SIZE,
    pool_size: int = RANKING_POOL_SIZE,
    query: object = None,
    search_pool_size: int = SEARCH_POOL_SIZE,
) -> FeedPage:
    """Возвращает страницу рекомендаций либо релевантных результатов поиска."""
    current = (now or datetime.now()).replace(tzinfo=None, microsecond=0)
    safe_page_size = min(PAGE_SIZE, max(1, int(page_size)))
    safe_pool_size = min(RANKING_POOL_SIZE, max(safe_page_size, int(pool_size)))
    prepared_query = community_search.prepare_query(query)
    if prepared_query.terms:
        safe_search_pool = min(
            SEARCH_POOL_SIZE,
            max(safe_page_size, int(search_pool_size)),
        )
        return _search_page(
            conn, viewer_id, cursor, prepared_query, current,
            safe_page_size, safe_search_pool,
        )
    cursor_kind, cursor_data = _parse_cursor(cursor)

    if cursor_kind == "chronological":
        before_id, previous_owner = cursor_data
        return _chronological_page(
            conn, viewer_id, before_id, current, safe_page_size, previous_owner,
        )

    if cursor_kind == "ranked":
        as_of, max_id, offset = cursor_data
    else:
        as_of, max_id, offset = current, None, 0

    recent = _candidate_rows(
        conn,
        viewer_id,
        as_of,
        limit=safe_pool_size + 1,
        max_id=max_id,
    )
    if not recent:
        return FeedPage([], None, "general", 0)
    if max_id is None:
        max_id = int(recent[0]["id"])

    has_older = len(recent) > safe_pool_size
    pool = recent[:safe_pool_size]
    ranked, mode = _ranked_rows(conn, viewer_id, pool, as_of)
    if offset >= len(ranked):
        if has_older and pool:
            return _chronological_page(
                conn, viewer_id, int(pool[-1]["id"]), as_of, safe_page_size,
            )
        return FeedPage([], None, mode, len(pool))

    page_rows = ranked[offset:offset + safe_page_size]
    next_offset = offset + len(page_rows)
    if next_offset < len(ranked):
        next_cursor = _ranked_cursor(as_of, max_id, next_offset)
    elif has_older:
        next_cursor = _chrono_cursor(
            int(pool[-1]["id"]), int(page_rows[-1]["owner_id"]),
        )
    else:
        next_cursor = None
    return FeedPage(page_rows, next_cursor, mode, len(pool))
