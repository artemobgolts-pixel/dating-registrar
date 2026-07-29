"""Скриншот фона (.bg-smoke/.bg-hearts) с public.css на десктоп- и мобиле-ширине.
Проверяем, что после перф-правок (снят SMIL animate, мобильный кап турбулентности)
фон остаётся «дымчатым» и красивым. Не тест — визуальная проверка для глаз.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CSS = (ROOT / "app" / "static" / "public.css").read_text(encoding="utf-8")
_cat = (ROOT / "app" / "templates" / "public" / "category.html").read_text(encoding="utf-8")
# вырезаем блок фона: от <div class="bg-hearts"> до закрытия .bg-smoke
_start = _cat.index('<div class="bg-hearts"')
_end = _cat.index("</div>", _cat.index("<span></span>")) + len("</div>")
BG = _cat[_start:_end]

HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{CSS}</style></head>
<body style="min-height:100vh">{BG}
<div class="wrap"><h1 class="serif" style="font-size:34px;color:#b65f6f">
События для тебя ♥</h1></div></body></html>"""

GL = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
      "--ignore-gpu-blocklist"]

with sync_playwright() as p:
    br = p.chromium.launch(args=GL)
    for name, w, h in [("desktop", 1100, 760), ("mobile", 390, 760)]:
        pg = br.new_page(viewport={"width": w, "height": h},
                         device_scale_factor=2 if name == "mobile" else 1)
        pg.set_content(HTML, wait_until="networkidle")
        pg.wait_for_timeout(400)
        out = HERE / f"refs/perf_bg_{name}.png"
        out.parent.mkdir(exist_ok=True)
        pg.screenshot(path=str(out))
        print(f"{name}: {out}")
        pg.close()
    br.close()
