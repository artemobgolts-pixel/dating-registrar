#!/usr/bin/env python3
"""CSRF regression coverage for operator writes from Telegram Web embeds."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-operator-csrf-")
os.environ.update({
    "DATA_DIR": _DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "operator-csrf.test",
    "SECRET_KEY": "operator-csrf-regression-secret",
    "TG_BOT_TOKEN": "",
    "TG_BOT_USERNAME": "date4you_operator_csrf_test_bot",
    "TG_WEBHOOK_SECRET": "operator-csrf-hook-secret",
    "OPERATOR_TG_IDS": "998001",
})

import db  # noqa: E402
import main  # noqa: E402


WEBHOOK_HEADERS = {
    "X-Telegram-Bot-Api-Secret-Token": "operator-csrf-hook-secret",
}
PUBLIC_ORIGIN = "https://operator-csrf.test"
REJECTION = "Запрос с другого сайта отклонён"
STAMP = "2030-01-01T10:00:00"


def login_operator(client: TestClient) -> str:
    code = client.post("/auth/start").json()["code"]
    person = {
        "id": 998001,
        "username": "operator_csrf",
        "first_name": "Админ",
    }
    assert client.post(
        "/tg/webhook",
        headers=WEBHOOK_HEADERS,
        json={"message": {"text": f"/start {code}", "from": person}},
    ).status_code == 200
    assert client.post(
        "/tg/webhook",
        headers=WEBHOOK_HEADERS,
        json={"callback_query": {
            "id": "confirm-operator-csrf",
            "data": f"auth_confirm:{code}",
            "from": person,
        }},
    ).status_code == 200
    assert client.get(f"/auth/poll?code={code}").json()["status"] == "ok"
    page = client.get("/operator/categories")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


class OperatorCsrfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, follow_redirects=False).__enter__()
        cls.csrf = login_operator(cls.client)

        conn = db.connect()
        try:
            cls.oauth_user_id = int(conn.execute(
                "INSERT INTO users(telegram_id,display_name,created_at) "
                "VALUES(NULL,?,?)",
                ("Пользователь Google", STAMP),
            ).lastrowid)
            conn.execute(
                "INSERT INTO oauth_accounts(provider,provider_uid,user_id,email,created_at) "
                "VALUES('google','operator-csrf-google',?,?,?)",
                (cls.oauth_user_id, "google@example.test", STAMP),
            )
            cls.category_id = int(conn.execute(
                "INSERT INTO categories("
                "owner_id,name,link_token,link_enabled,choice_mode,voting_deadline,"
                "voting_status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (cls.oauth_user_id, "Чужая подборка", "operator-csrf-category", 1,
                 "single", "2031-01-01T10:00", "open", STAMP),
            ).lastrowid)
            cls.date_id = int(conn.execute(
                "INSERT INTO dates(owner_id,name,starts_at,origin,created_at) "
                "VALUES(?,?,?,'admin',?)",
                (cls.oauth_user_id, "Событие", "2031-01-02T10:00", STAMP),
            ).lastrowid)
            conn.execute(
                "INSERT INTO date_categories(date_id,category_id,position) "
                "VALUES(?,?,0)",
                (cls.date_id, cls.category_id),
            )
            cls.booking_id = int(conn.execute(
                "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
                "VALUES(?,?,?,?,?)",
                (cls.date_id, cls.category_id, f"u{cls.oauth_user_id}",
                 cls.oauth_user_id, STAMP),
            ).lastrowid)
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        _DATA.cleanup()

    def embedded_post(self, path: str, referer: str):
        return self.client.post(
            path,
            data={"csrf": self.csrf},
            headers={
                "Origin": PUBLIC_ORIGIN,
                # Telegram Web can keep the same-origin app inside its
                # cross-site frame; the signed form token remains valid.
                "Sec-Fetch-Site": "cross-site",
                "Referer": PUBLIC_ORIGIN + referer,
            },
        )

    def assert_not_cross_site_rejection(self, response) -> None:
        self.assertEqual(response.status_code, 303, response.text)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        message = query.get("msg", [""])[0]
        self.assertNotIn(
            REJECTION,
            message,
            f"валидные Origin и CSRF были отклонены: {message}",
        )

    def assert_cross_site_rejection(self, response) -> None:
        self.assertEqual(response.status_code, 303, response.text)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertIn(REJECTION, query.get("msg", [""])[0])

    def test_block_oauth_user_from_embedded_operator_page(self):
        response = self.embedded_post(
            f"/operator/users/{self.oauth_user_id}/ban",
            f"/operator/users/{self.oauth_user_id}",
        )
        self.assert_not_cross_site_rejection(response)
        conn = db.connect()
        try:
            self.assertEqual(conn.execute(
                "SELECT is_active FROM users WHERE id=?", (self.oauth_user_id,),
            ).fetchone()[0], 0)
        finally:
            conn.close()

    def test_cross_site_guards_remain_enforced(self):
        path = f"/operator/users/{self.oauth_user_id}/ban"
        referer = PUBLIC_ORIGIN + f"/operator/users/{self.oauth_user_id}"
        conn = db.connect()
        try:
            active_before = conn.execute(
                "SELECT is_active FROM users WHERE id=?", (self.oauth_user_id,),
            ).fetchone()[0]
        finally:
            conn.close()

        foreign_origin = self.client.post(
            path,
            data={"csrf": self.csrf},
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "Referer": referer,
            },
        )
        self.assert_cross_site_rejection(foreign_origin)

        missing_origin = self.client.post(
            path,
            data={"csrf": self.csrf},
            headers={"Sec-Fetch-Site": "cross-site", "Referer": referer},
        )
        self.assert_cross_site_rejection(missing_origin)

        conn = db.connect()
        try:
            # Neither rejected request may reach the toggle handler.
            self.assertEqual(conn.execute(
                "SELECT is_active FROM users WHERE id=?", (self.oauth_user_id,),
            ).fetchone()[0], active_before)
        finally:
            conn.close()

    def test_disable_foreign_category_link_from_embedded_operator_page(self):
        response = self.embedded_post(
            f"/operator/categories/{self.category_id}/toggle",
            "/operator/categories",
        )
        self.assert_not_cross_site_rejection(response)
        conn = db.connect()
        try:
            self.assertEqual(conn.execute(
                "SELECT link_enabled FROM categories WHERE id=?", (self.category_id,),
            ).fetchone()[0], 0)
        finally:
            conn.close()

    def test_remove_vote_from_embedded_operator_page(self):
        response = self.embedded_post(
            f"/operator/bookings/{self.booking_id}/delete",
            "/operator/bookings",
        )
        self.assert_not_cross_site_rejection(response)
        conn = db.connect()
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM bookings WHERE id=?", (self.booking_id,),
            ).fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
