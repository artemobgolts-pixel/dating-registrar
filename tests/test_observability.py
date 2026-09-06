"""Регрессии безопасного logging/Sentry boundary."""

import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))

import observability  # noqa: E402


BOT_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_123456"
LINK_TOKEN = "persistent-secret-link-token"


class RequestIdTests(unittest.TestCase):
    def test_accepts_only_short_log_safe_request_ids(self):
        request_id, token = observability.bind_request_id("edge-01:req.42")
        try:
            self.assertEqual(request_id, "edge-01:req.42")
            self.assertEqual(observability.current_request_id(), request_id)
        finally:
            observability.reset_request_id(token)

        generated, token = observability.bind_request_id("bad\nlog-injection")
        try:
            self.assertRegex(generated, r"^[0-9a-f]{32}$")
            self.assertNotIn("bad", generated)
        finally:
            observability.reset_request_id(token)


class RedactionTests(unittest.TestCase):
    def test_json_formatter_redacts_tokens_and_keeps_correlation(self):
        request_id, token = observability.bind_request_id("req-safe")
        try:
            record = logging.LogRecord(
                "tests",
                logging.WARNING,
                __file__,
                1,
                f"failed /c/{LINK_TOKEN}?code=oauth-secret via "
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                (),
                None,
            )
            record.event = "dependency_failed"
            payload = json.loads(observability.JsonFormatter().format(record))
            pretty = observability.PrettyFormatter().format(record)
        finally:
            observability.reset_request_id(token)

        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["request_id"], "req-safe")
        self.assertEqual(payload["event"], "dependency_failed")
        self.assertNotIn(LINK_TOKEN, encoded)
        self.assertNotIn(BOT_TOKEN, encoded)
        self.assertNotIn("oauth-secret", encoded)
        self.assertIn("/c/{token}", encoded)
        for secret in (LINK_TOKEN, BOT_TOKEN, "oauth-secret"):
            self.assertNotIn(secret, pretty)
        self.assertIn("/c/{token}", pretty)

    def test_pretty_formatter_groups_http_fields_and_stays_on_one_line(self):
        record = logging.LogRecord(
            "app", logging.INFO, __file__, 1,
            "HTTP-запрос завершён\nподмена строки", (), None,
        )
        record.event = "http_request_completed"
        record.request_id = "edge-01:req.42"
        record.method = "GET"
        record.route = "/health"
        record.status_code = 200
        record.status_class = "2xx"
        record.duration_ms = 12.34

        rendered = observability.PrettyFormatter().format(record)

        self.assertIn("│ INFO  │ app │ HTTP-запрос завершён\\nподмена строки", rendered)
        self.assertIn("GET /health → 200 · 12.34 мс", rendered)
        self.assertIn("запрос=edge-01:req.42", rendered)
        self.assertIn("событие=http_request_completed", rendered)
        self.assertNotIn("\n", rendered)

    def test_pretty_formatter_labels_dependency_status_without_empty_route(self):
        record = logging.LogRecord(
            "notify", logging.WARNING, __file__, 1,
            "Telegram вернул ошибку", (), None,
        )
        record.event = "telegram_send_failed"
        record.provider = "telegram"
        record.status_code = 503
        record.outcome = "failure"

        rendered = observability.PrettyFormatter().format(record)

        self.assertIn("статус=503", rendered)
        self.assertNotIn("→ 503", rendered)

    def test_traceback_omits_exception_message_and_source_code(self):
        try:
            raise RuntimeError("private-value-from-user")
        except RuntimeError:
            record = logging.LogRecord(
                "app", logging.ERROR, __file__, 1,
                "Безопасное сообщение", (), sys.exc_info(),
            )

        rendered = observability.PrettyFormatter().format(record)

        self.assertIn("исключение=RuntimeError", rendered)
        self.assertIn("test_traceback_omits_exception_message_and_source_code", rendered)
        self.assertNotIn("private-value-from-user", rendered)

    def test_redactor_removes_complete_headers_email_and_media_filename(self):
        value = (
            "Authorization: Bearer top-secret-token\n"
            "Cookie: session=first; csrf=second\n"
            "user person@example.com opened /uploads/private-person-name.jpg"
        )

        scrubbed = observability.redact_text(value)

        for secret in (
            "top-secret-token", "first", "second", "person@example.com",
            "private-person-name.jpg",
        ):
            self.assertNotIn(secret, scrubbed)
        self.assertIn("Authorization: [REDACTED]", scrubbed)
        self.assertIn("Cookie: [REDACTED]", scrubbed)
        self.assertIn("/uploads/{filename}", scrubbed)

    def test_sentry_event_drops_request_payload_and_normalizes_url(self):
        event = {
            "transaction": f"/c/{LINK_TOKEN}",
            "transaction_info": {"source": "url"},
            "request": {
                "method": "POST",
                "url": (
                    f"https://date4you.online/c/{LINK_TOKEN}/image/private.jpg"
                    "?code=oauth-secret"
                ),
                "query_string": "code=oauth-secret",
                "cookies": {"session": "secret"},
                "data": {"email": "person@example.com"},
                "headers": {
                    "Authorization": "Bearer top-secret",
                    "Cookie": "session=secret",
                    "Content-Type": "application/json; note=private-token",
                    "X-Request-ID": "req-safe",
                },
            },
            "user": {"email": "sentry-user@example.com", "id": "42"},
            "extra": {"request_id": "edge-safe-42"},
            "message": "private display name",
            "spans": [{
                "op": "http.client",
                "description": "GET https://example.com/private-person?email=a@b.test",
                "data": {"url": "https://example.com/private-person"},
            }],
            "exception": {"values": [{
                "value": f"request {BOT_TOKEN} from crash@example.com",
            }]},
        }

        scrubbed = observability.scrub_sentry_event(event)
        encoded = json.dumps(scrubbed, ensure_ascii=False)
        self.assertEqual(scrubbed["transaction"], "unmatched")
        self.assertEqual(
            scrubbed["request"]["url"],
            "https://date4you.online/unmatched",
        )
        self.assertNotIn("query_string", scrubbed["request"])
        self.assertNotIn("cookies", scrubbed["request"])
        self.assertNotIn("data", scrubbed["request"])
        self.assertNotIn("headers", scrubbed["request"])
        self.assertEqual(scrubbed["request"]["method"], "POST")
        self.assertNotIn("user", scrubbed)
        self.assertNotIn("message", scrubbed)
        self.assertNotIn("value", scrubbed["exception"]["values"][0])
        self.assertNotIn("data", scrubbed["spans"][0])
        self.assertEqual(scrubbed["spans"][0]["description"], "http.client")
        self.assertEqual(scrubbed["tags"]["request_id"], "edge-safe-42")
        for secret in (LINK_TOKEN, BOT_TOKEN, "oauth-secret", "top-secret",
                       "private-token", "person@example.com",
                       "sentry-user@example.com", "crash@example.com"):
            self.assertNotIn(secret, encoded)

    def test_sentry_accepts_only_route_source_as_transaction_template(self):
        event = {
            "transaction": "/u/private-person-name",
            "transaction_info": {"source": "custom"},
            "request": {
                "method": "GET",
                "url": "https://date4you.online/u/private-person-name",
                "future_sdk_field": "private-person-name",
            },
        }

        scrubbed = observability.scrub_sentry_event(event)
        encoded = json.dumps(scrubbed, ensure_ascii=False)

        self.assertEqual(scrubbed["transaction"], "unmatched")
        self.assertEqual(
            scrubbed["request"],
            {"url": "https://date4you.online/unmatched", "method": "GET"},
        )
        self.assertNotIn("private-person-name", encoded)

    def test_sentry_event_drops_untrusted_request_id_tag(self):
        event = {
            "tags": {"request_id": "bad request id"},
            "extra": {"request_id": 123},
        }

        scrubbed = observability.scrub_sentry_event(event)

        self.assertNotIn("request_id", scrubbed.get("tags", {}))

    def test_sentry_recovers_request_id_from_exception_after_context_reset(self):
        exc = RuntimeError("arbitrary user text")
        observability.attach_request_id(exc, "edge-safe-42")

        scrubbed = observability.scrub_sentry_event(
            {"exception": {"values": [{"type": "RuntimeError"}]}},
            {"exc_info": (RuntimeError, exc, None)},
        )

        self.assertEqual(scrubbed["tags"]["request_id"], "edge-safe-42")

    def test_sentry_drops_logging_duplicate_of_unhandled_http_error(self):
        event = {
            "logger": "app",
            "extra": {"event": "http_request_unhandled_error",
                      "request_id": "edge-safe-42"},
        }

        self.assertIsNone(observability.scrub_sentry_event(event))

    def test_http_breadcrumb_hides_bot_token_and_query(self):
        breadcrumb = {
            "category": "httplib",
            "data": {
                "method": "POST",
                "url": (
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                    "?token=secondary-secret"
                ),
                "status_code": 500,
            },
        }
        scrubbed = observability.scrub_sentry_breadcrumb(breadcrumb)
        self.assertEqual(
            scrubbed["data"]["url"],
            "https://api.telegram.org",
        )
        encoded = json.dumps(scrubbed, ensure_ascii=False)
        self.assertNotIn(BOT_TOKEN, encoded)
        self.assertNotIn("secondary-secret", encoded)


class IntegrationConfigurationTests(unittest.TestCase):
    def test_sentry_uses_safe_error_capture_without_third_party_propagation(self):
        with mock.patch("sentry_sdk.init") as sentry_init:
            enabled = observability.init_sentry(
                dsn="https://public@example.invalid/1",
                environment="test",
                release="test-release",
                traces_sample_rate=0.05,
            )

        self.assertTrue(enabled)
        options = sentry_init.call_args.kwargs
        self.assertEqual(options["trace_propagation_targets"], [])
        self.assertFalse(options["propagate_traces"])
        logging_integration = options["integrations"][0]
        self.assertIsNotNone(logging_integration._handler)
        self.assertIsNone(logging_integration._breadcrumb_handler)
        self.assertIsNone(logging_integration._sentry_logs_handler)

    def test_uvicorn_loggers_are_routed_through_pretty_root(self):
        root = logging.getLogger()
        names = (
            "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore",
        )
        saved_root = (list(root.handlers), root.level)
        saved = {
            name: (
                list(logging.getLogger(name).handlers),
                logging.getLogger(name).level,
                logging.getLogger(name).propagate,
                logging.getLogger(name).disabled,
            )
            for name in names
        }
        try:
            for name in names:
                logging.getLogger(name).addHandler(logging.NullHandler())

            observability.configure_logging(
                level="INFO", environment="test", release="test-release",
            )

            self.assertIsInstance(root.handlers[0].formatter,
                                  observability.PrettyFormatter)
            for name in ("uvicorn", "uvicorn.error"):
                logger = logging.getLogger(name)
                self.assertEqual(logger.handlers, [])
                self.assertTrue(logger.propagate)
            access = logging.getLogger("uvicorn.access")
            self.assertEqual(access.handlers, [])
            self.assertTrue(access.disabled)
            self.assertFalse(access.propagate)
            self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
            self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)
        finally:
            root.handlers = saved_root[0]
            root.setLevel(saved_root[1])
            for name, (handlers, level, propagate, disabled) in saved.items():
                logger = logging.getLogger(name)
                logger.handlers = handlers
                logger.setLevel(level)
                logger.propagate = propagate
                logger.disabled = disabled

    def test_json_log_format_remains_available(self):
        root = logging.getLogger()
        names = (
            "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore",
        )
        saved_root = (list(root.handlers), root.level)
        saved = {
            name: (
                list(logging.getLogger(name).handlers),
                logging.getLogger(name).level,
                logging.getLogger(name).propagate,
                logging.getLogger(name).disabled,
            )
            for name in names
        }
        try:
            observability.configure_logging(
                level="INFO", environment="test", release="test-release",
                log_format="json",
            )

            self.assertIsInstance(root.handlers[0].formatter,
                                  observability.JsonFormatter)
        finally:
            root.handlers = saved_root[0]
            root.setLevel(saved_root[1])
            for name, (handlers, level, propagate, disabled) in saved.items():
                logger = logging.getLogger(name)
                logger.handlers = handlers
                logger.setLevel(level)
                logger.propagate = propagate
                logger.disabled = disabled


if __name__ == "__main__":
    unittest.main()
