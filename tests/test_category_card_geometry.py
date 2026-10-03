#!/usr/bin/env python3
"""Контракт компактных и доступных карточек подборок."""

from __future__ import annotations

import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


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

    def test_menu_is_anchored_to_the_card_outside_the_preview(self):
        source = (APP / "templates/admin/categories.html").read_text("utf-8")

        media = source.split('<div class="cat-media">', 1)[1].split(
            '</div>', 1,
        )[0]
        self.assertNotIn('class="menu-wrap cat-card-menu"', media)
        self.assertIn('class="cat-thumb"', media)
        self.assertIn('class="menu-wrap cat-card-menu"', source)
        self.assertNotIn("cat-tail", source)

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
        cls.backend = LiveBackend()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.backend.close()

    def setUp(self):
        self.uid, self.cookie = self.backend.user_cookie()
        import images
        # Чисто белый кадр выявляет недостаточное затемнение текста поверх фото.
        Image.new("RGB", (1200, 630), "white").save(
            images.UPLOAD_DIR / "category-white-preview.webp", "WEBP",
        )
        self.long_name = "ПодборкаБезПробелов" * 10
        self.categories = {}
        conn = self.backend.db.connect()
        try:
            for count in (0, 1, 2, 5, 11, 21):
                name = self.long_name if count == 5 else f"Прогулка {count}"
                cid = conn.execute(
                    "INSERT INTO categories(owner_id,name,link_token,og_image,choice_mode,"
                    "voting_deadline,voting_status,created_at) "
                    "VALUES(?,?,?,?,'single','2099-01-01T12:00','open',?)",
                    (self.uid, name, f"category-geometry-{count}",
                     "category-white-preview.webp", self.backend.main.now_iso()),
                ).lastrowid
                self.categories[count] = {"id": cid, "name": name}
                for index in range(count):
                    did = conn.execute(
                        "INSERT INTO dates(owner_id,name,created_at) VALUES(?,?,?)",
                        (self.uid, f"Событие {count}-{index}", self.backend.main.now_iso()),
                    ).lastrowid
                    conn.execute(
                        "INSERT INTO date_categories(date_id,category_id,position) VALUES(?,?,?)",
                        (did, cid, index),
                    )
            # Состав закрытой подборки сначала формируется при открытом голосовании.
            conn.execute(
                "UPDATE categories SET voting_deadline='2026-01-01T12:00', "
                "voting_status='tie',closed_at='2026-01-01T12:00', "
                "link_enabled=0,operator_review_pending=1 WHERE id=?",
                (self.categories[5]["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 900}, reduced_motion="reduce",
        )
        self.addCleanup(self.context.close)
        self.context.add_cookies([self.cookie])
        self.page = self.context.new_page()

    def open_list(self, width=1280, *, enlarged_text=False):
        page = self.page
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(self.backend.url + "/admin/categories")
        expect(page.locator(".cat-card")).to_have_count(6)
        page.evaluate("document.fonts.ready")
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
              .cat-card .cat-preview-count { font-size: 1rem !important; }
            """)
        return page

    def card(self, count=5):
        return self.page.locator(f".cat-card:has(#cat-status-{self.categories[count]['id']})")

    @staticmethod
    def appearance(page, skin, theme):
        page.evaluate("""([skin, theme]) => {
          document.documentElement.dataset.skin = skin;
          document.body.dataset.skin = skin;
          document.documentElement.dataset.theme = theme;
        }""", [skin, theme])

    def assert_text_contrast(self, locator):
        """Проверяем контраст букв с градиентом и тенью на белом фото."""
        locator.evaluate("el => el.scrollIntoView({block:'center',inline:'nearest'})")
        self.assertTrue(locator.evaluate("""el => {
          const r=el.getBoundingClientRect(), card=el.closest('.cat-card');
          return [r.y+r.height/4,r.y+r.height*3/4].every(y =>
            [r.x+r.width/4,r.x+r.width*3/4].every(x => {
              const hit=document.elementFromPoint(x,y);return hit && card.contains(hit);
            }));
        }"""), "Замер текста перекрыт шапкой страницы или другим элементом")
        appearance = locator.evaluate(r"""el => {
          const style = getComputedStyle(el);
          let opacity = 1;
          for (let node = el; node; node = node.parentElement) opacity *= Number(getComputedStyle(node).opacity);
          return {color: style.color.match(/[\d.]+/g).map(Number), opacity};
        }""")
        old_style = locator.get_attribute("style")
        rendered = Image.open(BytesIO(locator.screenshot())).convert("RGB")
        try:
            # Тень остаётся фоном букв: скрываем только сам цвет текста.
            locator.evaluate("el => el.style.setProperty('color','transparent','important')")
            pixels = Image.open(BytesIO(locator.screenshot())).convert("RGB")
        finally:
            locator.evaluate("(el, previous) => previous === null ? el.removeAttribute('style') : el.setAttribute('style', previous)", old_style)
        self.assertEqual(rendered.size, pixels.size)
        color = appearance["color"]
        alpha = (color[3] if len(color) > 3 else 1) * appearance["opacity"]

        def luminance(rgb):
            channels = [v / 255 for v in rgb]
            channels = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
            return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]

        contrasts = []
        for glyph, background in zip(rendered.getdata(), pixels.getdata()):
            foreground = [color[i] * alpha + background[i] * (1 - alpha) for i in range(3)]
            # Берём непрозрачные пиксели букв, исключая сглаженные края и пробелы.
            if max(abs(glyph[i] - foreground[i]) for i in range(3)) <= 3 and max(
                abs(glyph[i] - background[i]) for i in range(3)
            ) > 20:
                a, b = sorted((luminance(background), luminance(foreground)))
                contrasts.append((b + .05) / (a + .05))
        self.assertTrue(contrasts, "На снимке не найдены непрозрачные пиксели букв")
        self.assertGreaterEqual(min(contrasts), 4.5)

    def test_photo_overlay_wraps_all_text_at_each_size_and_appearance(self):
        for enlarged in (False, True):
            for width in (320, 390, 900, 1280):
                page = self.open_list(width, enlarged_text=enlarged)
                card = self.card()
                card.scroll_into_view_if_needed()
                page.wait_for_function("el => el.complete && el.naturalWidth > 0", arg=card.locator("img").element_handle())
                expect(card.locator(".cat-name")).to_have_text(self.long_name)
                expect(card.locator(".cat-status-row .entity-status")).to_have_count(4)
                for skin in ("friends", "romantic"):
                    for theme in ("light", "dark"):
                        with self.subTest(width=width, enlarged=enlarged, skin=skin, theme=theme):
                            self.appearance(page, skin, theme)
                            card.locator(".more").evaluate("el => el.scrollIntoView({block:'center',inline:'nearest'})")
                            geometry = card.evaluate("""el => {
                              const rect = node => {const r=node.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height}};
                              const media=el.querySelector('.cat-media'), body=el.querySelector('.cat-body');
                              const name=el.querySelector('.cat-name'), button=el.querySelector('.more');
                              const range=document.createRange();range.selectNodeContents(name);
                              return {overflow:document.documentElement.scrollWidth-innerWidth,
                                card:rect(el),media:rect(media),image:rect(el.querySelector('.cat-thumb')),
                                body:rect(body),name:rect(name),count:rect(el.querySelector('.cat-preview-count')),
                                status:rect(el.querySelector('.cat-status-row')),menu:rect(button),
                                titleLines:[...range.getClientRects()].map(r=>({left:r.left,right:r.right,top:r.top,bottom:r.bottom})),
                                textOverflow:Math.max(name.scrollHeight-name.clientHeight,body.scrollHeight-body.clientHeight),
                                interactive:button.contains(document.elementFromPoint(rect(button).left+22,rect(button).top+22)),
                                overlay:media.contains(body), gridColumns:getComputedStyle(el.parentElement).gridTemplateColumns.split(' ').length};
                            }""")
                            self.assertLessEqual(geometry["overflow"], 1)
                            self.assertTrue(geometry["overlay"])
                            self.assertLessEqual(geometry["textOverflow"], 1)
                            media = geometry["media"]
                            for key in ("body", "name", "status", "count", "menu"):
                                box = geometry[key]
                                self.assertGreaterEqual(box["left"], media["left"] - 1)
                                self.assertLessEqual(box["right"], media["right"] + 1)
                                self.assertGreaterEqual(box["top"], media["top"] - 1)
                                self.assertLessEqual(box["bottom"], media["bottom"] + 1)
                            for line in geometry["titleLines"]:
                                self.assertLessEqual(line["right"], geometry["body"]["right"] + 1)
                                self.assertGreaterEqual(line["top"], geometry["body"]["top"] - 1)
                                self.assertLessEqual(line["bottom"], geometry["body"]["bottom"] + 1)
                            self.assertGreaterEqual(geometry["name"]["top"], geometry["menu"]["bottom"] + 2)
                            self.assertLessEqual(geometry["count"]["right"], geometry["menu"]["left"] - 4)
                            self.assertGreaterEqual(geometry["menu"]["width"], 44)
                            self.assertGreaterEqual(geometry["menu"]["height"], 44)
                            self.assertTrue(geometry["interactive"])
                            self.assertAlmostEqual(geometry["image"]["width"], media["width"], delta=2)
                            self.assertAlmostEqual(geometry["image"]["height"], media["height"], delta=2)
                            self.assertEqual(geometry["gridColumns"] == 1, width < 900)
                            self.assert_text_contrast(card.locator(".cat-name"))
                            self.assert_text_contrast(card.locator(".cat-count-number"))
                            self.assert_text_contrast(card.locator(".cat-count-label"))
                            self.assert_text_contrast(card.locator(".cat-status-row .entity-status > span:last-child").first)

    def test_default_preview_is_unshaded_for_explicit_and_automatic_choices(self):
        conn = self.backend.db.connect()
        try:
            conn.execute("UPDATE categories SET og_image=NULL WHERE id=?", (self.categories[0]["id"],))
            conn.execute("UPDATE categories SET use_default_preview=1 WHERE id=?", (self.categories[1]["id"],))
            conn.execute("UPDATE categories SET og_image='missing-preview.webp' WHERE id=?", (self.categories[2]["id"],))
            conn.execute("UPDATE categories SET og_image=NULL WHERE id=?", (self.categories[11]["id"],))
            conn.execute(
                "INSERT INTO date_images(date_id,filename,position) "
                "SELECT date_id,'category-white-preview.webp',0 FROM date_categories "
                "WHERE category_id=? ORDER BY position LIMIT 1",
                (self.categories[11]["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        for width in (320, 390, 1280):
            page = self.open_list(width)
            for skin in ("friends", "romantic"):
                for theme in ("light", "dark"):
                    with self.subTest(width=width, skin=skin, theme=theme):
                        self.appearance(page, skin, theme)
                        for count in (0, 1, 2, 11, 21):
                            expect(self.card(count)).to_have_attribute(
                                "data-default-image", "1" if count in (0, 1, 2) else "0",
                            )
                        card = self.card(0)
                        card.scroll_into_view_if_needed()
                        page.wait_for_function("el => el.complete && el.naturalWidth > 0", arg=card.locator("img").element_handle())
                        original = Image.open(BytesIO(card.locator(".cat-media").screenshot())).convert("RGB")
                        card.evaluate("el => el.dataset.shadingProbe = 'true'")
                        probe = page.add_style_tag(content="""
                          [data-shading-probe] .cat-media::after,
                          [data-shading-probe] .cat-body::before { display:none !important; }
                          [data-shading-probe] .cat-media,
                          [data-shading-probe] .cat-body { background:transparent !important; }
                          [data-shading-probe] .cat-thumb { filter:none !important; }
                        """)
                        try:
                            unshaded = Image.open(BytesIO(card.locator(".cat-media").screenshot())).convert("RGB")
                        finally:
                            probe.evaluate("el => el.remove()")
                            card.evaluate("el => delete el.dataset.shadingProbe")
                        self.assertIsNone(ImageChops.difference(original, unshaded).getbbox(), "Стандартное превью затемнено")

    def test_preview_counts_use_actual_event_totals_and_russian_plural(self):
        self.open_list()
        endings = {0: "событий", 1: "событие", 2: "события", 5: "событий", 11: "событий", 21: "событие"}
        for count, ending in endings.items():
            with self.subTest(count=count):
                label = self.card(count).locator(".cat-preview-count")
                expect(label).to_have_text(f"{count} {ending}")
                geometry = label.evaluate("""el => {
                  const style=getComputedStyle(el);
                  return {insidePhoto:!!el.closest('.cat-media'),background:style.backgroundColor,
                    border:parseFloat(style.borderTopWidth),shadow:style.boxShadow};
                }""")
                self.assertTrue(geometry["insidePhoto"])
                self.assertIn(geometry["background"], ("rgba(0, 0, 0, 0)", "transparent"))
                self.assertEqual(geometry["border"], 0)
                self.assertEqual(geometry["shadow"], "none")

    def test_last_menu_item_is_visible_clickable_and_title_opens_editor(self):
        for width in (320, 1280):
            with self.subTest(width=width):
                page = self.open_list(width)
                card = self.card(1)
                card.scroll_into_view_if_needed()
                card.locator(".more").click()
                expect(card.locator(".more")).to_have_attribute("aria-expanded", "true")
                last = card.get_by_role("button", name="Удалить подборку", exact=True)
                expect(last).to_be_visible()
                last.scroll_into_view_if_needed()
                hit = last.evaluate("""el => {const r=el.getBoundingClientRect();return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}""")
                self.assertTrue(hit, "Последний пункт меню обрезан карточкой или перекрыт ссылкой")
                last.click()
                expect(page.get_by_role("alertdialog")).to_be_visible()
                page.get_by_role("button", name="Отмена", exact=True).click()
                expect(page.get_by_role("alertdialog")).to_be_hidden()
                self.assertIsNotNone(self.backend.row("SELECT id FROM categories WHERE id=?", (self.categories[1]["id"],)))
                page.keyboard.press("Escape")
                title = card.locator(".cat-name")
                title.scroll_into_view_if_needed()
                bounds = title.bounding_box()
                page.mouse.click(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
                expected_url = self.backend.url + f"/admin/categories/{self.categories[1]['id']}?"
                page.wait_for_url(lambda url: url.startswith(expected_url))
                expect(page.locator("#categoryVotingForm")).to_be_visible()

    def test_live_search_returns_matching_preview_and_preserves_card_actions(self):
        page = self.open_list(390)
        search = page.get_by_role("searchbox", name="Поиск по подборкам")
        search.fill("Прогулка 21")
        expect(page.locator(".cat-card")).to_have_count(1)
        expect(page.locator(".cat-name")).to_have_text("Прогулка 21")
        expect(page.locator(".cat-preview-count")).to_have_text("21 событие")
        card = self.card(21)
        card.locator(".more").click()
        expect(card.get_by_role("button", name="Отключить ссылку", exact=True)).to_be_visible()
        card.get_by_role("button", name="Отключить ссылку", exact=True).click()
        expect(page.locator("#categoryVotingForm")).to_be_visible()
        page.get_by_role("link", name="← Назад", exact=True).click()
        expect(page.locator(".cat-card")).to_have_count(1)
        expect(page.locator(".cat-status-row")).to_contain_text("Ссылка выключена")
        expect(page.get_by_role("searchbox", name="Поиск по подборкам")).to_have_value("Прогулка 21")
        self.assertEqual(self.backend.row("SELECT link_enabled FROM categories WHERE id=?", (self.categories[21]["id"],))["link_enabled"], 0)
        page.get_by_role("link", name="Очистить поиск", exact=True).click()
        expect(page.locator(".cat-card")).to_have_count(6)


if __name__ == "__main__":
    unittest.main()
