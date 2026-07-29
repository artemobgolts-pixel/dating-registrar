"""Обработка загружаемых фото.

Защита: лимит размера файла (10 МБ), проверка mime-типа, защита от
decompression bomb, лимит разрешения 8000×8000, пережатие в WebP
(EXIF-поворот, длинная сторона ≤ 2560).

save_batch() сначала конвертирует и записывает всю пачку; если любой
файл оказался битым — уже записанные в этой пачке удаляются с диска,
чтобы не оставлять файлов-сирот.
"""

import io
import logging
import os
import re
import secrets
import shutil
import hashlib
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    # HEIC/HEIF с айфонов: регистрируем декодер, дальше Pillow сам
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:                      # pragma: no cover
    pass

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
# Адаптивные копии для карточек и галерей. Это восстанавливаемый кэш, а не
# пользовательские данные: оригиналы по-прежнему лежат только в uploads.
# Версия в имени каталога позволяет безопасно поменять качество/алгоритм позже.
RESPONSIVE_DIR = DATA_DIR / "responsive-v1"
RESPONSIVE_DIR.mkdir(parents=True, exist_ok=True)
# Кэш сгенерированных коллажей-превью ссылок (og:image из фото событий).
# Имя файла = хэш набора исходников, поэтому при смене фото коллаж перегенерится.
OG_CACHE_DIR = DATA_DIR / "og-cache"
OG_CACHE_DIR.mkdir(parents=True, exist_ok=True)

MAX_BYTES = 10 * 1024 * 1024   # 10 МБ на файл
MAX_IMAGES = 5                 # до 5 фото на событие
MAX_SIDE = 2560                # длинная сторона после сжатия
MAX_DIM = 8000                 # максимально допустимое разрешение исходника
# 64/96/128 нужны для маленьких аватаров; 256 — для больших аватаров профиля
# на Retina. Остальные размеры обслуживают карточки и полноэкранные галереи.
RESPONSIVE_WIDTHS = (64, 96, 128, 256, 480, 960, 1600)
RESPONSIVE_QUALITY = 82
log = logging.getLogger("images")

# Decompression-bomb-защита: Pillow откажется декодировать монстров
Image.MAX_IMAGE_PIXELS = MAX_DIM * MAX_DIM

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_-]{8,64}\.(webp|mp4|webm)$")

MAX_VIDEO_BYTES = 60 * 1024 * 1024   # 60 МБ на видео
MAX_VIDEOS = 2                       # до 2 видео на событие
# MP4 faststart — безопасный opt-in: ffmpeg не становится обязательной
# зависимостью контейнера. Если флаг выключен, бинарник отсутствует, remux
# завершился ошибкой или превысил таймаут, сохраняется проверенный оригинал.
VIDEO_FASTSTART = os.getenv("VIDEO_FASTSTART", "").strip().lower() in (
    "1", "true", "yes")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg"
FFMPEG_TIMEOUT = 30


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
    # Новые фото сразу получают варианты для карточек и аватаров. Старые фото
    # по-прежнему обслуживает ленивый fallback в responsive_image().
    generate_responsive_variants(name, image=im)
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
    """Удаляет оригинал и его адаптивные копии (тихо, если их уже нет)."""
    p = UPLOAD_DIR / Path(filename).name
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    if p.suffix.lower() == ".webp":
        for width in RESPONSIVE_WIDTHS:
            try:
                _responsive_path(p.name, width).unlink()
            except FileNotFoundError:
                pass


def _responsive_path(filename: str, width: int) -> Path:
    stem = Path(filename).stem
    return RESPONSIVE_DIR / f"{stem}.w{width}.webp"


def _write_responsive_variant(source: Path, filename: str, width: int,
                              image: Image.Image) -> Path:
    """Атомарно записывает один вариант либо возвращает исходник.

    Возврат исходника для маленьких изображений намеренный: увеличивать их до
    запрошенной ширины бессмысленно. Ошибки кэша не должны ломать оригинал.
    """
    target = _responsive_path(filename, width)
    if target.exists():
        return target
    if image.width <= width:
        return source

    tmp = RESPONSIVE_DIR / (
        f".{target.name}.{os.getpid()}.{secrets.token_urlsafe(5)}.tmp")
    try:
        height = max(1, round(image.height * width / image.width))
        resized = image.resize((width, height), Image.Resampling.LANCZOS)
        if resized.mode in ("P", "LA"):
            resized = resized.convert("RGBA")
        elif resized.mode not in ("RGB", "RGBA"):
            resized = resized.convert("RGB")
        resized.save(tmp, "WEBP", quality=RESPONSIVE_QUALITY, method=4)
        # os.replace атомарен и безопасен при одновременной генерации одного
        # размера несколькими воркерами: победит полностью записанный файл.
        os.replace(tmp, target)
        return target
    except (OSError, ValueError) as exc:
        log.warning("responsive image fallback for %s (%s): %s",
                    filename, width, exc)
        return source
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def generate_responsive_variants(
        filename: str, widths=RESPONSIVE_WIDTHS,
        image: Image.Image | None = None) -> dict[int, Path]:
    """Предгенерирует поддерживаемые WebP-варианты без риска для оригинала.

    ``image`` позволяет save_upload() переиспользовать уже декодированное фото.
    Функция best-effort: любой сбой оставляет ленивую генерацию при первом
    запросе, а слишком маленькое фото обслуживается самим оригиналом.
    """
    name = Path(filename).name
    source = UPLOAD_DIR / name
    if not source.exists() or source.suffix.lower() != ".webp":
        return {}

    wanted = tuple(dict.fromkeys(
        int(width) for width in widths if width in RESPONSIVE_WIDTHS))
    if not wanted:
        return {}
    cached = {
        width: _responsive_path(name, width)
        for width in wanted
        if _responsive_path(name, width).exists()
    }
    if len(cached) == len(wanted):
        return cached

    def build(decoded: Image.Image) -> dict[int, Path]:
        decoded = ImageOps.exif_transpose(decoded)
        return {
            width: _write_responsive_variant(source, name, width, decoded)
            for width in wanted
        }

    if image is not None:
        try:
            return build(image)
        except (OSError, ValueError) as exc:
            log.warning("responsive image pre-generation failed for %s: %s",
                        name, exc)
            return {}

    try:
        with Image.open(source) as opened:
            opened.load()
            return build(opened)
    except (OSError, ValueError) as exc:
        log.warning("responsive image pre-generation failed for %s: %s",
                    name, exc)
        return {}


def responsive_image(filename: str, width: int | None = None) -> Path:
    """Возвращает оригинал либо WebP-копию нужной ширины.

    Для новых загрузок копии уже предгенерированы; недостающая копия создаётся
    лениво и атомарно. Поэтому старые фото работают без миграции, а параллельные
    запросы не увидят недописанный файл. Узкий исходник не увеличиваем.
    """
    name = Path(filename).name
    source = UPLOAD_DIR / name
    if not source.exists():
        raise FileNotFoundError(name)
    if width is None:
        return source
    if width not in RESPONSIVE_WIDTHS or source.suffix.lower() != ".webp":
        raise ValueError("unsupported responsive image width")

    target = _responsive_path(name, width)
    if target.exists():
        return target

    try:
        with Image.open(source) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened)
            return _write_responsive_variant(source, name, width, image)
    except (OSError, ValueError) as exc:
        # Повреждение кэша не должно ломать страницу: оригинал всё ещё можно
        # отдать. Ошибка останется в журнале для диагностики.
        log.warning("responsive image fallback for %s (%s): %s",
                    name, width, exc)
        return source


def copy_file(filename: str) -> str | None:
    """Делает физическую копию файла с новым именем (для клонирования
    события). Расширение сохраняем — отдача завязана на него. Возвращает
    имя копии или None, если оригинала уже нет на диске."""
    src = UPLOAD_DIR / Path(filename).name
    if not src.exists():
        return None
    name = f"{secrets.token_urlsafe(12)}{src.suffix}"
    shutil.copyfile(src, UPLOAD_DIR / name)
    if src.suffix.lower() == ".webp":
        # Уже прогретые варианты можно скопировать без повторного кодирования.
        # Для легаси-фото недостающие размеры достроятся из нового оригинала.
        for width in RESPONSIVE_WIDTHS:
            cached = _responsive_path(src.name, width)
            if not cached.exists():
                continue
            target = _responsive_path(name, width)
            tmp = RESPONSIVE_DIR / (
                f".{target.name}.{os.getpid()}.{secrets.token_urlsafe(5)}.tmp")
            try:
                shutil.copyfile(cached, tmp)
                os.replace(tmp, target)
            except OSError as exc:
                log.warning("responsive cache copy failed for %s (%s): %s",
                            src.name, width, exc)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
        generate_responsive_variants(name)
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


def _faststart_mp4(path: Path) -> bool:
    """Best-effort remux MP4 для начала воспроизведения до полной загрузки.

    Обработка запускается только при VIDEO_FASTSTART=1, без shell и с жёстким
    таймаутом. Неудача не отклоняет пользовательское видео.
    """
    if not VIDEO_FASTSTART or path.suffix.lower() != ".mp4":
        return False
    ffmpeg = shutil.which(FFMPEG_BIN)
    if not ffmpeg:
        log.warning("VIDEO_FASTSTART enabled, but ffmpeg was not found")
        return False

    tmp = path.with_name(
        f".{path.stem}.{os.getpid()}.{secrets.token_urlsafe(5)}.faststart.mp4")
    try:
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", str(path), "-map", "0", "-c", "copy",
             "-movflags", "+faststart", "-f", "mp4", str(tmp)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=FFMPEG_TIMEOUT, check=False,
        )
        valid_output = False
        if tmp.exists() and 0 < tmp.stat().st_size <= MAX_VIDEO_BYTES:
            with tmp.open("rb") as check:
                valid_output = _sniff_video_ext(check.read(16)) == ".mp4"
        if result.returncode != 0 or not valid_output:
            detail = result.stderr.decode("utf-8", "replace")[-500:]
            log.warning("ffmpeg faststart fallback for %s: %s",
                        path.name, detail or f"exit {result.returncode}")
            return False
        os.replace(tmp, path)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffmpeg faststart fallback for %s: %s", path.name, exc)
        return False
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


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
    _faststart_mp4(path)
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


# ---------------------------------------------------------------------------
# Коллаж-превью ссылки (og:image). Когда у категории нет своей картинки, OG
# собирается сеткой из фото её событий: 1 фото — целиком, 2–4 — сетка 2×2,
# 5–8 — сетка 4×2. Размер 1200×630 (стандарт Open Graph). Результат кэшируется
# на диск по хэшу набора исходников — краулеры мессенджеров не пересобирают.
# ---------------------------------------------------------------------------

OG_W, OG_H = 1200, 630


def _grid_for(n: int) -> tuple[int, int]:
    """Колонки×строки сетки под количество фото."""
    if n <= 1:
        return 1, 1
    if n == 2:
        return 2, 1
    if n <= 4:
        return 2, 2
    return 4, 2          # 5..8


def og_collage_name(filenames: list[str]) -> str | None:
    """Имя кэш-файла коллажа для набора фото (хэш отсортированного набора,
    но порядок учитываем — он влияет на раскладку). None, если фото нет."""
    files = [f for f in filenames if f][:8]
    if not files:
        return None
    # Версия входит в ключ: при изменении фирменного оформления старый кэш
    # автоматически перестаёт использоваться.
    h = hashlib.sha256(("brand-v5\n" + "\n".join(files)).encode()).hexdigest()[:24]
    return f"og_{h}.webp"


def build_og_collage(filenames: list[str]) -> str | None:
    """Собирает коллаж из фото событий и кэширует на диск. Возвращает путь к
    готовому файлу (внутри OG_CACHE_DIR) или None, если собрать не из чего.

    Битые/пропавшие исходники пропускаем; сетку берём по факту собранных фото.
    Кэш переиспользуем, если файл уже есть (имя завязано на набор исходников)."""
    files = [f for f in filenames if f][:8]
    if not files:
        return None
    name = og_collage_name(files)
    out = OG_CACHE_DIR / name
    if out.exists():
        return str(out)

    # открываем то, что реально лежит на диске
    imgs = []
    for fn in files:
        p = UPLOAD_DIR / Path(fn).name
        if not p.exists():
            continue
        try:
            im = Image.open(p)
            im.load()
            imgs.append(im.convert("RGB"))
        except Exception:
            continue
    if not imgs:
        return None

    cols, rows = _grid_for(len(imgs))
    canvas = Image.new("RGB", (OG_W, OG_H), (250, 245, 242))
    cell_w = OG_W // cols
    cell_h = OG_H // rows
    for idx in range(cols * rows):
        src = imgs[idx % len(imgs)]        # если фото меньше клеток — повторяем
        tile = ImageOps.fit(src, (cell_w, cell_h), Image.LANCZOS, centering=(0.5, 0.5))
        x = (idx % cols) * cell_w
        y = (idx // cols) * cell_h
        canvas.paste(tile, (x, y))

    # Лёгкая фирменная подпись по центру: она остаётся читаемой и на светлых,
    # и на тёмных кадрах, но не перекрывает сам коллаж плотной плашкой.
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font_path = Path(__file__).parent / "static" / "fonts" / "great-vibes-latin-400-normal.woff2"
        brand_font = ImageFont.truetype(str(font_path), 150)
    except (OSError, ValueError):
        brand_font = ImageFont.load_default()
    center = (OG_W // 2, OG_H // 2)
    draw.text((center[0] + 3, center[1] + 5), "date4you", font=brand_font,
              anchor="mm", fill=(35, 18, 25, 92))
    draw.text(center, "date4you", font=brand_font, anchor="mm",
              fill=(244, 170, 188, 255), stroke_width=1,
              stroke_fill=(122, 45, 67, 210))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    tmp = out.with_suffix(".tmp.webp")
    canvas.save(tmp, "WEBP", quality=82, method=4)
    tmp.replace(out)
    return str(out)


def _parse_focus(focus: str | None) -> tuple[float, float]:
    """«X% Y%» → (0..1, 0..1). Некорректное/пустое → центр (.5, .5)."""
    m = re.fullmatch(r"\s*(\d{1,3})%\s+(\d{1,3})%\s*", focus or "")
    if not m:
        return 0.5, 0.5
    x = min(100, int(m.group(1))) / 100.0
    y = min(100, int(m.group(2))) / 100.0
    return x, y


def build_og_crop(filename: str, focus: str | None) -> str | None:
    """Кроп своей картинки превью категории в 1200×630 по точке фокуса (WYSIWYG
    с редактором: как её двигает владелец, так og:image и выглядит). Кэш на диске
    по (файл, фокус) — краулеры мессенджеров не пересобирают. None, если исходника
    нет/битый."""
    if not filename:
        return None
    src = UPLOAD_DIR / Path(filename).name
    if not src.exists():
        return None
    fx, fy = _parse_focus(focus)
    key = f"{Path(filename).name}|{fx:.2f}|{fy:.2f}"
    h = hashlib.sha256(key.encode()).hexdigest()[:24]
    out = OG_CACHE_DIR / f"ogc_{h}.webp"
    if out.exists():
        return str(out)
    try:
        im = Image.open(src)
        im.load()
        im = im.convert("RGB")
    except Exception:
        return None
    # ImageOps.fit кропает под 1200×630, centering = точка фокуса
    tile = ImageOps.fit(im, (OG_W, OG_H), Image.LANCZOS, centering=(fx, fy))
    tmp = out.with_suffix(".tmp.webp")
    tile.save(tmp, "WEBP", quality=85, method=4)
    tmp.replace(out)
    return str(out)
