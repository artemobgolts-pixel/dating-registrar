#!/usr/bin/env python3
"""Регрессии постоянной публичности полей профиля и ростера голосования."""

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


class SchemaVisibilityTests(unittest.TestCase):
    def test_fresh_schema_has_no_profile_or_roster_visibility_switches(self):
        conn = memory_db()
        try:
            user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            category_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(categories)")
            }
            self.assertNotIn("birth_date_public", user_columns)
            self.assertNotIn("gender_public", user_columns)
            self.assertNotIn("show_participants", category_columns)

            add_user(conn, 1, "Автор")
            conn.execute(
                "INSERT INTO dates(id,owner_id,name,share_token,created_at) "
                "VALUES(1,1,'Прогулка','walk',?)",
                (STAMP,),
            )
            conn.execute(
                "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
                "VALUES(1,1,?,?)",
                (STAMP, STAMP),
            )
            # Публичность общей ленты и личной отметки остаётся отдельным выбором.
            self.assertEqual(conn.execute("SELECT is_public FROM dates").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT is_public FROM date_wants").fetchone()[0], 0,
            )
        finally:
            conn.close()

    def test_v34_removes_legacy_switches_without_losing_profile_or_category(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    display_name TEXT,
                    birth_date TEXT,
                    gender TEXT,
                    birth_date_public INTEGER NOT NULL DEFAULT 0,
                    gender_public INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE categories (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    show_participants INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO users VALUES(1, 'Алина', '1995-06-15', 'f', 0, 0);
                INSERT INTO categories VALUES(1, 'Планы', 0);
            """)
            self.assertEqual(db.LATEST_VERSION, 34)
            conn.executescript(db.MIGRATIONS[34])

            user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            category_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(categories)")
            }
            self.assertNotIn("birth_date_public", user_columns)
            self.assertNotIn("gender_public", user_columns)
            self.assertNotIn("show_participants", category_columns)
            self.assertEqual(
                tuple(conn.execute(
                    "SELECT display_name,birth_date,gender FROM users WHERE id=1"
                ).fetchone()),
                ("Алина", "1995-06-15", "f"),
            )
            self.assertEqual(
                conn.execute("SELECT name FROM categories WHERE id=1").fetchone()[0],
                "Планы",
            )
        finally:
            conn.close()


class ProfileVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.user = add_user(self.conn, 1, "Алина")
        self.request = SimpleNamespace(state=SimpleNamespace(user=self.user))

    def tearDown(self):
        self.conn.close()

    def test_filled_birth_date_and_gender_are_always_public(self):
        admin_routes.profile_save(
            self.request,
            display_name="Алина",
            birth_date="1995-06-15",
            gender="f",
            cursor_effects=None,
            admin_skin="friends",
            avatar=None,
            conn=self.conn,
        )
        self.assertEqual(
            tuple(self.conn.execute(
                "SELECT birth_date,gender FROM users WHERE id=1",
            ).fetchone()),
            ("1995-06-15", "f"),
        )

        form = (APP / "templates/admin/profile.html").read_text("utf-8")
        public = (APP / "templates/public/profile.html").read_text("utf-8")
        privacy = (APP / "templates/public/_privacy.html").read_text("utf-8")
        route = (APP / "admin_routes.py").read_text("utf-8")
        self.assertNotIn("birth_date_public", form + public + route)
        self.assertNotIn("gender_public", form + public + route)
        self.assertIn("{% if u['gender'] == 'm' %}", public)
        self.assertIn("{% if u['birth_date'] %}", public)
        self.assertIn("Указанные дата рождения и пол отображаются", privacy)
        self.assertNotIn("скрыты до", privacy)


class EventCreationDefaultsTests(unittest.TestCase):
    def test_new_event_starts_public_but_editing_preserves_saved_choice(self):
        template = (APP / "templates/admin/date_form.html").read_text("utf-8")
        self.assertIn(
            "{% if not date or date['is_public'] %}checked{% endif %}",
            template,
        )
        # Сервер по-прежнему отличает сознательно снятый checkbox от дефолта UI.
        self.assertIn("is_public: str | None = Form(None)",
                      (APP / "admin_routes.py").read_text("utf-8"))


class WantsAndRosterVisibilityTests(unittest.TestCase):
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
        self.conn.execute("INSERT INTO date_categories(date_id,category_id) VALUES(1,1)")
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
    @patch.object(
        public_routes.social_events, "queue_review_prompt", lambda *args, **kwargs: None,
    )
    @patch.object(
        public_routes.social_events, "cancel_review_prompt", lambda *args, **kwargs: 0,
    )
    def test_want_defaults_private_and_explicit_visibility_can_change(self):
        response = public_routes.shared_date_want(
            "date-token", self.request, visibility=None, conn=self.conn,
        )
        payload = json.loads(response.body)
        self.assertTrue(payload["wanted"])
        self.assertEqual(payload["want_visibility"], "private")

        response = public_routes.shared_date_want(
            "date-token", self.request, visibility="public", conn=self.conn,
        )
        payload = json.loads(response.body)
        self.assertTrue(payload["want_is_public"])

    def test_roster_identities_are_always_returned_and_have_no_setting(self):
        updates = public_routes._vote_card_updates(self.conn, 1, [1], "u2")
        self.assertEqual(updates[0]["vote_count"], 1)
        self.assertTrue(updates[0]["mine"])
        self.assertEqual(updates[0]["participants"][0]["name"], "Гость")

        sources = "".join((APP / path).read_text("utf-8") for path in (
            "admin_routes.py",
            "public_routes.py",
            "templates/admin/category_detail.html",
            "templates/public/category.html",
            "templates/public/share.html",
            "static/guest.js",
        ))
        self.assertNotIn("show_participants", sources)
        self.assertNotIn("category_participants_visibility", sources)
        self.assertNotIn("participant-privacy-setting", sources)
        self.assertIn("имена и\n  аватары участников во всех вариантах", (
            APP / "templates/public/_privacy.html"
        ).read_text("utf-8"))

    def test_roster_avatars_stay_available_for_ties_and_finished_options(self):
        self.conn.execute(
            "UPDATE users SET avatar_path='guest-avatar.webp' WHERE id=2",
        )
        self.conn.execute(
            "UPDATE categories SET voting_status='tie', closed_at=? WHERE id=1",
            (STAMP,),
        )
        self.conn.commit()

        with patch.object(
            public_routes.images, "responsive_image", return_value=Path(__file__),
        ):
            category_avatar = public_routes.public_participant_avatar(
                "category-token", 2, conn=self.conn,
            )
            shared_avatar = public_routes.shared_participant_avatar(
                "date-token", 2, conn=self.conn,
            )
        self.assertEqual(category_avatar.status_code, 200)
        self.assertEqual(shared_avatar.status_code, 200)

    def test_shared_date_keeps_category_context_internal_to_voting(self):
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
            response = public_routes.shared_date("date-token", request, conn=self.conn)
        self.assertNotIn("active_category", response.context)
        self.assertNotIn("active_category_event_count", response.context)
        self.assertTrue(response.context["can_act"])
        self.assertIsNotNone(response.context["vote_state"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
