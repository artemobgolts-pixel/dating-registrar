#!/usr/bin/env python3
"""Поведенческие регрессии очереди отзывов и новых массовых действий."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-v30-backend-")
os.environ.update({
    "DATA_DIR": _IMPORT_DATA.name,
    "COOKIE_SECURE": "false",
    # unittest discovery импортирует модули до запуска тестов. Совпадаем с
    # прежним первым integration-модулем test_copy_actions, чтобы новый файл
    # не менял закэшированные config/main для остального legacy-suite.
    "DOMAIN": "copy.test",
    "SECRET_KEY": "test-secret-key-for-copy-actions",
    "TG_BOT_TOKEN": "",
    "TG_CHAT_ID": "",
    "TG_BOT_USERNAME": "date4you_copy_test_bot",
    "TG_WEBHOOK_SECRET": "copy-hook-secret",
    "OPERATOR_TG_IDS": "",
})

import admin_routes  # noqa: E402
import db  # noqa: E402
import public_routes  # noqa: E402
import social_events  # noqa: E402
import tasks  # noqa: E402


STAMP = "2030-01-01T00:00:00"


class BackendV30RegressionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        self.owner_id = self._user(930001, "Владелец")
        self.viewer_id = self._user(930002, "Участник")
        self.other_id = self._user(930003, "Другой владелец")

    def tearDown(self):
        self.conn.close()

    def _user(self, telegram_id: int, name: str) -> int:
        return int(self.conn.execute(
            "INSERT INTO users(telegram_id,display_name,created_at) VALUES(?,?,?)",
            (telegram_id, name, STAMP),
        ).lastrowid)

    def _date(self, owner_id: int, name: str, *, starts_at: str | None = None,
              ends_at: str | None = None, share_token: str | None = None,
              is_public: int = 1) -> int:
        return int(self.conn.execute(
            "INSERT INTO dates(owner_id,name,starts_at,ends_at,share_token,"
            "is_public,created_at) VALUES(?,?,?,?,?,?,?)",
            (owner_id, name, starts_at, ends_at, share_token, is_public, STAMP),
        ).lastrowid)

    def _want(self, date_id: int, user_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
            "VALUES(?,?,?,?)",
            (user_id or self.viewer_id, date_id, STAMP, STAMP),
        )

    def _request_for(self, user_id: int) -> SimpleNamespace:
        user = self.conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,),
        ).fetchone()
        return SimpleNamespace(state=SimpleNamespace(user=user))

    def test_review_waiting_upserts_then_clears_without_duplicates(self):
        date_id = self._date(
            self.owner_id, "Прошедшая встреча",
            starts_at="2030-01-02T18:00:00",
            ends_at="2030-01-02T20:00:00",
            share_token="review-waiting",
        )
        self._want(date_id)

        first_time = datetime(2030, 1, 3, 12, 0, 0)
        second_time = datetime(2030, 1, 3, 13, 0, 0)
        self.assertTrue(social_events.mark_review_waiting(
            self.conn, date_id, self.viewer_id, "declined", now=first_time,
        ))
        self.assertTrue(social_events.mark_review_waiting(
            self.conn, date_id, self.viewer_id, "review_deleted", now=second_time,
        ))

        rows = self.conn.execute(
            "SELECT * FROM review_queue WHERE user_id=? AND date_id=?",
            (self.viewer_id, date_id),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "review_deleted")
        self.assertEqual(rows[0]["created_at"], "2030-01-03T12:00:00")
        self.assertEqual(rows[0]["updated_at"], "2030-01-03T13:00:00")

        self.assertEqual(
            social_events.clear_review_waiting(
                self.conn, date_id, self.viewer_id,
            ),
            1,
        )
        self.assertEqual(
            social_events.clear_review_waiting(
                self.conn, date_id, self.viewer_id,
            ),
            0,
        )

    def test_deleted_review_queue_preserves_return_right_after_withdrawal(self):
        date_id = self._date(
            self.owner_id, "Обзор после отказа",
            starts_at="2030-01-02T18:00:00",
            ends_at="2030-01-02T20:00:00",
            share_token="deleted-review-return",
        )
        # На момент удаления прежняя отметка/бронь уже может исчезнуть. Сам
        # принадлежавший пользователю обзор остаётся достаточным основанием
        # поместить событие в очередь и дать написать его заново.
        self.assertFalse(social_events.mark_review_waiting(
            self.conn, date_id, self.viewer_id, "review_deleted",
            now=datetime(2030, 1, 3, 12, 0, 0),
        ))
        self.assertTrue(social_events.mark_review_waiting(
            self.conn, date_id, self.viewer_id, "review_deleted",
            now=datetime(2030, 1, 3, 12, 0, 0), require_available=False,
        ))
        self.assertTrue(social_events.review_available(
            self.conn, date_id, self.viewer_id,
            now=datetime(2030, 1, 3, 12, 0, 0),
        ))
        self.assertEqual(self.conn.execute(
            "SELECT reason FROM review_queue WHERE user_id=? AND date_id=?",
            (self.viewer_id, date_id),
        ).fetchone()[0], "review_deleted")

    def test_archive_prompt_has_one_idempotency_key(self):
        date_id = self._date(
            self.owner_id, "Архивный prompt",
            ends_at="2030-01-02T20:00:00", share_token="archive-prompt",
        )
        self._want(date_id)

        first = social_events.queue_archive_review_prompt(
            self.conn, date_id, self.viewer_id,
            now=datetime(2030, 1, 3, 12, 0, 0),
        )
        second = social_events.queue_archive_review_prompt(
            self.conn, date_id, self.viewer_id,
            now=datetime(2030, 1, 3, 13, 0, 0),
        )
        self.assertEqual(first, second)
        rows = self.conn.execute(
            "SELECT id,event_key,kind,send_at FROM notification_outbox"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["event_key"],
            social_events.archive_prompt_key(date_id, self.viewer_id),
        )
        self.assertEqual(rows[0]["kind"], "review_prompt")
        self.assertEqual(rows[0]["send_at"], "2030-01-03T13:00:00")

    def test_autoarchive_creates_review_queue_and_prompt_once(self):
        date_id = self._date(
            self.owner_id, "Давно завершилось",
            starts_at="2000-01-01T18:00:00",
            ends_at="2000-01-01T20:00:00",
            share_token="autoarchive-review",
        )
        self._want(date_id)
        self.conn.commit()

        self.assertEqual(tasks.autoarchive_once(self.conn), 1)
        self.assertIsNotNone(self.conn.execute(
            "SELECT archived_at FROM dates WHERE id=?", (date_id,),
        ).fetchone()[0])
        queued = self.conn.execute(
            "SELECT user_id,date_id,reason FROM review_queue"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in queued],
            [(self.viewer_id, date_id, "due")],
        )
        prompts = self.conn.execute(
            "SELECT event_key FROM notification_outbox WHERE kind='review_prompt'"
        ).fetchall()
        self.assertEqual(
            [row["event_key"] for row in prompts],
            [social_events.archive_prompt_key(date_id, self.viewer_id)],
        )

        self.assertEqual(tasks.autoarchive_once(self.conn), 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE kind='review_prompt'"
            ).fetchone()[0],
            1,
        )

    def test_bulk_action_deduplicates_ids_and_rejects_foreign_dates(self):
        first = self._date(self.owner_id, "Первое", share_token="bulk-first")
        second = self._date(self.owner_id, "Второе", share_token="bulk-second")
        foreign = self._date(
            self.other_id, "Чужое", share_token="bulk-foreign",
        )
        self.conn.commit()
        request = self._request_for(self.owner_id)

        with patch.object(admin_routes, "user_throttle"):
            response = admin_routes.dates_bulk(
                request,
                BackgroundTasks(),
                action="make_private",
                date_ids=[first, first, second],
                next="/admin/dates?view=active",
                conn=self.conn,
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            [row["is_public"] for row in self.conn.execute(
                "SELECT is_public FROM dates WHERE id IN (?,?) ORDER BY id",
                (first, second),
            )],
            [0, 0],
        )

        self.conn.execute("UPDATE dates SET is_public=1 WHERE id=?", (first,))
        self.conn.commit()
        with patch.object(admin_routes, "user_throttle"):
            with self.assertRaises(HTTPException) as raised:
                admin_routes.dates_bulk(
                    request,
                    BackgroundTasks(),
                    action="make_private",
                    date_ids=[first, foreign],
                    next="/admin/dates",
                    conn=self.conn,
                )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.conn.execute(
            "SELECT is_public FROM dates WHERE id=?", (first,),
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT is_public FROM dates WHERE id=?", (foreign,),
        ).fetchone()[0], 1)

    def test_default_preview_toggle_is_owned_and_public_route_is_fixed(self):
        category_id = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,link_enabled,og_image,"
            "choice_mode,voting_deadline,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.owner_id, "С фиксированным превью", "fixed-preview", 1,
             "custom-preview.webp", "multiple", "2099-01-01T00:00:00", STAMP),
        ).lastrowid)
        self.conn.commit()

        owner_request = self._request_for(self.owner_id)
        response = admin_routes.category_default_preview(
            category_id, owner_request, conn=self.conn,
        )
        self.assertEqual(response.status_code, 303)
        row = self.conn.execute(
            "SELECT use_default_preview,og_image FROM categories WHERE id=?",
            (category_id,),
        ).fetchone()
        self.assertEqual(row["use_default_preview"], 1)
        self.assertEqual(row["og_image"], "custom-preview.webp")

        preview = public_routes.public_og_image("fixed-preview", conn=self.conn)
        self.assertEqual(preview.media_type, "image/jpeg")
        self.assertEqual(
            Path(preview.path).resolve(),
            (APP / "static" / "og-default.jpg").resolve(),
        )
        self.assertEqual(preview.headers["cache-control"], "public, max-age=3600")

        with self.assertRaises(HTTPException) as raised:
            admin_routes.category_default_preview(
                category_id, self._request_for(self.other_id), conn=self.conn,
            )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.conn.execute(
            "SELECT use_default_preview FROM categories WHERE id=?", (category_id,),
        ).fetchone()[0], 1)

        admin_routes.category_default_preview(
            category_id, owner_request, conn=self.conn,
        )
        row = self.conn.execute(
            "SELECT use_default_preview,og_image FROM categories WHERE id=?",
            (category_id,),
        ).fetchone()
        self.assertEqual(tuple(row), (0, "custom-preview.webp"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
