"""Prometheus-метрики HTTP, auth, внешних зависимостей и фоновых задач.

Модуль намеренно держит все label-наборы маленькими и предсказуемыми. В них
никогда не попадают request/user id, сырые URL, токены или тексты исключений.
Маршрут HTTP берётся только из сопоставленного шаблона Starlette/FastAPI; для
404 используется единая метка ``__unmatched__``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.responses import Response


NAMESPACE = "date4you"
UNMATCHED_ROUTE = "__unmatched__"

HTTP_METHODS = frozenset({
    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "OTHER",
})
HTTP_STATUS_CLASSES = frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "unknown"})

# Значения извлекаются только из этого набора. Неизвестное имя не создаёт новую
# серию, а сворачивается в ``other`` — это защищает от случайной кардинальности.
DEPENDENCIES = frozenset({
    "telegram", "oauth_discord", "oauth_google", "oauth_yandex", "maps",
    "backup", "other",
})
DEPENDENCY_OPERATIONS = frozenset({
    "send_message", "send_document", "send_media", "answer_callback", "get_me",
    "set_webhook", "set_menu", "token_exchange", "userinfo",
    "resolve_redirect", "upload", "other",
})
DEPENDENCY_RESULTS = frozenset({
    "success", "failure", "timeout", "exception", "cancelled",
})

AUTH_FLOWS = frozenset({
    "telegram_widget", "miniapp", "deep_link", "oauth",
})
AUTH_PROVIDERS = frozenset({"telegram", "discord", "google", "yandex"})
AUTH_RESULTS = frozenset({
    "success", "cancelled", "expired", "invalid", "banned", "conflict",
    "rate_limited", "provider_error",
})

OUTBOX_STATES = ("pending", "due", "claimed")
OUTBOX_RESULTS = ("sent", "failed", "deferred", "expired", "skipped")

BACKGROUND_JOBS = frozenset({
    "notification_outbox", "voting_close", "autoarchive", "backup",
    "repair_places", "rate_limit_gc", "other",
})
BACKGROUND_RESULTS = frozenset({"success", "failure", "cancelled"})
COMMUNITY_FEED_MODES = frozenset({
    "general", "personalized", "chronological", "search",
})

HTTP_DURATION_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
DEPENDENCY_DURATION_BUCKETS = (0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
                               10.0, 30.0, 60.0)
JOB_DURATION_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0, 120.0, 600.0)
COMMUNITY_CANDIDATE_BUCKETS = (0, 1, 6, 12, 24, 60, 120, 240, 500, 1000, 2000)


def _bounded(value: object, allowed: frozenset[str], fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def _method_label(method: object) -> str:
    normalized = str(method or "").strip().upper()
    return normalized if normalized in HTTP_METHODS else "OTHER"


def _status_class(status_code: object) -> str:
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return "unknown"
    label = f"{code // 100}xx"
    return label if label in HTTP_STATUS_CLASSES else "unknown"


def _route_template(scope: Mapping[str, Any]) -> str:
    """Возвращает только зарегистрированный шаблон, никогда сырой request path."""
    route = scope.get("route")
    template = getattr(route, "path", None)
    if not isinstance(template, str) or not template.startswith("/"):
        return UNMATCHED_ROUTE
    # Route-шаблоны задаются кодом и потому конечны. Ограничение длины защищает
    # от стороннего ASGI-компонента, который положил в scope произвольный объект.
    if len(template) > 200 or "?" in template or "://" in template:
        return UNMATCHED_ROUTE
    return template


class ApplicationMetrics:
    """Набор collector-ов; отдельный registry упрощает изолированные тесты."""

    def __init__(
        self,
        registry: CollectorRegistry = REGISTRY,
        *,
        monotonic: Callable[[], float] = time.perf_counter,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self.registry = registry
        self.monotonic = monotonic
        self.wall_time = wall_time

        self.http_requests = Counter(
            f"{NAMESPACE}_http_requests_total",
            "Completed HTTP requests.",
            ("method", "route", "status_class"),
            registry=registry,
        )
        self.http_duration = Histogram(
            f"{NAMESPACE}_http_request_duration_seconds",
            "HTTP request duration in seconds.",
            ("method", "route", "status_class"),
            buckets=HTTP_DURATION_BUCKETS,
            registry=registry,
        )
        self.dependency_requests = Counter(
            f"{NAMESPACE}_dependency_requests_total",
            "Completed external dependency operations.",
            ("dependency", "operation", "result"),
            registry=registry,
        )
        self.dependency_duration = Histogram(
            f"{NAMESPACE}_dependency_request_duration_seconds",
            "External dependency operation duration in seconds.",
            ("dependency", "operation", "result"),
            buckets=DEPENDENCY_DURATION_BUCKETS,
            registry=registry,
        )
        self.auth_attempts = Counter(
            f"{NAMESPACE}_auth_attempts_total",
            "Authentication funnel outcomes.",
            ("flow", "provider", "result"),
            registry=registry,
        )
        self.outbox_processed = Counter(
            f"{NAMESPACE}_notification_outbox_processed_total",
            "Notification outbox processing outcomes.",
            ("result",),
            registry=registry,
        )
        self.outbox_messages = Gauge(
            f"{NAMESPACE}_notification_outbox_messages",
            "Current notification outbox messages by bounded state.",
            ("state",),
            registry=registry,
        )
        self.outbox_oldest_due_age = Gauge(
            f"{NAMESPACE}_notification_outbox_oldest_due_age_seconds",
            "Age of the oldest due notification, or zero when none are due.",
            registry=registry,
        )
        self.background_runs = Counter(
            f"{NAMESPACE}_background_job_runs_total",
            "Completed background job runs.",
            ("job", "result"),
            registry=registry,
        )
        self.background_duration = Histogram(
            f"{NAMESPACE}_background_job_duration_seconds",
            "Background job run duration in seconds.",
            ("job", "result"),
            buckets=JOB_DURATION_BUCKETS,
            registry=registry,
        )
        self.background_running = Gauge(
            f"{NAMESPACE}_background_job_running",
            "Whether a background job is currently running.",
            ("job",),
            registry=registry,
        )
        self.background_last_success = Gauge(
            f"{NAMESPACE}_background_job_last_success_timestamp_seconds",
            "Unix timestamp of the last successful background job run.",
            ("job",),
            registry=registry,
        )
        # Ответы на два эксплуатационных вопроса: включается ли персональная
        # ветка и хватает ли ей кандидатов. Latency/error rate уже покрывает
        # общий RED middleware для /admin/community.
        self.community_feed_pages = Counter(
            f"{NAMESPACE}_community_feed_pages_total",
            "Rendered community feed pages by bounded ranking mode.",
            ("mode",),
            registry=registry,
        )
        self.community_feed_events = Counter(
            f"{NAMESPACE}_community_feed_events_total",
            "Events returned by community feed ranking mode.",
            ("mode",),
            registry=registry,
        )
        self.community_feed_candidates = Histogram(
            f"{NAMESPACE}_community_feed_candidates",
            "Candidate rows considered for a community feed page.",
            ("mode",),
            buckets=COMMUNITY_CANDIDATE_BUCKETS,
            registry=registry,
        )

        # Gauge-серии должны присутствовать с нуля уже до первого прохода.
        for state in OUTBOX_STATES:
            self.outbox_messages.labels(state=state).set(0)
        self.outbox_oldest_due_age.set(0)
        for job in BACKGROUND_JOBS:
            self.background_running.labels(job=job).set(0)
            self.background_last_success.labels(job=job).set(0)

    def observe_http(
        self,
        *,
        method: object,
        route: str,
        status_code: object,
        duration_seconds: float,
    ) -> None:
        labels = {
            "method": _method_label(method),
            "route": route,
            "status_class": _status_class(status_code),
        }
        self.http_requests.labels(**labels).inc()
        self.http_duration.labels(**labels).observe(max(0.0, duration_seconds))

    def dependency(self, dependency: object, operation: object) -> "DependencyTimer":
        return DependencyTimer(self, dependency, operation)

    def observe_dependency(
        self,
        *,
        dependency: object,
        operation: object,
        result: object,
        duration_seconds: float,
    ) -> None:
        labels = {
            "dependency": _bounded(dependency, DEPENDENCIES, "other"),
            "operation": _bounded(operation, DEPENDENCY_OPERATIONS, "other"),
            "result": _bounded(result, DEPENDENCY_RESULTS, "failure"),
        }
        self.dependency_requests.labels(**labels).inc()
        self.dependency_duration.labels(**labels).observe(max(0.0, duration_seconds))

    def observe_auth(
        self, *, flow: object, provider: object, result: object,
    ) -> None:
        # ``other`` остаётся единственным резервным значением для неизвестного
        # flow/provider: вход пользователя не должен ломаться из-за опечатки в
        # telemetry, но сырое значение также не должно создавать новую серию.
        labels = {
            "flow": _bounded(flow, AUTH_FLOWS, "other"),
            "provider": _bounded(provider, AUTH_PROVIDERS, "other"),
            "result": _bounded(result, AUTH_RESULTS, "provider_error"),
        }
        self.auth_attempts.labels(**labels).inc()

    def observe_outbox_batch(self, stats: object) -> None:
        for result in OUTBOX_RESULTS:
            try:
                count = max(0, int(getattr(stats, result, 0)))
            except (TypeError, ValueError):
                count = 0
            if count:
                self.outbox_processed.labels(result=result).inc(count)

    def set_outbox_state(
        self,
        *,
        pending: int,
        due: int,
        claimed: int,
        oldest_due_age_seconds: float,
    ) -> None:
        values = {"pending": pending, "due": due, "claimed": claimed}
        for state, value in values.items():
            self.outbox_messages.labels(state=state).set(max(0, int(value)))
        self.outbox_oldest_due_age.set(max(0.0, float(oldest_due_age_seconds)))

    def background_job(self, job: object) -> "BackgroundJobTimer":
        return BackgroundJobTimer(self, job)

    def observe_background_job(
        self, *, job: object, result: object, duration_seconds: float,
    ) -> None:
        job_label = _bounded(job, BACKGROUND_JOBS, "other")
        result_label = _bounded(result, BACKGROUND_RESULTS, "failure")
        labels = {"job": job_label, "result": result_label}
        self.background_runs.labels(**labels).inc()
        self.background_duration.labels(**labels).observe(
            max(0.0, duration_seconds)
        )
        if result_label == "success":
            self.background_last_success.labels(job=job_label).set(self.wall_time())

    def observe_community_feed(
        self, *, mode: object, candidate_count: int, returned_count: int,
    ) -> None:
        mode_label = _bounded(mode, COMMUNITY_FEED_MODES, "general")
        self.community_feed_pages.labels(mode=mode_label).inc()
        self.community_feed_events.labels(mode=mode_label).inc(
            max(0, int(returned_count))
        )
        self.community_feed_candidates.labels(mode=mode_label).observe(
            max(0, int(candidate_count))
        )


class DependencyTimer:
    """Контекст внешнего вызова; false/HTTP-error помечается через ``fail``."""

    def __init__(self, metrics: ApplicationMetrics, dependency: object,
                 operation: object) -> None:
        self.metrics = metrics
        self.dependency = dependency
        self.operation = operation
        self.result = "success"
        self.started = 0.0

    def __enter__(self) -> "DependencyTimer":
        self.started = self.metrics.monotonic()
        return self

    def fail(self, result: str = "failure") -> None:
        self.result = _bounded(result, DEPENDENCY_RESULTS, "failure")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc_type is not None:
            if issubclass(exc_type, asyncio.CancelledError):
                self.result = "cancelled"
            elif (issubclass(exc_type, TimeoutError)
                  or "timeout" in exc_type.__name__.lower()):
                self.result = "timeout"
            else:
                self.result = "exception"
        self.metrics.observe_dependency(
            dependency=self.dependency,
            operation=self.operation,
            result=self.result,
            duration_seconds=self.metrics.monotonic() - self.started,
        )
        return False


class BackgroundJobTimer:
    """Контекст одного прохода фоновой задачи с running/last-success сигналами."""

    def __init__(self, metrics: ApplicationMetrics, job: object) -> None:
        self.metrics = metrics
        self.job = _bounded(job, BACKGROUND_JOBS, "other")
        self.started = 0.0

    def __enter__(self) -> "BackgroundJobTimer":
        self.started = self.metrics.monotonic()
        self.metrics.background_running.labels(job=self.job).inc()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.metrics.background_running.labels(job=self.job).dec()
        if exc_type is None:
            result = "success"
        elif issubclass(exc_type, asyncio.CancelledError):
            result = "cancelled"
        else:
            result = "failure"
        self.metrics.observe_background_job(
            job=self.job,
            result=result,
            duration_seconds=self.metrics.monotonic() - self.started,
        )
        return False


APP_METRICS = ApplicationMetrics()


def track_dependency(dependency: object, operation: object) -> DependencyTimer:
    return APP_METRICS.dependency(dependency, operation)


def track_background_job(job: object) -> BackgroundJobTimer:
    return APP_METRICS.background_job(job)


def observe_auth(*, flow: object, provider: object, result: object) -> None:
    APP_METRICS.observe_auth(flow=flow, provider=provider, result=result)


def observe_outbox_batch(stats: object) -> None:
    APP_METRICS.observe_outbox_batch(stats)


def set_outbox_state(*, pending: int, due: int, claimed: int,
                     oldest_due_age_seconds: float) -> None:
    APP_METRICS.set_outbox_state(
        pending=pending,
        due=due,
        claimed=claimed,
        oldest_due_age_seconds=oldest_due_age_seconds,
    )


def observe_community_feed(*, mode: object, candidate_count: int,
                           returned_count: int) -> None:
    APP_METRICS.observe_community_feed(
        mode=mode,
        candidate_count=candidate_count,
        returned_count=returned_count,
    )


class PrometheusMiddleware:
    """ASGI RED middleware; label route берётся после завершения роутинга."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        metrics: ApplicationMetrics | None = None,
        exclude_paths: tuple[str, ...] = ("/metrics", "/metrics/"),
    ) -> None:
        self.app = app
        self.metrics = metrics or APP_METRICS
        self.exclude_paths = frozenset(exclude_paths)

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any],
                       send: Callable[..., Any]) -> None:
        if scope.get("type") != "http" or scope.get("path") in self.exclude_paths:
            await self.app(scope, receive, send)
            return

        started = self.metrics.monotonic()
        status_code = 500

        async def send_with_status(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            self.metrics.observe_http(
                method=scope.get("method"),
                route=_route_template(scope),
                status_code=status_code,
                duration_seconds=self.metrics.monotonic() - started,
            )


async def prometheus_endpoint(_request: object) -> Response:
    """Внутренний endpoint; внешний доступ должен ограничивать reverse proxy."""
    return Response(generate_latest(APP_METRICS.registry),
                    media_type=CONTENT_TYPE_LATEST)
