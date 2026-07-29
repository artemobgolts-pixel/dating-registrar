#!/usr/bin/env python3
"""Точечные тесты инфраструктуры Telegram notification outbox.

Запуск из корня репозитория: ``python tests/test_notification_outbox.py``.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-outbox-import-")
os.environ["DATA_DIR"] = _IMPORT_DATA.name
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-notification-outbox")

import db  # noqa: E402
import notification_outbox as outbox  # noqa: E402


NOW = "2030-01-01T10:00:00"


class MutatingDeliveryReadConnection(sqlite3.Connection):
    """Имитирует изменение строки другим соединением после её claim."""

    delivery_read_mutation: str | None = None

    def execute(self, sql, parameters=(), /):
        compact = " ".join(sql.split())
        mutation = self.delivery_read_mutation
        if mutation and compact.startswith(
            "SELECT o.id, o.user_id, o.text, o.attempts, u.telegram_id"
        ):
            self.delivery_read_mutation = None
            notification_id = parameters[0]
            if mutation == "cancel":
                super().execute(
                    "UPDATE notification_outbox SET cancelled_at=?, "
                    "claimed_at=NULL, updated_at=? WHERE id=?",
                    (NOW, NOW, notification_id),
                )
            elif mutation == "steal_claim":
                super().execute(
                    "UPDATE notification_outbox "
                    "SET claimed_at=claimed_at || '9' WHERE id=?",
                    (notification_id,),
                )
            else:  # pragma: no cover - защита тестового помощника
                raise AssertionError(f"unknown mutation: {mutation}")
            super().commit()
        return super().execute(sql, parameters)


class NotificationOutboxTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute(
            "INSERT INTO users(telegram_id, display_name, bot_linked, created_at) "
            "VALUES(1001, 'Аня', 0, ?)",
            (NOW,),
        )

    def tearDown(self):
        self.conn.close()

    def row(self, event_key: str):
        return self.conn.execute(
            "SELECT * FROM notification_outbox WHERE event_key=?", (event_key,)
        ).fetchone()

    def test_enqueue_deduplicates_and_pending_event_can_be_rescheduled(self):
        first = outbox.enqueue(
            self.conn, user_id=1, kind="result", event_key="category:7:result",
            text="Первый", send_at="2030-01-02T10:00:00", now=NOW,
        )
        second = outbox.enqueue(
            self.conn, user_id=1, kind="result", event_key="category:7:result",
            text="Обновлённый", send_at="2030-01-03T10:00:00", now=NOW,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0],
            1,
        )
        row = self.row("category:7:result")
        self.assertEqual(row["text"], "Обновлённый")
        self.assertEqual(row["send_at"], "2030-01-03T10:00:00")

    def test_sent_event_is_immutable_dedupe_record(self):
        outbox.enqueue(
            self.conn, user_id=1, kind="result", event_key="event:sent",
            text="Исходный", now=NOW,
        )
        self.conn.execute("UPDATE users SET bot_linked=1 WHERE id=1")
        sent = []
        stats = outbox.process_due(
            self.conn, now=NOW,
            sender=lambda chat_id, text: sent.append((chat_id, text)) or True,
        )
        self.assertEqual(stats.sent, 1)
        self.assertEqual(sent, [(1001, "Исходный")])

        same_id = outbox.enqueue(
            self.conn, user_id=1, kind="result", event_key="event:sent",
            text="Не должен заменить", now="2030-01-01T11:00:00",
        )
        row = self.row("event:sent")
        self.assertEqual(same_id, row["id"])
        self.assertEqual(row["text"], "Исходный")
        self.assertEqual(row["sent_at"], NOW)

    def test_unlinked_user_is_deferred_then_resolved_at_delivery_time(self):
        outbox.enqueue(
            self.conn, user_id=1, kind="winner", event_key="event:late-link",
            text="Вы выбрали событие", now=NOW,
            expires_at="2030-01-02T10:00:00",
        )
        calls = []
        stats = outbox.process_due(
            self.conn, now=NOW,
            sender=lambda chat_id, text: calls.append((chat_id, text)) or True,
        )
        self.assertEqual(stats.deferred, 1)
        self.assertEqual(calls, [])
        row = self.row("event:late-link")
        self.assertIsNone(row["sent_at"])
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["last_error"], "telegram_not_linked")

        # Telegram id меняется/появляется уже после постановки в очередь.
        self.conn.execute(
            "UPDATE users SET telegram_id=2002, bot_linked=1 WHERE id=1"
        )
        stats = outbox.process_due(
            self.conn, now="2030-01-01T10:00:30",
            sender=lambda chat_id, text: calls.append((chat_id, text)) or True,
        )
        self.assertEqual(stats.sent, 1)
        self.assertEqual(calls, [(2002, "Вы выбрали событие")])

    def test_unlinked_head_does_not_starve_later_due_messages(self):
        self.conn.execute(
            "INSERT INTO users(telegram_id, display_name, bot_linked, created_at) "
            "VALUES(1002, 'Борис', 1, ?)", (NOW,),
        )
        outbox.enqueue(
            self.conn, user_id=1, kind="result", event_key="event:unlinked-head",
            text="Ждёт привязки", now=NOW,
        )
        outbox.enqueue(
            self.conn, user_id=2, kind="result", event_key="event:deliverable-tail",
            text="Можно отправить", now=NOW,
        )
        first = outbox.process_due(self.conn, now=NOW, limit=1, sender=lambda *_: True)
        delivered = []
        second = outbox.process_due(
            self.conn, now=NOW, limit=1,
            sender=lambda chat_id, text: delivered.append((chat_id, text)) or True,
        )
        self.assertEqual(first.deferred, 1)
        self.assertEqual(second.sent, 1)
        self.assertEqual(delivered, [(1002, "Можно отправить")])

    def test_failure_retries_with_backoff_and_counts_real_attempts(self):
        self.conn.execute("UPDATE users SET bot_linked=1 WHERE id=1")
        outbox.enqueue(
            self.conn, user_id=1, kind="reminder", event_key="event:retry",
            text="Напоминание", now=NOW,
        )
        failed = outbox.process_due(self.conn, now=NOW, sender=lambda *_: False)
        self.assertEqual((failed.failed, failed.claimed), (1, 1))
        row = self.row("event:retry")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["send_at"], "2030-01-01T10:00:30")
        self.assertEqual(row["last_error"], "telegram_send_failed")

        early = outbox.process_due(
            self.conn, now="2030-01-01T10:00:29", sender=lambda *_: True
        )
        self.assertEqual(early.claimed, 0)
        retried = outbox.process_due(
            self.conn, now="2030-01-01T10:00:30", sender=lambda *_: True
        )
        self.assertEqual(retried.sent, 1)
        self.assertEqual(self.row("event:retry")["attempts"], 2)

    def test_cancel_by_literal_prefix_and_expiry_are_terminal(self):
        outbox.enqueue(
            self.conn, user_id=1, kind="reminder", event_key="date:5:%:24h",
            text="A", now=NOW,
        )
        outbox.enqueue(
            self.conn, user_id=1, kind="reminder", event_key="date:5:other",
            text="B", now=NOW,
        )
        self.assertEqual(outbox.cancel(
            self.conn, event_prefix="date:5:%", reason="date_changed", now=NOW
        ), 1)
        self.assertEqual(self.row("date:5:%:24h")["last_error"], "date_changed")
        self.assertIsNone(self.row("date:5:other")["cancelled_at"])

        outbox.enqueue(
            self.conn, user_id=1, kind="result", event_key="event:expired",
            text="Поздно", now=NOW, expires_at="2030-01-01T10:00:01",
        )
        stats = outbox.process_due(
            self.conn, now="2030-01-01T10:00:01", sender=lambda *_: True
        )
        self.assertEqual(stats.expired, 1)
        expired = self.row("event:expired")
        self.assertIsNone(expired["sent_at"])
        self.assertEqual(expired["last_error"], "expired")

    def test_cancel_requires_scope(self):
        with self.assertRaises(ValueError):
            outbox.cancel(self.conn)

    def test_delivery_rechecks_claim_and_terminal_state_before_send(self):
        for mutation in ("cancel", "steal_claim"):
            with self.subTest(mutation=mutation):
                conn = sqlite3.connect(
                    ":memory:", factory=MutatingDeliveryReadConnection
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(db.SCHEMA)
                conn.execute(
                    "INSERT INTO users(telegram_id, display_name, bot_linked, "
                    "created_at) VALUES(1001, 'Аня', 1, ?)",
                    (NOW,),
                )
                outbox.enqueue(
                    conn, user_id=1, kind="result",
                    event_key=f"event:race:{mutation}", text="Не отправлять",
                    now=NOW,
                )
                conn.commit()
                conn.delivery_read_mutation = mutation
                calls = []

                stats = outbox.process_due(
                    conn, now=NOW,
                    sender=lambda *args: calls.append(args) or True,
                )

                self.assertEqual(stats.claimed, 1)
                self.assertEqual(stats.sent, 0)
                self.assertEqual(calls, [])
                row = conn.execute(
                    "SELECT sent_at, cancelled_at FROM notification_outbox"
                ).fetchone()
                self.assertIsNone(row["sent_at"])
                if mutation == "cancel":
                    self.assertEqual(row["cancelled_at"], NOW)
                conn.close()


class NotificationOutboxMigrationTests(unittest.TestCase):
    def test_fresh_database_is_latest_and_has_outbox(self):
        with tempfile.TemporaryDirectory(prefix="date4you-v-latest-") as tmp:
            path = Path(tmp) / "app.db"
            old_path = db.DB_PATH
            try:
                db.DB_PATH = path
                db.init_db()
            finally:
                db.DB_PATH = old_path
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0],
                    db.LATEST_VERSION,
                )
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(notification_outbox)"
                    )
                }
                self.assertTrue({
                    "event_key", "send_at", "expires_at", "sent_at",
                    "attempts", "last_error", "cancelled_at", "claimed_at",
                } <= columns)
            finally:
                conn.close()

    def test_v22_database_gets_outbox_migration(self):
        with tempfile.TemporaryDirectory(prefix="date4you-v22-to-latest-") as tmp:
            path = Path(tmp) / "app.db"
            old_path = db.DB_PATH
            try:
                db.DB_PATH = path
                db.init_db()
                conn = sqlite3.connect(path)
                conn.execute("DROP TABLE notification_outbox")
                # Свежая фикстура уже содержит поля v26. Убираем их, чтобы
                # user_version=22 действительно описывал старую схему, а не
                # просил миграцию повторно добавить существующие колонки.
                conn.execute("ALTER TABLE categories DROP COLUMN category_skin")
                conn.execute("ALTER TABLE users DROP COLUMN admin_skin")
                conn.execute("PRAGMA user_version=22")
                conn.commit()
                conn.close()

                db.init_db()
            finally:
                db.DB_PATH = old_path
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0],
                    db.LATEST_VERSION,
                )
                self.assertTrue(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='notification_outbox'"
                ).fetchone())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
