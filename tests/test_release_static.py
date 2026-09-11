"""DEPLOY-03: immutable URL выдаёт только соответствующее хэшу содержимое."""

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from starlette.testclient import TestClient

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-release-static-")
os.environ.update({
    "DATA_DIR": _DATA.name, "SECRET_KEY": "synthetic-release-static",
    "COOKIE_SECURE": "false", "DOMAIN": "testserver", "LOG_LEVEL": "CRITICAL",
    "TG_BOT_TOKEN": "", "TG_CHAT_ID": "", "TG_BACKUP_CHAT_ID": "", "SENTRY_DSN": "",
})

import main


class ReleaseStaticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="assets-", dir=_DATA.name)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.original = b"body{color:navy}"
        self.file = self.directory / "sample.css"
        self.file.write_bytes(self.original)
        self.digest = hashlib.sha256(self.original).hexdigest()[:12]
        app = FastAPI()
        app.mount("/static", main.CachedStatic(directory=self.directory))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_matching_hash_is_immutable_and_exact(self):
        response = self.client.get(f"/static/sample.css?v={self.digest}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.original)
        self.assertIn("immutable", response.headers["cache-control"])

    def test_wrong_empty_duplicate_and_missing_versions_are_not_cached(self):
        for path in (
            "/static/sample.css?v=ffffffffffff", "/static/sample.css?v=",
            f"/static/sample.css?v={self.digest}&v={self.digest}",
            f"/static/missing.css?v={self.digest}",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotEqual(response.content, self.original)

    def test_old_version_cannot_receive_changed_bytes(self):
        self.file.write_bytes(b"body{color:orange}")
        response = self.client.get(f"/static/sample.css?v={self.digest}")
        self.assertEqual(response.status_code, 404)
        current = hashlib.sha256(self.file.read_bytes()).hexdigest()[:12]
        response = self.client.get(f"/static/sample.css?v={current}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.file.read_bytes())

    def test_unversioned_policy_and_unrelated_query_do_not_become_immutable(self):
        for suffix in ("", "?other=v=ffffffffffff", "?preview=1"):
            with self.subTest(suffix=suffix):
                response = self.client.get("/static/sample.css" + suffix)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"],
                                 "public, max-age=3600, must-revalidate")

    def test_conditional_requests_validate_version_before_304(self):
        first = self.client.get(f"/static/sample.css?v={self.digest}")
        headers = {"If-None-Match": first.headers["etag"]}
        matched = self.client.get(f"/static/sample.css?v={self.digest}", headers=headers)
        self.assertEqual(matched.status_code, 304)
        self.assertIn("immutable", matched.headers["cache-control"])
        wrong = self.client.get("/static/sample.css?v=ffffffffffff", headers=headers)
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(wrong.headers["cache-control"], "no-store")

    def test_versioned_head_retains_policy_without_body(self):
        response = self.client.head(f"/static/sample.css?v={self.digest}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertIn("immutable", response.headers["cache-control"])

    def test_webp_and_fonts_have_correct_mime_without_host_mime_database(self):
        for extension, expected in (("webp", "image/webp"), ("woff2", "font/woff2"),
                                    ("woff", "font/woff"), ("ttf", "font/ttf")):
            path = self.directory / f"asset.{extension}"
            path.write_bytes(b"synthetic static bytes")
            version = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            for query in ("", f"?v={version}"):
                with self.subTest(extension=extension, query=query), patch("starlette.responses.guess_type", return_value=(None, None)):
                    response = self.client.get(f"/static/asset.{extension}{query}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], expected)
                self.assertEqual(response.content, path.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
