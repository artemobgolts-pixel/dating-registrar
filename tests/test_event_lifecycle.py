#!/usr/bin/env python3
"""Жизненный цикл событий без пользовательского статуса «Неактивные»."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-lifecycle-import-")
os.environ["DATA_DIR"] = _IMPORT_DATA.name
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_PASSWORD", "test")

import db  # noqa: E402
import social_events  # noqa: E402
import tasks  # noqa: E402


STAMP = "2030-01-01T00:00:00"


class EventLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="date4you-lifecycle-")
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "app.db"
        db.init_db()
        self.conn = None

    def tearDown(self):
        if self.conn is not None:
            self.conn.close()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _user(self, conn: sqlite3.Connection, telegram_id: int, name: str) -> int:
        return int(conn.execute(
            "INSERT INTO users(telegram_id,display_name,created_at) VALUES(?,?,?)",
            (telegram_id, name, STAMP),
        ).lastrowid)

    def _date(self, conn: sqlite3.Connection, owner_id: int, name: str, **values) -> int:
        fields = ["owner_id", "name", "created_at"]
        params = [owner_id, name, STAMP]
        for key, value in values.items():
            fields.append(key)
            params.append(value)
        marks = ",".join("?" for _ in fields)
        return int(conn.execute(
            f"INSERT INTO dates({','.join(fields)}) VALUES({marks})", params,
        ).lastrowid)

    def test_v29_activates_owner_drafts_without_publishing_hidden_content(self):
        conn = db.connect()
        self.conn = conn
        owner = self._user(conn, 81001, "Владелец")
        ordinary = self._date(
            conn, owner, "Без категории", origin="admin", is_draft=1, is_public=1,
        )
        guest = self._date(
            conn, owner, "На модерации", origin="guest", is_draft=1, is_public=1,
        )
        archived = self._date(
            conn, owner, "Старый архив", origin="admin", is_draft=1,
            is_public=1, archived_at=STAMP,
        )
        conflict = self._date(
            conn, owner, "Конфликт дедлайна", origin="admin", is_draft=1,
            is_public=1, starts_at="2030-01-02T10:00:00",
        )
        category = int(conn.execute(
            "INSERT INTO categories(owner_id,name,choice_mode,voting_deadline,"
            "voting_status,created_at) VALUES(?,?,?,?,?,?)",
            (owner, "Открытая", "single", "2030-01-03T10:00:00", "open", STAMP),
        ).lastrowid)
        conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            (conflict, category),
        )
        # Свежая фикстура уже содержит объекты v30–v35; убираем их, чтобы версия
        # 28 честно прогнала все последующие миграции.
        conn.execute("DROP TABLE review_queue")
        conn.execute("ALTER TABLE categories DROP COLUMN use_default_preview")
        conn.execute("ALTER TABLE categories DROP COLUMN private_profiles")
        conn.execute("ALTER TABLE categories DROP COLUMN prevent_copying")
        conn.execute("ALTER TABLE categories DROP COLUMN pin_enabled")
        conn.execute("ALTER TABLE categories DROP COLUMN access_pin_hash")
        conn.execute("DROP INDEX idx_dates_owner_source")
        conn.execute("ALTER TABLE dates DROP COLUMN source_date_id")
        conn.execute("PRAGMA user_version=28")
        conn.commit()
        conn.close()
        self.conn = None

        db.init_db()
        conn = db.connect()
        self.conn = conn
        try:
            rows = {
                int(row["id"]): row
                for row in conn.execute(
                    "SELECT id,is_draft,is_public,archived_at FROM dates"
                )
            }
            self.assertEqual((rows[ordinary]["is_draft"], rows[ordinary]["is_public"]),
                             (0, 0))
            self.assertEqual((rows[guest]["is_draft"], rows[guest]["is_public"]),
                             (1, 1), "гостевое предложение нельзя авто-публиковать")
            self.assertEqual((rows[archived]["is_draft"], rows[archived]["is_public"]),
                             (1, 1), "архив миграция не переписывает")
            self.assertEqual((rows[conflict]["is_draft"], rows[conflict]["is_public"]),
                             (1, 0), "конфликт DB-инварианта остаётся скрытым")
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], db.LATEST_VERSION,
            )
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='idx_categories_winner_date'",
            ).fetchone())
            self.assertNotIn("birth_date_public", {
                row["name"] for row in conn.execute("PRAGMA table_info(users)")
            })
            self.assertNotIn("gender_public", {
                row["name"] for row in conn.execute("PRAGMA table_info(users)")
            })
            self.assertNotIn("show_participants", {
                row["name"] for row in conn.execute("PRAGMA table_info(categories)")
            })
        finally:
            conn.close()
            self.conn = None

    def test_only_review_by_actual_winner_participant_archives_undated_winner(self):
        conn = db.connect()
        self.conn = conn
        owner = self._user(conn, 82001, "Организатор")
        participant = self._user(conn, 82002, "Участник")
        outsider = self._user(conn, 82003, "Посторонний")
        winner = self._date(conn, owner, "Победитель")
        category = int(conn.execute(
            "INSERT INTO categories(owner_id,name,choice_mode,voting_deadline,"
            "voting_status,created_at) VALUES(?,?,?,?,?,?)",
            (owner, "Выбор", "single", "2030-12-31T10:00:00", "open", STAMP),
        ).lastrowid)
        conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            (winner, category),
        )
        conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
            "VALUES(?,?,?,?,?)",
            (winner, category, f"u{participant}", participant, STAMP),
        )
        conn.execute(
            "UPDATE categories SET voting_status='resolved', closed_at=?, resolved_at=?, "
            "winner_date_id=? WHERE id=?", (STAMP, STAMP, winner, category),
        )
        pending_category = int(conn.execute(
            "INSERT INTO categories(owner_id,name,choice_mode,voting_deadline,"
            "voting_status,created_at) VALUES(?,?,?,?,?,?)",
            (owner, "Ещё голосуют", "single", "2031-12-31T10:00:00", "open", STAMP),
        ).lastrowid)
        conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            (winner, pending_category),
        )
        conn.execute(
            "INSERT INTO date_reviews(user_id,date_id,rating,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (outsider, winner, 5, STAMP, STAMP),
        )
        conn.commit()

        after_due = datetime(2032, 1, 2, 12, 0, 0)
        self.assertTrue(
            social_events.review_available(
                conn, winner, participant, now=after_due,
            ),
            "голос за победителя сам даёт право на обзор",
        )
        self.assertFalse(
            social_events.review_available(conn, winner, outsider, now=after_due),
        )

        self.assertEqual(tasks.autoarchive_once(conn), 0)
        self.assertIsNone(conn.execute(
            "SELECT archived_at FROM dates WHERE id=?", (winner,),
        ).fetchone()[0])

        conn.execute(
            "INSERT INTO date_reviews(user_id,date_id,rating,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (participant, winner, 5, STAMP, STAMP),
        )
        conn.commit()
        self.assertEqual(tasks.autoarchive_once(conn), 0,
                         "открытая вторая категория не должна потерять вариант")
        conn.execute(
            "UPDATE categories SET voting_status='no_winner', closed_at=? WHERE id=?",
            (STAMP, pending_category),
        )
        conn.commit()
        self.assertEqual(tasks.autoarchive_once(conn), 1)
        self.assertIsNotNone(conn.execute(
            "SELECT archived_at FROM dates WHERE id=?", (winner,),
        ).fetchone()[0])
        conn.close()
        self.conn = None

    def test_withdrawn_participant_review_does_not_archive_winner(self):
        conn = db.connect()
        self.conn = conn
        owner = self._user(conn, 83001, "Организатор")
        participant = self._user(conn, 83002, "Отказавшийся")
        winner = self._date(conn, owner, "Победитель после отказа")
        category = int(conn.execute(
            "INSERT INTO categories(owner_id,name,choice_mode,voting_deadline,"
            "voting_status,created_at) VALUES(?,?,?,?,?,?)",
            (owner, "Выбор", "single", "2030-12-31T10:00:00", "open", STAMP),
        ).lastrowid)
        conn.execute("INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
                     (winner, category))
        conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,"
            "participation_withdrawn_at,created_at) VALUES(?,?,?,?,?,?)",
            (winner, category, f"u{participant}", participant, STAMP, STAMP),
        )
        conn.execute(
            "UPDATE categories SET voting_status='resolved', closed_at=?, resolved_at=?, "
            "winner_date_id=? WHERE id=?", (STAMP, STAMP, winner, category),
        )
        conn.execute(
            "INSERT INTO date_reviews(user_id,date_id,rating,created_at,updated_at) "
            "VALUES(?,?,?,?,?)", (participant, winner, 4, STAMP, STAMP),
        )
        conn.commit()
        self.assertEqual(tasks.autoarchive_once(conn), 0)
        self.assertIsNone(conn.execute(
            "SELECT archived_at FROM dates WHERE id=?", (winner,),
        ).fetchone()[0])
        conn.close()
        self.conn = None

    def test_archived_undated_event_is_not_a_current_want(self):
        conn = db.connect()
        self.conn = conn
        owner = self._user(conn, 84001, "Организатор")
        viewer = self._user(conn, 84002, "Гость")
        archived = self._date(
            conn, owner, "Архив без даты", archived_at=STAMP,
        )
        conn.execute(
            "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
            "VALUES(?,?,?,?)", (viewer, archived, STAMP, STAMP),
        )
        conn.commit()

        self.assertEqual(
            social_events.current_want_date_ids(conn, viewer, [archived]), set(),
        )
        self.assertFalse(
            social_events.want_action_available(conn, archived, viewer),
        )
        conn.close()
        self.conn = None

    def test_late_start_waits_three_hours_and_does_not_prompt_want_user(self):
        conn = db.connect()
        self.conn = conn
        owner = self._user(conn, 85001, "Организатор")
        viewer = self._user(conn, 85002, "Участник")
        late = self._date(
            conn, owner, "Поздняя встреча",
            starts_at="2030-01-01T23:30:00", share_token="late-review",
        )
        conn.execute(
            "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
            "VALUES(?,?,?,?)", (viewer, late, STAMP, STAMP),
        )
        conn.commit()

        with patch.object(
                tasks, "now_naive", return_value=datetime(2030, 1, 1, 23, 59, 59)), \
                patch.object(tasks, "now_iso", return_value="2030-01-01T23:59:59"):
            self.assertEqual(tasks.autoarchive_once(conn), 0)
        self.assertIsNone(conn.execute(
            "SELECT archived_at FROM dates WHERE id=?", (late,),
        ).fetchone()[0])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM review_queue WHERE date_id=?", (late,),
        ).fetchone()[0], 0)

        with patch.object(
                tasks, "now_naive", return_value=datetime(2030, 1, 2, 2, 31, 0)), \
                patch.object(tasks, "now_iso", return_value="2030-01-02T02:31:00"):
            self.assertEqual(tasks.autoarchive_once(conn), 1)
        self.assertEqual(conn.execute(
            "SELECT reason FROM review_queue WHERE user_id=? AND date_id=?",
            (viewer, late),
        ).fetchone()[0], "due")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM notification_outbox "
            "WHERE user_id=? AND kind='review_prompt'",
            (viewer,),
        ).fetchone()[0], 0)
        conn.close()
        self.conn = None

    def test_autoarchive_parses_only_due_range_candidates(self):
        conn = db.connect()
        self.conn = conn
        owner_id = self._user(conn, 86001, "Владелец")
        conn.executemany(
            "INSERT INTO dates(owner_id,name,starts_at,ends_at,created_at) "
            "VALUES(?,?,?,?,?)",
            [
                (owner_id, f"future-end-{i}", "2030-01-02T09:00:00",
                 "2030-01-02T12:00:00", STAMP)
                for i in range(500)
            ] + [
                (owner_id, f"future-start-{i}", "2030-01-02T09:00:00",
                 None, STAMP)
                for i in range(500)
            ] + [
                (owner_id, "due-end", "2029-12-31T09:00:00",
                 "2030-01-01T10:00:00", STAMP),
                (owner_id, "due-start", "2029-12-31T20:00:00", None, STAMP),
                (owner_id, "due-invalid-end", "2029-12-31T20:00:00",
                 "not-an-iso-time", STAMP),
            ],
        )
        real_parse = tasks._parse
        with patch.object(
                tasks, "now_naive", return_value=datetime(2030, 1, 1, 12, 0)), \
                patch.object(tasks, "_parse", wraps=real_parse) as parse_spy:
            self.assertEqual(tasks.autoarchive_once(conn), 3)
        self.assertEqual(parse_spy.call_count, 5)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM dates WHERE archived_at IS NOT NULL",
        ).fetchone()[0], 3)
        conn.close()
        self.conn = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
