"""Remote recovery publication uses local fixtures and a fully stubbed rclone."""

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "app")]
import backup_remote
import recovery


class RemoteBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="date4you-remote-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = self.root / "data"
        (data / "uploads").mkdir(parents=True)
        (data / "uploads" / "fixture1.webp").write_bytes(b"synthetic media")
        with closing(sqlite3.connect(data / "app.db")) as conn:
            conn.executescript("CREATE TABLE date_images(filename TEXT);"
                               "INSERT INTO date_images VALUES ('fixture1.webp');")
        self.bundle = self.root / "20260910T120000Z-aaaaaaaa"
        recovery.create_recovery(data, self.bundle, quiesced=True)
        self.remote = "fixture:recovery-test"
        self.calls = []
        self.names = ["20260907T120000Z-11111111", "20260908T120000Z-22222222",
                      "20260909T120000Z-33333333", self.bundle.name]
        self.complete = set(self.names)

    def run_stub(self, *args, **kwargs):
        args = tuple(str(value) for value in args)
        self.calls.append(args)
        self.assertEqual(args[0], "rclone")
        if args[1] == "lsf":
            if "--dirs-only" in args:
                return "\n".join(name + "/" for name in self.names)
            return "manifest.json\n" if args[2].split("/")[-1] in self.complete else ""
        return ""

    def publish(self):
        return backup_remote.publish(self.bundle, self.remote, 2)

    def test_data_check_precedes_marker_and_retention_removes_whole_old_bundles(self):
        with patch.object(backup_remote.release, "run", side_effect=self.run_stub):
            self.publish()
        self.assertEqual([args[1] for args in self.calls[:4]], ["copy", "check", "copyto", "check"])
        self.assertIn("--exclude", self.calls[0])
        self.assertEqual(self.calls[0][-1], "/manifest.json")
        self.assertIn("--download", self.calls[1])
        self.assertTrue(self.calls[2][-1].endswith("/manifest.json"))
        deleted = [args[2] for args in self.calls if args[1] == "purge"]
        self.assertEqual(deleted, [self.remote + "/recovery/" + name for name in self.names[:2]])

    def test_failed_upload_or_payload_check_never_publishes_marker_or_rotates(self):
        for failing in ("copy", "check"):
            with self.subTest(failing=failing):
                self.calls.clear()

                def fail(*args, **kwargs):
                    result = self.run_stub(*args, **kwargs)
                    if args[1] == failing:
                        raise subprocess.CalledProcessError(1, args)
                    return result

                with patch.object(backup_remote.release, "run", side_effect=fail):
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.publish()
                self.assertFalse(any(args[1] in ("copyto", "purge", "lsf") for args in self.calls))

    def test_failed_final_check_or_listing_never_rotates_previous_good(self):
        for failure in ("final_check", "listing"):
            with self.subTest(failure=failure):
                self.calls.clear()

                def fail(*args, **kwargs):
                    result = self.run_stub(*args, **kwargs)
                    if (failure == "final_check" and args[1] == "check" and "--exclude" not in args) \
                            or (failure == "listing" and args[1] == "lsf"):
                        raise subprocess.CalledProcessError(1, args)
                    return result

                with patch.object(backup_remote.release, "run", side_effect=fail):
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.publish()
                self.assertFalse(any(args[1] == "purge" for args in self.calls))
                marker_cleanup = [args for args in self.calls if args[1] == "deletefile"]
                self.assertEqual(len(marker_cleanup), 1 if failure == "final_check" else 0)

    def test_incomplete_or_unrecognized_remote_directories_do_not_count_for_retention(self):
        self.complete.remove(self.names[0])
        self.names.extend(["../outside", "legacy-uploads", "app-20200101.db"])
        with patch.object(backup_remote.release, "run", side_effect=self.run_stub):
            self.publish()
        deleted = [args[2] for args in self.calls if args[1] == "purge"]
        self.assertEqual(deleted, [self.remote + "/recovery/" + self.names[1]])

    def test_invalid_local_bundle_or_retention_fails_before_any_remote_calls(self):
        with patch.object(backup_remote.release, "run") as run:
            with self.assertRaises(ValueError):
                backup_remote.publish(self.bundle, self.remote, 0)
            (self.bundle / "uploads" / "fixture1.webp").write_bytes(b"corrupted")
            with self.assertRaises(ValueError):
                self.publish()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
