#!/usr/bin/env python3
"""CSRF-регрессии PIN-входа и выхода из Telegram/WebView."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-pin-webview-")
os.environ.update({
    "DATA_DIR": _DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "pin-webview.test",
    "SECRET_KEY": "pin-webview-regression-secret",
    "TG_BOT_TOKEN": "",
    "TG_BOT_USERNAME": "date4you_pin_test_bot",
    "TG_WEBHOOK_SECRET": "pin-webview-hook-secret",
    "OPERATOR_TG_IDS": "",
})

import category_access  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402


WEBHOOK_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "pin-webview-hook-secret"}
WEBVIEW_HEADERS = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}
STAMP = "2030-01-01T10:00:00"


def login(client: TestClient, telegram_id: int) -> str:
    code = client.post("/auth/start").json()["code"]
    person = {"id": telegram_id, "username": f"pin_{telegram_id}", "first_name": "Тест"}
    assert client.post(
        "/tg/webhook", headers=WEBHOOK_HEADERS,
        json={"message": {"text": f"/start {code}", "from": person}},
    ).status_code == 200
    assert client.post(
        "/tg/webhook", headers=WEBHOOK_HEADERS,
        json={"callback_query": {
            "id": f"confirm-{telegram_id}", "data": f"auth_confirm:{code}", "from": person,
        }},
    ).status_code == 200
    assert client.get(f"/auth/poll?code={code}").json()["status"] == "ok"
    page = client.get("/admin/categories")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


class GuestPinWebviewCsrfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        conn = db.connect()
        try:
            owner_id = int(conn.execute(
                "INSERT INTO users(telegram_id,display_name,created_at) VALUES(?,?,?)",
                (998101, "Организатор", STAMP),
            ).lastrowid)
            conn.execute(
                "INSERT INTO categories("
                "owner_id,name,category_skin,link_token,link_enabled,pin_enabled,"
                "access_pin_hash,created_at"
                ") VALUES(?,?,?,?,1,1,?,?)",
                (owner_id, "Закрытые планы", "friends", "pin-webview-category",
                 category_access.hash_pin("0427"), STAMP),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def pin_form(client: TestClient) -> tuple[str, str]:
        page = client.get("/c/pin-webview-category")
        match = re.search(r'name="csrf" value="([^"]*)"', page.text)
        assert match is not None
        return match.group(1), page.text

    def test_fresh_pin_page_mints_a_nonempty_session_token(self):
        with TestClient(main.app, follow_redirects=False) as client:
            csrf, html = self.pin_form(client)

        self.assertTrue(csrf)
        self.assertIn("Закрытая подборка", html)

    def test_valid_pin_and_csrf_work_with_webview_origin_metadata(self):
        metadata_cases = (
            WEBVIEW_HEADERS,
            {"Sec-Fetch-Site": "cross-site"},
        )
        for headers in metadata_cases:
            with self.subTest(headers=headers):
                with TestClient(main.app, follow_redirects=False) as client:
                    csrf, _ = self.pin_form(client)
                    response = client.post(
                        "/c/pin-webview-category/unlock",
                        data={"pin": "0427", "csrf": csrf},
                        headers=headers,
                    )

                self.assertEqual(response.status_code, 303, response.text)
                self.assertEqual(response.headers["location"], "/c/pin-webview-category")

    def test_foreign_origin_and_missing_token_remain_rejected(self):
        with TestClient(main.app, follow_redirects=False) as foreign_client:
            csrf, _ = self.pin_form(foreign_client)
            foreign = foreign_client.post(
                "/c/pin-webview-category/unlock",
                data={"pin": "0427", "csrf": csrf},
                headers={"Origin": "https://evil.example"},
            )
        with TestClient(main.app, follow_redirects=False) as missing_client:
            self.pin_form(missing_client)
            missing = missing_client.post(
                "/c/pin-webview-category/unlock",
                data={"pin": "0427"}, headers=WEBVIEW_HEADERS,
            )

        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(missing.status_code, 403)

    def test_logout_from_guest_surface_works_in_opaque_webview(self):
        with TestClient(main.app, follow_redirects=False) as client:
            csrf = login(client, 998102)
            response = client.post(
                "/admin/logout", data={"csrf": csrf}, headers=WEBVIEW_HEADERS,
            )

        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(response.headers["location"], "/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
