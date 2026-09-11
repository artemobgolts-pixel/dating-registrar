"""DEPLOY-02: real SQLite backup publication and safe synthetic failure injection."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import backup


class AtomicBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="date4you-atomic-backup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "app.db"
        self.backups = self.root / "backups"
        self.settings = patch.multiple(backup, DB_PATH=self.source, BACKUP_DIR=self.backups, KEEP=2)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, payload BLOB)")
            conn.executemany("INSERT INTO fixture(payload) VALUES (?)", [(b"x" * 4096,)] * 400)
            conn.commit()

    def assert_no_partials(self):
        self.assertEqual(list(self.backups.glob("*.partial*")), [])

    def test_success_publishes_valid_standalone_snapshot_and_is_fresh(self):
        snapshot = backup.make_backup()
        with closing(sqlite3.connect(snapshot)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertEqual(conn.execute("SELECT count(*) FROM fixture").fetchone(), (400,))
        self.assertIsNone(backup.make_backup_if_stale())
        self.assert_no_partials()

    def test_mid_sqlite_backup_failure_preserves_good_and_permits_retry(self):
        previous = backup.make_backup()
        original = previous.read_bytes()
        os.utime(previous, (1, 1))
        connect = sqlite3.connect
        progress_seen = []

        class InterruptedSource:
            def __init__(self, connection):
                self.connection = connection

            def backup(self, target, **kwargs):
                def interrupted(status, remaining, total):
                    progress_seen.append((remaining, total))
                    if remaining:
                        raise OSError("synthetic mid-copy disk failure")
                kwargs["progress"] = interrupted
                return self.connection.backup(target, **kwargs)

            def close(self):
                self.connection.close()

        def fail_source(*args, **kwargs):
            connection = connect(*args, **kwargs)
            return InterruptedSource(connection) if kwargs.get("uri") else connection

        with patch.object(backup.sqlite3, "connect", side_effect=fail_source):
            with self.assertRaisesRegex(OSError, "synthetic mid-copy"):
                backup.make_backup_if_stale()
        self.assertTrue(progress_seen)
        self.assertGreater(progress_seen[0][0], 0)
        self.assertEqual(previous.read_bytes(), original)
        self.assertEqual(list(self.backups.glob("app-*.db")), [previous])
        self.assert_no_partials()
        self.assertIsNotNone(backup.make_backup_if_stale())

    def test_publication_failure_does_not_rotate_or_suppress_retry(self):
        previous = backup.make_backup()
        os.utime(previous, (1, 1))
        with patch.object(backup.os, "replace", side_effect=OSError("synthetic rename failure")):
            with self.assertRaises(OSError):
                backup.make_backup_if_stale()
        self.assertTrue(previous.exists())
        self.assert_no_partials()
        self.assertIsNotNone(backup.make_backup_if_stale())

    def test_legacy_final_looking_partial_and_hidden_partial_are_not_fresh(self):
        self.backups.mkdir()
        legacy = self.backups / "app-20990101-000000.db"
        legacy.write_bytes(b"incomplete legacy backup")
        partial = self.backups / ".app-unrelated.partial"
        partial.write_bytes(b"interrupted unrelated process")
        self.assertIsNotNone(backup.make_backup_if_stale())
        self.assertTrue(legacy.exists())
        self.assertTrue(partial.exists())

    def test_retention_runs_only_after_success_and_retains_newest(self):
        first = backup.make_backup()
        os.utime(first, (1, 1))
        second = backup.make_backup()
        third = backup.make_backup()
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(third.exists())
        with patch.object(backup, "KEEP", 1):
            fourth = backup.make_backup()
        self.assertEqual(list(self.backups.glob("app-*.db")), [fourth])

    def test_missing_source_fails_without_creating_database(self):
        self.source.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            backup.make_backup()
        self.assertFalse(self.source.exists())
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_live_wal_committed_rows_are_in_snapshot(self):
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("INSERT INTO fixture(payload) VALUES ('committed in WAL')")
            conn.commit()
            self.assertTrue(Path(str(self.source) + "-wal").exists())
            snapshot = backup.make_backup()
            with closing(sqlite3.connect(snapshot)) as saved:
                self.assertEqual(saved.execute("SELECT count(*) FROM fixture").fetchone(), (401,))
                self.assertEqual(saved.execute("PRAGMA journal_mode").fetchone(), ("delete",))
            self.assertFalse(Path(str(snapshot) + "-wal").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
