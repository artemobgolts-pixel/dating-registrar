#!/usr/bin/env python3
"""Регрессии быстрых длительностей и выбора дедлайна."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


class TimeAndDeadlineContractTests(unittest.TestCase):
    def test_both_category_forms_have_the_same_deadline_presets(self):
        for relative in (
            "templates/admin/category_new.html",
            "templates/admin/category_detail.html",
        ):
            with self.subTest(template=relative):
                source = (APP / relative).read_text("utf-8")
                self.assertIn("data-deadline-picker", source)
                self.assertIn("data-deadline-readable", source)
                self.assertIn('data-deadline-hours="3"', source)
                self.assertIn('data-deadline-hours="24"', source)
                self.assertIn('data-deadline-hours="72"', source)
                self.assertIn('data-deadline-hours="168"', source)

        new_source = (APP / "templates/admin/category_new.html").read_text("utf-8")
        detail_source = (APP / "templates/admin/category_detail.html").read_text("utf-8")
        self.assertNotIn('data-deadline-mode="extend"', new_source)
        self.assertIn('data-deadline-mode="extend"', detail_source)
        self.assertIn("Продлить:", detail_source)
        self.assertIn("Изменить дедлайн и настройки", detail_source)

    def test_public_time_forms_keep_duration_actions(self):
        for relative in (
            "templates/public/category.html",
            "templates/public/share.html",
        ):
            with self.subTest(template=relative):
                source = (APP / relative).read_text("utf-8")
                self.assertIn('id="timeStart"', source)
                self.assertIn('id="timeEnd"', source)
                self.assertIn('data-dur="1"', source)
                self.assertIn('data-dur="2"', source)
                self.assertIn('data-dur="3"', source)


class TimeAndDeadlineBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - зависит от dev-окружения
            raise unittest.SkipTest(f"playwright недоступен: {exc!r}") from exc
        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - браузер может быть не установлен
            if getattr(cls, "playwright", None):
                cls.playwright.stop()
            raise unittest.SkipTest(f"playwright chromium недоступен: {exc!r}") from exc

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()

    def test_duration_first_click_uses_today_and_next_click_accumulates(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content("""
          <div id="root">
            <input id="start" type="datetime-local">
            <input id="end" type="datetime-local">
            <button type="button" data-dur="1">+1 час</button>
            <button type="button" data-dur="2">+2 часа</button>
          </div>
        """)
        page.add_script_tag(content=(APP / "static/ui.js").read_text("utf-8"))
        page.evaluate("""UI.dateChips(
          document.querySelector('#root'),
          document.querySelector('#start'),
          document.querySelector('#end')
        )""")
        browser_today_before = page.evaluate(
            "new Date().toLocaleDateString('sv-SE')"
        )
        page.locator('[data-dur="1"]').click()
        start = datetime.fromisoformat(page.locator("#start").input_value())
        first_end = datetime.fromisoformat(page.locator("#end").input_value())
        browser_today_after = page.evaluate(
            "new Date().toLocaleDateString('sv-SE')"
        )
        self.assertIn(start.date().isoformat(), {browser_today_before, browser_today_after})
        self.assertEqual(first_end - start, timedelta(hours=1))

        page.locator('[data-dur="2"]').click()
        second_end = datetime.fromisoformat(page.locator("#end").input_value())
        self.assertEqual(second_end - start, timedelta(hours=3))

    def test_duration_copies_the_start_date_and_handles_midnight(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content("""
          <div id="root">
            <input id="start" type="datetime-local" value="2030-05-07T23:30">
            <input id="end" type="datetime-local">
            <button type="button" data-dur="2">+2 часа</button>
          </div>
        """)
        page.add_script_tag(content=(APP / "static/ui.js").read_text("utf-8"))
        page.evaluate("""UI.dateChips(
          document.querySelector('#root'),
          document.querySelector('#start'),
          document.querySelector('#end')
        )""")
        page.locator('[data-dur="2"]').click()
        self.assertEqual(page.locator("#end").input_value(), "2030-05-08T01:30")

    def test_deadline_preset_sets_value_and_keeps_manual_keyboard_input(self):
        context = self.browser.new_context(timezone_id="UTC")
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_content("""
          <body>
            <div data-deadline-picker>
              <input name="voting_deadline" type="datetime-local" data-picker-only>
              <button type="button" data-deadline-hours="24"
                      aria-pressed="false">Завтра</button>
              <small data-deadline-readable></small>
            </div>
          </body>
        """)
        page.evaluate("""() => {
          const NativeDate = Date;
          const fixedNow = '2030-05-07T21:07:30.000Z';
          window.Date = class extends NativeDate {
            constructor(...args) {
              super(...(args.length ? args : [fixedNow]));
            }
            static now() { return new NativeDate(fixedNow).getTime(); }
          };
        }""")
        page.add_script_tag(content=(APP / "static/admin.js").read_text("utf-8"))
        page.wait_for_function(
            "document.querySelector('[data-deadline-picker]').dataset.deadlineReady === '1'"
        )
        page.locator('[data-deadline-hours="24"]').click()

        value = page.locator('[name="voting_deadline"]').input_value()
        # 21:07:30 UTC = 00:07:30 МСК; вверх до 00:15 и ещё 24 часа.
        self.assertEqual(value, "2030-05-09T00:15")
        self.assertEqual(
            page.locator('[name="voting_deadline"]').get_attribute("min"),
            "2030-05-08T00:08",
        )
        self.assertEqual(
            page.locator('[data-deadline-hours="24"]').get_attribute("aria-pressed"),
            "true",
        )
        readable = page.locator("[data-deadline-readable]").inner_text()
        self.assertIn("9 мая", readable)
        self.assertIn("00:15", readable)
        self.assertTrue(readable.endswith(" МСК"), readable)
        # Цифры больше не блокируются прежним обработчиком data-picker-only.
        allowed = page.evaluate("""() => document.querySelector(
          '[name="voting_deadline"]'
        ).dispatchEvent(new KeyboardEvent('keydown', {
          key: '2', bubbles: true, cancelable: true
        }))""")
        self.assertTrue(allowed)

    def test_detail_deadline_preset_extends_selected_value_without_rounding_it(self):
        context = self.browser.new_context(timezone_id="UTC")
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_content("""
          <body>
            <div data-deadline-picker data-deadline-mode="extend">
              <input name="voting_deadline" type="datetime-local"
                     value="2030-05-10T10:07" data-picker-only>
              <button type="button" data-deadline-hours="24"
                      aria-pressed="false">+1 день</button>
              <small data-deadline-readable></small>
            </div>
          </body>
        """)
        page.evaluate("""() => {
          const NativeDate = Date;
          const fixedNow = '2030-05-07T21:07:30.000Z';
          window.Date = class extends NativeDate {
            constructor(...args) {
              super(...(args.length ? args : [fixedNow]));
            }
            static now() { return new NativeDate(fixedNow).getTime(); }
          };
        }""")
        page.add_script_tag(content=(APP / "static/admin.js").read_text("utf-8"))
        page.wait_for_function(
            "document.querySelector('[data-deadline-picker]').dataset.deadlineReady === '1'"
        )

        page.locator('[data-deadline-hours="24"]').click()

        self.assertEqual(
            page.locator('[name="voting_deadline"]').input_value(),
            "2030-05-11T10:07",
        )
        self.assertEqual(
            page.locator('[data-deadline-hours="24"]').get_attribute("aria-pressed"),
            "true",
        )
        self.assertIn(
            "11 мая",
            page.locator("[data-deadline-readable]").inner_text(),
        )

    def test_public_event_card_is_horizontal_on_desktop_and_vertical_on_mobile(self):
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.addCleanup(page.close)
        page.set_content("""
          <html data-skin="romantic">
            <body class="public-event-page public-category-page">
              <div class="wrap">
                <section class="cards">
                  <article class="card">
                    <div class="gal-wrap">
                      <div class="gallery">
                        <img alt="" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='1200' height='800'/%3E">
                      </div>
                    </div>
                    <div class="body">
                      <h2 class="title">Событие</h2>
                      <p class="comment">Описание события для проверки геометрии.</p>
                    </div>
                  </article>
                </section>
              </div>
            </body>
          </html>
        """)
        page.add_style_tag(content=(APP / "static/public.css").read_text("utf-8"))

        def geometry():
            return page.evaluate("""() => {
              const wrap = document.querySelector('.wrap').getBoundingClientRect();
              const card = document.querySelector('.card');
              const media = document.querySelector('.gal-wrap').getBoundingClientRect();
              const body = document.querySelector('.card > .body').getBoundingClientRect();
              return {
                wrapWidth: wrap.width,
                display: getComputedStyle(card).display,
                mediaX: media.x,
                mediaY: media.y,
                mediaWidth: media.width,
                mediaHeight: media.height,
                bodyX: body.x,
                bodyY: body.y,
                bodyWidth: body.width
              };
            }""")

        for skin in ("romantic", "friends"):
            page.locator("html").evaluate(
                "(node, value) => node.dataset.skin = value", skin,
            )
            desktop = geometry()
            self.assertEqual(desktop["display"], "grid", skin)
            self.assertAlmostEqual(desktop["wrapWidth"], 1080, delta=1)
            self.assertGreaterEqual(desktop["mediaWidth"], 340)
            self.assertGreater(desktop["bodyX"], desktop["mediaX"])
            self.assertAlmostEqual(desktop["bodyY"], desktop["mediaY"], delta=2)
            self.assertGreater(desktop["bodyWidth"], desktop["mediaWidth"])

        page.set_viewport_size({"width": 390, "height": 844})
        for skin in ("romantic", "friends"):
            page.locator("html").evaluate(
                "(node, value) => node.dataset.skin = value", skin,
            )
            mobile = geometry()
            self.assertEqual(mobile["display"], "block", skin)
            self.assertLessEqual(mobile["wrapWidth"], 390)
            self.assertAlmostEqual(mobile["bodyX"], mobile["mediaX"], delta=2)
            self.assertGreaterEqual(
                mobile["bodyY"], mobile["mediaY"] + mobile["mediaHeight"] - 2,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
