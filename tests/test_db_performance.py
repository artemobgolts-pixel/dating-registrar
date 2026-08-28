#!/usr/bin/env python3
"""Миграции и планы запросов для горячих SQLite-путей."""

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

V32_INDEXES = {
    "idx_dates_autoarchive_ends_due",
    "idx_dates_autoarchive_starts_due",
    "idx_dates_autoarchive_end_fallback_start",
    "idx_date_wants_user_updated",
    "idx_reports_open_date_target",
    "idx_notification_outbox_expiry",
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
                for index in V25_INDEXES | V32_INDEXES:
                    conn.execute(f"DROP INDEX IF EXISTS {index}")
                # init_db выше создал свежую схему. Для честной эмуляции старой
                # v24 базы убираем объекты следующих миграций, иначе их ALTER
                # TABLE при повторном прогоне закономерно увидит дубль.
                conn.execute("ALTER TABLE categories DROP COLUMN category_skin")
                conn.execute("ALTER TABLE users DROP COLUMN admin_skin")
                conn.execute("ALTER TABLE notification_outbox DROP COLUMN action_url")
                conn.execute("ALTER TABLE notification_outbox DROP COLUMN action_label")
                conn.execute("DROP TABLE date_reviews")
                conn.execute("DROP TABLE date_wants")
                conn.execute("DROP TABLE notification_preferences")
                conn.execute("DROP TABLE review_queue")
                conn.execute("ALTER TABLE categories DROP COLUMN use_default_preview")
                conn.execute("ALTER TABLE users DROP COLUMN birth_date_public")
                conn.execute("ALTER TABLE users DROP COLUMN gender_public")
                conn.execute("ALTER TABLE categories DROP COLUMN show_participants")
                # В реальной v24 колонка is_public имела DEFAULT 1. Свежая
                # v31-фикстура уже использует приватный default, поэтому
                # восстанавливаем историческое состояние до миграции.
                conn.execute("UPDATE dates SET is_public=1")
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
                states = {
                    name: (is_draft, is_public)
                    for name, is_draft, is_public in conn.execute(
                        "SELECT name, is_draft, is_public FROM dates"
                    )
                }
                self.assertEqual(states["repair"], (0, 1))
                self.assertEqual(states["guest"], (1, 1))
                self.assertEqual(states["archived"], (1, 1))
                self.assertEqual(states["unlinked"], (0, 0))
                self.assertEqual(states["deadline-conflict"], (1, 0))
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertTrue((V25_INDEXES - {"idx_dates_autoarchive_active"})
                                | V32_INDEXES <= indexes)
                self.assertNotIn("idx_dates_autoarchive_active", indexes)
                self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())
            finally:
                conn.close()

    def test_hot_queries_use_expected_indexes(self):
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
                    "idx_dates_autoarchive_ends_due",
                    "SELECT id, starts_at, ends_at FROM dates "
                    "INDEXED BY idx_dates_autoarchive_ends_due "
                    "WHERE archived_at IS NULL AND ends_at IS NOT NULL "
                    "AND ends_at<?",
                    ("2030-01-01T00:00:00",),
                ),
                (
                    "idx_dates_autoarchive_starts_due",
                    "SELECT id, starts_at, ends_at FROM dates "
                    "INDEXED BY idx_dates_autoarchive_starts_due "
                    "WHERE archived_at IS NULL AND ends_at IS NULL "
                    "AND starts_at IS NOT NULL AND starts_at<? "
                    "AND owner_id=?",
                    ("2030-01-01T00:00:00", 1),
                ),
                (
                    "idx_dates_autoarchive_end_fallback_start",
                    "SELECT id,starts_at,ends_at FROM dates "
                    "INDEXED BY idx_dates_autoarchive_end_fallback_start "
                    "WHERE archived_at IS NULL AND ends_at IS NOT NULL "
                    "AND starts_at IS NOT NULL AND starts_at<? AND ends_at>=?",
                    ("2030-01-01T00:00:00", "2030-01-01T12:00:00"),
                ),
                (
                    "idx_date_wants_user_updated",
                    "SELECT date_id FROM date_wants WHERE user_id=? "
                    "ORDER BY updated_at DESC",
                    (1,),
                ),
                (
                    "idx_reports_open_date_target",
                    "SELECT target_id, COUNT(*) FROM reports "
                    "INDEXED BY idx_reports_open_date_target "
                    "WHERE target_type='date' AND status='open' GROUP BY target_id",
                    (),
                ),
                (
                    "idx_notification_outbox_expiry",
                    "UPDATE notification_outbox "
                    "INDEXED BY idx_notification_outbox_expiry SET cancelled_at=? "
                    "WHERE sent_at IS NULL AND cancelled_at IS NULL "
                    "AND expires_at IS NOT NULL AND expires_at<=?",
                    ("2030-01-01T00:00:00", "2030-01-01T00:00:00"),
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

            completed = plan(
                conn,
                "SELECT DISTINCT d.id FROM categories c "
                "JOIN dates d ON d.id=c.winner_date_id "
                "WHERE c.voting_status='resolved' AND c.winner_date_id IS NOT NULL "
                "AND d.archived_at IS NULL AND EXISTS ("
                " SELECT 1 FROM bookings b JOIN date_reviews r "
                " ON r.date_id=b.date_id AND r.user_id=b.user_id "
                " WHERE b.category_id=c.id AND b.date_id=c.winner_date_id "
                " AND b.user_id IS NOT NULL "
                " AND b.participation_withdrawn_at IS NULL) "
                "AND NOT EXISTS (SELECT 1 FROM date_categories pending_dc "
                " JOIN categories pending_c ON pending_c.id=pending_dc.category_id "
                " WHERE pending_dc.date_id=d.id "
                " AND pending_c.voting_status IN ('open','tie'))",
            )
            self.assertIn("idx_categories_winner_date", completed)
            self.assertIn("idx_book_cat_user", completed)
            self.assertNotIn("SCAN b", completed)
        finally:
            conn.close()

if __name__ == "__main__":
    unittest.main(verbosity=2)
