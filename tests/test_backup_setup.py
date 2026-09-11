"""CFG-01: настоящий setup-backup.sh с изолированной конфигурацией и заглушками."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/setup-backup.sh"
BASH = str(Path("C:/Program Files/Git/bin/bash.exe")) if os.name == "nt" else shutil.which("bash")


class BackupSetupTests(unittest.TestCase):
    def run_setup(self, initial="OTHER=unchanged\n", recipient=None, managed=False):
        with tempfile.TemporaryDirectory(prefix="date4you-backup-setup-") as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            if managed:
                (root / ".release").mkdir()
                (root / ".release/state.json").write_text('{"status":"active"}', encoding="utf-8")
            config = root / ".env"
            config.write_text(initial, encoding="utf-8")
            (root / "scripts/backup.sh").write_text('printf "backup\\n" >> "$TEST_LOG"\n', encoding="utf-8")
            wrapper = root / "run.sh"
            wrapper.write_text('''set -euo pipefail
export PATH="/usr/bin:/bin:$PATH"
rclone() { if [ "$*" = "config file" ]; then printf '%s\\n' "$PROJECT_DIR/fake-rclone.conf"; fi; return 0; }
docker() { printf 'docker %s\\n' "$*" >> "$TEST_LOG"; }
python3() { printf 'python3 %s\\n' "$*" >> "$TEST_LOG"; }
chmod() { return 0; }
curl() { echo 'FORBIDDEN NETWORK' >&2; return 99; }
sudo() { echo 'FORBIDDEN SYSTEM CHANGE' >&2; return 99; }
export -f rclone docker python3 chmod curl sudo
source "$1"
''', encoding="utf-8", newline="\n")
            env = dict(os.environ, PROJECT_DIR=root.as_posix(), TEST_LOG=(root / "calls").as_posix(),
                       S3_ENDPOINT="https://synthetic.invalid", S3_ACCESS_KEY="synthetic",
                       S3_SECRET_KEY="synthetic", S3_BUCKET="synthetic", REMOTE_NAME="synthetic")
            env.pop("TG_BACKUP_CHAT_ID", None)
            env.pop("PYTHON", None)
            if recipient is not None:
                env["TG_BACKUP_CHAT_ID"] = recipient
            result = subprocess.run([BASH, wrapper.as_posix(), SCRIPT.as_posix()], env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            calls = (root / "calls").read_text("utf-8") if (root / "calls").exists() else ""
            return result, config.read_text("utf-8"), calls

    def test_syntax(self):
        result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unset_and_empty_recipient_do_not_enable_telegram(self):
        for recipient in (None, ""):
            with self.subTest(recipient=recipient):
                result, config, calls = self.run_setup(recipient=recipient)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(config, "OTHER=unchanged\n")
                self.assertEqual(calls, "docker compose up -d app\nbackup\n")

    def test_explicit_recipient_is_added_and_app_restarted(self):
        for recipient in ("-100123456", "@synthetic_backup"):
            with self.subTest(recipient=recipient):
                result, config, calls = self.run_setup(recipient=recipient)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"TG_BACKUP_CHAT_ID={recipient}\n", config)
                self.assertEqual(calls, "docker compose up -d app\nbackup\n")

    def test_existing_choice_including_disabled_is_never_overwritten(self):
        for assignment in ("TG_BACKUP_CHAT_ID=-100999", "TG_BACKUP_CHAT_ID=",
                           'TG_BACKUP_CHAT_ID=""', "TG_BACKUP_CHAT_ID=''",
                           "  TG_BACKUP_CHAT_ID = -100999", "export TG_BACKUP_CHAT_ID=-100999"):
            for recipient in (None, "", "-100123456"):
                with self.subTest(assignment=assignment, recipient=recipient):
                    initial = "OTHER=unchanged\n" + assignment + "\n"
                    result, config, calls = self.run_setup(initial, recipient)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(config, initial)
                    self.assertEqual(calls, "docker compose up -d app\nbackup\n")

    def test_newline_recipient_cannot_inject_configuration(self):
        result, config, calls = self.run_setup(recipient="-100123\nINJECTED=1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(config, "OTHER=unchanged\n")
        self.assertEqual(calls, "")

    def test_managed_release_applies_current_choice_to_retained_image(self):
        for assignment in ("TG_BACKUP_CHAT_ID=", "TG_BACKUP_CHAT_ID=-100999"):
            initial = assignment + "\n"
            result, config, calls = self.run_setup(initial, recipient="-100123", managed=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config, initial)
            self.assertEqual(calls, "python3 scripts/release.py compose up -d --no-build --pull never app\nbackup\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
