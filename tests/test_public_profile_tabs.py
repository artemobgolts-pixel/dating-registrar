#!/usr/bin/env python3
"""Вкладки собственного публичного профиля и их границы приватности."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

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
import images  # noqa: E402
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
    client.headers["X-CSRF-Token"] = csrf
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
            other_id, other_csrf = login(other, 991002, "Борис")

            conn = db.connect()
            own_public = self._date(
                conn, owner_id, "Публичное событие Алины", "alice-public",
                is_public=1,
            )
            own_private = self._date(
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
            reviewed_public = self._date(
                conn, other_id, "Открытый спектакль", "review-public",
                is_public=1,
            )
            reviewed_private = self._date(
                conn, other_id, "Закрытый спектакль", "review-private",
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
                ((owner_id, reviewed_public, "Публичный обзор", STAMP, STAMP),
                 (owner_id, reviewed_private, "Личный обзор", STAMP, STAMP)),
            )
            public_review_id = int(conn.execute(
                "SELECT id FROM date_reviews WHERE user_id=? AND text=?",
                (owner_id, "Публичный обзор"),
            ).fetchone()[0])
            private_review_id = int(conn.execute(
                "SELECT id FROM date_reviews WHERE user_id=? AND text=?",
                (owner_id, "Личный обзор"),
            ).fetchone()[0])
            avatar_filename = f"profile-{owner_id}.webp"
            images.Image.new("RGB", (128, 128), "#725c91").save(
                images.UPLOAD_DIR / avatar_filename, "WEBP",
            )
            conn.execute(
                "UPDATE users SET admin_skin='romantic',avatar_path=? WHERE id=?",
                (avatar_filename, owner_id),
            )
            conn.commit()
            conn.close()

            own_events = owner.get("/admin/profile?tab=events").text
            self.assertIn('class="pub-dates profile-tab-events', own_events)
            self.assertNotIn("Открыть публичный профиль", own_events)
            self.assertIn("<h2>Коллекция событий</h2>", own_events)
            self.assertNotIn(
                "Так твои события, планы и обзоры собраны в профиле.", own_events,
            )
            for title in ("Публичные события", "Хочу сходить", "Обзоры"):
                self.assertIn(title, own_events)
            self.assertIn("Публичное событие Алины", own_events)
            self.assertNotIn("Личное событие Алины", own_events)
            self.assertIn(f"Публичные события <b>1</b>", own_events)
            self.assertIn(
                f'href="/admin/dates/{own_public}/edit?return_to=', own_events,
            )
            self.assertIn(
                f'data-profile-editor="/admin/dates/{own_public}/edit?return_to=',
                own_events,
            )
            self.assertRegex(own_events, r'<html lang="ru"\s+data-skin="romantic">')
            self.assertRegex(
                owner.get(f"/admin/dates/{own_public}/edit").text,
                r'<html lang="ru"\s+data-skin="romantic">',
            )
            self.assertNotIn(
                f'data-profile-widget="/u/{owner_id}/date/{own_public}/widget?skin=romantic"',
                own_events,
            )
            self.assertIn('id="profileEventDlg"', own_events)

            own_wants = owner.get("/admin/profile?tab=want").text
            self.assertIn('class="pub-dates profile-tab-want', own_wants)
            self.assertIn("Открытая прогулка", own_wants)
            self.assertIn("Закрытая прогулка", own_wants)
            self.assertIn("Хочу сходить <b>2</b>", own_wants)

            own_reviews = owner.get("/admin/profile?tab=reviews").text
            self.assertIn('class="pub-dates profile-tab-reviews', own_reviews)
            self.assertIn("Публичный обзор", own_reviews)
            self.assertIn("Личный обзор", own_reviews)
            self.assertIn("Обзоры <b>2</b>", own_reviews)
            self.assertIn("Изменить", own_reviews)
            self.assertIn("Удалить", own_reviews)
            self.assertNotIn("Убрать из профиля", own_reviews)
            self.assertIn(
                f'action="/u/{owner_id}/reviews/{public_review_id}/hide"',
                own_reviews,
            )
            self.assertIn(
                f'action="/u/{owner_id}/reviews/{private_review_id}/hide"',
                own_reviews,
            )
            self.assertIn(
                'data-confirm="Удалить обзор? Событие появится во вкладке «Ждут отзыва»."',
                own_reviews,
            )
            self.assertIn('class="review-menu-dots" viewBox="0 0 20 6"', own_reviews)
            self.assertIn(
                f'data-profile-widget="/u/{owner_id}/reviews/{public_review_id}/widget',
                own_reviews,
            )
            self.assertNotIn(
                f'data-profile-editor="/admin/dates/{reviewed_public}/edit"',
                own_reviews,
            )

            review_widget = owner.get(
                f"/u/{owner_id}/reviews/{public_review_id}/widget"
                "?next=/admin/profile%3Ftab%3Dreviews",
            )
            self.assertEqual(review_widget.status_code, 200)
            self.assertIn("Сохранить обзор", review_widget.text)
            self.assertIn('class="profile-rating-stars"', review_widget.text)
            self.assertIn('class="profile-review-actions', review_widget.text)
            self.assertIn(">Поделиться</button>", review_widget.text)
            self.assertNotIn('class="review-stars"', review_widget.text)
            self.assertNotIn('class="profile-review-copy', review_widget.text)
            self.assertIn(
                f"https://profile-tabs.test/d/review-public/review/{public_review_id}",
                review_widget.text,
            )
            for event_action in (
                    "Добавить себе", "Сохранить к себе", "Спросить",
                    "Предложить дату"):
                self.assertNotIn(event_action, review_widget.text)

            with TestClient(main.app, follow_redirects=False) as anonymous:
                shared_review = anonymous.get(
                    f"/d/review-public/review/{public_review_id}",
                )
                anonymous_profile = anonymous.get(f"/u/{owner_id}?tab=events")
                anonymous_avatar = anonymous.get(f"/u/{owner_id}/avatar?w=64")
                anonymous_wants = anonymous.get(f"/u/{owner_id}?tab=want")
                anonymous_reviews = anonymous.get(f"/u/{owner_id}?tab=reviews")
                anonymous_widget = anonymous.get(
                    f"/u/{owner_id}/reviews/{public_review_id}/widget",
                )
                anonymous_event_widget = anonymous.get(
                    f"/u/{owner_id}/date/{own_public}/widget",
                )
                anonymous_private_event = anonymous.get(
                    f"/u/{owner_id}/date/{own_private}/widget",
                )
                anonymous_private_review = anonymous.get(
                    f"/u/{owner_id}/reviews/{private_review_id}/widget",
                )
                anonymous_edit = anonymous.post(
                    f"/u/{owner_id}/reviews/{public_review_id}/edit",
                    data={"csrf": "", "rating": "5", "text": "Нельзя",
                          "next": f"/u/{owner_id}?tab=reviews"},
                )
                anonymous_hide = anonymous.post(
                    f"/u/{owner_id}/reviews/{public_review_id}/hide",
                    data={"csrf": "", "next": f"/u/{owner_id}?tab=reviews"},
                )
            self.assertEqual(shared_review.status_code, 200)
            self.assertEqual(anonymous_profile.status_code, 200)
            self.assertEqual(anonymous_avatar.status_code, 200)
            self.assertEqual(anonymous_avatar.headers["content-type"], "image/webp")
            self.assertEqual(
                anonymous_avatar.headers["cache-control"], "public, max-age=300",
            )
            self.assertIn("Публичное событие Алины", anonymous_profile.text)
            self.assertNotIn("Личное событие Алины", anonymous_profile.text)
            self.assertIn(">Войти</a>", anonymous_profile.text)
            self.assertNotIn("Редактировать профиль", anonymous_profile.text)
            self.assertIn("Открытая прогулка", anonymous_wants.text)
            self.assertNotIn("Закрытая прогулка", anonymous_wants.text)
            self.assertIn("Публичный обзор", anonymous_reviews.text)
            self.assertNotIn("Личный обзор", anonymous_reviews.text)
            self.assertEqual(anonymous_widget.status_code, 200)
            self.assertIn("Публичный обзор", anonymous_widget.text)
            self.assertNotIn("Сохранить обзор", anonymous_widget.text)
            self.assertEqual(anonymous_event_widget.status_code, 200)
            self.assertIn(
                "Войти или зарегистрироваться, чтобы добавить в коллекцию",
                anonymous_event_widget.text,
            )
            self.assertNotIn('data-add="', anonymous_event_widget.text)
            self.assertEqual(anonymous_private_event.status_code, 404)
            self.assertEqual(anonymous_private_review.status_code, 404)
            for response in (anonymous_edit, anonymous_hide):
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/login")
            self.assertIn("Публичный обзор", shared_review.text)  # текст самого отзыва
            self.assertNotIn(
                '<span class="review-share-eyebrow">Публичный обзор</span>',
                shared_review.text,
            )
            self.assertIn('<span class="review-share-eyebrow">Обзор</span>', shared_review.text)
            self.assertNotIn("Сохранить обзор", shared_review.text)
            self.assertIn('class="review-stars"', shared_review.text)
            self.assertIn('class="profile-review-copy"', shared_review.text)
            self.assertIn('data-community-share', shared_review.text)
            self.assertIn(
                f'data-share-url="https://profile-tabs.test/d/review-public/review/{public_review_id}"',
                shared_review.text,
            )
            self.assertIn('<html lang="ru" data-skin="friends">', shared_review.text)
            self.assertIn("Понравился обзор?", shared_review.text)
            self.assertIn("Добавить событие в коллекцию", shared_review.text)
            self.assertNotIn("Войти и добавить", shared_review.text)
            self.assertNotIn("← К событию", shared_review.text)
            signed_shared_review = other.get(
                f"/d/review-public/review/{public_review_id}",
            )
            self.assertIn(
                '<html lang="ru" data-skin="friends">',
                signed_shared_review.text,
            )
            reported = other.post(
                "/d/review-public/report",
                data={"csrf": other_csrf, "target_type": "date",
                      "target_id": str(reviewed_public), "reason": "Проверить обзор"},
            )
            self.assertEqual(reported.status_code, 200, reported.text)
            self.assertTrue(reported.json()["ok"])
            duplicate_report = other.post(
                "/d/review-public/report",
                data={"csrf": other_csrf, "target_type": "date",
                      "target_id": str(reviewed_public), "reason": "Повтор"},
            )
            self.assertEqual(duplicate_report.status_code, 200)
            conn = db.connect()
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM reports WHERE target_type='date' "
                "AND target_id=? AND reporter=?",
                (reviewed_public, f"u{other_id}"),
            ).fetchone()[0], 1)
            conn.close()
            self.assertEqual(
                owner.get(
                    f"/d/review-private/review/{private_review_id}",
                ).status_code,
                404,
            )
            self.assertEqual(
                owner.get(
                    f"/d/review-private/review/{public_review_id}",
                ).status_code,
                404,
            )
            edited_json = owner.post(
                f"/u/{owner_id}/reviews/{public_review_id}/edit",
                data={"csrf": owner_csrf, "rating": "4", "text": "Обзор без перехода",
                      "next": "/admin/profile?tab=reviews"},
                headers={"Accept": "application/json"},
            )
            self.assertEqual(edited_json.status_code, 200, edited_json.text)
            self.assertEqual(edited_json.json(), {
                "ok": True,
                "message": "Обзор обновлён",
                "rating": 4,
                "text": "Обзор без перехода",
                "is_public": True,
            })
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

            romantic_shared_review = other.get(
                f"/d/review-public/review/{public_review_id}",
            )
            self.assertIn(
                '<html lang="ru" data-skin="romantic">',
                romantic_shared_review.text,
            )

            foreign_events = other.get(f"/u/{owner_id}?tab=events").text
            self.assertIn('<html lang="ru" data-skin="romantic">', foreign_events)
            self.assertIn("Коллекция событий <b>1</b>", foreign_events)
            self.assertNotIn("Публичные события", foreign_events)
            self.assertIn(
                f'data-profile-widget="/u/{owner_id}/date/{own_public}/widget?skin=romantic"',
                foreign_events,
            )
            self.assertRegex(foreign_events, r'data-csrf="[^"]+"')

            category_context = other.get(
                f"/u/{owner_id}?tab=events&skin=friends",
            ).text
            self.assertIn('<html lang="ru" data-skin="friends">', category_context)
            self.assertIn(
                f'href="/u/{owner_id}?tab=want&amp;skin=friends#profileCollection"',
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
            self.assertNotIn("review-private", foreign_reviews)
            self.assertNotIn('class="review-menu"', foreign_reviews)
            self.assertIn("Обзоры <b>1</b>", foreign_reviews)
            self.assertRegex(
                foreign_reviews,
                rf'class="review-card"[^>]+role="link"[^>]+data-profile-widget="/u/{owner_id}/reviews/{public_review_id}/widget',
            )
            foreign_review_widget = other.get(
                f"/u/{owner_id}/reviews/{public_review_id}/widget",
            )
            self.assertEqual(foreign_review_widget.status_code, 200)
            self.assertNotIn("Сохранить обзор", foreign_review_widget.text)
            self.assertEqual(
                other.get(
                    f"/u/{owner_id}/reviews/{private_review_id}/widget",
                ).status_code,
                404,
            )

            # Виджет разрешает ровно те отношения, которые могут появиться в
            # профиле; приватную встречу нельзя раскрыть перебором id.
            signed_event_widget = other.get(
                f"/u/{owner_id}/date/{own_public}/widget",
            )
            self.assertEqual(signed_event_widget.status_code, 200)
            self.assertIn(
                f'data-add="/d/alice-public/add"', signed_event_widget.text,
            )
            self.assertNotIn("Войти или зарегистрироваться", signed_event_widget.text)
            self.assertEqual(
                other.get(f"/u/{owner_id}/date/{own_private}/widget").status_code,
                404,
            )
            self.assertEqual(
                owner.get(f"/u/{owner_id}/date/{wanted_private}/widget").status_code,
                200,
            )
            self.assertEqual(
                other.get(f"/u/{owner_id}/date/{wanted_private}/widget").status_code,
                404,
            )
            self.assertEqual(
                owner.get(f"/u/{owner_id}/date/{reviewed_private}/widget").status_code,
                200,
            )
            reviewed_widget = owner.get(
                f"/u/{owner_id}/date/{reviewed_public}/widget",
            )
            self.assertEqual(reviewed_widget.status_code, 200)
            self.assertNotIn("cwid-want", reviewed_widget.text)
            self.assertEqual(
                other.get(f"/u/{owner_id}/date/{reviewed_private}/widget").status_code,
                404,
            )

            # После сохранения редактор сохраняет безопасный возврат в ту же
            # вкладку профиля, а не сбрасывает пользователя на /admin/dates.
            editor_url = (
                f"/admin/dates/{own_public}/edit?return_to="
                "%2Fadmin%2Fprofile%3Ftab%3Devents%26page%3D1"
                "%23profileCollection"
            )
            editor = owner.get(editor_url)
            self.assertIn(
                'href="/admin/profile?tab=events#profileCollection"',
                editor.text,
            )
            saved = owner.post(editor_url, data={
                "csrf": owner_csrf, "name": "Публичное событие Алины",
                "place": "", "starts_at": "", "ends_at": "",
                "links": "", "comment": "", "capacity": "1",
                "is_public": "1",
            })
            self.assertEqual(saved.status_code, 303)
            self.assertIn(
                "/admin/profile?tab=events#profileCollection",
                unquote(saved.headers["location"]),
            )

            deleted = owner.post(
                f"/u/{owner_id}/reviews/{private_review_id}/hide",
                data={"csrf": owner_csrf, "next": "/admin/profile?tab=reviews"},
            )
            self.assertEqual(deleted.status_code, 303)
            conn = db.connect()
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM date_reviews WHERE id=?", (private_review_id,),
            ).fetchone())
            queued = conn.execute(
                "SELECT reason FROM review_queue WHERE user_id=? AND date_id=?",
                (owner_id, reviewed_private),
            ).fetchone()
            self.assertIsNotNone(queued)
            self.assertEqual(queued["reason"], "review_deleted")
            conn.close()

    def test_profile_pagination_uses_anchored_arrow_controls(self):
        with TestClient(main.app, follow_redirects=False) as owner, \
                TestClient(main.app, follow_redirects=False) as viewer:
            owner_id, _ = login(owner, 991011, "Вера")
            login(viewer, 991012, "Глеб")
            conn = db.connect()
            for index in range(13):
                self._date(
                    conn, owner_id, f"Страница {index}", f"paged-{index}",
                    is_public=1,
                )
            conn.commit()
            conn.close()

            first = viewer.get(f"/u/{owner_id}?tab=events").text
            second = viewer.get(f"/u/{owner_id}?tab=events&page=2").text
            self.assertEqual(first.count('class="pub-card"'), 12)
            self.assertEqual(second.count('class="pub-card"'), 1)
            self.assertIn(
                f'href="/u/{owner_id}?tab=events&page=2#profileCollection"',
                first,
            )
            self.assertIn('aria-label="Следующая страница"', first)
            self.assertIn('aria-label="Предыдущая страница"', second)
            self.assertNotIn("Ещё →", first)
            self.assertNotIn("← Новее", second)
            self.assertEqual(first.count('class="pub-page-arrow-icon"'), 2)
            self.assertIn('d="m9.5 5.5 6.5 6.5-6.5 6.5"', first)
            self.assertIn('src="/static/profile.js?', first)
            profile_js = (APP / "static/profile.js").read_text(encoding="utf-8")
            self.assertIn("section._d4yWidgetReady", profile_js)
            self.assertIn("new AbortController()", profile_js)
            self.assertIn("widgetRequest === controller", profile_js)
            self.assertIn("!response.ok || response.redirected", profile_js)
            self.assertIn("window.d4yProfileSave", profile_js)
            admin_js = (APP / "static/admin.js").read_text(encoding="utf-8")
            self.assertIn("keepalive: true", admin_js)
            self.assertIn("window.d4yProfileSave = pending", admin_js)
            self.assertIn('(pointer: coarse) and (max-width: 900px)', profile_js)

    def test_profile_icons_and_notification_hover_contract(self):
        profile = (APP / "templates/admin/profile.html").read_text(encoding="utf-8")
        css = (APP / "static/admin.css").read_text(encoding="utf-8")

        self.assertIn("icon='camera'", profile)
        self.assertNotIn(">📷<", profile)
        self.assertIn("social-state-add", profile)
        self.assertIn("social-state-remove", profile)
        avatar_delete = re.search(r"\.avatar-delete \{([^}]+)\}", css, re.S)
        self.assertIsNotNone(avatar_delete)
        self.assertIn("border-radius: 50%", avatar_delete.group(1))
        self.assertIn("linear-gradient", avatar_delete.group(1))
        self.assertIn("color: #fff", avatar_delete.group(1))
        self.assertIn("width: 34px", avatar_delete.group(1))
        self.assertIn("height: 34px", avatar_delete.group(1))
        self.assertIn("right: 2px", avatar_delete.group(1))
        self.assertIn("top: 2px", avatar_delete.group(1))
        self.assertIn("box-shadow", avatar_delete.group(1))
        self.assertIn(".avatar-delete::before", css)
        self.assertIn(".avatar-delete:focus-visible", css)
        self.assertIn(".social-state-add::after", css)
        profile_css = (APP / "static/profile.css").read_text(encoding="utf-8")
        self.assertIn(".review-menu-dots", profile_css)
        self.assertIn("place-items: center", profile_css)
        self.assertNotIn(".notif-settings-head:hover", css)
        self.assertNotIn(".notif-pref.toggle:hover", css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
