"""SEC-01: привязка OAuth к исходному аккаунту и registry-сессии."""
import base64
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-oauth-binding-")
SECRET = "oauth-binding-test-secret"
os.environ.update(DATA_DIR=_DATA.name, SECRET_KEY=SECRET, COOKIE_SECURE="false",
                  TG_BOT_TOKEN="", TG_CHAT_ID="", DOMAIN="testserver")

import auth_routes
import db
import main
import sessions
import users


class OAuthBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        main._rates.clear()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.conn.execute("DELETE FROM users")
        self.a, self.b = [self.conn.execute(
            "INSERT INTO users(display_name,created_at) VALUES(?,?)",
            (name, main.now_iso()),
        ).lastrowid for name in ("A", "B")]
        self.conn.commit()
        self.client = self.new_client()
        providers = patch.dict(auth_routes.OAUTH_PROVIDERS,
                               {"google": ("test", "test"), "discord": ("test", "test")})
        providers.start()
        self.addCleanup(providers.stop)
        identity = patch.object(auth_routes, "_oauth_fetch_identity", return_value={
            "uid": "external-test", "name": "External", "email": "test@example.invalid"})
        self.provider = identity.start()
        self.addCleanup(identity.stop)

    def new_client(self):
        client = TestClient(main.app, follow_redirects=False)
        self.addCleanup(client.close)
        return client

    def payload(self, client=None):
        cookie = (client or self.client).cookies.get("admin_s")
        return json.loads(base64.b64decode(TimestampSigner(SECRET).unsign(cookie))) if cookie else {}

    def set_payload(self, payload, client=None):
        client = client or self.client
        cookie = TimestampSigner(SECRET).sign(base64.b64encode(json.dumps(payload).encode())).decode()
        client.cookies.clear()
        client.cookies.set("admin_s", cookie, domain="testserver.local", path="/")

    def authenticate(self, uid, client=None):
        # Настоящий issuer ротирует registry-сессию, сохраняя незавершённый OAuth.
        from types import SimpleNamespace
        request = SimpleNamespace(session=self.payload(client))
        sessions.issue_session(request, self.conn, uid)
        self.set_payload(request.session, client)

    def start(self, provider="google", link=True, client=None):
        response = (client or self.client).get(f"/auth/{provider}", params={"link": "1"} if link else {})
        self.assertEqual(response.status_code, 303)
        return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]

    def callback(self, state, provider="google", client=None, **params):
        return (client or self.client).get(f"/auth/{provider}/callback",
                                          params={"state": state, "code": "mock-code", **params})

    def links(self):
        return [tuple(row) for row in self.conn.execute(
            "SELECT provider,provider_uid,user_id FROM oauth_accounts ORDER BY provider,provider_uid")]

    def assert_rejected(self, state, client=None):
        before = self.links()
        response = self.callback(state, client=client)
        self.assertEqual(self.links(), before, "Отказ не должен менять OAuth-привязки")
        self.assertEqual(response.status_code, 403)
        self.provider.assert_not_called()

    def test_correct_link(self):
        self.authenticate(self.a)
        self.assertEqual(self.callback(self.start()).status_code, 303)
        self.assertEqual(self.links(), [("google", "external-test", self.a)])
        self.assertEqual(self.payload()["user_id"], self.a)

    def test_account_changed_before_callback(self):
        self.authenticate(self.a)
        state = self.start()
        self.authenticate(self.b)
        self.assert_rejected(state)

    def test_same_user_rotated_session(self):
        self.authenticate(self.a)
        state = self.start()
        self.authenticate(self.a)
        self.assert_rejected(state)

    def test_independent_session_cannot_complete_flow(self):
        self.authenticate(self.a)
        state = self.start()
        second = self.new_client()
        self.authenticate(self.a, second)
        # Даже перенос browser-flow proof в другую легальную сессию не меняет target.
        copied = self.payload()
        other = self.payload(second)
        copied.update(user_id=other["user_id"], session_id=other["session_id"], csrf=other["csrf"])
        self.set_payload(copied, second)
        self.assert_rejected(state, second)

    def test_logout_rejects_callback_and_old_cookie(self):
        self.authenticate(self.a)
        state = self.start()
        copied = self.payload()
        response = self.client.post("/admin/logout", data={"csrf": copied["csrf"]})
        self.assertEqual(response.status_code, 303)
        self.assert_rejected(state)
        self.set_payload(copied)
        self.assert_rejected(state)

    def test_duplicate_identity_remains_with_original_user(self):
        users.link_oauth_account(self.conn, self.b, "google", "external-test")
        self.authenticate(self.a)
        self.assertEqual(self.callback(self.start()).status_code, 303)
        self.assertEqual(self.links(), [("google", "external-test", self.b)])

    def test_existing_own_identity_is_idempotent(self):
        users.link_oauth_account(self.conn, self.a, "google", "external-test")
        self.authenticate(self.a)
        self.assertEqual(self.callback(self.start()).status_code, 303)
        self.assertEqual(self.links(), [("google", "external-test", self.a)])

    def test_link_requires_authentication_at_start(self):
        self.assertEqual(self.client.get("/auth/google?link=1").status_code, 403)
        self.assertEqual(self.links(), [])

    def test_revocation_during_code_exchange_prevents_mutation(self):
        self.authenticate(self.a)
        state = self.start()
        identity = self.provider.return_value
        def revoke(*args):
            sessions.revoke_user_sessions(self.conn, self.a)
            return identity
        self.provider.side_effect = revoke
        self.assertEqual(self.callback(state).status_code, 403)
        self.assertEqual(self.links(), [])

    def test_v38_migration_preserves_users_sessions_and_identities(self):
        self.authenticate(self.a)
        users.link_oauth_account(self.conn, self.a, "google", "already-linked")
        original_sessions = [tuple(row) for row in self.conn.execute("SELECT * FROM auth_sessions")]
        original_links = self.links()
        self.conn.execute("DROP TABLE oauth_flows")
        self.conn.execute("PRAGMA user_version=38")
        self.conn.commit()
        db.init_db()
        db.init_db()
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], db.LATEST_VERSION)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM oauth_flows").fetchone()[0], 0)
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM auth_sessions")], original_sessions)
        self.assertEqual(self.links(), original_links)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.callback(self.start()).status_code, 303)

    def test_login_identity_is_not_published_before_session_is_issued(self):
        self.authenticate(self.a)
        state = self.start(link=False)
        issue = sessions.issue_session
        def inspect_then_issue(request, conn, uid, **kwargs):
            # Второе соединение не должно видеть частично завершённый login.
            self.assertEqual(self.links(), [])
            issue(request, conn, uid, **kwargs)
        with patch.object(sessions, "issue_session", side_effect=inspect_then_issue):
            self.assertEqual(self.callback(state).status_code, 303)
        self.assertEqual(len(self.links()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
