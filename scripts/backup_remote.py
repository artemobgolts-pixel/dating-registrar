"""Публикация цельных DB+media bundles; manifest.json — последний commit marker."""
from contextlib import ExitStack
import os
from pathlib import Path
import re

import release

TELEGRAM_CODE = (
    "import backup,tasks; "
    "enabled=bool(tasks.TG_BACKUP_CHAT_ID); "
    "sent=tasks.ship_backup_to_tg(backup.make_backup()) if enabled else False; "
    "print('Telegram: '+('sent' if sent else 'failed' if enabled else 'disabled')); "
    "raise SystemExit(1 if enabled and not sent else 0)"
)

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
    with release.release_lock(), ExitStack() as locks:
        app_id = None
        state_file = release.STATE / "state.json"
        if state_file.exists() or state_file.is_symlink():
            if (release.STATE / "legacy-backup.json").exists():
                raise ValueError("Незавершённый legacy backup: сначала восстановите исходные контейнеры")
            bundle = release.backup()
        else:
            import backup_legacy
            locks.enter_context(backup_legacy.update_lock())
            bundle, app_id = backup_legacy.backup()
        errors = []
        try:
            publish(bundle, remote, keep)
        except Exception as exc:
            errors.append(exc)
            print(f"Cloud backup failed ({type(exc).__name__}); local recovery bundle retained")
        # Phase A: только явно заданный TG_BACKUP_CHAT_ID внутри актуального env.
        # Telegram получает DB-only дополнительную копию, не recovery bundle.
        try:
            if app_id:
                release.run("docker", "exec", app_id, "python", "-c", TELEGRAM_CODE, timeout=60)
            else:
                release.compose("exec", "-T", "app", "python", "-c", TELEGRAM_CODE, timeout=60)
        except Exception as exc:
            errors.append(exc)
            print(f"Telegram backup failed ({type(exc).__name__}); recovery bundle retained")
        if errors:
            raise errors[0]


if __name__ == "__main__":
    main()
