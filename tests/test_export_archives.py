#!/usr/bin/env python3
"""Удаление личного экспорта и сохранение операторских export/backup."""

from __future__ import annotations

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
