"""Без Docker: fault injection границ release state machine и immutable artifacts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release

A, B = "a" * 40, "b" * 40
REAL_MANIFEST = release.manifest


class ReleaseProcedureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="release-procedure-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / ".release"
        self.state.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(release, "ROOT", self.root).start()
        patch.object(release, "STATE", self.state).start()
        self.values = {sha: {"release": sha, "image": "sha256:" + sha[:1] * 64,
                            "caddy_image": "sha256:" + "c" * 64, "schema": 38,
                            "legacy": False} for sha in (A, B)}
        self.calls = []
        patch.object(release, "manifest", side_effect=lambda sha: self.values[sha]).start()
        patch.object(release, "compose", side_effect=lambda *a, **kw: self.calls.append(a)).start()
        patch.object(release, "snapshot", side_effect=self.snapshot).start()
        patch.object(release, "install_configuration", side_effect=lambda v: self.calls.append(("install", v["release"]))).start()
        patch.object(release, "ready", side_effect=lambda v: self.calls.append(("ready", v["release"]))).start()
        release.write_json(self.state / "state.json", {"status": "active", "current": A, "previous": B})

    def snapshot(self, value, helper=None):
        self.calls.append(("snapshot", value["release"]))
        return "synthetic-paired-recovery"

    def test_success_snapshots_before_migration_and_opens_only_after_ready(self):
        release.activate(B)
        self.assertEqual(self.calls[:4], [("stop", "caddy"), ("stop", "app"),
            ("snapshot", A), ("install", B)])
        self.assertLess(self.calls.index(("ready", B)), self.calls.index(("up", "-d", "--no-build", "--pull", "never", "caddy")))
        state = release.read_json(self.state / "state.json")
        self.assertEqual((state["current"], state["previous"], state["recovery"]), (B, A, "synthetic-paired-recovery"))

    def test_snapshot_failure_never_starts_candidate_or_replaces_previous(self):
        with patch.object(release, "snapshot", side_effect=OSError("injected disk failure")):
            with self.assertRaises(OSError):
                release.activate(B)
        state = release.read_json(self.state / "state.json")
        self.assertEqual((state["status"], state["current"]), ("maintenance", A))
        self.assertFalse(any(c[0] in {"up", "install"} for c in self.calls))
        release.resume()
        self.assertEqual(release.read_json(self.state / "state.json")["current"], A)

    def test_failed_readiness_keeps_traffic_closed_and_recovery_without_auto_restore(self):
        with patch.object(release, "ready", side_effect=RuntimeError("locked")):
            with self.assertRaises(RuntimeError):
                release.activate(B)
        state = release.read_json(self.state / "state.json")
        self.assertEqual(state["status"], "candidate")
        self.assertEqual(state["recovery"], "synthetic-paired-recovery")
        self.assertFalse(state["traffic_opened"])
        self.assertNotIn(("up", "-d", "--no-build", "--pull", "never", "caddy"), self.calls)
        release.resume()
        self.assertEqual(release.read_json(self.state / "state.json")["current"], B)

    def test_proxy_failure_records_possible_traffic_before_command(self):
        def proxy_fault(*args, **kwargs):
            if args[-1] == "caddy" and args[0] == "up":
                self.assertTrue(release.read_json(self.state / "state.json")["traffic_opened"])
                raise RuntimeError("injected proxy failure")
        with patch.object(release, "compose", side_effect=proxy_fault):
            with self.assertRaises(RuntimeError):
                release.activate(B)
        self.assertEqual(release.read_json(self.state / "state.json")["status"], "opening")

    def test_rollback_requires_explicit_compatibility_and_preserves_latest_data(self):
        with self.assertRaises(ValueError):
            release.activate(B, rollback=True)
        self.assertEqual(self.calls, [])
        with patch.object(release, "run", return_value="38"):
            release.activate(B, rollback=True, compatible=True)
        self.assertEqual(release.read_json(self.state / "state.json")["current"], B)
        self.assertIn(("snapshot", A), self.calls)

    def test_incompatible_schema_never_starts_old_image(self):
        with patch.object(release, "run", return_value="39"):
            with self.assertRaisesRegex(ValueError, "Schema"):
                release.activate(B, rollback=True, compatible=True)
        self.assertFalse(any(c[0] in {"up", "install"} for c in self.calls))

    def test_failed_candidate_is_not_previous_known_good_after_rollback(self):
        older = "d" * 40
        release.write_json(self.state / "state.json", {"status": "active", "current": A, "previous": older})
        with patch.object(release, "ready", side_effect=RuntimeError("candidate failed")):
            with self.assertRaises(RuntimeError):
                release.activate(B)
        with patch.object(release, "run", return_value="38"):
            release.activate(A, rollback=True, compatible=True)
        state = release.read_json(self.state / "state.json")
        self.assertEqual((state["current"], state["previous"], state["failed_candidate"]), (A, older, B))

    def test_rollback_before_candidate_start_requires_resume(self):
        with patch.object(release, "snapshot", side_effect=OSError("snapshot failed")):
            with self.assertRaises(OSError):
                release.activate(B)
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "resume"):
            release.activate(A, rollback=True, compatible=True)
        self.assertEqual(self.calls, [])

    def test_backup_stop_failure_is_journalled_and_can_resume(self):
        with patch.object(release, "compose", side_effect=RuntimeError("stop failed")):
            with self.assertRaises(RuntimeError):
                release.backup()
        state = release.read_json(self.state / "state.json")
        self.assertEqual((state["status"], state["operation"]), ("maintenance", "backup"))
        release.resume()
        state = release.read_json(self.state / "state.json")
        self.assertEqual((state["status"], state["current"], state["previous"]), ("active", A, B))

    def test_backup_copy_failure_reopens_same_image_without_losing_previous(self):
        with patch.object(release, "snapshot", side_effect=OSError("copy failed")):
            with self.assertRaises(OSError):
                release.backup()
        state = release.read_json(self.state / "state.json")
        self.assertEqual((state["status"], state["current"], state["previous"]), ("active", A, B))
        self.assertIn(("ready", A), self.calls)

    def test_transferred_artifact_repopulates_empty_static_store(self):
        folder = release.artifact_path(A)
        (folder / "static").mkdir(parents=True)
        asset = folder / "static/admin.css"
        asset.write_bytes(b"transferred-exact-image-css")
        pwa = folder / "static/manifest.json"
        pwa.write_text('{"name":"synthetic-app"}', encoding="utf-8")
        value = dict(self.values[A], files={"static/admin.css": release.digest(asset),
                                           "static/manifest.json": release.digest(pwa)})
        release.write_json(folder / "manifest.json", value)
        with patch.object(release, "image_id", side_effect=lambda name: name):
            REAL_MANIFEST(A)
        self.assertEqual((self.state / "assets" / release.digest(asset)[:12] / "static/admin.css").read_bytes(), asset.read_bytes())
        (folder / "static/unverified.css").write_bytes(b"unverified")
        with self.assertRaisesRegex(ValueError, "Inventory"):
            REAL_MANIFEST(A)

    def test_readiness_deadline_caps_command_timeout_and_refuses_late_success(self):
        now = [0.0]
        limits = []
        def failing_check(timeout):
            limits.append(timeout)
            now[0] += timeout
            raise subprocess.TimeoutExpired("synthetic-probe", timeout)
        def sleep(seconds):
            now[0] += seconds
        with patch.object(release.time, "monotonic", side_effect=lambda: now[0]), patch.object(release.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(RuntimeError, "Readiness timeout"):
                release.poll(failing_check, seconds=8)
            self.assertEqual(limits, [5, 1])
            self.assertEqual(now[0], 8)
            def late_success(timeout):
                now[0] += timeout + 1
            with self.assertRaisesRegex(RuntimeError, "Readiness timeout"):
                release.poll(late_success, seconds=1)

    def test_lock_excludes_backup_and_release_without_removing_existing_lock(self):
        with release.release_lock():
            with self.assertRaises(FileExistsError):
                with release.release_lock():
                    self.fail("lock bypass")
            self.assertTrue((self.state / "operation.lock").exists())
        self.assertFalse((self.state / "operation.lock").exists())

    def test_versioned_assets_remain_identical_across_update_and_rollback(self):
        for sha, content in ((A, b"old-css"), (B, b"new-css"), (A, b"old-css")):
            folder = self.root / sha
            (folder / "static").mkdir(parents=True, exist_ok=True)
            (folder / "static/admin.css").write_bytes(content)
            release.publish_assets(folder)
        for content in (b"old-css", b"new-css"):
            hashed = hashlib.sha256(content).hexdigest()[:12]
            self.assertEqual((self.state / "assets" / hashed / "static/admin.css").read_bytes(), content)

    def test_artifact_path_rejects_traversal_and_moving_refs(self):
        for name in ("main", "../data", "abc123", "/tmp/anything"):
            with self.assertRaises(ValueError):
                release.artifact_path(name)

    def test_compose_contract_has_exact_images_no_build_host_static_or_public_app_port(self):
        config = release.configuration(self.values[A])
        app = config["services"]["app"]
        self.assertEqual(app["image"], self.values[A]["image"])
        self.assertEqual(app["pull_policy"], "never")
        self.assertEqual(app["environment"]["VIDEO_FASTSTART"], "${VIDEO_FASTSTART:-true}")
        self.assertNotIn("build", app)
        self.assertNotIn("ports", app)
        self.assertNotIn("app/static", json.dumps(config))
        self.assertIn("/health", json.dumps(app["healthcheck"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
