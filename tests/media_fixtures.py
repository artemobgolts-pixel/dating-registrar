"""Маленькие настоящие контейнеры для проверок загрузки без подмены probe.

Созданы локальным FFmpeg: color=navy, 32x24, 5 fps, 0.4 секунды;
MP4 — libx264/yuv420p/+faststart, WebM — libvpx-vp9.
audio-only.mp4: anullsrc, mono, 8000 Hz, 0.4 секунды, AAC.
Файлы заморожены, поэтому генератор и внешние источники тестам не нужны.
"""

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=3)
def video_bytes(kind="mp4") -> bytes:
    filenames = {"mp4": "tiny.mp4", "webm": "tiny.webm", "audio-only": "audio-only.mp4"}
    return (Path(__file__).resolve().parent / "fixtures" / "media" / filenames[kind]).read_bytes()
