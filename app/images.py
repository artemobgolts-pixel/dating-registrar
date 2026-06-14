"""Обработка загружаемых фото.

Защита: лимит размера файла (10 МБ), проверка mime-типа, защита от
decompression bomb, лимит разрешения 8000×8000, пережатие в WebP
(EXIF-поворот, длинная сторона ≤ 2560).

save_batch() сначала конвертирует и записывает всю пачку; если любой
файл оказался битым — уже записанные в этой пачке удаляются с диска,
чтобы не оставлять файлов-сирот.
"""

import io
import os
import re
import secrets
import shutil
from pathlib import Path

from PIL import Image, ImageOps

try:
    # HEIC/HEIF с айфонов: регистрируем декодер, дальше Pillow сам
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:                      # pragma: no cover
    pass

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_BYTES = 10 * 1024 * 1024   # 10 МБ на файл
MAX_IMAGES = 5                 # до 5 фото на свидание
MAX_SIDE = 2560                # длинная сторона после сжатия
MAX_DIM = 8000                 # максимально допустимое разрешение исходника

# Decompression-bomb-защита: Pillow откажется декодировать монстров
Image.MAX_IMAGE_PIXELS = MAX_DIM * MAX_DIM

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_-]{8,64}\.(webp|mp4|webm)$")

MAX_VIDEO_BYTES = 60 * 1024 * 1024   # 60 МБ на видео
MAX_VIDEOS = 2                       # до 2 видео на свидание


def save_upload(upload) -> str:
    """Принимает UploadFile, сохраняет как .webp, возвращает имя файла.

    Бросает ValueError с понятным русским текстом, если файл не подходит.
    """
    # mime — лишь подсказка клиента (iOS через «Файлы» шлёт octet-stream);
    # настоящая проверка — декодирование Pillow ниже
    ctype = (getattr(upload, "content_type", "") or "").lower()
    if ctype and not (ctype.startswith("image/") or ctype == "application/octet-stream"):
        raise ValueError("Можно загружать только изображения")

    data = upload.file.read(MAX_BYTES + 1)
    if not data:
        raise ValueError("Пустой файл")
    if len(data) > MAX_BYTES:
        raise ValueError("Файл больше 10 МБ")

    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Image.DecompressionBombError:
        raise ValueError(f"Слишком большое изображение (максимум {MAX_DIM}×{MAX_DIM})")
    except Exception:
        raise ValueError("Файл не похож на изображение")

    if im.width > MAX_DIM or im.height > MAX_DIM:
        raise ValueError(f"Слишком большое разрешение (максимум {MAX_DIM}×{MAX_DIM})")

    im = ImageOps.exif_transpose(im)

    if im.mode in ("P", "LA"):
        im = im.convert("RGBA")
    elif im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")

    im.thumbnail((MAX_SIDE, MAX_SIDE))

    name = f"{secrets.token_urlsafe(12)}.webp"
    im.save(UPLOAD_DIR / name, "WEBP", quality=85, method=4)
    return name


def save_batch(uploads) -> list[str]:
    """Сохраняет пачку файлов атомарно по принципу «всё или ничего».

    Возвращает список имён. Если какой-то файл битый — удаляет уже
    записанные из этой пачки и пробрасывает ValueError дальше.
    """
    saved: list[str] = []
    try:
        for f in uploads:
            saved.append(save_upload(f))
    except ValueError:
        for name in saved:
            delete_file(name)
        raise
    return saved


def delete_file(filename: str) -> None:
    """Удаляет файл фото с диска (тихо, если его уже нет)."""
    p = UPLOAD_DIR / Path(filename).name
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def copy_file(filename: str) -> str | None:
    """Делает физическую копию файла с новым именем (для клонирования
    свидания). Расширение сохраняем — отдача завязана на него. Возвращает
    имя копии или None, если оригинала уже нет на диске."""
    src = UPLOAD_DIR / Path(filename).name
    if not src.exists():
        return None
    name = f"{secrets.token_urlsafe(12)}{src.suffix}"
    shutil.copyfile(src, UPLOAD_DIR / name)
    return name


# ---------------------------------------------------------------------------
# Видео: принимаем mp4/webm как есть (без перекодирования), валидируем
# по сигнатуре файла, а не по mime, и пишем на диск потоково.
# ---------------------------------------------------------------------------

def _sniff_video_ext(head: bytes) -> str | None:
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return ".mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"
    return None


def save_video(upload) -> str:
    """Сохраняет видео, возвращает имя файла. ValueError — если не подходит."""
    head = upload.file.read(16)
    if not head:
        raise ValueError("Пустой файл видео")
    ext = _sniff_video_ext(head)
    if not ext:
        raise ValueError("Видео должно быть mp4 или webm (mov сначала сконвертируй)")
    name = f"{secrets.token_urlsafe(12)}{ext}"
    path = UPLOAD_DIR / name
    written = 0
    try:
        with open(path, "wb") as out:
            out.write(head)
            written = len(head)
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_VIDEO_BYTES:
                    raise ValueError("Видео больше 60 МБ")
                out.write(chunk)
    except ValueError:
        delete_file(name)
        raise
    return name


def save_videos_batch(uploads) -> list[str]:
    """Сохраняет пачку видео атомарно по принципу «всё или ничего».

    Возвращает список имён. Если какой-то файл битый или превышает лимит —
    удаляет уже записанные из этой пачки и пробрасывает ValueError дальше.
    """
    saved: list[str] = []
    try:
        for f in uploads:
            saved.append(save_video(f))
    except ValueError:
        for name in saved:
            delete_file(name)
        raise
    return saved
