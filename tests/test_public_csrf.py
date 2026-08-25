#!/usr/bin/env python3
"""CSRF-регрессии для изменяющих публичных /c и /d ручек."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
import ast
from pathlib import Path

from starlette.testclient import TestClient


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-public-csrf-")
os.environ.update({
    "DATA_DIR": _DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "csrf.test",
    "SECRET_KEY": "csrf-regression-secret",
    "TG_BOT_TOKEN": "",
    "TG_BOT_USERNAME": "date4you_csrf_test_bot",
    "TG_WEBHOOK_SECRET": "csrf-hook-secret",
    "OPERATOR_TG_IDS": "",
})

import db  # noqa: E402
import main  # noqa: E402
import ratelimit  # noqa: E402


WEBHOOK_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "csrf-hook-secret"}
STAMP = "2030-01-01T10:00:00"


def login(client: TestClient, telegram_id: int) -> tuple[int, str]:
    code = client.post("/auth/start").json()["code"]
    person = {
        "id": telegram_id,
        "username": f"csrf_{telegram_id}",
        "first_name": "Тест",
    }
    assert client.post(
        "/tg/webhook", headers=WEBHOOK_HEADERS,
        json={"message": {"text": f"/start {code}", "from": person}},
    ).status_code == 200
    assert client.post(
        "/tg/webhook", headers=WEBHOOK_HEADERS,
        json={"callback_query": {
            "id": f"confirm-{telegram_id}",
            "data": f"auth_confirm:{code}",
            "from": person,
        }},
    ).status_code == 200
    assert client.get(f"/auth/poll?code={code}").json()["status"] == "ok"
    page = client.get("/admin/categories")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    conn = db.connect()
    try:
        user_id = int(conn.execute(
            "SELECT id FROM users WHERE telegram_id=?", (telegram_id,),
        ).fetchone()[0])
    finally:
        conn.close()
    return user_id, csrf


class PublicCsrfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = TestClient(main.app, follow_redirects=False).__enter__()
        cls.viewer = TestClient(main.app, follow_redirects=False).__enter__()
        cls.anonymous = TestClient(main.app, follow_redirects=False).__enter__()
        cls.owner_id, _ = login(cls.owner, 997001)
        cls.viewer_id, cls.csrf = login(cls.viewer, 997002)

        conn = db.connect()
        try:
            cls.category_id = int(conn.execute(
                "INSERT INTO categories("
                "owner_id,name,category_skin,link_token,link_enabled,choice_mode,"
                "voting_deadline,voting_status,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (cls.owner_id, "CSRF категория", "friends", "csrf-category", 1,
                 "single", "2031-01-01T10:00", "open", STAMP),
            ).lastrowid)
            cls.date_id = int(conn.execute(
                "INSERT INTO dates("
                "owner_id,name,share_token,is_draft,is_public,origin,created_at"
                ") VALUES(?,?,?,0,1,'admin',?)",
                (cls.owner_id, "CSRF событие", "csrf-date", STAMP),
            ).lastrowid)
            conn.execute(
                "INSERT INTO date_categories(date_id,category_id,position) "
                "VALUES(?,?,0)", (cls.date_id, cls.category_id),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.anonymous.__exit__(None, None, None)
        cls.viewer.__exit__(None, None, None)
        cls.owner.__exit__(None, None, None)

    def setUp(self):
        main._rates.clear()
        conn = db.connect()
        try:
            conn.execute("DELETE FROM questions WHERE category_id=?", (self.category_id,))
            conn.execute(
                "DELETE FROM reports WHERE target_type='date' AND target_id=?",
                (self.date_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def _question_count(self) -> int:
        conn = db.connect()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM questions WHERE category_id=?",
                (self.category_id,),
            ).fetchone()[0])
        finally:
            conn.close()

    def test_missing_token_and_cross_site_origin_are_rejected(self):
        payload = {"date_id": self.date_id, "text": "Небезопасный вопрос"}

        missing = self.viewer.post("/c/csrf-category/question", data=payload)
        self.assertEqual(missing.status_code, 403)

        evil = self.viewer.post(
            "/c/csrf-category/question", data=payload,
            headers={"X-CSRF-Token": self.csrf, "Origin": "https://evil.example"},
        )
        self.assertEqual(evil.status_code, 403)
        self.assertEqual(self._question_count(), 0)

    def test_same_origin_header_and_form_token_work(self):
        normal = self.viewer.post(
            "/c/csrf-category/question",
            data={"date_id": self.date_id, "text": "Штатный вопрос"},
            # Публичный https-origin отличается от внутренней http-схемы за
            # reverse proxy — оба варианта должны считаться своими.
            headers={"X-CSRF-Token": self.csrf, "Origin": "https://csrf.test"},
        )
        self.assertEqual(normal.status_code, 200, normal.text)
        self.assertEqual(self._question_count(), 1)

        copied = self.viewer.post(
            "/d/csrf-date/add", data={"csrf": self.csrf},
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(copied.status_code, 303, copied.text)

    def test_anonymous_action_still_requests_login(self):
        response = self.anonymous.post(
            "/c/csrf-category/question",
            data={"date_id": self.date_id, "text": "Анонимный вопрос"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.json()["detail"]["need_login"])

    def test_report_is_anonymous_deduplicated_and_not_ip_derived(self):
        payload = {
            "target_type": "date",
            "target_id": self.date_id,
            "reason": "Проверить без входа",
        }
        first = self.anonymous.post("/c/csrf-category/report", data=payload)
        second = self.anonymous.post("/d/csrf-date/report", data=payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)

        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT reporter, reason FROM reports "
                "WHERE target_type='date' AND target_id=?",
                (self.date_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertRegex(rows[0]["reporter"], r"^a:[A-Za-z0-9_-]{24,64}$")
        self.assertNotIn("127.0.0.1", rows[0]["reporter"])
        self.assertNotIn("testclient", rows[0]["reporter"])
        self.assertEqual(rows[0]["reason"], "Проверить без входа")

    def test_anonymous_report_rejects_cross_site_browser_post(self):
        response = self.anonymous.post(
            "/c/csrf-category/report",
            data={"target_type": "date", "target_id": self.date_id},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_withdraw_has_a_rate_rule(self):
        self.assertIn("withdraw", ratelimit.RATE_RULES)

    def test_every_public_post_uses_the_shared_guard(self):
        """Новая публичная POST-ручка не должна тихо появиться без защиты."""
        source = (APP / "public_routes.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        unguarded: list[str] = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = " ".join(ast.unparse(d) for d in node.decorator_list)
            if ".post(" not in decorators:
                continue
            body = ast.unparse(node)
            if ("acting_user(" not in body
                    and "report_identity(" not in body
                    and "users.current_user" not in decorators):
                unguarded.append(node.name)
        self.assertEqual(unguarded, [])

        self.assertIn(
            '"X-CSRF-Token": CSRF',
            (APP / "static" / "guest.js").read_text(encoding="utf-8"),
        )
        self.assertIn(
            '"X-CSRF-Token"',
            (APP / "static" / "ui.js").read_text(encoding="utf-8"),
        )
        self.assertIn(
            '"X-CSRF-Token"',
            (APP / "static" / "profile.js").read_text(encoding="utf-8"),
        )
        self.assertIn(
            '"X-CSRF-Token"',
            (APP / "static" / "admin.js").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
