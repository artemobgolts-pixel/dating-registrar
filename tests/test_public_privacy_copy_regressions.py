#!/usr/bin/env python3
"""Сквозные регрессии PIN-приватности, копирования и публичных ссылок."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-public-privacy-copy-")
os.environ.update({
    "DATA_DIR": _DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "public-privacy-copy.test",
    "SECRET_KEY": "public-privacy-copy-test-secret",
    "TG_BOT_TOKEN": "",
    "TG_CHAT_ID": "",
    "TG_BOT_USERNAME": "",
    "TG_WEBHOOK_SECRET": "public-privacy-copy-hook",
    "OPERATOR_TG_IDS": "",
})

import admin_routes  # noqa: E402
import category_access  # noqa: E402
import db  # noqa: E402
import public_routes  # noqa: E402
import social_events  # noqa: E402


STAMP = "2030-01-01T10:00:00"


def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    return conn


def request_for(path: str, *, user_id: int | None = None,
                csrf: str = "csrf-test") -> Request:
    session: dict[str, object] = {"csrf": csrf}
    if user_id is not None:
        session["user_id"] = user_id
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"x-requested-with", b"fetch")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "session": session,
        "state": {},
    }
    return Request(scope)


class PublicPrivacyCopyRegressionTests(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.owner_id = self._user(501, "Организатор")
        self.viewer_id = self._user(502, "Участник")

    def tearDown(self):
        self.conn.close()

    def _user(self, telegram_id: int, name: str, *, limit: int = 100) -> int:
        return int(self.conn.execute(
            "INSERT INTO users(telegram_id,display_name,date_limit,created_at) "
            "VALUES(?,?,?,?)",
            (telegram_id, name, limit, STAMP),
        ).lastrowid)

    def _category(self, token: str = "collection-token", **values) -> int:
        fields = {
            "private_profiles": 0,
            "prevent_copying": 0,
            "pin_enabled": 0,
            "access_pin_hash": None,
        }
        fields.update(values)
        return int(self.conn.execute(
            "INSERT INTO categories("
            "owner_id,name,category_skin,link_token,link_enabled,choice_mode,"
            "voting_status,private_profiles,prevent_copying,pin_enabled,"
            "access_pin_hash,created_at"
            ") VALUES(?,?,?,?,1,'multiple','unconfigured',?,?,?,?,?)",
            (
                self.owner_id, "Планы на выходные", "friends", token,
                fields["private_profiles"], fields["prevent_copying"],
                fields["pin_enabled"], fields["access_pin_hash"], STAMP,
            ),
        ).lastrowid)

    def _date(self, owner_id: int, name: str, token: str, *, public: int = 1,
              source_date_id: int | None = None,
              starts_at: str | None = None, ends_at: str | None = None) -> int:
        return int(self.conn.execute(
            "INSERT INTO dates("
            "owner_id,name,share_token,is_public,source_date_id,starts_at,ends_at,"
            "created_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (owner_id, name, token, public, source_date_id,
             starts_at, ends_at, STAMP),
        ).lastrowid)

    def _render_category(self, token: str = "collection-token",
                         *, user_id: int | None = None) -> str:
        response = public_routes.public_category(
            token, request_for(f"/c/{token}", user_id=user_id), conn=self.conn,
        )
        return response.body.decode("utf-8")

    def test_fresh_and_migrated_accounts_effectively_default_to_100(self):
        fresh = self.conn.execute(
            "INSERT INTO users(telegram_id,created_at) VALUES(?,?) RETURNING date_limit",
            (599, STAMP),
        ).fetchone()[0]
        self.assertEqual(fresh, 100)

        old = sqlite3.connect(":memory:")
        old.row_factory = sqlite3.Row
        try:
            old.executescript("""
                PRAGMA foreign_keys=OFF;
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE, tg_username TEXT,
                    display_name TEXT, avatar_path TEXT, birth_date TEXT,
                    gender TEXT, is_active INTEGER NOT NULL DEFAULT 1,
                    is_operator INTEGER NOT NULL DEFAULT 0,
                    is_reviewed INTEGER NOT NULL DEFAULT 1,
                    date_limit INTEGER NOT NULL DEFAULT 30,
                    bot_linked INTEGER NOT NULL DEFAULT 0,
                    cursor_effects INTEGER NOT NULL DEFAULT 0,
                    admin_skin TEXT NOT NULL DEFAULT 'friends',
                    created_at TEXT NOT NULL, last_login_at TEXT
                );
                CREATE TABLE categories (
                    id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    voting_status TEXT NOT NULL DEFAULT 'unconfigured',
                    winner_date_id INTEGER
                );
                CREATE TABLE dates (
                    id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL
                );
                CREATE TABLE date_wants (
                    user_id INTEGER NOT NULL, date_id INTEGER NOT NULL,
                    is_public INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id,date_id)
                );
                CREATE TABLE bookings (
                    id INTEGER PRIMARY KEY, date_id INTEGER NOT NULL,
                    category_id INTEGER NOT NULL, user_id INTEGER,
                    participation_withdrawn_at TEXT
                );
                CREATE TABLE notification_outbox (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL, event_key TEXT NOT NULL UNIQUE,
                    sent_at TEXT, cancelled_at TEXT, claimed_at TEXT,
                    last_error TEXT, updated_at TEXT NOT NULL
                );
                INSERT INTO users(id,telegram_id,display_name,date_limit,created_at)
                    VALUES(1,7001,'Старый стандарт',30,'2030-01-01'),
                          (2,7002,'Ручной лимит',17,'2030-01-01');
                INSERT INTO categories(id,owner_id,name)
                    VALUES(1,1,'Сохранённая подборка');
                INSERT INTO dates VALUES(1,1,'Сохранённое событие');
                INSERT INTO date_wants VALUES(2,1,0,'2030-01-01','2030-01-01');
                INSERT INTO notification_outbox(
                    id,user_id,kind,event_key,updated_at
                ) VALUES(
                    1,2,'review_prompt','review:date:1:user:2:prompt','2030-01-01'
                );
            """)
            old.executescript(db.MIGRATIONS[35])

            limits = dict(old.execute(
                "SELECT display_name,date_limit FROM users ORDER BY id",
            ).fetchall())
            self.assertEqual(limits, {"Старый стандарт": 100, "Ручной лимит": 17})
            old.execute(
                "INSERT INTO users(telegram_id,created_at) VALUES(7003,'2030-01-01')"
            )
            # SQLite RETURNING отдаёт значения до AFTER INSERT-триггеров;
            # проверяем сохранённую эффективную квоту отдельным чтением.
            self.assertEqual(old.execute(
                "SELECT date_limit FROM users WHERE telegram_id=7003",
            ).fetchone()[0], 100)
            self.assertEqual(old.execute("SELECT COUNT(*) FROM categories").fetchone()[0], 1)
            self.assertEqual(old.execute("SELECT COUNT(*) FROM dates").fetchone()[0], 1)
            self.assertEqual(old.execute("SELECT is_public FROM date_wants").fetchone()[0], 1)
            self.assertIsNotNone(old.execute(
                "SELECT cancelled_at FROM notification_outbox WHERE id=1",
            ).fetchone()[0])
        finally:
            old.close()

    def test_pin_blocks_content_until_valid_session_grant_and_rotates(self):
        first_hash = category_access.hash_pin("0427")
        category_id = self._category(pin_enabled=1, access_pin_hash=first_hash)
        event_id = self._date(self.owner_id, "Секретная прогулка", "secret-date")
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            (event_id, category_id),
        )
        self.conn.commit()

        anonymous = request_for("/c/collection-token")
        locked = public_routes.public_category(
            "collection-token", anonymous, conn=self.conn,
        )
        locked_html = locked.body.decode("utf-8")
        self.assertEqual(locked.status_code, 200)
        self.assertIn("Закрытая подборка", locked_html)
        self.assertNotIn("Секретная прогулка", locked_html)

        with patch.object(public_routes, "guest_throttle", lambda *args, **kwargs: None):
            wrong = public_routes.public_category_unlock(
                "collection-token", anonymous, "1111", conn=self.conn,
            )
            self.assertEqual(wrong.status_code, 403)
            opened = public_routes.public_category_unlock(
                "collection-token", anonymous, "0427", conn=self.conn,
            )
        self.assertEqual(opened.status_code, 303)
        self.assertIn("Секретная прогулка", self._render_category_with_request(
            "collection-token", anonymous,
        ))

        # Grant привязан к хешу: смена PIN закрывает уже открытую сессию.
        self.conn.execute(
            "UPDATE categories SET access_pin_hash=? WHERE id=?",
            (category_access.hash_pin("9000"), category_id),
        )
        rotated = public_routes.public_category(
            "collection-token", anonymous, conn=self.conn,
        ).body.decode("utf-8")
        self.assertIn("Закрытая подборка", rotated)
        self.assertNotIn("Секретная прогулка", rotated)

        # Unicode-цифры не проходят протокол четырёх ASCII-цифр.
        self.assertFalse(category_access.validate_pin("１２３４"))
        self.assertFalse(category_access.verify_pin("１２３４", first_hash))

    def _render_category_with_request(self, token: str, request: Request) -> str:
        return public_routes.public_category(
            token, request, conn=self.conn,
        ).body.decode("utf-8")

    def test_rendered_privacy_flags_control_profile_links_and_copy_actions(self):
        category_id = self._category()
        event_id = self._date(self.owner_id, "Кино", "movie-date")
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            (event_id, category_id),
        )
        self.conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
            "VALUES(?,?,?,?,?)",
            (event_id, category_id, f"u{self.viewer_id}", self.viewer_id, STAMP),
        )
        self.conn.commit()

        public_html = self._render_category()
        self.assertIn(f'href="/u/{self.viewer_id}?skin=friends"', public_html)
        self.assertIn("data-copy-link=\"/d/movie-date\"", public_html)
        self.assertIn("data-copy-event-form", public_html)

        self.conn.execute(
            "UPDATE categories SET private_profiles=1,prevent_copying=1 WHERE id=?",
            (category_id,),
        )
        private_html = self._render_category()
        self.assertIn("Участник", private_html)
        self.assertNotIn(f'href="/u/{self.viewer_id}?skin=friends"', private_html)
        self.assertNotIn("data-copy-link=\"/d/movie-date\"", private_html)
        self.assertNotIn("data-copy-event-form", private_html)

    @patch.object(public_routes, "guest_throttle", lambda *args, **kwargs: None)
    def test_event_copy_is_idempotent_bypasses_personal_quota_and_leaves_feed(self):
        self.conn.execute(
            "UPDATE users SET date_limit=0 WHERE id=?", (self.viewer_id,),
        )
        source_id = self._date(self.owner_id, "Уже добавлено", "source-date")
        untouched_id = self._date(self.owner_id, "Ещё не добавлено", "untouched-date")
        request = request_for("/d/source-date/add", user_id=self.viewer_id)

        first = public_routes.shared_date_add(
            "source-date", request, csrf="csrf-test",
            source_category_token="", conn=self.conn,
        )
        second = public_routes.shared_date_add(
            "source-date", request, csrf="csrf-test",
            source_category_token="", conn=self.conn,
        )
        first_payload = json.loads(first.body)
        second_payload = json.loads(second.body)
        self.assertEqual(first_payload["edit_url"], second_payload["edit_url"])
        self.assertTrue(second_payload["already_added"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM dates WHERE owner_id=? AND source_date_id=?",
            (self.viewer_id, source_id),
        ).fetchone()[0], 1)

        cards, _ = admin_routes._community_cards(self.conn, self.viewer_id, None)
        visible_ids = {int(card["id"]) for card in cards}
        self.assertNotIn(source_id, visible_ids)
        self.assertIn(untouched_id, visible_ids)

        copied_id = int(self.conn.execute(
            "SELECT id FROM dates WHERE owner_id=? AND source_date_id=?",
            (self.viewer_id, source_id),
        ).fetchone()[0])
        self.conn.execute("DELETE FROM dates WHERE id=?", (source_id,))
        copied = self.conn.execute(
            "SELECT origin,source_date_id FROM dates WHERE id=?", (copied_id,),
        ).fetchone()
        self.assertEqual((copied["origin"], copied["source_date_id"]),
                         ("copy", source_id))
        self.assertEqual(
            public_routes.personal_date_quota_used(self.conn, self.viewer_id), 0,
        )

    @patch.object(public_routes, "guest_throttle", lambda *args, **kwargs: None)
    def test_collection_copy_ban_is_enforced_on_the_server(self):
        category_id = self._category(prevent_copying=1)
        event_id = self._date(self.owner_id, "Только посмотреть", "view-only-date")
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            (event_id, category_id),
        )
        self.conn.commit()
        request = request_for("/d/view-only-date/add", user_id=self.viewer_id)

        with self.assertRaises(HTTPException) as raised:
            public_routes.shared_date_add(
                "view-only-date", request, csrf="csrf-test",
                source_category_token="collection-token", conn=self.conn,
            )
        self.assertEqual(raised.exception.status_code, 403)
        with self.assertRaises(HTTPException) as stripped:
            public_routes.shared_date_add(
                "view-only-date", request, csrf="csrf-test",
                source_category_token="", conn=self.conn,
            )
        self.assertEqual(stripped.exception.status_code, 403)
        page = public_routes.shared_date(
            "view-only-date",
            request_for("/d/view-only-date", user_id=self.viewer_id),
            conn=self.conn,
        ).body.decode("utf-8")
        self.assertNotIn('action="/d/view-only-date/add"', page)
        self.assertIn("Организатор отключил копирование", page)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM dates WHERE owner_id=? AND source_date_id=?",
            (self.viewer_id, event_id),
        ).fetchone()[0], 0)

    def test_direct_event_html_discloses_no_collection_or_voting_state(self):
        category_id = self._category(token="voting-collection")
        event_id = self._date(self.owner_id, "Прямая ссылка", "direct-date")
        self.conn.execute(
            "UPDATE categories SET voting_status='open',"
            "voting_deadline='2035-01-01T10:00:00' WHERE id=?",
            (category_id,),
        )
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            (event_id, category_id),
        )
        self.conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
            "VALUES(?,?,?,?,?)",
            (event_id, category_id, f"u{self.viewer_id}", self.viewer_id, STAMP),
        )
        self.conn.commit()

        response = public_routes.shared_date(
            "direct-date", request_for("/d/direct-date"), conn=self.conn,
        )
        html = response.body.decode("utf-8")
        self.assertIn("Прямая ссылка", html)
        self.assertNotIn("voting-collection", html)
        self.assertNotIn("vote-progress", html)
        self.assertNotIn("До конца голосования", html)
        self.assertNotIn(f"/u/{self.viewer_id}?skin=", html)
        route_paths = {route.path for route in public_routes.router.routes}
        self.assertNotIn("/d/{token}/book", route_paths)
        self.assertNotIn("/d/{token}/withdraw", route_paths)
        self.assertNotIn("/d/{token}/participant-avatar/{user_id}", route_paths)

    def test_want_grants_manual_review_without_any_telegram_prompt(self):
        event_id = self._date(
            self.owner_id, "Прошедший план", "past-plan",
            starts_at="2030-01-02T18:00:00",
            ends_at="2030-01-02T20:00:00",
        )
        self.conn.execute(
            "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
            "VALUES(?,?,?,?)",
            (self.viewer_id, event_id, STAMP, STAMP),
        )
        self.conn.commit()

        self.assertIsNone(social_events.queue_review_prompt(
            self.conn, event_id, self.viewer_id,
        ))
        self.assertEqual(social_events.queue_review_prompts_for_date(
            self.conn, event_id,
        ), 0)
        notifications, waiting = social_events.queue_archive_review_fanout(
            self.conn, event_id, now=datetime(2030, 1, 2, 20, 0, 1),
        )
        self.assertEqual(notifications, 0)
        self.assertEqual(waiting, 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE kind='review_prompt'",
        ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
