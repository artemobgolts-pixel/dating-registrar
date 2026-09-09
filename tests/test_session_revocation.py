"""SEC-03: отзыв подписанной сессии через настоящий HTTP/backend и SQLite."""

import base64
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-session-revocation-")
os.environ.update({
    "DATA_DIR": _DATA.name, "SECRET_KEY": "revocation-test-secret",
    "COOKIE_SECURE": "false", "TG_BOT_TOKEN": "", "TG_CHAT_ID": "",
    "TG_BOT_USERNAME": "revocation_test_bot", "DOMAIN": "testserver",
})

import db
import auth_routes
import main
import sessions


class SessionRevocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        main._rates.clear()
        self.conn = db.connect()
        self.conn.execute("DELETE FROM users")
        self.conn.execute("DELETE FROM login_codes")
        self.uid = self.conn.execute(
            "INSERT INTO users(telegram_id,display_name,created_at) VALUES(90001,'Тест',?)",
            (main.now_iso(),),
        ).lastrowid
        self.conn.commit()
        self.client = TestClient(main.app, follow_redirects=False)
        self.addCleanup(self.client.close)
        self.addCleanup(self.conn.close)

    def login(self, client=None):
        client = client or self.client
        response = client.post("/auth/start")
        self.assertEqual(response.status_code, 200)
        code = response.json()["code"]
        # Синтетически подтверждаем только код этого браузера, без Telegram.
        self.conn.execute(
            "UPDATE login_codes SET status='confirmed',telegram_id=90001 WHERE code=?",
            (code,),
        )
        self.conn.commit()
        self.assertEqual(client.get("/auth/poll", params={"code": code}).json()["status"], "ok")
        return client.cookies.get("admin_s")

    def replay(self, cookie):
        self.client.cookies.clear()
        self.client.cookies.set("admin_s", cookie)
        return self.client.get("/admin/profile")

    def assert_rejected(self, cookie):
        response = self.replay(cookie)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login"))

    def test_logout_replay_and_independent_session(self):
        first = self.login()
        second_client = TestClient(main.app, follow_redirects=False)
        self.addCleanup(second_client.close)
        second = self.login(second_client)
        self.assertNotEqual(first, second)
        page = self.client.get("/admin/profile")
        self.assertEqual(page.status_code, 200)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        copied = self.client.cookies.get("admin_s")
        self.assertEqual(self.client.post("/admin/logout", data={"csrf": csrf}).status_code, 303)
        self.assert_rejected(copied)
        self.assert_rejected(copied)
        self.assertEqual(second_client.get("/admin/profile").status_code, 200)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 1)
        # Повторный вход не оживляет старый cookie.
        self.client.cookies.clear()
        self.login()
        self.assert_rejected(copied)

    def test_revoke_all_sessions_is_persistent(self):
        first = self.login()
        self.client.cookies.clear()
        second = self.login()
        sessions.revoke_user_sessions(self.conn, self.uid)
        self.conn.commit()
        self.assert_rejected(first)
        self.assert_rejected(second)

    def test_deactivated_user_cannot_use_copied_session(self):
        cookie = self.login()
        self.conn.execute("UPDATE users SET is_active=0 WHERE id=?", (self.uid,))
        self.conn.commit()
        self.assert_rejected(cookie)

    def test_server_expiration_cannot_be_extended_by_cookie_refresh(self):
        cookie = self.login()
        self.conn.execute("UPDATE auth_sessions SET expires_at=?", (int(time.time()) - 1,))
        self.conn.commit()
        self.assert_rejected(cookie)

    def test_expired_signed_cookie_is_rejected_even_with_active_registry(self):
        cookie = self.login()
        payload = TimestampSigner("revocation-test-secret").unsign(cookie)
        with patch.object(TimestampSigner, "get_timestamp", return_value=int(time.time()) - 31 * 86400):
            expired = TimestampSigner("revocation-test-secret").sign(payload).decode()
        self.assert_rejected(expired)

    def test_legacy_signed_cookie_without_registry_is_not_migrated_on_replay(self):
        payload = base64.b64encode(json.dumps({"user_id": self.uid, "csrf": "legacy"}).encode())
        cookie = TimestampSigner("revocation-test-secret").sign(payload).decode()
        self.assert_rejected(cookie)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)

    def test_revocation_also_removes_public_authentication(self):
        cookie = self.login()
        sessions.revoke_user_sessions(self.conn, self.uid)
        self.conn.commit()
        self.client.cookies.clear()
        self.client.cookies.set("admin_s", cookie)
        # /auth/start различает обычный вход и привязку к текущему аккаунту.
        response = self.client.post("/auth/start")
        row = self.conn.execute("SELECT purpose,user_id FROM login_codes WHERE code=?", (response.json()["code"],)).fetchone()
        self.assertEqual(tuple(row), ("login", None))

    def test_v37_migration_adds_empty_registry_and_preserves_users(self):
        self.conn.execute("DROP TABLE auth_sessions")
        self.conn.execute("PRAGMA user_version=37")
        self.conn.commit()
        db.init_db()
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], db.LATEST_VERSION)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)
        self.assertIsNotNone(self.conn.execute("SELECT id FROM users WHERE id=?", (self.uid,)).fetchone())

    def test_revoked_oauth_link_state_cannot_reach_provider_or_link_account(self):
        self.login()
        with patch.dict(auth_routes.OAUTH_PROVIDERS, {"google": ("test-id", "test-secret")}):
            response = self.client.get("/auth/google?link=1")
            state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
            copied = self.client.cookies.get("admin_s")
            sessions.revoke_user_sessions(self.conn, self.uid)
            self.client.cookies.clear()
            self.client.cookies.set("admin_s", copied)
            with patch.object(auth_routes, "_oauth_fetch_identity") as provider:
                response = self.client.get("/auth/google/callback", params={"state": state, "code": "synthetic"})
            self.assertEqual(response.status_code, 403)
            provider.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM oauth_accounts").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
