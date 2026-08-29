#!/usr/bin/env python3
"""Точечные тесты «Хочу сходить», обзоров и review-notifications."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-social-import-")
os.environ["DATA_DIR"] = _IMPORT_DATA.name
os.environ.setdefault("SECRET_KEY", "social-test-secret")
os.environ.setdefault("DOMAIN", "social.test")

import db  # noqa: E402
import notify  # noqa: E402
import social_events  # noqa: E402


class SocialEventsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        for user_id, tg, name in ((1, 101, "Автор"), (2, 202, "Гость")):
            self.conn.execute(
                "INSERT INTO users(id, telegram_id, display_name, created_at) "
                "VALUES(?,?,?,?)",
                (user_id, tg, name, "2030-01-01T00:00:00"),
            )

    def tearDown(self):
        self.conn.close()

    def _category(self, cid: int, deadline: str) -> None:
        self.conn.execute(
            "INSERT INTO categories(id, owner_id, name, choice_mode, "
            "voting_deadline, created_at) VALUES(?,1,?,?,?,?)",
            (cid, f"Категория {cid}", "multiple", deadline,
             "2030-01-01T00:00:00"),
        )

    def _date(self, did: int, *, starts: str | None, ends: str | None) -> None:
        self.conn.execute(
            "INSERT INTO dates(id, owner_id, name, starts_at, ends_at, share_token, "
            "is_draft, is_public, created_at) VALUES(?,1,?,?,?,?,0,1,?)",
            (did, f"Событие {did}", starts, ends, f"token-{did}",
             "2030-01-01T00:00:00"),
        )

    def _want(self, did: int) -> None:
        self.conn.execute(
            "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
            "VALUES(2,?,?,?)",
            (did, "2030-01-01T00:00:00", "2030-01-01T00:00:00"),
        )

    def test_due_waits_for_event_and_uses_latest_category_deadline(self):
        self._category(1, "2030-01-02T10:00:00")
        self._category(2, "2030-01-03T10:00:00")
        self._date(1, starts="2030-01-04T18:00:00", ends=None)
        self.conn.executemany(
            "INSERT INTO date_categories(date_id,category_id) VALUES(1,?)",
            [(1,), (2,)],
        )
        self.assertEqual(
            social_events.review_due(self.conn, 1),
            datetime(2030, 1, 4, 21, 0),
        )

    def test_undated_event_uses_latest_deadline_without_extra_day(self):
        self._category(1, "2030-01-02T10:00:00")
        self._category(2, "2030-01-05T12:30:00")
        self._date(1, starts=None, ends=None)
        self.conn.executemany(
            "INSERT INTO date_categories(date_id,category_id) VALUES(1,?)",
            [(1,), (2,)],
        )
        self.assertEqual(
            social_events.review_due(self.conn, 1),
            datetime(2030, 1, 5, 12, 30),
        )

        self._date(2, starts=None, ends=None)
        self.assertEqual(
            social_events.review_due(self.conn, 2),
            datetime(2030, 1, 2, 0, 0),
        )

    def test_current_wants_use_review_due_and_do_not_duplicate_reviews(self):
        now = datetime(2030, 1, 3, 12, 0)
        self._category(1, "2030-01-02T10:00:00")
        self._category(2, "2029-12-30T10:00:00")
        self._category(3, "2030-01-02T11:00:00")
        self._category(4, "2030-01-04T10:00:00")
        self._date(1, starts="2030-01-04T18:00:00", ends=None)
        self._date(2, starts="2030-01-01T18:00:00", ends="2030-01-01T20:00:00")
        self._date(3, starts="2030-01-05T18:00:00", ends=None)
        self._date(4, starts="2030-01-05T18:00:00", ends=None)
        self.conn.executemany(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            [(1, 1), (2, 2), (3, 3), (4, 4)],
        )
        for date_id in (1, 2, 3, 4):
            self._want(date_id)
        self.conn.execute(
            "INSERT INTO date_reviews(user_id,date_id,rating,created_at,updated_at) "
            "VALUES(2,3,5,?,?)",
            ("2030-01-03T10:00:00", "2030-01-03T10:00:00"),
        )

        # Review по-прежнему ждёт саму встречу, но «Хочу сходить» выполняет
        # буквальный пользовательский контракт и закрывается уже по дедлайну.
        self.assertGreater(social_events.review_due(self.conn, 1), now)
        traced = []
        self.conn.set_trace_callback(traced.append)
        current_ids = social_events.current_want_date_ids(
            self.conn, 2, [1, 2, 3, 4], now=now,
        )
        self.conn.set_trace_callback(None)
        self.assertEqual(current_ids, {4})
        self.assertEqual(
            sum(statement.lstrip().upper().startswith("SELECT") for statement in traced),
            2,
        )
        self.assertFalse(social_events.want_is_current(self.conn, 1, 2, now=now))
        self.assertFalse(social_events.want_is_current(self.conn, 2, 2, now=now))
        self.assertFalse(social_events.want_is_current(self.conn, 3, 2, now=now))
        self.assertTrue(social_events.want_is_current(self.conn, 4, 2, now=now))
        self.assertFalse(social_events.want_action_available(
            self.conn, 1, 2, now=now,
        ))
        self.assertTrue(social_events.want_action_available(
            self.conn, 4, 2, now=now,
        ))

    def test_one_prompt_per_user_and_date_is_rescheduled_and_cancelled_by_review(self):
        self._category(1, "2030-01-02T10:00:00")
        self._date(1, starts="2030-01-03T18:00:00", ends="2030-01-03T20:00:00")
        self.conn.execute("INSERT INTO date_categories(date_id,category_id) VALUES(1,1)")
        self._want(1)

        first = social_events.queue_review_prompt(self.conn, 1, 2)
        second = social_events.queue_review_prompt(self.conn, 1, 2)
        self.assertEqual(first, second)
        rows = self.conn.execute(
            "SELECT kind, event_key, send_at, action_label FROM notification_outbox"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "review_prompt")
        self.assertEqual(rows[0]["send_at"], "2030-01-03T20:00:00")
        self.assertEqual(rows[0]["action_label"], "Оставить отзыв")
        self.assertEqual(notify.preference_for_kind("review_prompt"), "reviews")

        self.conn.execute(
            "INSERT INTO date_reviews(user_id,date_id,rating,text,created_at,updated_at) "
            "VALUES(2,1,5,NULL,?,?)",
            ("2030-01-04T00:00:00", "2030-01-04T00:00:00"),
        )
        self.assertIsNone(social_events.queue_review_prompt(self.conn, 1, 2))
        cancelled = self.conn.execute(
            "SELECT cancelled_at,last_error FROM notification_outbox"
        ).fetchone()
        self.assertIsNotNone(cancelled["cancelled_at"])
        self.assertEqual(cancelled["last_error"], "review_exists")

    def test_review_fanout_query_count_scales_by_chunks_not_users(self):
        self._date(10, starts="2030-01-03T18:00:00",
                   ends="2030-01-03T20:00:00")
        self._date(11, starts="2030-01-03T18:00:00",
                   ends="2030-01-03T20:00:00")
        self.conn.executemany(
            "INSERT INTO users(id,telegram_id,display_name,created_at) "
            "VALUES(?,?,?,?)",
            ((user_id, 1000 + user_id, f"Участник {user_id}",
              "2030-01-01T00:00:00") for user_id in range(3, 102)),
        )
        user_ids = list(range(2, 102))
        self.conn.executemany(
            "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
            "VALUES(?,?,?,?)",
            ((user_id, date_id, "2030-01-01T00:00:00",
              "2030-01-01T00:00:00")
             for date_id in (10, 11) for user_id in user_ids),
        )

        traced: list[str] = []
        self.conn.set_trace_callback(traced.append)
        self.assertEqual(
            social_events.queue_review_prompts_for_date(self.conn, 10), 100,
        )
        self.conn.set_trace_callback(None)
        selects = sum(sql.lstrip().upper().startswith("SELECT") for sql in traced)
        outbox_inserts = sum(
            sql.lstrip().upper().startswith("INSERT INTO NOTIFICATION_OUTBOX")
            for sql in traced
        )
        self.assertEqual(selects, 3)
        self.assertEqual(outbox_inserts, 2)  # 80 + 20 rows

        traced.clear()
        self.conn.set_trace_callback(traced.append)
        queued, waiting = social_events.queue_archive_review_fanout(
            self.conn, 11, now=datetime(2030, 1, 4, 12, 0),
        )
        self.conn.set_trace_callback(None)
        self.assertEqual((queued, waiting), (100, 100))
        self.assertEqual(
            sum(sql.lstrip().upper().startswith("SELECT") for sql in traced), 4,
        )
        self.assertEqual(
            sum(sql.lstrip().upper().startswith(
                "INSERT INTO NOTIFICATION_OUTBOX") for sql in traced),
            2,
        )
        self.assertEqual(
            sum(sql.lstrip().upper().startswith(
                "INSERT INTO REVIEW_QUEUE") for sql in traced),
            1,
        )

    def test_rating_constraint_and_delete_cascade(self):
        self._date(1, starts=None, ends=None)
        self._want(1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO date_reviews(user_id,date_id,rating,created_at,updated_at) "
                "VALUES(2,1,6,?,?)",
                ("2030-01-01T00:00:00", "2030-01-01T00:00:00"),
            )
        self.conn.execute(
            "INSERT INTO date_reviews(user_id,date_id,rating,created_at,updated_at) "
            "VALUES(2,1,5,?,?)",
            ("2030-01-01T00:00:00", "2030-01-01T00:00:00"),
        )
        self.conn.execute("DELETE FROM dates WHERE id=1")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM date_wants").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM date_reviews").fetchone()[0], 0)

    def test_v28_backfills_legacy_category_deadline(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                """
                CREATE TABLE users(id INTEGER PRIMARY KEY);
                CREATE TABLE dates(id INTEGER PRIMARY KEY);
                CREATE TABLE categories(
                    id INTEGER PRIMARY KEY,
                    choice_mode TEXT,
                    voting_deadline TEXT
                );
                CREATE TABLE notification_preferences(
                    user_id INTEGER PRIMARY KEY,
                    votes INTEGER NOT NULL DEFAULT 1,
                    questions INTEGER NOT NULL DEFAULT 1,
                    proposals INTEGER NOT NULL DEFAULT 1,
                    updates INTEGER NOT NULL DEFAULT 1,
                    reminders INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT
                );
                INSERT INTO categories(id,choice_mode,voting_deadline)
                    VALUES(1,NULL,NULL);
                """
            )
            conn.executescript(db.MIGRATIONS[28])
            row = conn.execute(
                "SELECT choice_mode,voting_deadline FROM categories WHERE id=1"
            ).fetchone()
            self.assertEqual(row[0], "multiple")
            self.assertTrue(row[1])
            self.assertIn(
                "reviews",
                {column[1] for column in conn.execute(
                    "PRAGMA table_info(notification_preferences)"
                )},
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
