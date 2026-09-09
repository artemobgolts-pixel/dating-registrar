#!/usr/bin/env python3
"""SEC-02: ownership и отсутствие побочных эффектов при отклонённом detach."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-detach-import-")
os.environ.update({
    "DATA_DIR": _IMPORT_DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "category-detach.test",
    "SECRET_KEY": "category-detach-test-secret",
    "TG_BOT_TOKEN": "",
})

import admin_routes  # noqa: E402
import db  # noqa: E402


STAMP = "2030-01-01T10:00:00"


class CategoryDetachSecurityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        self.conn.executemany(
            "INSERT INTO users(id,telegram_id,display_name,is_operator,created_at) "
            "VALUES(?,?,?,?,?)",
            ((1, 1001, "Владелец", 0, STAMP),
             (2, 1002, "Чужой владелец", 0, STAMP),
             (3, 1003, "Участник", 0, STAMP),
             (4, 1004, "Оператор", 1, STAMP)),
        )
        self.conn.executemany(
            "INSERT INTO categories(id,owner_id,name,link_token,created_at) "
            "VALUES(?,?,?,?,?)",
            ((1, 1, "Своя подборка", "own-category", STAMP),
             (2, 2, "Чужая подборка", "foreign-category", STAMP),
             (3, 1, "Другая своя подборка", "other-category", STAMP)),
        )
        self.conn.executemany(
            "INSERT INTO dates(id,owner_id,name,share_token,created_at) VALUES(?,?,?,?,?)",
            ((1, 1, "Своё событие", "own-event", STAMP),
             (2, 2, "Чужое событие", "foreign-event", STAMP),
             (3, 1, "Событие без привязки", "unattached-event", STAMP)),
        )
        self.conn.executemany(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            ((1, 1), (2, 2), (1, 3)),
        )
        self.conn.executemany(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
            "VALUES(?,?,'synthetic-guest',3,?)",
            ((1, 1, STAMP), (2, 2, STAMP), (1, 3, STAMP)),
        )
        # Реальные pending/cancelled outbox и review_queue должны пережить
        # отклонённый запрос без изменений, включая timestamps и last_error.
        for date_id in (1, 2, 3):
            for user_id, cancelled in ((3, None), (4, STAMP)):
                self.conn.execute(
                    "INSERT INTO notification_outbox(user_id,kind,event_key,text,"
                    "send_at,cancelled_at,last_error,created_at,updated_at) "
                    "VALUES(?,'review_prompt',?,'synthetic review',?,?,?,?,?)",
                    (user_id, f"review:date:{date_id}:user:{user_id}:prompt",
                     STAMP, cancelled, "intentional" if cancelled else None,
                     STAMP, STAMP),
                )
                self.conn.execute(
                    "INSERT INTO review_queue(user_id,date_id,created_at,updated_at,"
                    "dismissed_at) VALUES(?,?,?,?,?)",
                    (user_id, date_id, STAMP, STAMP, cancelled),
                )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def request(self, user_id=1):
        user = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return SimpleNamespace(state=SimpleNamespace(user=user), query_params={})

    def assert_rejected_without_effects(self, date_id, *, cid=1, user_id=1):
        before = list(self.conn.iterdump())
        changes = self.conn.total_changes
        with (
            patch.object(admin_routes, "_require_category_composition_mutable") as lock,
            patch.object(admin_routes.voting_events, "queue_date_removed") as removed,
            patch.object(admin_routes.voting_events, "cancel_deadline_reminder") as cancel,
            patch.object(admin_routes.social_events, "queue_review_prompts_for_date") as review,
            self.assertRaises(HTTPException) as denied,
        ):
            admin_routes.category_detach(
                cid, self.request(user_id), date_id=date_id, conn=self.conn,
            )
        self.assertEqual(denied.exception.status_code, 404)
        for helper in (lock, removed, cancel, review):
            helper.assert_not_called()
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertFalse(self.conn.in_transaction)

    def test_owner_detach_removes_only_requested_relationship_and_bookings(self):
        response = admin_routes.category_detach(
            1, self.request(), date_id=1, conn=self.conn,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("/admin/categories/1", response.headers["location"])
        for table in ("date_categories", "bookings"):
            self.assertIsNone(self.conn.execute(
                f"SELECT 1 FROM {table} WHERE date_id=1 AND category_id=1",
            ).fetchone())
            self.assertIsNotNone(self.conn.execute(
                f"SELECT 1 FROM {table} WHERE date_id=1 AND category_id=3",
            ).fetchone())
            self.assertIsNotNone(self.conn.execute(
                f"SELECT 1 FROM {table} WHERE date_id=2 AND category_id=2",
            ).fetchone())
        event = self.conn.execute("SELECT * FROM dates WHERE id=1").fetchone()
        self.assertIsNotNone(event)
        self.assertIsNone(event["archived_at"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE kind='date_removed'",
        ).fetchone()[0], 1)

    def test_operator_uses_category_owner_context(self):
        response = admin_routes.category_detach(
            1, self.request(4), date_id=1, conn=self.conn,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM date_categories WHERE date_id=1 AND category_id=1",
        ).fetchone())

    def test_foreign_event_rejected_without_side_effects(self):
        self.assert_rejected_without_effects(2)

    def test_foreign_event_with_inconsistent_relationship_still_rejected(self):
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(2,1)",
        )
        self.conn.commit()
        self.assert_rejected_without_effects(2)

    def test_operator_cannot_detach_cross_owner_event(self):
        self.assert_rejected_without_effects(2, user_id=4)

    def test_missing_event_rejected_without_side_effects(self):
        self.assert_rejected_without_effects(99999)

    def test_unattached_owned_event_rejected_without_side_effects(self):
        self.assert_rejected_without_effects(3)

    def test_foreign_category_rejected_without_side_effects(self):
        self.assert_rejected_without_effects(2, cid=2)

    def test_repeated_detach_is_safe_404(self):
        admin_routes.category_detach(1, self.request(), date_id=1, conn=self.conn)
        self.assert_rejected_without_effects(1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
