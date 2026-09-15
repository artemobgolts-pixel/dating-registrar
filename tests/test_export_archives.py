#!/usr/bin/env python3
"""Удаление личного экспорта и сохранение операторских export/backup."""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-export-import-")
os.environ.update({
    "DATA_DIR": _IMPORT_DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "export-archives.test",
    "SECRET_KEY": "export-archives-test-secret",
    "TG_BOT_TOKEN": "",
})

import admin_routes  # noqa: E402
import db  # noqa: E402
import images  # noqa: E402


STAMP = "2030-01-01T10:00:00"


class ExportArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="date4you-export-archives-")
        self.root = Path(self.tmp.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.upload_patch = patch.object(images, "UPLOAD_DIR", self.uploads)
        self.upload_patch.start()

        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        self.conn.executemany(
            "INSERT INTO users(id,telegram_id,display_name,avatar_path,is_operator,created_at) "
            "VALUES(?,?,?,?,?,?)",
            ((1, 1001, "Обычный", "account-avatar.webp", 0, STAMP),
             (2, 1002, "Оператор", "operator-avatar.webp", 1, STAMP)),
        )
        self.conn.executemany(
            "INSERT INTO categories(id,owner_id,name,og_image,created_at) VALUES(?,?,?,?,?)",
            ((1, 1, "Своя", "account-preview.webp", STAMP),
             (2, 2, "Чужая", "operator-preview.webp", STAMP)),
        )
        self.conn.executemany(
            "INSERT INTO dates(id,owner_id,name,share_token,created_at) VALUES(?,?,?,?,?)",
            ((1, 1, "Своё событие", "account-date", STAMP),
             (2, 2, "Чужое событие", "operator-date", STAMP)),
        )
        self.conn.executemany(
            "INSERT INTO date_images(date_id,filename,position) VALUES(?,?,0)",
            ((1, "account-photo.webp"), (2, "operator-photo.webp")),
        )
        self.conn.executemany(
            "INSERT INTO date_videos(date_id,filename,position) VALUES(?,?,0)",
            ((1, "account-video.mp4"), (2, "operator-video.webm")),
        )
        self.conn.commit()

        self.files = {
            "account-avatar.webp": b"account avatar",
            "account-preview.webp": b"account preview",
            "account-photo.webp": b"account photo",
            "account-video.mp4": b"account video",
            "operator-avatar.webp": b"operator avatar",
            "operator-preview.webp": b"operator preview",
            "operator-photo.webp": b"operator photo",
            "operator-video.webm": b"operator video",
            "orphan-file.webp": b"unreferenced source",
        }
        for filename, content in self.files.items():
            (self.uploads / filename).write_bytes(content)

        self.normal = self.conn.execute("SELECT * FROM users WHERE id=1").fetchone()
        self.operator = self.conn.execute("SELECT * FROM users WHERE id=2").fetchone()
        self.http_user = self.normal
        app = FastAPI()

        @app.middleware("http")
        async def synthetic_user(request, call_next):
            request.state.user = self.http_user
            return await call_next(request)

        app.dependency_overrides[admin_routes.current_user] = lambda: self.http_user
        app.dependency_overrides[admin_routes.get_db] = lambda: self.conn
        app.include_router(admin_routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.conn.close()
        self.upload_patch.stop()
        self.tmp.cleanup()

    def test_account_archive_route_is_removed_for_all_users(self):
        for user in (self.normal, self.operator):
            with self.subTest(user=user["id"]):
                self.http_user = user
                response = self.client.get("/admin/export/account-archive")
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("content-disposition", response.headers)

    def test_ordinary_user_cannot_export_or_create_platform_backup(self):
        with patch.object(admin_routes.backup, "make_backup") as make_backup:
            for suffix in ("csv", "json", "platform-backup", "archive"):
                with self.subTest(export=suffix):
                    response = self.client.get(f"/admin/export/{suffix}")
                    self.assertEqual(response.status_code, 404)
            make_backup.assert_not_called()

    def test_operator_csv_and_json_exports_remain_functional(self):
        self.http_user = self.operator
        response = self.client.get("/admin/export/json")
        self.assertEqual(response.status_code, 200)
        exported = response.json()
        self.assertEqual([date["id"] for date in exported["dates"]], [2])
        self.assertEqual(exported["dates"][0]["videos"], ["operator-video.webm"])
        self.assertEqual(exported["dates"][0]["images"], ["operator-photo.webp"])
        self.assertEqual([cat["id"] for cat in exported["categories"]], [2])

        response = self.client.get("/admin/export/csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Чужое событие", response.text)
        self.assertNotIn("Своё событие", response.text)
        self.assertIn("attachment;", response.headers["content-disposition"])

    def _export_csv_row(self):
        self.http_user = self.operator
        response = self.client.get("/admin/export/csv")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(
            io.StringIO(response.content.decode("utf-8-sig"), newline=""),
            delimiter=";", strict=True,
        ))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), 13)
        self.assertEqual(len(rows[1]), 13)
        return dict(zip(rows[0], rows[1]))

    def test_operator_csv_neutralizes_formula_names_and_places(self):
        for value in ("=1+1", "+SUM(1,1)", "-1+2", "@SUM(1,1)"):
            with self.subTest(value=value):
                self.conn.execute(
                    "UPDATE dates SET name=?,place=? WHERE id=2", (value, value),
                )
                self.conn.commit()
                row = self._export_csv_row()
                self.assertEqual(row["Название"], "'" + value)
                self.assertEqual(row["Место"], "'" + value)

    def test_operator_csv_neutralizes_formulas_after_whitespace_and_controls(self):
        prefixes = (
            " ", "\t", "\r", "\n", "\x00", "\x1b", "\x7f", "\x85",
            "\u00a0", "\u2003", "\u200b", "\ufeff", " \t\x00\ufeff",
        )
        for prefix in prefixes:
            for formula in ("=1+1", "+SUM(1,1)", "-1+2", "@SUM(1,1)"):
                value = prefix + formula
                with self.subTest(value=value):
                    self.conn.execute(
                        "UPDATE dates SET name=?,place=? WHERE id=2", (value, value),
                    )
                    self.conn.commit()
                    row = self._export_csv_row()
                    self.assertEqual(row["Название"], "'" + value)
                    self.assertEqual(row["Место"], "'" + value)

    def test_operator_csv_protects_all_text_fields_without_changing_db_or_json(self):
        values = {
            "Название": "=1+1", "Место": "+SUM(1,1)", "Начало": "-1+2",
            "Конец": "@SUM(1,1)", "Кто выбрал": "\t=1+1",
            "Категории": "\ufeff+SUM(1,1)", "Ссылки": " \n@SUM(1,1)",
        }
        self.conn.execute(
            "UPDATE dates SET name=?,place=?,starts_at=?,ends_at=? WHERE id=2",
            tuple(values[key] for key in ("Название", "Место", "Начало", "Конец")),
        )
        self.conn.execute("UPDATE categories SET name=? WHERE id=2", (values["Категории"],))
        self.conn.execute("INSERT INTO date_categories(date_id,category_id) VALUES(2,2)")
        self.conn.execute(
            "INSERT INTO date_links(date_id,url) VALUES(2,?)", (values["Ссылки"],),
        )
        self.conn.execute(
            "INSERT INTO guests(token,name,created_at) VALUES('csv-guest',?,?)",
            (values["Кто выбрал"], STAMP),
        )
        self.conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,created_at) "
            "VALUES(2,2,'csv-guest',?)", (STAMP,),
        )
        self.conn.commit()
        before_db = list(self.conn.iterdump())
        self.http_user = self.operator
        before_json = self.client.get("/admin/export/json").json()

        row = self._export_csv_row()
        for column, value in values.items():
            with self.subTest(column=column):
                self.assertEqual(row[column], "'" + value)
        self.assertEqual(row["id"], "2")
        self.assertEqual(row["Выборы"], "1")
        self.assertEqual(list(self.conn.iterdump()), before_db)
        after_json = self.client.get("/admin/export/json").json()
        before_json.pop("exported_at")
        after_json.pop("exported_at")
        self.assertEqual(after_json, before_json)
        exported_date = after_json["dates"][0]
        self.assertEqual(exported_date["name"], values["Название"])
        self.assertEqual(exported_date["place"], values["Место"])
        self.assertEqual(exported_date["starts_at"], values["Начало"])
        self.assertEqual(exported_date["ends_at"], values["Конец"])
        self.assertEqual(exported_date["booked_by"], [values["Кто выбрал"]])
        self.assertEqual(exported_date["links"], [values["Ссылки"]])
        self.assertEqual(after_json["categories"][0]["name"], values["Категории"])

    def test_operator_csv_preserves_normal_text_unicode_and_csv_quoting(self):
        for value in (
            "Обычное событие", "Кафе, парк; терраса", 'Встреча "Лето"',
            "Первая строка\nВторая строка", "Первая\r\nВторая", "東京 — Москва 🥐",
            "  Текст с пробелами", "\tОбычный текст", "\ufeffНазвание", "", " \t\n",
            "Текст =1+1", "'Уже текст", ",=1+1", ";=1+1", '"=1+1',
        ):
            with self.subTest(value=value):
                self.conn.execute(
                    "UPDATE dates SET name=?,place=? WHERE id=2", (value, value),
                )
                self.conn.commit()
                row = self._export_csv_row()
                self.assertEqual(row["Название"], value)
                self.assertEqual(row["Место"], value)

    def test_platform_backup_is_operator_only_and_contains_snapshot_plus_all_uploads(self):
        snapshot = self.root / "consistent-snapshot.db"
        snapshot_db = sqlite3.connect(snapshot)
        snapshot_db.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        snapshot_db.execute("INSERT INTO marker VALUES('consistent')")
        snapshot_db.commit()
        snapshot_db.close()

        nested = self.uploads / "future-layout"
        nested.mkdir()
        (nested / "nested-media.bin").write_bytes(b"nested")

        self.http_user = self.operator
        for suffix in ("platform-backup", "archive"):
            with self.subTest(export=suffix), patch.object(
                admin_routes.backup, "make_backup", return_value=snapshot,
            ) as make_backup:
                response = self.client.get(f"/admin/export/{suffix}")
                make_backup.assert_called_once_with()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "private, no-store")
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                names = set(archive.namelist())
                expected_uploads = {
                    f"uploads/{filename}" for filename in self.files
                }
                expected_uploads.add("uploads/future-layout/nested-media.bin")
                self.assertEqual(names, {"app.db", *expected_uploads})
                self.assertNotIn("export.json", names)
                extracted = self.root / "extracted.db"
                extracted.write_bytes(archive.read("app.db"))
            check = sqlite3.connect(extracted)
            try:
                self.assertEqual(
                    check.execute("SELECT value FROM marker").fetchone()[0],
                    "consistent",
                )
            finally:
                check.close()

    def test_profile_does_not_advertise_archive_downloads(self):
        template = (APP / "templates/admin/profile.html").read_text("utf-8")
        self.assertNotIn('href="/admin/export/account-archive"', template)
        self.assertNotIn('href="/admin/export/platform-backup"', template)
        self.assertNotIn("Архив данных аккаунта", template)
        self.assertNotIn("Резервная копия всей платформы", template)
        for path in (APP / "templates").rglob("*.html"):
            with self.subTest(template=path.relative_to(APP)):
                self.assertNotIn(
                    "/admin/export/account-archive", path.read_text("utf-8"),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
