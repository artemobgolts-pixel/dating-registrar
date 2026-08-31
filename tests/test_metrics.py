#!/usr/bin/env python3
"""Точечные тесты Prometheus-сигналов и outbox-инструментации."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-metrics-import-")
os.environ["DATA_DIR"] = _IMPORT_DATA.name
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-metrics")

import db  # noqa: E402
import metrics  # noqa: E402
import notification_outbox as outbox  # noqa: E402
import tasks  # noqa: E402
from prometheus_client import CollectorRegistry, generate_latest  # noqa: E402


NOW = "2030-01-01T10:00:00"


class ApplicationMetricsTests(unittest.TestCase):
    def test_dependency_timer_classifies_library_timeouts_without_raw_labels(self):
        registry = CollectorRegistry()
        clock = iter((10.0, 10.75))
        app_metrics = metrics.ApplicationMetrics(
            registry, monotonic=lambda: next(clock),
        )

        class ClientTimeoutError(Exception):
            pass

        with self.assertRaises(ClientTimeoutError):
            with app_metrics.dependency("telegram", "send_message"):
                raise ClientTimeoutError("private request URL")

        labels = {
            "dependency": "telegram", "operation": "send_message",
            "result": "timeout",
        }
        self.assertEqual(registry.get_sample_value(
            "date4you_dependency_requests_total", labels,
        ), 1.0)
        payload = generate_latest(registry).decode("utf-8")
        self.assertNotIn("private request URL", payload)

    def test_background_timer_bounds_job_and_records_success(self):
        registry = CollectorRegistry()
        clock = iter((10.0, 12.5))
        app_metrics = metrics.ApplicationMetrics(
            registry,
            monotonic=lambda: next(clock),
            wall_time=lambda: 1234.0,
        )

        with app_metrics.background_job("user-controlled-job-name"):
            pass

        labels = {"job": "other", "result": "success"}
        self.assertEqual(registry.get_sample_value(
            "date4you_background_job_runs_total", labels,
        ), 1.0)
        self.assertEqual(registry.get_sample_value(
            "date4you_background_job_duration_seconds_sum", labels,
        ), 2.5)
        self.assertEqual(registry.get_sample_value(
            "date4you_background_job_running", {"job": "other"},
        ), 0.0)
        self.assertEqual(registry.get_sample_value(
            "date4you_background_job_last_success_timestamp_seconds",
            {"job": "other"},
        ), 1234.0)
        self.assertNotIn("user-controlled-job-name", generate_latest(
            registry,
        ).decode("utf-8"))

    def test_background_gauges_exist_before_first_run(self):
        registry = CollectorRegistry()
        metrics.ApplicationMetrics(registry)

        for job in metrics.BACKGROUND_JOBS:
            with self.subTest(job=job):
                self.assertEqual(registry.get_sample_value(
                    "date4you_background_job_running", {"job": job},
                ), 0.0)
                self.assertEqual(registry.get_sample_value(
                    "date4you_background_job_last_success_timestamp_seconds",
                    {"job": job},
                ), 0.0)

    def test_background_timer_records_failure_and_resets_running(self):
        registry = CollectorRegistry()
        clock = iter((4.0, 4.25))
        app_metrics = metrics.ApplicationMetrics(
            registry,
            monotonic=lambda: next(clock),
            wall_time=lambda: 999.0,
        )

        with self.assertRaisesRegex(RuntimeError, "test failure"):
            with app_metrics.background_job("notification_outbox"):
                raise RuntimeError("test failure")

        labels = {"job": "notification_outbox", "result": "failure"}
        self.assertEqual(registry.get_sample_value(
            "date4you_background_job_runs_total", labels,
        ), 1.0)
        self.assertEqual(registry.get_sample_value(
            "date4you_background_job_running", {"job": "notification_outbox"},
        ), 0.0)
        self.assertEqual(registry.get_sample_value(
            "date4you_background_job_last_success_timestamp_seconds",
            {"job": "notification_outbox"},
        ), 0.0)

    def test_outbox_metrics_publish_only_bounded_outcomes_and_states(self):
        registry = CollectorRegistry()
        app_metrics = metrics.ApplicationMetrics(registry)
        app_metrics.observe_outbox_batch(outbox.ProcessStats(
            claimed=8, sent=3, failed=2, deferred=1, expired=1, skipped=1,
        ))
        app_metrics.set_outbox_state(
            pending=7, due=4, claimed=2, oldest_due_age_seconds=65.5,
        )

        expected = {
            "sent": 3.0,
            "failed": 2.0,
            "deferred": 1.0,
            "expired": 1.0,
            "skipped": 1.0,
        }
        for result, value in expected.items():
            with self.subTest(result=result):
                self.assertEqual(registry.get_sample_value(
                    "date4you_notification_outbox_processed_total",
                    {"result": result},
                ), value)
        self.assertIsNone(registry.get_sample_value(
            "date4you_notification_outbox_processed_total",
            {"result": "claimed"},
        ))
        for state, value in {"pending": 7.0, "due": 4.0, "claimed": 2.0}.items():
            with self.subTest(state=state):
                self.assertEqual(registry.get_sample_value(
                    "date4you_notification_outbox_messages", {"state": state},
                ), value)
        self.assertEqual(registry.get_sample_value(
            "date4you_notification_outbox_oldest_due_age_seconds",
        ), 65.5)

    def test_auth_funnel_has_bounded_labels_and_safe_fallbacks(self):
        registry = CollectorRegistry()
        app_metrics = metrics.ApplicationMetrics(registry)

        app_metrics.observe_auth(
            flow="oauth", provider="google", result="success",
        )
        app_metrics.observe_auth(
            flow="raw-user-flow", provider="raw-user-provider",
            result="raw-error-text",
        )

        self.assertEqual(registry.get_sample_value(
            "date4you_auth_attempts_total",
            {"flow": "oauth", "provider": "google", "result": "success"},
        ), 1.0)
        self.assertEqual(registry.get_sample_value(
            "date4you_auth_attempts_total",
            {"flow": "other", "provider": "other", "result": "provider_error"},
        ), 1.0)
        payload = generate_latest(registry).decode("utf-8")
        self.assertNotIn("raw-user-flow", payload)
        self.assertNotIn("raw-user-provider", payload)
        self.assertNotIn("raw-error-text", payload)


class PrometheusMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_endpoint_has_prometheus_content_type(self):
        response = await metrics.prometheus_endpoint(None)

        self.assertEqual(
            response.headers["content-type"], metrics.CONTENT_TYPE_LATEST,
        )
        self.assertIn(b"date4you_", response.body)

    async def test_metrics_paths_are_not_included_in_http_red_metrics(self):
        registry = CollectorRegistry()
        app_metrics = metrics.ApplicationMetrics(registry)

        async def app(_scope, _receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"metrics"})

        async def send(_message):
            return None

        middleware = metrics.PrometheusMiddleware(app, metrics=app_metrics)
        for path in ("/metrics", "/metrics/"):
            with self.subTest(path=path):
                await middleware(
                    {"type": "http", "method": "GET", "path": path},
                    asyncio.Queue().get,
                    send,
                )

        payload = generate_latest(registry).decode("utf-8")
        self.assertNotIn("date4you_http_requests_total{", payload)

    async def test_http_metric_uses_route_template_not_secret_path(self):
        registry = CollectorRegistry()
        app_metrics = metrics.ApplicationMetrics(registry, monotonic=lambda: 1.0)

        async def app(scope, _receive, send):
            scope["route"] = SimpleNamespace(path="/c/{token}")
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = metrics.PrometheusMiddleware(app, metrics=app_metrics)
        sent = []
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/c/super-secret-token",
        }

        async def send(message):
            sent.append(message)

        await middleware(scope, asyncio.Queue().get, send)

        labels = {"method": "GET", "route": "/c/{token}", "status_class": "2xx"}
        self.assertEqual(registry.get_sample_value(
            "date4you_http_requests_total", labels,
        ), 1.0)
        self.assertNotIn("super-secret-token", generate_latest(
            registry,
        ).decode("utf-8"))


class OutboxStateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute(
            "INSERT INTO users(telegram_id, display_name, bot_linked, created_at) "
            "VALUES(1001, 'Аня', 1, ?)",
            (NOW,),
        )

    def tearDown(self):
        self.conn.close()

    def enqueue(self, key: str, *, send_at: str, expires_at: str | None = None):
        outbox.enqueue(
            self.conn,
            user_id=1,
            kind="result",
            event_key=key,
            text="Тест",
            send_at=send_at,
            expires_at=expires_at,
            now=NOW,
        )

    def test_snapshot_separates_due_and_live_claims_and_excludes_terminal_rows(self):
        self.enqueue("due", send_at="2030-01-01T09:58:00")
        self.enqueue("future", send_at="2030-01-01T11:00:00")
        self.enqueue("live-claim", send_at="2030-01-01T09:57:00")
        self.enqueue("stale-claim", send_at="2030-01-01T09:50:00")
        self.enqueue(
            "expired", send_at="2030-01-01T09:00:00",
            expires_at="2030-01-01T09:59:00",
        )
        self.enqueue("sent", send_at="2030-01-01T09:00:00")
        self.enqueue("cancelled", send_at="2030-01-01T09:00:00")
        self.conn.execute(
            "UPDATE notification_outbox SET claimed_at=? WHERE event_key=?",
            ("2030-01-01T09:59:00", "live-claim"),
        )
        self.conn.execute(
            "UPDATE notification_outbox SET claimed_at=? WHERE event_key=?",
            ("2030-01-01T09:55:00", "stale-claim"),
        )
        self.conn.execute(
            "UPDATE notification_outbox SET sent_at=? WHERE event_key=?",
            (NOW, "sent"),
        )
        self.conn.execute(
            "UPDATE notification_outbox SET cancelled_at=? WHERE event_key=?",
            (NOW, "cancelled"),
        )

        state = outbox.snapshot_state(self.conn, now=NOW)

        self.assertEqual((state.pending, state.due, state.claimed), (4, 2, 1))
        self.assertAlmostEqual(state.oldest_due_age_seconds, 600.0, delta=0.01)


class TaskInstrumentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_outbox_loop_publishes_batch_and_queue_state(self):
        stats = outbox.ProcessStats(sent=2, failed=1)
        state = outbox.OutboxState(
            pending=5, due=3, claimed=1, oldest_due_age_seconds=90.0,
        )

        async def to_thread(func, *args, **kwargs):
            if func is outbox.process_due:
                return stats
            if func is outbox.snapshot_state:
                return state
            raise AssertionError(f"unexpected function: {func}")

        async def stop_after_iteration(_delay):
            raise asyncio.CancelledError

        with (
            mock.patch.object(tasks.asyncio, "to_thread", side_effect=to_thread),
            mock.patch.object(tasks.asyncio, "sleep", side_effect=stop_after_iteration),
            mock.patch.object(tasks.metrics, "track_background_job") as track,
            mock.patch.object(tasks.metrics, "observe_outbox_batch") as observe,
            mock.patch.object(tasks.metrics, "set_outbox_state") as set_state,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await tasks.notification_outbox_loop()

        track.assert_called_once_with("notification_outbox")
        observe.assert_called_once_with(stats)
        set_state.assert_called_once_with(
            pending=5,
            due=3,
            claimed=1,
            oldest_due_age_seconds=90.0,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
