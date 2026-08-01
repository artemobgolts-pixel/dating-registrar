#!/usr/bin/env python3
"""Копирование категорий/событий и регрессии карточной ленты/фокуса."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from PIL import Image
from starlette.testclient import TestClient


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-copy-actions-")
os.environ.update({
    "DATA_DIR": _DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "copy.test",
    "SECRET_KEY": "test-secret-key-for-copy-actions",
    "TG_BOT_TOKEN": "",
    "TG_BOT_USERNAME": "date4you_copy_test_bot",
    "TG_WEBHOOK_SECRET": "copy-hook-secret",
    "OPERATOR_TG_IDS": "",
})

import db  # noqa: E402
import main  # noqa: E402


HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "copy-hook-secret"}
NOW = "2030-01-01T10:00:00"


def login(client: TestClient, telegram_id: int) -> str:
    start = client.post("/auth/start")
    code = start.json()["code"]
    person = {"id": telegram_id, "username": f"u{telegram_id}", "first_name": "Тест"}
    response = client.post(
        "/tg/webhook", headers=HEADERS,
        json={"message": {"text": f"/start {code}", "from": person}},
    )
    assert response.status_code == 200
    response = client.post(
        "/tg/webhook", headers=HEADERS,
        json={"callback_query": {
            "id": f"confirm-{telegram_id}",
            "data": f"auth_confirm:{code}",
            "from": person,
        }},
    )
    assert response.status_code == 200
    assert client.get(f"/auth/poll?code={code}").json()["status"] == "ok"
    page = client.get("/admin/categories")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


class CopyActionTests(unittest.TestCase):
    def setUp(self):
        main._rates.clear()

    def _seed_source(self, telegram_id: int) -> tuple[int, int, set[str]]:
        conn = db.connect()
        uid = int(conn.execute(
            "SELECT id FROM users WHERE telegram_id=?", (telegram_id,),
        ).fetchone()[0])
        upload_dir = main.images.UPLOAD_DIR
        upload_dir.mkdir(parents=True, exist_ok=True)
        og_name = f"copy-{telegram_id}-og.webp"
        photo_name = f"copy-{telegram_id}-photo.webp"
        video_name = f"copy-{telegram_id}-video.mp4"
        Image.new("RGB", (320, 180), (160, 70, 90)).save(upload_dir / og_name, "WEBP")
        Image.new("RGB", (320, 240), (40, 90, 130)).save(upload_dir / photo_name, "WEBP")
        (upload_dir / video_name).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 48)

        category_id = int(conn.execute(
            "INSERT INTO categories("
            "owner_id,name,category_skin,link_token,link_enabled,description,"
            "og_title,og_desc,og_image,og_focus,choice_mode,voting_deadline,"
            "voting_status,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, "Маршрут", "romantic", f"cat-{telegram_id}", 1,
             "Описание", "Заголовок", "Подпись", og_name, "30% 70%",
             "single", "2030-01-03T10:00", "open", NOW),
        ).lastrowid)
        date_id = int(conn.execute(
            "INSERT INTO dates("
            "owner_id,name,place,starts_at,ends_at,comment,origin,is_draft,"
            "pay_split,share_token,is_public,capacity,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, "Прогулка", "Парк", "2030-01-10T18:00", "2030-01-10T20:00",
             "Тёплый вечер", "admin", 0, 1, f"date-{telegram_id}", 1, 4, NOW),
        ).lastrowid)
        conn.execute(
            "INSERT INTO date_categories(date_id,category_id,position) VALUES(?,?,7)",
            (date_id, category_id),
        )
        conn.execute(
            "INSERT INTO date_links(date_id,url,position) VALUES(?,?,0)",
            (date_id, "https://example.com/route"),
        )
        conn.execute(
            "INSERT INTO date_images(date_id,filename,position,focus) VALUES(?,?,0,?)",
            (date_id, photo_name, "25% 75%"),
        )
        conn.execute(
            "INSERT INTO date_videos(date_id,filename,position) VALUES(?,?,0)",
            (date_id, video_name),
        )
        conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,created_at) VALUES(?,?,?,?)",
            (date_id, category_id, "guest-source", NOW),
        )
        conn.execute(
            "INSERT INTO questions(date_id,category_id,guest_token,text,created_at) "
            "VALUES(?,?,?,?,?)",
            (date_id, category_id, "guest-source", "А во сколько?", NOW),
        )
        conn.commit()
        conn.close()
        return category_id, date_id, {og_name, photo_name, video_name}

    def test_category_copy_is_independent_and_owner_guarded(self):
        with TestClient(main.app, follow_redirects=False) as owner:
            csrf = login(owner, 880101)
            source_category, source_date, source_files = self._seed_source(880101)

            detail = owner.get(f"/admin/categories/{source_category}").text
            self.assertIn("Скопировать категорию", detail)
            self.assertIn(f'action="/admin/categories/{source_category}/clone"', detail)
            category_list = owner.get("/admin/categories").text
            self.assertIn(
                f'action="/admin/categories/{source_category}/clone"', category_list,
            )
            event_editor = owner.get(f"/admin/dates/{source_date}/edit").text
            self.assertIn("Другие действия с событием", event_editor)
            self.assertIn("Скопировать событие", event_editor)

            response = owner.post(
                f"/admin/categories/{source_category}/clone", data={"csrf": csrf},
            )
            self.assertEqual(response.status_code, 303)

            conn = db.connect()
            copied_category = conn.execute(
                "SELECT * FROM categories WHERE name='Маршрут (копия)'",
            ).fetchone()
            self.assertIsNotNone(copied_category)
            self.assertNotEqual(copied_category["id"], source_category)
            self.assertNotEqual(copied_category["link_token"], "cat-880101")
            self.assertEqual(copied_category["category_skin"], "romantic")
            self.assertEqual(copied_category["voting_status"], "unconfigured")
            self.assertEqual(copied_category["voting_deadline"], "2030-01-03T10:00:00")
            self.assertEqual(copied_category["link_enabled"], 0)
            self.assertNotEqual(copied_category["og_image"], "copy-880101-og.webp")
            self.assertEqual(copied_category["og_focus"], "30% 70%")

            copied_date = conn.execute(
                "SELECT d.*, dc.position AS cat_position FROM dates d "
                "JOIN date_categories dc ON dc.date_id=d.id "
                "WHERE dc.category_id=?",
                (copied_category["id"],),
            ).fetchone()
            self.assertEqual(copied_date["name"], "Прогулка (копия)")
            self.assertNotEqual(copied_date["id"], source_date)
            self.assertNotEqual(copied_date["share_token"], "date-880101")
            self.assertEqual(copied_date["is_public"], 1)
            self.assertEqual(copied_date["is_draft"], 1)
            self.assertEqual(copied_date["cat_position"], 7)
            self.assertEqual(conn.execute(
                "SELECT url FROM date_links WHERE date_id=?", (copied_date["id"],),
            ).fetchone()[0], "https://example.com/route")
            copied_media = {row["filename"] for row in conn.execute(
                "SELECT filename FROM date_images WHERE date_id=?",
                (copied_date["id"],),
            )}
            copied_media.update(row["filename"] for row in conn.execute(
                "SELECT filename FROM date_videos WHERE date_id=?",
                (copied_date["id"],),
            ))
            self.assertTrue(copied_media.isdisjoint(source_files))
            self.assertEqual(conn.execute(
                "SELECT focus FROM date_images WHERE date_id=?", (copied_date["id"],),
            ).fetchone()[0], "25% 75%")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE date_id=?", (copied_date["id"],),
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM questions WHERE date_id=?", (copied_date["id"],),
            ).fetchone()[0], 0)
            copied_files = copied_media | {copied_category["og_image"]}
            conn.close()
            self.assertTrue(all((main.images.UPLOAD_DIR / name).exists()
                                for name in source_files | copied_files))

            with TestClient(main.app, follow_redirects=False) as stranger:
                stranger_csrf = login(stranger, 880102)
                before = db.connect()
                before_count = before.execute(
                    "SELECT COUNT(*) FROM categories",
                ).fetchone()[0]
                before.close()
                forbidden = stranger.post(
                    f"/admin/categories/{source_category}/clone",
                    data={"csrf": stranger_csrf},
                )
                # POST-ошибки кабинета превращаются в дружелюбный flash-redirect,
                # но owner-гейт не должен создать ни одной строки/копии.
                self.assertEqual(forbidden.status_code, 303)
                self.assertIn("Категория не найдена", unquote(forbidden.headers["location"]))
                after = db.connect()
                self.assertEqual(after.execute(
                    "SELECT COUNT(*) FROM categories",
                ).fetchone()[0], before_count)
                after.close()

    def test_responsive_feed_and_focus_drag_guards_are_present(self):
        css = (APP / "static/admin.css").read_text(encoding="utf-8")
        self.assertIn(
            ".cfeed {\n  display: grid;\n  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));",
            css,
        )
        self.assertIn(".grid, .cfeed { grid-template-columns: minmax(0, 1fr); }", css)
        self.assertIn(
            "(max-width: 950px) and (max-height: 600px) and (pointer: coarse)", css,
        )

        ui = (APP / "static/ui.js").read_text(encoding="utf-8")
        self.assertIn('.ptile, .ed-slide, button, a', ui)
        admin = (APP / "static/admin.js").read_text(encoding="utf-8")
        self.assertIn("touchEditsFocus", admin)
        self.assertIn("img.releasePointerCapture(e.pointerId)", admin)
        self.assertIn("e.stopPropagation();", admin)

        date_form = (APP / "templates/admin/date_form.html").read_text(encoding="utf-8")
        public_category = (APP / "templates/public/category.html").read_text(encoding="utf-8")
        self.assertIn('class="required-title"', date_form)
        self.assertIn('class="required-title"', public_category)
        self.assertIn(".required-mark {", css)
        self.assertIn(
            ".cat-editor-main-actions > .btn { padding: 13px 18px; font-size: 16px; }",
            css,
        )

    def test_community_widget_toggles_want_without_copying_event(self):
        with TestClient(main.app, follow_redirects=False) as owner:
            login(owner, 880201)
            _, source_date, _ = self._seed_source(880201)

        with TestClient(main.app, follow_redirects=False) as viewer:
            csrf = login(viewer, 880202)
            widget = viewer.get(f"/admin/community/date/{source_date}")
            self.assertEqual(widget.status_code, 200)
            self.assertIn("Хочу сходить", widget.text)
            self.assertIn(f'data-want="/d/date-880201/want"', widget.text)

            marked = viewer.post(
                "/d/date-880201/want",
                data={"csrf": csrf},
                headers={"X-Requested-With": "fetch"},
            )
            self.assertEqual(marked.status_code, 200, marked.text)
            self.assertEqual(marked.json()["wanted"], True)

            conn = db.connect()
            viewer_id = conn.execute(
                "SELECT id FROM users WHERE telegram_id=880202",
            ).fetchone()[0]
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM date_wants WHERE user_id=? AND date_id=?",
                (viewer_id, source_date),
            ).fetchone())
            # Отметка не вызывает существующую ручку копирования события.
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM dates WHERE owner_id=?",
                (viewer_id,),
            ).fetchone()[0], 0)
            conn.close()

            marked_widget = viewer.get(f"/admin/community/date/{source_date}")
            self.assertIn("Убрать из «Хочу сходить»", marked_widget.text)

            unmarked = viewer.post(
                "/d/date-880201/want",
                data={"csrf": csrf},
                headers={"X-Requested-With": "fetch"},
            )
            self.assertEqual(unmarked.status_code, 200, unmarked.text)
            self.assertEqual(unmarked.json()["wanted"], False)
            conn = db.connect()
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM date_wants WHERE user_id=? AND date_id=?",
                (viewer_id, source_date),
            ).fetchone())
            conn.close()

    def test_feed_share_and_status_tabs_ui_contract(self):
        with TestClient(main.app, follow_redirects=False) as owner:
            login(owner, 880301)
            _, source_date, _ = self._seed_source(880301)

        with TestClient(main.app, follow_redirects=False) as viewer:
            login(viewer, 880302)
            feed = viewer.get("/admin/community")
            self.assertEqual(feed.status_code, 200)
            self.assertIn("Поделиться", feed.text)
            self.assertIn('data-community-share', feed.text)
            self.assertIn('data-share-url="https://copy.test/d/date-880301"', feed.text)
            self.assertIn('class="cfeed-card-actions"', feed.text)
            self.assertIn("Добавить в коллекцию", feed.text)
            self.assertIn(
                f'data-add="/d/date-880301/add"', feed.text,
            )
            self.assertNotIn('class="cfeed-owner"', feed.text)

            widget = viewer.get(f"/admin/community/date/{source_date}")
            self.assertEqual(widget.status_code, 200)
            self.assertIn('class="btn ghost cwid-share"', widget.text)
            self.assertIn('data-community-share', widget.text)

            dates = viewer.get("/admin/dates?view=active")
            self.assertEqual(dates.status_code, 200)
            self.assertIn('class="tabs dates-status-tabs"', dates.text)
            self.assertIn('data-glass-key="dates-status"', dates.text)

        admin_js = (APP / "static/admin.js").read_text(encoding="utf-8")
        self.assertIn('window.matchMedia("(hover: none) and (pointer: coarse)")', admin_js)
        self.assertIn('navigator.share({', admin_js)
        self.assertIn('navigator.clipboard.writeText(url)', admin_js)
        self.assertIn('Ссылка на событие скопирована', admin_js)
        self.assertIn('[data-community-add]', admin_js)

        ui_js = (APP / "static/ui.js").read_text(encoding="utf-8")
        self.assertIn("container.dataset.glassKey", ui_js)
        self.assertIn("if (container._d4yGlassReady) return", ui_js)
        self.assertIn("x: el.offsetLeft", ui_js)
        self.assertNotIn("el.offsetLeft - container.scrollLeft", ui_js)
        self.assertLess(
            ui_js.index('ind.style.width = start.w + "px"'),
            ui_js.index("container.appendChild(ind)"),
        )

        css = (APP / "static/admin.css").read_text(encoding="utf-8")
        self.assertIn(".dates-status-tabs a {", css)
        self.assertIn("font-size: 15.5px;", css)
        self.assertIn("background: var(--accent);", css)
        self.assertIn(
            "grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);", css,
        )
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("cubic-bezier(.2, .75, .3, 1)", css)

    def test_dates_visibility_filter_executes_and_survives_status_navigation(self):
        with TestClient(main.app, follow_redirects=False) as client:
            login(client, 880401)
            conn = db.connect()
            owner_id = int(conn.execute(
                "SELECT id FROM users WHERE telegram_id=880401",
            ).fetchone()[0])
            conn.executemany(
                "INSERT INTO dates("
                "owner_id,name,is_public,is_draft,archived_at,created_at"
                ") VALUES(?,?,?,?,?,?)",
                [
                    (owner_id, "Фильтр: публичное активное", 1, 0, None, NOW),
                    (owner_id, "Фильтр: непубличное активное", 0, 0, None, NOW),
                    (owner_id, "Фильтр: публичный черновик", 1, 1, None, NOW),
                    (owner_id, "Фильтр: непубличный черновик", 0, 1, None, NOW),
                    (owner_id, "Фильтр: публичный архив", 1, 0, NOW, NOW),
                    (owner_id, "Фильтр: непубличный архив", 0, 0, NOW, NOW),
                ],
            )
            conn.commit()
            conn.close()

            public_active = client.get("/admin/dates?view=active&f=public")
            self.assertEqual(public_active.status_code, 200)
            self.assertIn("Фильтр: публичное активное", public_active.text)
            self.assertNotIn("Фильтр: непубличное активное", public_active.text)
            self.assertNotIn("Фильтр: публичный черновик", public_active.text)
            self.assertRegex(
                public_active.text,
                r'<option value="public"\s+selected>Публичные</option>',
            )
            self.assertIn(
                'href="/admin/dates?view=drafts&amp;f=public"',
                public_active.text,
            )
            self.assertIn(
                'href="/admin/dates?view=archived&amp;f=public"',
                public_active.text,
            )
            self.assertIn(
                '<input type="hidden" name="view" value="active">',
                public_active.text,
            )

            private_active = client.get("/admin/dates?view=active&f=private")
            self.assertIn("Фильтр: непубличное активное", private_active.text)
            self.assertNotIn("Фильтр: публичное активное", private_active.text)
            self.assertRegex(
                private_active.text,
                r'<option value="private"\s+selected>Непубличные</option>',
            )
            self.assertIn(
                'href="/admin/dates?view=drafts&amp;f=private"',
                private_active.text,
            )

            public_drafts = client.get("/admin/dates?view=drafts&f=public")
            self.assertIn("Фильтр: публичный черновик", public_drafts.text)
            self.assertNotIn("Фильтр: непубличный черновик", public_drafts.text)
            self.assertIn(
                'href="/admin/dates?view=active&amp;f=public"',
                public_drafts.text,
            )

            private_archive = client.get("/admin/dates?view=archived&f=private")
            self.assertIn("Фильтр: непубличный архив", private_archive.text)
            self.assertNotIn("Фильтр: публичный архив", private_archive.text)
            self.assertIn(
                'href="/admin/dates?view=active&amp;f=private"',
                private_archive.text,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
