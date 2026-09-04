#!/usr/bin/env python3
"""UI-регрессии профиля, редактора события и контекстного обучения."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


class ProfileContractTests(unittest.TestCase):
    def test_profile_heading_has_its_own_stable_spacing_hook(self):
        profile = (APP / "templates/admin/profile.html").read_text("utf-8")
        self.assertIn('class="bar profile-header"', profile)


class ProfileEditorTourBrowserTests(unittest.TestCase):
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

    def test_avatar_delete_matches_camera_control_and_profile_gap_is_visible(self):
        page = self.browser.new_page(viewport={"width": 390, "height": 720})
        self.addCleanup(page.close)
        page.set_content("""
          <main class="wrap">
            <div class="bar profile-header" id="profileHeader"><h1>Мой профиль</h1></div>
            <div class="card" id="profileContent">
              <div class="avatar-field"><div class="avatar-wrap">
                <label class="avatar-pick">
                  <span class="avatar-ph">🙂</span>
                  <span class="avatar-cam" id="camera"></span>
                </label>
                <button class="avatar-delete" id="deleteAvatar" type="button"></button>
              </div></div>
            </div>
          </main>
        """)
        page.add_style_tag(content=(APP / "static/admin.css").read_text("utf-8"))

        geometry = page.evaluate("""() => {
          const header = document.querySelector('#profileHeader').getBoundingClientRect();
          const content = document.querySelector('#profileContent').getBoundingClientRect();
          const camera = document.querySelector('#camera').getBoundingClientRect();
          const remove = document.querySelector('#deleteAvatar').getBoundingClientRect();
          return {
            gap: content.top - header.bottom,
            cameraWidth: camera.width,
            cameraHeight: camera.height,
            removeWidth: remove.width,
            removeHeight: remove.height,
          };
        }""")
        self.assertAlmostEqual(geometry["gap"], 18, delta=0.1)
        self.assertAlmostEqual(geometry["removeWidth"], geometry["cameraWidth"], delta=0.1)
        self.assertAlmostEqual(geometry["removeHeight"], geometry["cameraHeight"], delta=0.1)
        self.assertEqual(geometry["removeWidth"], 34)

    def test_capacity_stepper_prevents_double_tap_zoom_but_keeps_press_feedback(self):
        page = self.browser.new_page(viewport={"width": 390, "height": 720})
        self.addCleanup(page.close)
        page.set_content('<button class="capacity-step" type="button">+</button>')
        page.add_style_tag(content=(APP / "static/admin.css").read_text("utf-8"))

        button = page.locator(".capacity-step")
        self.assertEqual(button.evaluate("node => getComputedStyle(node).touchAction"), "manipulation")
        box = button.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        self.assertNotEqual(button.evaluate("node => getComputedStyle(node).transform"), "none")
        page.mouse.up()

        page.emulate_media(reduced_motion="reduce")
        page.mouse.down()
        self.assertEqual(button.evaluate("node => getComputedStyle(node).transform"), "none")
        page.mouse.up()

    def test_not_important_hides_the_last_selected_payer_in_preview(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content("""
          <form id="dateForm">
            <input name="name"><input name="place"><textarea id="linksInput"></textarea>
            <textarea id="descInput"></textarea>
            <div class="pcard" id="edCard">
              <span id="payPhoto" class="ed-gallery-pay media-badge media-badge--pay"
                    data-preview="pay-photo" hidden></span>
              <h3 class="ttl"><span id="payPill"
                    class="pay media-badge media-badge--pay media-badge--inflow"
                    data-preview="pay" hidden></span></h3>
              <span id="edTitle"></span><span id="edPlace"></span><span id="edLinks"></span>
            </div>
            <label><input type="radio" name="pay" value="0" data-bind="pay" checked>Не важно</label>
            <label><input type="radio" name="pay" value="2" data-bind="pay">Я плачу</label>
          </form>
        """)
        page.add_style_tag(content=(APP / "static/admin.css").read_text("utf-8"))
        page.add_script_tag(content=(APP / "static/ui.js").read_text("utf-8"))
        page.add_script_tag(content=(APP / "static/admin.js").read_text("utf-8"))
        page.wait_for_function("document.querySelector('#dateForm').dataset.edReady === '1'")

        page.locator('input[name="pay"][value="2"]').check()
        self.assertTrue(page.locator("#payPill").is_visible())
        self.assertIn("Я плачу", page.locator("#payPill").inner_text())

        page.locator('input[name="pay"][value="0"]').check()
        self.assertFalse(page.locator("#payPill").is_visible())
        self.assertFalse(page.locator("#payPhoto").is_visible())

    def test_basics_navigation_carries_an_explicit_tour_request(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.route(
            "**/admin/profile",
            lambda route: route.fulfill(
                status=200, content_type="text/html",
                body="<html><body data-user-id='7'><main></main></body></html>",
            ),
        )
        page.goto("https://date4you.test/admin/profile")
        page.add_script_tag(content=(APP / "static/tour.js").read_text("utf-8"))
        page.evaluate("""() => {
          window.Turbo = { visit(url) { window.__tourVisit = url; } };
          window.d4yStartTour('dashboard');
        }""")
        self.assertEqual(page.evaluate("window.__tourVisit"), "/admin/#tour=dashboard")

    def test_category_tour_opens_appearance_before_highlighting_its_fields(self):
        page = self.browser.new_page(viewport={"width": 390, "height": 720})
        self.addCleanup(page.close)
        body = """
          <html><body data-user-id="7"><main class="wrap">
            <section data-tour="category-dates">События</section>
            <section data-tour="category-voting">Голосование</section>
            <section data-tour="category-actions">Поделиться</section>
            <details id="categoryAppearance">
              <summary>Оформление ссылки</summary>
              <div data-tour="category-description">Название и описание</div>
              <div data-tour="category-skin">Оформление страницы</div>
              <div data-tour="category-preview">Превью ссылки</div>
            </details>
          </main></body></html>
        """
        page.route(
            "**/admin/categories/7",
            lambda route: route.fulfill(status=200, content_type="text/html", body=body),
        )
        page.goto("https://date4you.test/admin/categories/7#tour=category-editor")
        page.add_script_tag(content=(APP / "static/tour.js").read_text("utf-8"))
        page.wait_for_selector(".tour-overlay")

        for _ in range(3):
            page.locator(".tour-next").click()
        page.wait_for_function("document.querySelector('#tourTitle').textContent === 'Название и описание'")

        self.assertTrue(page.locator("#categoryAppearance").evaluate("node => node.open"))
        self.assertGreater(page.locator('[data-tour="category-description"]').bounding_box()["height"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
