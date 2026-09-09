#!/usr/bin/env python3
"""SEC-04: ранние лимиты декодирования и очистка файлов загрузки.

Все данные синтетические; размеры для проверки лимитов остаются маленькими.
Запуск: python tests/test_image_upload_security.py
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import Mock, patch


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-image-security-import-")
os.environ["DATA_DIR"] = _IMPORT_DATA.name

import images  # noqa: E402
from PIL import Image  # noqa: E402


class Upload:
    def __init__(self, data: bytes, content_type="image/png"):
        self.file = io.BytesIO(data)
        self.content_type = content_type


def png(size=(320, 160), mode="RGB") -> bytes:
    out = io.BytesIO()
    with Image.new(mode, size) as image:
        image.save(out, "PNG")
    return out.getvalue()


class ImageUploadSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="date4you-image-security-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, path in (
                ("UPLOAD_DIR", self.root / "uploads"),
                ("RESPONSIVE_DIR", self.root / "responsive")):
            path.mkdir()
            self.enterContext(patch.object(images, name, path))

    def assert_no_artifacts(self):
        self.assertEqual(list(images.UPLOAD_DIR.iterdir()), [])
        self.assertEqual(list(images.RESPONSIVE_DIR.iterdir()), [])

    def test_real_skinny_png_exceeds_dimension_limit(self):
        for size in ((8001, 1), (1, 8001)):
            with self.subTest(size=size):
                with self.assertRaisesRegex(ValueError, "максимум 8000×8000"):
                    images.save_upload(Upload(png(size)))
                self.assert_no_artifacts()

    def test_oversized_header_is_rejected_without_pixel_decode(self):
        opened = Mock(width=8001, height=1)
        with patch.object(images.Image, "open", return_value=opened):
            with self.assertRaisesRegex(ValueError, "максимум 8000×8000"):
                images.save_upload(Upload(b"synthetic header"))
        opened.load.assert_not_called()
        opened.close.assert_called_once_with()
        self.assert_no_artifacts()

    def test_file_byte_limit_is_checked_before_pillow_open(self):
        upload = Upload(b"x" * 100)
        with patch.object(images, "MAX_BYTES", 32), \
                patch.object(images.Image, "open") as open_image:
            with self.assertRaisesRegex(ValueError, "Файл больше"):
                images.save_upload(upload)
        open_image.assert_not_called()
        self.assertEqual(upload.file.tell(), 33)
        self.assert_no_artifacts()

    def test_pillow_bomb_warning_and_error_reject_small_synthetic_image(self):
        data = png((6, 4))
        # 24 пикселя превышают порог предупреждения 16 и удвоенный порог 8.
        for max_pixels in (16, 8):
            with self.subTest(max_pixels=max_pixels), \
                    patch.object(images.Image, "MAX_IMAGE_PIXELS", max_pixels):
                with self.assertRaisesRegex(ValueError, "Слишком большое изображение"):
                    images.save_upload(Upload(data))
                self.assert_no_artifacts()

    def test_warning_during_load_rejects_and_closes_image(self):
        def warn_on_load():
            warnings.warn("synthetic bomb warning", Image.DecompressionBombWarning)

        opened = Mock(width=1, height=1)
        opened.load.side_effect = warn_on_load
        with patch.object(images.Image, "open", return_value=opened):
            with self.assertRaisesRegex(ValueError, "Слишком большое изображение"):
                images.save_upload(Upload(b"synthetic header"))
        opened.close.assert_called_once_with()
        self.assert_no_artifacts()

    def test_invalid_image_and_failed_decode_leave_no_artifacts(self):
        with self.assertRaisesRegex(ValueError, "Файл не похож"):
            images.save_upload(Upload(b"not a photo"))
        opened = Mock(width=1, height=1)
        opened.load.side_effect = OSError("synthetic decode failure")
        with patch.object(images.Image, "open", return_value=opened):
            with self.assertRaisesRegex(ValueError, "Файл не похож"):
                images.save_upload(Upload(b"synthetic header"))
        opened.close.assert_called_once_with()
        self.assert_no_artifacts()

    def test_valid_photo_is_closed_and_has_responsive_variants(self):
        data = png(mode="L")
        opened = Image.open(io.BytesIO(data))
        with patch.object(images.Image, "open", return_value=opened):
            filename = images.save_upload(Upload(data, "application/octet-stream"))
        with self.assertRaises(ValueError):
            opened.getpixel((0, 0))
        with Image.open(images.UPLOAD_DIR / filename) as stored:
            self.assertEqual(stored.format, "WEBP")
            self.assertEqual(stored.size, (320, 160))
            self.assertEqual(stored.mode, "RGB")
        for width in (64, 96, 128, 256):
            variant = images.RESPONSIVE_DIR / f"{Path(filename).stem}.w{width}.webp"
            with Image.open(variant) as stored:
                self.assertEqual(stored.size, (width, width // 2))

    def test_partial_original_is_deleted_on_encoder_failure(self):
        data = png()

        def fail_save(_image, destination, *_args, **_kwargs):
            Path(destination).write_bytes(b"partial webp")
            raise OSError("synthetic encoder failure")

        with patch.object(images.Image.Image, "save", fail_save):
            with self.assertRaisesRegex(OSError, "synthetic encoder failure"):
                images.save_upload(Upload(data))
        self.assert_no_artifacts()

    def test_unexpected_responsive_failure_rolls_back_original_and_variants(self):
        write_variant = images._write_responsive_variant

        def fail_second_variant(source, filename, width, image):
            if width == 96:
                raise RuntimeError("synthetic resize failure")
            return write_variant(source, filename, width, image)

        with patch.object(images, "_write_responsive_variant", fail_second_variant):
            with self.assertRaisesRegex(RuntimeError, "synthetic resize failure"):
                images.save_upload(Upload(png()))
        self.assert_no_artifacts()

    def test_responsive_write_failure_cleans_temp_and_keeps_valid_original(self):
        replace = images.os.replace

        def fail_first_variant(source, target):
            if Path(target).name.endswith(".w64.webp"):
                raise OSError("synthetic cache write failure")
            return replace(source, target)

        with patch.object(images.os, "replace", fail_first_variant), \
                self.assertLogs("images", level="WARNING"):
            filename = images.save_upload(Upload(png()))
        with Image.open(images.UPLOAD_DIR / filename) as stored:
            stored.load()
        self.assertFalse(any(path.name.endswith(".tmp")
                             for path in images.RESPONSIVE_DIR.iterdir()))
        self.assertFalse(images._responsive_path(filename, 64).exists())
        self.assertTrue(images._responsive_path(filename, 96).exists())

    def test_photo_batch_rolls_back_after_invalid_second_photo(self):
        with self.assertRaises(ValueError):
            images.save_batch([Upload(png()), Upload(b"invalid photo")])
        self.assert_no_artifacts()

    def test_photo_batch_rolls_back_after_second_encoder_io_failure(self):
        data = png()
        save = Image.Image.save
        originals_written = 0

        def fail_second_original(image, destination, *args, **kwargs):
            nonlocal originals_written
            if Path(destination).parent == images.UPLOAD_DIR:
                originals_written += 1
                if originals_written == 2:
                    Path(destination).write_bytes(b"partial webp")
                    raise OSError("synthetic second write failure")
            return save(image, destination, *args, **kwargs)

        with patch.object(images.Image.Image, "save", fail_second_original):
            with self.assertRaisesRegex(OSError, "synthetic second write failure"):
                images.save_batch([Upload(data), Upload(data)])
        self.assertEqual(originals_written, 2)
        self.assert_no_artifacts()

    def test_valid_photo_batch_preserves_all_originals(self):
        filenames = images.save_batch([Upload(png()), Upload(png((100, 50)))])
        self.assertEqual(len(set(filenames)), 2)
        for filename in filenames:
            with Image.open(images.UPLOAD_DIR / filename) as stored:
                stored.load()

    def test_valid_video_batch_preserves_mp4_and_webm(self):
        payloads = [b"\0\0\0\x18ftypmp42" + b"x" * 24,
                    b"\x1aE\xdf\xa3" + b"x" * 24]
        with patch.object(images, "VIDEO_FASTSTART", False):
            filenames = images.save_videos_batch([
                Upload(data, "video/mp4") for data in payloads])
        self.assertEqual([Path(name).suffix for name in filenames], [".mp4", ".webm"])
        for filename, data in zip(filenames, payloads):
            self.assertEqual((images.UPLOAD_DIR / filename).read_bytes(), data)

    def test_video_batch_limit_failure_cleans_prior_and_partial_files(self):
        header = b"\0\0\0\x18ftypmp42"
        with patch.object(images, "MAX_VIDEO_BYTES", 32), \
                patch.object(images, "VIDEO_FASTSTART", False):
            with self.assertRaisesRegex(ValueError, "Видео больше"):
                images.save_videos_batch([Upload(header), Upload(header + b"x" * 24)])
        self.assert_no_artifacts()

    def test_video_batch_io_failure_cleans_prior_and_partial_files(self):
        class FailedRead(io.BytesIO):
            def read(self, size=-1):
                if self.tell() >= 16:
                    raise OSError("synthetic video read failure")
                return super().read(size)

        header = b"\0\0\0\x18ftypmp42" + b"x" * 24
        failed = Upload(header)
        failed.file = FailedRead(header)
        with patch.object(images, "VIDEO_FASTSTART", False):
            with self.assertRaisesRegex(OSError, "synthetic video read failure"):
                images.save_videos_batch([Upload(header), failed])
        self.assert_no_artifacts()


if __name__ == "__main__":
    unittest.main(verbosity=2)
