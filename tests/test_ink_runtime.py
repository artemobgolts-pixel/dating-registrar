#!/usr/bin/env python3
"""Поведенческие регрессии ступенчатой инициализации ink.js."""

import base64
import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
INK = (ROOT / "app/static/ink.js").read_text(encoding="utf-8")
RUNTIME = (ROOT / "app/static/ink-runtime.js").read_text(encoding="utf-8")
POSTER = ROOT / "app/static/ink-static-romantic-light.webp"
GL_FLAGS = [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
]
HTML = """<!doctype html><html><head><style>
html,body,.bg-smoke{margin:0;width:100%;height:100%}
.bg-smoke{position:fixed;inset:0}.ink-canvas,.ink-static-frame{opacity:0}
.bg-smoke.has-ink .ink-canvas,.bg-smoke.has-ink .ink-static-frame{opacity:1}
.bg-smoke.has-ink .fallback{display:none}
</style></head><body><div class="bg-smoke"><span class="fallback"></span></div></body></html>"""

PROBE = r"""
(() => {
  const s = window.__glProbe = {contexts:0, vertex:0, fragment:0, programs:0,
    framebuffers:0, textures:0, draws:0, raf:0, cancels:0};
  const gc = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (...a) {
    if (a[0] === 'webgl2') s.contexts++;
    return gc.apply(this, a);
  };
  const wrap = (name, count) => {
    const p = WebGL2RenderingContext.prototype, original = p[name];
    p[name] = function (...a) { count(a); return original.apply(this, a); };
  };
  wrap('createShader', a => { if(a[0]===35633)s.vertex++;if(a[0]===35632)s.fragment++; });
  wrap('createProgram', () => s.programs++);
  wrap('createFramebuffer', () => s.framebuffers++);
  wrap('createTexture', () => s.textures++);
  wrap('drawArrays', () => s.draws++);
  const raf = window.requestAnimationFrame, cancel = window.cancelAnimationFrame;
  window.requestAnimationFrame = function (...a) { s.raf++; return raf.apply(this,a); };
  window.cancelAnimationFrame = function (...a) { s.cancels++; return cancel.apply(this,a); };
})();
"""


class InkRuntimeBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise unittest.SkipTest(f"playwright недоступен: {error}")
        cls._playwright = sync_playwright().start()
        try:
            cls.browser = cls._playwright.chromium.launch(
                headless=True, args=GL_FLAGS)
        except Exception as error:
            cls._playwright.stop()
            raise unittest.SkipTest(f"Chromium недоступен: {error}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._playwright.stop()

    def make_page(self, *, reduced=False, mobile=False, interactive=False,
                  force_main=True):
        context = self.browser.new_context(
            viewport={"width": 390 if mobile else 1200,
                      "height": 780 if mobile else 760},
            is_mobile=mobile,
            has_touch=mobile,
            reduced_motion="reduce" if reduced else "no-preference",
        )
        page = context.new_page()
        page.set_content(HTML)
        page.evaluate(PROBE)
        page.evaluate(
            "([forceMain]) => {window.__INK_FORCE_MAIN=forceMain;"
            "window.requestIdleCallback=()=>0}",
            [force_main],
        )
        if interactive:
            page.evaluate("document.documentElement.dataset.inkInteractive='1'")
        page.add_script_tag(content=RUNTIME)
        return context, page

    @staticmethod
    def inject_controller(page, poster_data="", runtime_src="", worker_src="",
                          poster_variants=None):
        page.evaluate(
            """([source,poster,runtimeSrc,workerSrc,variants]) => {
              const tag=document.createElement('script');
              if(runtimeSrc) tag.dataset.runtimeSrc=runtimeSrc;
              if(workerSrc) tag.dataset.workerSrc=workerSrc;
              if(poster){
                tag.dataset.staticRomanticLight=poster;
                tag.dataset.staticRomanticDark=poster;
                tag.dataset.staticFriendsLight=poster;
                tag.dataset.staticFriendsDark=poster;
              }
              for (const [key,value] of Object.entries(variants || {})) {
                tag.dataset[key]=value;
              }
              tag.textContent=source;document.head.appendChild(tag);
            }""",
            [INK, poster_data, runtime_src, worker_src, poster_variants or {}],
        )

    @staticmethod
    def tiny_poster(color):
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2">'
               f'<path fill="{color}" d="M0 0h2v2H0z"/></svg>')
        return "data:image/svg+xml;base64," + base64.b64encode(
            svg.encode("utf-8")).decode("ascii")

    def test_anonymous_and_mobile_compile_only_base_pipeline(self):
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                context, page = self.make_page(mobile=mobile)
                try:
                    self.inject_controller(page)
                    page.wait_for_selector(".bg-smoke.has-ink")
                    probe = page.evaluate("({...window.__glProbe})")
                    self.assertEqual(probe["vertex"], 1)
                    self.assertEqual(probe["fragment"], 1)
                    self.assertEqual(probe["programs"], 1)
                    self.assertEqual(probe["framebuffers"], 0)
                    self.assertEqual(probe["textures"], 0)
                    self.assertEqual(page.evaluate("window.__inkStats().mode"), "base")
                finally:
                    context.close()

    def test_reduced_motion_uses_decoded_poster_without_webgl(self):
        context, page = self.make_page(reduced=True)
        try:
            poster = "data:image/webp;base64," + base64.b64encode(
                POSTER.read_bytes()).decode("ascii")
            self.inject_controller(page, poster)
            page.wait_for_function(
                "document.querySelector('.ink-static-frame')?.naturalWidth > 0")
            self.assertEqual(page.evaluate("window.__glProbe.contexts"), 0)
            self.assertEqual(page.evaluate("window.__glProbe.raf"), 0)
            self.assertFalse(page.locator(".ink-canvas").count())
            self.assertTrue(page.locator(".bg-smoke").evaluate(
                "node => node.classList.contains('has-ink')"))

            page.emulate_media(reduced_motion="no-preference")
            page.wait_for_selector(".bg-smoke.has-ink .ink-canvas")
            self.assertEqual(page.evaluate("window.__inkStats().backend"),
                             "main")
            self.assertEqual(page.evaluate("window.__glProbe.contexts"), 1)
        finally:
            context.close()

    def test_enabling_reduced_motion_replaces_live_canvas_with_poster(self):
        context, page = self.make_page()
        try:
            poster = "data:image/webp;base64," + base64.b64encode(
                POSTER.read_bytes()).decode("ascii")
            self.inject_controller(page, poster)
            page.wait_for_selector(".bg-smoke.has-ink .ink-canvas")

            page.emulate_media(reduced_motion="reduce")
            page.wait_for_function(
                "document.querySelector('.ink-static-frame')?.naturalWidth > 0")
            page.wait_for_selector(".bg-smoke.has-ink .ink-static-frame")

            self.assertFalse(page.locator(".ink-canvas").count())
            self.assertEqual(page.evaluate("window.__inkStats().backend"),
                             "poster")
            self.assertTrue(page.evaluate(
                "document.documentElement.classList.contains('ink-static')"))

            page.emulate_media(reduced_motion="no-preference")
            page.wait_for_selector(".bg-smoke.has-ink .ink-canvas")
            self.assertEqual(page.evaluate("window.__inkStats().backend"),
                             "main")
        finally:
            context.close()

    def test_static_poster_follows_both_skin_and_theme_axes(self):
        context, page = self.make_page(reduced=True)
        try:
            variants = {
                "staticRomanticLight": self.tiny_poster("#ff0000"),
                "staticRomanticDark": self.tiny_poster("#880000"),
                "staticFriendsLight": self.tiny_poster("#0000ff"),
                "staticFriendsDark": self.tiny_poster("#000088"),
            }
            self.inject_controller(page, poster_variants=variants)
            page.wait_for_function(
                "document.querySelector('.ink-static-frame')?.naturalWidth > 0")
            image = page.locator(".ink-static-frame")
            self.assertEqual(image.get_attribute("src"),
                             variants["staticRomanticLight"])

            page.evaluate("""() => {
              document.documentElement.dataset.skin='friends';
              document.dispatchEvent(new Event('d4y:skinchange'));
            }""")
            page.wait_for_function(
                "expected => document.querySelector('.ink-static-frame').src === expected",
                arg=variants["staticFriendsLight"])
            page.evaluate("""() => {
              document.documentElement.dataset.theme='dark';
              document.dispatchEvent(new Event('d4y:themechange'));
            }""")
            page.wait_for_function(
                "expected => document.querySelector('.ink-static-frame').src === expected",
                arg=variants["staticFriendsDark"])

            page.evaluate("""() => {
              document.documentElement.dataset.skin='romantic';
              document.dispatchEvent(new Event('d4y:skinchange'));
            }""")
            page.wait_for_function(
                "expected => document.querySelector('.ink-static-frame').src === expected",
                arg=variants["staticRomanticDark"])
        finally:
            context.close()

    def test_stale_poster_decode_cannot_reveal_or_hide_live_canvas(self):
        context, page = self.make_page(reduced=True)
        try:
            page.evaluate("""() => {
              HTMLImageElement.prototype.decode = function () {
                return new Promise(resolve => {
                  window.__resolvePosterDecode = resolve;
                });
              };
            }""")
            poster = "data:image/webp;base64," + base64.b64encode(
                POSTER.read_bytes()).decode("ascii")
            self.inject_controller(page, poster)
            page.wait_for_function(
                "typeof window.__resolvePosterDecode === 'function'")
            page.evaluate("""() => {
              window.__stalePosterError =
                document.querySelector('.ink-static-frame').onerror;
              window.__queuedInkRafs = [];
              let nextId = 1;
              window.requestAnimationFrame = callback => {
                window.__queuedInkRafs.push(callback);
                return nextId++;
              };
              window.cancelAnimationFrame = () => {};
            }""")

            page.emulate_media(reduced_motion="no-preference")
            page.wait_for_function(
                "window.__inkStats().backend === 'main' && "
                "document.querySelector('.ink-canvas')")
            page.evaluate("window.__resolvePosterDecode()")
            self.assertFalse(page.locator(".bg-smoke").evaluate(
                "node => node.classList.contains('has-ink')"))

            for _ in range(3):
                page.evaluate("""() => {
                  const callbacks = window.__queuedInkRafs.splice(0);
                  const now = performance.now() + 40;
                  callbacks.forEach(callback => callback(now));
                }""")
                if page.locator(".bg-smoke").evaluate(
                        "node => node.classList.contains('has-ink')"):
                    break
            self.assertTrue(page.locator(".bg-smoke").evaluate(
                "node => node.classList.contains('has-ink')"))

            page.evaluate("window.__stalePosterError()")
            self.assertTrue(page.locator(".bg-smoke").evaluate(
                "node => node.classList.contains('has-ink')"))
        finally:
            context.close()

    def test_worker_first_frame_then_failure_waits_for_fallback_frame(self):
        context, page = self.make_page(force_main=False)
        try:
            page.evaluate("""() => {
              window.__drawsAtReveal = null;
              new MutationObserver(() => {
                if (document.querySelector('.bg-smoke.has-ink') &&
                    window.__drawsAtReveal === null) {
                  window.__drawsAtReveal = window.__glProbe.draws;
                }
              }).observe(document.querySelector('.bg-smoke'), {attributes:true});
              window.Worker = class {
                constructor() { this.terminated = false; }
                postMessage(message) {
                  if (message.type !== 'init') return;
                  setTimeout(() => {
                    if (this.terminated) return;
                    this.onmessage({data:{type:'first-frame',
                      detail:{mode:'base'}}});
                    this.onerror(new Event('error'));
                  }, 0);
                }
                terminate() { this.terminated = true; }
              };
            }""")
            self.inject_controller(
                page, runtime_src="fake-runtime.js",
                worker_src="fake-worker.js")
            page.wait_for_selector(".bg-smoke.has-ink .ink-canvas")

            self.assertEqual(page.evaluate("window.__inkStats().backend"),
                             "main")
            self.assertFalse(page.evaluate("window.__inkStats().worker"))
            self.assertEqual(page.locator(".ink-canvas").count(), 1)
            self.assertGreater(page.evaluate("window.__drawsAtReveal"), 0)
        finally:
            context.close()

    def test_first_frame_reveals_and_pointer_burst_is_coalesced(self):
        context, page = self.make_page(interactive=True)
        try:
            page.evaluate("""() => {
              window.__drawsAtReveal = null;
              new MutationObserver(() => {
                if (document.querySelector('.bg-smoke.has-ink') &&
                    window.__drawsAtReveal === null) {
                  window.__drawsAtReveal = window.__glProbe.draws;
                }
              }).observe(document.querySelector('.bg-smoke'), {attributes:true});
            }""")
            self.inject_controller(page)
            page.wait_for_selector(".bg-smoke.has-ink")
            self.assertGreater(page.evaluate("window.__drawsAtReveal"), 0)
            before = page.evaluate("({...window.__glProbe})")
            page.evaluate("""() => {
              for(let i=0;i<200;i++) window.dispatchEvent(new PointerEvent(
                'pointermove',{clientX:20+i,clientY:180,pointerType:'mouse'}));
            }""")
            page.wait_for_function("window.__inkStats().pointerFlushes === 1")
            after = page.evaluate("({...window.__glProbe})")
            stats = page.evaluate("window.__inkStats()")
            self.assertEqual(after["cancels"] - before["cancels"], 0)
            self.assertEqual(stats["pointerFlushes"], 1)
            self.assertEqual(after["vertex"], 1)
            self.assertGreater(after["fragment"], 1)
            self.assertEqual(after["framebuffers"], 10)
            # Повторный burst не создаёт новый pipeline.
            page.evaluate("window.dispatchEvent(new PointerEvent('pointermove',"
                          "{clientX:400,clientY:200,pointerType:'mouse'}))")
            page.wait_for_function("window.__inkStats().pointerFlushes === 2")
            self.assertEqual(page.evaluate("window.__glProbe.framebuffers"), 10)
        finally:
            context.close()


class InkRuntimeSourceContracts(unittest.TestCase):
    def test_motion_speed_and_optional_timer_contract(self):
        self.assertIn("float tt=t*0.016", RUNTIME)
        self.assertIn('getExtension("EXT_disjoint_timer_query_webgl2")', RUNTIME)
        self.assertIn('diagnostics.qualitySource="cadence"', RUNTIME)
        self.assertIn("function ensureInteractive()", RUNTIME)
        self.assertLess(RUNTIME.index("BASE_DISPLAY_FS"),
                        RUNTIME.index("function ensureInteractive()"))

    def test_worker_posters_templates_and_csp_are_versioned(self):
        static = ROOT / "app/static"
        for name in (
            "ink-worker.js", "ink-static-friends-light.webp",
            "ink-static-friends-dark.webp", "ink-static-romantic-light.webp",
            "ink-static-romantic-dark.webp",
        ):
            self.assertGreater((static / name).stat().st_size, 100, name)
        templates = list((ROOT / "app/templates").rglob("*.html"))
        ink_templates = [path for path in templates
                         if "asset('ink.js')" in path.read_text(encoding="utf-8")]
        self.assertTrue(ink_templates)
        for path in ink_templates:
            source = path.read_text(encoding="utf-8")
            self.assertIn("asset('ink-runtime.js')", source, path.name)
            self.assertIn("asset('ink-worker.js')", source, path.name)
            self.assertIn("ink-static-romantic-dark.webp", source, path.name)
        main = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertEqual(main.count("worker-src 'self'"), 3)


if __name__ == "__main__":
    sys.exit(unittest.main())
