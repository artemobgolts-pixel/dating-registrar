#!/usr/bin/env python3
"""Поведенческие регрессии очереди отзывов и новых массовых действий."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-v30-backend-")
os.environ.update({
    "DATA_DIR": _IMPORT_DATA.name,
    "COOKIE_SECURE": "false",
    # unittest discovery импортирует модули до запуска тестов. Совпадаем с
    # прежним первым integration-модулем test_copy_actions, чтобы новый файл
    # не менял закэшированные config/main для остального legacy-suite.
    "DOMAIN": "copy.test",
    "SECRET_KEY": "test-secret-key-for-copy-actions",
    "TG_BOT_TOKEN": "",
    "TG_CHAT_ID": "",
    "TG_BOT_USERNAME": "date4you_copy_test_bot",
    "TG_WEBHOOK_SECRET": "copy-hook-secret",
    "OPERATOR_TG_IDS": "",
})

import admin_routes  # noqa: E402
import db  # noqa: E402
import images  # noqa: E402
import public_routes  # noqa: E402
import social_events  # noqa: E402
import tasks  # noqa: E402


STAMP = "2030-01-01T00:00:00"


class BackendV30RegressionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        self.owner_id = self._user(930001, "Владелец")
        self.viewer_id = self._user(930002, "Участник")
        self.other_id = self._user(930003, "Другой владелец")

    def tearDown(self):
        self.conn.close()

    def _user(self, telegram_id: int, name: str) -> int:
        return int(self.conn.execute(
            "INSERT INTO users(telegram_id,display_name,created_at) VALUES(?,?,?)",
            (telegram_id, name, STAMP),
        ).lastrowid)

    def _date(self, owner_id: int, name: str, *, starts_at: str | None = None,
              ends_at: str | None = None, share_token: str | None = None,
              is_public: int = 1) -> int:
        return int(self.conn.execute(
            "INSERT INTO dates(owner_id,name,starts_at,ends_at,share_token,"
            "is_public,created_at) VALUES(?,?,?,?,?,?,?)",
            (owner_id, name, starts_at, ends_at, share_token, is_public, STAMP),
        ).lastrowid)

    def _want(self, date_id: int, user_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
            "VALUES(?,?,?,?)",
            (user_id or self.viewer_id, date_id, STAMP, STAMP),
        )

    def _request_for(self, user_id: int) -> SimpleNamespace:
        user = self.conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,),
        ).fetchone()
        return SimpleNamespace(state=SimpleNamespace(user=user))

    def test_review_waiting_upserts_then_clears_without_duplicates(self):
        date_id = self._date(
            self.owner_id, "Прошедшая встреча",
            starts_at="2030-01-02T18:00:00",
            ends_at="2030-01-02T20:00:00",
            share_token="review-waiting",
        )
        self._want(date_id)

        first_time = datetime(2030, 1, 3, 12, 0, 0)
        second_time = datetime(2030, 1, 3, 13, 0, 0)
        self.assertTrue(social_events.mark_review_waiting(
            self.conn, date_id, self.viewer_id, "declined", now=first_time,
        ))
        self.assertTrue(social_events.mark_review_waiting(
            self.conn, date_id, self.viewer_id, "review_deleted", now=second_time,
        ))

        rows = self.conn.execute(
            "SELECT * FROM review_queue WHERE user_id=? AND date_id=?",
            (self.viewer_id, date_id),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "review_deleted")
        self.assertEqual(rows[0]["created_at"], "2030-01-03T12:00:00")
        self.assertEqual(rows[0]["updated_at"], "2030-01-03T13:00:00")

        self.assertEqual(
            social_events.clear_review_waiting(
                self.conn, date_id, self.viewer_id,
            ),
            1,
        )
        self.assertEqual(
            social_events.clear_review_waiting(
                self.conn, date_id, self.viewer_id,
            ),
            0,
        )

    def test_deleted_review_queue_preserves_return_right_after_withdrawal(self):
        date_id = self._date(
            self.owner_id, "Обзор после отказа",
            starts_at="2030-01-02T18:00:00",
            ends_at="2030-01-02T20:00:00",
            share_token="deleted-review-return",
        )
        # На момент удаления прежняя отметка/бронь уже может исчезнуть. Сам
        # принадлежавший пользователю обзор остаётся достаточным основанием
        # поместить событие в очередь и дать написать его заново.
        self.assertFalse(social_events.mark_review_waiting(
            self.conn, date_id, self.viewer_id, "review_deleted",
            now=datetime(2030, 1, 3, 12, 0, 0),
        ))
        self.assertTrue(social_events.mark_review_waiting(
            self.conn, date_id, self.viewer_id, "review_deleted",
            now=datetime(2030, 1, 3, 12, 0, 0), require_available=False,
        ))
        self.assertTrue(social_events.review_available(
            self.conn, date_id, self.viewer_id,
            now=datetime(2030, 1, 3, 12, 0, 0),
        ))
        self.assertEqual(self.conn.execute(
            "SELECT reason FROM review_queue WHERE user_id=? AND date_id=?",
            (self.viewer_id, date_id),
        ).fetchone()[0], "review_deleted")

    def test_archive_prompt_has_one_idempotency_key(self):
        date_id = self._date(
            self.owner_id, "Архивный prompt",
            ends_at="2030-01-02T20:00:00", share_token="archive-prompt",
        )
        self._want(date_id)

        first = social_events.queue_archive_review_prompt(
            self.conn, date_id, self.viewer_id,
            now=datetime(2030, 1, 3, 12, 0, 0),
        )
        second = social_events.queue_archive_review_prompt(
            self.conn, date_id, self.viewer_id,
            now=datetime(2030, 1, 3, 13, 0, 0),
        )
        self.assertEqual(first, second)
        rows = self.conn.execute(
            "SELECT id,event_key,kind,send_at FROM notification_outbox"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["event_key"],
            social_events.archive_prompt_key(date_id, self.viewer_id),
        )
        self.assertEqual(rows[0]["kind"], "review_prompt")
        self.assertEqual(rows[0]["send_at"], "2030-01-03T13:00:00")

    def test_autoarchive_creates_review_queue_and_prompt_once(self):
        date_id = self._date(
            self.owner_id, "Давно завершилось",
            starts_at="2000-01-01T18:00:00",
            ends_at="2000-01-01T20:00:00",
            share_token="autoarchive-review",
        )
        self._want(date_id)
        self.conn.commit()

        self.assertEqual(tasks.autoarchive_once(self.conn), 1)
        self.assertIsNotNone(self.conn.execute(
            "SELECT archived_at FROM dates WHERE id=?", (date_id,),
        ).fetchone()[0])
        queued = self.conn.execute(
            "SELECT user_id,date_id,reason FROM review_queue"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in queued],
            [(self.viewer_id, date_id, "due")],
        )
        prompts = self.conn.execute(
            "SELECT event_key FROM notification_outbox WHERE kind='review_prompt'"
        ).fetchall()
        self.assertEqual(
            [row["event_key"] for row in prompts],
            [social_events.archive_prompt_key(date_id, self.viewer_id)],
        )

        self.assertEqual(tasks.autoarchive_once(self.conn), 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE kind='review_prompt'"
            ).fetchone()[0],
            1,
        )

    def test_bulk_action_deduplicates_ids_and_rejects_foreign_dates(self):
        first = self._date(self.owner_id, "Первое", share_token="bulk-first")
        second = self._date(self.owner_id, "Второе", share_token="bulk-second")
        foreign = self._date(
            self.other_id, "Чужое", share_token="bulk-foreign",
        )
        self.conn.commit()
        request = self._request_for(self.owner_id)

        with patch.object(admin_routes, "user_throttle"):
            response = admin_routes.dates_bulk(
                request,
                BackgroundTasks(),
                action="make_private",
                date_ids=[first, first, second],
                next="/admin/dates?view=active",
                conn=self.conn,
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            [row["is_public"] for row in self.conn.execute(
                "SELECT is_public FROM dates WHERE id IN (?,?) ORDER BY id",
                (first, second),
            )],
            [0, 0],
        )

        self.conn.execute("UPDATE dates SET is_public=1 WHERE id=?", (first,))
        self.conn.commit()
        with patch.object(admin_routes, "user_throttle"):
            with self.assertRaises(HTTPException) as raised:
                admin_routes.dates_bulk(
                    request,
                    BackgroundTasks(),
                    action="make_private",
                    date_ids=[first, foreign],
                    next="/admin/dates",
                    conn=self.conn,
                )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.conn.execute(
            "SELECT is_public FROM dates WHERE id=?", (first,),
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT is_public FROM dates WHERE id=?", (foreign,),
        ).fetchone()[0], 1)

    def test_default_preview_toggle_is_owned_and_public_route_is_fixed(self):
        category_id = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,link_enabled,og_image,"
            "choice_mode,voting_deadline,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.owner_id, "С фиксированным превью", "fixed-preview", 1,
             "custom-preview.webp", "multiple", "2099-01-01T00:00:00", STAMP),
        ).lastrowid)
        self.conn.commit()

        owner_request = self._request_for(self.owner_id)
        response = admin_routes.category_default_preview(
            category_id, owner_request, conn=self.conn,
        )
        self.assertEqual(response.status_code, 303)
        row = self.conn.execute(
            "SELECT use_default_preview,og_image FROM categories WHERE id=?",
            (category_id,),
        ).fetchone()
        self.assertEqual(row["use_default_preview"], 1)
        self.assertEqual(row["og_image"], "custom-preview.webp")

        preview = public_routes.public_og_image("fixed-preview", conn=self.conn)
        self.assertEqual(preview.media_type, "image/jpeg")
        self.assertEqual(
            Path(preview.path).resolve(),
            (APP / "static" / "og-friends.jpg").resolve(),
        )
        self.assertEqual(preview.headers["cache-control"], "public, no-cache")

        with self.assertRaises(HTTPException) as raised:
            admin_routes.category_default_preview(
                category_id, self._request_for(self.other_id), conn=self.conn,
            )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.conn.execute(
            "SELECT use_default_preview FROM categories WHERE id=?", (category_id,),
        ).fetchone()[0], 1)

        admin_routes.category_default_preview(
            category_id, owner_request, conn=self.conn,
        )
        row = self.conn.execute(
            "SELECT use_default_preview,og_image FROM categories WHERE id=?",
            (category_id,),
        ).fetchone()
        self.assertEqual(tuple(row), (0, "custom-preview.webp"))

        generated = APP / "static" / "og-default.jpg"
        with patch.object(admin_routes.images, "upload_image_exists", return_value=True), \
                patch.object(admin_routes.images, "build_og_crop", return_value=generated):
            admin_dynamic = admin_routes.category_og_preview(
                category_id, owner_request, conn=self.conn,
            )
            public_dynamic = public_routes.public_og_image(
                "fixed-preview", conn=self.conn,
            )
        self.assertEqual(
            admin_dynamic.headers["cache-control"], "private, no-cache",
        )
        self.assertEqual(
            public_dynamic.headers["cache-control"], "public, no-cache",
        )

        detail_template = (APP / "templates/admin/category_detail.html").read_text(
            encoding="utf-8",
        )
        public_template = (APP / "templates/public/category.html").read_text(
            encoding="utf-8",
        )
        self.assertIn(
            "{% if cat['use_default_preview'] %}{{ asset('og-friends.jpg' if category_skin == 'friends' else 'og-default.jpg') }}",
            detail_template,
        )
        self.assertIn(
            "{% if cat['use_default_preview'] %}{{ BASE_URL }}{{ asset('og-friends.jpg' if active_skin == 'friends' else 'og-default.jpg') }}",
            public_template,
        )
        self.assertIn("&amp;v={{ preview_revision }}", public_template)

    def test_category_preview_sources_are_diverse_and_keep_focus(self):
        category_id = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,created_at) "
            "VALUES(?,?,?,?)",
            (self.owner_id, "Разнообразный коллаж", "diverse-preview", STAMP),
        ).lastrowid)
        galleries = (
            (("alpha000.webp", "10% 20%"),
             ("alpha001.webp", "11% 21%"),
             ("alpha002.webp", "12% 22%")),
            (("bravo000.webp", "30% 40%"),
             ("bravo001.webp", "31% 41%")),
            (("charlie0.webp", "50% 60%"),
             ("charlie1.webp", "51% 61%"),
             ("charlie2.webp", "52% 62%"),
             ("charlie3.webp", "53% 63%")),
        )
        for category_position, gallery in enumerate(galleries):
            date_id = self._date(self.owner_id, f"Событие {category_position}")
            self.conn.execute(
                "INSERT INTO date_categories(date_id,category_id,position) "
                "VALUES(?,?,?)",
                (date_id, category_id, category_position),
            )
            self.conn.executemany(
                "INSERT INTO date_images(date_id,filename,focus,position) "
                "VALUES(?,?,?,?)",
                (
                    (date_id, filename, focus, image_position)
                    for image_position, (filename, focus) in enumerate(gallery)
                ),
            )
        self.conn.commit()

        expected = [
            galleries[0][0], galleries[1][0], galleries[2][0],
            galleries[0][1], galleries[1][1], galleries[2][1],
            galleries[0][2], galleries[2][2],
        ]
        self.assertEqual(
            public_routes.category_og_sources(
                self.conn, category_id, include_focus=True,
            ),
            expected,
        )
        self.assertEqual(
            public_routes.category_og_sources(self.conn, category_id),
            [filename for filename, _focus in expected],
        )

    def test_missing_custom_preview_falls_back_to_live_collage(self):
        category_id = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,link_enabled,og_image,"
            "created_at) VALUES(?,?,?,?,?,?)",
            (self.owner_id, "Пропавшее превью", "missing-custom", 1,
             "missing-custom.webp", STAMP),
        ).lastrowid)
        date_id = self._date(self.owner_id, "С фото")
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id,position) VALUES(?,?,0)",
            (date_id, category_id),
        )
        self.conn.execute(
            "INSERT INTO date_images(date_id,filename,focus,position) VALUES(?,?,?,0)",
            (date_id, "live-photo.webp", "15% 65%"),
        )
        self.conn.commit()
        generated = APP / "static" / "og-default.jpg"

        def file_exists(filename):
            return filename == "live-photo.webp"

        with patch.object(
                images, "upload_image_exists", side_effect=file_exists), \
                patch.object(images, "build_og_collage", return_value=generated) as collage, \
                patch.object(images, "build_og_crop") as crop:
            admin_preview = admin_routes.category_og_preview(
                category_id, self._request_for(self.owner_id), conn=self.conn,
            )
            public_preview = public_routes.public_og_image(
                "missing-custom", conn=self.conn,
            )

        self.assertEqual(admin_preview.media_type, "image/webp")
        self.assertEqual(public_preview.media_type, "image/webp")
        self.assertFalse(crop.called)
        self.assertEqual(collage.call_count, 2)
        for call in collage.call_args_list:
            self.assertEqual(call.args[0], [("live-photo.webp", "15% 65%")])

    def test_auto_preview_revision_follows_category_lifecycle(self):
        category_id = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,category_skin,link_token,"
            "link_enabled,created_at) VALUES(?,?,?,?,1,?)",
            (self.owner_id, "Живое превью", "friends", "live-preview", STAMP),
        ).lastrowid)
        first = self._date(self.owner_id, "Первое")
        second = self._date(self.owner_id, "Второе")
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id,position) VALUES(?,?,0)",
            (first, category_id),
        )
        self.conn.execute(
            "INSERT INTO date_images(date_id,filename,position) VALUES(?,?,0)",
            (first, "firstimage.webp"),
        )
        self.conn.commit()

        sources = public_routes.category_og_sources(self.conn, category_id)
        self.assertEqual(sources, ["firstimage.webp"])
        first_revision = images.og_preview_revision(sources, "friends")

        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id,position) VALUES(?,?,1)",
            (second, category_id),
        )
        self.conn.execute(
            "INSERT INTO date_images(date_id,filename,position) VALUES(?,?,0)",
            (second, "secondimage.webp"),
        )
        self.conn.commit()
        added_sources = public_routes.category_og_sources(self.conn, category_id)
        added_revision = images.og_preview_revision(added_sources, "friends")
        self.assertEqual(added_sources, ["firstimage.webp", "secondimage.webp"])
        self.assertNotEqual(first_revision, added_revision)

        self.conn.execute(
            "UPDATE date_categories SET position=2-position WHERE category_id=?",
            (category_id,),
        )
        self.conn.commit()
        reordered_sources = public_routes.category_og_sources(self.conn, category_id)
        self.assertEqual(reordered_sources, ["secondimage.webp", "firstimage.webp"])
        self.assertNotEqual(
            added_revision,
            images.og_preview_revision(reordered_sources, "friends"),
        )

        self.conn.execute("UPDATE dates SET archived_at=? WHERE id=?", (STAMP, second))
        self.conn.commit()
        removed_sources = public_routes.category_og_sources(self.conn, category_id)
        self.assertEqual(removed_sources, ["firstimage.webp"])
        self.assertEqual(
            first_revision,
            images.og_preview_revision(removed_sources, "friends"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
