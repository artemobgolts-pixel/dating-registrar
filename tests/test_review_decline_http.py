"""FLOW-02: отказ от отзыва через настоящие session/CSRF/limiter и SQLite."""

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
_DATA = tempfile.TemporaryDirectory(prefix="date4you-review-decline-http-")
os.environ.update({
    "DATA_DIR": _DATA.name, "SECRET_KEY": "review-decline-http-synthetic-secret",
    "COOKIE_SECURE": "false", "DOMAIN": "testserver", "LOG_LEVEL": "CRITICAL",
    "TG_BOT_TOKEN": "", "TG_CHAT_ID": "", "TG_BACKUP_CHAT_ID": "",
    "TG_BOT_USERNAME": "review_decline_test_bot", "OPERATOR_TG_IDS": "", "SENTRY_DSN": "",
})

import db
import main
import ratelimit

STAMP = "2000-01-01T12:00:00"


class ReviewDeclineHttpTests(unittest.TestCase):
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
            "INSERT INTO users(telegram_id,display_name,created_at) VALUES(92001,'Организатор',?)",
            (STAMP,),
        ).lastrowid
        self.viewer_id = self.conn.execute(
            "INSERT INTO users(telegram_id,display_name,created_at) VALUES(92002,'Участник',?)",
            (STAMP,),
        ).lastrowid
        self.conn.commit()
        self.client = TestClient(main.app, follow_redirects=False, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.login(92002)

    def login(self, telegram_id):
        self.client.cookies.clear()
        response = self.client.post("/auth/start")
        self.assertEqual(response.status_code, 200, response.text)
        code = response.json()["code"]
        # Синтетическое подтверждение внешнего входа; сессию и CSRF создаёт
        # настоящий auth endpoint, registry проверяет middleware.
        self.conn.execute(
            "UPDATE login_codes SET status='confirmed',telegram_id=? WHERE code=?",
            (telegram_id, code),
        )
        self.conn.commit()
        self.assertEqual(self.client.get("/auth/poll", params={"code": code}).json()["status"], "ok")
        page = self.client.get("/admin/dates")
        self.assertEqual(page.status_code, 200, page.text)
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def date(self, *, eligible=True, future=False):
        token = "decline-" + os.urandom(12).hex()
        did = self.conn.execute(
            "INSERT INTO dates(owner_id,name,share_token,is_draft,is_public,ends_at,created_at) "
            "VALUES(?,'Синтетическое событие',?,0,1,?,?)",
            (self.owner_id, token, "2099-01-01T12:00:00" if future else STAMP, STAMP),
        ).lastrowid
        if eligible:
            self.conn.execute(
                "INSERT INTO date_wants(user_id,date_id,is_public,created_at,updated_at) VALUES(?,?,1,?,?)",
                (self.viewer_id, did, STAMP, STAMP),
            )
        self.conn.commit()
        return did, token

    def reminder(self, did, *, user_id=None, suffix="prompt", kind="review_prompt"):
        uid = user_id or self.viewer_id
        nid = self.conn.execute(
            "INSERT INTO notification_outbox(user_id,kind,event_key,text,send_at,created_at,updated_at) "
            "VALUES(?,?,?,'Синтетическое напоминание',?,?,?)",
            (uid, kind, f"review:date:{did}:user:{uid}:{suffix}", STAMP, STAMP, STAMP),
        ).lastrowid
        self.conn.commit()
        return nid

    def post(self, token, *, fetch=False, csrf=None, headers=None):
        request_headers = {"Referer": f"http://testserver/d/{token}"}
        if fetch:
            request_headers["X-Requested-With"] = "fetch"
        request_headers.update(headers or {})
        return self.client.post(
            f"/d/{token}/review/decline",
            data={"csrf": self.csrf if csrf is None else csrf}, headers=request_headers,
        )

    def table(self, name):
        return [dict(row) for row in self.conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]

    def state(self):
        return {name: self.table(name) for name in
                ("dates", "date_wants", "date_reviews", "review_queue", "notification_outbox")}

    def assert_visible_result(self, response, token, expected):
        self.assertEqual(response.status_code, 303, response.text)
        location = response.headers["location"]
        self.assertEqual(urlsplit(location).path, f"/d/{token}")
        self.assertIn(expected, parse_qs(urlsplit(location).query)["msg"][0])
        page = self.client.get(location)
        self.assertEqual(page.status_code, 200, page.text)
        result = re.search(r'<p\b[^>]*id="reviewResult"[^>]*>(.*?)</p>', page.text, re.S)
        self.assertIsNotNone(result, "Результат должен быть виден в HTML, а не только в URL")
        self.assertIn(expected, result.group(1))
        return page

    def test_valid_decline_persists_queue_and_cancels_reminder(self):
        did, token = self.date()
        reminder_id = self.reminder(did)
        archive_reminder_id = self.reminder(did, suffix="archived")
        other_did, _ = self.date()
        untouched_ids = [self.reminder(other_did), self.reminder(did, user_id=self.owner_id),
                         self.reminder(did, suffix="other", kind="question_received")]
        untouched_before = [dict(self.conn.execute(
            "SELECT * FROM notification_outbox WHERE id=?", (nid,),
        ).fetchone()) for nid in untouched_ids]
        event_before = self.table("dates")
        wants_before = self.table("date_wants")
        response = self.post(token)
        page = self.assert_visible_result(response, token, "Событие добавлено в «Ждут отзыва»")
        self.assertIn(f'action="/d/{token}/review"', page.text)
        queue = self.table("review_queue")
        self.assertEqual(len(queue), 1)
        self.assertEqual((queue[0]["user_id"], queue[0]["date_id"], queue[0]["reason"]),
                         (self.viewer_id, did, "declined"))
        self.assertIsNone(queue[0]["dismissed_at"])
        for nid in (reminder_id, archive_reminder_id):
            reminder = self.conn.execute("SELECT * FROM notification_outbox WHERE id=?", (nid,)).fetchone()
            self.assertIsNotNone(reminder["cancelled_at"])
            self.assertEqual(reminder["last_error"], "review_declined")
            self.assertIsNone(reminder["sent_at"])
        self.assertEqual([dict(self.conn.execute(
            "SELECT * FROM notification_outbox WHERE id=?", (nid,),
        ).fetchone()) for nid in untouched_ids], untouched_before)
        self.assertEqual(self.table("dates"), event_before)
        self.assertEqual(self.table("date_wants"), wants_before)
        self.assertEqual(self.table("date_reviews"), [])

    def test_repeated_decline_keeps_one_queue_row_and_one_cancelled_reminder(self):
        did, token = self.date()
        self.reminder(did)
        self.assertEqual(self.post(token, fetch=True).json()["ok"], True)
        first = self.state()
        response = self.post(token, fetch=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "ok": True, "message": "Событие добавлено в «Ждут отзыва»",
        })
        after = self.state()
        self.assertEqual(len(after["review_queue"]), 1)
        # Повтор обновляет только updated_at очереди; не создаёт уведомлений,
        # не переносит дату отмены и не удаляет исходное событие/план.
        self.assertGreaterEqual(after["review_queue"][0]["updated_at"],
                                first["review_queue"][0]["updated_at"])
        after["review_queue"][0]["updated_at"] = first["review_queue"][0]["updated_at"]
        self.assertEqual(after, first)

    def test_decline_reopens_dismissed_queue_and_allows_publishing_later(self):
        did, token = self.date()
        self.reminder(did)
        self.conn.execute(
            "INSERT INTO review_queue(user_id,date_id,reason,created_at,updated_at,dismissed_at) "
            "VALUES(?,?,'due',?,?,?)", (self.viewer_id, did, STAMP, STAMP, STAMP),
        )
        self.conn.commit()
        self.assertEqual(self.post(token, fetch=True).status_code, 200)
        queue = self.table("review_queue")[0]
        self.assertIsNone(queue["dismissed_at"])
        self.assertEqual(queue["created_at"], STAMP)
        self.assertEqual(queue["reason"], "declined")
        response = self.client.post(f"/d/{token}/review", data={
            "csrf": self.csrf, "rating": 4, "text": "Синтетический отзыв после паузы",
        }, headers={"Referer": f"http://testserver/d/{token}"})
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(self.table("date_reviews")[0]["rating"], 4)
        self.assertIsNotNone(self.table("review_queue")[0]["dismissed_at"])
        before = self.state()
        rejected = self.post(token, fetch=True)
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertIn("Отзыв уже создан", rejected.json()["detail"])
        self.assertEqual(self.state(), before)

    def test_invalid_and_unavailable_contexts_leave_state_unchanged(self):
        did, token = self.date()
        self.reminder(did)
        for case in ("missing", "future", "not_participant", "moderation"):
            with self.subTest(case=case):
                if case == "missing":
                    target_token, status, message = "invalid-review-context", 404, "Событие не найдено"
                else:
                    target_id, target_token = self.date(
                        eligible=case != "not_participant", future=case == "future",
                    )
                    if case == "moderation":
                        self.conn.execute("UPDATE dates SET operator_review_pending=1 WHERE id=?", (target_id,))
                        self.conn.commit()
                        status, message = 404, "Событие не найдено"
                    else:
                        status, message = 409, "Отзыв пока недоступен"
                before = self.state()
                response = self.post(target_token, fetch=True)
                self.assertEqual(response.status_code, status, response.text)
                self.assertIn(message, response.json()["detail"])
                self.assertEqual(self.state(), before)
                if status == 409:
                    self.assert_visible_result(self.post(target_token), target_token, message)
                    self.assertEqual(self.state(), before)
        self.login(92001)
        before = self.state()
        self.assert_visible_result(self.post(token), token, "Это твоё событие")
        self.assertEqual(self.state(), before)

    def test_unauthenticated_and_expired_sessions_cannot_decline(self):
        did, token = self.date()
        self.reminder(did)
        before = self.state()
        anonymous = TestClient(main.app, follow_redirects=False, raise_server_exceptions=False)
        self.addCleanup(anonymous.close)
        response = anonymous.post(f"/d/{token}/review/decline", data={"csrf": self.csrf})
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(urlsplit(response.headers["location"]).path, "/login")
        self.assertEqual(self.state(), before)
        self.conn.execute("UPDATE auth_sessions SET expires_at=0 WHERE user_id=?", (self.viewer_id,))
        self.conn.commit()
        response = self.post(token)
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(urlsplit(response.headers["location"]).path, "/login")
        self.assertEqual(self.state(), before)

    def test_csrf_and_cross_origin_rejections_are_visible_and_recoverable(self):
        did, token = self.date()
        self.reminder(did)
        before = self.state()
        for csrf in ("", "wrong-token"):
            self.assert_visible_result(self.post(token, csrf=csrf), token, "Сессия устарела")
            response = self.post(token, csrf=csrf, fetch=True)
            self.assertEqual(response.status_code, 403, response.text)
            self.assertIn("Сессия устарела", response.json()["detail"])
            self.assertEqual(self.state(), before)
        self.assert_visible_result(
            self.post(token, headers={"Origin": "https://foreign.example"}), token,
            "Запрос с другого сайта отклонён",
        )
        self.assertEqual(self.state(), before)
        self.assertEqual(self.post(token, fetch=True).status_code, 200)
        self.assertEqual(self.table("review_queue")[0]["reason"], "declined")

    def test_real_rate_limit_reports_native_and_fetch_errors_and_recovers(self):
        _, token = self.date()
        guest_limit, _, window = ratelimit.RATE_RULES["review-decline"]
        for attempt in range(guest_limit):
            response = self.post(token, fetch=True)
            self.assertEqual(response.status_code, 200, (attempt, response.text))
        blocked_id, blocked_token = self.date()
        self.reminder(blocked_id)
        before = self.state()
        response = self.post(blocked_token, fetch=True)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertIn("Слишком много действий", response.json()["detail"])
        page = self.assert_visible_result(
            self.post(blocked_token, headers={"Referer": "https://foreign.example/elsewhere"}),
            blocked_token, "Слишком много действий",
        )
        self.assertIn(f'action="/d/{blocked_token}/review/decline"', page.text)
        self.assertEqual(self.state(), before)
        self.assertTrue(any(key.startswith("review-decline:g:") for key in ratelimit._rates))
        self.assertTrue(any(key.startswith("review-decline:i:") for key in ratelimit._rates))
        # Старение реальных buckets, без замены limiter и без sleep.
        for key, timestamps in ratelimit._rates.items():
            if key.startswith("review-decline:"):
                ratelimit._rates[key] = [timestamp - window - 1 for timestamp in timestamps]
        self.assertEqual(self.post(blocked_token, fetch=True).status_code, 200)
        row = self.conn.execute(
            "SELECT reason FROM review_queue WHERE user_id=? AND date_id=?", (self.viewer_id, blocked_id),
        ).fetchone()
        self.assertEqual(row["reason"], "declined")
        self.assertIsNotNone(self.table("notification_outbox")[0]["cancelled_at"])

    def test_ip_rate_limit_blocks_a_user_with_unused_guest_budget(self):
        guest_limit, ip_limit, _ = ratelimit.RATE_RULES["review-decline"]
        spent = 0
        number = 0
        while spent < ip_limit:
            if number:
                telegram_id = 92200 + number
                self.viewer_id = self.conn.execute(
                    "INSERT INTO users(telegram_id,display_name,created_at) VALUES(?,'Участник IP теста',?)",
                    (telegram_id, STAMP),
                ).lastrowid
                self.conn.commit()
                self.login(telegram_id)
            _, token = self.date()
            for _ in range(min(guest_limit, ip_limit - spent)):
                response = self.post(token, fetch=True)
                self.assertEqual(response.status_code, 200, response.text)
                spent += 1
            number += 1
        telegram_id = 92200 + number
        self.viewer_id = self.conn.execute(
            "INSERT INTO users(telegram_id,display_name,created_at) VALUES(?,'Новый участник',?)",
            (telegram_id, STAMP),
        ).lastrowid
        self.conn.commit()
        self.login(telegram_id)
        did, token = self.date()
        self.reminder(did)
        before = self.state()
        response = self.post(token, fetch=True)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertIn("Слишком много действий", response.json()["detail"])
        self.assertEqual(self.state(), before)

    def test_expired_reminder_does_not_remove_existing_review_eligibility(self):
        did, token = self.date()
        reminder_id = self.reminder(did)
        self.conn.execute(
            "UPDATE notification_outbox SET expires_at=? WHERE id=?", (STAMP, reminder_id),
        )
        self.conn.commit()
        response = self.post(token, fetch=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.table("review_queue")[0]["reason"], "declined")
        reminder = self.table("notification_outbox")[0]
        self.assertEqual(reminder["expires_at"], STAMP)
        self.assertIsNotNone(reminder["cancelled_at"])
        self.assertIsNone(reminder["sent_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
