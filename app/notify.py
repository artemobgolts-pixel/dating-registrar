"""Telegram-уведомления: rich-карточки, адресная доставка и настройки типов."""

import html
import logging
import os
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from config import BASE_URL, TG_MINI_APP_URL

log = logging.getLogger("notify")

TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
CHAT = os.getenv("TG_CHAT_ID", "").strip()

PREFERENCE_KEYS = ("votes", "questions", "proposals", "updates", "reminders", "reviews")
_OUTBOX_PREFERENCES = {
    "voting_deadline": "reminders",
    "winner_reminder": "reminders",
    "participant_withdrawal": "votes",
    "date_changed": "updates",
    "date_removed": "updates",
    "vote_removed": "updates",
    "review_prompt": "reviews",
    "review_received": "reviews",
}


def esc(s: str | None) -> str:
    return html.escape(s or "")


def notify(text: str, *, reply_markup: dict | None = None) -> None:
    """Отправляет сообщение боту. Вызывается из BackgroundTasks — ответ юзеру не ждёт."""
    if not TOKEN or not CHAT:
        return
    send_to(CHAT, text, reply_markup=reply_markup)


def get_preferences(conn, user_id: int) -> dict[str, bool]:
    """Возвращает настройки типов уведомлений; отсутствие строки = всё включено."""
    row = conn.execute(
        "SELECT votes, questions, proposals, updates, reminders, reviews "
        "FROM notification_preferences WHERE user_id=?", (user_id,),
    ).fetchone()
    if row is None:
        return {key: True for key in PREFERENCE_KEYS}
    return {key: bool(row[key]) for key in PREFERENCE_KEYS}


def save_preferences(conn, user_id: int, values: dict[str, bool]) -> None:
    """Атомарно сохраняет полный набор переключателей пользователя без commit."""
    if set(values) != set(PREFERENCE_KEYS):
        raise ValueError("all notification preferences are required")
    args = (user_id, *(1 if values[key] else 0 for key in PREFERENCE_KEYS))
    conn.execute(
        """
        INSERT INTO notification_preferences(
            user_id, votes, questions, proposals, updates, reminders, reviews
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            votes=excluded.votes,
            questions=excluded.questions,
            proposals=excluded.proposals,
            updates=excluded.updates,
            reminders=excluded.reminders,
            reviews=excluded.reviews,
            updated_at=CURRENT_TIMESTAMP
        """,
        args,
    )


def preference_enabled(conn, user_id: int, preference: str | None) -> bool:
    """Проверяет одну группу; неизвестная группа не должна молча отключать доставку."""
    if preference is None:
        return True
    if preference not in PREFERENCE_KEYS:
        raise ValueError(f"unknown notification preference: {preference}")
    row = conn.execute(
        f"SELECT {preference} FROM notification_preferences WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return row is None or bool(row[preference])


def preference_for_kind(kind: str) -> str:
    """Сопоставляет технический outbox-kind с пользовательским переключателем."""
    kind = (kind or "").strip()
    if kind in _OUTBOX_PREFERENCES:
        return _OUTBOX_PREFERENCES[kind]
    if kind.startswith("voting_"):
        return "updates"
    if "reminder" in kind:
        return "reminders"
    # Старые/редкие типы являются продуктовыми обновлениями. Такой фолбэк
    # сохраняет обратную совместимость и не создаёт скрытый шестой переключатель.
    return "updates"


def owner_chat_id(conn, owner_id: int, preference: str | None = None) -> int | None:
    """telegram_id владельца, КОМУ можно слать уведомления о действиях с его
    событием. None — если бот не подключён (вошёл только через виджет), забанен
    или это служебный легаси-владелец (telegram_id=0). Резолвить ОБЯЗАТЕЛЬНО
    внутри запроса: в BackgroundTask соединение уже закрыто."""
    if not preference_enabled(conn, owner_id, preference):
        return None
    row = conn.execute(
        "SELECT telegram_id FROM users "
        "WHERE id=? AND bot_linked=1 AND is_active=1 AND telegram_id<>0",
        (owner_id,)).fetchone()
    return row["telegram_id"] if row else None


# user_chat_id — то же правило, что и owner_chat_id (бот подключён, активен,
# не легаси). Отдельное имя для читаемости в местах «уведомить автора действия».
user_chat_id = owner_chat_id


def card(title: str, *lines: str) -> str:
    """Собирает аккуратное многострочное уведомление: жирный заголовок,
    затем строки (пустые/None пропускаются). Текст внутри строк должен быть
    уже экранирован через esc(), где это нужно."""
    body = "\n".join(l for l in lines if l)
    head = f"<b>{title}</b>"
    return f"{head}\n{body}" if body else head


def action_markup(label: str | None, url: str | None) -> dict | None:
    """Собирает безопасную primary-кнопку для Telegram rich message.

    Внутренние ссылки открываем как Web App: так на телефоне и Desktop сначала
    пройдёт initData-автологин. Внешние HTTPS-ссылки остаются обычными URL.
    """
    label = (label or "").strip()
    url = (url or "").strip()
    if not label and not url:
        return None
    if not label or not url:
        raise ValueError("action label and url must be set together")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Telegram action url must be absolute http(s)")
    if len(url) > 2048:
        raise ValueError("Telegram action url is too long")
    button: dict = {"text": label[:64], "style": "primary"}
    base = urlsplit(BASE_URL)
    try:
        parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        base_port = base.port or (443 if base.scheme == "https" else 80)
    except ValueError:
        raise ValueError("Telegram action url has an invalid port") from None
    same_origin = (parsed.scheme == base.scheme and parsed.hostname == base.hostname
                   and parsed_port == base_port)
    # Только пользовательские/кабинетные разделы допустимы как post-login next.
    # //evil и произвольные служебные роуты не попадут в Mini App redirect.
    path_query = (parsed.path
                  + (f"?{parsed.query}" if parsed.query else "")
                  + (f"#{parsed.fragment}" if parsed.fragment else ""))
    safe_next = (path_query.startswith(("/admin", "/c/", "/d/", "/u/"))
                 and not path_query.startswith("//"))
    mini = urlsplit(TG_MINI_APP_URL)
    if (same_origin and safe_next and mini.scheme == "https" and mini.netloc):
        sep = "&" if mini.query else "?"
        button["web_app"] = {"url": f"{TG_MINI_APP_URL}{sep}next={quote(path_query, safe='')}"}
    else:
        button["url"] = url
    return {"inline_keyboard": [[button]]}


_LEADING_TITLE = re.compile(r"^\s*<b>([^\n]*?)</b>(?:\n+|$)")


def _as_rich_html(text: str) -> str:
    """Переводит старую HTML-карточку в блоки Bot API 10.1 Rich Messages."""
    text = (text or "").strip()
    if not text:
        return "<p> </p>"
    if text.startswith(("<h1>", "<h2>", "<h3>", "<p>")):
        return text
    blocks: list[str] = []
    title = _LEADING_TITLE.match(text)
    if title:
        blocks.append(f"<h3>{title.group(1)}</h3>")
        text = text[title.end():].strip()
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if paragraph:
            blocks.append(f"<p>{paragraph.replace(chr(10), '<br>')}</p>")
    return "".join(blocks) or "<p> </p>"


def _legacy_markup(reply_markup: dict | None) -> dict | None:
    """Убирает Bot API 10.2 styling для fallback на старый sendMessage."""
    if not reply_markup:
        return None
    keyboard = []
    for row in reply_markup.get("inline_keyboard", []):
        keyboard.append([{k: v for k, v in button.items() if k != "style"}
                         for button in row])
    return {"inline_keyboard": keyboard}


def send_to(chat_id: int | str, text: str, *, reply_markup: dict | None = None) -> bool:
    """Шлёт сообщение конкретному chat_id (напр. тому, кто подтвердил вход).

    В отличие от notify() не требует CHAT — нужен только TOKEN. Ошибки не валят
    вызвавшего: вход не должен падать из-за недоставленного предупреждения.
    Возвращает True только после успешного ответа Telegram — очередь использует
    результат, чтобы не помечать недоставленное сообщение отправленным.
    """
    if not TOKEN:
        return False
    try:
        rich_payload = {
            "chat_id": chat_id,
            "rich_message": {
                "html": _as_rich_html(text),
                "skip_entity_detection": True,
            },
        }
        if reply_markup:
            rich_payload["reply_markup"] = reply_markup
        r = httpx.post(
            f"https://api.telegram.org/bot{TOKEN}/sendRichMessage",
            json=rich_payload,
            timeout=10,
        )
        if r.status_code < 400:
            return True
        # Bot API 10.1 может быть ещё недоступен в self-hosted gateway или rich
        # HTML может оказаться несовместимым. Уведомление не теряем.
        log.warning("Telegram sendRichMessage %s, fallback: %s",
                    r.status_code, r.text[:200])
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        fallback_markup = _legacy_markup(reply_markup)
        if fallback_markup:
            payload["reply_markup"] = fallback_markup
        fallback = httpx.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        if fallback.status_code < 400:
            return True
        log.warning("Telegram sendMessage %s: %s",
                    fallback.status_code, fallback.text[:200])
        return False
    except Exception as e:
        log.warning("Не удалось отправить сообщение в Telegram: %s", e)
        return False


def answer_callback(query_id: str, text: str = "") -> bool:
    """Закрывает индикатор inline-кнопки Telegram; ошибка не ломает вход."""
    if not TOKEN or not query_id:
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
            json={"callback_query_id": query_id, "text": text[:200]},
            timeout=10,
        )
        return r.status_code < 400
    except Exception as e:
        log.warning("Не удалось ответить на callback Telegram: %s", e)
        return False


def send_document(chat_id: int | str, path, caption: str | None = None,
                  filename: str | None = None) -> bool:
    """Шлёт файл документом конкретному chat_id (напр. снимок базы для бэкапа).

    Как и send_to, требует только TOKEN (не CHAT). Ошибки не валят вызвавшего:
    недоставленный бэкап логируем, но фоновую задачу не роняем. Возвращает True
    при успешной отправке. Блокирующий httpx — звать из потока, не из event loop.
    """
    if not TOKEN:
        return False
    path = str(path)
    try:
        with open(path, "rb") as f:
            data: dict[str, str] = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            r = httpx.post(
                f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                data=data,
                files={"document": (filename or os.path.basename(path), f)},
                timeout=60,
            )
        if r.status_code >= 400:
            log.warning("Telegram sendDocument %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("Не удалось отправить документ в Telegram: %s", e)
        return False


# Алёрты о сбоях (500-е) оператору. Дедупликация по тексту, чтобы всплеск
# одинаковых ошибок не превратился в флуд: одинаковый алёрт — не чаще раза в окно.
_ALERT_WINDOW = 300          # секунд
_alert_seen: dict[str, float] = {}


def alert(text: str) -> None:
    """Шлёт алёрт о сбое (тот же бот/чат, что и уведомления), с троттлингом.

    Блокирующий httpx.post — вызывать из потока/боновой задачи, не из event loop.
    """
    if not TOKEN or not CHAT:
        return
    now = time.monotonic()
    # чистим протухшие записи окна и проверяем дедуп
    for k, t in list(_alert_seen.items()):
        if now - t > _ALERT_WINDOW:
            _alert_seen.pop(k, None)
    if now - _alert_seen.get(text, -_ALERT_WINDOW) < _ALERT_WINDOW:
        return
    _alert_seen[text] = now
    notify(text)
