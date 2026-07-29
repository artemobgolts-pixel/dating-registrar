#!/usr/bin/env python3
"""Точечные тесты предгенерации изображений и ленивого видео.

Запуск из корня репозитория:
    python tests/test_media_optimization.py
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-media-import-")
os.environ["DATA_DIR"] = _IMPORT_DATA.name

import images  # noqa: E402
from PIL import Image  # noqa: E402


class Upload:
    content_type = "image/png"

    def __init__(self, data: bytes):
        self.file = io.BytesIO(data)


def png(size=(2000, 1000)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (170, 80, 105)).save(out, "PNG")
    return out.getvalue()


class ResponsiveImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="date4you-media-")
        self.root = Path(self.tmp.name)
        self.old_upload = images.UPLOAD_DIR
        self.old_responsive = images.RESPONSIVE_DIR
        images.UPLOAD_DIR = self.root / "uploads"
        images.RESPONSIVE_DIR = self.root / "responsive"
        images.UPLOAD_DIR.mkdir()
        images.RESPONSIVE_DIR.mkdir()

    def tearDown(self):
        images.UPLOAD_DIR = self.old_upload
        images.RESPONSIVE_DIR = self.old_responsive
        self.tmp.cleanup()

    def test_upload_prewarms_card_and_avatar_variants(self):
        filename = images.save_upload(Upload(png()))
        for width in images.RESPONSIVE_WIDTHS:
            variant = images.RESPONSIVE_DIR / (
                f"{Path(filename).stem}.w{width}.webp")
            self.assertTrue(variant.exists(), width)
            with Image.open(variant) as opened:
                self.assertEqual(opened.width, width)
                self.assertEqual(opened.height, width // 2)

    def test_legacy_image_keeps_lazy_fallback(self):
        filename = "legacy_photo.webp"
        Image.new("RGB", (1200, 600), "navy").save(
            images.UPLOAD_DIR / filename, "WEBP")
        variant = images.RESPONSIVE_DIR / "legacy_photo.w480.webp"
        self.assertFalse(variant.exists())
        self.assertEqual(images.responsive_image(filename, 480), variant)
        self.assertTrue(variant.exists())

    def test_clone_copies_cache_and_generates_missing_variant(self):
        filename = images.save_upload(Upload(png()))
        missing = images.RESPONSIVE_DIR / (
            f"{Path(filename).stem}.w960.webp")
        missing.unlink()

        clone = images.copy_file(filename)

        self.assertIsNotNone(clone)
        for width in images.RESPONSIVE_WIDTHS:
            variant = images.RESPONSIVE_DIR / (
                f"{Path(clone).stem}.w{width}.webp")
            self.assertTrue(variant.exists(), width)


class FaststartTests(unittest.TestCase):
    def test_missing_ffmpeg_leaves_original_untouched(self):
        with tempfile.TemporaryDirectory(prefix="date4you-video-") as td:
            path = Path(td) / "sample.mp4"
            original = b"\0\0\0\x18ftypmp42" + b"x" * 32
            path.write_bytes(original)
            with patch.object(images, "VIDEO_FASTSTART", True), \
                    patch.object(images.shutil, "which", return_value=None):
                self.assertFalse(images._faststart_mp4(path))
            self.assertEqual(path.read_bytes(), original)

    def test_successful_faststart_atomically_replaces_file(self):
        with tempfile.TemporaryDirectory(prefix="date4you-video-") as td:
            path = Path(td) / "sample.mp4"
            path.write_bytes(b"original")

            class Result:
                returncode = 0
                stderr = b""

            def fake_run(args, **_kwargs):
                Path(args[-1]).write_bytes(
                    b"\0\0\0\x18ftypmp42" + b"optimized")
                return Result()

            with patch.object(images, "VIDEO_FASTSTART", True), \
                    patch.object(images.shutil, "which", return_value="ffmpeg"), \
                    patch.object(images.subprocess, "run", side_effect=fake_run):
                self.assertTrue(images._faststart_mp4(path))
            self.assertEqual(
                path.read_bytes(), b"\0\0\0\x18ftypmp42" + b"optimized")


class FrontendMediaContractTests(unittest.TestCase):
    def test_vote_ui_updates_cards_without_page_reload(self):
        guest = (APP / "static/guest.js").read_text(encoding="utf-8")
        vote_block = guest.split("async function doBook", 1)[1].split(
            'document.querySelectorAll(".withdraw-vote")', 1)[0]
        self.assertIn("applyVoteUpdate", vote_block)
        self.assertIn("renderParticipants", guest)
        self.assertNotIn("location.reload", vote_block)

    def test_persisted_videos_have_lazy_source_and_poster(self):
        templates = (
            APP / "templates/public/category.html",
            APP / "templates/public/share.html",
            APP / "templates/admin/date_form.html",
            APP / "templates/admin/_community_widget.html",
        )
        for path in templates:
            text = path.read_text(encoding="utf-8")
            self.assertIn("<video data-src=", text, path.name)
            self.assertIn('preload="none"', text, path.name)
            self.assertIn("poster=", text, path.name)

        ui = (APP / "static/ui.js").read_text(encoding="utf-8")
        self.assertIn('querySelectorAll("video[data-src]")', ui)
        self.assertIn("IntersectionObserver", ui)
        self.assertIn('"pointerdown"', ui)

    def test_small_avatar_srcsets_are_used(self):
        templates = (
            APP / "templates/public/category.html",
            APP / "templates/public/share.html",
            APP / "templates/admin/_community_cards.html",
            APP / "templates/admin/_community_widget.html",
        )
        for path in templates:
            text = path.read_text(encoding="utf-8")
            self.assertIn("?w=64", text, path.name)
            self.assertIn("?w=96 96w", text, path.name)
            self.assertIn("?w=128 128w", text, path.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
