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
import operator_routes  # noqa: E402
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

    def _questions_context(self, user_id: int, filter_value: str):
        user = self.conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,),
        ).fetchone()
        request = SimpleNamespace(
            query_params={"f": filter_value},
            state=SimpleNamespace(user=user),
            session={"csrf": "test"},
        )
        with patch.object(
                admin_routes.templates, "TemplateResponse",
                side_effect=lambda _request, _name, context: context), \
                patch.object(
                    admin_routes.notify, "get_preferences", return_value={},
                ):
            return admin_routes.questions_list(request, conn=self.conn)

    def test_reviews_filter_selects_due_want_without_prebuilt_queue(self):
        date_id = self._date(
            self.owner_id, "Выбранное прошедшее событие",
            starts_at="2030-01-02T18:00:00",
            ends_at="2030-01-02T20:00:00",
            share_token="selected-past-event",
        )
        self._want(date_id)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0],
            0,
            "страница должна находить due-событие, а не зависеть от побочного заполнения очереди",
        )

        current = datetime(2030, 1, 2, 20, 0, 1)
        with patch.object(admin_routes, "now_naive", return_value=current), \
                patch.object(social_events, "now_naive", return_value=current):
            context = self._questions_context(self.viewer_id, "reviews")

        self.assertEqual(
            [row["date_name"] for row in context["review_rows"]],
            ["Выбранное прошедшее событие"],
        )

    def test_reviews_filter_selects_due_winning_booking(self):
        date_id = self._date(
            self.owner_id, "Прошедший выбранный победитель",
            starts_at="2030-01-02T18:00:00",
            ends_at="2030-01-02T20:00:00",
            share_token="selected-winning-event",
        )
        category_id = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,choice_mode,voting_deadline,"
            "voting_status,created_at) VALUES(?,?,?,?,?,?)",
            (
                self.owner_id, "Завершённая подборка", "multiple",
                "2030-01-01T20:00:00", "unconfigured", STAMP,
            ),
        ).lastrowid)
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            (date_id, category_id),
        )
        self.conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
            "VALUES(?,?,?,?,?)",
            (date_id, category_id, "winner", self.viewer_id, STAMP),
        )
        self.conn.execute(
            "UPDATE categories SET voting_status='resolved',winner_date_id=?,"
            "closed_at=?,resolved_at=? WHERE id=?",
            (date_id, STAMP, STAMP, category_id),
        )

        current = datetime(2030, 1, 2, 20, 0, 1)
        with patch.object(social_events, "now_naive", return_value=current):
            context = self._questions_context(self.viewer_id, "reviews")

        self.assertEqual(
            [row["date_name"] for row in context["review_rows"]],
            ["Прошедший выбранный победитель"],
        )

    def test_dismissed_due_event_stays_out_of_review_waiting(self):
        date_id = self._date(
            self.owner_id, "Убранное прошедшее событие",
            starts_at="2030-01-02T18:00:00",
            ends_at="2030-01-02T20:00:00",
            share_token="dismissed-due-event",
        )
        self._want(date_id)
        current = datetime(2030, 1, 2, 20, 0, 1)

        with patch.object(social_events, "now_naive", return_value=current):
            self.assertEqual(
                [row["date_name"] for row in self._questions_context(
                    self.viewer_id, "reviews",
                )["review_rows"]],
                ["Убранное прошедшее событие"],
            )
            self.assertEqual(social_events.clear_review_waiting(
                self.conn, date_id, self.viewer_id,
            ), 1)
            self.assertEqual(
                self._questions_context(self.viewer_id, "reviews")["review_rows"],
                [],
            )
            self.assertEqual(social_events.clear_review_waiting(
                self.conn, date_id, self.viewer_id,
            ), 0)

    def test_undated_want_is_due_immediately_after_collection_deadline(self):
        category_id = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,choice_mode,voting_deadline,"
            "voting_status,created_at) VALUES(?,?,?,?,?,?)",
            (
                self.owner_id, "Подборка с дедлайном", "multiple",
                "2030-01-03T11:00:00", "open", STAMP,
            ),
        ).lastrowid)
        date_id = self._date(
            self.owner_id, "Выбранное событие без своей даты",
            share_token="undated-selected-event",
        )
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            (date_id, category_id),
        )
        self.conn.execute(
            "UPDATE categories SET voting_status='no_winner',closed_at=? WHERE id=?",
            ("2030-01-03T11:00:00", category_id),
        )
        self._want(date_id)

        deadline = datetime(2030, 1, 3, 11, 0, 0)
        self.assertEqual(
            social_events.review_due(self.conn, date_id), deadline,
            "у события без собственной даты дедлайн подборки сам открывает отзыв",
        )
        self.assertTrue(social_events.review_available(
            self.conn, date_id, self.viewer_id,
            now=datetime(2030, 1, 3, 11, 0, 1),
        ))

    def test_undated_standalone_want_is_not_automatically_waiting(self):
        date_id = self._date(
            self.owner_id, "Событие без даты и подборки",
            share_token="undated-standalone-event",
        )
        self._want(date_id)

        current = datetime(2030, 1, 10, 12, 0, 0)
        with patch.object(social_events, "now_naive", return_value=current):
            context = self._questions_context(self.viewer_id, "reviews")

        self.assertEqual(context["review_rows"], [])

    def test_invalid_legacy_deadline_does_not_override_valid_deadline(self):
        date_id = self._date(
            self.owner_id, "Событие со старым битым дедлайном",
            share_token="legacy-deadline-event",
        )
        valid_category = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,choice_mode,voting_deadline,"
            "voting_status,created_at) VALUES(?,?,?,?,?,?)",
            (
                self.owner_id, "Корректная подборка", "multiple",
                "2030-01-05T12:00:00", "unconfigured", STAMP,
            ),
        ).lastrowid)
        invalid_category = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,choice_mode,voting_deadline,"
            "voting_status,created_at) VALUES(?,?,?,?,?,?)",
            (
                self.owner_id, "Старая подборка", "multiple", "zzzz",
                "unconfigured", STAMP,
            ),
        ).lastrowid)
        self.conn.executemany(
            "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
            ((date_id, valid_category), (date_id, invalid_category)),
        )
        self._want(date_id)

        self.assertEqual(
            social_events.review_due(self.conn, date_id),
            datetime(2030, 1, 5, 12, 0, 0),
        )
        current = datetime(2030, 1, 4, 12, 0, 0)
        with patch.object(social_events, "now_naive", return_value=current):
            context = self._questions_context(self.viewer_id, "reviews")
        self.assertEqual(context["review_rows"], [])

    def test_removed_all_filter_cannot_return_answered_notifications(self):
        date_id = self._date(self.viewer_id, "Событие с вопросами")
        self.conn.executemany(
            "INSERT INTO questions(date_id,guest_token,text,answer,is_read,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                (date_id, "new", "Нужен ответ", None, 0, STAMP),
                (date_id, "seen", "Прочитан, но ждёт ответа", None, 1, STAMP),
                (date_id, "done", "Уже отвечен", "Да", 1, STAMP),
            ),
        )

        context = self._questions_context(self.viewer_id, "all")

        self.assertNotEqual(context["f"], "all")
        self.assertCountEqual(
            [row["text"] for row in context["rows"]],
            ["Нужен ответ", "Прочитан, но ждёт ответа"],
        )

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

    def test_archive_does_not_prompt_for_want_only_user(self):
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
        self.assertIsNone(first)
        self.assertIsNone(second)
        rows = self.conn.execute(
            "SELECT id,event_key,kind,send_at FROM notification_outbox"
        ).fetchall()
        self.assertEqual(rows, [])

    def test_autoarchive_creates_review_queue_without_want_prompt(self):
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
        self.assertEqual(prompts, [])

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
            0,
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
            "og_focus,choice_mode,voting_deadline,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (self.owner_id, "С фиксированным превью", "fixed-preview", 1,
             "custom-preview.webp", "24% 76%", "multiple",
             "2099-01-01T00:00:00", STAMP),
        ).lastrowid)
        self.conn.commit()

        owner_request = self._request_for(self.owner_id)
        response = admin_routes.category_default_preview(
            category_id, owner_request, conn=self.conn,
        )
        self.assertEqual(response.status_code, 303)
        row = self.conn.execute(
            "SELECT use_default_preview,og_image,og_focus FROM categories WHERE id=?",
            (category_id,),
        ).fetchone()
        self.assertEqual(row["use_default_preview"], 1)
        self.assertEqual(row["og_image"], "custom-preview.webp")
        self.assertEqual(row["og_focus"], "24% 76%")

        preview = public_routes.public_og_image("fixed-preview", conn=self.conn)
        self.assertEqual(preview.media_type, "image/png")
        self.assertEqual(
            Path(preview.path).resolve(),
            images.og_default_path("friends").resolve(),
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
            "SELECT use_default_preview,og_image,og_focus FROM categories WHERE id=?",
            (category_id,),
        ).fetchone()
        self.assertEqual(tuple(row), (0, "custom-preview.webp", "24% 76%"))

        generated = images.OG_CACHE_DIR / "generated-custom-preview.webp"
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
            "data-friends-src=\"/admin/categories/{{ cat['id'] }}/og-preview?skin=friends",
            detail_template,
        )
        self.assertIn(
            "data-romantic-src=\"/admin/categories/{{ cat['id'] }}/og-preview?skin=romantic",
            detail_template,
        )
        self.assertNotIn("og-friends.jpg", detail_template)
        self.assertNotIn("og-default.jpg", detail_template)
        self.assertIn(
            "{{ BASE_URL }}/c/{{ token }}/og-image?skin={{ active_skin }}",
            public_template,
        )
        self.assertIn("&amp;v={{ preview_revision }}", public_template)

    def test_category_rename_saves_new_preview_and_focus_together(self):
        category_id = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,link_enabled,"
            "use_default_preview,choice_mode,voting_deadline,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (self.owner_id, "До кропа", "crop-preview", 1, 1, "multiple",
             "2099-01-01T00:00:00", STAMP),
        ).lastrowid)
        self.conn.commit()
        request = self._request_for(self.owner_id)
        upload = SimpleNamespace(filename="preview.png")

        with patch.object(admin_routes.images, "save_upload",
                          return_value="saved-preview.webp") as save_upload:
            response = admin_routes.category_rename(
                category_id, request,
                name="После кропа", description="Описание",
                og_title="Заголовок", og_desc="Подпись",
                category_skin="friends", og_image=upload,
                og_focus="23% 77%", conn=self.conn,
            )
        self.assertEqual(response.status_code, 303)
        save_upload.assert_called_once_with(upload)
        row = self.conn.execute(
            "SELECT og_image,og_focus,use_default_preview FROM categories WHERE id=?",
            (category_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("saved-preview.webp", "23% 77%", 0))

        # Валидация идёт до записи файла: ошибочный crop не оставляет orphan.
        with patch.object(admin_routes.images, "save_upload") as invalid_save:
            with self.assertRaises(HTTPException) as raised:
                admin_routes.category_rename(
                    category_id, request,
                    name="После кропа", description="Описание",
                    og_title="Заголовок", og_desc="Подпись",
                    category_skin="friends", og_image=upload,
                    og_focus="101% 50%", conn=self.conn,
                )
        self.assertEqual(raised.exception.status_code, 400)
        invalid_save.assert_not_called()

    def test_category_focus_compare_and_set_rejects_stale_requests(self):
        category_id = int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,link_enabled,og_image,"
            "og_focus,use_default_preview,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.owner_id, "CAS-превью", "cas-preview", 1,
             "saved-preview.webp", None, 0, STAMP),
        ).lastrowid)
        self.conn.commit()
        request = self._request_for(self.owner_id)

        with patch.object(admin_routes.images, "upload_image_exists", return_value=True):
            response = admin_routes.category_og_focus(
                category_id, request,
                focus="20% 80%",
                expected_image="saved-preview.webp",
                # NULL в БД является центром для compare-and-set.
                expected_focus="50% 50%",
                conn=self.conn,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.conn.execute(
            "SELECT og_focus FROM categories WHERE id=?", (category_id,),
        ).fetchone()[0], "20% 80%")

        # Более старый drag всё ещё ожидает исходный центр. Он не должен
        # перезаписать уже подтверждённый focus того же файла.
        with patch.object(admin_routes.images, "upload_image_exists", return_value=True):
            with self.assertRaises(HTTPException) as stale_focus:
                admin_routes.category_og_focus(
                    category_id, request,
                    focus="90% 10%",
                    expected_image="saved-preview.webp",
                    expected_focus="50% 50%",
                    conn=self.conn,
                )
        self.assertEqual(stale_focus.exception.status_code, 409)
        self.assertEqual(tuple(self.conn.execute(
            "SELECT og_image,og_focus FROM categories WHERE id=?", (category_id,),
        ).fetchone()), ("saved-preview.webp", "20% 80%"))

        # Обычный submit успел заменить файл и его crop. Запоздалый POST от
        # предыдущей картинки не имеет права менять focus новой.
        self.conn.execute(
            "UPDATE categories SET og_image=?,og_focus=? WHERE id=?",
            ("replacement-preview.webp", "65% 35%", category_id),
        )
        self.conn.commit()
        with patch.object(admin_routes.images, "upload_image_exists", return_value=True):
            with self.assertRaises(HTTPException) as stale_image:
                admin_routes.category_og_focus(
                    category_id, request,
                    focus="5% 95%",
                    expected_image="saved-preview.webp",
                    expected_focus="20% 80%",
                    conn=self.conn,
                )
        self.assertEqual(stale_image.exception.status_code, 409)
        self.assertEqual(tuple(self.conn.execute(
            "SELECT og_image,og_focus FROM categories WHERE id=?", (category_id,),
        ).fetchone()), ("replacement-preview.webp", "65% 35%"))

        # То же CAS-условие защищает скрытую custom-картинку, пока включено
        # фиксированное стандартное превью.
        self.conn.execute(
            "UPDATE categories SET use_default_preview=1 WHERE id=?", (category_id,),
        )
        self.conn.commit()
        with patch.object(admin_routes.images, "upload_image_exists", return_value=True):
            with self.assertRaises(HTTPException) as default_mode:
                admin_routes.category_og_focus(
                    category_id, request,
                    focus="30% 70%",
                    expected_image="replacement-preview.webp",
                    expected_focus="65% 35%",
                    conn=self.conn,
                )
        self.assertEqual(default_mode.exception.status_code, 409)
        self.assertEqual(self.conn.execute(
            "SELECT og_focus FROM categories WHERE id=?", (category_id,),
        ).fetchone()[0], "65% 35%")

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

    def test_category_list_batches_sources_and_skips_irrelevant_file_checks(self):
        for index in range(10):
            self.conn.execute(
                "INSERT INTO categories(owner_id,name,og_image,use_default_preview,"
                "created_at) VALUES(?,?,?,1,?)",
                (self.owner_id, f"Default {index}", "default-unused.webp", STAMP),
            )
        for index in range(10):
            self.conn.execute(
                "INSERT INTO categories(owner_id,name,og_image,created_at) "
                "VALUES(?,?,?,?)",
                (self.owner_id, f"Custom {index}", "custom-shared.webp", STAMP),
            )
        for index in range(10):
            category_id = int(self.conn.execute(
                "INSERT INTO categories(owner_id,name,created_at) VALUES(?,?,?)",
                (self.owner_id, f"Auto {index}", STAMP),
            ).lastrowid)
            date_id = self._date(self.owner_id, f"Фото {index}")
            self.conn.execute(
                "INSERT INTO date_categories(date_id,category_id,position) "
                "VALUES(?,?,0)",
                (date_id, category_id),
            )
            self.conn.execute(
                "INSERT INTO date_images(date_id,filename,position) VALUES(?,?,0)",
                (date_id, "live-shared.webp"),
            )

        traced: list[str] = []
        with patch.object(
                images, "upload_image_exists",
                side_effect=lambda filename: filename in {
                    "custom-shared.webp", "live-shared.webp",
                }) as exists:
            self.conn.set_trace_callback(traced.append)
            cats = admin_routes._categories_list_data(self.conn, self.owner_id)
            self.conn.set_trace_callback(None)

        self.assertEqual(len(cats), 30)
        self.assertTrue(all(category["has_og"] for category in cats))
        self.assertEqual(
            sum(sql.lstrip().upper().startswith("SELECT") for sql in traced), 2,
        )
        self.assertEqual(exists.call_count, 2)
        self.assertEqual(
            {call.args[0] for call in exists.call_args_list},
            {"custom-shared.webp", "live-shared.webp"},
        )

    def test_category_activity_is_defined_only_by_a_valid_future_deadline(self):
        now = datetime(2030, 1, 1, 12, 0, 0)
        cases = (
            ("2030-01-01T12:00:01", "active"),
            ("2030-01-01T12:00:00", "expired"),
            ("2029-12-31T23:59:59", "expired"),
            (None, "missing"),
            ("", "missing"),
            ("not-a-date", "missing"),
            ("2030-01-01T12:00:01+03:00", "missing"),
        )
        for deadline, expected in cases:
            with self.subTest(deadline=deadline):
                self.assertEqual(
                    admin_routes._category_deadline_state(deadline, now=now),
                    expected,
                )

    def test_operator_report_lists_use_constant_joined_queries(self):
        reported_date = self._date(self.owner_id, "С жалобами")
        clean_date = self._date(self.owner_id, "Без открытых жалоб")
        self.conn.executemany(
            "INSERT INTO reports(target_type,target_id,reporter,reason,status,created_at) "
            "VALUES('date',?,?,?,?,?)",
            (
                (reported_date, "u2", "Первая", "open", STAMP),
                (reported_date, "u3", "Вторая", "open", STAMP),
                (clean_date, "u2", "Закрытая", "resolved", STAMP),
            ),
        )
        user = self.conn.execute(
            "SELECT * FROM users WHERE id=?", (self.owner_id,),
        ).fetchone()
        request = SimpleNamespace(
            state=SimpleNamespace(user=user), session={"csrf": "test"},
        )

        with patch.object(
                operator_routes.templates, "TemplateResponse",
                side_effect=lambda _request, _name, context: context):
            traced: list[str] = []
            self.conn.set_trace_callback(traced.append)
            dates_context = operator_routes.dates_list(
                request, flt="reported", conn=self.conn,
            )
            self.conn.set_trace_callback(None)
            reported_plan = "\n".join(
                row[3]
                for sql in traced if sql.lstrip().upper().startswith("SELECT")
                for row in self.conn.execute("EXPLAIN QUERY PLAN " + sql)
            )
            self.assertEqual(
                sum(sql.lstrip().upper().startswith("SELECT") for sql in traced), 2,
            )
            self.assertIn("idx_reports_open_date_target", reported_plan)
            self.assertEqual(dates_context["total"], 1)
            self.assertEqual(len(dates_context["rows"]), 1)
            self.assertEqual(dates_context["rows"][0]["id"], reported_date)
            self.assertEqual(dates_context["rows"][0]["reports"], 2)

            traced.clear()
            self.conn.set_trace_callback(traced.append)
            reports_context = operator_routes.reports_list(
                request, status="open", conn=self.conn,
            )
            self.conn.set_trace_callback(None)
            self.assertEqual(
                sum(sql.lstrip().upper().startswith("SELECT") for sql in traced), 2,
                "пагинация делает один COUNT и один JOIN-запрос без N+1",
            )
            self.assertEqual(len(reports_context["items"]), 2)
            self.assertTrue(all(
                item["t"]["name"] == "С жалобами"
                for item in reports_context["items"]
            ))

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
        generated = images.OG_CACHE_DIR / "generated-live-collage.webp"

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
