"""DEPLOY-06: offline synthetic DB + all original-media recovery roundtrips."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="date4you-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.uploads = self.data / "uploads"
        self.uploads.mkdir(parents=True)
        self.bundle = self.root / "recovery" / "snapshot"
        self.restored = self.root / "restored"
        with closing(sqlite3.connect(self.data / "app.db")) as conn:
            conn.executescript("""
                CREATE TABLE date_images (filename TEXT);
                INSERT INTO date_images VALUES ('event001.webp');
                CREATE TABLE date_videos (filename TEXT);
                INSERT INTO date_videos VALUES ('event001.mp4');
                CREATE TABLE users (avatar_path TEXT);
                INSERT INTO users VALUES ('avatar01.webp');
                CREATE TABLE categories (og_image TEXT);
                INSERT INTO categories VALUES ('preview1.webp');
                PRAGMA user_version=38;
            """)
        for name in ("event001.webp", "event001.mp4", "avatar01.webp", "preview1.webp", "orphan01.webp"):
            (self.uploads / name).write_bytes(("synthetic " + name).encode())
        (self.data / "responsive-v1").mkdir()
        (self.data / "responsive-v1" / "cache.webp").write_bytes(b"rebuildable")

    def snapshot(self):
        return recovery.create_recovery(self.data, self.bundle, quiesced=True,
                                        release="a" * 40, image="sha256:" + "b" * 64)

    def test_linked_media_roundtrip_after_db_mutation_and_media_deletion(self):
        expected = {p.name: p.read_bytes() for p in self.uploads.iterdir()}
        self.snapshot()
        manifest = recovery.verify_recovery(self.bundle)
        self.assertEqual(manifest["schema_version"], 38)
        self.assertEqual(manifest["release_sha"], "a" * 40)
        self.assertEqual(len(manifest["media_references"]), 4)
        self.assertFalse((self.bundle / "responsive-v1").exists())
        with closing(sqlite3.connect(self.data / "app.db")) as conn:
            conn.execute("DELETE FROM date_images")
            conn.commit()
        for path in self.uploads.iterdir():
            path.unlink()
        recovery.restore_recovery(self.bundle, self.restored)
        with closing(sqlite3.connect(self.restored / "app.db")) as conn:
            self.assertEqual(conn.execute("SELECT filename FROM date_images").fetchall(), [("event001.webp",)])
        self.assertEqual({p.name: p.read_bytes() for p in (self.restored / "uploads").iterdir()}, expected)
        self.assertEqual(list(self.uploads.iterdir()), [])  # Original live path stays mutated.
        recovery.verify_recovery(self.restored)

    def test_quiescence_acknowledgement_required(self):
        with self.assertRaisesRegex(ValueError, "quiesced"):
            recovery.create_recovery(self.data, self.bundle)
        self.assertFalse(self.bundle.exists())

    def test_missing_referenced_media_prevents_publication(self):
        (self.uploads / "avatar01.webp").unlink()
        with self.assertRaisesRegex(ValueError, "missing original media"):
            self.snapshot()
        self.assertFalse(self.bundle.exists())
        self.assertEqual(list(self.bundle.parent.iterdir()), [])

    def test_mid_media_copy_failure_preserves_previous_bundle_and_retries(self):
        self.snapshot()
        before = (self.bundle / "manifest.json").read_bytes()
        another = self.bundle.parent / "next"
        copy = recovery.shutil.copyfile
        calls = []

        def fail_later(source, target):
            calls.append(source)
            if len(calls) == 2:
                Path(target).write_bytes(b"partial")
                raise OSError("synthetic full disk")
            return copy(source, target)

        with patch.object(recovery.shutil, "copyfile", side_effect=fail_later):
            with self.assertRaises(OSError):
                recovery.create_recovery(self.data, another, quiesced=True)
        self.assertFalse(another.exists())
        self.assertEqual((self.bundle / "manifest.json").read_bytes(), before)
        self.assertEqual(list(self.bundle.parent.iterdir()), [self.bundle])
        recovery.create_recovery(self.data, another, quiesced=True)
        recovery.verify_recovery(another)

    def test_restore_refuses_existing_directory_even_empty(self):
        self.snapshot()
        self.restored.mkdir()
        with self.assertRaisesRegex(ValueError, "NEW"):
            recovery.restore_recovery(self.bundle, self.restored)
        with self.assertRaisesRegex(ValueError, "NEW"):
            recovery.restore_recovery(self.bundle, self.data)

    def test_tampered_media_or_incomplete_bundle_is_rejected(self):
        self.snapshot()
        asset = self.bundle / "uploads" / "event001.webp"
        original = asset.read_bytes()
        asset.write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "hash/size"):
            recovery.restore_recovery(self.bundle, self.restored)
        self.assertFalse(self.restored.exists())
        asset.write_bytes(original)
        asset.unlink()
        with self.assertRaisesRegex(ValueError, "inventory"):
            recovery.verify_recovery(self.bundle)

    def test_unexpected_file_or_manifest_path_is_rejected(self):
        self.snapshot()
        extra = self.bundle / "surprise.txt"
        extra.write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inventory"):
            recovery.verify_recovery(self.bundle)
        extra.unlink()
        path = self.bundle / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["files"]["../outside"] = {"size": 0, "sha256": "0" * 64}
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            recovery.restore_recovery(self.bundle, self.restored)
        self.assertFalse(self.restored.exists())

    def test_restore_copy_failure_does_not_publish_or_change_bundle(self):
        self.snapshot()
        with patch.object(recovery.shutil, "copyfile", side_effect=OSError("synthetic copy error")):
            with self.assertRaises(OSError):
                recovery.restore_recovery(self.bundle, self.restored)
        self.assertFalse(self.restored.exists())
        self.assertEqual(list(self.root.glob(".restored-*.partial")), [])
        recovery.verify_recovery(self.bundle)

    def test_output_inside_live_data_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            recovery.create_recovery(self.data, self.data / "snapshot", quiesced=True)

    def test_cli_create_verify_restore_works_without_app_environment(self):
        commands = [
            ["create", "--data", str(self.data), "--output", str(self.bundle), "--quiesced"],
            ["verify", str(self.bundle)],
            ["restore", str(self.bundle), "--destination", str(self.restored)],
        ]
        for arguments in commands:
            result = subprocess.run([sys.executable, str(APP / "recovery.py"), *arguments],
                                    capture_output=True, text=True, timeout=20,
                                    env={**os.environ, "DATA_DIR": str(self.root / "unused"),
                                         "TG_BOT_TOKEN": "", "SECRET_KEY": ""})
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "unused").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
