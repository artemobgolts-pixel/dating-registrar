#!/usr/bin/env python3
"""Регрессии бесплатного события, позиции редактора и подписи автора."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path


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
        self.assertEqual(helpers.pay_label(4), "🆓 Бесплатно")

        admin_template = (APP / "templates/admin/date_form.html").read_text("utf-8")
        guest_template = (APP / "templates/public/category.html").read_text("utf-8")
        for template in (admin_template, guest_template):
            self.assertIn('name="pay" value="4"', template)
            self.assertIn("🆓 Бесплатно", template)

        for relative in ("static/admin.js", "static/guest.js", "static/ui.js"):
            with self.subTest(script=relative):
                source = (APP / relative).read_text("utf-8")
                self.assertIn('"4": "🆓 Бесплатно"', source)


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

    def test_successful_editor_save_restores_exact_scroll_position(self):
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

        for form_id, path in (
            ("categoryEditForm", "/admin/categories/7?return_to=%2Fadmin%2Fcategories"),
            ("dateForm", "/admin/dates/9/edit?return_to=%2Fadmin%2Fdates%3Fview%3Dcards"),
        ):
            with self.subTest(form=form_id):
                page = self.browser.new_page(viewport={"width": 900, "height": 600})
                self.addCleanup(page.close)
                page.route(
                    "https://price-scroll.test/**",
                    lambda route: route.fulfill(
                        content_type="text/html",
                        body="<!doctype html><html><body></body></html>",
                    ),
                )
                page.goto(f"https://price-scroll.test{path}")
                page.set_content(
                    f'<form id="{form_id}" data-preserve-scroll></form>'
                    '<div style="height:2400px"></div>'
                )
                page.add_script_tag(content=(APP / "static/admin.js").read_text("utf-8"))
                page.evaluate("document.dispatchEvent(new Event('turbo:load'))")
                page.evaluate("window.scrollTo(0, 780)")
                page.locator(f"#{form_id}").dispatch_event("submit")
                page.evaluate("""document.dispatchEvent(new CustomEvent(
                  'turbo:submit-end', { detail: { success: true } }
                ))""")
                separator = "&" if "?" in path else "?"
                page.evaluate(
                    "url => window.history.replaceState({}, '', url)",
                    f"{path}{separator}msg=Сохранено",
                )
                page.evaluate("window.scrollTo(0, 0)")
                page.evaluate("document.dispatchEvent(new Event('turbo:load'))")
                page.wait_for_timeout(50)
                self.assertAlmostEqual(page.evaluate("window.scrollY"), 780, delta=1)

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
                  <span aria-hidden="true">×</span>
                </button>
              </div>
              <button type="submit" class="btn primary">Найти</button>
            </div>
          </form>
          <div id="communityFeed" data-feed-url="/admin/community"></div>
        """)
        page.add_style_tag(content=css)
        page.add_script_tag(content=script)
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
            inputRight: input.right,
            clearRight: clear.right,
            clearWidth: clear.width,
            clearHeight: clear.height,
          };
        }""")
        self.assertLess(geometry["clearRight"], geometry["inputRight"])
        self.assertGreaterEqual(geometry["clearWidth"], 24)
        self.assertGreaterEqual(geometry["clearHeight"], 24)
        clear.click()
        self.assertEqual(field.input_value(), "")
        self.assertTrue(clear.is_hidden())


if __name__ == "__main__":
    unittest.main(verbosity=2)
