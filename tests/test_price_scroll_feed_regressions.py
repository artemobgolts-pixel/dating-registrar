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
        self.assertEqual(helpers.pay_label(4), "Бесплатно")

        admin_template = (APP / "templates/admin/date_form.html").read_text("utf-8")
        guest_template = (APP / "templates/public/category.html").read_text("utf-8")
        self.assertIn('name="pay" value="4"', admin_template)
        self.assertIn('name="pay" value="4"', guest_template)

        for relative in ("static/admin.js", "static/guest.js", "static/ui.js"):
            with self.subTest(script=relative):
                source = (APP / relative).read_text("utf-8")
                self.assertIn('"4": "Бесплатно"', source)


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
            ("categoryEditForm", "/admin/categories/7"),
            ("dateForm", "/admin/dates/9/edit"),
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
                page.evaluate("window.scrollTo(0, 0)")
                page.evaluate("document.dispatchEvent(new Event('turbo:load'))")
                page.wait_for_timeout(50)
                self.assertAlmostEqual(page.evaluate("window.scrollY"), 780, delta=1)

    def test_feed_author_emoji_sits_close_to_name(self):
        page = self.browser.new_page(viewport={"width": 900, "height": 600})
        self.addCleanup(page.close)
        page.set_content("""
          <a class="cfeed-owner cfeed-card-owner" href="#">
            <span class="cfeed-ava cfeed-ava-ph" aria-hidden="true">🙂</span>
            <span class="cfeed-owner-name">Автор</span>
          </a>
        """)
        page.add_style_tag(content=(APP / "static/admin.css").read_text("utf-8"))
        gap = page.evaluate("""() => {
          const avatar = document.querySelector('.cfeed-ava').getBoundingClientRect();
          const name = document.querySelector('.cfeed-owner-name').getBoundingClientRect();
          return name.left - avatar.right;
        }""")
        self.assertLessEqual(gap, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
