#!/usr/bin/env python3
"""Изолированный gate точного SHA. Developer PASS не разрешает выпуск образа."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[1]
HOST_ENV = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "HOME", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "ALLUSERSPROFILE",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL",
    "DISPLAY", "XAUTHORITY", "PLAYWRIGHT_BROWSERS_PATH", "VIRTUAL_ENV",
}
INTEGRATIONS = (
    "TG_BOT_TOKEN", "TG_CHAT_ID", "TG_BACKUP_CHAT_ID", "TG_BOT_USERNAME",
    "TG_WEBHOOK_SECRET", "TG_MINI_APP_URL", "OPERATOR_TG_IDS", "SENTRY_DSN",
    "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET", "YANDEX_CLIENT_ID", "YANDEX_CLIENT_SECRET",
)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, encoding="utf-8",
        stderr=subprocess.PIPE,
    ).strip()


def checkout_state(repo: Path, sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Нужен полный SHA: 40 строчных hex-символов")
    if git(repo, "rev-parse", "HEAD") != sha:
        raise ValueError("HEAD не совпадает с запрошенным SHA")
    return {
        "sha": sha,
        "tree": git(repo, "rev-parse", f"{sha}^{{tree}}"),
        "clean_checkout": not git(repo, "status", "--porcelain", "--untracked-files=all"),
    }


def safe_environment(parent: dict, directory: Path) -> dict:
    """Наследуем только runtime-пути, не credentials/proxy/PYTHONPATH/.env."""
    env = {key: value for key, value in parent.items() if key.upper() in HOST_ENV}
    for name in ("tmp", "data", "output"):
        (directory / name).mkdir(parents=True, exist_ok=True)
    env.update({name: "" for name in INTEGRATIONS})
    env.update({
        "TMP": str(directory / "tmp"), "TEMP": str(directory / "tmp"),
        "TMPDIR": str(directory / "tmp"), "DATA_DIR": str(directory / "data"),
        "TEST_OUTPUT_DIR": str(directory / "output"), "DOMAIN": "localhost",
        "SECRET_KEY": "release-gate-synthetic-secret", "COOKIE_SECURE": "false",
        "APP_ENV": "test", "APP_RELEASE": "release-gate", "LOG_LEVEL": "ERROR",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
        "RELEASE_GATE": "1",
    })
    return env


def snapshot_source(repo: Path, sha: str, target: Path, dirty: bool) -> list[str]:
    target.mkdir()
    if dirty:
        names = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
        for name in sorted(set(filter(None, names))):
            source = repo / name
            if not source.exists():
                continue
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"Snapshot не поддерживает symlink/submodule: {name}")
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    else:
        archive = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=zip", sha])
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            for entry in zipped.infolist():
                destination = (target / entry.filename).resolve()
                if not destination.is_relative_to(target.resolve()):
                    raise ValueError("Небезопасный путь в git archive")
            zipped.extractall(target)
            if os.name != "nt":
                for entry in zipped.infolist():
                    mode = (entry.external_attr >> 16) & 0o777
                    if mode:
                        (target / entry.filename).chmod(mode)
    return sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file())


def source_digest(root: Path, names: list[str]) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8") + b"\0")
        path = root / name
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"MISSING")
    return digest.hexdigest()


def runtime_checks(mode: str, env: dict) -> dict:
    checks = {
        "python": {"status": "PASS" if sys.version_info >= (3, 12) else "BLOCKED", "version": platform.python_version()},
        "production_runtime": {
            "status": "PASS" if platform.system() == "Linux" and sys.version_info[:2] == (3, 12) else "NOT TESTED",
            "required": mode == "release", "os": platform.platform(),
        },
    }
    if mode == "release" and checks["production_runtime"]["status"] != "PASS":
        checks["production_runtime"]["status"] = "BLOCKED"
    packages = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--format=json", "--disable-pip-version-check"],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    checks["installed_packages"] = {
        "status": "PASS" if packages.returncode == 0 else "BLOCKED",
        "packages": json.loads(packages.stdout) if packages.returncode == 0 else [],
    }
    for name in ("sh", "bash", "ffmpeg"):
        required = name != "ffmpeg" or mode == "release"
        command = [name, "-version"] if name == "ffmpeg" else [name, "-c", "exit 0"]
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            checks[name] = {"status": "PASS" if result.returncode == 0 else ("BLOCKED" if required else "NOT TESTED"), "required": required, "version": result.stdout.splitlines()[0] if result.stdout else "available"}
        except (OSError, subprocess.TimeoutExpired) as error:
            checks[name] = {"status": "BLOCKED" if required else "NOT TESTED", "required": required, "detail": str(error)}
    probe = """import json
from importlib.metadata import version
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content('<title>release-gate</title>')
    assert page.title() == 'release-gate'
    print(json.dumps({'version': browser.version, 'playwright': version('playwright')}))
    browser.close()
"""
    try:
        result = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
        checks["chromium"] = {"status": "PASS" if result.returncode == 0 else "BLOCKED", "required": True, "detail": result.stdout.strip() if result.returncode == 0 else result.stderr[-4000:]}
    except (OSError, subprocess.TimeoutExpired) as error:
        checks["chromium"] = {"status": "BLOCKED", "required": True, "detail": str(error)}
    for browser in ("firefox", "webkit"):
        checks[browser] = {"status": "NOT TESTED", "required": False, "detail": "Текущие regression suites используют Chromium"}
    return checks


def run_module(path: Path, result_path: Path) -> int:
    """Сохраняем реальные unittest skips, включая class-level SkipTest."""
    collected = []
    original = unittest.TextTestRunner.run

    def record(runner, suite):
        result = original(runner, suite)
        collected.append({
            "tests": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped),
            "skip_reasons": [str(reason) for _, reason in result.skipped],
            "unexpected_successes": len(result.unexpectedSuccesses),
        })
        return result

    unittest.TextTestRunner.run = record
    sys.path.insert(0, str(path.parent))
    sys.argv = [str(path)]
    code = 0
    try:
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
    finally:
        unittest.TextTestRunner.run = original
        result_path.write_text(json.dumps(collected, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


def classify_result(name: str, returncode: int, output: str, records: list[dict]) -> dict:
    summary = {"module": name, "returncode": returncode, "tests": sum(r["tests"] for r in records), "skipped": sum(r["skipped"] for r in records), "status": "FAIL"}
    if returncode != 0:
        return summary
    if records:
        if summary["tests"] and not any(r["failures"] or r["errors"] or r["skipped"] or r["unexpected_successes"] for r in records):
            summary["status"] = "PASS"
        return summary
    # Слово skipped в рабочих application logs не является test skip.
    if re.search(r"\(skip\)|^\s*SKIP\b", output, flags=re.I | re.M):
        summary["skipped"] = 1
        return summary
    if name == "test_smoke.py":
        match = re.search(r"Все проверки пройдены: (\d+) блоков", output)
        if match and int(match[1]) > 0:
            summary.update(status="PASS", smoke_blocks=int(match[1]))
    elif name == "test_ink.py" and "✓ след от клика совпадает с эталоном" in output:
        summary.update(status="PASS", screenshot_checks=1)
    return summary


def validate_receipt(path: Path, sha: str, repo: Path = ROOT) -> dict:
    """Receipt принимается только от доверенного CI; это свидетельство, не подпись."""
    receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    state = checkout_state(Path(repo), sha)
    expected = sorted(name.removeprefix("tests/") for name in git(Path(repo), "ls-tree", "-r", "--name-only", sha, "tests").splitlines() if re.fullmatch(r"tests/test_[^/]+\.py", name))
    tests = receipt.get("tests", [])
    required_checks = ("python", "production_runtime", "sh", "bash", "ffmpeg", "chromium")
    valid = (
        receipt.get("schema_version") == SCHEMA_VERSION and receipt.get("status") == "PASS"
        and receipt.get("mode") == "release" and receipt.get("release_qualified") is True
        and receipt.get("clean_checkout") is True and state["clean_checkout"]
        and receipt.get("sha") == sha and receipt.get("tree") == state["tree"]
        and receipt.get("source_unchanged") is True and bool(expected)
        and sorted(test.get("module", "") for test in tests) == expected
        and all(test.get("status") == "PASS" and test.get("returncode") == 0 and test.get("skipped") == 0 for test in tests)
        and all(receipt.get("runtime", {}).get(key, {}).get("status") == "PASS" for key in required_checks)
    )
    if not valid:
        raise ValueError("Receipt не разрешает release: нужны чистый точный SHA, release Linux/3.12 и полный PASS без skips")
    return receipt


def run_gate(args, repo: Path = ROOT) -> int:
    state = checkout_state(repo, args.sha)
    if args.allow_dirty and args.mode != "developer":
        raise ValueError("--allow-dirty допустим только в developer mode")
    if not state["clean_checkout"] and not args.allow_dirty:
        raise ValueError("Рабочее дерево не чистое; developer validation требует явный --allow-dirty")
    output = args.output.resolve()
    if output.is_relative_to(repo.resolve()):
        relative = output.relative_to(repo.resolve()).as_posix()
        if subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", relative + "/"]).returncode != 0:
            raise ValueError("Output внутри checkout должен быть gitignored (например artifacts/release-gate)")
    output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=f"{args.sha[:12]}-{args.mode}-", dir=output))
    receipt = {
        "schema_version": SCHEMA_VERSION, **state, "mode": args.mode,
        "status": "FAIL", "release_qualified": False,
        "started_at": datetime.now(timezone.utc).isoformat(), "run_directory": str(run),
        "tests": [], "source_unchanged": False,
        "environment": {"DOMAIN": "localhost", "credentials": "synthetic", "integrations": "disabled", "isolation": "source snapshot + process/TMP/DATA/output per module"},
    }
    print(f"Gate evidence: {run / 'receipt.json'}", flush=True)
    try:
        source = run / "source"
        names = snapshot_source(repo, args.sha, source, not state["clean_checkout"])
        initial = source_digest(source, names)
        receipt["source_sha256"] = initial
        receipt["runtime"] = runtime_checks(args.mode, safe_environment(os.environ, run / "preflight"))
        if any(check["status"] == "BLOCKED" for check in receipt["runtime"].values()):
            receipt["status"] = "BLOCKED"
            return 1
        modules = sorted((source / "tests").glob("test_*.py"))
        if not modules:
            raise ValueError("В source snapshot нет tests/test_*.py")
        for module in modules:
            directory = run / module.stem
            env = safe_environment(os.environ, directory)
            result_path = directory / "unittest.json"
            started = time.monotonic()
            try:
                result = subprocess.run(
                    [sys.executable, str(source / "scripts/release_gate.py"), "_module", str(module), str(result_path)],
                    cwd=source, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                )
                log = result.stdout + result.stderr
                records = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else []
                summary = classify_result(module.name, result.returncode, log, records)
            except subprocess.TimeoutExpired as error:
                log = "TIMEOUT after 600 seconds\n" + str(error.stdout or "") + str(error.stderr or "")
                summary = {"module": module.name, "status": "FAIL", "returncode": -1, "skipped": 0, "tests": 0}
            (directory / "output.log").write_text(log, encoding="utf-8")
            summary.update(seconds=round(time.monotonic() - started, 3), log=str(directory / "output.log"))
            receipt["tests"].append(summary)
            print(f"{summary['status']} {module.name}: tests={summary['tests']}, skips={summary['skipped']}", flush=True)
            if summary["status"] != "PASS":
                return 1
        receipt["source_unchanged"] = source_digest(source, names) == initial
        current = checkout_state(repo, args.sha)
        if not args.allow_dirty and not current["clean_checkout"]:
            raise ValueError("Рабочее дерево изменилось во время gate")
        if not receipt["source_unchanged"]:
            raise ValueError("Тесты изменили исходные файлы snapshot")
        receipt["status"] = "PASS"
        receipt["release_qualified"] = args.mode == "release" and state["clean_checkout"]
        return 0
    except Exception as error:
        receipt["error"] = f"{type(error).__name__}: {error}"
        print(receipt["error"], file=sys.stderr, flush=True)
        return 1
    finally:
        receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
        (run / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Gate {receipt['status']}; release_qualified={receipt['release_qualified']}", flush=True)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_module":
        return run_module(Path(sys.argv[2]), Path(sys.argv[3]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--mode", choices=("developer", "release"), default="developer")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--verify-receipt", type=Path)
    args = parser.parse_args()
    try:
        if args.verify_receipt:
            validate_receipt(args.verify_receipt, args.sha)
            print("PASS: receipt разрешает release указанного SHA")
            return 0
        if args.output is None:
            parser.error("--output обязателен для запуска gate")
        return run_gate(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Gate rejected: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
