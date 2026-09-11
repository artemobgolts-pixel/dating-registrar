"""Offline DB + original-media recovery bundles; no app imports or integrations.

Stop ingress AND every app/background writer before `create --quiesced`.
The flag acknowledges that external precondition; SQLite alone cannot lock
filesystem uploads. Restore publishes only to a NEW directory and never switches
traffic or overwrites live data. Keep/delete/copy each entire bundle as one unit.
Rebuildable responsive-v1 and og-cache caches are deliberately excluded.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile

from backup import _sync_directory, _sync_file, _write_snapshot

FORMAT_VERSION = 1
MEDIA_COLUMNS = (("date_images", "filename"), ("date_videos", "filename"),
                 ("users", "avatar_path"), ("categories", "og_image"))


def _digest(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"size": path.stat().st_size, "sha256": digest.hexdigest()}


def _files(root: Path) -> dict[str, Path]:
    """List regular files only; never follow symlinks or read pipes/devices."""
    found = {}
    def fail(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        for name in dirs + files:
            path = Path(directory) / name
            kind = path.lstat().st_mode
            if not (stat.S_ISREG(kind) or stat.S_ISDIR(kind)):
                raise ValueError(f"Unsupported non-regular recovery path: {path}")
        for name in files:
            path = Path(directory) / name
            found[path.relative_to(root).as_posix()] = path
    return found


def _database_info(db_path: Path) -> tuple[int, list[str]]:
    # Only called on frozen, standalone snapshots (never a live WAL database).
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Recovery database integrity_check failed")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        references = set()
        for table, column in MEDIA_COLUMNS:
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if column not in columns:
                continue  # Older schemas need not contain all current columns.
            for (name,) in conn.execute(
                    f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
                if not name:
                    continue
                if not isinstance(name, str) or "\\" in name or ":" in name \
                        or PurePosixPath(name).name != name or name in (".", ".."):
                    raise ValueError(f"Unsafe media reference in {table}.{column}")
                references.add("uploads/" + name)
        return version, sorted(references)


def _new_destination(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise ValueError(f"Destination must be a NEW directory: {path}")
    return path.resolve()


def _publish_directory(staging: Path, destination: Path) -> None:
    # Check again just before publication. rename also refuses any nonempty
    # existing directory, so a populated production DATA_DIR cannot be replaced.
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Destination appeared during recovery: {destination}")
    for directory, _, _ in os.walk(staging, topdown=False):
        _sync_directory(Path(directory))
    staging.rename(destination)
    _sync_directory(destination.parent)


def create_recovery(data: Path, output: Path, *, quiesced: bool = False,
                    release: str = "", image: str = "") -> Path:
    if not quiesced:
        raise ValueError("Stop ingress and ALL app/background writers; pass --quiesced")
    data = Path(data).resolve()
    output = _new_destination(Path(output))
    if output == data or output.is_relative_to(data) or data.is_relative_to(output):
        raise ValueError("Recovery output must be outside the source DATA_DIR")
    source_db = data / "app.db"
    source_uploads = data / "uploads"
    if source_db.is_symlink() or not source_db.is_file():
        raise ValueError("Source app.db must be an existing regular file")
    if source_uploads.is_symlink() or (source_uploads.exists() and not source_uploads.is_dir()):
        raise ValueError("Source uploads must be a regular directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", suffix=".partial", dir=output.parent))
    try:
        _write_snapshot(source_db, staging / "app.db")
        (staging / "uploads").mkdir()
        if source_uploads.exists():
            for relative, source in _files(source_uploads).items():
                target = staging / "uploads" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                _sync_file(target)
        version, references = _database_info(staging / "app.db")
        files = {name: _digest(path) for name, path in sorted(_files(staging).items())}
        missing = set(references) - files.keys()
        if missing:
            raise ValueError(f"Database references missing original media: {sorted(missing)}")
        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "quiesced": True,
            "release_sha": release,
            "image_id": image,
            "schema_version": version,
            "scope": "app.db+uploads",
            "excluded_rebuildable_caches": ["responsive-v1", "og-cache"],
            "media_references": references,
            "files": files,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _sync_file(manifest_path)
        verify_recovery(staging)
        _publish_directory(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output


def verify_recovery(bundle: Path) -> dict:
    bundle = Path(bundle)
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("Recovery bundle must be an existing regular directory")
    paths = _files(bundle)
    if "manifest.json" not in paths:
        raise ValueError("Recovery manifest is missing; bundle is not published")
    manifest = json.loads(paths["manifest.json"].read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION \
            or manifest.get("quiesced") is not True or manifest.get("scope") != "app.db+uploads":
        raise ValueError("Unsupported or unquiesced recovery manifest")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or "app.db" not in expected:
        raise ValueError("Recovery manifest must include app.db")
    if set(paths) != set(expected) | {"manifest.json"}:
        raise ValueError("Recovery file inventory differs from manifest")
    for name, info in expected.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name \
                or name != relative.as_posix() \
                or (name != "app.db" and not name.startswith("uploads/")):
            raise ValueError(f"Unsafe path in recovery manifest: {name}")
        if info != _digest(paths[name]):
            raise ValueError(f"Recovery hash/size mismatch: {name}")
    version, references = _database_info(paths["app.db"])
    if version != manifest.get("schema_version") or references != manifest.get("media_references"):
        raise ValueError("Recovery database metadata differs from manifest")
    if set(references) - expected.keys():
        raise ValueError("Recovery database references missing original media")
    return manifest


def restore_recovery(bundle: Path, destination: Path) -> Path:
    bundle = Path(bundle)
    manifest = verify_recovery(bundle)
    bundle = bundle.resolve()
    destination = _new_destination(Path(destination))
    if destination.is_relative_to(bundle) or bundle.is_relative_to(destination):
        raise ValueError("Restore destination must be outside the bundle")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", suffix=".partial", dir=destination.parent))
    try:
        (staging / "uploads").mkdir()
        for name in list(manifest["files"]) + ["manifest.json"]:
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bundle / name, target)
            _sync_file(target)
        # Re-verify the copies; source tampering or a failed copy cannot publish.
        verify_recovery(staging)
        _publish_directory(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--data", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--release", default="")
    create.add_argument("--image", default="")
    create.add_argument("--quiesced", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("bundle", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("bundle", type=Path)
    restore.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create_recovery(args.data, args.output, quiesced=args.quiesced,
                                     release=args.release, image=args.image)
        elif args.command == "restore":
            result = restore_recovery(args.bundle, args.destination)
        else:
            verify_recovery(args.bundle)
            result = args.bundle
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Recovery failed: {exc}\n")
    print(result)


if __name__ == "__main__":
    main()
