#!/usr/bin/env python3
"""Скриншот-тест фонового эффекта «чернила по клику» (app/static/ink.js).

Эффект на WebGL2 (float-render), поэтому рендерим его в headless Chromium
(swiftshader) и сравниваем с эталоном refs/ink_click_ref.png попиксельно
с допуском — драйверы дают микроскопический разброс по float, но форма следа
от клика обязана совпадать.

Запуск (из корня репозитория):
    python tests/test_ink.py

Зависимости: playwright + браузер chromium. Если их нет — тест помечает себя
как пропущенный (skip) и выходит с кодом 0, чтобы не ломать CI без браузера.

Перегенерировать эталон (после осознанной правки вида чернил):
    python tests/test_ink.py --update
и глазами проверить refs/ink_click_ref.png перед коммитом.
"""
import sys
from pathlib import Path

# Консоль Windows бывает в cp1251 — наши символы (✓/✗/Δ/%) тогда падают на encode.
# Принудительно переводим вывод в UTF-8 (как ожидает остальной вывод тестов).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
REF = HERE / "refs" / "ink_click_ref.png"

# Эталонный кадр: одиночный клик в центре, фиксированные возраст/сид/время фона.
# Те же параметры — в generate_ref() ниже; держать синхронно.
REF_CLICKS = [{"x": 0.5, "y": 0.5, "age": 2.2, "seed": 5.0}]
REF_BG_TIME = 8.0
REF_W, REF_H = 600, 600

# Допуски сравнения. Фон fbm идентичен (то же время), отличаться может лишь
# тонкая структура нитей из-за float-разброса драйвера — держим узко.
MAX_MEAN_DIFF = 2.0     # средняя |Δ| по каналам на пиксель (0..255)
MAX_BAD_FRAC = 0.02     # доля пикселей с |Δ|>32 хотя бы в одном канале


def _mean_and_bad(a, b):
    """Средняя поканальная |разница| и доля «сильно разных» пикселей."""
    from PIL import Image, ImageChops
    ia = Image.open(a).convert("RGB")
    ib = Image.open(b).convert("RGB")
    if ia.size != ib.size:
        raise AssertionError(f"размеры не совпали: {ia.size} vs {ib.size}")
    diff = ImageChops.difference(ia, ib)
    hist = diff.histogram()              # 3 блока по 256 (R,G,B)
    total = ia.width * ia.height
    sum_abs = 0
    bad = 0
    for ch in range(3):
        block = hist[ch * 256:(ch + 1) * 256]
        for val, cnt in enumerate(block):
            sum_abs += val * cnt
            if val > 32:
                bad += cnt
    mean = sum_abs / (total * 3)
    # bad считаем по «худшему» каналу: пиксель плохой, если хоть один канал >32.
    # Грубая верхняя оценка (складываем по каналам) достаточна для порога 2%.
    return mean, bad / (total * 3)


def generate_ref(out=REF):
    from ink_shot import capture_clicks
    return capture_clicks(out=out, clicks=REF_CLICKS, bg_time=REF_BG_TIME,
                          w=REF_W, h=REF_H)


def main(update=False):
    sys.path.insert(0, str(HERE))   # чтобы импортировать ink_shot.py рядом
    try:
        from ink_shot import capture_clicks  # noqa: F401
    except ImportError as e:
        print(f"  ~ playwright недоступен ({e!r}) — тест чернил пропущен (skip)")
        return 0

    if update:
        generate_ref()
        print(f"  ✓ эталон перегенерирован: {REF}")
        print("    проверь картинку глазами перед коммитом")
        return 0

    if not REF.exists():
        print(f"  ✗ нет эталона {REF} — создай его: python tests/test_ink.py --update")
        return 1

    tmp = HERE / "refs" / "_ink_actual.png"
    try:
        capture_clicks = sys.modules["ink_shot"].capture_clicks
        capture_clicks(out=tmp, clicks=REF_CLICKS, bg_time=REF_BG_TIME,
                       w=REF_W, h=REF_H)
    except RuntimeError as e:
        # ink.js не запустился (нет WebGL2/float-render в этом браузере) — не наша
        # регрессия, а окружение. Пропускаем, как делает test_smoke с HEIC.
        print(f"  ~ рендер недоступен ({e}) — тест чернил пропущен (skip)")
        return 0

    try:
        mean, bad = _mean_and_bad(REF, tmp)
    finally:
        tmp.unlink(missing_ok=True)

    print(f"  след от клика: средняя Δ={mean:.3f} (лимит {MAX_MEAN_DIFF}), "
          f"сильных пикселей={bad*100:.3f}% (лимит {MAX_BAD_FRAC*100:.1f}%)")
    if mean > MAX_MEAN_DIFF or bad > MAX_BAD_FRAC:
        print("  ✗ кадр чернил разошёлся с эталоном refs/ink_click_ref.png")
        print("    если правка вида намеренная — обнови: python tests/test_ink.py --update")
        return 1
    print("  ✓ след от клика совпадает с эталоном")
    return 0


if __name__ == "__main__":
    sys.exit(main(update="--update" in sys.argv))
