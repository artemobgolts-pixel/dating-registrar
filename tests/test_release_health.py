"""DEPLOY-04: liveness не зависит от writer lock, readiness ограничена по времени."""

import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import httpx
from starlette.testclient import TestClient

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-release-health-")
os.environ.update({
    "DATA_DIR": _DATA.name, "SECRET_KEY": "synthetic-release-health",
    "COOKIE_SECURE": "false", "DOMAIN": "testserver", "LOG_LEVEL": "CRITICAL",
    "APP_RELEASE": "a" * 40, "TG_BOT_TOKEN": "", "TG_CHAT_ID": "",
    "TG_BACKUP_CHAT_ID": "", "SENTRY_DSN": "",
})

import db
import main


class ReleaseHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="probe-", dir=_DATA.name)
        self.addCleanup(temporary.cleanup)
        patcher = mock.patch.object(db, "DB_PATH", Path(temporary.name) / "app.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_ready_reports_release_schema_and_does_not_change_data(self):
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            before = list(conn.iterdump())
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "ok": True, "release": "a" * 40,
            "schema_version": db.LATEST_VERSION,
            "expected_schema_version": db.LATEST_VERSION,
        })
        self.assertEqual(response.headers["cache-control"], "no-store")
        with closing(sqlite3.connect(db.DB_PATH, timeout=0)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.assertEqual(list(conn.iterdump()), before)
            conn.rollback()

    def test_locked_writer_is_not_ready_but_live_and_recovers(self):
        conn = sqlite3.connect(db.DB_PATH)
        self.addCleanup(conn.close)
        conn.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        response = self.client.get("/ready")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["reason"], "database_busy")
        self.assertEqual(self.client.get("/health").json(), {"ok": True})
        conn.rollback()
        self.assertEqual(self.client.get("/ready").status_code, 200)

    def test_liveness_never_opens_database_or_touches_disk(self):
        with mock.patch.object(sqlite3, "connect", side_effect=AssertionError("DB access")):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertFalse((db.DATA_DIR / ".health").exists())

    def test_missing_database_is_not_created(self):
        missing = db.DB_PATH.parent / "missing.db"
        with mock.patch.object(db, "DB_PATH", missing):
            response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["reason"], "database_unavailable")
        self.assertFalse(missing.exists())

    def test_schema_mismatch_is_not_ready_and_is_not_migrated(self):
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            conn.execute(f"PRAGMA user_version={db.LATEST_VERSION - 1}")
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["reason"], "schema_mismatch")
        self.assertEqual(response.json()["schema_version"], db.LATEST_VERSION - 1)
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             db.LATEST_VERSION - 1)

    def test_lock_probe_uses_worker_thread_and_event_loop_stays_available(self):
        writer = sqlite3.connect(db.DB_PATH)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        real_connect = sqlite3.connect
        probe_threads = []

        def tracked_connect(*args, **kwargs):
            probe_threads.append(threading.get_ident())
            return real_connect(*args, **kwargs)

        async def exercise():
            loop_thread = threading.get_ident()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://testserver",
            ) as client:
                with mock.patch.object(sqlite3, "connect", side_effect=tracked_connect):
                    pending = asyncio.create_task(client.get("/ready"))
                    beats = 0
                    while not pending.done():
                        await asyncio.sleep(0.005)
                        beats += 1
                    response = await pending
                self.assertEqual(response.status_code, 503)
                self.assertGreater(beats, 3, "event loop должен работать во время writer wait")
                self.assertTrue(probe_threads)
                self.assertNotIn(loop_thread, probe_threads)
                self.assertEqual((await client.get("/health")).status_code, 200)

        asyncio.run(exercise())
        writer.rollback()


if __name__ == "__main__":
    unittest.main(verbosity=2)
