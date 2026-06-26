"""Время, форматирование и валидация форм. Всё время — МСК, naive-строки."""

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlparse

from fastapi import HTTPException
from markupsafe import Markup, escape

from config import MSK

# Длина токена ссылки в байтах. 8 байт = 64 бита энтропии → ссылка ~11 символов
# (base64url). Перебрать нереально, а ссылка втрое короче прежних 24 байт.
LINK_TOKEN_BYTES = 8


def new_link_token() -> str:
    """Короткий непредсказуемый токен для ссылок категорий и шаринга свиданий.
    Длина задаётся LINK_TOKEN_BYTES; старые длинные токены продолжают работать."""
    return secrets.token_urlsafe(LINK_TOKEN_BYTES)


RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def now_naive() -> datetime:
    return datetime.now(MSK).replace(tzinfo=None, microsecond=0)


def now_iso() -> str:
    return now_naive().isoformat(sep="T")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _fmt_point(dt: datetime, with_year: bool = True) -> str:
    base = f"{dt.day} {RU_MONTHS[dt.month - 1]}"
    if with_year:
        base += f" {dt.year}"
    return f"{base}, {dt:%H:%M}"


def fmt_when(starts: str | None, ends: str | None = None) -> str:
    """«12 июня 2026, 19:00–22:00» / «12 июня 2026, 19:00 — 13 июня 2026, 01:00»."""
    a, b = _parse(starts), _parse(ends)
    if not a:
        return ""
    if not b:
        return _fmt_point(a)
    if a.date() == b.date():
        return f"{_fmt_point(a)}–{b:%H:%M}"
    return f"{_fmt_point(a)} — {_fmt_point(b)}"


def fmt_ts(s: str | None) -> str:
    dt = _parse(s)
    if not dt:
        return ""
    n = now_naive()
    if dt.date() == n.date():
        return f"сегодня, {dt:%H:%M}"
    if dt.date() == (n - timedelta(days=1)).date():
        return f"вчера, {dt:%H:%M}"
    return _fmt_point(dt, with_year=dt.year != n.year)


def fmt_short(t: str | None) -> str:
    return (t or "??????")[:6]


def fmt_host(u: str) -> str:
    netloc = urlparse(u).netloc
    return netloc or (u[:40] + "…" if len(u) > 40 else u)


def fmt_ymaps(place: str | None) -> str:
    """Ссылка для клика по «месту».

    Если в place лежит готовая ссылка (старые записи, где имя ещё не
    распозналось) — используем её как есть. Иначе строим поиск по адресу.
    Это чинит старые свидания, у которых ссылка осела в place, а place_url
    остался пустым: без проверки она уходила в ?text= как поисковый запрос.
    """
    p = (place or "").strip()
    if p.startswith(("http://", "https://")):
        return p
    return "https://yandex.ru/maps/?text=" + quote(p)


def placename(place: str | None) -> str:
    """Подпись «места» на карточке: сырую ссылку прячем за нейтральным текстом."""
    p = (place or "").strip()
    if p.startswith(("http://", "https://")):
        return "Место на карте"
    return p



def fmt_gcal(name: str, starts: str, ends: str | None,
             place: str | None, comment: str | None, links: list[str]) -> str:
    """Ссылка «добавить в Google Календарь» без скачивания файла."""
    s = _parse(starts).replace(tzinfo=MSK)
    e = _parse(ends).replace(tzinfo=MSK) if ends else s + timedelta(hours=2)
    f = "%Y%m%dT%H%M%SZ"
    q = {
        "action": "TEMPLATE",
        "text": name,
        "dates": f"{s.astimezone(timezone.utc).strftime(f)}/"
                 f"{e.astimezone(timezone.utc).strftime(f)}",
    }
    details = "\n".join(x for x in [comment or "", *links] if x)
    if details:
        q["details"] = details
    if place:
        q["location"] = place
    return "https://calendar.google.com/calendar/render?" + urlencode(q)


# ---------------------------------------------------------------------------
# Валидация форм
# ---------------------------------------------------------------------------

def parse_dt_local(s: str | None) -> str | None:
    """Парсит значение <input type=datetime-local>, нормализует до минут."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            continue
    raise HTTPException(400, "Неверный формат даты/времени")


def parse_birth_date(s: str | None) -> str | None:
    """Парсит <input type=date> для даты рождения. Пусто → None.

    Возвращает ISO yyyy-mm-dd. Отвергает будущее и заведомо нереальный возраст
    (>120 лет).
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Неверный формат даты рождения")
    today = now_naive().date()
    if d > today:
        raise HTTPException(400, "Дата рождения не может быть в будущем")
    age = (today - d).days // 365
    if age > 120:
        raise HTTPException(400, "Проверь дату рождения")
    return d.isoformat()


def normalize_period(starts: str | None, ends: str | None) -> tuple[str | None, str | None]:
    if ends and not starts:
        starts, ends = ends, None
    if starts and ends:
        if ends < starts:
            starts, ends = ends, starts
        if ends == starts:
            ends = None
    return starts, ends


def parse_links(raw: str | None) -> list[str]:
    out: list[str] = []
    for line in (raw or "").splitlines():
        u = line.strip()
        if not u:
            continue
        if not (u.startswith("http://") or u.startswith("https://")):
            u = "https://" + u
        if len(u) > 500:
            raise HTTPException(400, "Слишком длинная ссылка")
        out.append(u)
        if len(out) >= 10:
            break
    return out


def clean_text(s: str | None, limit: int, field: str, required: bool = False) -> str | None:
    s = (s or "").strip()
    if required and not s:
        raise HTTPException(400, f"Поле «{field}» обязательно")
    if len(s) > limit:
        raise HTTPException(400, f"Поле «{field}» слишком длинное (до {limit} символов)")
    return s or None


# ---------------------------------------------------------------------------
# Мини-разметка для комментариев и описаний: **жирный**, *курсив*,
# __подчёркнутый__, ~~зачёркнутый~~ и [ссылки](https://...).
# HTML экранируется ДО подстановки тегов — инъекции невозможны.
# ---------------------------------------------------------------------------

import re as _re

_RICH_LINK = _re.compile(r"\[([^\]\n]{1,100})\]\((https?://[^\s)]{1,500})\)")


def rich(text: str | None) -> Markup:
    if not text:
        return Markup("")
    s = str(escape(text))
    s = _RICH_LINK.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s, flags=_re.S)
    s = _re.sub(r"__(.+?)__", r"<u>\1</u>", s, flags=_re.S)
    s = _re.sub(r"~~(.+?)~~", r"<s>\1</s>", s, flags=_re.S)
    s = _re.sub(r"\*(.+?)\*", r"<i>\1</i>", s, flags=_re.S)
    return Markup(s)


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русские склонения: 1 свидание, 2 свидания, 5 свиданий."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# Варианты оплаты свидания (колонка pay_split): 0 — не указано, 1 — 50/50,
# 2 — платит автор, 3 — платит гость. Текст для карточки/превью.
PAY_LABELS = {1: "💸 50/50", 2: "👌 Я плачу", 3: "🫵 Ты платишь"}


def pay_label(value) -> str:
    """Подпись капсулы оплаты по значению pay_split (пусто, если не задано)."""
    try:
        return PAY_LABELS.get(int(value or 0), "")
    except (TypeError, ValueError):
        return ""

