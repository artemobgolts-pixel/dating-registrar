#!/usr/bin/env python3
"""Миграции v25–v26 и планы запросов для горячих SQLite-путей."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

# db создаёт DATA_DIR при импорте, поэтому изолируем его до import.
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-db-perf-import-")
os.environ["DATA_DIR"] = _IMPORT_DATA.name

import db  # noqa: E402


V25_INDEXES = {
    "idx_di_filename_date",
    "idx_dv_filename_date",
    "idx_categories_og_image",
    "idx_book_cat_user",
    "idx_categories_open_deadline",
    "idx_dates_autoarchive_active",
    "idx_q_date_read",
}


def plan(conn: sqlite3.Connection, sql: str, params=()) -> str:
    return "\n".join(
        row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    )


class DatabasePerformanceMigrationTests(unittest.TestCase):
    def test_v25_repair_is_selective_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="date4you-v24-perf-") as tmp:
            path = Path(tmp) / "app.db"
            old_path = db.DB_PATH
            try:
                db.DB_PATH = path
                db.init_db()
                conn = db.connect()
                owner_id = conn.execute(
                    "INSERT INTO users(telegram_id, display_name, created_at) "
                    "VALUES(?,?,?)",
                    (99001, "Владелец", "2030-01-01T00:00:00"),
                ).lastrowid
                category_id = conn.execute(
                    "INSERT INTO categories(owner_id, name, created_at) VALUES(?,?,?)",
                    (owner_id, "Основная", "2030-01-01T00:00:00"),
                ).lastrowid
                open_category_id = conn.execute(
                    "INSERT INTO categories("
                    "owner_id, name, choice_mode, voting_deadline, voting_status, created_at"
                    ") VALUES(?,?,?,?,?,?)",
                    (
                        owner_id,
                        "Открытая",
                        "multiple",
                        "2030-02-10T10:00:00",
                        "open",
                        "2030-01-01T00:00:00",
                    ),
                ).lastrowid

                rows = (
                    ("repair", "admin", None, None),
                    ("guest", "guest", None, None),
                    ("archived", "admin", "2030-01-02T00:00:00", None),
                    ("unlinked", "admin", None, None),
                    # Публикацию этой строки отверг бы v24-триггер дедлайна.
                    ("deadline-conflict", "admin", None, "2030-02-01T10:00:00"),
                )
                ids: dict[str, int] = {}
                for name, origin, archived_at, starts_at in rows:
                    ids[name] = int(conn.execute(
                        "INSERT INTO dates("
                        "owner_id, name, starts_at, origin, is_draft, archived_at, created_at"
                        ") VALUES(?,?,?,?,?,?,?)",
                        (
                            owner_id,
                            name,
                            starts_at,
                            origin,
                            1,
                            archived_at,
                            "2030-01-01T00:00:00",
                        ),
                    ).lastrowid)
                for name in ("repair", "guest", "archived"):
                    conn.execute(
                        "INSERT INTO date_categories(date_id, category_id) VALUES(?,?)",
                        (ids[name], category_id),
                    )
                conn.execute(
                    "INSERT INTO date_categories(date_id, category_id) VALUES(?,?)",
                    (ids["deadline-conflict"], open_category_id),
                )
                for index in V25_INDEXES:
                    conn.execute(f"DROP INDEX {index}")
                # init_db выше создал свежую v26-схему. Для честной эмуляции
                # старой v24 базы убираем две колонки следующей миграции, иначе
                # после проверки v25 ALTER TABLE v26 закономерно увидит дубль.
                conn.execute("ALTER TABLE categories DROP COLUMN category_skin")
                conn.execute("ALTER TABLE users DROP COLUMN admin_skin")
                conn.execute("PRAGMA user_version=24")
                conn.commit()
                conn.close()

                db.init_db()
                # Повторный startup не меняет данные и не конфликтует с индексами.
                db.init_db()
            finally:
                db.DB_PATH = old_path

            conn = sqlite3.connect(path)
            try:
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0],
                    db.LATEST_VERSION,
                )
                drafts = dict(conn.execute("SELECT name, is_draft FROM dates"))
                self.assertEqual(drafts["repair"], 0)
                self.assertEqual(drafts["guest"], 1)
                self.assertEqual(drafts["archived"], 1)
                self.assertEqual(drafts["unlinked"], 1)
                self.assertEqual(drafts["deadline-conflict"], 1)
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertTrue(V25_INDEXES <= indexes)
                self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())
            finally:
                conn.close()

    def test_hot_queries_use_v25_indexes(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            cases = (
                (
                    "idx_di_filename_date",
                    "SELECT 1 FROM date_images di JOIN dates d ON d.id=di.date_id "
                    "WHERE di.filename=? AND d.owner_id=?",
                    ("image.webp", 1),
                ),
                (
                    "idx_dv_filename_date",
                    "SELECT 1 FROM date_videos dv JOIN dates d ON d.id=dv.date_id "
                    "WHERE dv.filename=? AND d.owner_id=?",
                    ("video.mp4", 1),
                ),
                (
                    "idx_categories_og_image",
                    "SELECT 1 FROM categories WHERE og_image=? AND owner_id=?",
                    ("og.webp", 1),
                ),
                (
                    "idx_book_cat_user",
                    "SELECT 1 FROM bookings "
                    "WHERE category_id=? AND user_id=? LIMIT 1",
                    (1, 2),
                ),
                (
                    "idx_categories_open_deadline",
                    "SELECT id FROM categories "
                    "WHERE voting_status='open' AND closed_at IS NULL "
                    "AND voting_deadline<=?",
                    ("2030-01-01T00:00:00",),
                ),
                (
                    "idx_dates_autoarchive_active",
                    "SELECT id, starts_at, ends_at FROM dates "
                    "WHERE archived_at IS NULL "
                    "AND (starts_at IS NOT NULL OR ends_at IS NOT NULL)",
                    (),
                ),
                (
                    "idx_dates_autoarchive_active",
                    "SELECT id, starts_at, ends_at FROM dates "
                    "WHERE archived_at IS NULL "
                    "AND (starts_at IS NOT NULL OR ends_at IS NOT NULL) "
                    "AND owner_id=?",
                    (1,),
                ),
                (
                    "idx_q_date_read",
                    "SELECT COUNT(*) FROM questions WHERE is_read=0 "
                    "AND date_id IN (SELECT id FROM dates WHERE owner_id=?)",
                    (1,),
                ),
            )
            for index, sql, params in cases:
                with self.subTest(index=index, sql=sql):
                    self.assertIn(index, plan(conn, sql, params))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
