"""Скриншот-харнесс для фонового эффекта «чернила» (app/static/ink.js).

Эффект живёт на WebGL2 (float-render). Чтобы снимать его детерминированно,
запускаем headless Chromium со swiftshader и подменяем часы:

  performance.now()        → читает управляемое нами время __T (мс)
  requestAnimationFrame    → складывает колбэк в очередь
  __inkAdvance(dtMs)       → двигает __T и проливает очередь (= один кадр)

В ink.js нет Math.random/Date.now, а clickSeed растёт детерминированно,
поэтому при фиксированной последовательности кадров картинка воспроизводима.

Использование:
    from ink_shot import capture
    capture(out="refs/ink_click.png",
            clicks=[(0, 0.5, 0.5)],   # (кадр, x-доля, y-доля)
            frames=90, dt_ms=16, w=900, h=600)
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
INK_JS = HERE.parent / "app" / "static" / "ink.js"
INK_RUNTIME_JS = HERE.parent / "app" / "static" / "ink-runtime.js"

# Флаги, чтобы headless Chromium рендерил WebGL2 без железного GPU.
GL_FLAGS = ["--use-gl=angle", "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]

# Управляемые часы + очередь rAF. Ставится init-скриптом ДО загрузки ink.js,
# поэтому IIFE захватывает уже наши performance.now/requestAnimationFrame.
CLOCK_JS = r"""
(function () {
  var t = 0, cbs = [], nextId = 1;
  performance.now = function () { return t; };
  window.requestAnimationFrame = function (cb) {
    var id = nextId++; cbs.push({ id: id, cb: cb }); return id;
  };
  window.cancelAnimationFrame = function (id) {
    cbs = cbs.filter(function (c) { return c.id !== id; });
  };
  Object.defineProperty(document, "hidden", { get: function () { return false; } });
  window.__inkAdvance = function (dtMs) {
    t += dtMs;
    var due = cbs; cbs = [];
    for (var i = 0; i < due.length; i++) due[i].cb(t);
  };
})();
"""

# Минимальная страница: только хост .bg-smoke на весь вьюпорт.
PAGE_HTML = """<!doctype html><html><head><meta charset=utf-8>
<style>
  html,body{margin:0;height:100%;background:#faf5f2}
  .bg-smoke{position:fixed;inset:0;width:100vw;height:100vh}
  .ink-canvas{position:fixed;inset:0;width:100vw;height:100vh;display:block}
</style></head><body><div class="bg-smoke"></div></body></html>"""


def capture(out, clicks=(), frames=90, dt_ms=16, w=900, h=600, debug=False):
    """Снять PNG эффекта.

    clicks  — список (номер_кадра, x_доля, y_доля); x/y в долях вьюпорта 0..1.
    frames  — сколько кадров проиграть.
    dt_ms   — шаг времени на кадр (16 мс ≈ 60 fps; ink.js клампит dt до 22 мс).
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ink_src = INK_JS.read_text(encoding="utf-8")
    runtime_src = INK_RUNTIME_JS.read_text(encoding="utf-8")
    click_map = {}
    for fr, x, y in clicks:
        click_map.setdefault(int(fr), []).append((x, y))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=GL_FLAGS)
        page = browser.new_page(viewport={"width": w, "height": h},
                                device_scale_factor=1,
                                reduced_motion="no-preference")
        if debug:
            page.on("console", lambda m: print("  [console]", m.text))
        page.set_content(PAGE_HTML)
        if debug:
            page.evaluate("window.__INK_DEBUG = true;")
        # Часы ставим ДО вставки ink.js: его IIFE захватит уже наши
        # performance.now/requestAnimationFrame. __INK_PRESERVE — чтобы кадр
        # сохранялся в буфере и его можно было прочитать через toDataURL.
        page.evaluate("window.__INK_PRESERVE = true;")
        page.evaluate("window.__INK_FORCE_MAIN = true;")
        page.evaluate("document.documentElement.dataset.inkInteractive = '1';")
        page.evaluate(CLOCK_JS)
        page.add_script_tag(content=runtime_src)
        page.add_script_tag(content=ink_src)
        # ink.js при загрузке уже зарегистрировал первый кадр через наш rAF.
        for f in range(frames):
            for (x, y) in click_map.get(f, []):
                page.evaluate(
                    "([x,y]) => window.dispatchEvent(new MouseEvent('mousedown',"
                    "{clientX: x*window.innerWidth, clientY: y*window.innerHeight}))",
                    [x, y])
            page.evaluate("(dt) => window.__inkAdvance(dt)", dt_ms)
        has_canvas = page.evaluate(
            "() => !!document.querySelector('.bg-smoke.has-ink .ink-canvas')")
        if not has_canvas:
            browser.close()
            raise RuntimeError("ink.js не запустился (нет .ink-canvas) — "
                               "проверь WebGL2/float-render в браузере")
        # Скриншот берём через canvas.toDataURL, а НЕ page.screenshot: мы подменили
        # requestAnimationFrame своими часами, из-за чего ломается rAF-проверка
        # «стабильности элемента» внутри Playwright и .screenshot() висит вечно.
        _save_canvas_png(page, out)
        browser.close()
    return out


def _save_canvas_png(page, out):
    """Сохраняет пиксели .ink-canvas в PNG через toDataURL (минуя page.screenshot)."""
    import base64
    data_url = page.evaluate(
        "() => document.querySelector('.ink-canvas').toDataURL('image/png')")
    Path(out).write_bytes(base64.b64decode(data_url.split(",", 1)[1]))


def capture_clicks(out, clicks, bg_time=8.0, w=900, h=600,
                   skin="romantic", theme="light"):
    """Снять PNG детерминированно через тест-хук __inkRenderClicks.

    В отличие от capture() здесь НЕ крутится жидкость и нет случайного dye-следа:
    ink.js под window.__INK_TEST рисует ровно один кадр из явно заданных клякс,
    поэтому картинка воспроизводима пиксель-в-пиксель — годится в эталон.

    clicks — список словарей {x, y, age, seed}: x/y в долях вьюпорта 0..1
             (как в проде: y снизу вверх), age в секундах (0..6.5), seed — форма.
    bg_time — фиксированное «время» фоновой анимации fbm.
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ink_src = INK_JS.read_text(encoding="utf-8")
    runtime_src = INK_RUNTIME_JS.read_text(encoding="utf-8")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=GL_FLAGS)
        page = browser.new_page(viewport={"width": w, "height": h},
                                device_scale_factor=1,
                                reduced_motion="no-preference")
        page.set_content(PAGE_HTML)
        page.evaluate("window.__INK_TEST = true;")
        page.evaluate("window.__INK_FORCE_MAIN = true;")
        page.evaluate("window.__INK_PRESERVE = true;")
        page.evaluate(
            "([skin, theme]) => { document.documentElement.dataset.skin = skin; "
            "document.documentElement.dataset.theme = theme; }",
            [skin, theme],
        )
        page.add_script_tag(content=runtime_src)
        page.add_script_tag(content=ink_src)
        if not page.evaluate("() => window.__inkReady === true"):
            browser.close()
            raise RuntimeError("ink.js не вышел в тест-режим (__inkReady) — "
                               "проверь WebGL2/float-render в браузере")
        page.evaluate(
            "([cl, bt]) => window.__inkRenderClicks(cl, bt)", [list(clicks), bg_time])
        page.locator(".ink-canvas").screenshot(path=str(out))
        browser.close()
    return out


def capture_background(out, skin="romantic", theme="light", bg_time=8.0,
                       w=1440, h=900):
    """Снять чистый текстурный фон при фиксированном времени (main-thread)."""
    return capture_clicks(
        out=out, clicks=(), bg_time=bg_time, w=w, h=h,
        skin=skin, theme=theme,
    )


if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "refs/ink_current.png"
    path = capture(out=HERE / name, clicks=[(2, 0.5, 0.5)], frames=80, debug=True)
    print("saved", path)
