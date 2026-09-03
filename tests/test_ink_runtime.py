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
HTML = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
html,body,.bg-smoke{margin:0;width:100%;height:100%}
.bg-smoke{position:fixed;inset:0}.ink-canvas,.ink-static-frame{opacity:0}
.bg-smoke.has-ink .ink-canvas,.bg-smoke.has-ink .ink-static-frame{opacity:1}
.bg-smoke.has-ink .ink-static-frame.is-pending{opacity:0!important}
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
                  force_main=True, device_scale_factor=1):
        context = self.browser.new_context(
            viewport={"width": 390 if mobile else 1200,
                      "height": 780 if mobile else 760},
            is_mobile=mobile,
            has_touch=mobile,
            device_scale_factor=device_scale_factor,
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

    def test_coarse_mobile_starts_full_fidelity_then_adapts_when_slow(self):
        context, page = self.make_page(mobile=True, device_scale_factor=3)
        try:
            page.evaluate("""() => {
              const getExtension=WebGL2RenderingContext.prototype.getExtension;
              WebGL2RenderingContext.prototype.getExtension=function(name){
                if(name==='EXT_disjoint_timer_query_webgl2') return null;
                return getExtension.call(this,name);
              };
              const canvas=document.createElement('canvas');
              document.body.appendChild(canvas);
              const queue=[];
              let nextId=0;
              const runtime=window.D4YInkRuntime.create(canvas,{
                now:()=>0,
                requestFrame:callback=>{queue.push(callback);return ++nextId;},
                cancelFrame:()=>{},
              });
              runtime.setState({width:390,height:780,dpr:3,
                interactive:false,fine:false});
              runtime.start();
              window.__qualityProbe={canvas,queue,runtime};
            }""")
            initial = page.evaluate("""() => {
              const probe=window.__qualityProbe;
              probe.queue.shift()(0);
              return {width:probe.canvas.width,height:probe.canvas.height,
                scale:probe.runtime.stats().scale};
            }""")
            self.assertEqual(initial["scale"], 1)
            self.assertEqual(initial["width"] * initial["height"], 1_216_800)

            boundary = page.evaluate("""() => {
              const probe=window.__qualityProbe;
              // Одиннадцать slow-сэмплов ещё не меняют качество.
              for(let i=1;i<=12;i++) probe.queue.shift()(i*50);
              probe.runtime.pause();
              probe.queue.length=0;
              probe.runtime.start();
              probe.queue.shift()(2000);
              probe.queue.shift()(2050);
              return {width:probe.canvas.width,height:probe.canvas.height,
                scale:probe.runtime.stats().scale};
            }""")
            self.assertEqual(boundary["scale"], 1)
            self.assertEqual(
                boundary["width"] * boundary["height"],
                initial["width"] * initial["height"],
            )

            adapted = page.evaluate("""() => {
              const probe=window.__qualityProbe;
              for(let i=1;i<=12;i++) probe.queue.shift()(2050+i*50);
              return {width:probe.canvas.width,height:probe.canvas.height,
                scale:probe.runtime.stats().scale};
            }""")
            self.assertLess(adapted["scale"], 1)
            self.assertLess(
                adapted["width"] * adapted["height"],
                initial["width"] * initial["height"],
            )

            recovered_scale = page.evaluate("""() => {
              const probe=window.__qualityProbe;
              for(let i=1;i<=90;i++) probe.queue.shift()(2650+i*34);
              return probe.runtime.stats().scale;
            }""")
            self.assertGreater(recovered_scale, adapted["scale"])
            page.evaluate("window.__qualityProbe.runtime.destroy()")
        finally:
            context.close()

    def test_paused_canvas_keeps_its_last_frame_during_mobile_resize(self):
        context, page = self.make_page(
            mobile=True, force_main=True, device_scale_factor=3)
        try:
            page.evaluate("window.__INK_PRESERVE = true")
            self.inject_controller(page)
            page.wait_for_selector(".bg-smoke.has-ink .ink-canvas")
            alpha_before = page.locator(".ink-canvas").evaluate("""node => {
              const gl=node.getContext('webgl2');
              const pixel=new Uint8Array(4);
              gl.readPixels(node.width>>1,node.height>>1,1,1,gl.RGBA,
                            gl.UNSIGNED_BYTE,pixel);
              return pixel[3];
            }""")
            self.assertEqual(alpha_before, 255)

            page.evaluate(
                "document.querySelector('.bg-smoke').__d4yInkController.pause()")
            page.set_viewport_size({"width": 390, "height": 700})
            page.wait_for_timeout(80)
            alpha_after = page.locator(".ink-canvas").evaluate("""node => {
              const gl=node.getContext('webgl2');
              const pixel=new Uint8Array(4);
              gl.readPixels(node.width>>1,node.height>>1,1,1,gl.RGBA,
                            gl.UNSIGNED_BYTE,pixel);
              return pixel[3];
            }""")
            self.assertEqual(alpha_after, 255)
            self.assertTrue(page.locator(".bg-smoke").evaluate(
                "node => node.classList.contains('has-ink')"))
        finally:
            context.close()

    def test_background_keeps_rendering_the_new_palette_during_appearance_wave(self):
        context, page = self.make_page(force_main=True)
        try:
            self.inject_controller(page)
            page.wait_for_selector(".bg-smoke.has-ink .ink-canvas")
            draws_before = page.evaluate("window.__glProbe.draws")

            page.evaluate("""() => {
              document.dispatchEvent(new CustomEvent(
                'd4y:appearance-transition-start'));
              document.documentElement.dataset.theme = 'dark';
              document.dispatchEvent(new CustomEvent('d4y:themechange', {
                detail: {theme: 'dark'},
              }));
            }""")
            page.wait_for_function(
                "before => window.__glProbe.draws > before", arg=draws_before)

            self.assertGreater(
                page.evaluate("window.__glProbe.draws"),
                draws_before,
                "Живой фон нельзя останавливать до снимка новой темы: иначе "
                "он меняется целиком только после завершения волны",
            )
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

    def test_portrait_standard_light_uses_the_portrait_poster(self):
        context, page = self.make_page(reduced=True, mobile=True)
        try:
            variants = {
                "staticFriendsLight": self.tiny_poster("#0000ff"),
                "staticFriendsLightPortrait": self.tiny_poster("#00ff00"),
            }
            page.evaluate(
                "document.documentElement.dataset.skin='friends'")
            self.inject_controller(page, poster_variants=variants)
            page.wait_for_selector(".bg-smoke.has-ink .ink-static-frame")
            self.assertEqual(
                page.locator(".ink-static-frame").get_attribute("src"),
                variants["staticFriendsLightPortrait"],
            )
        finally:
            context.close()

    def test_poster_swap_keeps_the_previous_frame_until_decode(self):
        context, page = self.make_page(reduced=True)
        try:
            variants = {
                "staticRomanticLight": self.tiny_poster("#ff0000"),
                "staticRomanticDark": self.tiny_poster("#880000"),
            }
            self.inject_controller(page, poster_variants=variants)
            page.wait_for_selector(".bg-smoke.has-ink .ink-static-frame")
            page.evaluate("""() => {
              const original=HTMLImageElement.prototype.decode;
              HTMLImageElement.prototype.decode=function () {
                if (!this.classList.contains('is-pending')) {
                  return original ? original.call(this) : Promise.resolve();
                }
                return new Promise(resolve => {
                  window.__resolveNextPosterDecode=resolve;
                });
              };
              document.documentElement.dataset.theme='dark';
              document.dispatchEvent(new Event('d4y:themechange'));
            }""")
            page.wait_for_function(
                "typeof window.__resolveNextPosterDecode === 'function'")

            host = page.locator(".bg-smoke")
            self.assertTrue(host.evaluate(
                "node => node.classList.contains('has-ink')"))
            self.assertEqual(page.locator(
                ".ink-static-frame:not(.is-pending)").get_attribute("src"),
                variants["staticRomanticLight"],
            )
            self.assertEqual(page.locator(
                ".ink-static-frame.is-pending").evaluate(
                    "node => getComputedStyle(node).opacity"), "0")

            page.evaluate("window.__resolveNextPosterDecode()")
            page.wait_for_function(
                "expected => document.querySelector("
                "'.ink-static-frame:not(.is-pending)').getAttribute('src') === expected",
                arg=variants["staticRomanticDark"],
            )
            self.assertEqual(page.locator(".ink-static-frame").count(), 1)
            self.assertTrue(host.evaluate(
                "node => node.classList.contains('has-ink')"))
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

    def test_first_pointer_sample_does_not_draw_from_viewport_center(self):
        context, page = self.make_page(
            interactive=True, force_main=False)
        try:
            page.evaluate("""() => {
              window.__inkInputs=[];
              window.Worker=class {
                constructor(){this.terminated=false;}
                postMessage(message){
                  if(message.type==='init')setTimeout(()=>{
                    if(!this.terminated)this.onmessage({data:{
                      type:'first-frame',detail:{mode:'base'},
                    }});
                  },0);
                  if(message.type==='input')window.__inkInputs.push(message.payload);
                }
                terminate(){this.terminated=true;}
              };
            }""")
            self.inject_controller(
                page, runtime_src="fake-runtime.js",
                worker_src="fake-worker.js")
            page.wait_for_selector(".bg-smoke.has-ink")

            page.dispatch_event("body", "pointermove", {
                "clientX": 120, "clientY": 300, "pointerType": "mouse",
            })
            page.wait_for_function("window.__inkInputs.length === 1")
            first = page.evaluate("window.__inkInputs[0].move")
            self.assertAlmostEqual(first["px"], first["x"])
            self.assertAlmostEqual(first["py"], first["y"])

            page.dispatch_event("body", "pointermove", {
                "clientX": 960, "clientY": 300, "pointerType": "mouse",
            })
            page.wait_for_function("window.__inkInputs.length === 2")
            second = page.evaluate("window.__inkInputs[1].move")
            self.assertAlmostEqual(second["px"], first["x"])
            self.assertAlmostEqual(second["py"], first["y"])

            page.evaluate(
                "document.documentElement.dataset.inkInteractive='0'")
            page.wait_for_timeout(40)
            page.evaluate(
                "document.documentElement.dataset.inkInteractive='1'")
            page.wait_for_timeout(40)
            page.dispatch_event("body", "pointermove", {
                "clientX": 360, "clientY": 260, "pointerType": "mouse",
            })
            page.wait_for_function("window.__inkInputs.length === 3")
            after_toggle = page.evaluate("window.__inkInputs[2].move")
            self.assertAlmostEqual(after_toggle["px"], after_toggle["x"])
            self.assertAlmostEqual(after_toggle["py"], after_toggle["y"])
        finally:
            context.close()

    def test_worker_pauses_while_document_is_hidden_and_stops_on_pagehide(self):
        context, page = self.make_page(force_main=False)
        try:
            page.evaluate("""() => {
              window.__inkWorkerMessages = [];
              window.__inkDocumentHidden = false;
              Object.defineProperty(document, 'hidden', {
                configurable: true,
                get: () => window.__inkDocumentHidden,
              });
              window.Worker = class {
                constructor() { this.terminated = false; }
                postMessage(message) {
                  window.__inkWorkerMessages.push(message.type === 'init'
                    ? {type: message.type}
                    : JSON.parse(JSON.stringify(message)));
                  if (message.type === 'init') {
                    setTimeout(() => {
                      if (!this.terminated) this.onmessage({data:{
                        type:'first-frame', detail:{mode:'base'},
                      }});
                    }, 0);
                  }
                }
                terminate() {
                  this.terminated = true;
                  window.__inkWorkerTerminated = true;
                }
              };
            }""")
            self.inject_controller(
                page, runtime_src="fake-runtime.js",
                worker_src="fake-worker.js")
            page.wait_for_selector(".bg-smoke.has-ink .ink-canvas")

            page.evaluate("""() => {
              window.__inkDocumentHidden = true;
              document.dispatchEvent(new Event('visibilitychange'));
            }""")
            page.wait_for_timeout(20)
            messages = page.evaluate("window.__inkWorkerMessages")
            self.assertTrue(any(
                message.get("type") == "pause"
                or (message.get("type") == "visibility"
                    and (message.get("hidden") is True
                         or message.get("visible") is False))
                for message in messages
            ), messages)

            page.evaluate("window.dispatchEvent(new Event('pagehide'))")
            page.wait_for_timeout(20)
            self.assertTrue(page.evaluate(
                "window.__inkWorkerTerminated === true"))
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

    def test_fast_pointer_move_is_rendered_as_a_continuous_segment(self):
        context, page = self.make_page(interactive=True)
        try:
            page.set_viewport_size({"width": 720, "height": 200})
            self.inject_controller(page)
            page.wait_for_selector(".bg-smoke.has-ink")
            page.dispatch_event("body", "pointermove", {
                "clientX": 72, "clientY": 100, "pointerType": "mouse",
            })
            page.wait_for_function("window.__inkStats().pointerFlushes >= 1")
            page.wait_for_timeout(80)
            before = page.evaluate("window.__inkStats().pathSegments || 0")

            page.dispatch_event("body", "pointermove", {
                "clientX": 648, "clientY": 100, "pointerType": "mouse",
            })
            page.wait_for_function("window.__inkStats().pointerFlushes >= 2")
            page.wait_for_timeout(80)
            added = page.evaluate(
                "(window.__inkStats().pathSegments || 0) - %d" % before)
            self.assertGreaterEqual(added, 1, added)
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
            "ink-static-friends-light-portrait.webp",
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
            self.assertIn("ink-static-friends-light-portrait.webp", source,
                          path.name)
            self.assertIn("ink-static-romantic-dark.webp", source, path.name)
        main = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertEqual(main.count("worker-src 'self'"), 3)

        from PIL import Image
        with Image.open(static / "ink-static-friends-light.webp") as image:
            self.assertGreaterEqual(image.width, 2560)
            self.assertGreaterEqual(image.height, 1600)
        with Image.open(
                static / "ink-static-friends-light-portrait.webp") as image:
            self.assertGreaterEqual(image.width, 1400)
            self.assertGreaterEqual(image.height, 2500)

    def test_profile_assets_do_not_force_a_full_turbo_reload(self):
        profile = (ROOT / "app/templates/admin/profile.html").read_text(
            encoding="utf-8")
        base = (ROOT / "app/templates/admin/base.html").read_text(
            encoding="utf-8")
        head = profile.split("{% block head %}", 1)[1].split(
            "{% endblock %}", 1)[0]
        self.assertNotIn('data-turbo-track="reload"', head)
        self.assertIn('data-turbo-track="dynamic"', head)
        self.assertNotIn("asset('profile.js')", head)
        self.assertEqual(base.count("asset('profile.js')"), 1)
        self.assertIn(
            '<script src="{{ asset(\'profile.js\') }}" '
            'data-turbo-track="reload" defer></script>',
            base,
        )

    def test_standard_light_poster_keeps_texture_without_excess_noise(self):
        from PIL import Image, ImageChops, ImageFilter, ImageStat

        path = ROOT / "app/static/ink-static-friends-light.webp"
        with Image.open(path) as source:
            luminance = source.convert("L").resize(
                (512, 512), Image.Resampling.LANCZOS)
        residual = ImageChops.difference(
            luminance, luminance.filter(ImageFilter.GaussianBlur(1.5)))
        histogram = residual.histogram()
        contrast = max(ImageStat.Stat(luminance).stddev[0], 1e-6)
        normalized_noise = ImageStat.Stat(residual).rms[0] / contrast
        visible_noise = sum(histogram[3:]) / sum(histogram)

        self.assertLessEqual(normalized_noise, 0.11)
        self.assertLessEqual(visible_noise, 0.075)


if __name__ == "__main__":
    sys.exit(unittest.main())
