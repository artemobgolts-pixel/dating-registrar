"""SEC-04: ранние ограничения multipart, авторизация и уборка временных файлов."""

import asyncio
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch

import httpx
from PIL import Image
from starlette.testclient import TestClient

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-upload-security-")
os.environ.update({
    "DATA_DIR": _DATA.name, "SECRET_KEY": "upload-test-secret",
    "COOKIE_SECURE": "false", "TG_BOT_TOKEN": "", "TG_CHAT_ID": "",
    "TG_BOT_USERNAME": "upload_test_bot", "DOMAIN": "testserver",
})

import db
import main
import images
import uploads


def png():
    data = io.BytesIO()
    Image.new("RGB", (32, 24), "navy").save(data, "PNG")
    return data.getvalue()


VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"synthetic video payload"


class UploadSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        main._rates.clear()
        self.conn = db.connect()
        self.conn.execute("DELETE FROM users")
        self.conn.execute("DELETE FROM login_codes")
        self.uid = self.conn.execute(
            "INSERT INTO users(telegram_id,display_name,created_at) VALUES(90001,'Тест',?)",
            (main.now_iso(),),
        ).lastrowid
        self.other = self.conn.execute(
            "INSERT INTO users(telegram_id,display_name,created_at) VALUES(90002,'Чужой',?)",
            (main.now_iso(),),
        ).lastrowid
        self.did = self.conn.execute(
            "INSERT INTO dates(owner_id,name,created_at) VALUES(?,'Чужое событие',?)",
            (self.other, main.now_iso()),
        ).lastrowid
        self.cid = self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,created_at) VALUES(?,'Подборка','upload-category',?)",
            (self.other, main.now_iso()),
        ).lastrowid
        self.conn.commit()
        self.client = TestClient(main.app, follow_redirects=False)
        self.addCleanup(self.client.close)
        self.addCleanup(self.conn.close)
        self.tempfiles = []
        original = tempfile.SpooledTemporaryFile

        def spool(*args, **kwargs):
            value = original(*args, **kwargs)
            self.tempfiles.append(value)
            return value

        self.spool_patch = patch("starlette.formparsers.SpooledTemporaryFile", side_effect=spool)
        self.spool_patch.start()
        self.addCleanup(self.spool_patch.stop)

    def login(self):
        code = self.client.post("/auth/start").json()["code"]
        self.conn.execute("UPDATE login_codes SET status='confirmed',telegram_id=90001 WHERE code=?", (code,))
        self.conn.commit()
        self.assertEqual(self.client.get("/auth/poll", params={"code": code}).json()["status"], "ok")
        payload = self.client.cookies.get("admin_s").split(".")[0]
        return json.loads(base64.b64decode(payload))["csrf"]

    def assert_closed(self):
        self.assertTrue(self.tempfiles)
        self.assertTrue(all(file.closed for file in self.tempfiles))

    def test_anonymous_upload_rejected_without_spooling_or_decoding(self):
        with patch.object(images, "save_upload") as decode:
            response = self.client.post("/admin/profile", data={"display_name": "Тест"}, files={"avatar": ("photo.png", png(), "image/png")})
        self.assertIn(response.status_code, (401, 303))
        self.assertEqual(self.tempfiles, [])
        decode.assert_not_called()

    def test_foreign_event_upload_rejected_before_spooling(self):
        csrf = self.login()
        before = list(self.conn.iterdump())
        response = self.client.post(f"/admin/dates/{self.did}/edit", data={"name": "Подмена", "csrf": csrf}, files={"images": ("photo.png", png(), "image/png")})
        self.assertIn(response.status_code, (303, 404))
        self.assertEqual(self.tempfiles, [])
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_invalid_origin_rejected_before_spooling(self):
        csrf = self.login()
        response = self.client.post("/admin/profile", headers={"Origin": "https://foreign.test"}, data={"display_name": "Тест", "csrf": csrf}, files={"avatar": ("photo.png", png(), "image/png")})
        self.assertIn(response.status_code, (303, 403))
        self.assertEqual(self.tempfiles, [])

    def test_nonoperator_import_rejected_before_spooling(self):
        csrf = self.login()
        response = self.client.post("/admin/import/json", data={"csrf": csrf}, files={"file": ("export.json", b"{}", "application/json")})
        self.assertIn(response.status_code, (303, 404))
        self.assertEqual(self.tempfiles, [])

    def test_guest_proposal_accepts_legacy_video_and_photos(self):
        csrf = self.login()
        response = self.client.post("/c/upload-category/propose", headers={"X-CSRF-Token": csrf}, data={"name": "Предложение", "csrf": csrf}, files=[("images", ("p.png", png(), "image/png")), ("video", ("v.mp4", VIDEO, "video/mp4"))])
        self.assertEqual(response.status_code, 200, response.text)
        did = response.json()["id"]
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM date_images WHERE date_id=?", (did,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM date_videos WHERE date_id=?", (did,)).fetchone()[0], 1)
        self.assert_closed()

    def test_unavailable_guest_category_rejected_before_spooling(self):
        csrf = self.login()
        self.conn.execute("UPDATE categories SET link_enabled=0 WHERE id=?", (self.cid,))
        self.conn.commit()
        response = self.client.post("/c/upload-category/propose", headers={"X-CSRF-Token": csrf}, data={"name": "Предложение"}, files={"images": ("p.png", png(), "image/png")})
        self.assertEqual(response.status_code, 410)
        self.assertEqual(self.tempfiles, [])

    def test_truncated_multipart_closes_partial_file_without_processing(self):
        self.login()
        content = b'--test\r\nContent-Disposition: form-data; name="avatar"; filename="p.png"\r\nContent-Type: image/png\r\n\r\npartial'
        with patch.object(images, "save_upload") as decode:
            response = self.client.post("/admin/profile", content=content, headers={"Content-Type": "multipart/form-data; boundary=test"})
        self.assertIn(response.status_code, (400, 413))
        decode.assert_not_called()
        self.assert_closed()

    def test_valid_profile_upload_persists_and_closes_parser_files(self):
        csrf = self.login()
        response = self.client.post("/admin/profile", data={"display_name": "Новое имя", "csrf": csrf}, files={"avatar": ("photo.png", png(), "image/png")})
        self.assertIn(response.status_code, (200, 303))
        row = self.conn.execute("SELECT display_name,avatar_path FROM users WHERE id=?", (self.uid,)).fetchone()
        self.assertEqual(row["display_name"], "Новое имя")
        self.assertTrue((images.UPLOAD_DIR / row["avatar_path"]).is_file())
        self.assert_closed()

    def test_maximum_photo_video_batch_still_works(self):
        csrf = self.login()
        files = [("images", (f"photo{i}.png", png(), "image/png")) for i in range(5)]
        files += [("videos", (f"video{i}.mp4", VIDEO, "video/mp4")) for i in range(2)]
        response = self.client.post("/admin/dates/new", data={"name": "Пачка", "csrf": csrf}, files=files)
        self.assertEqual(response.status_code, 303)
        did = self.conn.execute("SELECT id FROM dates WHERE owner_id=?", (self.uid,)).fetchone()[0]
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM date_images WHERE date_id=?", (did,)).fetchone()[0], 5)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM date_videos WHERE date_id=?", (did,)).fetchone()[0], 2)
        self.assert_closed()

    def test_file_limit_rejects_before_image_decode_and_cleans_spool(self):
        csrf = self.login()
        with patch.object(images, "MAX_BYTES", 64), patch.object(images, "save_upload") as decode:
            response = self.client.post("/admin/profile", data={"display_name": "Тест", "csrf": csrf}, files={"avatar": ("photo.png", b"x" * 65, "image/png")})
        self.assertEqual(response.status_code, 413)
        decode.assert_not_called()
        self.assert_closed()

    def test_video_file_limit_rejects_before_media_processing(self):
        csrf = self.login()
        with patch.object(images, "MAX_VIDEO_BYTES", 32), patch.object(images, "save_video") as save:
            response = self.client.post("/admin/dates/new", data={"name": "Видео", "csrf": csrf}, files={"videos": ("video.mp4", VIDEO + b"x" * 33, "video/mp4")})
        self.assertEqual(response.status_code, 413)
        save.assert_not_called()
        self.assert_closed()

    def test_excess_file_parts_are_bounded_and_closed(self):
        csrf = self.login()
        response = self.client.post("/admin/dates/new", data={"name": "Пачка", "csrf": csrf}, files=[("images", (f"p{i}.png", png(), "image/png")) for i in range(9)])
        self.assertEqual(response.status_code, 413)
        self.assertLessEqual(len(self.tempfiles), 7)
        self.assert_closed()

    def test_oversized_text_part_is_rejected_and_prior_file_closed(self):
        csrf = self.login()
        response = self.client.post("/admin/profile", files=[("avatar", ("p.png", png(), "image/png")), ("csrf", (None, csrf)), ("display_name", (None, "x" * (128 * 1024)))])
        self.assertEqual(response.status_code, 413)
        self.assert_closed()

    def test_declared_oversized_body_is_rejected_without_spooling(self):
        csrf = self.login()
        with patch.object(uploads, "MAX_REQUEST_BYTES", 64):
            response = self.client.post("/admin/profile", data={"display_name": "Тест", "csrf": csrf}, files={"avatar": ("p.png", png(), "image/png")})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.tempfiles, [])

    def test_excess_field_parts_are_rejected(self):
        self.login()
        response = self.client.post("/admin/profile", files=[(f"field{i}", (None, "x")) for i in range(129)])
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.tempfiles, [])

    def test_multipart_headers_and_boundary_are_bounded(self):
        self.login()
        cases = [
            ("x" * 201, b"unused"),
            ("test", b'--test\r\nContent-Disposition: form-data; name="avatar"; filename="' + b"x" * (17 * 1024) + b'"\r\n\r\nx\r\n--test--\r\n'),
        ]
        for boundary, content in cases:
            with self.subTest(boundary_length=len(boundary)):
                response = self.client.post("/admin/profile", content=content, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
                self.assertIn(response.status_code, (400, 413))
                self.assertEqual(self.tempfiles, [])

    def test_streamed_body_limit_closes_already_started_file(self):
        self.login()
        prefix = b'--test\r\nContent-Disposition: form-data; name="avatar"; filename="p.png"\r\nContent-Type: image/png\r\n\r\n'
        # Content-Length отсутствует: ограничение должно считать реальные байты.
        def chunks():
            yield prefix + b"x" * 16
            yield b"y" * 128
            yield b"\r\n--test--\r\n"

        with patch.object(uploads, "MAX_REQUEST_BYTES", len(prefix) + 64):
            response = self.client.post("/admin/profile", content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=test"})
        self.assertEqual(response.status_code, 413)
        # ASGI TestClient вправе объединить chunks, поэтому файл мог ещё не открыться.
        self.assertTrue(all(file.closed for file in self.tempfiles))

    def test_disconnected_stream_closes_partial_spool(self):
        self.login()
        cookie = self.client.cookies.get("admin_s")

        async def broken_stream():
            yield b'--test\r\nContent-Disposition: form-data; name="avatar"; filename="p.png"\r\nContent-Type: image/png\r\n\r\npartial'
            raise RuntimeError("synthetic disconnected upload")

        async def request():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                await client.post("/admin/profile", content=broken_stream(), headers={
                    "Content-Type": "multipart/form-data; boundary=test",
                    "Cookie": f"admin_s={cookie}",
                })

        with self.assertRaises(Exception) as caught:
            asyncio.run(request())
        # AnyIO может завернуть ошибку транспорта в ExceptionGroup.
        self.assertIn("synthetic disconnected upload", "".join(traceback.format_exception(caught.exception)))
        self.assert_closed()


if __name__ == "__main__":
    unittest.main(verbosity=2)
