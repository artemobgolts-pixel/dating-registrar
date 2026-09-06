"""Консервативный полнотекстовый поиск для экспериментальной ленты.

Индекс и внешние сервисы намеренно не используются: модуль можно удалить без
миграции базы, а пользовательские ссылки никогда не открываются сервером. Из
URL извлекаются только уже записанные домен, путь и параметры — в том числе
percent-encoded названия мест и латинские slug'и.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote_plus, urlsplit


MAX_QUERY_LENGTH = 80
MAX_QUERY_TERMS = 6
SEARCH_POOL_SIZE = 2000

_WORD_RE = re.compile(r"[0-9a-zа-я]+", re.IGNORECASE)
_URL_NOISE = frozenset({
    "2gis", "com", "dir", "firm", "geo", "google", "html", "http",
    "https", "map", "maps", "mode", "org", "place", "query", "ru",
    "search", "text", "url", "www", "yandex",
})

# Это не попытка угадать смысл события. Группы связывают только формы одного
# понятия и распространённые варианты из латинских URL; близкие, но разные
# категории (например, ресторан и антикафе) остаются раздельными.
_ALIAS_GROUPS = (
    ("ресторан", "рестораны", "restaurant", "restaurants", "restoran"),
    ("кафе", "cafe", "cafes"),
    ("антикафе", "anticafe"),
    ("кофейня", "кофейни", "coffeehouse", "coffeeshop"),
    ("бар", "бары", "bar", "bars"),
    ("паб", "пабы", "pub", "pubs"),
    ("кино", "кинотеатр", "кинотеатры", "cinema"),
    ("театр", "театры", "theatre", "theater"),
    ("музей", "музеи", "museum", "museums"),
    ("выставка", "выставки", "exhibition"),
    ("концерт", "концерты", "concert"),
    ("квест", "квесты", "quest"),
    ("парк", "парки", "park", "parks"),
)
_ALIASES = {
    item: group
    for group in _ALIAS_GROUPS
    for item in group
}

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})


@dataclass(frozen=True)
class SearchTerm:
    alternatives: tuple[str, ...]


@dataclass(frozen=True)
class SearchQuery:
    display: str
    normalized: str
    terms: tuple[SearchTerm, ...]


@dataclass(frozen=True)
class _Field:
    normalized: str
    words: tuple[str, ...]
    weight: int


def clean_query(value: object) -> str:
    """Ограничивает ввод до безопасной короткой строки для UI и поиска."""
    raw = unicodedata.normalize("NFKC", str(value or ""))
    raw = "".join(ch for ch in raw if unicodedata.category(ch)[0] != "C")
    return " ".join(raw.split())[:MAX_QUERY_LENGTH].strip()


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(_WORD_RE.findall(text.replace("ё", "е")))


def _transliterate(value: str) -> str:
    return value.translate(_TRANSLIT)


def prepare_query(value: object) -> SearchQuery:
    display = clean_query(value)
    normalized = _decode_url(display) if "://" in display else _normalize(display)
    terms: list[SearchTerm] = []
    seen: set[tuple[str, ...]] = set()
    for word in normalized.split():
        if len(word) < 2:
            continue
        variants: list[str] = []
        for alias in _ALIASES.get(word, (word,)):
            for variant in (alias, _transliterate(alias)):
                if len(variant) >= 2 and variant not in variants:
                    variants.append(variant)
        key = tuple(variants)
        if key and key not in seen:
            terms.append(SearchTerm(key))
            seen.add(key)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return SearchQuery(display, normalized, tuple(terms))


def _decode_url(raw: object) -> str:
    value = str(raw or "").strip()[:4096]
    if not value:
        return ""
    # Двойное кодирование встречается в redirect-ссылках. Двух проходов
    # достаточно, чтобы не превращать разбор в неограниченную работу.
    for _ in range(2):
        decoded = unquote_plus(value)
        if decoded == value:
            break
        value = decoded
    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
    except ValueError:
        return _normalize(value)
    host = (parsed.hostname or "").replace(".", " ")
    query = parsed.query.replace("&", " ").replace("=", " ")
    words = _normalize(" ".join((host, parsed.path, query, parsed.fragment))).split()
    return " ".join(
        word for word in words
        if word not in _URL_NOISE and not word.isdigit()
    )


def _field(value: object, weight: int, *, is_url: bool = False) -> _Field | None:
    normalized = _decode_url(value) if is_url else _normalize(value)
    if not normalized:
        return None
    return _Field(normalized, tuple(normalized.split()), weight)


def _edit_distance(left: str, right: str, maximum: int) -> int:
    """Levenshtein с ранним выходом; вызывается только для близких длин."""
    if abs(len(left) - len(right)) > maximum:
        return maximum + 1
    previous = list(range(len(right) + 1))
    for index, left_char in enumerate(left, start=1):
        current = [index]
        row_min = index
        for other_index, right_char in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[other_index] + 1,
                previous[other_index - 1] + (left_char != right_char),
            ))
            row_min = min(row_min, current[-1])
        if row_min > maximum:
            return maximum + 1
        previous = current
    return previous[-1]


def _match_quality(term: SearchTerm, field: _Field) -> int:
    best = 0
    for alternative in term.alternatives:
        for word in field.words:
            if alternative == word:
                best = max(best, 10)
                continue
            if len(alternative) >= 3 and word.startswith(alternative):
                best = max(best, 8)
                continue
            # Опечатки допускаются только в достаточно длинном слове и не
            # больше одной (двух для 9+ символов): смысловые догадки запрещены.
            if len(alternative) < 5:
                continue
            maximum = 2 if len(alternative) >= 9 else 1
            if abs(len(alternative) - len(word)) <= maximum \
                    and _edit_distance(alternative, word, maximum) <= maximum:
                best = max(best, 6)
    return best


def _row_value(row: object, key: str) -> object:
    try:
        return row[key]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        return None


def score_event(row: object, links: list[str] | tuple[str, ...],
                query: SearchQuery) -> int:
    """Текстовая релевантность события; ноль означает «не показывать»."""
    if not query.terms:
        return 0
    fields = [
        _field(_row_value(row, "name"), 12),
        _field(_row_value(row, "place"), 10),
        _field(_row_value(row, "comment"), 5),
        _field(_row_value(row, "place_url"), 8, is_url=True),
    ]
    fields.extend(_field(link, 7, is_url=True) for link in links)
    present = [field for field in fields if field is not None]
    if not present:
        return 0

    score = 0
    for term in query.terms:
        term_score = max(
            (field.weight * _match_quality(term, field) for field in present),
            default=0,
        )
        # Все значимые слова запроса обязательны. Это главная защита от
        # «умных», но нерелевантных результатов.
        if term_score == 0:
            return 0
        score += term_score

    # Точная фраза лишь улучшает порядок; она не может протащить событие,
    # которое не прошло проверку каждого слова выше.
    for field in present:
        if query.normalized and query.normalized in field.normalized:
            score += field.weight * 4
    return score
