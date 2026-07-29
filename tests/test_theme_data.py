#!/usr/bin/env python3
"""Контракт хранения и валидации независимых оформлений."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from starlette.datastructures import UploadFile


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

# db создаёт DATA_DIR при импорте, поэтому изолируем его заранее.
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-theme-import-")
os.environ.update(
    {
        "DATA_DIR": _IMPORT_DATA.name,
        "COOKIE_SECURE": "false",
        "DOMAIN": "theme.test",
        "SECRET_KEY": "theme-test-secret",
        "TG_BOT_TOKEN": "",
        "TG_CHAT_ID": "",
        "TG_BOT_USERNAME": "date4you_theme_test_bot",
        "TG_WEBHOOK_SECRET": "theme-hook-secret",
        "OPERATOR_TG_IDS": "26002",
    }
)

import admin_routes  # noqa: E402
import appearance  # noqa: E402
import db  # noqa: E402
import images  # noqa: E402


class ThemeMigrationTests(unittest.TestCase):
    def test_v26_preserves_old_categories_and_defaults_new_records_to_friends(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    display_name TEXT
                );
                CREATE TABLE categories (
                    id INTEGER PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL
                );
                INSERT INTO users(id, display_name) VALUES(1, 'Старый пользователь');
                INSERT INTO categories(id, owner_id, name)
                    VALUES(1, 1, 'Существующая категория');
                """
            )
            conn.executescript(db.MIGRATIONS[26])

            self.assertEqual(
                conn.execute(
                    "SELECT category_skin FROM categories WHERE id=1"
                ).fetchone()[0],
                "romantic",
            )
            self.assertEqual(
                conn.execute("SELECT admin_skin FROM users WHERE id=1").fetchone()[0],
                "friends",
            )

            conn.execute("INSERT INTO users(id, display_name) VALUES(2, 'Новый')")
            conn.execute(
                "INSERT INTO categories(id, owner_id, name) VALUES(2, 2, 'Новая')"
            )
            self.assertEqual(
                conn.execute(
                    "SELECT category_skin FROM categories WHERE id=2"
                ).fetchone()[0],
                "friends",
            )
            self.assertEqual(
                conn.execute("SELECT admin_skin FROM users WHERE id=2").fetchone()[0],
                "friends",
            )

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE categories SET category_skin='evil' WHERE id=2")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE users SET admin_skin='evil' WHERE id=2")
        finally:
            conn.close()

    def test_fresh_schema_uses_friends_defaults(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            uid = conn.execute(
                "INSERT INTO users(telegram_id, display_name, created_at) VALUES(?,?,?)",
                (26001, "Новый пользователь", "2030-01-01T00:00:00"),
            ).lastrowid
            cid = conn.execute(
                "INSERT INTO categories(owner_id, name, created_at) VALUES(?,?,?)",
                (uid, "Новая категория", "2030-01-01T00:00:00"),
            ).lastrowid
            self.assertEqual(
                conn.execute(
                    "SELECT category_skin FROM categories WHERE id=?", (cid,)
                ).fetchone()[0],
                "friends",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT admin_skin FROM users WHERE id=?", (uid,)
                ).fetchone()[0],
                "friends",
            )
        finally:
            conn.close()


class ThemePreviewTests(unittest.TestCase):
    def test_friends_preview_is_exact_lightweight_og_image(self):
        path = APP / "static" / "og-friends.jpg"
        self.assertTrue(path.is_file())
        self.assertLess(path.stat().st_size, 200_000)
        with images.Image.open(path) as preview:
            self.assertEqual(preview.format, "JPEG")
            self.assertEqual(preview.size, (1200, 630))

    def test_friends_install_icons_do_not_fall_back_to_romantic_assets(self):
        static = APP / "static"
        expected = {
            "icon-friends-192.png": (192, 192),
            "icon-friends-512.png": (512, 512),
            "apple-touch-icon-friends.png": (180, 180),
        }
        for filename, size in expected.items():
            with self.subTest(filename=filename), images.Image.open(
                    static / filename) as icon:
                self.assertEqual(icon.size, size)

        manifest = json.loads((static / "manifest-friends.json").read_text("utf-8"))
        sources = {entry["src"] for entry in manifest["icons"]}
        self.assertEqual(
            sources,
            {f"/static/{filename}" for filename in expected},
        )
        self.assertEqual(manifest["theme_color"], "#554eae")

    def test_collage_cache_is_separate_for_each_skin(self):
        filenames = ["first.webp", "second.webp"]
        friends = images.og_collage_name(filenames, appearance.FRIENDS)
        romantic = images.og_collage_name(filenames, appearance.ROMANTIC)
        old_digest = hashlib.sha256(
            ("brand-v5\n" + "\n".join(filenames)).encode()
        ).hexdigest()[:24]
        self.assertEqual(romantic, f"og_{old_digest}.webp")
        self.assertNotEqual(friends, romantic)
        self.assertEqual(
            images.og_collage_name(filenames, "invalid-skin"),
            romantic,
        )


class ThemeRouteDataTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizer_never_returns_arbitrary_skin(self):
        self.assertEqual(appearance.normalize_skin("friends"), "friends")
        self.assertEqual(appearance.normalize_skin("romantic"), "romantic")
        self.assertEqual(
            appearance.normalize_skin("injected-class", default="romantic"),
            "romantic",
        )
        with self.assertRaises(ValueError):
            appearance.normalize_skin("injected-class", default="also-invalid")

    async def test_json_import_preserves_skin_and_legacy_defaults_to_romantic(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.executescript(db.SCHEMA)
            uid = conn.execute(
                "INSERT INTO users(telegram_id, display_name, is_operator, created_at) "
                "VALUES(?,?,1,?)",
                (26002, "Оператор", "2030-01-01T00:00:00"),
            ).lastrowid
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            request = SimpleNamespace(state=SimpleNamespace(user=user))
            payload = {
                "categories": [
                    {"id": 10, "name": "Старый экспорт"},
                    {"id": 11, "name": "Дружеская", "category_skin": "friends"},
                    {"id": 12, "name": "Романтическая", "category_skin": "romantic"},
                    {"id": 13, "name": "Некорректная", "category_skin": "arbitrary"},
                ],
                "dates": [],
            }
            upload = UploadFile(
                filename="export.json",
                file=io.BytesIO(json.dumps(payload).encode("utf-8")),
            )

            response = await admin_routes.import_json(request, upload, conn)
            self.assertEqual(response.status_code, 303)
            skins = dict(
                conn.execute("SELECT name, category_skin FROM categories ORDER BY id")
            )
            self.assertEqual(skins["Старый экспорт"], "romantic")
            self.assertEqual(skins["Дружеская"], "friends")
            self.assertEqual(skins["Романтическая"], "romantic")
            self.assertEqual(skins["Некорректная"], "romantic")

            exported = admin_routes._full_dump(conn, uid)
            exported_skins = {
                category["name"]: category["category_skin"]
                for category in exported["categories"]
            }
            self.assertEqual(exported_skins, skins)
        finally:
            conn.close()


class ThemeFormPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        self.uid = self.conn.execute(
            "INSERT INTO users(telegram_id, display_name, is_operator, created_at) "
            "VALUES(?,?,1,?)",
            (26003, "Владелец", "2030-01-01T00:00:00"),
        ).lastrowid
        self.cid = self.conn.execute(
            "INSERT INTO categories(owner_id, name, created_at) VALUES(?,?,?)",
            (self.uid, "Категория", "2030-01-01T00:00:00"),
        ).lastrowid
        self.request = SimpleNamespace(
            state=SimpleNamespace(
                user=self.conn.execute(
                    "SELECT * FROM users WHERE id=?", (self.uid,)
                ).fetchone()
            )
        )

    def tearDown(self):
        self.conn.close()

    def test_category_save_accepts_skin_and_rejects_arbitrary_value(self):
        admin_routes.category_rename(
            self.cid,
            self.request,
            name="Категория",
            description="",
            og_title="",
            og_desc="",
            category_skin="romantic",
            og_image=None,
            conn=self.conn,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT category_skin FROM categories WHERE id=?", (self.cid,)
            ).fetchone()[0],
            "romantic",
        )

        admin_routes.category_rename(
            self.cid,
            self.request,
            name="Категория",
            description="",
            og_title="",
            og_desc="",
            category_skin="arbitrary-class",
            og_image=None,
            conn=self.conn,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT category_skin FROM categories WHERE id=?", (self.cid,)
            ).fetchone()[0],
            "romantic",
        )

    def test_profile_save_accepts_skin_and_rejects_arbitrary_value(self):
        admin_routes.profile_save(
            self.request,
            display_name="Владелец",
            birth_date="",
            gender="",
            cursor_effects=None,
            admin_skin="romantic",
            avatar=None,
            conn=self.conn,
        )
        self.request.state.user = self.conn.execute(
            "SELECT * FROM users WHERE id=?", (self.uid,)
        ).fetchone()
        self.assertEqual(self.request.state.user["admin_skin"], "romantic")

        admin_routes.profile_save(
            self.request,
            display_name="Владелец",
            birth_date="",
            gender="",
            cursor_effects=None,
            admin_skin="arbitrary-class",
            avatar=None,
            conn=self.conn,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT admin_skin FROM users WHERE id=?", (self.uid,)
            ).fetchone()[0],
            "romantic",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
