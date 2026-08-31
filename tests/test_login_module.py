from html import unescape
from pathlib import Path
import re
from types import SimpleNamespace
import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape


APP = Path(__file__).resolve().parents[1] / "app"


def source(relative: str) -> str:
    return (APP / relative).read_text("utf-8")


class SharedLoginModuleTests(unittest.TestCase):
    def make_env(self, vpn_url="https://vpn.example/project"):
        env = Environment(
            loader=FileSystemLoader(APP / "templates"),
            autoescape=select_autoescape(("html",)),
        )
        env.globals.update(
            asset=lambda name: f"/static/{name}",
            VPN_URL=vpn_url,
        )
        return env

    def test_all_four_surfaces_use_the_shared_module_and_stylesheet(self):
        surfaces = {
            "templates/auth/login.html": "login_module('page'",
            "templates/public/category.html": "login_module('dialog'",
            "templates/public/share.html": "login_module('dialog'",
            "templates/public/profile_review.html": "login_module('dialog'",
        }

        for relative, invocation in surfaces.items():
            with self.subTest(surface=relative):
                page = source(relative)
                self.assertIn(
                    '{% from "auth/_login_module.html" import login_module with context %}',
                    page,
                )
                self.assertIn(invocation, page)
                self.assertIn("{{ asset('auth.css') }}", page)
                self.assertNotIn('{% include "auth/_login_methods.html" %}', page)

        module = source("templates/auth/_login_module.html")
        self.assertEqual(module.count('{% include "auth/_login_methods.html" %}'), 1)
        self.assertIn('class="login-card auth-module auth-module--page"', module)
        self.assertIn('class="login-dlg auth-module auth-module--dialog"', module)

        standalone = source("templates/auth/login.html")
        self.assertIn("{{ asset('auth-page.css') }}", standalone)
        self.assertNotIn("{{ asset('admin.css') }}", standalone)
        for relative in surfaces:
            if relative == "templates/auth/login.html":
                continue
            with self.subTest(dialog_surface=relative):
                self.assertNotIn("auth-page.css", source(relative))

    def test_page_keeps_exact_clickable_vpn_copy(self):
        env = self.make_env()
        request = SimpleNamespace(state=SimpleNamespace(user=None))
        rendered = env.get_template("auth/login.html").render(
            request=request,
            bot="date4you_bot",
            oauth=[],
            widget_state="state",
            next_url="/admin/",
            csp_nonce="nonce",
        )
        visible_text = unescape(re.sub(r"<[^>]+>", "", rendered))
        visible_text = " ".join(visible_text.split())

        self.assertIn("Чтобы войти через Telegram, включите VPN.", visible_text)
        self.assertRegex(
            rendered,
            r'<a href="https://vpn\.example/project" target="_blank" '
            r'rel="noopener sponsored">VPN</a>',
        )

    def test_vpn_promotion_is_not_rendered_in_dialog_mode(self):
        env = self.make_env()
        template = env.from_string(
            """{% from "auth/_login_module.html" import login_module with context %}
            {{ login_module('dialog', 'Войти', 'Описание', '/return') }}"""
        )
        rendered = template.render(
            bot="date4you_bot",
            oauth=[],
            widget_state="state",
        )

        self.assertNotIn("Чтобы войти через Telegram", rendered)
        self.assertNotIn("vpn.example", rendered)
        self.assertIn('data-login-methods', rendered)
        self.assertIn('return_to=/return', rendered)

    def test_methods_keep_oauth_disabled_states_legal_copy_and_next(self):
        methods = source("templates/auth/_login_methods.html")

        self.assertIn("p['enabled']", methods)
        self.assertIn('aria-disabled="true"', methods)
        self.assertIn("next_url | urlencode", methods)
        self.assertIn("Пользовательское соглашение", methods)
        self.assertIn("Политику конфиденциальности", methods)

    def test_module_uses_one_skin_invariant_geometry_source(self):
        css = source("static/auth.css")

        self.assertIn(".auth-module--page.login-card", css)
        self.assertIn(".auth-module--dialog.login-dlg", css)
        self.assertNotRegex(
            css,
            r'data-skin[^\n]*\}\s*(?:width|height|padding|margin|font-size)',
        )
        self.assertIn('data-skin="friends"] .auth-module { --auth-accent:', css)

    def test_standalone_shell_is_small_and_keeps_accessibility_preferences(self):
        css = source("static/auth-page.css")

        self.assertLess(len(css.encode("utf-8")), 32_000)
        self.assertIn(".login-page", css)
        self.assertIn(".bg-smoke", css)
        self.assertIn(".ink-canvas", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("@media (prefers-reduced-transparency: reduce)", css)
        self.assertIn("@media (prefers-contrast: more)", css)


class LoginGeometryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - зависит от dev-окружения
            raise unittest.SkipTest(f"playwright недоступен: {exc!r}") from exc
        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - браузер может отсутствовать
            if getattr(cls, "playwright", None):
                cls.playwright.stop()
            raise unittest.SkipTest(f"playwright chromium недоступен: {exc!r}") from exc

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()

    def make_page(self):
        env = Environment(
            loader=FileSystemLoader(APP / "templates"),
            autoescape=select_autoescape(("html",)),
        )
        env.globals.update(
            asset=lambda name: f"/static/{name}",
            VPN_URL="https://vpn.example/project",
        )
        request = SimpleNamespace(state=SimpleNamespace(user=None))
        rendered = env.get_template("auth/login.html").render(
            request=request,
            bot="date4you_bot",
            oauth=[],
            widget_state="state",
            next_url="/admin/",
            csp_nonce="nonce",
        )
        page = self.browser.new_page(viewport={"width": 390, "height": 844})
        self.addCleanup(page.close)
        page.set_content(rendered)
        page.add_style_tag(content=(APP / "static/auth-page.css").read_text("utf-8"))
        page.add_style_tag(content=(APP / "static/auth.css").read_text("utf-8"))
        page.add_style_tag(content="*,*::before,*::after{animation:none!important;transition:none!important}")
        return page

    @staticmethod
    def geometry_snapshot(page):
        selectors = (
            ".auth-module--page",
            ".auth-module__brand",
            ".auth-module__description",
            ".auth-module__logo",
        )
        return page.evaluate("""selectors => Object.fromEntries(selectors.map(selector => {
          const node = document.querySelector(selector);
          const rect = node.getBoundingClientRect();
          const style = getComputedStyle(node);
          return [selector, {
            rect: [rect.x, rect.y, rect.width, rect.height],
            type: [style.fontFamily, style.fontSize, style.fontWeight,
                   style.lineHeight, style.letterSpacing],
          }];
        }))""", selectors)

    def test_page_shell_typography_and_logo_slot_do_not_move_between_skins(self):
        page = self.make_page()
        snapshots = {}
        for skin in ("friends", "romantic"):
            page.locator("html").evaluate("(node, value) => node.dataset.skin = value", skin)
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
            snapshots[skin] = self.geometry_snapshot(page)

        for selector in snapshots["friends"]:
            with self.subTest(selector=selector):
                standard = snapshots["friends"][selector]
                romantic = snapshots["romantic"][selector]
                for old, new in zip(standard["rect"], romantic["rect"]):
                    self.assertAlmostEqual(old, new, delta=.25)
                self.assertEqual(standard["type"], romantic["type"])

    def test_friends_brand_keeps_its_theme_aware_gradient_after_css_split(self):
        page = self.make_page()
        expected = {
            "light": {
                "color": "rgb(52, 65, 127)",
                "stops": ("rgb(52, 65, 127)", "rgb(75, 89, 166)",
                          "rgb(35, 142, 137)"),
            },
            "dark": {
                "color": "rgb(189, 198, 255)",
                "stops": ("rgb(189, 198, 255)", "rgb(146, 160, 255)",
                          "rgb(89, 201, 193)"),
            },
        }

        for theme, wanted in expected.items():
            with self.subTest(theme=theme):
                page.locator("html").evaluate(
                    "(node, value) => { node.dataset.skin = 'friends'; "
                    "node.dataset.theme = value; }",
                    theme,
                )
                styles = page.evaluate("""() => {
                  const brand = getComputedStyle(document.querySelector('.auth-module__brand'));
                  const digit = getComputedStyle(document.querySelector('.auth-module__brand span'));
                  return {
                    color: brand.color,
                    background: brand.backgroundImage,
                    clip: brand.backgroundClip,
                    fill: brand.webkitTextFillColor,
                    digitColor: digit.color,
                    digitFill: digit.webkitTextFillColor,
                  };
                }""")

                self.assertEqual(styles["color"], wanted["color"])
                self.assertEqual(styles["clip"], "text")
                self.assertEqual(styles["fill"], "rgba(0, 0, 0, 0)")
                for stop in wanted["stops"]:
                    self.assertIn(stop, styles["background"])
                self.assertEqual(styles["digitColor"], wanted["color"])
                self.assertEqual(styles["digitFill"], wanted["color"])

    def test_logo_artwork_is_optically_normalized_inside_the_fixed_slot(self):
        try:
            from PIL import Image
        except Exception as exc:  # pragma: no cover - production dependency
            raise unittest.SkipTest(f"Pillow недоступен: {exc!r}") from exc

        alpha_bounds = {}
        for skin, filename in (
            ("friends", "logo-standard.png"),
            ("romantic", "logo-romantic.png"),
        ):
            image = Image.open(APP / "static" / filename).convert("RGBA")
            alpha_bounds[skin] = (image.size, image.getchannel("A").getbbox())

        page = self.make_page()
        optical = {}
        for skin, selector in (
            ("friends", ".auth-module__logo-standard"),
            ("romantic", ".auth-module__logo-romantic"),
        ):
            page.locator("html").evaluate("(node, value) => node.dataset.skin = value", skin)
            rects = page.evaluate("""selector => {
              const slot = document.querySelector('.auth-module__logo').getBoundingClientRect();
              const art = document.querySelector(selector).getBoundingClientRect();
              return {slot: [slot.x, slot.y, slot.width, slot.height],
                      art: [art.x, art.y, art.width, art.height]};
            }""", selector)
            (natural_width, natural_height), (left, top, right, bottom) = alpha_bounds[skin]
            art_x, art_y, art_width, art_height = rects["art"]
            optical[skin] = {
                "width": art_width * (right - left) / natural_width,
                "height": art_height * (bottom - top) / natural_height,
                "center_x": art_x + art_width * (left + right) / (2 * natural_width),
                "center_y": art_y + art_height * (top + bottom) / (2 * natural_height),
                "slot_center_x": rects["slot"][0] + rects["slot"][2] / 2,
                "slot_center_y": rects["slot"][1] + rects["slot"][3] / 2,
            }

        self.assertAlmostEqual(optical["friends"]["width"], optical["romantic"]["width"], delta=1.5)
        self.assertAlmostEqual(optical["friends"]["height"], optical["romantic"]["height"], delta=3)
        for values in optical.values():
            self.assertAlmostEqual(values["center_x"], values["slot_center_x"], delta=1)
            self.assertAlmostEqual(values["center_y"], values["slot_center_y"], delta=1)

    def test_dialog_stays_scrollable_in_short_landscape_viewports(self):
        page = self.browser.new_page(viewport={"width": 640, "height": 320})
        self.addCleanup(page.close)
        page.set_content("""
          <html data-skin="friends"><body>
            <dialog open class="login-dlg auth-module auth-module--dialog">
              <div class="auth-module__logo"></div>
              <h2 class="auth-module__title">Войти в date4you</h2>
              <p class="auth-module__description">Описание</p>
              <div style="height:420px">Способы входа</div>
            </dialog>
          </body></html>
        """)
        page.add_style_tag(content=(APP / "static/public.css").read_text("utf-8"))
        page.add_style_tag(content=(APP / "static/auth.css").read_text("utf-8"))
        state = page.locator("dialog").evaluate("""node => ({
          overflowY: getComputedStyle(node).overflowY,
          clientHeight: node.clientHeight,
          scrollHeight: node.scrollHeight,
          viewportHeight: innerHeight,
        })""")
        self.assertIn(state["overflowY"], ("auto", "scroll"))
        self.assertGreater(state["scrollHeight"], state["clientHeight"])
        self.assertLessEqual(state["clientHeight"], state["viewportHeight"])


if __name__ == "__main__":
    unittest.main()
