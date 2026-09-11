"""Локальный HTTP drill настоящего Caddyfile: static update/failure/rollback.

TLS отключён только в disposable адаптированной конфигурации. Без production env.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import release


def drill(caddy):
    with tempfile.TemporaryDirectory(prefix="date4you-caddy-drill-") as directory:
        root = Path(directory)
        assets = root / "assets"
        assets.mkdir()
        env = dict(os.environ, DOMAIN="localhost")
        config = json.loads(subprocess.check_output(
            [caddy, "adapt", "--config", str(release.ROOT / "Caddyfile")], env=env))
        server = next(iter(config["apps"]["http"]["servers"].values()))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server["listen"] = [f"127.0.0.1:{port}"]
        server["automatic_https"] = {"disable": True}
        server["routes"] = [route for route in server["routes"] if route["match"] == [{"host": ["localhost"]}]]
        server["routes"][0].pop("match")
        config["admin"] = {"disabled": True}
        serialized = json.dumps(config).replace("/srv/assets", assets.as_posix())
        (root / "caddy.json").write_text(serialized, encoding="utf-8")
        def publish(content):
            version = hashlib.sha256(content).hexdigest()[:12]
            destination = assets / version / "static" / "admin.css"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            return version
        old = publish(b"old-release-css")
        def get(version):
            request = urllib.request.Request(f"http://127.0.0.1:{port}/static/admin.css?v={version}")
            with urllib.request.urlopen(request, timeout=2) as response:
                assert "immutable" in response.headers["Cache-Control"]
                return response.read()
        with (root / "caddy.log").open("wb") as log:
            process = subprocess.Popen([caddy, "run", "--config", str(root / "caddy.json")],
                                       cwd=root, stdout=log, stderr=log)
            try:
                for attempt in range(50):
                    try:
                        assert get(old) == b"old-release-css"
                        break
                    except (OSError, urllib.error.URLError):
                        if process.poll() is not None:
                            raise RuntimeError((root / "caddy.log").read_text("utf-8"))
                        time.sleep(.1)
                else:
                    raise RuntimeError("Caddy startup timeout")
                new = publish(b"new-release-css")
                for _ in range(2):  # failed activation и rollback не меняют immutable mapping
                    assert get(old) == b"old-release-css"
                    assert get(new) == b"new-release-css"
                for bad in ("0" * 12, "../../private", "bad-hash"):
                    try:
                        get(bad)
                        raise AssertionError("Caddy accepted missing/unsafe asset version")
                    except urllib.error.HTTPError as error:
                        assert error.code == 404
                print("PASS: Caddy old/new immutable bytes, failed-update model, rollback model, invalid hash 404")
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caddy", required=True)
    args = parser.parse_args()
    drill(str(Path(args.caddy).resolve()))
