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
                <article class="dcard" data-status-tone="warning">
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
              <article class="card cat-card has-thumb" data-status-tone="danger">
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
                <article class="drow" data-status-tone="neutral">
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
                <article class="drow drow-without-cover" data-status-tone="success">
                  <label class="drow-select"><input class="drow-check" type="checkbox"></label>
                  <div class="drow-main"><h2 class="drow-ttl">Событие без фото</h2></div>
                  <div class="drow-act"><button class="btn">Редактировать</button></div>
                </article>
              </section>
              <section class="card category-events-card">
                <div class="table-wrap"><table><tbody>
                  <tr data-status-tone="warning">
                    <td data-label="Порядок">⋮⋮</td>
                    <td data-label="Событие">Событие внутри подборки</td>
                    <td data-label="Когда">25.08.2026</td>
                    <td data-label="Голоса / мест">2 / 4</td>
                    <td data-label="Действия"><button>Открыть</button></td>
                  </tr>
                </tbody></table></div>
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

            status_surfaces = page.evaluate("""() => {
              const read = selector => {
                const style = getComputedStyle(document.querySelector(selector));
                return {
                  edgeWidth: style.borderInlineStartWidth,
                  edgeColor: style.borderInlineStartColor,
                  wash: style.backgroundColor,
                };
              };
              return {
                warning: read('.dcard[data-status-tone="warning"]'),
                danger: read('.cat-card[data-status-tone="danger"]'),
                neutral: read('.drow[data-status-tone="neutral"]'),
                success: read('.drow[data-status-tone="success"]'),
              };
            }""")
            for tone in ("warning", "danger", "neutral", "success"):
                self.assertEqual(status_surfaces[tone]["edgeWidth"], "4px", (skin, tone))
            for tone in ("warning", "danger", "neutral"):
                self.assertNotIn(
                    status_surfaces[tone]["wash"], ("transparent", "rgba(0, 0, 0, 0)"),
                    (skin, tone),
                )
            self.assertIn(
                status_surfaces["success"]["wash"],
                ("transparent", "rgba(0, 0, 0, 0)"),
                skin,
            )
            self.assertNotEqual(
                status_surfaces["warning"]["edgeColor"],
                status_surfaces["danger"]["edgeColor"],
                skin,
            )
            table_status = page.locator(
                '.category-events-card tr[data-status-tone="warning"] > td:first-child',
            ).evaluate("""node => {
              const style = getComputedStyle(node);
              return {edgeWidth: style.borderInlineStartWidth, wash: style.backgroundColor};
            }""")
            self.assertEqual(table_status["edgeWidth"], "4px", skin)
            self.assertNotIn(
                table_status["wash"], ("transparent", "rgba(0, 0, 0, 0)"), skin,
            )

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
            mobile_table_status = page.locator(
                '.category-events-card tr[data-status-tone="warning"]',
            ).evaluate("""node => {
              const style = getComputedStyle(node);
              const first = getComputedStyle(node.querySelector('td:first-child'));
              return {
                edgeWidth: style.borderInlineStartWidth,
                wash: style.backgroundColor,
                cellEdgeWidth: first.borderInlineStartWidth,
              };
            }""")
            self.assertEqual(mobile_table_status["edgeWidth"], "4px", skin)
            self.assertEqual(mobile_table_status["cellEdgeWidth"], "0px", skin)
            self.assertNotIn(
                mobile_table_status["wash"],
                ("transparent", "rgba(0, 0, 0, 0)"),
                skin,
            )

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
                for selector in (
                    '.dcard[data-status-tone="warning"]',
                    '.cat-card[data-status-tone="danger"]',
                    '.drow[data-status-tone="neutral"]',
                ):
                    self.assertEqual(
                        page.locator(selector).evaluate(
                            "node => getComputedStyle(node).borderInlineStartWidth",
                        ),
                        "4px",
                        (skin, theme, selector),
                    )

    def test_public_winner_keeps_a_green_semantic_status_in_every_theme(self):
        page = self.page_with_styles("""
          <html data-skin="romantic" data-theme="light"><body>
            <article id="winner" class="card booked-me vote-winner">
              <div class="gal-wrap">
                <div class="winner-ribbon">
                  <svg class="ui-icon ui-icon-check winner-check" viewBox="0 0 24 24">
                    <circle cx="12" cy="12" r="9"></circle>
                    <path d="m8 12 2.7 2.8L16.6 9"></path>
                  </svg>
                  <span>Победитель</span>
                </div>
                <div class="gallery"></div>
              </div>
              <div class="body"><h2 class="title">Общий выбор</h2></div>
            </article>
          </body></html>
        """, "static/public.css")

        for skin in ("romantic", "friends"):
            for theme in ("light", "dark"):
                page.locator("html").evaluate(
                    """(node, appearance) => {
                      node.dataset.skin = appearance.skin;
                      node.dataset.theme = appearance.theme;
                    }""",
                    {"skin": skin, "theme": theme},
                )
                status = page.evaluate("""() => {
                  const card = getComputedStyle(document.querySelector('#winner'));
                  const winnerEdge = getComputedStyle(
                    document.querySelector('#winner'), '::before'
                  );
                  const ribbon = getComputedStyle(document.querySelector('.winner-ribbon'));
                  const body = getComputedStyle(document.querySelector('#winner > .body'));
                  const icon = document.querySelector('.winner-check').getBoundingClientRect();
                  const channels = card.borderTopColor.match(/[\\d.]+/g).map(Number);
                  return {
                    topWidth: card.borderTopWidth,
                    leftWidth: card.borderLeftWidth,
                    border: channels.slice(0, 3),
                    shadow: card.boxShadow,
                    selectedEdge: winnerEdge.backgroundImage,
                    ribbonColor: ribbon.color,
                    ribbonBackground: ribbon.backgroundImage,
                    bodyTint: body.backgroundColor,
                    icon: [icon.width, icon.height],
                  };
                }""")
                context = (skin, theme)
                self.assertEqual(status["topWidth"], "2px", context)
                self.assertEqual(
                    status["leftWidth"], "5px" if skin == "friends" else "2px",
                    context,
                )
                red, green, blue = status["border"]
                self.assertGreater(green, red, context)
                self.assertGreater(green, blue, context)
                self.assertNotEqual(status["shadow"], "none", context)
                self.assertIn("linear-gradient", status["selectedEdge"], context)
                self.assertNotIn("rgb(182, 95, 111)", status["selectedEdge"], context)
                self.assertEqual(status["ribbonColor"], "rgb(255, 255, 255)", context)
                self.assertIn("linear-gradient", status["ribbonBackground"], context)
                self.assertNotEqual(status["bodyTint"], "rgba(0, 0, 0, 0)", context)
                self.assertEqual(status["icon"], [14, 14], context)

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

    def test_semantic_badges_keep_distinct_shapes_and_editor_positioning(self):
        guest_js = (APP / "static/guest.js").read_text("utf-8")
        self.assertIn(
            'cnt.className = "gal-count count-badge count-badge--overlay"',
            guest_js,
        )
        for template_name in ("category.html", "share.html"):
            template = (APP / "templates/public" / template_name).read_text("utf-8")
            self.assertIn(
                'class="lb-count count-badge count-badge--overlay"', template,
            )
        dates_template = (APP / "templates/admin/dates.html").read_text("utf-8")
        self.assertIn(
            "badge badge-meta media-badge media-badge--guest media-badge--inflow",
            dates_template,
        )
        self.assertIn(
            "badge badge-split media-badge media-badge--pay media-badge--inflow",
            dates_template,
        )

        admin = self.page_with_styles(f"""
          <html data-skin="romantic" data-theme="light"><body>
            <span id="count" class="badge count-badge count-badge--label">4 события</span>
            <a class="bell"><span id="overlay-count" class="bell-count count-badge count-badge--overlay">99+</span></a>
            <div class="tabs"><a class="on">Активные <span id="tab-count" class="pill count-badge count-badge--tab">999</span></a></div>
            <button id="choice" class="deadline-preset choice-chip" aria-pressed="true">Завтра</button>
            <label id="pay-choice" class="pay-opt choice-chip"><input type="radio" checked>50/50</label>
            <article class="dcard">
              <div class="ph">
                <img alt="" src="{PHOTO}">
                <span id="photo" class="bdg guest media-badge media-badge--guest">Идея гостя</span>
              </div>
              <div class="badges media-badges media-badges--inflow inflow">
                <span id="inflow" class="bdg guest media-badge media-badge--guest">Предложено</span>
              </div>
            </article>
            <article class="pcard">
              <div class="ed-gallery">
                <span id="editor-photo" class="ed-gallery-pay bdg pay media-badge media-badge--pay">50/50</span>
              </div>
              <div class="body"><h3 class="ttl">
                Событие
                <span id="editor-flow" class="pay media-badge media-badge--pay media-badge--inflow">50/50</span>
              </h3></div>
            </article>
          </body></html>
        """, "static/admin.css")

        public = self.page_with_styles(f"""
          <html data-skin="romantic" data-theme="light"><body>
            <article class="card"><div class="gal-wrap">
              <div class="gallery"><img alt="" src="{PHOTO}"></div>
              <div class="badges"><span id="photo" class="badge guest media-badge media-badge--guest">Идея гостя</span></div>
            </div></article>
            <div class="badges media-badges media-badges--inflow inflow">
              <span id="inflow" class="badge wait media-badge media-badge--pending">Ждёт проверки</span>
            </div>
            <div class="date-widget-dialog">
              <button id="choice" class="chip choice-chip">Завтра</button>
              <label id="pay-choice" class="pay-opt choice-chip"><input type="radio" checked>50/50</label>
              <article class="pcard"><div class="ed-gallery">
                <span id="editor-photo" class="ed-gallery-pay pay media-badge media-badge--pay">50/50</span>
              </div><div class="body"><h3 class="ttl">
                Событие
                <span id="editor-flow" class="pay media-badge media-badge--pay media-badge--inflow">50/50</span>
              </h3></div></article>
            </div>
            <div id="gal-count" class="gal-count count-badge count-badge--overlay">3 / 12</div>
            <div id="lb-count" class="lb-count count-badge count-badge--overlay">3 / 12</div>
          </body></html>
        """, "static/public.css")

        def badge_styles(page, selector):
            return page.locator(selector).evaluate("""node => {
              const style = getComputedStyle(node);
              const mark = getComputedStyle(node, '::before');
              return {
                radius: style.borderRadius,
                background: style.backgroundColor,
                color: style.color,
                position: style.position,
                shadow: style.boxShadow,
                markWidth: mark.width,
                markHeight: mark.height,
              };
            }""")

        def contrast_ratio(page, selector):
            return page.locator(selector).evaluate("""node => {
              const parse = value => {
                const parts = value.match(/[\\d.]+/g).map(Number);
                return [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1];
              };
              const compositeOnWhite = rgba => rgba.slice(0, 3).map(
                channel => channel * rgba[3] + 255 * (1 - rgba[3])
              );
              const luminance = rgb => {
                const linear = rgb.map(value => {
                  const channel = value / 255;
                  return channel <= .04045
                    ? channel / 12.92
                    : Math.pow((channel + .055) / 1.055, 2.4);
                });
                return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2];
              };
              const style = getComputedStyle(node);
              const foreground = compositeOnWhite(parse(style.color));
              const background = compositeOnWhite(parse(style.backgroundColor));
              const a = luminance(foreground);
              const b = luminance(background);
              return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
            }""")

        self.assertEqual(badge_styles(admin, "#count")["radius"], "7px")
        self.assertEqual(badge_styles(admin, "#choice")["radius"], "11px")

        for page, label in ((admin, "admin"), (public, "public")):
            for skin in ("romantic", "friends"):
                for theme in ("light", "dark"):
                    page.locator("html").evaluate("""(node, appearance) => {
                      node.dataset.skin = appearance.skin;
                      node.dataset.theme = appearance.theme;
                    }""", {"skin": skin, "theme": theme})
                    context = (label, skin, theme)
                    photo = badge_styles(page, "#photo")
                    inflow = badge_styles(page, "#inflow")
                    editor_photo = badge_styles(page, "#editor-photo")
                    editor_flow = badge_styles(page, "#editor-flow")
                    self.assertEqual(photo["radius"], "8px", context)
                    self.assertEqual(
                        photo["background"], "rgba(31, 27, 30, 0.84)", context,
                    )
                    self.assertEqual(photo["color"], "rgb(255, 255, 255)", context)
                    self.assertEqual(photo["markWidth"], "3px", context)
                    self.assertEqual(photo["markHeight"], "14px", context)
                    self.assertNotEqual(inflow["background"], photo["background"], context)
                    self.assertEqual(inflow["shadow"], "none", context)
                    self.assertEqual(editor_photo["position"], "absolute", context)
                    self.assertEqual(editor_photo["radius"], "8px", context)
                    self.assertNotEqual(editor_flow["background"], photo["background"], context)
                    self.assertEqual(editor_flow["shadow"], "none", context)

                    choice_selectors = ["#pay-choice"]
                    count_selectors = ["#gal-count", "#lb-count"] if label == "public" else [
                        "#overlay-count", "#tab-count",
                    ]
                    if label == "admin":
                        choice_selectors.append("#choice")
                    for selector in choice_selectors + count_selectors:
                        self.assertGreaterEqual(
                            contrast_ratio(page, selector), 4.5,
                            (context, selector, "contrast"),
                        )
                    for selector in count_selectors:
                        fits = page.locator(selector).evaluate(
                            "node => node.scrollWidth <= node.clientWidth",
                        )
                        self.assertTrue(fits, (context, selector, "count clipping"))

        self.assertEqual(badge_styles(public, "#choice")["radius"], "11px")

    def test_admin_buttons_center_labels_and_social_remove_keeps_a_44px_hit_area(self):
        page = self.page_with_styles("""
          <html data-skin="friends" data-theme="light"><body>
            <a id="back" class="btn editor-back" href="#"><span>← Назад</span></a>
            <button id="action" class="btn primary" type="button">
              <svg class="admin-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h16"/></svg>
              <span>Добавить</span>
            </button>
            <div class="social-links">
              <a class="social-service" href="#">
                <span class="social-state social-state-add" id="socialAdd"></span>
              </a>
              <div class="social-service is-linked">
                <form class="social-service-action">
                  <button class="social-state-control" id="socialRemoveAction" type="button">
                    <span class="social-state social-state-remove" id="socialRemove"></span>
                  </button>
                </form>
              </div>
            </div>
          </body></html>
        """, "static/admin.css")

        for selector, expected_display in (("#back", "flex"), ("#action", "inline-flex")):
            geometry = page.locator(selector).evaluate("""node => {
              const outer = node.getBoundingClientRect();
              const label = node.querySelector('span').getBoundingClientRect();
              const style = getComputedStyle(node);
              return {
                display: style.display,
                align: style.alignItems,
                justify: style.justifyContent,
                centerDelta: Math.abs(
                  (outer.top + outer.height / 2) - (label.top + label.height / 2)
                ),
              };
            }""")
            self.assertEqual(geometry["display"], expected_display, selector)
            self.assertEqual(geometry["align"], "center", selector)
            self.assertEqual(geometry["justify"], "center", selector)
            self.assertLessEqual(geometry["centerDelta"], 1, selector)

        sizes = page.evaluate("""() => {
          const size = selector => {
            const rect = document.querySelector(selector).getBoundingClientRect();
            return [rect.width, rect.height];
          };
          return {
            add: size('#socialAdd'),
            remove: size('#socialRemove'),
            action: size('#socialRemoveAction'),
          };
        }""")
        self.assertEqual(sizes["add"], [24, 24])
        self.assertEqual(sizes["remove"], [24, 24])
        self.assertEqual(sizes["action"], [44, 44])

        for viewport in ({"width": 1280, "height": 700}, {"width": 390, "height": 700}):
            page.set_viewport_size(viewport)
            for skin in ("friends", "romantic"):
                for theme in ("light", "dark"):
                    page.locator("html").evaluate(
                        "(node, state) => { node.dataset.skin = state.skin; "
                        "node.dataset.theme = state.theme; }",
                        {"skin": skin, "theme": theme},
                    )
                    centers = page.locator("#socialRemove").evaluate("""node => {
                      const circle = node.getBoundingClientRect();
                      const positioned = getComputedStyle(node).position !== 'static'
                        ? node
                        : node.closest('.social-state-control');
                      const owner = positioned.getBoundingClientRect();
                      const mark = getComputedStyle(node, '::before');
                      const resolve = (value, size) => value.endsWith('%')
                        ? size * Number.parseFloat(value) / 100
                        : Number.parseFloat(value);
                      return {
                        circleX: circle.left + circle.width / 2,
                        circleY: circle.top + circle.height / 2,
                        crossX: owner.left + positioned.clientLeft
                          + resolve(mark.left, positioned.clientWidth),
                        crossY: owner.top + positioned.clientTop
                          + resolve(mark.top, positioned.clientHeight),
                      };
                    }""")
                    context = (viewport, skin, theme, centers)
                    self.assertLessEqual(
                        abs(centers["circleX"] - centers["crossX"]), 0.5, context,
                    )
                    self.assertLessEqual(
                        abs(centers["circleY"] - centers["crossY"]), 0.5, context,
                    )

    def test_dashboard_copy_feedback_preserves_responsive_labels_and_icon(self):
        page = self.page_with_styles("""
          <html data-skin="friends" data-theme="light"><body>
            <div class="quick">
              <button id="copy" type="button" class="btn ghost" data-copy="https://example.test">
                <svg id="copyIcon" class="admin-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h16"/></svg>
                <span class="lbl-full">Скопировать ссылку</span><span class="lbl-short">Ссылка</span>
              </button>
            </div>
          </body></html>
        """, "static/admin.css")
        page.evaluate("""() => {
          Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText: () => Promise.resolve() },
          });
          window.UI = {};
        }""")
        page.add_script_tag(content=(APP / "static/admin.js").read_text("utf-8"))

        copy = page.locator("#copy")
        copy.click()
        page.wait_for_function(
            "document.querySelector('.lbl-full').textContent === 'Скопировано ✓'",
        )
        self.assertEqual(copy.locator("#copyIcon").count(), 1)
        self.assertEqual(copy.locator(".lbl-full, .lbl-short").count(), 2)

        # Повторный быстрый клик должен продлить feedback, но исходное содержимое
        # по-прежнему восстанавливается один раз и без склеивания подписей.
        copy.click()
        page.wait_for_timeout(1600)
        self.assertEqual(copy.locator("#copyIcon").count(), 1)
        self.assertEqual(copy.locator(".lbl-full").inner_text(), "Скопировать ссылку")
        self.assertEqual(copy.locator(".lbl-short").inner_text(), "Ссылка")
        self.assertEqual(copy.locator(".lbl-full, .lbl-short").count(), 2)

        page.set_viewport_size({"width": 390, "height": 844})
        self.assertTrue(copy.locator(".lbl-short").is_visible())
        self.assertFalse(copy.locator(".lbl-full").is_visible())

    def test_category_flow_nav_tracks_clicked_and_scrolled_desktop_section(self):
        page = self.page_with_styles("""
          <html data-skin="friends" data-theme="light"><body>
            <main class="wrap">
              <nav class="category-flow-nav" aria-label="Этапы настройки подборки">
                <a class="is-active" href="#categoryDates" aria-current="location"><span>1</span> События</a>
                <a href="#categoryVoting"><span>2</span> Правила</a>
                <a href="#categoryShare"><span>3</span> Публикация</a>
                <a href="#categoryAppearance"><span>4</span> Оформление</a>
              </nav>
              <div class="category-editor-sections">
                <section class="card" id="categoryDates" style="height:720px">События</section>
                <section class="card" id="categoryVoting" style="height:720px">Правила</section>
                <section class="card" id="categoryShare" style="height:720px">Публикация</section>
                <section class="card" id="categoryAppearance" style="height:720px">Оформление</section>
              </div>
            </main>
          </body></html>
        """, "static/admin.css")
        page.evaluate("window.UI = {}")
        page.add_script_tag(content=(APP / "static/admin.js").read_text("utf-8"))

        links = page.locator(".category-flow-nav a")
        selected = page.locator('.category-flow-nav a[aria-current="location"]')
        self.assertEqual(selected.get_attribute("href"), "#categoryDates")
        links.nth(1).click()
        self.assertEqual(selected.get_attribute("href"), "#categoryVoting")

        page.locator("#categoryShare").evaluate("node => node.scrollIntoView()")
        page.wait_for_function("""() =>
          document.querySelector('.category-flow-nav a[aria-current="location"]')
            ?.getAttribute('href') === '#categoryShare'
        """)
        selected_style = links.nth(2).evaluate(
            "node => getComputedStyle(node).backgroundImage",
        )
        self.assertNotEqual(selected_style, "none")

        # Если весь редактор помещается на очень высоком экране, начальное
        # положение страницы не является «низом прокрутки»: выбран первый шаг.
        page.set_viewport_size({"width": 1280, "height": 4000})
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_function("""() =>
          document.querySelector('.category-flow-nav a[aria-current="location"]')
            ?.getAttribute('href') === '#categoryDates'
        """)

    def test_category_flow_nav_keeps_clamped_anchor_active(self):
        # У короткой подборки браузер не может довести средний якорь до его
        # scroll-margin: прокрутка упирается в низ страницы. Нижняя эвристика
        # scrollspy не должна из-за этого заменять выбранный шаг «Оформлением».
        markup = """
          <html data-skin="friends" data-theme="light"><body>
            <header style="height:110px">Шапка кабинета</header>
            <main class="wrap">
              <nav class="category-flow-nav" aria-label="Этапы настройки подборки">
                <a class="is-active" href="#categoryDates" aria-current="location"><span>1</span> События</a>
                <a href="#categoryVoting"><span>2</span> Правила</a>
                <a href="#categoryShare"><span>3</span> Публикация</a>
                <a href="#categoryAppearance"><span>4</span> Оформление</a>
              </nav>
              <div class="category-editor-sections">
                <section class="card category-events-card" id="categoryDates" style="height:260px">События</section>
                <section class="card voting-settings" id="categoryVoting" style="height:220px">Правила</section>
                <section class="card category-share-card" id="categoryShare" style="height:110px">Публикация</section>
                <details class="card category-appearance-card" id="categoryAppearance">
                  <summary class="category-appearance-summary">Оформление</summary>
                </details>
              </div>
            </main>
          </body></html>
        """
        cases = (
            ({"width": 1280, "height": 700}, "#categoryVoting"),
            ({"width": 390, "height": 700}, "#categoryShare"),
        )
        for viewport, target in cases:
            with self.subTest(viewport=viewport, target=target):
                page = self.page_with_styles(markup, "static/admin.css")
                page.set_viewport_size(viewport)
                page.evaluate("window.UI = {}")
                page.add_script_tag(content=(APP / "static/admin.js").read_text("utf-8"))

                page.locator(f'a[href="{target}"]').click()
                page.evaluate("""() => new Promise(resolve =>
                  requestAnimationFrame(() => requestAnimationFrame(resolve)))
                """)

                self.assertEqual(page.evaluate("location.hash"), target)
                self.assertEqual(
                    page.locator(
                        '.category-flow-nav a[aria-current="location"]'
                    ).get_attribute("href"),
                    target,
                )

                # Deep-link/Turbo restore и hash-history могут не менять
                # scrollY: несколько нижних секций делят один clamped maxY.
                # Активным всё равно остаётся пункт из URL, а не последний.
                restored = "#categoryShare" if target == "#categoryVoting" else "#categoryVoting"
                page.evaluate("hash => { location.hash = hash; }", restored)
                page.wait_for_function(
                    "hash => document.querySelector("
                    "'.category-flow-nav a[aria-current=\"location\"]'"
                    ")?.getAttribute('href') === hash",
                    arg=restored,
                )
                page.evaluate("hash => { location.hash = hash; }", target)
                page.wait_for_function(
                    "hash => document.querySelector("
                    "'.category-flow-nav a[aria-current=\"location\"]'"
                    ")?.getAttribute('href') === hash",
                    arg=target,
                )

                # Повторная Turbo-инициализация обязана снять старый
                # hashchange-listener и восстановить тот же активный пункт.
                page.locator(".category-flow-nav a").evaluate_all("""links => {
                  links.forEach(link => {
                    link.classList.remove('is-active');
                    link.removeAttribute('aria-current');
                  });
                  const last = links[links.length - 1];
                  last.classList.add('is-active');
                  last.setAttribute('aria-current', 'location');
                }""")
                page.evaluate("document.dispatchEvent(new Event('turbo:load'))")
                self.assertEqual(
                    page.locator(
                        '.category-flow-nav a[aria-current="location"]'
                    ).get_attribute("href"),
                    target,
                )
                page.evaluate("hash => { location.hash = hash; }", restored)
                page.wait_for_function(
                    "hash => document.querySelector("
                    "'.category-flow-nav a[aria-current=\"location\"]'"
                    ")?.getAttribute('href') === hash",
                    arg=restored,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
