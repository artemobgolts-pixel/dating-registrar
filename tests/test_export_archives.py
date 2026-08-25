#!/usr/bin/env python3
"""Доступ и точный состав личного архива и platform backup."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException


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

        self.conn = sqlite3.connect(":memory:")
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

    def tearDown(self):
        self.conn.close()
        self.upload_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def request(user):
        return SimpleNamespace(state=SimpleNamespace(user=user))

    @staticmethod
    def cleanup_response(response) -> None:
        Path(response.path).unlink(missing_ok=True)

    def test_account_archive_is_available_to_user_and_contains_only_own_data(self):
        response = admin_routes.export_account_archive(
            self.request(self.normal), conn=self.conn,
        )
        try:
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            with zipfile.ZipFile(response.path) as archive:
                names = set(archive.namelist())
                self.assertEqual(names, {
                    "export.json",
                    "uploads/account-avatar.webp",
                    "uploads/account-preview.webp",
                    "uploads/account-photo.webp",
                    "uploads/account-video.mp4",
                })
                self.assertNotIn("app.db", names)
                exported = json.loads(archive.read("export.json"))
                self.assertEqual(
                    [category["name"] for category in exported["categories"]],
                    ["Своя"],
                )
                self.assertEqual(
                    [date["name"] for date in exported["dates"]],
                    ["Своё событие"],
                )
        finally:
            self.cleanup_response(response)

    def test_platform_backup_is_operator_only_and_contains_snapshot_plus_all_uploads(self):
        with self.assertRaises(HTTPException) as denied:
            admin_routes.export_platform_backup(self.request(self.normal))
        self.assertEqual(denied.exception.status_code, 404)

        snapshot = self.root / "consistent-snapshot.db"
        snapshot_db = sqlite3.connect(snapshot)
        snapshot_db.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        snapshot_db.execute("INSERT INTO marker VALUES('consistent')")
        snapshot_db.commit()
        snapshot_db.close()

        nested = self.uploads / "future-layout"
        nested.mkdir()
        (nested / "nested-media.bin").write_bytes(b"nested")

        with patch.object(admin_routes.backup, "make_backup", return_value=snapshot):
            response = admin_routes.export_platform_backup(
                self.request(self.operator),
            )
        try:
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            with zipfile.ZipFile(response.path) as archive:
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
        finally:
            self.cleanup_response(response)

    def test_profile_explains_both_archives_without_conflating_scope(self):
        template = (APP / "templates/admin/profile.html").read_text("utf-8")
        self.assertIn('href="/admin/export/account-archive"', template)
        self.assertIn("Общей базы\n    SQLite, чужих файлов", template)
        self.assertIn("{% if user['is_operator'] %}", template)
        self.assertIn('href="/admin/export/platform-backup"', template)
        self.assertIn("консистентный снимок всей SQLite-базы", template)
        self.assertIn("responsive-копии и кеши OG-превью не включаются", template)


if __name__ == "__main__":
    unittest.main(verbosity=2)
