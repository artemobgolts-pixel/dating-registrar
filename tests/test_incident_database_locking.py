"""Регрессия: writer-lock не превращает начало OAuth в 500 и не держится в сети."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-incident-locking-")
os.environ.update(
    DATA_DIR=_DATA.name, SECRET_KEY="synthetic-incident-locking",
    COOKIE_SECURE="false", DOMAIN="testserver", LOG_LEVEL="CRITICAL",
    TG_BOT_TOKEN="", TG_CHAT_ID="", TG_BACKUP_CHAT_ID="", SENTRY_DSN="",
)

import auth_routes
import db
import main
import places


class IncidentDatabaseLockingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="database-", dir=_DATA.name)
        self.addCleanup(temporary.cleanup)
        path = patch.object(db, "DB_PATH", Path(temporary.name) / "app.db")
        path.start()
        self.addCleanup(path.stop)
        db.init_db()
        main._rates.clear()
        self.client = TestClient(main.app, follow_redirects=False,
                                 raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        providers = patch.dict(auth_routes.OAUTH_PROVIDERS, {"google": ("test", "test")})
        providers.start()
        self.addCleanup(providers.stop)
        # Настоящие соединения/lock SQLite; только срок ожидания укорочен,
        # чтобы регрессия не тратила штатные 15 секунд на каждый запрос.
        connect = self.normal_connect = db.connect
        def short_wait_connection():
            conn = connect()
            conn.execute("PRAGMA busy_timeout=35")
            return conn
        connections = patch.object(db, "connect", side_effect=short_wait_connection)
        connections.start()
        self.addCleanup(connections.stop)

    def test_oauth_start_writer_busy_is_retryable_without_partial_flow(self):
        with closing(sqlite3.connect(db.DB_PATH)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            response = self.client.get("/auth/google")
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.headers.get("retry-after"), "1")
            self.assertNotIn("location", response.headers)
            self.assertIsNone(self.client.cookies.get("admin_s"),
                              "Неуспешный flow не должен выдавать browser proof")
            with closing(db.connect()) as reader:
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM oauth_flows").fetchone()[0], 0)
            writer.rollback()
        recovered = self.client.get("/auth/google")
        self.assertEqual(recovered.status_code, 303, recovered.text)
        self.assertTrue(recovered.headers["location"].startswith(
            "https://accounts.google.com/o/oauth2/v2/auth?"))
        with closing(db.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM oauth_flows").fetchone()[0], 1)

    def test_oauth_start_schema_error_is_not_reported_as_writer_busy(self):
        with closing(db.connect()) as conn:
            conn.execute("DROP TABLE oauth_flows")
            conn.commit()
        response = self.client.get("/auth/google")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("retry-after", response.headers)

    def test_oauth_start_default_writer_wait_is_bounded(self):
        # Здесь штатный SQLite timeout=15: HTTP-вход обязан ограничить его сам.
        with closing(sqlite3.connect(db.DB_PATH)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with patch.object(db, "connect", side_effect=self.normal_connect):
                started = time.monotonic()
                response = self.client.get("/auth/google")
                elapsed = time.monotonic() - started
            writer.rollback()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertLess(elapsed, 2.5, "Начало OAuth не должно ждать writer 15 секунд")

    def test_legacy_place_resolution_does_not_hold_writer_during_next_network_call(self):
        with closing(db.connect()) as conn:
            owner = conn.execute(
                "INSERT INTO users(display_name,created_at) VALUES('Fixture',?)",
                (main.now_iso(),),
            ).lastrowid
            conn.executemany(
                "INSERT INTO dates(owner_id,name,place,created_at) VALUES(?,?,?,?)",
                [(owner, str(index), f"https://yandex.ru/maps/?fixture={index}", main.now_iso())
                 for index in range(2)],
            )
            conn.commit()
        network_calls = []
        def resolve_name(url):
            # Включая второй вызов после первого успешного UPDATE: другое
            # соединение должно сразу получать writer, пока resolver в сети.
            with closing(sqlite3.connect(db.DB_PATH, timeout=0)) as other_writer:
                other_writer.execute("BEGIN IMMEDIATE")
                other_writer.rollback()
            network_calls.append(url)
            return f"Место {len(network_calls)}"
        with patch.object(places, "resolve_name", side_effect=resolve_name):
            self.assertEqual(places.repair_legacy_places(), 2)
        self.assertEqual(len(network_calls), 2)
        with closing(db.connect()) as conn:
            self.assertEqual([tuple(row) for row in conn.execute(
                "SELECT place,place_url FROM dates ORDER BY id")], [
                    (f"Место {index + 1}", f"https://yandex.ru/maps/?fixture={index}")
                    for index in range(2)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
