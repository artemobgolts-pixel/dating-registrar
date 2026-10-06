"""Цельный backup legacy/simple установки с сохранением реальных контейнеров.

Вызывается только под release_lock. Приватный journal позволяет следующему
запуску сначала завершить прерванную операцию, не открывая writers при живом
snapshot helper. Managed state.json этот модуль никогда не создаёт и не меняет.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

import release

IDENTITY = re.compile(r"[0-9a-f]{64}")
HELPER = re.compile(r"date4you-legacy-snapshot-[0-9a-f]{32}")


@contextmanager
def update_lock():
    """Тот же atomic mkdir lock, который использует серверный simple updater."""
    simple = release.STATE / "simple"
    if not (simple / "compose.json").exists():
        yield
        return
    lock = simple / "update.lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        raise ValueError("Simple update.lock уже занят; проверьте updater до повторного backup") from None
    try:
        yield
    finally:
        # Только lock, успешно созданный этой операцией. Чужой/stale lock
        # не удаляем; после SIGKILL его проверяет оператор, как operation.lock.
        lock.rmdir()


def inspect(*identities):
    return json.loads(release.run("docker", "inspect", *identities, capture=True, timeout=10))


def service(name):
    identities = release.run("docker", "ps", "--filter", "label=com.docker.compose.project=date4you",
                             "--filter", f"label=com.docker.compose.service={name}",
                             "--format", "{{.ID}}", capture=True, timeout=10).splitlines()
    if len(identities) != 1:
        raise ValueError(f"Legacy backup требует ровно один работающий {name} контейнер")
    value = inspect(identities[0])[0]
    if not IDENTITY.fullmatch(value["Id"]) or not value["State"]["Running"]:
        raise ValueError(f"Некорректный {name} контейнер")
    return value


def data_mount(app, data):
    mounts = [mount for mount in app["Mounts"] if mount["Destination"] == "/data"]
    if len(mounts) != 1 or mounts[0]["Type"] != "bind" \
            or Path(mounts[0]["Source"]).resolve() != data:
        raise ValueError("DATA_DIR не совпадает с реальным bind mount приложения")


def reject_other_writers(data, app_id):
    identities = release.run("docker", "ps", "-q", capture=True, timeout=10).splitlines()
    if not identities:
        return
    for container in inspect(*identities):
        if container["Id"] == app_id:
            continue
        for mount in container["Mounts"]:
            if mount.get("Type") not in {"bind", "volume"} or not mount.get("Source"):
                continue
            source = Path(mount["Source"]).resolve()
            if mount.get("RW", True) and (source == data or source.is_relative_to(data)
                                           or data.is_relative_to(source)):
                raise ValueError("Обнаружен дополнительный writable контейнер: writers не остановлены")


def cleanup_helper(name):
    if not HELPER.fullmatch(name):
        raise ValueError("Некорректный snapshot helper в legacy backup journal")
    def exists():
        return bool(release.run("docker", "ps", "-a", "--filter", f"name=^/{name}$",
                                "--format", "{{.ID}}", capture=True, timeout=10))
    if exists():
        release.run("docker", "rm", "-f", name, timeout=30)
        if exists():
            raise RuntimeError("Snapshot helper ещё существует; writers остаются остановлены")


def resume():
    journal = release.STATE / "legacy-backup.json"
    if not journal.exists():
        return
    state = release.read_json(journal)
    app_id, caddy_id = state["app"]["id"], state["caddy"]["id"]
    if not all(IDENTITY.fullmatch(identity) for identity in (app_id, caddy_id)):
        raise ValueError("Некорректные container IDs в legacy backup journal")
    app, caddy = inspect(app_id, caddy_id)
    for name, value in (("app", app), ("caddy", caddy)):
        expected = state[name]
        labels = value["Config"].get("Labels", {})
        if value["Id"] != expected["id"] or value["Image"] != expected["image"] \
                or labels.get("com.docker.compose.project") != "date4you" \
                or labels.get("com.docker.compose.service") != name:
            raise ValueError("Контейнеры изменились во время legacy backup; требуется проверка оператора")
    data_mount(app, Path(state["data"]).resolve())
    # Ошибка daemon/CLI оставляет journal и закрытый ingress; отсутствие helper
    # подтверждается Docker до любого повторного запуска приложения.
    cleanup_helper(state["helper"])
    if not app["State"]["Running"]:
        release.run("docker", "start", app_id, timeout=30)
    release.poll(lambda timeout: release.run(
        "docker", "exec", app_id, "python", "-c", release.probe_code("", legacy=True),
        capture=True, timeout=timeout))
    if not caddy["State"]["Running"]:
        release.run("docker", "start", caddy_id, timeout=30)
    journal.unlink()


def backup():
    resume()
    data = Path(os.getenv("DATA_DIR", str(release.ROOT / "data"))).resolve()
    app, caddy = service("app"), service("caddy")
    data_mount(app, data)
    reject_other_writers(data, app["Id"])
    name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    parent = release.STATE / "recovery"
    parent.mkdir(exist_ok=True)
    helper = "date4you-legacy-snapshot-" + uuid.uuid4().hex
    state = {"data": str(data), "helper": helper,
             "app": {"id": app["Id"], "image": app["Image"]},
             "caddy": {"id": caddy["Id"], "image": caddy["Image"]}}
    app_release = next((value.split("=", 1)[1] for value in app["Config"].get("Env", [])
                        if value.startswith("APP_RELEASE=")), "")
    release.write_json(release.STATE / "legacy-backup.json", state)
    try:
        release.run("docker", "stop", caddy["Id"], timeout=40)
        release.run("docker", "stop", app["Id"], timeout=40)
        if any(value["State"]["Running"] for value in inspect(app["Id"], caddy["Id"])):
            raise RuntimeError("Legacy backup requires stopped ingress and writers")
        reject_other_writers(data, app["Id"])
        release.run("docker", "create", "--name", helper, "--network", "none", "--entrypoint", "python",
                    "-v", f"{data}:/data:ro", "-v", f"{parent}:/recovery",
                    app["Image"], "/app/recovery.py", "create", "--data", "/data",
                    "--output", f"/recovery/{name}", "--release", app_release,
                    "--image", app["Image"], "--quiesced", capture=True, timeout=30)
        release.run("docker", "start", "-a", helper, capture=True)
        code = release.run("docker", "inspect", helper, "--format", "{{.State.ExitCode}}",
                           capture=True, timeout=10)
        if code != "0":
            raise RuntimeError(f"Snapshot helper завершился с кодом {code}")
        sys.path.insert(0, str(release.ROOT / "app"))
        import recovery
        recovery.verify_recovery(parent / name)
    finally:
        resume()
    return parent / name, app["Id"]
