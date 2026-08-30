#!/usr/bin/env python3
"""Строгий пространственный контракт между оформлениями date4you."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
PHOTO = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
    "width='1200' height='675'/%3E"
)


class SkinTemplateContractTests(unittest.TestCase):
    def test_public_surfaces_keep_copy_and_decorative_slots_skin_independent(self):
        for name in ("category.html", "share.html", "profile_review.html"):
            source = (APP / "templates/public" / name).read_text("utf-8")
            with self.subTest(template=name):
                self.assertIn('class="hero-ornament"', source)
                self.assertIn('class="footer-ornament"', source)
                self.assertIn("придумано, чтобы встречаться", source)
                self.assertNotIn("сделано с любовью", source)

        combined = "\n".join(
            (APP / "templates/public" / name).read_text("utf-8")
            for name in ("category.html", "share.html")
        )
        self.assertNotIn("Победитель выбран ♥", combined)
        self.assertNotIn("Победитель ♥", combined)
        self.assertNotIn("Это победившее событие ♥", combined)
        self.assertNotIn("кнопка внизу ♥", combined)



class SkinGeometryContractTests(unittest.TestCase):
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

    def page_with_styles(self, html: str, stylesheet: str, viewport: dict[str, int]):
        page = self.browser.new_page(viewport=viewport)
        self.addCleanup(page.close)
        page.set_content(html)
        page.add_style_tag(content=(APP / stylesheet).read_text("utf-8"))
        page.add_style_tag(content="""
          *, *::before, *::after {
            animation: none !important;
            transition: none !important;
          }
        """)
        page.evaluate("document.fonts && document.fonts.ready")
        return page

    @staticmethod
    def snapshot(page, selectors: tuple[str, ...]):
        return page.evaluate("""selectors => Object.fromEntries(selectors.map(selector => {
          const node = document.querySelector(selector);
          const rect = node.getBoundingClientRect();
          const style = getComputedStyle(node);
          return [selector, {
            rect: [rect.x, rect.y, rect.width, rect.height],
            typography: [
              style.fontFamily, style.fontSize, style.fontWeight,
              style.lineHeight, style.letterSpacing,
            ],
          }];
        }))""", selectors)

    def assert_skin_geometry(self, page, selectors: tuple[str, ...], context: str):
        snapshots = {}
        for skin in ("romantic", "friends"):
            page.locator("html").evaluate("(node, value) => node.dataset.skin = value", skin)
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            snapshots[skin] = self.snapshot(page, selectors)

        for selector in selectors:
            romantic = snapshots["romantic"][selector]
            friends = snapshots["friends"][selector]
            for axis, old, new in zip(("x", "y", "width", "height"), romantic["rect"], friends["rect"]):
                self.assertAlmostEqual(
                    old, new, delta=.75,
                    msg=(context, selector, axis, romantic["rect"], friends["rect"]),
                )
            self.assertEqual(
                romantic["typography"], friends["typography"],
                (context, selector, romantic["typography"], friends["typography"]),
            )

    def test_public_event_keeps_identical_geometry_between_skins(self):
        markup = f"""
          <html data-skin="romantic" data-theme="light"><body>
            <main class="wrap public-event-page public-share-page">
              <header class="hero"><h1>Прогулка по Крестовскому острову</h1></header>
              <section class="cards">
                <article class="card">
                  <div class="gal-wrap"><div class="gallery">
                    <img src="{PHOTO}" alt="">
                  </div></div>
                  <div class="body">
                    <div class="title-row"><h2 class="title">ЦПКиО (Крестовский остров)</h2></div>
                    <div class="meta"><div class="when">Завтра в 19:00</div></div>
                    <p class="comment">На крестах есть и парк, и кофейни.</p>
                    <div class="actions"><button class="btn book">Добавить</button><button class="btn ghost">Поделиться</button></div>
                  </div>
                </article>
              </section>
            </main>
          </body></html>
        """
        selectors = (
            ".wrap", ".hero", ".hero h1", ".cards", ".card", ".gallery",
            ".body", ".title", ".meta", ".comment", ".actions", ".btn.book",
        )
        for viewport in ({"width": 390, "height": 844}, {"width": 1100, "height": 900}):
            page = self.page_with_styles(markup, "static/public.css", viewport)
            self.assert_skin_geometry(page, selectors, f"public-{viewport['width']}")

    def test_admin_event_card_keeps_identical_geometry_between_skins(self):
        markup = f"""
          <html data-skin="romantic" data-theme="light"><body>
            <main class="wrap">
              <h1>События и подборки</h1>
              <section class="grid">
                <article class="dcard" data-status-tone="warning">
                  <div class="ph"><img src="{PHOTO}" alt=""></div>
                  <div class="b">
                    <h2 class="ttl serif">Прогулка по Крестовскому острову</h2>
                    <div class="entity-status-row dcard-status-row"><span class="entity-status">Активно</span></div>
                    <div class="meta"><span class="mrow">Завтра в 19:00</span></div>
                    <div class="foot"><a class="edit-hint">Редактировать</a><a class="btn small open-date">Открыть</a></div>
                  </div>
                </article>
              </section>
            </main>
          </body></html>
        """
        selectors = (
            ".wrap", "h1", ".grid", ".dcard", ".ph", ".b", ".ttl",
            ".dcard-status-row", ".meta", ".foot", ".edit-hint", ".open-date",
        )
        for viewport in ({"width": 390, "height": 844}, {"width": 1100, "height": 900}):
            page = self.page_with_styles(markup, "static/admin.css", viewport)
            self.assert_skin_geometry(page, selectors, f"admin-{viewport['width']}")

    def test_public_card_states_keep_identical_geometry_between_skins(self):
        markup = """
          <html data-skin="romantic" data-theme="light"><body>
            <main class="wrap public-event-page public-share-page">
              <section class="cards">
                <article class="card nophoto booked-me vote-winner">
                  <div class="body">
                    <h2 class="title">Очень длинное название общего события</h2>
                    <div class="winner-ribbon inflow"><span>Общий выбор</span></div>
                    <div class="meta"><div class="when">Завтра в 19:00</div></div>
                    <div class="actions"><button class="btn book">Добавить</button></div>
                  </div>
                </article>
              </section>
            </main>
          </body></html>
        """
        selectors = (
            ".card", ".body", ".title", ".winner-ribbon", ".meta",
            ".actions", ".btn.book",
        )
        for viewport in ({"width": 390, "height": 844}, {"width": 1100, "height": 900}):
            page = self.page_with_styles(markup, "static/public.css", viewport)
            self.assert_skin_geometry(page, selectors, f"public-states-{viewport['width']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
