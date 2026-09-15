#!/usr/bin/env python3
"""FUN-01: повреждённое видео не публикуется и не оставляет записей или файлов."""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_upload_security as upload_fixture
from media_fixtures import video_bytes


images = upload_fixture.images
SIGNATURE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24
SIGNATURE_WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 24


class Upload:
    def __init__(self, data: bytes):
        self.file = io.BytesIO(data)
        self.content_type = "application/octet-stream"


class VideoValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="date4you-video-validation-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.object(images, "UPLOAD_DIR", self.root))
        self.enterContext(patch.object(images, "VIDEO_FASTSTART", False))

    def test_signature_only_mp4_is_rejected_and_removed(self):
        with self.assertRaisesRegex(ValueError, "[Вв]идео"):
            images.save_video(Upload(SIGNATURE_MP4))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_signature_only_webm_is_rejected_and_removed(self):
        with self.assertRaisesRegex(ValueError, "[Вв]идео"):
            images.save_video(Upload(SIGNATURE_WEBM))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_valid_mp4_is_preserved(self):
        payload = video_bytes("mp4")
        filename = images.save_video(Upload(payload))
        self.assertTrue(filename.endswith(".mp4"))
        self.assertEqual((self.root / filename).read_bytes(), payload)
        self.assertEqual(list(self.root.iterdir()), [self.root / filename])

    def test_valid_webm_is_preserved(self):
        payload = video_bytes("webm")
        filename = images.save_video(Upload(payload))
        self.assertTrue(filename.endswith(".webm"))
        self.assertEqual((self.root / filename).read_bytes(), payload)
        self.assertEqual(list(self.root.iterdir()), [self.root / filename])

    def test_valid_mp4_faststart_is_rechecked_and_published_without_temporary_files(self):
        with patch.object(images, "VIDEO_FASTSTART", True):
            with patch.object(images, "_probe_video", wraps=images._probe_video) as probe:
                filename = images.save_video(Upload(video_bytes()))
        self.assertEqual(probe.call_count, 2)
        self.assertFalse(filename.startswith("."))
        self.assertEqual(Path(filename).name, filename)
        self.assertEqual(list(self.root.iterdir()), [self.root / filename])
        images._probe_video(self.root / filename, ".mp4")

    def test_truncated_containers_are_rejected_and_removed(self):
        for kind in ("mp4", "webm"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "[Вв]идео"):
                    images.save_video(Upload(video_bytes(kind)[:64]))
                self.assertEqual(list(self.root.iterdir()), [])

    def test_audio_only_mp4_is_rejected_and_removed(self):
        with self.assertRaisesRegex(ValueError, "[Вв]идео"):
            images.save_video(Upload(video_bytes("audio-only")))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_corrupt_second_video_rolls_back_first_video(self):
        with self.assertRaisesRegex(ValueError, "[Вв]идео"):
            images.save_videos_batch([Upload(video_bytes()), Upload(SIGNATURE_WEBM)])
        self.assertEqual(list(self.root.iterdir()), [])

    def _probe_success(self, metadata=None):
        if metadata is None:
            metadata = {
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 32, "height": 24}],
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "0.4"},
            }
        return subprocess.CompletedProcess(
            ["ffprobe"], 0, json.dumps(metadata).encode(), b"",
        )

    def test_probe_timeout_failure_or_unavailable_binary_cleans_temp(self):
        failures = (
            subprocess.TimeoutExpired(["ffprobe"], 10),
            OSError("synthetic failed process start"),
            subprocess.CompletedProcess(["ffprobe"], 1, b"", b"synthetic probe error"),
            subprocess.CompletedProcess(["ffprobe"], 0, b"{}", b"synthetic probe warning"),
        )
        for failure in failures:
            with self.subTest(failure=repr(failure)):
                options = ({"side_effect": failure} if isinstance(failure, Exception)
                           else {"return_value": failure})
                with patch.object(images.subprocess, "run", **options):
                    with self.assertRaisesRegex(ValueError, "[Вв]идео"):
                        images.save_video(Upload(video_bytes()))
                self.assertEqual(list(self.root.iterdir()), [])
        with patch.object(images.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "[Вв]идео"):
                images.save_video(Upload(video_bytes()))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_malformed_metadata_is_rejected_and_removed(self):
        invalid = [b"not-json", b"null", b"[]", b"{}", b"x" * 16385]
        for key, value in (
            ("width", 0), ("width", -1), ("width", "32"), ("width", True),
            ("width", 1000000000), ("height", None), ("codec_name", "unknown"),
            ("codec_name", []), ("codec_type", "audio"),
        ):
            metadata = json.loads(self._probe_success().stdout)
            metadata["streams"][0][key] = value
            invalid.append(json.dumps(metadata).encode())
        for key, value in (
            ("duration", "NaN"), ("duration", "Infinity"), ("duration", "0"),
            ("duration", "-1"), ("duration", []), ("format_name", None),
            ("format_name", 42), ("format_name", "matroska,webm"),
        ):
            metadata = json.loads(self._probe_success().stdout)
            metadata["format"][key] = value
            invalid.append(json.dumps(metadata).encode())
        for stream_value in ([], [None], [{}]):
            metadata = json.loads(self._probe_success().stdout)
            metadata["streams"] = stream_value
            invalid.append(json.dumps(metadata).encode())

        for payload in invalid:
            with self.subTest(metadata=payload[:250]):
                result = subprocess.CompletedProcess(["ffprobe"], 0, payload, b"")
                with patch.object(images.subprocess, "run", return_value=result) as probe:
                    with self.assertRaisesRegex(ValueError, "[Вв]идео"):
                        images.save_video(Upload(video_bytes()))
                probe.assert_called_once()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_or_empty_decode_is_rejected_and_removed(self):
        failures = (
            subprocess.TimeoutExpired(["ffmpeg"], 10),
            subprocess.CompletedProcess(["ffmpeg"], 1, b"", b"synthetic decode error"),
            subprocess.CompletedProcess(["ffmpeg"], 0, b"frame=0\nprogress=end\n", b""),
            subprocess.CompletedProcess(["ffmpeg"], 0, b"frame=1\n", b"decode error"),
        )
        for failure in failures:
            with self.subTest(failure=repr(failure)):
                with patch.object(images.subprocess, "run", side_effect=[self._probe_success(), failure]):
                    with self.assertRaisesRegex(ValueError, "[Вв]идео"):
                        images.save_video(Upload(video_bytes()))
                self.assertEqual(list(self.root.iterdir()), [])

    def test_probe_and_decode_are_bounded_and_forbid_network_protocols(self):
        with patch.object(images.subprocess, "run", wraps=subprocess.run) as run:
            filename = images.save_video(Upload(video_bytes()))
        self.assertEqual((self.root / filename).read_bytes(), video_bytes())
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            args = call.args[0]
            self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file")
            self.assertEqual(set(args[args.index("-format_whitelist") + 1].split(",")),
                             {"mov", "matroska", "webm"})
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 10)
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertFalse(call.kwargs.get("shell", False))
            self.assertIn("-max_alloc", args)
            self.assertIn("-probesize", args)
            self.assertIn("-analyzeduration", args)

    def test_byte_limit_applies_before_probe_including_initial_header(self):
        for limit in (8, len(video_bytes()) - 1):
            with self.subTest(limit=limit), patch.object(images, "MAX_VIDEO_BYTES", limit):
                with patch.object(images.subprocess, "run") as probe:
                    with self.assertRaisesRegex(ValueError, "Видео больше"):
                        images.save_video(Upload(video_bytes()))
                probe.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])


class VideoValidationHttpTests(upload_fixture.UploadSecurityTests):
    def _media_snapshot(self):
        return {(label, path.relative_to(root)): path.read_bytes()
                for label, root in (("uploads", images.UPLOAD_DIR), ("responsive", images.RESPONSIVE_DIR))
                for path in root.rglob("*") if path.is_file()}

    def _assert_rejected_video(self, url, field, payload, csrf=None):
        csrf = csrf or self.login()
        before_db = list(self.conn.iterdump())
        before_files = self._media_snapshot()
        response = self.client.post(
            url, headers={"X-CSRF-Token": csrf},
            data={"name": "Повреждённое видео", "csrf": csrf},
            files=[("images", ("photo.png", upload_fixture.png(), "image/png")),
                   (field, ("broken.mp4", payload, "video/mp4"))],
        )
        self.assertEqual(response.status_code, 422 if url.startswith("/admin/") else 400, response.text)
        self.assertIn("видео", response.text.lower())
        self.assertEqual(list(self.conn.iterdump()), before_db)
        self.assertEqual(self._media_snapshot(), before_files)
        self.assert_closed()

    def test_admin_create_rejects_signature_only_mp4_atomically(self):
        self._assert_rejected_video("/admin/dates/new", "videos", SIGNATURE_MP4)

    def test_admin_create_rejects_signature_only_webm_atomically(self):
        self._assert_rejected_video("/admin/dates/new", "videos", SIGNATURE_WEBM)

    def test_guest_proposal_rejects_signature_only_video_atomically(self):
        self._assert_rejected_video("/c/upload-category/propose", "video", SIGNATURE_MP4)

    def test_admin_edit_rejects_corrupt_video_without_changing_existing_event(self):
        self.conn.execute("UPDATE dates SET owner_id=? WHERE id=?", (self.uid, self.did))
        self.conn.commit()
        self._assert_rejected_video(f"/admin/dates/{self.did}/edit", "videos", SIGNATURE_MP4)

    def test_guest_edit_rejects_corrupt_replacement_and_keeps_existing_media(self):
        csrf = self.login()
        response = self.client.post(
            "/c/upload-category/propose", headers={"X-CSRF-Token": csrf},
            data={"name": "Исходное предложение", "csrf": csrf},
            files=[("images", ("photo.png", upload_fixture.png(), "image/png")),
                   ("video", ("valid.mp4", video_bytes(), "video/mp4"))],
        )
        self.assertEqual(response.status_code, 200, response.text)
        did = response.json()["id"]
        self._assert_rejected_video(
            f"/c/upload-category/propose/{did}/edit", "video", SIGNATURE_WEBM, csrf,
        )

    def test_valid_five_photos_and_two_video_formats_are_persisted(self):
        csrf = self.login()
        files = [("images", (f"photo{i}.png", upload_fixture.png(), "image/png")) for i in range(5)]
        files += [("videos", (f"tiny.{kind}", video_bytes(kind), f"video/{kind}"))
                  for kind in ("mp4", "webm")]
        response = self.client.post(
            "/admin/dates/new", data={"name": "Пять фото и два видео", "csrf": csrf}, files=files,
        )
        self.assertEqual(response.status_code, 303, response.text)
        did = self.conn.execute("SELECT id FROM dates WHERE owner_id=?", (self.uid,)).fetchone()[0]
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM date_images WHERE date_id=?", (did,)).fetchone()[0], 5)
        videos = self.conn.execute("SELECT filename FROM date_videos WHERE date_id=? ORDER BY position,id", (did,)).fetchall()
        self.assertEqual(len(videos), 2)
        for row, kind in zip(videos, ("mp4", "webm")):
            self.assertEqual((images.UPLOAD_DIR / row["filename"]).read_bytes(), video_bytes(kind))
        self.assertFalse(any(".pending" in path.name for path in images.UPLOAD_DIR.iterdir()))
        self.assert_closed()


def load_tests(loader, tests, pattern):
    # Общая HTTP-fixture используется без повторного запуска её 18 тестов.
    return unittest.TestSuite(
        cls(name)
        for cls in (VideoValidationTests, VideoValidationHttpTests)
        for name in cls.__dict__ if name.startswith("test_")
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
