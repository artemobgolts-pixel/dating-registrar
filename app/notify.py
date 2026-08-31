"""Telegram-уведомления: rich-карточки, адресная доставка и настройки типов."""

import html
import json
import logging
import os
import re
import time
from urllib.parse import quote, urlsplit

import httpx

import metrics
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


def _telegram_post(operation: str, *args, **kwargs) -> httpx.Response:
    """Один bounded RED-сигнал на попытку Telegram Bot API."""
    with metrics.track_dependency("telegram", operation) as observation:
        response = httpx.post(*args, **kwargs)
        if response.status_code >= 400:
            observation.fail()
        return response


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
        r = _telegram_post(
            "send_message",
            f"https://api.telegram.org/bot{TOKEN}/sendRichMessage",
            json=rich_payload,
            timeout=10,
        )
        if r.status_code < 400:
            return True
        # Bot API 10.1 может быть ещё недоступен в self-hosted gateway или rich
        # HTML может оказаться несовместимым. Уведомление не теряем.
        log.warning(
            "Telegram Rich Message недоступен, использую fallback",
            extra={"event": "telegram_rich_message_fallback", "provider": "telegram",
                   "operation": "send_message", "status_code": r.status_code,
                   "status_class": f"{r.status_code // 100}xx", "outcome": "fallback"},
        )
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        fallback_markup = _legacy_markup(reply_markup)
        if fallback_markup:
            payload["reply_markup"] = fallback_markup
        fallback = _telegram_post(
            "send_message",
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        if fallback.status_code < 400:
            return True
        log.warning(
            "Telegram не принял сообщение",
            extra={"event": "telegram_send_failed", "provider": "telegram",
                   "operation": "send_message", "status_code": fallback.status_code,
                   "status_class": f"{fallback.status_code // 100}xx", "outcome": "failure"},
        )
        return False
    except Exception as exc:
        log.warning(
            "Не удалось отправить сообщение в Telegram",
            extra={"event": "telegram_send_failed", "provider": "telegram",
                   "operation": "send_message",
                   "exception_type": type(exc).__name__, "outcome": "failure"},
        )
        return False


def send_photo_to(chat_id: int | str, path, caption: str,
                  *, reply_markup: dict | None = None) -> bool:
    """Шлёт приветствие нативным фото-сообщением Telegram.

    ``sendPhoto`` ставит медиа вплотную к верхнему краю пузыря, тогда как
    Rich Message визуально резервирует над первым media-блоком внутренний отступ.
    Актуальный Bot API принимает styled inline-кнопки и здесь; для старого или
    self-hosted gateway один раз повторяем запрос без поля ``style``. Если само
    фото отправить нельзя, остаётся надёжный текстовый fallback.
    """
    if not TOKEN:
        return False
    path = str(path)
    try:
        data: dict[str, str] = {
            "chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML",
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(
                reply_markup, ensure_ascii=False, separators=(",", ":"),
            )
        with open(path, "rb") as photo:
            response = _telegram_post(
                "send_media",
                f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                data=data,
                files={"photo": (os.path.basename(path), photo, "image/png")},
                timeout=30,
            )
        if response.status_code < 400:
            return True
        log.warning(
            "Telegram не принял styled photo, использую fallback",
            extra={"event": "telegram_media_fallback", "provider": "telegram",
                   "operation": "send_media", "status_code": response.status_code,
                   "status_class": f"{response.status_code // 100}xx", "outcome": "fallback"},
        )

        legacy_data: dict[str, str] = {
            "chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML",
        }
        fallback_markup = _legacy_markup(reply_markup)
        if fallback_markup:
            legacy_data["reply_markup"] = json.dumps(
                fallback_markup, ensure_ascii=False, separators=(",", ":"),
            )
        with open(path, "rb") as photo:
            response = _telegram_post(
                "send_media",
                f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                data=legacy_data,
                files={"photo": (os.path.basename(path), photo, "image/png")},
                timeout=30,
            )
        if response.status_code < 400:
            return True
        log.warning(
            "Telegram не принял photo, использую текстовый fallback",
            extra={"event": "telegram_media_failed", "provider": "telegram",
                   "operation": "send_media", "status_code": response.status_code,
                   "status_class": f"{response.status_code // 100}xx", "outcome": "failure"},
        )
    except Exception as exc:
        log.warning(
            "Не удалось отправить изображение в Telegram, использую fallback",
            extra={"event": "telegram_media_fallback", "provider": "telegram",
                   "operation": "send_media",
                   "exception_type": type(exc).__name__, "outcome": "fallback"},
        )
    return send_to(chat_id, caption, reply_markup=reply_markup)


def send_video_to(chat_id: int | str, path, caption: str,
                  *, reply_markup: dict | None = None) -> bool:
    """Шлёт приветственный MP4 нативным ``sendVideo``.

    Сначала сохраняем актуальные styled-кнопки Bot API, затем один раз
    повторяем запрос с legacy-разметкой. Любая ошибка медиа оставляет текстовый
    fallback, чтобы вход через Telegram не зависел от доставки ролика.
    """
    if not TOKEN:
        return False
    path = str(path)

    def upload(markup: dict | None):
        data: dict[str, str] = {
            "chat_id": str(chat_id),
            "caption": caption,
            "parse_mode": "HTML",
            "supports_streaming": "true",
        }
        if markup:
            data["reply_markup"] = json.dumps(
                markup, ensure_ascii=False, separators=(",", ":"),
            )
        with open(path, "rb") as video:
            return _telegram_post(
                "send_media",
                f"https://api.telegram.org/bot{TOKEN}/sendVideo",
                data=data,
                files={"video": (os.path.basename(path), video, "video/mp4")},
                timeout=45,
            )

    try:
        response = upload(reply_markup)
        if response.status_code < 400:
            return True
        log.warning(
            "Telegram не принял styled video, использую fallback",
            extra={"event": "telegram_media_fallback", "provider": "telegram",
                   "operation": "send_media", "status_code": response.status_code,
                   "status_class": f"{response.status_code // 100}xx", "outcome": "fallback"},
        )

        legacy_markup = _legacy_markup(reply_markup)
        response = upload(legacy_markup)
        if response.status_code < 400:
            return True
        log.warning(
            "Telegram не принял video, использую текстовый fallback",
            extra={"event": "telegram_media_failed", "provider": "telegram",
                   "operation": "send_media", "status_code": response.status_code,
                   "status_class": f"{response.status_code // 100}xx", "outcome": "failure"},
        )
    except Exception as exc:
        log.warning(
            "Не удалось отправить видео в Telegram, использую fallback",
            extra={"event": "telegram_media_fallback", "provider": "telegram",
                   "operation": "send_media",
                   "exception_type": type(exc).__name__, "outcome": "fallback"},
        )
    return send_to(chat_id, caption, reply_markup=reply_markup)


def answer_callback(query_id: str, text: str = "") -> bool:
    """Закрывает индикатор inline-кнопки Telegram; ошибка не ломает вход."""
    if not TOKEN or not query_id:
        return False
    try:
        r = _telegram_post(
            "answer_callback",
            f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
            json={"callback_query_id": query_id, "text": text[:200]},
            timeout=10,
        )
        return r.status_code < 400
    except Exception as exc:
        log.warning(
            "Не удалось ответить на callback Telegram",
            extra={"event": "telegram_callback_failed", "provider": "telegram",
                   "operation": "answer_callback",
                   "exception_type": type(exc).__name__, "outcome": "failure"},
        )
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
            r = _telegram_post(
                "send_document",
                f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                data=data,
                files={"document": (filename or os.path.basename(path), f)},
                timeout=60,
            )
        if r.status_code >= 400:
            log.warning(
                "Telegram не принял документ",
                extra={"event": "telegram_send_failed", "provider": "telegram",
                       "operation": "send_document", "status_code": r.status_code,
                       "status_class": f"{r.status_code // 100}xx", "outcome": "failure"},
            )
            return False
        return True
    except Exception as exc:
        log.warning(
            "Не удалось отправить документ в Telegram",
            extra={"event": "telegram_send_failed", "provider": "telegram",
                   "operation": "send_document",
                   "exception_type": type(exc).__name__, "outcome": "failure"},
        )
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
