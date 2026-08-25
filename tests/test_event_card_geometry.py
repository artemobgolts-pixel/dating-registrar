#!/usr/bin/env python3
"""Браузерные проверки вертикальной геометрии карточек событий."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
PHOTO = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
    "width='1200' height='675'/%3E"
)


class EventCardGeometryBrowserTests(unittest.TestCase):
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

    def page_with_styles(self, html: str, *styles: str):
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.addCleanup(page.close)
        page.set_content(html)
        for relative in styles:
            page.add_style_tag(content=(APP / relative).read_text("utf-8"))
        page.add_style_tag(content="""
          *, *::before, *::after {
            animation: none !important;
            transition: none !important;
          }
        """)
        return page

    @staticmethod
    def geometry(page, outer: str, media: str, body: str):
        return page.evaluate("""selectors => {
          const outer = document.querySelector(selectors.outer);
          const media = document.querySelector(selectors.media).getBoundingClientRect();
          const body = document.querySelector(selectors.body).getBoundingClientRect();
          const card = outer.getBoundingClientRect();
          const style = getComputedStyle(outer);
          return {
            display: style.display,
            direction: style.flexDirection,
            cardWidth: card.width,
            mediaX: media.x,
            mediaY: media.y,
            mediaWidth: media.width,
            mediaHeight: media.height,
            bodyX: body.x,
            bodyY: body.y,
            bodyWidth: body.width,
          };
        }""", {"outer": outer, "media": media, "body": body})

    def assert_vertical(self, geometry, label):
        self.assertAlmostEqual(
            geometry["bodyX"], geometry["mediaX"], delta=2, msg=label,
        )
        self.assertAlmostEqual(
            geometry["bodyWidth"], geometry["mediaWidth"], delta=2, msg=label,
        )
        self.assertGreaterEqual(
            geometry["bodyY"],
            geometry["mediaY"] + geometry["mediaHeight"] - 2,
            label,
        )

    @staticmethod
    def vertical_gap(page, upper: str, lower: str) -> float:
        return page.evaluate("""selectors => {
          const upper = document.querySelector(selectors.upper).getBoundingClientRect();
          const lower = document.querySelector(selectors.lower).getBoundingClientRect();
          return lower.top - upper.bottom;
        }""", {"upper": upper, "lower": lower})

    def test_admin_collection_feed_and_widget_keep_media_above_content(self):
        page = self.page_with_styles(f"""
          <html data-skin="romantic"><body>
            <main class="wrap">
              <section class="grid">
                <article class="dcard">
                  <div class="ph"><img alt="" src="{PHOTO}"></div>
                  <div class="b">
                    <h2 class="ttl">Моё событие</h2>
                    <div class="meta">Описание</div>
                    <div class="foot"><a class="edit-hint">Редактировать</a></div>
                  </div>
                </article>
              </section>
              <section class="ed-preview-col">
                <article class="pcard editable">
                  <div class="ed-gallery"><img alt="" src="{PHOTO}"></div>
                  <div class="body">
                    <h2 class="ttl">Превью редактора</h2>
                    <div class="pmeta">
                      <div class="ed-when">25.08.2026 в 19:00–21:00</div>
                      <div class="ed-place-row"><span>⌖</span><span class="ed-place">Место</span></div>
                    </div>
                    <div class="ed-field ed-desc">Описание события</div>
                    <div class="ed-links-row">
                      <span>↗</span><span class="ed-field ed-links">https://example.com</span>
                    </div>
                    <div class="acts"><button class="bk">Выбрать</button></div>
                  </div>
                </article>
              </section>
              <article class="card cat-card has-thumb">
                <img class="cat-thumb" alt="" src="{PHOTO}">
                <div class="cat-body">
                  <div class="bar">
                    <div><span class="cat-name">Категория</span></div>
                    <div class="cat-tail">
                      <div class="menu-wrap"><button class="more">⋯</button></div>
                    </div>
                  </div>
                </div>
              </article>
              <section class="dlist">
                <article class="drow">
                  <label class="drow-select"><input class="drow-check" type="checkbox"></label>
                  <a class="drow-cover"><img alt="" src="{PHOTO}"></a>
                  <div class="drow-main"><h2 class="drow-ttl">Событие списка</h2></div>
                  <div class="drow-act">
                    <button class="btn">Редактировать</button>
                    <div class="menu-wrap">
                      <button class="more">⋯</button>
                      <div class="menu open"><button>Удалить</button></div>
                    </div>
                  </div>
                </article>
                <article class="drow drow-without-cover">
                  <label class="drow-select"><input class="drow-check" type="checkbox"></label>
                  <div class="drow-main"><h2 class="drow-ttl">Событие без фото</h2></div>
                  <div class="drow-act"><button class="btn">Редактировать</button></div>
                </article>
              </section>
              <section class="cfeed">
                <article class="cfeed-card">
                  <div class="cfeed-ph"><img alt="" src="{PHOTO}"></div>
                  <div class="cfeed-body">
                    <h2 class="cfeed-ttl">Событие ленты</h2>
                    <div class="cfeed-card-actions"><button>Добавить</button></div>
                  </div>
                </article>
              </section>
              <div id="communityDlg" class="community-dlg">
                <div class="cwid has-media">
                  <div class="cwid-gallery"><img alt="" src="{PHOTO}"></div>
                  <div class="cwid-body">
                    <h2 class="cwid-ttl">Открытое событие</h2>
                    <div class="cwid-actions"><button class="btn">Поделиться</button></div>
                  </div>
                </div>
              </div>
            </main>
          </body></html>
        """, "static/admin.css")

        surfaces = (
            ("collection", ".dcard", ".dcard > .ph", ".dcard > .b"),
            (
                "admin-editor-preview", ".pcard.editable",
                ".pcard.editable > .ed-gallery", ".pcard.editable > .body",
            ),
            ("admin-list", ".drow", ".drow > .drow-cover", ".drow > .drow-main"),
            ("feed", ".cfeed-card", ".cfeed-card > .cfeed-ph", ".cfeed-card > .cfeed-body"),
            (
                "admin-widget", "#communityDlg .cwid.has-media",
                "#communityDlg .cwid-gallery", "#communityDlg .cwid-body",
            ),
        )
        for skin in ("romantic", "friends"):
            page.locator("html").evaluate(
                "(node, value) => node.dataset.skin = value", skin,
            )
            for name, outer, media, body in surfaces:
                measured = self.geometry(page, outer, media, body)
                self.assert_vertical(measured, (name, skin, "desktop"))
                self.assertLessEqual(measured["cardWidth"], 682, (name, skin))
                if name in {"collection", "admin-list", "feed", "admin-widget"}:
                    self.assertEqual(measured["direction"], "column", (name, skin))
            list_media = page.locator(".drow-cover").bounding_box()
            list_actions = page.locator(
                ".drow:not(.drow-without-cover) .drow-act",
            ).bounding_box()
            checkbox = page.locator(".drow-check").first.bounding_box()
            no_cover_checkbox = page.locator(
                ".drow-without-cover .drow-select",
            ).bounding_box()
            no_cover_title = page.locator(
                ".drow-without-cover .drow-ttl",
            ).bounding_box()
            open_menu = page.locator(".drow .menu.open").bounding_box()
            self.assertGreaterEqual(
                list_actions["y"], list_media["y"] + list_media["height"] - 2,
                skin,
            )
            self.assertIsNotNone(checkbox, skin)
            self.assertLessEqual(
                no_cover_checkbox["x"] + no_cover_checkbox["width"],
                no_cover_title["x"],
                skin,
            )
            self.assertEqual(
                page.locator(".drow").first.evaluate(
                    "node => getComputedStyle(node).overflow",
                ),
                "visible",
                skin,
            )
            self.assertIsNotNone(open_menu, skin)

            editor_actions_gap = self.vertical_gap(
                page, ".ed-preview-col .ed-links-row", ".ed-preview-col .acts",
            )
            self.assertGreaterEqual(editor_actions_gap, 12, (skin, "desktop"))
            self.assertLessEqual(editor_actions_gap, 18, (skin, "desktop"))

        page.set_viewport_size({"width": 390, "height": 844})
        for skin in ("romantic", "friends"):
            page.locator("html").evaluate(
                "(node, value) => node.dataset.skin = value", skin,
            )
            for name, outer, media, body in surfaces:
                if name == "admin-list":
                    # Мобильный list-view сохраняет прежнюю компактную строку.
                    continue
                self.assert_vertical(
                    self.geometry(page, outer, media, body),
                    (name, skin, "mobile"),
                )
            editor_actions_gap = self.vertical_gap(
                page, ".ed-preview-col .ed-links-row", ".ed-preview-col .acts",
            )
            self.assertGreaterEqual(editor_actions_gap, 12, (skin, "mobile"))
            self.assertLessEqual(editor_actions_gap, 18, (skin, "mobile"))

        for skin in ("romantic", "friends"):
            for theme in ("light", "dark"):
                page.locator("html").evaluate("""(node, appearance) => {
                  node.dataset.skin = appearance.skin;
                  node.dataset.theme = appearance.theme;
                }""", {"skin": skin, "theme": theme})
                right_edges = page.evaluate("""() => {
                  const thumb = document.querySelector(".cat-thumb").getBoundingClientRect();
                  const tail = document.querySelector(".cat-tail").getBoundingClientRect();
                  return {thumb: thumb.right, tail: tail.right};
                }""")
                self.assertAlmostEqual(
                    right_edges["thumb"], right_edges["tail"], delta=1,
                    msg=(skin, theme, "mobile category right edge"),
                )

    def test_profile_cards_and_open_widget_are_vertical_in_both_themes(self):
        page = self.page_with_styles(f"""
          <html data-skin="romantic"><body class="profile-public-page">
            <main class="profile-public-shell">
              <section class="pub-dates profile-tab-events">
                <div class="pub-grid">
                  <a class="pub-card">
                    <div class="ph"><img alt="" src="{PHOTO}"></div>
                    <div class="cb"><div class="t">Событие профиля</div></div>
                  </a>
                </div>
              </section>
              <div class="profile-event-dlg">
                <div class="cwid has-media">
                  <div class="cwid-gallery"><img alt="" src="{PHOTO}"></div>
                  <div class="cwid-body">
                    <h2 class="cwid-ttl">Открытое событие профиля</h2>
                    <div class="cwid-actions"><button class="btn">Поделиться</button></div>
                  </div>
                </div>
              </div>
              <div class="date-widget-dialog">
                <div class="ed-cols">
                  <div class="ed-preview-col">
                    <article class="pcard editable">
                      <div class="ed-gallery"><img alt="" src="{PHOTO}"></div>
                      <div class="body">
                        <h2 class="ttl">Превью предложения</h2>
                        <div class="acts"><button class="bk">Выбрать</button></div>
                      </div>
                    </article>
                  </div>
                  <aside class="ed-side-col">Поля события</aside>
                </div>
              </div>
            </main>
          </body></html>
        """, "static/public.css", "static/profile.css")

        surfaces = (
            ("profile-card", ".pub-card", ".pub-card > .ph", ".pub-card > .cb"),
            (
                "proposal-preview", ".date-widget-dialog .pcard",
                ".date-widget-dialog .ed-gallery", ".date-widget-dialog .body",
            ),
            (
                "profile-widget", ".profile-event-dlg .cwid.has-media",
                ".profile-event-dlg .cwid-gallery", ".profile-event-dlg .cwid-body",
            ),
        )
        for skin in ("romantic", "friends"):
            page.locator("html").evaluate(
                "(node, value) => node.dataset.skin = value", skin,
            )
            for name, outer, media, body in surfaces:
                measured = self.geometry(page, outer, media, body)
                self.assert_vertical(measured, (name, skin, "desktop"))
                self.assertLessEqual(measured["cardWidth"], 682, (name, skin))
            widget = self.geometry(
                page, ".profile-event-dlg .cwid.has-media",
                ".profile-event-dlg .cwid-gallery", ".profile-event-dlg .cwid-body",
            )
            self.assertEqual(widget["direction"], "column", skin)

        page.set_viewport_size({"width": 390, "height": 844})
        for name, outer, media, body in surfaces:
            self.assert_vertical(
                self.geometry(page, outer, media, body),
                (name, "mobile"),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
