"""TEST-04: gate не превращает skips, чужое окружение или dirty SHA в release."""
import argparse
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_gate", ROOT / "scripts/release_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class ReleaseGateTests(unittest.TestCase):
    def test_environment_drops_credentials_proxies_and_python_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            env = gate.safe_environment({
                "PATH": os.environ["PATH"], "SYSTEMROOT": "runtime-path",
                "SECRET_KEY": "real-secret", "TG_BOT_TOKEN": "real-token",
                "TG_BACKUP_CHAT_ID": "real-recipient", "SENTRY_DSN": "real-sentry",
                "HTTP_PROXY": "real-proxy", "PYTHONPATH": "foreign-code",
                "S3_SECRET_KEY": "real-cloud-secret", "DOMAIN": "production.example",
                "DATA_DIR": "real-data", "UNRELATED_TOKEN": "secret",
            }, Path(directory))
            self.assertEqual(env["DOMAIN"], "localhost")
            self.assertEqual(env["SECRET_KEY"], "release-gate-synthetic-secret")
            self.assertEqual(env["SYSTEMROOT"], "runtime-path")
            for key in gate.INTEGRATIONS:
                self.assertEqual(env[key], "")
            for key in ("HTTP_PROXY", "PYTHONPATH", "S3_SECRET_KEY", "UNRELATED_TOKEN"):
                self.assertNotIn(key, env)
            for key in ("TMP", "TEMP", "TMPDIR", "DATA_DIR", "TEST_OUTPUT_DIR"):
                self.assertTrue(Path(env[key]).is_relative_to(directory))

    def test_modules_have_distinct_temp_data_and_output_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            one = gate.safe_environment({}, Path(directory) / "one")
            two = gate.safe_environment({}, Path(directory) / "two")
            for key in ("TMP", "DATA_DIR", "TEST_OUTPUT_DIR"):
                self.assertNotEqual(one[key], two[key])

    def test_class_level_skip_is_recorded_and_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "test_skips.py"
            module.write_text('''import unittest
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise unittest.SkipTest("synthetic browser unavailable")
    def test_browser(self): pass
if __name__ == "__main__": unittest.main()
''', encoding="utf-8")
            record = root / "result.json"
            result = subprocess.run([sys.executable, str(ROOT / "scripts/release_gate.py"), "_module", str(module), str(record)], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0)
            records = json.loads(record.read_text(encoding="utf-8"))
            summary = gate.classify_result(module.name, result.returncode, result.stdout + result.stderr, records)
            self.assertEqual(summary["status"], "FAIL")
            self.assertEqual(summary["skipped"], 1)

    def test_skip_after_successful_test_still_fails(self):
        result = gate.classify_result("test_example.py", 0, "", [{"tests": 2, "failures": 0, "errors": 0, "skipped": 1, "unexpected_successes": 0}])
        self.assertEqual(result["status"], "FAIL")

    def test_custom_runners_require_positive_completion_and_no_skip(self):
        for name, log in (("test_ink.py", "рендер недоступен — (skip)"), ("test_smoke.py", "partial"), ("test_other.py", "OK")):
            with self.subTest(name=name):
                self.assertEqual(gate.classify_result(name, 0, log, [])["status"], "FAIL")
        ink = gate.classify_result("test_ink.py", 0, "✓ след от клика совпадает с эталоном", [])
        self.assertEqual(ink["status"], "PASS")
        smoke = gate.classify_result("test_smoke.py", 0, "map url skipped\nВсе проверки пройдены: 85 блоков ✔", [])
        self.assertEqual(smoke["status"], "PASS")
        self.assertEqual(smoke["smoke_blocks"], 85)

    def test_nonzero_exit_cannot_pass_with_success_text(self):
        self.assertEqual(gate.classify_result("test_smoke.py", 1, "Все проверки пройдены: 85 блоков", [])["status"], "FAIL")

    def synthetic_repo(self, root):
        repo = root / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "tests").mkdir()
        (repo / "scripts/release_gate.py").write_bytes((ROOT / "scripts/release_gate.py").read_bytes())
        (repo / "tests/test_synthetic.py").write_text('''import os
from pathlib import Path
import tempfile
import unittest
class SyntheticTests(unittest.TestCase):
    def test_isolation(self):
        self.assertEqual(Path(tempfile.gettempdir()), Path(os.environ["TMPDIR"]))
        self.assertEqual(os.environ["DOMAIN"], "localhost")
        self.assertEqual(os.environ["TG_BOT_TOKEN"], "")
        self.assertTrue(Path(os.environ["DATA_DIR"]).is_dir())
if __name__ == "__main__": unittest.main()
''', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Gate Fixture", "-c", "user.email=gate@invalid", "commit", "-qm", "synthetic"], check=True)
        return repo, gate.git(repo, "rev-parse", "HEAD")

    def run_synthetic_gate(self, root, repo, sha, mode="release", allow_dirty=False):
        args = argparse.Namespace(sha=sha, mode=mode, output=root / "evidence", allow_dirty=allow_dirty)
        checks = {name: {"status": "PASS"} for name in ("python", "production_runtime", "sh", "bash", "ffmpeg", "chromium")}
        with patch.object(gate, "runtime_checks", return_value=checks):
            self.assertEqual(gate.run_gate(args, repo), 0)
        paths = sorted(args.output.glob("*/receipt.json"), key=lambda path: path.stat().st_mtime_ns)
        return paths[-1], json.loads(paths[-1].read_text(encoding="utf-8"))

    def test_exact_clean_sha_complete_gate_receipt_is_accepted(self):
        with tempfile.TemporaryDirectory(prefix="gate-") as directory:
            root = Path(directory)
            repo, sha = self.synthetic_repo(root)
            path, receipt = self.run_synthetic_gate(root, repo, sha)
            self.assertTrue(receipt["release_qualified"])
            self.assertTrue(receipt["source_unchanged"])
            self.assertEqual(gate.validate_receipt(path, sha, repo)["sha"], sha)

    def test_receipt_rejects_missing_tests_skips_and_runtime_gap(self):
        with tempfile.TemporaryDirectory(prefix="gate-") as directory:
            root = Path(directory)
            repo, sha = self.synthetic_repo(root)
            path, receipt = self.run_synthetic_gate(root, repo, sha)
            missing = deepcopy(receipt)
            missing["tests"] = []
            skipped = deepcopy(receipt)
            skipped["tests"][0]["skipped"] = 1
            runtime_gap = deepcopy(receipt)
            runtime_gap["runtime"]["chromium"]["status"] = "BLOCKED"
            wrong_tree = deepcopy(receipt)
            wrong_tree["tree"] = "0" * 40
            for modified in (missing, skipped, runtime_gap, wrong_tree):
                path.write_text(json.dumps(modified), encoding="utf-8")
                with self.assertRaises(ValueError):
                    gate.validate_receipt(path, sha, repo)

    def test_developer_pass_never_qualifies_release(self):
        with tempfile.TemporaryDirectory(prefix="gate-") as directory:
            root = Path(directory)
            repo, sha = self.synthetic_repo(root)
            path, receipt = self.run_synthetic_gate(root, repo, sha, mode="developer")
            self.assertEqual(receipt["status"], "PASS")
            self.assertFalse(receipt["release_qualified"])
            with self.assertRaises(ValueError):
                gate.validate_receipt(path, sha, repo)

    def test_dirty_checkout_and_wrong_sha_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="gate-") as directory:
            root = Path(directory)
            repo, sha = self.synthetic_repo(root)
            (repo / "unexpected.py").write_text("synthetic\n", encoding="utf-8")
            args = argparse.Namespace(sha=sha, mode="release", output=root / "evidence", allow_dirty=False)
            with self.assertRaises(ValueError):
                gate.run_gate(args, repo)
            args.allow_dirty = True
            with self.assertRaises(ValueError):
                gate.run_gate(args, repo)
            with self.assertRaises(ValueError):
                gate.checkout_state(repo, "0" * 40)
            with self.assertRaises(ValueError):
                gate.checkout_state(repo, sha[:12])

    def test_allow_dirty_snapshots_changes_but_cannot_qualify_release(self):
        with tempfile.TemporaryDirectory(prefix="gate-") as directory:
            root = Path(directory)
            repo, sha = self.synthetic_repo(root)
            (repo / "new-file.txt").write_text("synthetic dirty change", encoding="utf-8")
            path, receipt = self.run_synthetic_gate(root, repo, sha, mode="developer", allow_dirty=True)
            self.assertFalse(receipt["clean_checkout"])
            self.assertFalse(receipt["release_qualified"])
            self.assertEqual((path.parent / "source/new-file.txt").read_text(encoding="utf-8"), "synthetic dirty change")

    def test_runtime_blocker_writes_blocked_receipt_without_running_tests(self):
        with tempfile.TemporaryDirectory(prefix="gate-") as directory:
            root = Path(directory)
            repo, sha = self.synthetic_repo(root)
            args = argparse.Namespace(sha=sha, mode="release", output=root / "evidence", allow_dirty=False)
            with patch.object(gate, "runtime_checks", return_value={"chromium": {"status": "BLOCKED"}}):
                self.assertEqual(gate.run_gate(args, repo), 1)
            receipt = json.loads(next(args.output.glob("*/receipt.json")).read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "BLOCKED")
            self.assertEqual(receipt["tests"], [])
            self.assertFalse(receipt["release_qualified"])


if __name__ == "__main__":
    unittest.main()
