"""Публикация цельных DB+media bundles; manifest.json — последний commit marker."""
import os
from pathlib import Path
import re

import release

BUNDLE_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")


def publish(bundle: Path, remote: str, keep: int):
    if keep < 1:
        raise ValueError("KEEP_REMOTE должен быть >= 1")
    if not BUNDLE_NAME.fullmatch(bundle.name):
        raise ValueError("Неизвестное имя recovery point")
    # Только завершённый локальный bundle с проверенными hashes и ссылками.
    import sys
    sys.path.insert(0, str(release.ROOT / "app"))
    import recovery
    recovery.verify_recovery(bundle)
    prefix = remote.rstrip("/") + "/recovery"
    target = prefix + "/" + bundle.name
    release.run("rclone", "copy", bundle, target, "--exclude", "/manifest.json")
    # --download не зависит от поддержки общего checksum у backend rclone.
    release.run("rclone", "check", bundle, target, "--one-way", "--download", "--exclude", "/manifest.json")
    release.run("rclone", "copyto", bundle / "manifest.json", target + "/manifest.json")
    try:
        release.run("rclone", "check", bundle, target, "--one-way", "--download")
    except Exception:
        # Не оставляем commit marker точки, не прошедшей итоговую проверку.
        release.run("rclone", "deletefile", target + "/manifest.json")
        raise
    # Retention только после полной успешной публикации. Ошибка listing не считается пустым списком.
    names = release.run("rclone", "lsf", prefix, "--dirs-only", capture=True).splitlines()
    complete = []
    for name in names:
        name = name.rstrip("/")
        if not BUNDLE_NAME.fullmatch(name):
            continue
        marker = release.run("rclone", "lsf", prefix + "/" + name,
                             "--files-only", "--include", "manifest.json", capture=True)
        if marker.strip() == "manifest.json":
            complete.append(name)
    for name in sorted(complete)[:-keep]:
        release.run("rclone", "purge", prefix + "/" + name)


def main():
    remote = os.getenv("RCLONE_REMOTE", "backup:date4you")
    keep = int(os.getenv("KEEP_REMOTE", "30"))
    if keep < 1:
        raise ValueError("KEEP_REMOTE должен быть >= 1")
    with release.release_lock():
        bundle = release.backup()
        publish(bundle, remote, keep)
        # Phase A: только явно заданный TG_BACKUP_CHAT_ID внутри актуального env.
        # Telegram получает DB-only дополнительную копию, не recovery bundle.
        try:
            release.compose("exec", "-T", "app", "python", "-c",
                "import backup,tasks; print('Telegram:',tasks.ship_backup_to_tg(backup.make_backup()))")
        except Exception as exc:
            print(f"Telegram backup failed ({type(exc).__name__}); recovery bundle retained")


if __name__ == "__main__":
    main()
