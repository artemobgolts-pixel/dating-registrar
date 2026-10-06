"""Бэкап реальной legacy/simple установки без fabricated release state."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "app")]
import backup_remote
import recovery


class LegacyBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="legacy-backup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / ".release"
        self.state.mkdir()
        self.data = self.root / "data"
        (self.data / "uploads").mkdir(parents=True)
        (self.data / "uploads" / "fixture.webp").write_bytes(b"original media")
        with closing(sqlite3.connect(self.data / "app.db")) as conn:
            conn.executescript("CREATE TABLE date_images(filename TEXT);"
                               "INSERT INTO date_images VALUES ('fixture.webp');")
        self.app = "a" * 64
        self.caddy = "c" * 64
        self.containers = {
            self.app: self.container(self.app, "app", [self.mount(self.data)]),
            self.caddy: self.container(self.caddy, "caddy", []),
        }
        self.calls = []
        self.helper = None
        self.bundle = None
        self.failure = None
        self.publish_failure = False
        self.stack = [patch.object(backup_remote.release, "ROOT", self.root),
                      patch.object(backup_remote.release, "STATE", self.state),
                      patch.object(backup_remote.release, "run", side_effect=self.docker_run),
                      patch.object(backup_remote, "publish", side_effect=self.publish),
                      patch.dict(os.environ, {"DATA_DIR": str(self.data),
                                             "RCLONE_REMOTE": "fixture:backup"})]
        for item in self.stack:
            item.start()
            self.addCleanup(item.stop)

    def mount(self, source, *, writable=True):
        return {"Type": "bind", "Source": str(source), "Destination": "/data", "RW": writable}

    def container(self, identity, service, mounts):
        return {"Id": identity, "Image": "sha256:" + identity, "Name": "/date4you-" + service + "-1",
                "State": {"Running": True}, "Mounts": mounts,
                "Config": {"Env": ["APP_RELEASE=" + "b" * 40], "Labels": {
                    "com.docker.compose.project": "date4you", "com.docker.compose.service": service}}}

    def publish(self, bundle, remote, keep):
        recovery.verify_recovery(bundle)
        self.assertTrue(self.containers[self.app]["State"]["Running"])
        self.assertTrue(self.containers[self.caddy]["State"]["Running"])
        self.calls.append(("publish",))
        if self.publish_failure:
            raise RuntimeError("synthetic cloud failure")

    def docker_run(self, *args, **kwargs):
        args = tuple(str(value) for value in args)
        self.calls.append(args)
        if self.failure:
            self.failure(args)
        self.assertEqual(args[0], "docker")
        command = args[1]
        if command == "ps":
            if "--filter" in args:
                filters = [args[i + 1] for i, value in enumerate(args[:-1]) if value == "--filter"]
                for value in filters:
                    if value.startswith("name="):
                        return "helper" if self.helper and self.helper in value else ""
                    if "service=" in value:
                        return self.app if value.endswith("app") else self.caddy
            return "\n".join(key for key, value in self.containers.items() if value["State"]["Running"])
        if command == "inspect":
            if "--format" in args:
                return "0"
            return json.dumps([self.containers[key] for key in args[2:]])
        if command == "stop":
            self.containers[args[-1]]["State"]["Running"] = False
        elif command == "create":
            self.assertFalse(self.containers[self.app]["State"]["Running"])
            self.assertFalse(self.containers[self.caddy]["State"]["Running"])
            self.assertTrue((self.state / "legacy-backup.json").is_file())
            self.helper = args[args.index("--name") + 1]
            self.bundle = self.state / "recovery" / args[args.index("--output") + 1].split("/")[-1]
            self.assertIn("--quiesced", args)
            self.assertEqual(args[args.index("--network") + 1], "none")
            self.assertIn(str(self.data) + ":/data:ro", args)
            self.assertIn("sha256:" + self.app, args)
        elif command == "start":
            if "-a" in args:
                recovery.create_recovery(self.data, self.bundle, quiesced=True)
            else:
                self.assertIsNone(self.helper, "writers restarted before helper cleanup")
                self.containers[args[-1]]["State"]["Running"] = True
        elif command == "rm":
            self.helper = None
        elif command == "exec":
            self.assertTrue(self.containers[self.app]["State"]["Running"])
            self.assertLessEqual(kwargs.get("timeout", 600), 60)
        else:
            self.fail("Unexpected command: " + str(args))
        return ""

    def test_cron_main_without_managed_state_creates_verified_bundle_and_delivers(self):
        backup_remote.main()
        manifest = recovery.verify_recovery(self.bundle)
        self.assertEqual(manifest["scope"], "app.db+uploads")
        self.assertFalse((self.state / "state.json").exists())
        self.assertFalse((self.state / "legacy-backup.json").exists())
        self.assertFalse((self.state / "operation.lock").exists())
        self.assertTrue(any(args[:2] == ("docker", "exec") and "ship_backup_to_tg" in args[-1]
                            for args in self.calls))

    def test_managed_failure_never_falls_back_to_legacy(self):
        (self.state / "state.json").write_text(json.dumps({"status": "maintenance"}))
        with patch.object(backup_remote.release, "backup", side_effect=ValueError("incomplete release")):
            with self.assertRaisesRegex(ValueError, "incomplete release"):
                backup_remote.main()
        self.assertFalse(any(args[:2] == ("docker", "stop") for args in self.calls))

    def test_managed_state_never_bypasses_unfinished_legacy_backup(self):
        (self.state / "state.json").write_text(json.dumps({"status": "active"}))
        (self.state / "legacy-backup.json").write_text("{}")
        with patch.object(backup_remote.release, "backup") as managed:
            with self.assertRaisesRegex(ValueError, "Незавершённый legacy"):
                backup_remote.main()
        managed.assert_not_called()

    def test_cloud_failure_still_attempts_explicit_telegram_delivery(self):
        self.publish_failure = True
        with self.assertRaisesRegex(RuntimeError, "synthetic cloud failure"):
            backup_remote.main()
        self.assertTrue(any(args[:2] == ("docker", "exec") and "ship_backup_to_tg" in args[-1]
                            for args in self.calls))

    def test_extra_writable_container_is_rejected_before_stopping_service(self):
        self.containers["e" * 64] = self.container("e" * 64, "other", [self.mount(self.data.parent)])
        with self.assertRaisesRegex(ValueError, "writer|writ|писател"):
            backup_remote.main()
        self.assertFalse(any(args[:2] == ("docker", "stop") for args in self.calls))

    def test_unrelated_writable_tmpfs_does_not_block_backup_from_project_directory(self):
        self.containers["e" * 64] = self.container("e" * 64, "other", [
            {"Type": "tmpfs", "Source": "", "Destination": "/run", "RW": True}])
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            backup_remote.main()
        finally:
            os.chdir(previous)
        recovery.verify_recovery(self.bundle)

    def test_snapshot_failure_restores_same_running_containers_after_cleanup(self):
        def fail(args):
            if args[:3] == ("docker", "start", "-a"):
                raise subprocess.CalledProcessError(1, args)
        self.failure = fail
        with self.assertRaises(subprocess.CalledProcessError):
            backup_remote.main()
        self.assertTrue(self.containers[self.app]["State"]["Running"])
        self.assertTrue(self.containers[self.caddy]["State"]["Running"])
        self.assertFalse((self.state / "legacy-backup.json").exists())

    def test_cleanup_failure_preserves_journal_and_stopped_writers_then_retries(self):
        def fail(args):
            if args[:2] == ("docker", "rm"):
                raise RuntimeError("synthetic helper cleanup failure")
        self.failure = fail
        with self.assertRaisesRegex(RuntimeError, "cleanup failure"):
            backup_remote.main()
        self.assertTrue((self.state / "legacy-backup.json").exists())
        self.assertFalse(self.containers[self.app]["State"]["Running"])
        self.assertFalse(self.containers[self.caddy]["State"]["Running"])
        self.failure = None
        backup_remote.main()
        self.assertTrue(self.containers[self.app]["State"]["Running"])
        self.assertFalse((self.state / "legacy-backup.json").exists())

    def test_nonzero_helper_exit_is_not_published_and_restores_services(self):
        original = self.docker_run
        def docker_run(*args, **kwargs):
            result = original(*args, **kwargs)
            return "7" if args[:2] == ("docker", "inspect") and "--format" in args else result
        with patch.object(backup_remote.release, "run", side_effect=docker_run):
            with self.assertRaisesRegex(RuntimeError, "кодом 7"):
                backup_remote.main()
        self.assertNotIn(("publish",), self.calls)
        self.assertTrue(self.containers[self.app]["State"]["Running"])
        self.assertTrue(self.containers[self.caddy]["State"]["Running"])

    def test_readiness_failure_keeps_ingress_closed_and_resume_journal(self):
        with patch.object(backup_remote.release, "poll", side_effect=RuntimeError("readiness timeout")):
            with self.assertRaisesRegex(RuntimeError, "readiness timeout"):
                backup_remote.main()
        self.assertIsNone(self.helper)
        self.assertTrue(self.containers[self.app]["State"]["Running"])
        self.assertFalse(self.containers[self.caddy]["State"]["Running"])
        self.assertTrue((self.state / "legacy-backup.json").exists())
        backup_remote.main()
        self.assertTrue(self.containers[self.caddy]["State"]["Running"])

    def test_interrupted_backup_never_restarts_changed_container_image(self):
        def fail(args):
            if args[:2] == ("docker", "rm"):
                raise RuntimeError("cleanup failure")
        self.failure = fail
        with self.assertRaises(RuntimeError):
            backup_remote.main()
        self.containers[self.app]["Image"] = "sha256:" + "d" * 64
        self.failure = None
        with self.assertRaisesRegex(ValueError, "изменились"):
            backup_remote.main()
        self.assertFalse(self.containers[self.app]["State"]["Running"])
        self.assertFalse(self.containers[self.caddy]["State"]["Running"])
        self.assertTrue((self.state / "legacy-backup.json").exists())

    def test_data_mount_mismatch_fails_before_stopping_services(self):
        self.containers[self.app]["Mounts"] = [self.mount(self.root / "other-data")]
        with self.assertRaisesRegex(ValueError, "DATA_DIR"):
            backup_remote.main()
        self.assertFalse(any(args[:2] == ("docker", "stop") for args in self.calls))

    def simple_config(self):
        simple = self.state / "simple"
        simple.mkdir()
        (simple / "compose.json").write_text("{}")
        return simple / "update.lock"

    def test_preexisting_updater_lock_blocks_backup_and_preserves_foreign_lock(self):
        lock = self.simple_config()
        lock.mkdir()
        marker = lock / "foreign-owner"
        marker.write_text("updater")
        with self.assertRaisesRegex(ValueError, "update.lock"):
            backup_remote.main()
        self.assertEqual(marker.read_text(), "updater")
        self.assertEqual(self.calls, [])
        self.assertFalse((self.state / "operation.lock").exists())

    def test_simple_lock_held_through_delivery_and_released_on_success(self):
        lock = self.simple_config()
        original = self.docker_run
        def docker_run(*args, **kwargs):
            self.assertTrue(lock.is_dir())
            return original(*args, **kwargs)
        with patch.object(backup_remote.release, "run", side_effect=docker_run):
            backup_remote.main()
        self.assertFalse(lock.exists())

    def test_simple_lock_released_after_destination_error(self):
        lock = self.simple_config()
        self.publish_failure = True
        with self.assertRaisesRegex(RuntimeError, "cloud failure"):
            backup_remote.main()
        self.assertFalse(lock.exists())
        self.assertFalse((self.state / "operation.lock").exists())

    def test_stale_simple_lock_blocks_resume_and_leaves_legacy_journal_untouched(self):
        lock = self.simple_config()
        lock.mkdir()
        journal = self.state / "legacy-backup.json"
        journal.write_text('{"interrupted":true}')
        with self.assertRaisesRegex(ValueError, "update.lock"):
            backup_remote.main()
        self.assertEqual(journal.read_text(), '{"interrupted":true}')
        self.assertTrue(lock.is_dir())
        self.assertEqual(self.calls, [])

    def test_disabled_telegram_does_not_create_or_send_snapshot(self):
        backup = types.SimpleNamespace(make_backup=lambda: self.fail("disabled backup was created"))
        tasks = types.SimpleNamespace(TG_BACKUP_CHAT_ID="", ship_backup_to_tg=lambda value: self.fail("sent"))
        with patch.dict(sys.modules, {"backup": backup, "tasks": tasks}):
            with self.assertRaises(SystemExit) as result:
                exec(backup_remote.TELEGRAM_CODE)
        self.assertEqual(result.exception.code, 0)

    def test_failed_enabled_telegram_sets_nonzero_status(self):
        backup = types.SimpleNamespace(make_backup=lambda: self.bundle)
        tasks = types.SimpleNamespace(TG_BACKUP_CHAT_ID="fixture-recipient", ship_backup_to_tg=lambda value: False)
        with patch.dict(sys.modules, {"backup": backup, "tasks": tasks}):
            with self.assertRaises(SystemExit) as result:
                exec(backup_remote.TELEGRAM_CODE)
        self.assertEqual(result.exception.code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
