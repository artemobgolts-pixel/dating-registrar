"""Безопасные логи, корреляция запросов и error reporting.

Модуль намеренно не знает о FastAPI-приложении: его можно подключить до сборки
middleware, а функции очистки отдельно проверить без запуска сервера.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
import traceback
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


SERVICE_NAME = "date4you"

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_EXCEPTION_REQUEST_ID_ATTR = "_date4you_request_id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_TOKEN_PATH_RE = re.compile(r"(?P<prefix>/(?:c|d)/)[^/?#\s]+")
_TELEGRAM_URL_RE = re.compile(r"(?P<prefix>/bot)[^/?#\s]+")
_MEDIA_PATH_RE = re.compile(
    r"(?P<prefix>/(?:uploads|image|video|avatar)/)[^/?#\s]+"
)
_EMAIL_RE = re.compile(
    r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])"
)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_QUERY_SECRET_RE = re.compile(
    r"(?i)(?P<key>(?:code|state|token|secret|password|init_?data|email|phone|"
    r"username|first_?name|last_?name))="
    r"(?P<value>[^&#\s]*)"
)
_HEADER_SECRET_RE = re.compile(
    r"(?im)(?P<key>(?:authorization|cookie|set-cookie|"
    r"x-telegram-bot-api-secret-token))"
    r"(?P<sep>\s*[:=]\s*)(?P<value>[^\r\n]*)"
)

_SAFE_EXTRA_FIELDS = (
    "method",
    "route",
    "status_class",
    "status_code",
    "duration_ms",
    "provider",
    "operation",
    "outcome",
    "job",
    "result",
    "count",
    "exception_type",
)
_SECRET_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "set-cookie",
    "password",
    "secret",
    "secret_key",
    "token",
    "access_token",
    "refresh_token",
    "code",
    "state",
    "init_data",
    "initdata",
    "query_string",
    "email",
    "phone",
    "username",
    "first_name",
    "last_name",
}

_environment = "production"
_release = "unknown"


def current_request_id() -> str | None:
    return _request_id.get()


def valid_request_id(value: object) -> bool:
    return isinstance(value, str) and bool(_REQUEST_ID_RE.fullmatch(value))


def bind_request_id(incoming: str | None = None) -> tuple[str, Token]:
    """Принимает короткий безопасный ID или создаёт локальный случайный."""
    request_id = incoming if valid_request_id(incoming) else secrets.token_hex(16)
    return request_id, _request_id.set(request_id)


def reset_request_id(token: Token) -> None:
    _request_id.reset(token)


def attach_request_id(exc: BaseException, request_id: str) -> None:
    """Сохраняет безопасную корреляцию на exception до Sentry hook."""
    if not valid_request_id(request_id):
        return
    try:
        setattr(exc, _EXCEPTION_REQUEST_ID_ATTR, request_id)
    except (AttributeError, TypeError):
        pass


def sanitize_path(path: str) -> str:
    """Редактирует секретные path-параметры, сохраняя диагностическую форму."""
    value = _TOKEN_PATH_RE.sub(r"\g<prefix>{token}", path)
    value = _TELEGRAM_URL_RE.sub(r"\g<prefix>{redacted}", value)
    value = _MEDIA_PATH_RE.sub(r"\g<prefix>{filename}", value)
    return value


def sanitize_url(value: str) -> str:
    """Оставляет host/path, удаляет query/fragment и секретные сегменты."""
    try:
        parts = urlsplit(value)
    except (TypeError, ValueError):
        return redact_text(str(value))
    path = sanitize_path(parts.path)
    if parts.scheme or parts.netloc:
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return path


def sanitize_origin(value: str) -> str:
    """Возвращает только scheme + host без userinfo, path и query."""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = parts.port
    except (TypeError, ValueError):
        return "external"
    if not parts.scheme or not host:
        return "external"
    display_host = f"[{host}]" if ":" in host else host
    netloc = f"{display_host}:{port}" if port else display_host
    return urlunsplit((parts.scheme, netloc, "", "", ""))


def redact_text(value: str) -> str:
    """Best-effort страховка для exception text и старых prose-логов."""
    text = _URL_RE.sub(lambda match: sanitize_url(match.group(0)), str(value))
    text = _TELEGRAM_TOKEN_RE.sub("[REDACTED_TELEGRAM_TOKEN]", text)
    text = _TOKEN_PATH_RE.sub(r"\g<prefix>{token}", text)
    text = _TELEGRAM_URL_RE.sub(r"\g<prefix>{redacted}", text)
    text = _MEDIA_PATH_RE.sub(r"\g<prefix>{filename}", text)
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _QUERY_SECRET_RE.sub(r"\g<key>=[REDACTED]", text)
    text = _HEADER_SECRET_RE.sub(r"\g<key>\g<sep>[REDACTED]", text)
    return text


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    return redact_text(str(value))


def _safe_stacktrace(tb: Any, *, limit: int = 8) -> str:
    """Короткий стек без строк кода, где могут находиться секретные литералы."""
    frames = traceback.extract_tb(tb)
    omitted = max(0, len(frames) - limit)
    lines = [f"… пропущено кадров: {omitted}"] if omitted else []
    for frame in frames[-limit:]:
        lines.append(
            f'File "{redact_text(frame.filename)}", line {frame.lineno}, '
            f"in {frame.name}"
        )
    return "\n".join(lines)


def _record_payload(record: logging.LogRecord) -> dict[str, Any]:
    """Собирает единый безопасный payload для любого формата вывода."""
    payload: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "level": record.levelname.lower(),
        "service": SERVICE_NAME,
        "environment": _environment,
        "release": _release,
        "logger": record.name,
        "event": _safe_value(
            getattr(record, "event", f"{record.name}.{record.levelname.lower()}")
        ),
        "message": redact_text(record.getMessage()),
    }
    request_id = getattr(record, "request_id", None) or current_request_id()
    if valid_request_id(request_id):
        payload["request_id"] = request_id
    for field in _SAFE_EXTRA_FIELDS:
        if hasattr(record, field):
            payload[field] = _safe_value(getattr(record, field))
    if record.exc_info:
        exc_type, _exc, tb = record.exc_info
        payload["exception"] = {
            "type": exc_type.__name__ if exc_type else "Exception",
            # Exception message может содержать введённые человеком данные.
            # Для диагностики достаточно типа и frames без local variables.
            "stacktrace": _safe_stacktrace(tb),
        }
    return payload


class JsonFormatter(logging.Formatter):
    """Один JSON-объект на строку с allowlist дополнительных полей."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            _record_payload(record), ensure_ascii=False, separators=(",", ":")
        )


_PRETTY_LABELS = {
    "request_id": "запрос",
    "event": "событие",
    "provider": "сервис",
    "operation": "операция",
    "outcome": "результат",
    "job": "задача",
    "result": "итог",
    "count": "количество",
    "exception_type": "ошибка",
    "status_code": "статус",
    "duration_ms": "длительность_мс",
}
_PRETTY_FIELDS = (
    "request_id",
    "event",
    "provider",
    "operation",
    "outcome",
    "job",
    "result",
    "count",
    "exception_type",
    "status_code",
    "duration_ms",
)
_LEVEL_LABELS = {
    "debug": "DEBUG",
    "info": "INFO",
    "warning": "WARN",
    "error": "ERROR",
    "critical": "CRIT",
}
_LEVEL_COLORS = {
    "debug": "\x1b[36m",
    "info": "\x1b[32m",
    "warning": "\x1b[33m",
    "error": "\x1b[31m",
    "critical": "\x1b[1;31m",
}


def _one_line(value: Any) -> str:
    """Не даёт значениям ломать визуальную структуру одной записи."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


class PrettyFormatter(logging.Formatter):
    """Компактный человекочитаемый формат с устойчивым порядком полей."""

    def __init__(self, *, colors: bool = False) -> None:
        super().__init__()
        self.colors = colors

    def format(self, record: logging.LogRecord) -> str:
        payload = _record_payload(record)
        timestamp = str(payload["timestamp"]).replace("T", " ")
        level = str(payload["level"])
        level_label = _LEVEL_LABELS.get(level, level.upper())[:5].ljust(5)
        logger_name = _one_line(payload["logger"])
        message = _one_line(payload["message"])

        if self.colors:
            timestamp = f"\x1b[2m{timestamp}\x1b[0m"
            level_label = f"{_LEVEL_COLORS.get(level, '')}{level_label}\x1b[0m"
            logger_name = f"\x1b[36m{logger_name}\x1b[0m"

        parts = [f"{timestamp} │ {level_label} │ {logger_name} │ {message}"]
        is_http = any(field in payload for field in ("method", "route"))
        if is_http:
            request_summary = " ".join(
                _one_line(payload[field])
                for field in ("method", "route")
                if field in payload
            )
            if "status_code" in payload:
                request_summary += f" → {_one_line(payload['status_code'])}"
            if "duration_ms" in payload:
                request_summary += f" · {_one_line(payload['duration_ms'])} мс"
            parts.append(request_summary)

        for field in _PRETTY_FIELDS:
            if field in payload:
                if field == "event" and not hasattr(record, "event"):
                    continue
                if is_http and field in ("status_code", "duration_ms"):
                    continue
                parts.append(f"{_PRETTY_LABELS[field]}={_one_line(payload[field])}")

        exception = payload.get("exception")
        if isinstance(exception, Mapping):
            if "exception_type" not in payload:
                parts.append(
                    f"исключение={_one_line(exception.get('type', 'Exception'))}"
                )
            stacktrace = exception.get("stacktrace")
            if stacktrace:
                compact_stack = " ↳ ".join(
                    _one_line(line.strip())
                    for line in str(stacktrace).splitlines()
                    if line.strip()
                )
                parts.append(f"стек={compact_stack}")
        return " │ ".join(parts)


def _stream_supports_colors(stream: Any) -> bool:
    return (
        os.getenv("NO_COLOR") is None
        and hasattr(stream, "isatty")
        and stream.isatty()
    )


def configure_logging(
    *, level: str, environment: str, release: str, log_format: str = "pretty"
) -> None:
    global _environment, _release
    _environment = environment or "production"
    _release = release or "unknown"
    numeric_level = getattr(logging, (level or "INFO").upper(), logging.INFO)
    handler = logging.StreamHandler()
    if (log_format or "pretty").strip().lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            PrettyFormatter(colors=_stream_supports_colors(sys.stderr))
        )
    logging.basicConfig(level=numeric_level, handlers=[handler], force=True)
    # Uvicorn настраивает собственные handlers до импорта приложения. Убираем
    # их, чтобы startup/error тоже проходили через единый formatter.
    for logger_name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
    # Сырой request-target Uvicorn содержит query и секретные path-параметры.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    # httpx INFO содержит полный URL внешнего запроса (включая bot token и
    # пользовательские ссылки карт). Для диагностики есть bounded metrics и
    # наши structured dependency events без request-target.
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def route_template(request: Any) -> str:
    """Возвращает bounded FastAPI route template, никогда не сырой URL."""
    route = request.scope.get("route") if getattr(request, "scope", None) else None
    path = getattr(route, "path", None)
    if (not isinstance(path, str) or not path.startswith("/") or len(path) > 200
            or "?" in path or "://" in path):
        return "unmatched"
    return path


def _secret_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return (
        normalized in _SECRET_KEYS
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
    )


def _scrub_value(value: Any, *, key: str | None = None) -> Any:
    if key and _secret_key(key):
        return "[Filtered]"
    if isinstance(value, Mapping):
        return {str(k): _scrub_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def scrub_sentry_event(
    event: dict[str, Any], hint: Any = None,
) -> dict[str, Any] | None:
    """Удаляет body/query/cookies и нормализует URL до отправки наружу."""
    original_extra = event.get("extra")
    if (event.get("logger") == "app" and isinstance(original_extra, Mapping)
            and original_extra.get("event") == "http_request_unhandled_error"):
        # Этот ERROR уже будет захвачен ASGI integration как настоящий
        # unhandled exception. Отбрасываем logging-копию без двойного события.
        return None
    scrubbed = _scrub_value(event)
    transaction_info = event.get("transaction_info")
    transaction_source = (
        transaction_info.get("source")
        if isinstance(transaction_info, Mapping) else None
    )
    transaction = event.get("transaction")
    route = None
    if (transaction_source == "route" and isinstance(transaction, str)
            and transaction.startswith("/") and len(transaction) <= 200
            and "?" not in transaction and "://" not in transaction):
        route = sanitize_path(transaction)
    if isinstance(transaction, str):
        scrubbed["transaction"] = route or "unmatched"
    else:
        scrubbed.pop("transaction", None)

    request = scrubbed.get("request")
    original_request = event.get("request")
    if isinstance(request, dict):
        safe_request: dict[str, Any] = {}
        if isinstance(original_request, Mapping) and isinstance(
            original_request.get("url"), str
        ):
            origin = sanitize_origin(original_request["url"])
            safe_request["url"] = origin + (route or "/unmatched")
        if isinstance(original_request, Mapping):
            method = original_request.get("method")
            if isinstance(method, str) and re.fullmatch(
                r"[A-Z]{3,10}", method.upper()
            ):
                safe_request["method"] = method.upper()
        # Request превращаем в строгий allowlist: даже значения безобидных на
        # вид headers/content-type и будущие поля SDK контролирует клиент.
        if safe_request:
            scrubbed["request"] = safe_request
        else:
            scrubbed.pop("request", None)
    scrubbed.pop("user", None)

    scrubbed.pop("message", None)
    scrubbed.pop("logentry", None)

    exception = scrubbed.get("exception")
    if isinstance(exception, dict) and isinstance(exception.get("values"), list):
        for value in exception["values"]:
            if isinstance(value, dict):
                # Тип + frames остаются, произвольный exception message — нет.
                value.pop("value", None)

    # Trace сохраняет op/status/duration, но не SQL, URL и arbitrary span data.
    spans = scrubbed.get("spans")
    if isinstance(spans, list):
        for span in spans:
            if not isinstance(span, dict):
                continue
            span.pop("data", None)
            operation = str(span.get("op") or "operation")
            span["description"] = (
                operation if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", operation)
                else "operation"
            )

    # ASGI integration может вызвать hook уже после reset ContextVar. В таком
    # случае берём только ранее валидированный ID из structured extra.
    candidates = [current_request_id()]
    if isinstance(hint, Mapping):
        exc_info = hint.get("exc_info")
        if isinstance(exc_info, tuple) and len(exc_info) >= 2:
            candidates.append(
                getattr(exc_info[1], _EXCEPTION_REQUEST_ID_ATTR, None)
            )
    extra = scrubbed.get("extra")
    if isinstance(extra, Mapping):
        candidates.append(extra.get("request_id"))
    request_id = next((value for value in candidates if valid_request_id(value)), None)
    tags = scrubbed.get("tags")
    if isinstance(tags, dict) and "request_id" in tags:
        if valid_request_id(tags["request_id"]):
            request_id = request_id or tags["request_id"]
        else:
            tags.pop("request_id", None)
    # В Sentry extra оставляем только bounded машинные поля. LoggingIntegration
    # видит исходный LogRecord, а не allowlist JsonFormatter.
    safe_extra: dict[str, Any] = {}
    if isinstance(original_extra, Mapping):
        for key in ("event", "provider", "operation", "outcome", "job", "result",
                    "exception_type", "method", "status_class"):
            value = original_extra.get(key)
            if isinstance(value, str) and re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,64}", value
            ):
                safe_extra[key] = value
        for key in ("status_code", "duration_ms", "count"):
            value = original_extra.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                safe_extra[key] = value
    if request_id:
        safe_extra["request_id"] = request_id
        scrubbed["tags"] = {"request_id": request_id}
    else:
        scrubbed.pop("tags", None)
    if safe_extra:
        scrubbed["extra"] = safe_extra
    else:
        scrubbed.pop("extra", None)
    scrubbed.pop("fingerprint", None)
    if isinstance(transaction, str) or isinstance(transaction_info, Mapping):
        scrubbed["transaction_info"] = {
            "source": "route" if route else "url",
        }
    else:
        scrubbed.pop("transaction_info", None)
    return scrubbed


def scrub_sentry_breadcrumb(
    breadcrumb: dict[str, Any], hint: Any = None
) -> dict[str, Any]:
    scrubbed = _scrub_value(breadcrumb)
    data = scrubbed.get("data")
    original_data = breadcrumb.get("data")
    if isinstance(data, dict) and isinstance(original_data, Mapping):
        safe_data: dict[str, Any] = {}
        url = original_data.get("url")
        if isinstance(url, str):
            safe_data["url"] = sanitize_origin(url)
        method = original_data.get("method")
        if isinstance(method, str) and re.fullmatch(r"[A-Z]{3,10}", method.upper()):
            safe_data["method"] = method.upper()
        status_code = original_data.get("status_code")
        if isinstance(status_code, int) and 100 <= status_code <= 599:
            safe_data["status_code"] = status_code
        scrubbed["data"] = safe_data
    # Произвольный breadcrumb message может содержать имя/текст формы.
    scrubbed.pop("message", None)
    return scrubbed


def init_sentry(
    *, dsn: str, environment: str, release: str, traces_sample_rate: float
) -> bool:
    """Подключает Sentry, если задан DSN; ошибка телеметрии не валит продукт."""
    if not dsn:
        return False
    log = logging.getLogger("observability")
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        log.error(
            "Sentry настроен, но SDK не установлен",
            extra={"event": "sentry_dependency_missing"},
        )
        return False
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release or None,
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            traces_sample_rate=traces_sample_rate,
            trace_propagation_targets=[],
            propagate_traces=False,
            integrations=[LoggingIntegration(
                level=None,
                event_level=logging.ERROR,
                sentry_logs_level=None,
                capture_sentry_logs=False,
            )],
            before_send=scrub_sentry_event,
            before_send_transaction=scrub_sentry_event,
            before_breadcrumb=scrub_sentry_breadcrumb,
        )
    except Exception:
        log.exception(
            "Не удалось инициализировать Sentry",
            extra={"event": "sentry_initialization_failed"},
        )
        return False
    log.info("Sentry подключён", extra={"event": "sentry_initialized"})
    return True
