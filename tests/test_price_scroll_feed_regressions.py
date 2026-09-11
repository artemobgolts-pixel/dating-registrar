#!/usr/bin/env python3
"""Регрессии бесплатного события, позиции редактора и подписи автора."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-price-scroll-")
os.environ.update({
    "DATA_DIR": _IMPORT_DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "price-scroll.test",
    "SECRET_KEY": "price-scroll-test-secret",
    "TG_BOT_TOKEN": "",
})

import admin_routes  # noqa: E402
import helpers  # noqa: E402
import public_routes  # noqa: E402


class FreePriceModifierTests(unittest.TestCase):
    def test_free_price_is_supported_by_server_and_both_editors(self):
        self.assertEqual(admin_routes.parse_pay("4"), 4)
        self.assertEqual(public_routes.parse_pay_split("4"), 4)
        self.assertEqual(helpers.pay_label(4), "Бесплатно")

        admin_template = (APP / "templates/admin/date_form.html").read_text("utf-8")
        guest_template = (APP / "templates/public/category.html").read_text("utf-8")
        for template in (admin_template, guest_template):
            self.assertIn('name="pay" value="4"', template)
            self.assertIn(">Бесплатно</label>", template)
            self.assertNotIn("🆓 Бесплатно", template)

        # Оба действующих редактора; неиспользуемый UI.editorPreview удалён в D.
        for relative in ("static/admin.js", "static/guest.js"):
            with self.subTest(script=relative):
                source = (APP / relative).read_text("utf-8")
                self.assertIn('"4": "Бесплатно"', source)
                self.assertIn("dataset.payValue", source)

    def test_free_price_badge_gets_the_selected_modifier_green(self):
        admin_template = (APP / "templates/admin/date_form.html").read_text("utf-8")
        guest_template = (APP / "templates/public/category.html").read_text("utf-8")
        for template in (admin_template, guest_template):
            self.assertGreaterEqual(template.count("data-pay-value="), 2)

        page_source = """
          <html data-theme="dark"><body>
            <span class="media-badge media-badge--pay" data-pay-value="4">Бесплатно</span>
          </body></html>
        """
        for stylesheet in ("static/admin.css", "static/public.css"):
            with self.subTest(stylesheet=stylesheet):
                from playwright.sync_api import sync_playwright

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.set_content(page_source)
                        page.add_style_tag(content=(APP / stylesheet).read_text("utf-8"))
                        colors = page.locator("[data-pay-value='4']").evaluate("""badge => {
                          const style = getComputedStyle(badge);
                          return { background: style.backgroundColor, color: style.color };
                        }""")
                        self.assertEqual(colors["background"], "rgb(21, 115, 74)")
                        self.assertEqual(colors["color"], "rgb(255, 255, 255)")
                    finally:
                        browser.close()


class EditorScrollAndFeedGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - зависит от локального окружения
            raise unittest.SkipTest(f"playwright недоступен: {exc!r}") from exc
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - зависит от локального окружения
            cls.playwright.stop()
            raise unittest.SkipTest(f"playwright chromium недоступен: {exc!r}") from exc

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()

    def test_successful_editor_save_restores_same_viewport_position_after_redirect(self):
        category_template = (APP / "templates/admin/category_detail.html").read_text("utf-8")
        date_template = (APP / "templates/admin/date_form.html").read_text("utf-8")
        self.assertRegex(
            category_template,
            re.compile(r'<form id="categoryEditForm"[^>]*data-preserve-scroll', re.S),
        )
        self.assertGreaterEqual(category_template.count("data-preserve-scroll"), 3)
        self.assertRegex(
            date_template,
            re.compile(r'<form[^>]*id="dateForm"[^>]*data-preserve-scroll', re.S),
        )
        self.assertIn('id="categoryVotingForm"', category_template)
        self.assertIn('id="categoryPrivacyForm"', category_template)

        for form_id, path, navigation in (
            ("categoryEditForm", "/admin/categories/7?return_to=%2Fadmin%2Fcategories", "turbo"),
            ("dateForm", "/admin/dates/9/edit?return_to=%2Fadmin%2Fdates%3Fview%3Dcards", "turbo"),
            ("categoryEditForm", "/admin/categories/7?return_to=%2Fadmin%2Fcategories", "reload"),
            ("dateForm", "/admin/dates/9/edit?return_to=%2Fadmin%2Fdates%3Fview%3Dcards", "reload"),
        ):
            with self.subTest(form=form_id, navigation=navigation):
                page = self.browser.new_page(viewport={"width": 900, "height": 600})
                self.addCleanup(page.close)

                def editor_document(with_flash=False):
                    flash = '<div class="flash" style="height:80px">Сохранено</div>' \
                        if with_flash else ""
                    turbo = '<script src="/static/vendor/turbo.min.js" defer></script>' \
                        if navigation == "turbo" else ""
                    return f"""<!doctype html>
                      <html><head>
                        <meta name="turbo-cache-control" content="no-cache">
                        {turbo}
                        <script src="/static/admin.js" defer></script>
                      </head><body>
                        {flash}
                        <form id="{form_id}" method="post" enctype="multipart/form-data"
                              data-preserve-scroll>
                          <input name="title" value="Тест">
                          <div style="height:2400px"></div>
                        </form>
                        <button id="save" type="submit" form="{form_id}"
                                style="position:fixed;inset:10px auto auto 10px">Сохранить</button>
                      </body></html>"""

                class EditorHandler(BaseHTTPRequestHandler):
                    def send_body(self, body, content_type, status=200, headers=None):
                        encoded = body if isinstance(body, bytes) else body.encode("utf-8")
                        self.send_response(status)
                        self.send_header("content-type", content_type)
                        self.send_header("content-length", str(len(encoded)))
                        for name, value in (headers or {}).items():
                            self.send_header(name, value)
                        self.end_headers()
                        self.wfile.write(encoded)

                    def do_GET(self):  # noqa: N802 - имя задаёт BaseHTTPRequestHandler
                        parsed = urlsplit(self.path)
                        if parsed.path == "/static/vendor/turbo.min.js":
                            self.send_body(
                                (APP / "static/vendor/turbo.min.js").read_bytes(),
                                "application/javascript",
                            )
                        elif parsed.path == "/static/admin.js":
                            self.send_body(
                                (APP / "static/admin.js").read_bytes(),
                                "application/javascript",
                            )
                        else:
                            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                            self.send_body(
                                editor_document(with_flash="msg" in query),
                                "text/html; charset=utf-8",
                            )

                    def do_POST(self):  # noqa: N802 - имя задаёт BaseHTTPRequestHandler
                        parsed = urlsplit(self.path)
                        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                        query["msg"] = "Сохранено"
                        location = urlunsplit(("", "", parsed.path, urlencode(query), ""))
                        self.send_body(b"", "text/plain", status=303, headers={"location": location})

                    def log_message(self, *_args):
                        pass

                server = ThreadingHTTPServer(("127.0.0.1", 0), EditorHandler)
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                try:
                    origin = f"http://127.0.0.1:{server.server_port}"
                    page.goto(f"{origin}{path}")
                    if navigation == "turbo":
                        page.wait_for_function("window.Turbo !== undefined")
                    page.evaluate("window.scrollTo(0, 780)")
                    anchor_top = page.locator(f"#{form_id}").evaluate(
                        "form => form.getBoundingClientRect().top"
                    )
                    page.locator("#save").click()
                    page.wait_for_url(re.compile(r"[?&]msg="), timeout=5000)
                    page.wait_for_timeout(100)
                    restored_anchor_top = page.locator(f"#{form_id}").evaluate(
                        "form => form.getBoundingClientRect().top"
                    )
                    self.assertAlmostEqual(restored_anchor_top, anchor_top, delta=1)
                finally:
                    server.shutdown()
                    server.server_close()
                    server_thread.join(timeout=2)

    def test_feed_author_avatar_and_emoji_both_have_five_pixel_gap(self):
        page = self.browser.new_page(viewport={"width": 900, "height": 600})
        self.addCleanup(page.close)
        page.set_content("""
          <a class="cfeed-owner cfeed-card-owner" href="#">
            <img class="cfeed-ava" alt="" width="26" height="26"
                 src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='26' height='26'/%3E">
            <span class="cfeed-owner-name">Автор с фото</span>
          </a>
          <a class="cfeed-owner cfeed-card-owner" href="#">
            <span class="cfeed-ava cfeed-ava-ph" aria-hidden="true">🙂</span>
            <span class="cfeed-owner-name">Автор без фото</span>
          </a>
        """)
        page.add_style_tag(content=(APP / "static/admin.css").read_text("utf-8"))
        gaps = page.evaluate("""() => {
          return [...document.querySelectorAll('.cfeed-owner')].map((owner) => {
            const avatar = owner.querySelector('.cfeed-ava').getBoundingClientRect();
            const name = owner.querySelector('.cfeed-owner-name').getBoundingClientRect();
            return name.left - avatar.right;
          });
        }""")
        self.assertEqual(gaps, [5, 5])

        self.assertRegex(
            (APP / "static/admin.css").read_text("utf-8"),
            re.compile(r"\.cfeed-ava-ph\s*\{[^}]*width:\s*auto", re.S),
        )
        placeholder_background = page.locator(".cfeed-ava-ph").evaluate(
            "el => getComputedStyle(el).backgroundColor"
        )
        self.assertEqual(placeholder_background, "rgba(0, 0, 0, 0)")

    def test_dark_theme_booking_card_uses_theme_surface(self):
        template = (APP / "templates/admin/date_form.html").read_text("utf-8")
        css = (APP / "static/admin.css").read_text("utf-8")
        self.assertIn('class="card date-bookings-card"', template)
        self.assertNotIn(
            'style="background:rgba(253,244,246,.85);border-color:#f0d9de"',
            template,
        )
        self.assertIn(".date-bookings-list", css)
        self.assertIn(".date-booking-row", css)

        page = self.browser.new_page(viewport={"width": 900, "height": 600})
        self.addCleanup(page.close)
        page.set_content("""
          <html data-theme="dark"><body>
            <div class="card date-bookings-card">
              <b>Кто выбрал</b>
              <div class="date-bookings-list">
                <div class="date-booking-row"><span>Артём — «Выезд на дачу»</span></div>
              </div>
            </div>
          </body></html>
        """)
        page.add_style_tag(content=css)
        contrast = page.locator(".date-bookings-card").evaluate("""card => {
          const context = document.createElement('canvas').getContext('2d');
          function rgb(value) {
            context.clearRect(0, 0, 1, 1);
            context.fillStyle = value;
            context.fillRect(0, 0, 1, 1);
            return [...context.getImageData(0, 0, 1, 1).data.slice(0, 3)];
          }
          function luminance(values) {
            const channels = values.map(value => {
              value /= 255;
              return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
            });
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
          }
          const style = getComputedStyle(card);
          const foreground = luminance(rgb(style.color));
          const background = luminance(rgb(style.backgroundColor));
          return (Math.max(foreground, background) + 0.05) /
                 (Math.min(foreground, background) + 0.05);
        }""")
        self.assertGreaterEqual(contrast, 4.5)

    def test_feed_search_clear_is_an_icon_inside_the_input(self):
        template = (APP / "templates/admin/dashboard.html").read_text("utf-8")
        css = (APP / "static/admin.css").read_text("utf-8")
        script = (APP / "static/admin.js").read_text("utf-8")

        search = template.split('id="communitySearchForm"', 1)[1].split("</form>", 1)[0]
        self.assertIn('class="cfeed-search-input"', search)
        self.assertRegex(
            search,
            re.compile(
                r'class="cfeed-search-input".*?id="communitySearchInput".*?'
                r'id="communitySearchClear"[^>]*aria-label="Сбросить поиск"[^>]*hidden',
                re.S,
            ),
        )
        self.assertNotIn(">Сбросить</button>", search)
        self.assertIn('<span aria-hidden="true">⟲</span>', search)
        self.assertNotIn(
            "Ищем также в описании и читаемой части ссылок на карты.", template,
        )
        self.assertIn(".cfeed-search-clear {", css)
        self.assertIn(".cfeed-search-clear[hidden] { display: none; }", css)
        self.assertNotIn(".cfeed-search-clear { grid-column: 1 / -1; width: 100%; }", css)
        self.assertIn("searchClear.hidden = !searchInput.value", script)

        page = self.browser.new_page(viewport={"width": 390, "height": 700})
        self.addCleanup(page.close)
        page.route(
            "https://price-scroll.test/**",
            lambda route: route.fulfill(content_type="text/html", body=""),
        )
        page.goto("https://price-scroll.test/admin/")
        page.set_content("""
          <form id="communitySearchForm">
            <div class="cfeed-search-row">
              <div class="cfeed-search-input">
                <input id="communitySearchInput" type="search">
                <button id="communitySearchClear" class="cfeed-search-clear"
                        type="button" aria-label="Сбросить поиск" hidden>
                  <span aria-hidden="true">⟲</span>
                </button>
              </div>
              <button type="submit" class="btn primary">Найти</button>
            </div>
          </form>
          <div id="communityFeed" data-feed-url="/admin/community"></div>
        """)
        page.add_style_tag(content=css)
        page.add_script_tag(content=script)
        page.evaluate("""() => {
          document.documentElement.dataset.theme = 'dark';
          document.documentElement.dataset.skin = 'friends';
        }""")
        page.evaluate("document.dispatchEvent(new Event('turbo:load'))")

        clear = page.locator("#communitySearchClear")
        field = page.locator("#communitySearchInput")
        self.assertTrue(clear.is_hidden())
        field.fill("я")
        self.assertTrue(clear.is_visible())
        geometry = page.evaluate("""() => {
          const input = document.querySelector('#communitySearchInput').getBoundingClientRect();
          const clear = document.querySelector('#communitySearchClear').getBoundingClientRect();
          return {
            inputLeft: input.left,
            inputTop: input.top,
            inputBottom: input.bottom,
            inputRight: input.right,
            clearLeft: clear.left,
            clearTop: clear.top,
            clearBottom: clear.bottom,
            clearRight: clear.right,
            clearWidth: clear.width,
            clearHeight: clear.height,
            background: getComputedStyle(document.querySelector('#communitySearchClear')).backgroundColor,
          };
        }""")
        self.assertGreaterEqual(geometry["clearLeft"], geometry["inputLeft"])
        self.assertLessEqual(geometry["clearRight"], geometry["inputRight"])
        self.assertGreaterEqual(geometry["clearTop"], geometry["inputTop"])
        self.assertLessEqual(geometry["clearBottom"], geometry["inputBottom"])
        self.assertGreaterEqual(geometry["clearWidth"], 24)
        self.assertGreaterEqual(geometry["clearHeight"], 24)
        self.assertEqual(geometry["background"], "rgba(0, 0, 0, 0)")
        clear.hover()
        self.assertEqual(
            clear.evaluate("el => getComputedStyle(el).backgroundColor"),
            "rgba(0, 0, 0, 0)",
        )
        field.press("Tab")
        self.assertEqual(page.evaluate("document.activeElement.id"), "communitySearchClear")
        clear.press("Enter")
        self.assertEqual(field.input_value(), "")
        self.assertTrue(clear.is_hidden())


if __name__ == "__main__":
    unittest.main(verbosity=2)
