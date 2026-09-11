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
REAL_SNAPSHOT = release.snapshot


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

    def snapshot_docker(self, *, present=False, failures=None, keep_after_rm=False, exit_code="0"):
        """Локальная модель daemon: CLI может оборваться, контейнер остаётся."""
        engine = {"present": present}
        failures = failures or {}

        def docker(*args, **kwargs):
            self.assertEqual(args[0], "docker")
            operation = args[1]
            self.calls.append(("docker", operation))
            marker = release.read_json(self.state / "state.json").get("snapshot_container")
            self.assertRegex(marker or "", r"^date4you-snapshot-[0-9a-f]{32}$")
            if operation == "create":
                self.assertEqual(args[args.index("--name") + 1], marker)
                # Daemon мог принять create до timeout/interrupt клиентского CLI.
                engine["present"] = True
            if operation in failures:
                raise failures[operation]
            if operation == "create":
                return "synthetic-container-id"
            if operation == "start":
                self.assertTrue(engine["present"])
                self.assertIn("-a", args)
                self.assertEqual(args[-1], marker)
                return ""
            if operation == "inspect":
                self.assertEqual(args[2], marker)
                self.assertEqual(args[args.index("--format") + 1], "{{.State.ExitCode}}")
                return exit_code
            if operation == "ps":
                self.assertIn("-a", args)
                self.assertEqual(args[args.index("--filter") + 1], f"name=^/{marker}$")
                return "synthetic-container-id" if engine["present"] else ""
            if operation == "rm":
                self.assertIn("-f", args)
                self.assertEqual(args[-1], marker)
                engine["present"] = keep_after_rm
                return ""
            self.fail(f"Непредусмотренный Docker вызов: {args}")

        return engine, docker

    def maintenance_state(self, *, marker=False):
        state = {"status": "maintenance", "current": A, "target": B,
                 "previous": B, "traffic_opened": False}
        if marker:
            state["snapshot_container"] = "date4you-snapshot-" + "c" * 32
        release.write_json(self.state / "state.json", state)
        return state

    def test_snapshot_records_identity_before_create_and_cleans_after_success(self):
        original = self.maintenance_state()
        engine, docker = self.snapshot_docker()
        with patch.object(release, "run", side_effect=docker):
            name = REAL_SNAPSHOT(self.values[A])
        self.assertRegex(name, r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
        self.assertEqual(self.calls, [("docker", name) for name in
                                     ("create", "start", "inspect", "ps", "rm", "ps")])
        self.assertFalse(engine["present"])
        self.assertEqual(release.read_json(self.state / "state.json"), original)

    def test_snapshot_cli_timeout_and_interrupt_remove_surviving_helper(self):
        for operation in ("create", "start"):
            for error_type in (subprocess.TimeoutExpired, KeyboardInterrupt):
                with self.subTest(operation=operation, error=error_type.__name__):
                    self.calls.clear()
                    original = self.maintenance_state()
                    error = (subprocess.TimeoutExpired("synthetic-docker", 600)
                             if error_type is subprocess.TimeoutExpired else KeyboardInterrupt())
                    engine, docker = self.snapshot_docker(failures={operation: error})
                    with patch.object(release, "run", side_effect=docker):
                        with self.assertRaises(error_type):
                            REAL_SNAPSHOT(self.values[A])
                    self.assertFalse(engine["present"])
                    self.assertEqual(self.calls[-3:], [("docker", "ps"), ("docker", "rm"), ("docker", "ps")])
                    self.assertEqual(release.read_json(self.state / "state.json"), original)

    def test_snapshot_nonzero_container_exit_is_not_reported_as_success(self):
        original = self.maintenance_state()
        engine, docker = self.snapshot_docker(exit_code="23")
        with patch.object(release, "run", side_effect=docker):
            with self.assertRaisesRegex(RuntimeError, "23"):
                REAL_SNAPSHOT(self.values[A])
        self.assertFalse(engine["present"])
        self.assertEqual(release.read_json(self.state / "state.json"), original)

    def test_cleanup_failure_blocks_all_writer_entry_points_and_preserves_marker(self):
        operations = {
            "backup": release.backup,
            "resume": release.resume,
            "deploy": lambda: release.activate(B),
            "rollback": lambda: release.activate(B, rollback=True, compatible=True),
        }
        for name, invoke in operations.items():
            for failed_command in ("ps", "rm"):
                with self.subTest(operation=name, failure=failed_command):
                    self.calls.clear()
                    original = self.maintenance_state(marker=True)
                    if name != "resume":
                        original["status"] = "active"
                        release.write_json(self.state / "state.json", original)
                    engine, docker = self.snapshot_docker(present=True, failures={
                        failed_command: subprocess.CalledProcessError(1, "synthetic-docker"),
                    })
                    with patch.object(release, "run", side_effect=docker):
                        with self.assertRaises(subprocess.CalledProcessError):
                            invoke()
                    self.assertTrue(engine["present"])
                    self.assertEqual(release.read_json(self.state / "state.json"), original)
                    self.assertFalse(any(call[0] in {"up", "install", "stop", "snapshot"}
                                         for call in self.calls))

    def test_cleanup_requires_confirmed_absence_before_clearing_marker(self):
        original = self.maintenance_state(marker=True)
        engine, docker = self.snapshot_docker(present=True, keep_after_rm=True)
        with patch.object(release, "run", side_effect=docker):
            with self.assertRaises(RuntimeError):
                release.resume()
        self.assertTrue(engine["present"])
        self.assertEqual(release.read_json(self.state / "state.json"), original)
        self.assertEqual(self.calls, [("docker", "ps"), ("docker", "rm"), ("docker", "ps")])

    def test_backup_outer_finally_does_not_restart_writers_after_cleanup_failure(self):
        engine, docker = self.snapshot_docker(failures={
            "start": subprocess.TimeoutExpired("synthetic-docker", 600),
            "rm": subprocess.CalledProcessError(1, "synthetic-docker"),
        })
        with patch.object(release, "snapshot", REAL_SNAPSHOT), patch.object(release, "run", side_effect=docker):
            with self.assertRaises(subprocess.CalledProcessError):
                release.backup()
        state = release.read_json(self.state / "state.json")
        self.assertEqual((state["status"], state["operation"]), ("maintenance", "backup"))
        self.assertRegex(state["snapshot_container"], r"^date4you-snapshot-[0-9a-f]{32}$")
        self.assertTrue(engine["present"])
        self.assertFalse(any(call[0] in {"up", "ready", "install"} for call in self.calls))

    def test_backup_timeout_reopens_same_artifact_only_after_helper_cleanup(self):
        engine, docker = self.snapshot_docker(failures={
            "start": subprocess.TimeoutExpired("synthetic-docker", 600),
        })
        with patch.object(release, "snapshot", REAL_SNAPSHOT), patch.object(release, "run", side_effect=docker):
            with self.assertRaises(subprocess.TimeoutExpired):
                release.backup()
        state = release.read_json(self.state / "state.json")
        self.assertEqual((state["status"], state["current"], state["previous"]), ("active", A, B))
        self.assertNotIn("snapshot_container", state)
        self.assertFalse(engine["present"])
        removal = self.calls.index(("docker", "rm"))
        confirmation = self.calls.index(("docker", "ps"), removal + 1)
        self.assertLess(confirmation, self.calls.index(("up", "-d", "--no-build", "--pull", "never", "app")))

    def test_backup_nonzero_helper_never_returns_or_prints_successful_bundle(self):
        engine, docker = self.snapshot_docker(exit_code="23")
        with (patch.object(release, "snapshot", REAL_SNAPSHOT),
              patch.object(release, "run", side_effect=docker), patch("builtins.print") as output):
            with self.assertRaisesRegex(RuntimeError, "23"):
                release.backup()
        output.assert_not_called()
        self.assertFalse(engine["present"])
        state = release.read_json(self.state / "state.json")
        self.assertEqual((state["status"], state["current"]), ("active", A))
        self.assertNotIn("snapshot_container", state)

    def test_resume_confirms_cleanup_before_installing_or_starting_app(self):
        self.maintenance_state(marker=True)
        engine, docker = self.snapshot_docker(present=True)
        with patch.object(release, "run", side_effect=docker):
            release.resume()
        self.assertEqual(self.calls[:4], [("docker", "ps"), ("docker", "rm"),
                                         ("docker", "ps"), ("install", A)])
        self.assertFalse(engine["present"])
        self.assertNotIn("snapshot_container", release.read_json(self.state / "state.json"))

    def test_cleanup_rejects_unowned_container_name_without_docker(self):
        original = self.maintenance_state(marker=True)
        original["snapshot_container"] = "date4you-app-1"
        release.write_json(self.state / "state.json", original)
        with patch.object(release, "run") as docker:
            with self.assertRaises(ValueError):
                release.cleanup_snapshot()
        docker.assert_not_called()
        self.assertEqual(release.read_json(self.state / "state.json"), original)

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
