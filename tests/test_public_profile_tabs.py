#!/usr/bin/env python3
"""Вкладки собственного публичного профиля и их границы приватности."""

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
_DATA = tempfile.TemporaryDirectory(prefix="date4you-profile-tabs-")
os.environ.update({
    "DATA_DIR": _DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "profile-tabs.test",
    "SECRET_KEY": "profile-tabs-test-secret",
    "TG_BOT_TOKEN": "",
    "TG_BOT_USERNAME": "date4you_profile_tabs_bot",
    "TG_WEBHOOK_SECRET": "profile-tabs-hook",
    "OPERATOR_TG_IDS": "",
})

import db  # noqa: E402
import main  # noqa: E402


HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "profile-tabs-hook"}
STAMP = "2030-01-01T10:00:00"


def login(client: TestClient, telegram_id: int, name: str) -> tuple[int, str]:
    code = client.post("/auth/start").json()["code"]
    person = {
        "id": telegram_id,
        "username": f"profile_{telegram_id}",
        "first_name": name,
    }
    assert client.post(
        "/tg/webhook", headers=HEADERS,
        json={"message": {"text": f"/start {code}", "from": person}},
    ).status_code == 200
    assert client.post(
        "/tg/webhook", headers=HEADERS,
        json={"callback_query": {
            "id": f"confirm-{telegram_id}",
            "data": f"auth_confirm:{code}",
            "from": person,
        }},
    ).status_code == 200
    assert client.get(f"/auth/poll?code={code}").json()["status"] == "ok"
    profile = client.get("/admin/profile")
    csrf = re.search(r'name="csrf" value="([^"]+)"', profile.text).group(1)
    conn = db.connect()
    try:
        user_id = int(conn.execute(
            "SELECT id FROM users WHERE telegram_id=?", (telegram_id,),
        ).fetchone()[0])
    finally:
        conn.close()
    return user_id, csrf


class PublicProfileTabsTests(unittest.TestCase):
    def setUp(self):
        main._rates.clear()

    @staticmethod
    def _date(conn, owner_id: int, name: str, token: str,
              *, is_public: int) -> int:
        return int(conn.execute(
            "INSERT INTO dates(owner_id,name,share_token,is_draft,is_public,created_at) "
            "VALUES(?,?,?,0,?,?)",
            (owner_id, name, token, is_public, STAMP),
        ).lastrowid)

    def test_owner_reaches_all_tabs_while_foreign_view_stays_public(self):
        with TestClient(main.app, follow_redirects=False) as owner, \
                TestClient(main.app, follow_redirects=False) as other:
            owner_id, owner_csrf = login(owner, 991001, "Алина")
            other_id, _ = login(other, 991002, "Борис")

            conn = db.connect()
            own_public = self._date(
                conn, owner_id, "Публичное событие Алины", "alice-public",
                is_public=1,
            )
            self._date(
                conn, owner_id, "Личное событие Алины", "alice-private",
                is_public=0,
            )
            wanted_public = self._date(
                conn, other_id, "Открытая прогулка", "walk-public",
                is_public=1,
            )
            wanted_private = self._date(
                conn, other_id, "Закрытая прогулка", "walk-private",
                is_public=0,
            )
            conn.executemany(
                "INSERT INTO date_wants(user_id,date_id,is_public,created_at,updated_at) "
                "VALUES(?,?,1,?,?)",
                ((owner_id, wanted_public, STAMP, STAMP),
                 (owner_id, wanted_private, STAMP, STAMP)),
            )
            conn.executemany(
                "INSERT INTO date_reviews(user_id,date_id,rating,text,is_public,created_at,updated_at) "
                "VALUES(?,?,5,?,1,?,?)",
                ((owner_id, wanted_public, "Публичный обзор", STAMP, STAMP),
                 (owner_id, wanted_private, "Личный обзор", STAMP, STAMP)),
            )
            public_review_id = int(conn.execute(
                "SELECT id FROM date_reviews WHERE user_id=? AND text=?",
                (owner_id, "Публичный обзор"),
            ).fetchone()[0])
            conn.commit()
            conn.close()

            own_events = owner.get("/admin/profile?tab=events").text
            self.assertNotIn("Открыть публичный профиль", own_events)
            for title in ("Публичные события", "Хочу сходить", "Обзоры"):
                self.assertIn(title, own_events)
            self.assertIn("Публичное событие Алины", own_events)
            self.assertNotIn("Личное событие Алины", own_events)
            self.assertIn(f"Публичные события <b>1</b>", own_events)

            own_wants = owner.get("/admin/profile?tab=want").text
            self.assertIn("Открытая прогулка", own_wants)
            self.assertIn("Закрытая прогулка", own_wants)
            self.assertIn("Хочу сходить <b>2</b>", own_wants)

            own_reviews = owner.get("/admin/profile?tab=reviews").text
            self.assertIn("Публичный обзор", own_reviews)
            self.assertIn("Личный обзор", own_reviews)
            self.assertIn("Обзоры <b>2</b>", own_reviews)
            self.assertIn("Изменить", own_reviews)
            self.assertIn("Убрать из профиля", own_reviews)
            edited = owner.post(
                f"/u/{owner_id}/reviews/{public_review_id}/edit",
                data={"csrf": owner_csrf, "rating": "5", "text": "Публичный обзор",
                      "next": "/admin/profile?tab=reviews"},
            )
            self.assertEqual(edited.status_code, 303)
            self.assertTrue(edited.headers["location"].startswith(
                "/admin/profile?tab=reviews&msg=",
            ))

            conn = db.connect()
            conn.execute("UPDATE users SET admin_skin='romantic' WHERE id=?", (other_id,))
            conn.commit()
            conn.close()

            foreign_events = other.get(f"/u/{owner_id}?tab=events").text
            self.assertIn('<html lang="ru" data-skin="romantic">', foreign_events)
            self.assertIn("События <b>1</b>", foreign_events)
            self.assertNotIn("Публичные события", foreign_events)

            category_context = other.get(
                f"/u/{owner_id}?tab=events&skin=friends",
            ).text
            self.assertIn('<html lang="ru" data-skin="friends">', category_context)
            self.assertIn(
                f'href="/u/{owner_id}?tab=want&amp;skin=friends"',
                category_context,
            )

            foreign_wants = other.get(f"/u/{owner_id}?tab=want").text
            self.assertIn("Открытая прогулка", foreign_wants)
            self.assertNotIn("Закрытая прогулка", foreign_wants)
            self.assertNotIn("walk-private", foreign_wants)
            self.assertIn("Хочу сходить <b>1</b>", foreign_wants)

            foreign_reviews = other.get(f"/u/{owner_id}?tab=reviews").text
            self.assertIn("Публичный обзор", foreign_reviews)
            self.assertNotIn("Личный обзор", foreign_reviews)
            self.assertNotIn("walk-private", foreign_reviews)
            self.assertNotIn('class="review-menu"', foreign_reviews)
            self.assertIn("Обзоры <b>1</b>", foreign_reviews)

    def test_profile_icons_and_notification_hover_contract(self):
        profile = (APP / "templates/admin/profile.html").read_text(encoding="utf-8")
        css = (APP / "static/admin.css").read_text(encoding="utf-8")

        self.assertIn("icon='camera'", profile)
        self.assertNotIn(">📷<", profile)
        self.assertIn("social-state-add", profile)
        self.assertIn("social-state-remove", profile)
        avatar_delete = re.search(r"\.avatar-delete \{([^}]+)\}", css, re.S)
        self.assertIsNotNone(avatar_delete)
        self.assertIn("background: transparent", avatar_delete.group(1))
        self.assertNotIn("box-shadow", avatar_delete.group(1))
        self.assertIn(".avatar-delete::before", css)
        self.assertIn(".social-state-add::after", css)
        self.assertNotIn(".notif-settings-head:hover", css)
        self.assertNotIn(".notif-pref.toggle:hover", css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
