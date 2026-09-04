#!/usr/bin/env python3
"""Точечные регрессии административной части поиска и приватности."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-admin-search-")
os.environ.update({
    "DATA_DIR": _DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "admin-search.test",
    "SECRET_KEY": "admin-search-test-secret",
    "TG_BOT_TOKEN": "",
    "TG_BOT_USERNAME": "",
})

import admin_routes  # noqa: E402
import category_access  # noqa: E402
import db  # noqa: E402


STAMP = "2030-01-01T10:00:00"


def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    return conn


def add_user(conn: sqlite3.Connection, *, limit: int = 100) -> sqlite3.Row:
    conn.execute(
        "INSERT INTO users(id,telegram_id,display_name,date_limit,created_at) "
        "VALUES(1,1001,'Алина',?,?)",
        (limit, STAMP),
    )
    return conn.execute("SELECT * FROM users WHERE id=1").fetchone()


def request_for(user: sqlite3.Row, **query) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(user=user),
        query_params=query,
        cookies={},
        session={},
    )


class AdminSearchPrivacyQuotaTests(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.user = add_user(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_category(self, category_id: int, name: str, description: str = "") -> None:
        self.conn.execute(
            "INSERT INTO categories(id,owner_id,name,description,link_token,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (category_id, 1, name, description, f"category-{category_id}", STAMP),
        )

    def test_collection_search_treats_like_metacharacters_literally(self):
        self.add_category(1, "Поход 100%")
        self.add_category(2, "Поход 100 дней")
        rows = admin_routes._categories_list_data(
            self.conn, 1, query="100%",
        )
        self.assertEqual([row["id"] for row in rows], [1])

    def test_privacy_requires_four_digits_and_preserves_existing_hash(self):
        self.add_category(1, "Планы")
        request = request_for(self.user)
        with self.assertRaises(HTTPException):
            admin_routes.category_privacy_save(
                1, request, pin_enabled="1", access_pin="12a4", conn=self.conn,
            )

        admin_routes.category_privacy_save(
            1, request,
            private_profiles="1", prevent_copying="1", pin_enabled="1",
            access_pin="0123", conn=self.conn,
        )
        first = self.conn.execute("SELECT * FROM categories WHERE id=1").fetchone()
        self.assertTrue(category_access.verify_pin("0123", first["access_pin_hash"]))
        first_hash = first["access_pin_hash"]

        admin_routes.category_privacy_save(
            1, request, pin_enabled="1", access_pin="", conn=self.conn,
        )
        saved = self.conn.execute("SELECT * FROM categories WHERE id=1").fetchone()
        self.assertEqual(saved["access_pin_hash"], first_hash)
        self.assertEqual(saved["private_profiles"], 0)
        self.assertEqual(saved["prevent_copying"], 0)

    def test_proposals_and_copies_do_not_consume_personal_quota(self):
        self.conn.execute("UPDATE users SET date_limit=1 WHERE id=1")
        self.user = self.conn.execute("SELECT * FROM users WHERE id=1").fetchone()
        self.conn.executemany(
            "INSERT INTO dates(id,owner_id,name,origin,source_date_id,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                (1, 1, "Личное", "admin", None, STAMP),
                (2, 1, "Предложение", "guest", None, STAMP),
                (3, 1, "Копия", "copy", 1, STAMP),
            ),
        )
        tasks = BackgroundTasks()
        with self.assertRaises(admin_routes.DateQuotaExceeded):
            admin_routes.enforce_date_quota(self.conn, self.user, tasks)
        self.assertEqual(len(tasks.tasks), 1)

    def test_clone_cannot_bypass_personal_quota_and_notifies_admin(self):
        self.conn.execute("UPDATE users SET date_limit=1 WHERE id=1")
        self.conn.execute(
            "INSERT INTO dates(id,owner_id,name,origin,created_at) "
            "VALUES(1,1,'Личное','admin',?)",
            (STAMP,),
        )
        self.user = self.conn.execute("SELECT * FROM users WHERE id=1").fetchone()
        tasks = BackgroundTasks()

        response = admin_routes.date_clone(
            1, request_for(self.user), tasks, conn=self.conn,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM dates").fetchone()[0], 1,
        )
        self.assertEqual(len(tasks.tasks), 1)

    def test_event_views_separate_all_proposals_and_keep_search(self):
        self.conn.executemany(
            "INSERT INTO dates(id,owner_id,name,origin,archived_at,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                (1, 1, "Личный пикник", "admin", None, STAMP),
                (2, 1, "Активное предложение", "guest", None, STAMP),
                (3, 1, "Архивное предложение", "guest", STAMP, STAMP),
            ),
        )

        def render(request, name, context, **kwargs):
            return SimpleNamespace(context=context, headers={})

        with patch.object(admin_routes.templates, "TemplateResponse", side_effect=render):
            active = admin_routes.dates_list(
                request_for(self.user, view="active", q="пикник"), conn=self.conn,
            ).context
            proposed = admin_routes.dates_list(
                request_for(self.user, view="proposed"), conn=self.conn,
            ).context

        self.assertEqual([row["id"] for row in active["rows"]], [1])
        self.assertEqual({row["id"] for row in proposed["rows"]}, {2, 3})
        self.assertIn("q=%D0%BF%D0%B8%D0%BA%D0%BD%D0%B8%D0%BA", active["current_list_url"])
        self.assertEqual(active["proposed_n"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
