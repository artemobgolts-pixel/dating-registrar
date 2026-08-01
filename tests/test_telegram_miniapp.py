#!/usr/bin/env python3
"""Безопасность Telegram Mini App, /start-регистрация и Bot API rich messages."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-miniapp-")
os.environ["DATA_DIR"] = _DATA.name
os.environ["SECRET_KEY"] = "miniapp-test-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TG_BOT_TOKEN"] = "123456:miniapp-test-token"
os.environ["TG_BOT_USERNAME"] = "date4you_test_bot"
os.environ["TG_WEBHOOK_SECRET"] = "miniapp-webhook-secret"
os.environ["TG_MINI_APP_URL"] = "https://localhost/tg/app"

import auth_routes  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402
import notify  # noqa: E402


def signed_init_data(user: dict, *, auth_date: int | None = None,
                     token: str | None = None) -> str:
    values = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAE-test-query",
        "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", (token or notify.TOKEN).encode(),
                      hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class TelegramMiniAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = TestClient(main.app, follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        _DATA.cleanup()

    def setUp(self):
        main._rates.clear()
        conn = db.connect()
        conn.execute("DELETE FROM users")
        conn.commit()
        conn.close()
        self.client.cookies.clear()

    def post_raw(self, raw: str, *, next_path="", origin="https://localhost"):
        boot = self.client.get("/tg/app")
        nonce = re.search(r'data-miniapp-nonce="([^"]+)"', boot.text).group(1)
        return self.client.post(
            "/auth/miniapp",
            headers={"Origin": origin},
            json={"init_data": raw, "nonce": nonce, "next": next_path},
        )

    def post_auth(self, user: dict, *, next_path="", auth_date=None):
        return self.post_raw(
            signed_init_data(user, auth_date=auth_date), next_path=next_path,
        )

    def test_boot_page_allows_only_telegram_framing(self):
        self.assertEqual(main.session_same_site(True), "none")
        self.assertEqual(main.session_same_site(False), "lax")
        async def set_session(request: Request):
            request.session["user_id"] = 1
            return JSONResponse({"ok": True})
        cookie_app = Starlette(routes=[Route("/", set_session)])
        cookie_app.add_middleware(
            SessionMiddleware, secret_key="cookie-test", session_cookie="__Host-admin_s",
            same_site=main.session_same_site(True), https_only=True,
        )
        with TestClient(cookie_app, base_url="https://testserver") as cookie_client:
            set_cookie = cookie_client.get("/").headers["set-cookie"].lower()
        self.assertIn("samesite=none", set_cookie)
        self.assertIn("secure", set_cookie)
        response = self.client.get("/tg/app")
        self.assertEqual(response.status_code, 200)
        self.assertIn("telegram-web-app.js?63", response.text)
        self.assertNotIn("x-frame-options", response.headers)
        csp = response.headers["content-security-policy"]
        self.assertIn("frame-ancestors https://telegram.org https://*.telegram.org", csp)

    def test_signed_init_data_creates_session_without_false_bot_link(self):
        response = self.post_auth({
            "id": 880001, "first_name": "Аня", "username": "anya",
        }, next_path="/admin/questions")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["redirect"], "/admin/questions")
        self.assertFalse(response.json()["notifications_enabled"])
        conn = db.connect()
        row = conn.execute(
            "SELECT bot_linked, display_name FROM users WHERE telegram_id=880001"
        ).fetchone()
        conn.close()
        self.assertEqual((row["bot_linked"], row["display_name"]), (0, "Аня"))
        dashboard = self.client.get("/admin/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("telegram-miniapp.js", dashboard.text)
        self.assertNotIn("x-frame-options", dashboard.headers)

    def test_allows_write_to_pm_is_signed_and_enables_notifications(self):
        response = self.post_auth({
            "id": 880002, "first_name": "Борис", "allows_write_to_pm": True,
        })
        self.assertTrue(response.json()["notifications_enabled"])
        conn = db.connect()
        self.assertEqual(conn.execute(
            "SELECT bot_linked FROM users WHERE telegram_id=880002"
        ).fetchone()[0], 1)
        conn.close()

    def test_forged_stale_and_external_redirect_are_rejected(self):
        raw = signed_init_data({"id": 880003, "first_name": "Ира"})
        forged = raw.replace("880003", "880004")
        self.assertEqual(self.post_raw(forged).status_code, 403)
        stale = self.post_auth(
            {"id": 880003, "first_name": "Ира"},
            auth_date=int(time.time()) - auth_routes.MINIAPP_AUTH_TTL_SECONDS - 1,
        )
        self.assertEqual(stale.status_code, 403)
        safe = self.post_auth(
            {"id": 880003, "first_name": "Ира"},
            next_path="https://evil.example/steal",
        )
        self.assertEqual(safe.json()["redirect"], "/admin/")
        profile = self.post_auth(
            {"id": 880005, "first_name": "Маша"},
            next_path="/u/880005?tab=reviews#review-7",
        )
        self.assertEqual(
            profile.json()["redirect"], "/u/880005?tab=reviews#review-7"
        )

    def test_login_csrf_requires_boot_nonce_json_and_same_origin(self):
        raw = signed_init_data({"id": 880006, "first_name": "Лиза"})
        no_boot = self.client.post(
            "/auth/miniapp", headers={"Origin": "https://localhost"},
            json={"init_data": raw, "nonce": "attacker"},
        )
        self.assertEqual(no_boot.status_code, 403)
        wrong_origin = self.post_raw(raw, origin="https://evil.example")
        self.assertEqual(wrong_origin.status_code, 403)
        text_plain = self.client.post(
            "/auth/miniapp",
            headers={"Origin": "https://localhost", "Content-Type": "text/plain"},
            content=json.dumps({"init_data": raw, "nonce": "attacker"}),
        )
        self.assertEqual(text_plain.status_code, 415)

    def test_plain_start_registers_only_from_private_chat_and_returns_web_app(self):
        sent = []
        original = notify.send_video_to
        notify.send_video_to = lambda chat, path, caption, **kwargs: sent.append(
            (chat, Path(path), caption, kwargs)) or True
        try:
            group = self.client.post(
                "/tg/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "miniapp-webhook-secret"},
                json={"message": {
                    "text": "/start", "from": {"id": 880010},
                    "chat": {"id": -100123, "type": "supergroup"},
                }},
            )
            self.assertEqual(group.status_code, 200)
            private = self.client.post(
                "/tg/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "miniapp-webhook-secret"},
                json={"message": {
                    "text": "/start", "from": {
                        "id": 880011, "first_name": "Новый", "username": "new_user",
                    },
                    "chat": {"id": 880011, "type": "private"},
                }},
            )
        finally:
            notify.send_video_to = original
        self.assertEqual(private.status_code, 200)
        conn = db.connect()
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM users WHERE telegram_id=880010"
        ).fetchone())
        row = conn.execute(
            "SELECT bot_linked, display_name FROM users WHERE telegram_id=880011"
        ).fetchone()
        conn.close()
        self.assertEqual((row["bot_linked"], row["display_name"]), (1, "Новый"))
        self.assertEqual(sent[0][1], auth_routes.START_VIDEO_PATH)
        self.assertTrue(sent[0][1].is_file())
        self.assertIn("Добро пожаловать", sent[0][2])
        button = sent[0][3]["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["style"], "primary")
        self.assertEqual(button["web_app"]["url"], "https://localhost/tg/app")
        browser = sent[0][3]["reply_markup"]["inline_keyboard"][1][0]
        self.assertEqual(browser["text"], "Открыть в браузере")
        self.assertEqual(browser["url"], "https://localhost/")
        self.assertNotIn("web_app", browser)

    def test_expired_start_payload_does_not_create_separate_account(self):
        sent = []
        original = notify.send_to
        notify.send_to = lambda chat, text, **kwargs: sent.append(text) or True
        try:
            response = self.client.post(
                "/tg/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "miniapp-webhook-secret"},
                json={"message": {
                    "text": "/start expired-login-code",
                    "from": {"id": 880013, "first_name": "Олег"},
                    "chat": {"id": 880013, "type": "private"},
                }},
            )
        finally:
            notify.send_to = original
        self.assertEqual(response.status_code, 200)
        conn = db.connect()
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM users WHERE telegram_id=880013"
        ).fetchone())
        conn.close()
        self.assertTrue(any("устарела" in text for text in sent))

    def test_write_access_service_message_marks_real_chat_linked(self):
        conn = db.connect()
        conn.execute(
            "INSERT INTO users(telegram_id, display_name, bot_linked, created_at) "
            "VALUES(880012, 'Лена', 0, CURRENT_TIMESTAMP)"
        )
        conn.commit(); conn.close()
        original = notify.send_to
        notify.send_to = lambda *args, **kwargs: True
        try:
            response = self.client.post(
                "/tg/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "miniapp-webhook-secret"},
                json={"message": {
                    "from": {"id": 880012, "first_name": "Лена"},
                    "chat": {"id": 880012, "type": "private"},
                    "write_access_allowed": {},
                }},
            )
        finally:
            notify.send_to = original
        self.assertEqual(response.status_code, 200)
        conn = db.connect()
        self.assertEqual(conn.execute(
            "SELECT bot_linked FROM users WHERE telegram_id=880012"
        ).fetchone()[0], 1)
        conn.close()

    def test_blocking_bot_disables_notifications_via_chat_member_update(self):
        conn = db.connect()
        conn.execute(
            "INSERT INTO users(telegram_id, display_name, bot_linked, created_at) "
            "VALUES(880014, 'Саша', 1, CURRENT_TIMESTAMP)"
        )
        conn.commit(); conn.close()
        response = self.client.post(
            "/tg/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "miniapp-webhook-secret"},
            json={"my_chat_member": {
                "chat": {"id": 880014, "type": "private"},
                "new_chat_member": {"status": "kicked"},
            }},
        )
        self.assertEqual(response.status_code, 200)
        conn = db.connect()
        self.assertEqual(conn.execute(
            "SELECT bot_linked FROM users WHERE telegram_id=880014"
        ).fetchone()[0], 0)
        conn.close()

    def test_startup_configures_global_desktop_mobile_menu_button(self):
        calls = []

        class Response:
            status_code = 200
            text = "ok"

        original = auth_routes.httpx.post
        auth_routes.httpx.post = lambda url, json=None, timeout=None: (
            calls.append((url, json)) or Response()
        )
        try:
            auth_routes.setup_miniapp_menu()
        finally:
            auth_routes.httpx.post = original
        self.assertTrue(calls[0][0].endswith("/setChatMenuButton"))
        menu = calls[0][1]["menu_button"]
        self.assertEqual(menu["type"], "web_app")
        self.assertEqual(menu["web_app"]["url"], "https://localhost/tg/app")


class TelegramRichMessageTests(unittest.TestCase):
    def test_internal_action_uses_miniapp_and_external_cannot_redirect(self):
        internal = notify.action_markup(
            "Открыть", "https://localhost/admin/questions?f=all"
        )["inline_keyboard"][0][0]
        self.assertEqual(internal["style"], "primary")
        self.assertIn("web_app", internal)
        self.assertIn("next=%2Fadmin%2Fquestions%3Ff%3Dall",
                      internal["web_app"]["url"])
        review = notify.action_markup(
            "К обзору", "https://localhost/u/42?tab=reviews#review-7"
        )["inline_keyboard"][0][0]
        self.assertIn("web_app", review)
        self.assertIn(
            "next=%2Fu%2F42%3Ftab%3Dreviews%23review-7",
            review["web_app"]["url"],
        )
        self.assertEqual(
            auth_routes._safe_next("/u/42?tab=reviews#review-7"),
            "/u/42?tab=reviews#review-7",
        )
        lookalike = notify.action_markup(
            "Открыть", "https://localhost.evil.example/admin/questions"
        )["inline_keyboard"][0][0]
        self.assertNotIn("web_app", lookalike)
        self.assertEqual(lookalike["url"],
                         "https://localhost.evil.example/admin/questions")

    def test_send_rich_message_and_safe_sendmessage_fallback(self):
        calls = []

        class Response:
            def __init__(self, status):
                self.status_code = status
                self.text = "response"

        original = notify.httpx.post
        notify.TOKEN = "123456:miniapp-test-token"
        markup = notify.action_markup("Открыть", "https://localhost/admin/")
        try:
            notify.httpx.post = lambda url, json=None, timeout=None: (
                calls.append((url.rsplit("/", 1)[-1], json)) or Response(200)
            )
            self.assertTrue(notify.send_to(
                880020, notify.card("Новое событие", "Появился новый вариант."),
                reply_markup=markup,
            ))
            method, payload = calls[0]
            self.assertEqual(method, "sendRichMessage")
            self.assertIn("<h3>Новое событие</h3>", payload["rich_message"]["html"])
            self.assertIn("<p>Появился новый вариант.</p>",
                          payload["rich_message"]["html"])
            self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["style"],
                             "primary")

            calls.clear()
            statuses = iter((404, 200))
            notify.httpx.post = lambda url, json=None, timeout=None: (
                calls.append((url.rsplit("/", 1)[-1], json))
                or Response(next(statuses))
            )
            self.assertTrue(notify.send_to(880020, "Fallback", reply_markup=markup))
            self.assertEqual([method for method, _ in calls],
                             ["sendRichMessage", "sendMessage"])
            legacy_button = calls[1][1]["reply_markup"]["inline_keyboard"][0][0]
            self.assertNotIn("style", legacy_button)
        finally:
            notify.httpx.post = original

    def test_start_video_is_native_media_and_has_legacy_fallback(self):
        calls = []

        class Response:
            def __init__(self, status):
                self.status_code = status
                self.text = "response"

        original_post = notify.httpx.post
        original_send = notify.send_to
        original_token = notify.TOKEN
        notify.TOKEN = "123456:miniapp-test-token"
        markup = auth_routes._miniapp_button()
        try:
            def post(url, *, data=None, files=None, timeout=None, **_kwargs):
                calls.append((url.rsplit("/", 1)[-1], data, files))
                return Response(200)

            notify.httpx.post = post
            self.assertTrue(notify.send_video_to(
                880021, auth_routes.START_VIDEO_PATH, "<b>Добро пожаловать</b>",
                reply_markup=markup,
            ))
            method, data, files = calls[0]
            self.assertEqual(method, "sendVideo")
            self.assertIn("video", files)
            self.assertEqual(data["supports_streaming"], "true")
            self.assertEqual(data["caption"], "<b>Добро пожаловать</b>")
            self.assertEqual(data["parse_mode"], "HTML")
            keyboard = json.loads(data["reply_markup"])["inline_keyboard"]
            self.assertEqual(keyboard[0][0]["web_app"]["url"],
                             "https://localhost/tg/app")
            self.assertEqual(keyboard[1][0]["text"], "Открыть в браузере")
            self.assertEqual(keyboard[0][0]["style"], "primary")

            calls.clear()
            fallbacks = []
            def failing_post(url, *, data=None, files=None, **_kwargs):
                calls.append((url.rsplit("/", 1)[-1], data, files))
                return Response(500)
            notify.httpx.post = failing_post
            notify.send_to = lambda chat, text, **kwargs: (
                fallbacks.append((chat, text, kwargs)) or True
            )
            self.assertTrue(notify.send_video_to(
                880021, auth_routes.START_VIDEO_PATH, "Привет",
                reply_markup=markup,
            ))
            self.assertEqual([call[0] for call in calls],
                             ["sendVideo", "sendVideo"])
            styled = json.loads(calls[0][1]["reply_markup"])
            legacy = json.loads(calls[1][1]["reply_markup"])
            self.assertEqual(styled["inline_keyboard"][0][0]["style"], "primary")
            self.assertNotIn("style", legacy["inline_keyboard"][0][0])
            self.assertEqual(fallbacks[0][0:2], (880021, "Привет"))
            self.assertEqual(fallbacks[0][2]["reply_markup"], markup)
        finally:
            notify.httpx.post = original_post
            notify.send_to = original_send
            notify.TOKEN = original_token


if __name__ == "__main__":
    unittest.main()
