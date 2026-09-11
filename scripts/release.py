"""Точный релиз и восстановление. Только явные CLI-команды меняют сервер.

Состояние .release/ приватно: образы, конфигурация и recovery points не в Git.
Зависимости хоста: Python >=3.12, Linux, Docker Compose v2. См. docs/release.md.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".release"


def run(*args, capture=False, timeout=600, **kwargs):
    result = subprocess.run([str(a) for a in args], check=True, text=True,
                            encoding="utf-8", stdout=subprocess.PIPE if capture else None,
                            timeout=timeout, **kwargs)
    return result.stdout.strip() if capture else None


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read_json(path):
    return json.loads(Path(path).read_text("utf-8"))


@contextmanager
def release_lock():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = STATE / "operation.lock"
    # Не удалять чужую блокировку; после crash оператор проверяет процессы.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink()


def compose(*args, capture=False, timeout=600):
    return run("docker", "compose", "--project-directory", ROOT,
               "--env-file", ROOT / ".env", "-f", STATE / "compose.json",
               *args, capture=capture, timeout=timeout)


def image_id(name):
    value = run("docker", "image", "inspect", name, "--format", "{{.Id}}", capture=True)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Некорректный Docker image ID")
    return value


def artifact_path(release):
    if not re.fullmatch(r"[0-9a-f]{40}|legacy-[0-9a-f]{12}", release):
        raise ValueError("Нужен полный SHA релиза или идентификатор legacy artifact")
    return STATE / "artifacts" / release


def manifest(release):
    folder = artifact_path(release)
    value = read_json(folder / "manifest.json")
    if value["release"] != release:
        raise ValueError("Artifact ID не совпадает")
    inventory = {p.relative_to(folder).as_posix() for p in folder.rglob("*")
                 if p.is_file() and p != folder / "manifest.json"}
    if inventory != set(value["files"]) or any(p.is_symlink() for p in folder.rglob("*")):
        raise ValueError("Inventory artifact изменён")
    for name, expected in value["files"].items():
        path = folder / name
        if Path(name).is_absolute() or ".." in Path(name).parts or digest(path) != expected:
            raise ValueError(f"Повреждён artifact: {name}")
    for key in ("image", "caddy_image"):
        try:
            actual = image_id(value[key])
        except subprocess.CalledProcessError:
            run("docker", "load", "-i", folder / (key + ".tar"))
            actual = image_id(value[key])
        if actual != value[key]:
            raise ValueError("Загружен другой образ")
    if value.get("legacy"):
        helper_release = value["recovery_release"]
        if not re.fullmatch(r"[0-9a-f]{40}", helper_release):
            raise ValueError("Legacy recovery helper должен ссылаться на подготовленный SHA")
        helper = manifest(helper_release)
        if helper["image"] != value["recovery_image"]:
            raise ValueError("Legacy recovery helper image не совпадает")
    # CI передаёт artifact без общего assets-каталога: восстановить mapping до traffic.
    publish_assets(folder)
    return value


def publish_assets(folder):
    """Append-only контентные адреса сохраняют старые immutable URL при откате."""
    for source in sorted((folder / "static").rglob("*")):
        if source.is_symlink():
            raise ValueError("Symlink в static artifact")
        if not source.is_file():
            continue
        relative = source.relative_to(folder / "static")
        target = STATE / "assets" / digest(source)[:12] / "static" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if digest(target) != digest(source):
                raise ValueError("Коллизия content hash; публикация запрещена")
        else:
            temporary = target.with_name(target.name + ".partial")
            shutil.copyfile(source, temporary)
            temporary.replace(target)


def retain(folder, value):
    for key in ("image", "caddy_image"):
        run("docker", "save", "-o", folder / (key + ".tar"), value[key], timeout=1800)
    container = run("docker", "create", value["image"], capture=True)
    try:
        run("docker", "cp", f"{container}:/app/static", folder / "static")
    finally:
        run("docker", "rm", container)
    value["files"] = {p.relative_to(folder).as_posix(): digest(p)
                      for p in sorted(folder.rglob("*")) if p.is_file()}
    write_json(folder / "manifest.json", value)
    publish_assets(folder)


def prepare(sha, receipt):
    from release_gate import validate_receipt
    validate_receipt(Path(receipt), sha, ROOT)
    destination = artifact_path(sha)
    if destination.exists():
        raise ValueError("Artifact уже существует; не пересобираем тот же релиз")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=destination.parent) as temporary:
        folder = Path(temporary)
        archive = folder / "source.tar"
        run("git", "-C", ROOT, "archive", "--format=tar", "-o", archive, sha)
        source = folder / "source"
        source.mkdir()
        with tarfile.open(archive) as stream:
            stream.extractall(source, filter="data")
        run("docker", "build", "--label", f"org.opencontainers.image.revision={sha}",
            "--iidfile", folder / "iid", source / "app", timeout=1800)
        app_image = image_id((folder / "iid").read_text().strip())
        run("docker", "pull", "caddy:2-alpine")
        caddy_image = image_id("caddy:2-alpine")
        # Сначала проверяем настоящий собранный image без сети и production env.
        drill(app_image, sha)
        bundle = folder / "bundle"
        bundle.mkdir()
        shutil.copyfile(source / "Caddyfile", bundle / "Caddyfile")
        shutil.copyfile(receipt, bundle / "gate.json")
        schema = int(run("docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
                         app_image, "-c", "import db; print(db.LATEST_VERSION)", capture=True))
        retain(bundle, {"release": sha, "image": app_image, "caddy_image": caddy_image,
                        "schema": schema, "legacy": False, "image_drill": "PASS"})
        # Адаптация Caddyfile проверяется тем же retained proxy image.
        run("docker", "run", "--rm", "--network", "none", "-e", "DOMAIN=localhost",
            "-v", f"{bundle / 'Caddyfile'}:/etc/caddy/Caddyfile:ro", caddy_image,
            "caddy", "adapt", "--config", "/etc/caddy/Caddyfile", "--validate")
        bundle.rename(destination)
    print(destination)


def drill(image, sha):
    """Настоящие lifespan/migrations/ffmpeg + HTTP + snapshot, без внешней сети."""
    name = "date4you-drill-" + uuid.uuid4().hex[:12]
    try:
        run("docker", "run", "-d", "--name", name, "--network", "none",
            "--tmpfs", "/data", "-e", "SECRET_KEY=synthetic-release-drill",
            "-e", "DOMAIN=localhost", "-e", "COOKIE_SECURE=false",
            "-e", f"APP_RELEASE={sha}", image)
        poll(lambda timeout: run("docker", "exec", name, "python", "-c", probe_code(sha),
                                 capture=True, timeout=timeout))
        run("docker", "exec", name, "ffmpeg", "-version")
        run("docker", "exec", name, "python", "-c",
            "import urllib.request,backup; "
            "assert urllib.request.urlopen('http://127.0.0.1:8000/login',timeout=3).status==200; "
            "print(backup.make_backup())")
    finally:
        run("docker", "rm", "-f", name)


def probe_code(sha, legacy=False):
    path = "/health" if legacy else "/ready"
    return ("import json,urllib.request,sqlite3; "
            f"r=json.load(urllib.request.urlopen('http://127.0.0.1:8000{path}',timeout=2)); "
            "assert r.get('ok') is True; " +
            ("" if legacy else f"assert r['release']=={sha!r}; ") +
            "c=sqlite3.connect('file:/data/app.db?mode=rw',uri=True,timeout=.1); "
            "c.execute('BEGIN IMMEDIATE'); c.rollback(); c.close()")


def poll(check, seconds=60):
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Readiness timeout; traffic remains stopped")
        try:
            check(min(5, remaining))
            if time.monotonic() > deadline:
                raise RuntimeError("Readiness timeout; traffic remains stopped")
            return
        except (subprocess.SubprocessError, OSError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Readiness timeout; traffic remains stopped") from None
            time.sleep(min(2, max(0, deadline - time.monotonic())))


def configuration(value):
    logging = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
    return {"name": "date4you", "services": {
        "app": {"image": value["image"], "pull_policy": "never", "restart": "unless-stopped",
                "env_file": [str(ROOT / ".env")],
                "environment": {"TZ": "Europe/Moscow", "APP_RELEASE": value["release"],
                                "DATA_DIR": "/data", "VIDEO_FASTSTART": "${VIDEO_FASTSTART:-true}"},
                "volumes": [f"{ROOT / 'data'}:/data"], "expose": ["8000"], "logging": logging,
                "healthcheck": {"test": ["CMD", "python", "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3)"],
                    "interval": "30s", "timeout": "5s", "retries": 3, "start_period": "15s"}},
        "caddy": {"image": value["caddy_image"], "pull_policy": "never", "restart": "unless-stopped",
                  "ports": ["80:80", "443:443"], "environment": {"DOMAIN": "${DOMAIN:?DOMAIN required}"},
                  "volumes": [f"{STATE / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
                              f"{STATE / 'assets'}:/srv/assets:ro", "caddy_data:/data", "caddy_config:/config"],
                  "logging": logging}}, "volumes": {"caddy_data": {}, "caddy_config": {}}}


def install_configuration(value):
    shutil.copyfile(artifact_path(value["release"]) / "Caddyfile", STATE / "Caddyfile")
    write_json(STATE / "compose.json", configuration(value))


def adopt(candidate):
    """Первый переход сохраняет реально работающие образы, даже без старого gate."""
    ids = {}
    for service in ("app", "caddy"):
        container = run("docker", "ps", "--filter", "label=com.docker.compose.project=date4you",
                        "--filter", f"label=com.docker.compose.service={service}",
                        "--format", "{{.ID}}", capture=True)
        if not container or len(container.splitlines()) != 1:
            raise ValueError("Для первой установки используйте deploy --first-install (только пустой data)")
        ids[service] = run("docker", "inspect", container, "--format", "{{.Image}}", capture=True)
        if service == "app":
            schema = int(run("docker", "exec", container, "python", "-c",
                             "import db; print(db.LATEST_VERSION)", capture=True))
    release = "legacy-" + ids["app"].split(":")[-1][:12]
    folder = artifact_path(release)
    if folder.exists():
        value = manifest(release)
        install_configuration(value)
        return value
    value = {"release": release, "image": ids["app"], "caddy_image": candidate["caddy_image"],
             "original_caddy_image": ids["caddy"], "legacy": True, "schema": schema,
             "recovery_image": candidate["image"], "recovery_release": candidate["release"],
             "gate": "NOT TESTED: imported running baseline"}
    with tempfile.TemporaryDirectory(prefix="adopt-", dir=folder.parent) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir()
        # Новый proxy использует append-only assets, в том числе для старого image.
        shutil.copyfile(artifact_path(candidate["release"]) / "Caddyfile", stage / "Caddyfile")
        run("docker", "save", "-o", stage / "original-caddy.tar", ids["caddy"])
        retain(stage, value)
        stage.rename(folder)
    install_configuration(value)
    return value


def snapshot(value, recovery_image=None):
    name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    parent = STATE / "recovery"
    parent.mkdir(exist_ok=True)
    run("docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
        "-v", f"{ROOT / 'data'}:/data:ro", "-v", f"{parent}:/recovery",
        recovery_image or value.get("recovery_image", value["image"]), "/app/recovery.py", "create", "--data", "/data",
        "--output", f"/recovery/{name}", "--release", value["release"], "--image", value["image"], "--quiesced")
    return name


def ready(value):
    poll(lambda timeout: compose("exec", "-T", "app", "python", "-c",
                                 probe_code(value["release"], value.get("legacy", False)),
                                 capture=True, timeout=timeout))


def activate(release, first_install=False, rollback=False, compatible=False):
    target = manifest(release)
    state_file = STATE / "state.json"
    state = read_json(state_file) if state_file.exists() else None
    if state and state["status"] != "active" and not rollback:
        raise ValueError("Незавершённая операция: inspect state.json; используйте resume или rollback")
    if rollback and state and state["status"] == "maintenance":
        raise ValueError("Candidate ещё не запускался; используйте resume вместо rollback")
    source = state["current"] if state and state["status"] == "active" else state.get("target") if state else None
    previous = manifest(source) if source else None
    if not previous and not first_install:
        previous = adopt(target)
    if first_install and (previous or any((ROOT / "data").glob("*"))):
        raise ValueError("--first-install разрешён только с пустым data и без предыдущего релиза")
    if previous and previous["release"] == release:
        raise ValueError("Этот релиз уже активен")
    if rollback and (not compatible or not previous or release != state.get("previous")):
        raise ValueError("Rollback только к recorded previous с --schema-compatible; иначе ручное recovery")
    known_previous = previous["release"] if previous else None
    failed_candidate = None
    if rollback and state["status"] != "active":
        # Не рекламировать неудавшийся candidate как следующий known-good rollback.
        known_previous = (state.get("prior_state") or {}).get("previous")
        failed_candidate = previous["release"]
    transition = {"status": "maintenance", "current": previous["release"] if previous else None,
                  "target": release, "previous": known_previous, "failed_candidate": failed_candidate,
                  "recovery": None, "traffic_opened": False,
                  "rollback": rollback,
                  "prior_state": {k: v for k, v in state.items() if k != "prior_state"} if state else None}
    write_json(state_file, transition)
    if previous:
        compose("stop", "caddy")
        compose("stop", "app")
        helper_image = previous.get("recovery_image", previous["image"]) if rollback else target["image"]
        if rollback:
            actual_schema = int(run("docker", "run", "--rm", "--network", "none",
                "--entrypoint", "python", "-v", f"{ROOT / 'data'}:/data:ro", helper_image, "-c",
                "import sqlite3; c=sqlite3.connect('file:/data/app.db?mode=ro',uri=True); "
                "print(c.execute('PRAGMA user_version').fetchone()[0]); c.close()", capture=True))
            if actual_schema != target["schema"]:
                raise ValueError("Schema несовместима; traffic остановлен, требуется ручное recovery")
        transition["recovery"] = snapshot(previous, helper_image)
        write_json(state_file, transition)
    install_configuration(target)
    # С этого места возможны migrations: при любой ошибке ничего не восстанавливаем автоматически.
    transition["status"] = "candidate"
    write_json(state_file, transition)
    compose("up", "-d", "--no-build", "--pull", "never", "app")
    ready(target)
    # Пишем intent ДО открытия traffic: crash никогда не оправдывает автоматический DB restore.
    transition["status"] = "opening"
    transition["traffic_opened"] = True
    write_json(state_file, transition)
    compose("up", "-d", "--no-build", "--pull", "never", "caddy")
    transition.update(status="active", current=release)
    write_json(state_file, transition)


def backup():
    state = read_json(STATE / "state.json")
    if state["status"] != "active":
        raise ValueError("Backup требует завершённого релиза")
    value = manifest(state["current"])
    progress = {**state, "status": "maintenance", "target": state["current"],
                "traffic_opened": False, "operation": "backup",
                "prior_state": {k: v for k, v in state.items() if k != "prior_state"}}
    write_json(STATE / "state.json", progress)
    compose("stop", "caddy")
    compose("stop", "app")
    try:
        name = snapshot(value)
    finally:
        compose("up", "-d", "--no-build", "--pull", "never", "app")
        ready(value)
        progress.update(status="opening", traffic_opened=True)
        write_json(STATE / "state.json", progress)
        compose("up", "-d", "--no-build", "--pull", "never", "caddy")
        write_json(STATE / "state.json", state)
    print(STATE / "recovery" / name)
    return STATE / "recovery" / name


def resume():
    state = read_json(STATE / "state.json")
    if state["status"] not in {"maintenance", "candidate", "opening"}:
        raise ValueError("Resume только для незавершённой операции")
    # maintenance записан до запуска migrations; можно вернуть прежний image.
    maintenance = state["status"] == "maintenance"
    selected = state["current"] if maintenance else state["target"]
    if not selected:
        raise ValueError("Прерванная первая установка: требуется ручная проверка пустого data")
    value = manifest(selected)
    install_configuration(value)
    compose("up", "-d", "--no-build", "--pull", "never", "app")
    ready(value)
    state.update(status="opening", traffic_opened=True)
    write_json(STATE / "state.json", state)
    compose("up", "-d", "--no-build", "--pull", "never", "caddy")
    if maintenance and state.get("prior_state"):
        state = state["prior_state"]
        # Возвращаем исходную идентичность previous/recovery после прерванной операции.
    state.update(status="active", current=value["release"], traffic_opened=True)
    write_json(STATE / "state.json", state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--sha", required=True)
    p.add_argument("--receipt", required=True)
    p = sub.add_parser("deploy")
    p.add_argument("--sha", required=True)
    p.add_argument("--first-install", action="store_true")
    p = sub.add_parser("rollback")
    p.add_argument("--release", required=True)
    p.add_argument("--schema-compatible", action="store_true")
    sub.add_parser("backup")
    sub.add_parser("resume")
    sub.add_parser("status")
    p = sub.add_parser("compose")
    p.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(read_json(STATE / "state.json"), indent=2))
        return
    if args.command == "compose":
        compose(*args.args)
        return
    if sys.platform != "linux":
        parser.error("Операции сервера требуют Linux; используйте unit fixtures на других ОС")
    with release_lock():
        if args.command == "prepare":
            prepare(args.sha, args.receipt)
        elif args.command == "deploy":
            activate(args.sha, first_install=args.first_install)
        elif args.command == "rollback":
            activate(args.release, rollback=True, compatible=args.schema_compatible)
        elif args.command == "backup":
            backup()
        else:
            resume()


if __name__ == "__main__":
    main()
