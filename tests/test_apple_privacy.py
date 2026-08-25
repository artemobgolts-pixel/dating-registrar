#!/usr/bin/env python3
"""Регрессии privacy-by-default из Apple Design-аудита."""

from __future__ import annotations

import json
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
_DATA = tempfile.TemporaryDirectory(prefix="date4you-apple-privacy-")
os.environ.update({
    "DATA_DIR": _DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "apple-privacy.test",
    "SECRET_KEY": "apple-privacy-test-secret",
    "TG_BOT_TOKEN": "",
    "TG_BOT_USERNAME": "",
})

import admin_routes  # noqa: E402
import db  # noqa: E402
import public_routes  # noqa: E402


STAMP = "2030-01-01T10:00:00"


def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    return conn


def add_user(conn: sqlite3.Connection, user_id: int, name: str) -> sqlite3.Row:
    conn.execute(
        "INSERT INTO users(id,telegram_id,display_name,created_at) VALUES(?,?,?,?)",
        (user_id, 1000 + user_id, name, STAMP),
    )
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


class SchemaPrivacyTests(unittest.TestCase):
    def test_fresh_schema_defaults_are_private(self):
        conn = memory_db()
        try:
            add_user(conn, 1, "Автор")
            add_user(conn, 2, "Гость")
            conn.execute(
                "INSERT INTO categories(id,owner_id,name,created_at) VALUES(1,1,'Планы',?)",
                (STAMP,),
            )
            conn.execute(
                "INSERT INTO dates(id,owner_id,name,share_token,created_at) "
                "VALUES(1,1,'Прогулка','walk',?)",
                (STAMP,),
            )
            conn.execute(
                "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
                "VALUES(2,1,?,?)",
                (STAMP, STAMP),
            )
            user = conn.execute(
                "SELECT birth_date_public,gender_public FROM users WHERE id=2",
            ).fetchone()
            self.assertEqual(tuple(user), (0, 0))
            self.assertEqual(
                conn.execute("SELECT show_participants FROM categories").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT is_public FROM dates").fetchone()[0], 0,
            )
            self.assertEqual(
                conn.execute("SELECT is_public FROM date_wants").fetchone()[0], 0,
            )
        finally:
            conn.close()

    def test_v31_migration_preserves_existing_visibility_and_changes_defaults(self):
        legacy_schema = db.SCHEMA
        legacy_schema = legacy_schema.replace(
            "    birth_date_public INTEGER NOT NULL DEFAULT 0\n"
            "        CHECK(birth_date_public IN (0, 1)), -- отдельно разрешается для публичного профиля\n"
            "    gender_public INTEGER NOT NULL DEFAULT 0\n"
            "        CHECK(gender_public IN (0, 1)),     -- отдельно разрешается для публичного профиля\n",
            "",
        )
        legacy_schema = legacy_schema.replace(
            "    show_participants INTEGER NOT NULL DEFAULT 0\n"
            "        CHECK(show_participants IN (0, 1)), -- имена и аватары ростера только по opt-in\n",
            "",
        )
        legacy_schema = legacy_schema.replace(
            "    is_public INTEGER NOT NULL DEFAULT 0,   -- 1 = видно в общей ленте комьюнити\n",
            "    is_public INTEGER NOT NULL DEFAULT 1,   -- 1 = видно в общей ленте комьюнити\n",
            1,
        )
        legacy_schema = legacy_schema.replace(
            "    is_public INTEGER NOT NULL DEFAULT 0 CHECK(is_public IN (0, 1)),\n",
            "    is_public INTEGER NOT NULL DEFAULT 1 CHECK(is_public IN (0, 1)),\n",
            1,
        )
        self.assertNotIn("birth_date_public", legacy_schema)
        self.assertNotIn("show_participants", legacy_schema)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(legacy_schema)
            conn.execute(
                "INSERT INTO users(id,telegram_id,display_name,birth_date,gender,created_at) "
                "VALUES(1,1001,'Заполненный','1990-05-01','m',?)",
                (STAMP,),
            )
            conn.execute(
                "INSERT INTO users(id,telegram_id,display_name,created_at) "
                "VALUES(2,1002,'Пустой',?)",
                (STAMP,),
            )
            conn.execute(
                "INSERT INTO categories(id,owner_id,name,created_at) VALUES(1,1,'Старая',?)",
                (STAMP,),
            )
            conn.executemany(
                "INSERT INTO dates(id,owner_id,name,share_token,is_public,created_at) "
                "VALUES(?,?,?,?,?,?)",
                ((1, 1, "Открытое", "open", 1, STAMP),
                 (2, 1, "Закрытое", "closed", 0, STAMP)),
            )
            conn.executemany(
                "INSERT INTO date_wants(user_id,date_id,is_public,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                ((2, 1, 1, STAMP, STAMP), (2, 2, 0, STAMP, STAMP)),
            )
            conn.commit()

            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript(db.MIGRATIONS[31])
            conn.execute("PRAGMA foreign_keys=ON")
            # init_db всегда завершает цепочку этим идемпотентным проходом:
            # он возвращает индексы и триггеры, снятые для безопасного RENAME.
            conn.executescript(db.SCHEMA)

            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_dates_capacity_update'",
            ).fetchone())
            self.assertEqual(
                [tuple(row) for row in conn.execute(
                    "SELECT birth_date_public,gender_public FROM users ORDER BY id"
                )],
                [(1, 1), (0, 0)],
            )
            self.assertEqual(
                conn.execute("SELECT show_participants FROM categories").fetchone()[0],
                1,
            )
            self.assertEqual(
                [row[0] for row in conn.execute("SELECT is_public FROM dates ORDER BY id")],
                [1, 0],
            )
            self.assertEqual(
                [row[0] for row in conn.execute(
                    "SELECT is_public FROM date_wants ORDER BY date_id"
                )],
                [1, 0],
            )
            defaults = {
                row[1]: row[4] for row in conn.execute("PRAGMA table_info(dates)")
            }
            want_defaults = {
                row[1]: row[4] for row in conn.execute("PRAGMA table_info(date_wants)")
            }
            self.assertEqual(defaults["is_public"], "0")
            self.assertEqual(want_defaults["is_public"], "0")
        finally:
            conn.close()


class ProfilePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.user = add_user(self.conn, 1, "Алина")
        self.request = SimpleNamespace(state=SimpleNamespace(user=self.user))

    def tearDown(self):
        self.conn.close()

    def test_profile_visibility_is_saved_independently(self):
        admin_routes.profile_save(
            self.request,
            display_name="Алина",
            birth_date="1995-06-15",
            gender="f",
            birth_date_public="1",
            gender_public=None,
            cursor_effects=None,
            admin_skin="friends",
            avatar=None,
            conn=self.conn,
        )
        row = self.conn.execute(
            "SELECT birth_date_public,gender_public FROM users WHERE id=1",
        ).fetchone()
        self.assertEqual(tuple(row), (1, 0))

        template = (APP / "templates/public/profile.html").read_text("utf-8")
        self.assertIn("is_me or u['gender_public']", template)
        self.assertIn("is_me or u['birth_date_public']", template)


class WantsAndRosterPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.owner = add_user(self.conn, 1, "Автор")
        self.viewer = add_user(self.conn, 2, "Гость")
        self.conn.execute(
            "INSERT INTO categories(id,owner_id,name,link_token,choice_mode,"
            "voting_deadline,created_at) VALUES(1,1,'Планы','category-token',"
            "'multiple','2035-01-01T10:00:00',?)",
            (STAMP,),
        )
        self.conn.execute(
            "INSERT INTO dates(id,owner_id,name,share_token,is_public,created_at) "
            "VALUES(1,1,'Прогулка','date-token',1,?)",
            (STAMP,),
        )
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(1,1)",
        )
        self.conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
            "VALUES(1,1,'u2',2,?)",
            (STAMP,),
        )
        self.conn.commit()
        self.request = SimpleNamespace(
            state=SimpleNamespace(user=self.viewer),
            headers={"x-requested-with": "fetch"},
        )

    def tearDown(self):
        self.conn.close()

    @patch.object(public_routes, "guest_throttle", lambda *args, **kwargs: None)
    @patch.object(public_routes.social_events, "queue_review_prompt", lambda *args, **kwargs: None)
    @patch.object(public_routes.social_events, "cancel_review_prompt", lambda *args, **kwargs: 0)
    def test_want_defaults_private_and_explicit_visibility_can_change(self):
        response = public_routes.shared_date_want(
            "date-token", self.request, visibility=None, conn=self.conn,
        )
        payload = json.loads(response.body)
        self.assertTrue(payload["wanted"])
        self.assertEqual(payload["want_visibility"], "private")
        self.assertEqual(
            self.conn.execute("SELECT is_public FROM date_wants").fetchone()[0], 0,
        )

        response = public_routes.shared_date_want(
            "date-token", self.request, visibility="public", conn=self.conn,
        )
        payload = json.loads(response.body)
        self.assertTrue(payload["wanted"])
        self.assertTrue(payload["want_is_public"])
        self.assertEqual(
            self.conn.execute("SELECT is_public FROM date_wants").fetchone()[0], 1,
        )

        response = public_routes.shared_date_want(
            "date-token", self.request, visibility=None, conn=self.conn,
        )
        payload = json.loads(response.body)
        self.assertFalse(payload["wanted"])
        self.assertIsNone(payload["want_visibility"])

    def test_hidden_roster_keeps_count_but_removes_identities_and_avatars(self):
        updates = public_routes._vote_card_updates(
            self.conn, 1, [1], "u2", show_participants=False,
        )
        self.assertEqual(updates[0]["vote_count"], 1)
        self.assertTrue(updates[0]["mine"])
        self.assertEqual(updates[0]["participants"], [])
        self.assertEqual(updates[0]["hidden_count"], 0)

        with self.assertRaises(HTTPException) as category_error:
            public_routes.public_participant_avatar(
                "category-token", 2, conn=self.conn,
            )
        self.assertEqual(category_error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as share_error:
            public_routes.shared_participant_avatar(
                "date-token", 2, conn=self.conn,
            )
        self.assertEqual(share_error.exception.status_code, 404)

        request = SimpleNamespace(state=SimpleNamespace(user=self.owner))
        admin_routes.category_participants_visibility(
            1, request, enabled="1", conn=self.conn,
        )
        updates = public_routes._vote_card_updates(
            self.conn, 1, [1], "u2", show_participants=True,
        )
        self.assertEqual(updates[0]["participants"][0]["name"], "Гость")

    def test_shared_date_exposes_only_safe_active_category_context(self):
        self.conn.executemany(
            "INSERT INTO dates(id,owner_id,name,share_token,is_draft,archived_at,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ((2, 1, "Второй", "second", 0, None, STAMP),
             (3, 1, "Черновик", "draft", 1, None, STAMP),
             (4, 1, "Архив", "archive", 0, STAMP, STAMP)),
        )
        self.conn.executemany(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,1)",
            ((2,), (3,), (4,)),
        )
        self.conn.commit()
        request = SimpleNamespace(session={}, query_params={})

        with patch.object(
            public_routes.templates,
            "TemplateResponse",
            side_effect=lambda request, name, context, **kwargs: SimpleNamespace(
                context=context, headers={},
            ),
        ):
            response = public_routes.shared_date(
                "date-token", request, conn=self.conn,
            )
        context = response.context

        self.assertEqual(context["active_category"], {
            "id": 1,
            "name": "Планы",
            "link_token": "category-token",
        })
        self.assertEqual(context["active_category_event_count"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
