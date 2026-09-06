#!/usr/bin/env python3
"""Regression coverage for operator user cards."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-operator-card-")
os.environ.update({
    "DATA_DIR": _IMPORT_DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "operator-card.test",
    "SECRET_KEY": "operator-card-test-secret",
    "TG_BOT_TOKEN": "",
    "TG_CHAT_ID": "",
    "TG_BOT_USERNAME": "date4you_operator_card_bot",
    "TG_WEBHOOK_SECRET": "operator-card-hook-secret",
    "OPERATOR_TG_IDS": "",
})

import db  # noqa: E402
import operator_routes  # noqa: E402


STAMP = "2030-01-01T00:00:00"


class OperatorUserCardTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        self.operator_id = self._user(950001, "Администратор", is_operator=1)

        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="operator-card-test-secret")
        app.include_router(operator_routes.router)

        def test_db():
            yield self.conn

        async def test_operator(request: Request):
            operator = self.conn.execute(
                "SELECT * FROM users WHERE id=?", (self.operator_id,),
            ).fetchone()
            request.state.user = operator
            request.session["csrf"] = "operator-card-csrf"
            return operator

        app.dependency_overrides[operator_routes.get_db] = test_db
        app.dependency_overrides[operator_routes.current_operator] = test_operator
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.conn.close()

    def _user(self, telegram_id: int | None, name: str, *,
              is_operator: int = 0) -> int:
        return int(self.conn.execute(
            "INSERT INTO users(telegram_id,display_name,is_operator,created_at) "
            "VALUES(?,?,?,?)",
            (telegram_id, name, is_operator, STAMP),
        ).lastrowid)

    def _add_content(self, owner_id: int) -> tuple[int, int]:
        category_id = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,created_at) "
            "VALUES(?,?,?,?)",
            (owner_id, f"Подборка {owner_id}", f"operator-card-{owner_id}", STAMP),
        ).lastrowid)
        date_id = int(self.conn.execute(
            "INSERT INTO dates(owner_id,name,origin,created_at) VALUES(?,?,?,?)",
            (owner_id, f"Событие {owner_id}", "owner", STAMP),
        ).lastrowid)
        self.conn.commit()
        return category_id, date_id

    def _assert_card_renders(self, user_id: int) -> str:
        category_id, date_id = self._add_content(user_id)
        response = self.client.get(f"/operator/users/{user_id}")
        self.assertEqual(response.status_code, 200)
        html = response.text
        return_to = f"/operator/users/{user_id}"
        self.assertIn(
            f'/admin/categories/{category_id}?return_to={return_to}', html,
        )
        self.assertIn(
            f'/admin/dates/{date_id}/edit?return_to={return_to}', html,
        )
        return html

    def test_oauth_only_user_with_content_renders(self):
        oauth_id = self._user(None, "Пользователь Google")
        self.conn.execute(
            "INSERT INTO oauth_accounts(provider,provider_uid,user_id,email,created_at) "
            "VALUES(?,?,?,?,?)",
            ("google", "google-950002", oauth_id, "oauth@example.test", STAMP),
        )
        self.conn.commit()

        html = self._assert_card_renders(oauth_id)

        self.assertIn("OAuth-аккаунт", html)
        self.assertNotIn("Telegram ID None", html)

    def test_operator_can_open_own_card_with_content(self):
        html = self._assert_card_renders(self.operator_id)

        self.assertIn("это вы", html)

    def test_card_shows_votes_cast_by_target_user(self):
        voter_id = self._user(950010, "Голосующий")
        host_id = self._user(950011, "Организатор")
        host_category, host_date = self._add_content(host_id)
        own_category, own_date = self._add_content(voter_id)
        self.conn.executemany(
            "INSERT INTO date_categories(date_id,category_id,position) VALUES(?,?,0)",
            ((host_date, host_category), (own_date, own_category)),
        )
        self.conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,"
            "participation_withdrawn_at,created_at) VALUES(?,?,?,?,?,?)",
            (host_date, host_category, f"u{voter_id}", voter_id, STAMP, STAMP),
        )
        # Чужой голос за событие пользователя не является его собственным голосом.
        self.conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
            "VALUES(?,?,?,?,?)",
            (own_date, own_category, f"u{host_id}", host_id, STAMP),
        )
        self.conn.commit()

        response = self.client.get(f"/operator/users/{voter_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Голоса пользователя", response.text)
        self.assertIn(f"Событие {host_id}", response.text)
        self.assertIn(f"Подборка {host_id}", response.text)
        self.assertIn("отказался от участия", response.text)
        self.assertIn(f"/operator/bookings?voter_id={voter_id}", response.text)
        self.assertNotIn(f"Событие {voter_id}</a>", response.text)

    def test_card_paginates_events_and_keeps_total_count(self):
        user_id = self._user(950020, "Много событий")
        for number in range(12):
            self.conn.execute(
                "INSERT INTO dates(owner_id,name,origin,created_at) VALUES(?,?,?,?)",
                (user_id, f"Постраничное событие {number:02d}", "owner", STAMP),
            )
        self.conn.commit()

        first = self.client.get(f"/operator/users/{user_id}")
        second = self.client.get(f"/operator/users/{user_id}?events_page=2")
        clamped = self.client.get(f"/operator/users/{user_id}?events_page=999")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn("Постраничное событие 11", first.text)
        self.assertNotIn("Постраничное событие 00", first.text)
        self.assertIn("Постраничное событие 00", second.text)
        self.assertNotIn("Постраничное событие 11", second.text)
        self.assertIn("страница 2 из 2", second.text)
        self.assertIn("Постраничное событие 00", clamped.text)
        self.assertIn("<strong>12</strong><span>событий</span>", second.text)

    def test_oauth_only_user_in_list_has_provider_label(self):
        oauth_id = self._user(None, "Пользователь Google")
        self.conn.execute(
            "INSERT INTO oauth_accounts(provider,provider_uid,user_id,email,created_at) "
            "VALUES(?,?,?,?,?)",
            ("google", "google-950003", oauth_id, "list@example.test", STAMP),
        )
        self.conn.commit()

        response = self.client.get("/operator/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn("OAuth-аккаунт", response.text)
        self.assertNotIn("Telegram ID None", response.text)

        self.conn.execute("UPDATE users SET is_reviewed=0 WHERE id=?", (oauth_id,))
        self.conn.commit()
        for path in ("/operator/", "/operator/review"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertNotIn("Telegram ID None", page.text, path)

    def test_operator_lists_filter_risk_and_moderation_state(self):
        suspicious_id = self._user(950030, "Рискованный 100% пользователь")
        regular_id = self._user(950031, "Обычный пользователь")
        self.conn.execute(
            "UPDATE users SET is_suspicious=1 WHERE id=?", (suspicious_id,),
        )
        pending_category = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,is_reviewed,"
            "operator_review_pending,created_at) VALUES(?,?,?,?,1,?)",
            (suspicious_id, "Скрытая 100% подборка", "filtered-pending", 0, STAMP),
        ).lastrowid)
        self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,created_at) "
            "VALUES(?,?,?,?)",
            (regular_id, "Обычная подборка", "filtered-regular", STAMP),
        )
        self.conn.execute(
            "INSERT INTO dates(owner_id,name,origin,operator_review_pending,created_at) "
            "VALUES(?,?,?,1,?)",
            (suspicious_id, "Скрытое событие", "owner", STAMP),
        )
        self.conn.execute(
            "INSERT INTO dates(owner_id,name,origin,created_at) VALUES(?,?,?,?)",
            (regular_id, "Обычное событие", "owner", STAMP),
        )
        self.conn.commit()

        users = self.client.get("/operator/users?risk=suspicious&q=100%25")
        self.assertEqual(users.status_code, 200)
        self.assertIn("Рискованный 100% пользователь", users.text)
        self.assertNotIn("Обычный пользователь", users.text)

        categories = self.client.get("/operator/categories?review=operator")
        self.assertEqual(categories.status_code, 200)
        self.assertIn("Скрытая 100% подборка", categories.text)
        self.assertNotIn("Обычная подборка", categories.text)
        self.assertIn(f"/admin/categories/{pending_category}", categories.text)

        dates = self.client.get("/operator/dates?flt=review")
        self.assertEqual(dates.status_code, 200)
        self.assertIn("Скрытое событие", dates.text)
        self.assertNotIn("Обычное событие", dates.text)


if __name__ == "__main__":
    unittest.main()
