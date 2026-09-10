"""FLOW-01 / TEST-01: массовые действия через настоящий HTTP → middleware → SQLite."""

import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-bulk-http-")
os.environ.update({
    "DATA_DIR": _DATA.name, "SECRET_KEY": "bulk-http-synthetic-secret",
    "COOKIE_SECURE": "false", "DOMAIN": "testserver", "LOG_LEVEL": "CRITICAL",
    "TG_BOT_TOKEN": "", "TG_CHAT_ID": "", "TG_BACKUP_CHAT_ID": "",
    "TG_BOT_USERNAME": "bulk_http_test_bot", "OPERATOR_TG_IDS": "", "SENTRY_DSN": "",
})

import db
import main
import ratelimit

STAMP = "2030-01-01T12:00:00"
ACTIONS = ("archive", "restore", "make_public", "make_private", "delete")


class BulkHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        ratelimit._rates.clear()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.conn.execute("DELETE FROM users")
        self.conn.execute("DELETE FROM login_codes")
        self.owner_id = self.conn.execute(
            "INSERT INTO users(telegram_id,display_name,created_at) VALUES(91001,'Владелец',?)",
            (STAMP,),
        ).lastrowid
        self.foreign_id = self.conn.execute(
            "INSERT INTO users(telegram_id,display_name,created_at) VALUES(91002,'Другой',?)",
            (STAMP,),
        ).lastrowid
        self.conn.commit()
        self.client = TestClient(main.app, follow_redirects=False, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.login(91001)

    def login(self, telegram_id):
        self.client.cookies.clear()
        started = self.client.post("/auth/start")
        self.assertEqual(started.status_code, 200, started.text)
        code = started.json()["code"]
        # Только внешнее подтверждение входа синтетическое; cookie и реестр
        # сессий создаёт штатный /auth/poll, CSRF берём из настоящей страницы.
        self.conn.execute(
            "UPDATE login_codes SET status='confirmed',telegram_id=? WHERE code=?", (telegram_id, code),
        )
        self.conn.commit()
        self.assertEqual(self.client.get("/auth/poll", params={"code": code}).json()["status"], "ok")
        page = self.client.get("/admin/dates")
        self.assertEqual(page.status_code, 200, page.text)
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def date(self, action, *, owner=None):
        did = self.conn.execute(
            "INSERT INTO dates(owner_id,name,share_token,is_draft,is_public,origin,archived_at,created_at) "
            "VALUES(?,?,lower(hex(randomblob(16))),0,?,'admin',?,?)",
            (owner or self.owner_id, "Синтетическое событие", int(action == "make_private"),
             STAMP if action == "restore" else None, STAMP),
        ).lastrowid
        self.conn.commit()
        return did

    def state(self, did):
        row = self.conn.execute("SELECT * FROM dates WHERE id=?", (did,)).fetchone()
        return dict(row) if row is not None else None

    def post(self, action, ids, **kwargs):
        data = {"action": action, "date_ids": ids, "csrf": self.csrf,
                "next": "/admin/dates?view=active"}
        data.update(kwargs.pop("data", {}))
        headers = {"Referer": "http://testserver/admin/dates?view=active"}
        headers.update(kwargs.pop("headers", {}))
        return self.client.post("/admin/dates/bulk", data=data, headers=headers, **kwargs)

    def message(self, response):
        self.assertEqual(response.status_code, 303, response.text)
        return parse_qs(urlsplit(response.headers["location"]).query).get("msg", [""])[0]

    def assert_visible_error(self, response, expected):
        message = self.message(response)
        self.assertIn(expected, message)
        self.assertEqual(urlsplit(response.headers["location"]).path, "/admin/dates")
        page = self.client.get(response.headers["location"])
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn(expected, page.text)

    def assert_action(self, action):
        first, second = self.date(action), self.date(action)
        response = self.post(action, [first, first, second])
        self.assertIn(": 2 события", self.message(response))
        for did in (first, second):
            state = self.state(did)
            if action == "delete":
                self.assertIsNone(state)
            elif action in {"archive", "restore"}:
                self.assertEqual(bool(state["archived_at"]), action == "archive")
            else:
                self.assertEqual(state["is_public"], int(action == "make_public"))

    def test_archive_owned_dates(self):
        self.assert_action("archive")

    def test_restore_owned_dates(self):
        self.assert_action("restore")

    def test_make_public_owned_dates(self):
        self.assert_action("make_public")

    def test_make_private_owned_dates(self):
        self.assert_action("make_private")

    def test_delete_owned_dates(self):
        self.assert_action("delete")

    def test_all_actions_reject_foreign_ids_atomically_and_allow_retry(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                owned = self.date(action)
                foreign = self.date(action, owner=self.foreign_id)
                before = [self.state(did) for did in (owned, foreign)]
                self.assert_visible_error(self.post(action, [owned, foreign]), "Одно из событий не найдено")
                self.assertEqual([self.state(did) for did in (owned, foreign)], before)
                self.assertIn(": 1 событие", self.message(self.post(action, [owned])))
                self.assertEqual(self.state(foreign), before[1])

    def test_all_actions_require_session_and_csrf(self):
        anonymous = TestClient(main.app, follow_redirects=False, raise_server_exceptions=False)
        self.addCleanup(anonymous.close)
        for action in ACTIONS:
            with self.subTest(action=action):
                did = self.date(action)
                before = self.state(did)
                response = anonymous.post("/admin/dates/bulk", data={
                    "action": action, "date_ids": [did], "csrf": self.csrf,
                })
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/login")
                for invalid in ("", "wrong-token"):
                    self.assert_visible_error(
                        self.post(action, [did], data={"csrf": invalid}), "Сессия устарела",
                    )
                    self.assertEqual(self.state(did), before)
                self.assert_visible_error(
                    self.post(action, [did], headers={"Origin": "https://foreign.example"}),
                    "Запрос с другого сайта отклонён",
                )
                self.assertEqual(self.state(did), before)

    def test_repeated_actions_are_noops_and_repeated_delete_is_not_found(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                did = self.date(action)
                self.assertIn(": 1 событие", self.message(self.post(action, [did])))
                after = self.state(did)
                repeated = self.post(action, [did])
                if action == "delete":
                    self.assert_visible_error(repeated, "Одно из событий не найдено")
                else:
                    self.assertIn(": 0 событий", self.message(repeated))
                self.assertEqual(self.state(did), after)

    def test_each_action_enforces_real_rate_limit_and_recovers_after_window(self):
        limit, window = ratelimit.USER_RATE_RULES["datebulk"]
        for action in ACTIONS:
            with self.subTest(action=action):
                ratelimit._rates.clear()
                did = self.date(action)
                for attempt in range(limit):
                    response = self.post(action, [did])
                    self.assertNotIn("Слишком много действий", self.message(response), attempt)
                blocked = self.date(action)
                before = self.state(blocked)
                self.assert_visible_error(self.post(action, [blocked]), "Слишком много действий")
                self.assertEqual(self.state(blocked), before)
                self.assertTrue(any(key.startswith("datebulk:u:") for key in ratelimit._rates))
                self.assertTrue(any(key.startswith("datebulk:i:") for key in ratelimit._rates))
                # Время окна проходит без sleep и без замены limiter/registry.
                for key, timestamps in ratelimit._rates.items():
                    if key.startswith("datebulk:"):
                        ratelimit._rates[key] = [timestamp - window - 1 for timestamp in timestamps]
                self.assertIn(": 1 событие", self.message(self.post(action, [blocked])))

    def test_ip_budget_blocks_a_fresh_user_after_three_user_budgets(self):
        limit, _ = ratelimit.USER_RATE_RULES["datebulk"]
        # Четыре настоящие сессии с общим адресом клиента. Первые три
        # исчерпывают IP-бюджет; у четвёртой собственный бюджет ещё пуст.
        for number in range(4):
            telegram_id = 91100 + number
            uid = self.conn.execute(
                "INSERT INTO users(telegram_id,display_name,created_at) VALUES(?,'IP тест',?)",
                (telegram_id, STAMP),
            ).lastrowid
            self.conn.commit()
            self.login(telegram_id)
            did = self.date("make_public", owner=uid)
            if number < 3:
                for attempt in range(limit):
                    self.assertNotIn("Слишком много действий", self.message(self.post("make_public", [did])), attempt)
                self.assertEqual(self.state(did)["is_public"], 1)
            else:
                self.assert_visible_error(self.post("make_public", [did]), "Слишком много действий")
                self.assertEqual(self.state(did)["is_public"], 0)

    def test_later_lifecycle_error_rolls_back_rows_bookings_and_notifications(self):
        for action in ("archive", "restore", "delete"):
            with self.subTest(action=action):
                first, blocked = self.date(action), self.date(action)
                for did, closed in ((first, False), (blocked, True)):
                    cid = self.conn.execute(
                        "INSERT INTO categories(owner_id,name,link_token,choice_mode,voting_deadline,"
                        "voting_status,closed_at,created_at) VALUES(?,?,lower(hex(randomblob(16))),"
                        "'multiple','2099-01-01T00:00:00',?,?,?)",
                        (self.owner_id, "Синтетическая подборка", "open", None, STAMP),
                    ).lastrowid
                    self.conn.execute("INSERT INTO date_categories(date_id,category_id) VALUES(?,?)", (did, cid))
                    self.conn.execute(
                        "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) VALUES(?,?,?,?,?)",
                        (did, cid, "synthetic-guest", self.foreign_id, STAMP),
                    )
                    if closed:
                        self.conn.execute(
                            "UPDATE categories SET voting_status='no_winner',closed_at=? WHERE id=?", (STAMP, cid),
                        )
                self.conn.commit()
                tables = ("dates", "date_categories", "bookings", "notification_outbox", "review_queue")
                before = {table: [tuple(row) for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                          for table in tables}
                self.assert_visible_error(self.post(action, [first, blocked]), "уже завершено")
                after = {table: [tuple(row) for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                         for table in tables}
                self.assertEqual(after, before)
                self.assertIn(": 1 событие", self.message(self.post(action, [first])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
