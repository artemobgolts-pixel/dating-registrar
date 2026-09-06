#!/usr/bin/env python3
"""End-to-end regressions for per-user mandatory content review."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from starlette.testclient import TestClient


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-suspicious-review-")
os.environ.update({
    "DATA_DIR": _DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "moderation.test",
    "SECRET_KEY": "suspicious-review-test-secret",
    "TG_BOT_TOKEN": "",
    "TG_CHAT_ID": "",
    "TG_BOT_USERNAME": "date4you_moderation_test_bot",
    "TG_WEBHOOK_SECRET": "moderation-hook-secret",
    "OPERATOR_TG_IDS": "996001",
})

import db  # noqa: E402
import auth_routes  # noqa: E402
import main  # noqa: E402
import social_events  # noqa: E402
import users  # noqa: E402


WEBHOOK_HEADERS = {
    "X-Telegram-Bot-Api-Secret-Token": "moderation-hook-secret",
}


def login(client: TestClient, telegram_id: int, name: str) -> tuple[int, str]:
    code = client.post("/auth/start").json()["code"]
    person = {
        "id": telegram_id,
        "username": f"moderation_{telegram_id}",
        "first_name": name,
    }
    assert client.post(
        "/tg/webhook", headers=WEBHOOK_HEADERS,
        json={"message": {"text": f"/start {code}", "from": person}},
    ).status_code == 200
    assert client.post(
        "/tg/webhook", headers=WEBHOOK_HEADERS,
        json={"callback_query": {
            "id": f"confirm-moderation-{telegram_id}",
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


def category_payload(name: str) -> dict[str, str]:
    return {
        "name": name,
        "choice_mode": "multiple",
        "voting_deadline": "2034-01-01T10:00",
    }


class SuspiciousModerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.operator = TestClient(main.app, follow_redirects=False).__enter__()
        cls.author = TestClient(main.app, follow_redirects=False).__enter__()
        cls.owner = TestClient(main.app, follow_redirects=False).__enter__()
        cls.public = TestClient(main.app, follow_redirects=False).__enter__()
        cls.operator_id, cls.operator_csrf = login(
            cls.operator, 996001, "Администратор",
        )
        cls.author_id, cls.author_csrf = login(
            cls.author, 996002, "Автор под проверкой",
        )
        cls.owner_id, cls.owner_csrf = login(
            cls.owner, 996003, "Обычный владелец",
        )

    @classmethod
    def tearDownClass(cls):
        cls.public.__exit__(None, None, None)
        cls.owner.__exit__(None, None, None)
        cls.author.__exit__(None, None, None)
        cls.operator.__exit__(None, None, None)
        _DATA.cleanup()

    def _post(self, client: TestClient, path: str, csrf: str,
              data: dict | None = None):
        return client.post(path, data={**(data or {}), "csrf": csrf})

    def setUp(self):
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE users SET is_suspicious=0, is_operator=0 WHERE id=?",
                (self.author_id,),
            )
            conn.commit()
        finally:
            conn.close()
        main._rates.clear()

    def _one(self, sql: str, args=()):
        conn = db.connect()
        try:
            return conn.execute(sql, args).fetchone()
        finally:
            conn.close()

    def test_new_content_is_hidden_until_operator_decides(self):
        # Контент, созданный до пометки, не должен измениться задним числом.
        self.assertEqual(self._post(
            self.author, "/admin/categories/create", self.author_csrf,
            category_payload("Существующая подборка"),
        ).status_code, 303)
        existing = self._one(
            "SELECT * FROM categories WHERE name='Существующая подборка'",
        )
        self.assertEqual(existing["operator_review_pending"], 0)

        marked = self._post(
            self.operator,
            f"/operator/users/{self.author_id}/suspicious",
            self.operator_csrf,
            {"enabled": "1"},
        )
        self.assertEqual(marked.status_code, 303)
        self.assertEqual(self._one(
            "SELECT is_suspicious FROM users WHERE id=?", (self.author_id,),
        )[0], 1)

        # И подборка, и самостоятельное событие получают серверный hard hold.
        self.assertEqual(self._post(
            self.author, "/admin/categories/create", self.author_csrf,
            category_payload("Скрытая подборка"),
        ).status_code, 303)
        held_category = self._one(
            "SELECT * FROM categories WHERE name='Скрытая подборка'",
        )
        self.assertEqual(held_category["operator_review_pending"], 1)
        self.assertEqual(held_category["is_reviewed"], 0)
        self.assertEqual(
            self.public.get(f"/c/{held_category['link_token']}").status_code,
            404,
        )
        dashboard = self.author.get("/admin/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn(held_category["link_token"], dashboard.text)

        main._rates.clear()
        self.assertEqual(self._post(
            self.author, "/admin/dates/new", self.author_csrf,
            {
                "name": "Скрытое событие",
                "starts_at": "2034-02-01T18:00",
                "categories": str(existing["id"]),
                "is_public": "1",
            },
        ).status_code, 303)
        held_date = self._one(
            "SELECT * FROM dates WHERE name='Скрытое событие'",
        )
        self.assertEqual(held_date["operator_review_pending"], 1)
        self.assertEqual(held_date["is_draft"], 0)
        self.assertEqual(
            self.public.get(f"/d/{held_date['share_token']}").status_code,
            404,
        )
        visible_category = self.public.get(f"/c/{existing['link_token']}")
        self.assertEqual(visible_category.status_code, 200)
        self.assertNotIn("Скрытое событие", visible_category.text)

        queue = self.operator.get("/operator/review")
        self.assertEqual(queue.status_code, 200)
        self.assertIn("Скрытая подборка", queue.text)
        self.assertIn("Скрытое событие", queue.text)
        filtered = self.operator.get("/operator/users?risk=suspicious")
        self.assertIn("Автор под проверкой", filtered.text)
        self.assertNotIn("Обычный владелец", filtered.text)

        # Одобрение снимает только платформенный hold и открывает URL.
        approved_date = self._post(
            self.operator,
            f"/operator/review/date/{held_date['id']}/approve",
            self.operator_csrf,
        )
        self.assertEqual(approved_date.status_code, 303)
        self.assertEqual(self.public.get(
            f"/d/{held_date['share_token']}",
        ).status_code, 200)
        self.assertIn(
            "Скрытое событие",
            self.public.get(f"/c/{existing['link_token']}").text,
        )
        approved_category = self._post(
            self.operator,
            f"/operator/review/category/{held_category['id']}/approve",
            self.operator_csrf,
        )
        self.assertEqual(approved_category.status_code, 303)
        self.assertEqual(self.public.get(
            f"/c/{held_category['link_token']}",
        ).status_code, 200)

        # Снятие пометки влияет только на будущие INSERT и не выпускает очередь.
        self.assertEqual(self._post(
            self.author, "/admin/categories/create", self.author_csrf,
            category_payload("Останется на проверке"),
        ).status_code, 303)
        still_held = self._one(
            "SELECT * FROM categories WHERE name='Останется на проверке'",
        )
        self.assertEqual(self._post(
            self.operator,
            f"/operator/users/{self.author_id}/suspicious",
            self.operator_csrf,
            {"enabled": "0"},
        ).status_code, 303)
        self.assertEqual(self._one(
            "SELECT operator_review_pending FROM categories WHERE id=?",
            (still_held["id"],),
        )[0], 1)
        self.assertEqual(self._post(
            self.operator,
            f"/operator/review/category/{still_held['id']}/reject",
            self.operator_csrf,
        ).status_code, 303)
        self.assertIsNone(self._one(
            "SELECT 1 FROM categories WHERE id=?", (still_held["id"],),
        ))
        self.assertEqual(self._post(
            self.author, "/admin/categories/create", self.author_csrf,
            category_payload("Обычная новая подборка"),
        ).status_code, 303)
        self.assertEqual(self._one(
            "SELECT operator_review_pending FROM categories "
            "WHERE name='Обычная новая подборка'",
        )[0], 0)

    def test_hard_hold_does_not_change_ordinary_draft_share_semantics(self):
        conn = db.connect()
        try:
            conn.executemany(
                "INSERT INTO dates(owner_id,name,share_token,is_draft,"
                "operator_review_pending,created_at) VALUES(?,?,?,1,?,?)",
                (
                    (self.author_id, "Обычный черновик", "ordinary-draft", 0,
                     "2030-01-01T00:00:00"),
                    (self.author_id, "Черновик на проверке", "held-draft", 1,
                     "2030-01-01T00:00:00"),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        ordinary = self.public.get("/d/ordinary-draft")
        self.assertEqual(ordinary.status_code, 200)
        self.assertIn("Обычный черновик", ordinary.text)
        self.assertEqual(self.public.get("/d/held-draft").status_code, 404)

    def test_trusted_telegram_link_clears_suspicious_invariant(self):
        trusted_telegram_id = 996099
        conn = db.connect()
        users.OPERATOR_TG_IDS.add(trusted_telegram_id)
        try:
            user_id = int(conn.execute(
                "INSERT INTO users(display_name,is_suspicious,created_at) "
                "VALUES('OAuth администратор',1,?)",
                ("2030-01-01T00:00:00",),
            ).lastrowid)
            linked, error = auth_routes._link_telegram(
                conn, user_id, trusted_telegram_id, "trusted_operator",
            )
            self.assertTrue(linked)
            self.assertIsNone(error)
            state = conn.execute(
                "SELECT is_operator,is_suspicious FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            self.assertEqual(tuple(state), (1, 0))
        finally:
            users.OPERATOR_TG_IDS.discard(trusted_telegram_id)
            conn.rollback()
            conn.close()

    def test_guest_proposal_uses_actor_risk_not_collection_owner(self):
        self.assertEqual(self._post(
            self.owner, "/admin/categories/create", self.owner_csrf,
            category_payload("Подборка обычного владельца"),
        ).status_code, 303)
        category = self._one(
            "SELECT * FROM categories WHERE name='Подборка обычного владельца'",
        )
        self.assertEqual(self._post(
            self.operator,
            f"/admin/categories/{category['id']}/moderation",
            self.operator_csrf,
        ).status_code, 303)
        self.assertEqual(self._post(
            self.operator,
            f"/operator/users/{self.author_id}/suspicious",
            self.operator_csrf,
            {"enabled": "1"},
        ).status_code, 303)

        main._rates.clear()
        response = self.author.post(
            f"/c/{category['link_token']}/propose",
            data={"name": "Предложение подозрительного автора"},
            headers={"X-CSRF-Token": self.author_csrf,
                     "Origin": "https://moderation.test"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["moderated"])
        self.assertTrue(response.json()["operator_review_pending"])
        proposal = self._one(
            "SELECT * FROM dates WHERE name='Предложение подозрительного автора'",
        )
        self.assertEqual(proposal["owner_id"], self.owner_id)
        self.assertEqual(proposal["proposed_by"], self.author_id)
        self.assertEqual(proposal["operator_review_pending"], 1)
        self.assertEqual(proposal["is_draft"], 1)
        self.assertNotIn(
            "Предложение подозрительного автора",
            self.public.get(f"/c/{category['link_token']}").text,
        )
        owner_approved = self._post(
            self.owner,
            f"/admin/dates/{proposal['id']}/publish",
            self.owner_csrf,
            {"next": "/admin/dates?view=proposed"},
        )
        self.assertEqual(owner_approved.status_code, 303)
        after_owner = self._one(
            "SELECT is_draft,operator_review_pending FROM dates WHERE id=?",
            (proposal["id"],),
        )
        self.assertEqual(tuple(after_owner), (0, 1))
        self.assertNotIn(
            "Предложение подозрительного автора",
            self.public.get(f"/c/{category['link_token']}").text,
        )

        # Риск несовместим с ролью администратора и не снимается двойным POST.
        refused_role = self._post(
            self.operator,
            f"/operator/users/{self.author_id}/operator",
            self.operator_csrf,
        )
        self.assertEqual(refused_role.status_code, 303)
        self.assertEqual(self._one(
            "SELECT is_operator FROM users WHERE id=?", (self.author_id,),
        )[0], 0)
        refused_self = self._post(
            self.operator,
            f"/operator/users/{self.operator_id}/suspicious",
            self.operator_csrf,
            {"enabled": "1"},
        )
        self.assertEqual(refused_self.status_code, 303)
        self.assertEqual(self._one(
            "SELECT is_suspicious FROM users WHERE id=?", (self.operator_id,),
        )[0], 0)
        rejected = self._post(
            self.operator,
            f"/operator/review/date/{proposal['id']}/reject",
            self.operator_csrf,
        )
        self.assertEqual(rejected.status_code, 303)
        self.assertIsNone(self._one(
            "SELECT 1 FROM dates WHERE id=?", (proposal["id"],),
        ))

    def test_pending_content_is_inert_for_review_lifecycle(self):
        """Hard hold не должен выдавать отзыв или менять дедлайн живого события."""
        conn = db.connect()
        try:
            visible_date = int(conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,created_at) "
                "VALUES(?,?,?,?)",
                (self.owner_id, "Живое событие для review", "visible-review-date",
                 "2030-01-01T00:00:00"),
            ).lastrowid)
            pending_category = int(conn.execute(
                "INSERT INTO categories(owner_id,name,link_token,choice_mode,"
                "voting_deadline,voting_status,operator_review_pending,created_at) "
                "VALUES(?,?,?,?,?,'open',1,?)",
                (self.owner_id, "Невидимая будущая подборка", "pending-review-cat",
                 "multiple", "2040-01-01T10:00:00", "2030-01-01T00:00:00"),
            ).lastrowid)
            conn.execute(
                "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
                (visible_date, pending_category),
            )
            held_date = int(conn.execute(
                "INSERT INTO dates(owner_id,name,starts_at,ends_at,share_token,"
                "operator_review_pending,created_at) VALUES(?,?,?,?,?,1,?)",
                (self.owner_id, "Удержанное событие для review",
                 "2030-01-01T18:00:00", "2030-01-01T20:00:00",
                 "held-review-date", "2030-01-01T00:00:00"),
            ).lastrowid)
            conn.executemany(
                "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
                "VALUES(?,?,?,?)",
                (
                    (self.author_id, visible_date, "2030-01-01T00:00:00",
                     "2030-01-01T00:00:00"),
                    (self.author_id, held_date, "2030-01-01T00:00:00",
                     "2030-01-01T00:00:00"),
                ),
            )
            conn.commit()

            current = datetime(2035, 1, 1, 12, 0, 0)
            self.assertTrue(social_events.review_available(
                conn, visible_date, self.author_id, now=current,
            ))
            self.assertFalse(social_events.review_available(
                conn, held_date, self.author_id, now=current,
            ))
            waiting = {
                row["date_id"] for row in social_events.review_waiting_rows(
                    conn, self.author_id, now=current,
                )
            }
            self.assertNotIn(held_date, waiting)
        finally:
            conn.close()

    def test_approval_detaches_held_event_from_frozen_vote(self):
        conn = db.connect()
        try:
            category_id = int(conn.execute(
                "INSERT INTO categories(owner_id,name,link_token,choice_mode,"
                "voting_deadline,voting_status,created_at) "
                "VALUES(?,?,?,?,?,'open',?)",
                (self.owner_id, "Завершённая подборка", "frozen-held-category",
                 "multiple", "2030-01-01T10:00:00",
                 "2030-01-01T00:00:00"),
            ).lastrowid)
            date_id = int(conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,is_draft,"
                "operator_review_pending,created_at) VALUES(?,?,?,0,1,?)",
                (self.author_id, "Опоздавшее событие", "late-held-event",
                 "2030-01-01T00:00:00"),
            ).lastrowid)
            # Эмулируем реальный порядок: pending-кандидат был прикреплён, пока
            # голосование ещё было открыто, затем фоновый worker его закрыл.
            conn.execute(
                "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
                (date_id, category_id),
            )
            conn.execute(
                "UPDATE categories SET voting_status='no_winner', closed_at=? "
                "WHERE id=?",
                ("2030-01-01T10:01:00", category_id),
            )
            conn.commit()
        finally:
            conn.close()

        approved = self._post(
            self.operator,
            f"/operator/review/date/{date_id}/approve",
            self.operator_csrf,
        )
        self.assertEqual(approved.status_code, 303)
        self.assertEqual(self._one(
            "SELECT operator_review_pending FROM dates WHERE id=?", (date_id,),
        )[0], 0)
        self.assertIsNone(self._one(
            "SELECT 1 FROM date_categories WHERE date_id=? AND category_id=?",
            (date_id, category_id),
        ))
        self.assertEqual(self.public.get("/d/late-held-event").status_code, 200)


if __name__ == "__main__":
    unittest.main()
