#!/usr/bin/env python3
"""Контракт компактных и доступных карточек подборок."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
PHOTO = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
    "width='1200' height='630'/%3E"
)


class CategoryCardTemplateTests(unittest.TestCase):
    def test_card_uses_deadline_activity_and_only_visible_exceptions(self):
        source = (APP / "templates/admin/categories.html").read_text("utf-8")

        self.assertIn("data-category-active=", source)
        self.assertIn("c['is_active']", source)
        self.assertIn("Дедлайн прошёл", source)
        self.assertIn("Настройте дедлайн", source)
        self.assertIn("Нужен выбор", source)
        self.assertIn("Ссылка выключена", source)
        self.assertNotIn("{{ category_voting(c['voting_status']) }}", source)
        self.assertNotIn("{{ category_link(c['link_enabled']) }}", source)
        self.assertIn('class="sr-only" id="cat-status-', source)
        self.assertIn("{% set has_status_exceptions =", source)
        status_row = source.split('{% if has_status_exceptions %}', 1)[1].split(
            '{% endif %}', 1,
        )[0]
        self.assertIn('class="entity-status-row cat-status-row"', status_row)

    def test_menu_lives_in_media_and_body_has_no_tail_reservation(self):
        source = (APP / "templates/admin/categories.html").read_text("utf-8")
        css = (APP / "static/admin.css").read_text("utf-8")

        media = source.split('<div class="cat-media">', 1)[1].split(
            '<div class="cat-body">', 1,
        )[0]
        self.assertIn('class="menu-wrap cat-card-menu"', media)
        self.assertIn('class="cat-thumb"', media)
        self.assertNotIn("cat-tail", source)
        self.assertNotIn("padding-right: 44px", css)

    def test_each_disclosure_menu_has_a_contextual_name_and_control(self):
        source = (APP / "templates/admin/categories.html").read_text("utf-8")

        self.assertIn(
            'aria-label="Ещё действия с подборкой «{{ c[\'name\'] }}»"', source,
        )
        self.assertIn('aria-controls="cat-menu-{{ c[\'id\'] }}"', source)
        self.assertIn('id="cat-menu-{{ c[\'id\'] }}"', source)
        self.assertNotIn('aria-haspopup="true"', source)


class CategoryCardGeometryBrowserTests(unittest.TestCase):
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

    def page(self, width: int, *, enlarged_text: bool = False):
        page = self.browser.new_page(viewport={"width": width, "height": 844})
        self.addCleanup(page.close)
        page.set_content(f"""
          <html data-skin="friends"><body>
            <main class="wrap">
              <article class="card cat-card has-thumb"
                       data-status-tone="neutral" data-category-active="false">
                <a class="cat-link" href="#opened" aria-describedby="cat-status-1"></a>
                <div class="cat-media">
                  <img class="cat-thumb" alt="" src="{PHOTO}">
                  <div class="menu-wrap cat-card-menu">
                    <button type="button" class="more" aria-label="Ещё действия">⋯</button>
                    <div class="menu"><button>Удалить подборку</button></div>
                  </div>
                </div>
                <div class="cat-body">
                  <div class="cat-heading-line">
                    <span class="cat-name">Очень длинное название подборки для телефона</span>
                    <span class="badge">12 событий</span>
                  </div>
                  <div class="entity-status-row cat-status-row">
                    <span class="entity-status entity-status--neutral">
                      <span class="entity-status-dot"></span><span>Дедлайн прошёл</span>
                    </span>
                    <span class="entity-status entity-status--danger">
                      <span class="entity-status-dot"></span><span>Ссылка выключена</span>
                    </span>
                    <span class="entity-status entity-status--warning">
                      <span class="entity-status-dot"></span><span>Нужен выбор</span>
                    </span>
                  </div>
                  <span class="sr-only" id="cat-status-1">Подборка неактивна.</span>
                </div>
              </article>
            </main>
          </body></html>
        """)
        page.add_style_tag(content=(APP / "static/admin.css").read_text("utf-8"))
        page.add_style_tag(content="""
          *, *::before, *::after {
            animation: none !important;
            transition: none !important;
          }
        """)
        if enlarged_text:
            page.add_style_tag(content="""
              .cat-card .cat-name { font-size: 1.5rem !important; }
              .cat-card .entity-status { font-size: 1.125rem !important; }
              .cat-card .badge { font-size: 1rem !important; }
            """)
        return page

    def test_mobile_media_menu_and_body_geometry_at_320_and_390(self):
        for width in (320, 390):
            with self.subTest(width=width):
                page = self.page(width)
                geometry = page.evaluate("""() => {
                  const card = document.querySelector('.cat-card').getBoundingClientRect();
                  const media = document.querySelector('.cat-media').getBoundingClientRect();
                  const image = document.querySelector('.cat-thumb').getBoundingClientRect();
                  const body = document.querySelector('.cat-body').getBoundingClientRect();
                  const menu = document.querySelector('.cat-card-menu .more').getBoundingClientRect();
                  return {
                    pageOverflow: document.documentElement.scrollWidth - innerWidth,
                    card: {left: card.left, right: card.right},
                    media: {left: media.left, right: media.right, top: media.top, bottom: media.bottom},
                    image: {width: image.width, height: image.height},
                    body: {left: body.left, right: body.right, top: body.top},
                    menu: {left: menu.left, right: menu.right, top: menu.top, bottom: menu.bottom,
                           width: menu.width, height: menu.height},
                  };
                }""")
                self.assertLessEqual(geometry["pageOverflow"], 1)
                self.assertAlmostEqual(
                    geometry["image"]["width"] / geometry["image"]["height"],
                    1200 / 630,
                    delta=0.03,
                )
                self.assertGreaterEqual(geometry["menu"]["width"], 44)
                self.assertGreaterEqual(geometry["menu"]["height"], 44)
                self.assertGreaterEqual(geometry["menu"]["left"], geometry["media"]["left"])
                self.assertLessEqual(geometry["menu"]["right"], geometry["media"]["right"])
                self.assertLessEqual(geometry["menu"]["top"], geometry["media"]["top"] + 12)
                self.assertAlmostEqual(
                    geometry["menu"]["right"], geometry["media"]["right"] - 8,
                    delta=3,
                )
                self.assertLessEqual(geometry["menu"]["bottom"], geometry["media"]["bottom"])
                self.assertAlmostEqual(
                    geometry["media"]["bottom"] - geometry["media"]["top"],
                    geometry["image"]["height"],
                    delta=2,
                )
                self.assertGreaterEqual(geometry["body"]["top"], geometry["media"]["bottom"] - 1)
                self.assertAlmostEqual(
                    geometry["body"]["right"] - geometry["body"]["left"],
                    geometry["media"]["right"] - geometry["media"]["left"],
                    delta=24,
                )

    def test_desktop_keeps_preview_beside_the_card_body(self):
        page = self.page(900)
        geometry = page.evaluate("""() => {
          const media = document.querySelector('.cat-media').getBoundingClientRect();
          const body = document.querySelector('.cat-body').getBoundingClientRect();
          return {media: {right: media.right, top: media.top, bottom: media.bottom},
                  body: {left: body.left, top: body.top, bottom: body.bottom}};
        }""")
        self.assertGreaterEqual(geometry["body"]["left"], geometry["media"]["right"])
        self.assertLessEqual(geometry["body"]["top"], geometry["media"]["bottom"])
        self.assertGreaterEqual(geometry["body"]["bottom"], geometry["media"]["top"])

    def test_enlarged_text_wraps_without_overflow_or_menu_collision(self):
        for width in (320, 390):
            with self.subTest(width=width):
                page = self.page(width, enlarged_text=True)
                geometry = page.evaluate("""() => {
                  const body = document.querySelector('.cat-body').getBoundingClientRect();
                  const name = document.querySelector('.cat-name').getBoundingClientRect();
                  const statuses = document.querySelector('.cat-status-row').getBoundingClientRect();
                  const menu = document.querySelector('.cat-card-menu .more').getBoundingClientRect();
                  return {
                    overflow: document.documentElement.scrollWidth - innerWidth,
                    bodyRight: body.right,
                    nameRight: name.right,
                    statusesRight: statuses.right,
                    bodyTop: body.top,
                    menuBottom: menu.bottom,
                  };
                }""")
                self.assertLessEqual(geometry["overflow"], 1)
                self.assertLessEqual(geometry["nameRight"], geometry["bodyRight"] + 1)
                self.assertLessEqual(geometry["statusesRight"], geometry["bodyRight"] + 1)
                self.assertGreaterEqual(geometry["bodyTop"], geometry["menuBottom"] - 1)


if __name__ == "__main__":
    unittest.main()
