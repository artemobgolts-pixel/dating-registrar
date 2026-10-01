"""Реальные клики по карточкам: выбор, меню и компактная геометрия."""

import unittest
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend

APP = Path(__file__).resolve().parents[1] / "app"


class MinimalUiBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = LiveBackend()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.backend.close()

    def setUp(self):
        uid, cookie = self.backend.user_cookie()
        conn = self.backend.db.connect()
        try:
            for index in range(2):
                conn.execute(
                    "INSERT INTO dates(owner_id,name,share_token,is_public,is_draft,created_at) "
                    "VALUES(?,?,?,1,0,?)",
                    (uid, f"Событие {index}", f"minimal-{index}", self.backend.main.now_iso()),
                )
            conn.commit()
        finally:
            conn.close()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.context.add_cookies([cookie])
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()

    def test_cards_select_and_reveal_bottom_actions_even_with_old_list_cookie(self):
        self.context.add_cookies([{"name": "layout", "value": "list", "url": self.backend.url + "/admin"}])
        self.page.goto(self.backend.url + "/admin/dates")
        cards = self.page.locator(".dcard")
        expect(cards).to_have_count(2)
        expect(self.page.locator("#viewtog")).to_have_count(0)
        toolbar = self.page.locator("#datesBulkForm")
        expect(toolbar).to_be_hidden()
        self.page.locator("[data-bulk-item]").first.check()
        expect(cards.first).to_have_class(re.compile(r"is-selected"))
        expect(toolbar).to_be_visible()
        self.assertIn("data:image/svg+xml", self.page.locator("[data-bulk-item]").first.evaluate(
            "el => getComputedStyle(el).backgroundImage"))
        self.assertEqual(toolbar.evaluate("el => getComputedStyle(el).position"), "fixed")
        expect(toolbar.locator("[data-bulk-count]")).to_have_text("Выбрано: 1")
        expect(toolbar.locator("button[value=archive]")).to_be_enabled()
        self.assertEqual(toolbar.locator(".dates-bulk-actions button").evaluate_all(
            "buttons => buttons.map(button => button.value)"),
            ["make_public", "archive", "make_private", "delete"])
        self.page.locator("[data-bulk-all]").check()
        expect(toolbar.locator("[data-bulk-count]")).to_have_text("Выбрано: 2")
        self.page.locator("[data-bulk-all]").uncheck()
        expect(toolbar).to_be_hidden()

    def test_tab_numbers_share_text_metrics_and_stay_centered(self):
        page = self.page
        page.goto(self.backend.url + "/admin/dates")
        page.locator(".dates-status-tabs a").evaluate_all('''tabs => {
            tabs.forEach(tab => {
                if (!tab.querySelector('.count-badge')) {
                    const count = document.createElement('span');
                    count.className = 'pill count-badge'; count.textContent = '128';
                    tab.append(count);
                }
            });
        }''')
        for width in (320, 390, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            for skin in ("friends", "romantic"):
                for theme in ("light", "dark"):
                    page.locator("html").evaluate(
                        "(el, values) => {el.dataset.skin=values[0];el.dataset.theme=values[1]}",
                        [skin, theme])
                    metrics = page.locator(".dates-status-tabs .count-badge").first.evaluate('''el => {
                        const parent = el.closest('a'), r = el.getBoundingClientRect(), p = parent.getBoundingClientRect();
                        const text = getComputedStyle(parent), count = getComputedStyle(el);
                        return {offset: r.y+r.height/2-p.y-p.height/2,
                            textFont: text.fontSize, countFont: count.fontSize,
                            textLine: text.lineHeight, countLine: count.lineHeight};
                    }''')
                    self.assertAlmostEqual(metrics["offset"], 0, delta=.6)
                    self.assertEqual(metrics["textFont"], metrics["countFont"])
                    self.assertEqual(metrics["textLine"], metrics["countLine"])
                    self.assertTrue(page.locator(".dates-status-tabs a").evaluate_all('''tabs =>
                        tabs.every(tab => {
                            const count = tab.querySelector('.count-badge').getBoundingClientRect();
                            const link = tab.getBoundingClientRect();
                            return count.left >= link.left && count.right <= link.right;
                        })'''), "Счётчик выходит за границы вкладки")

    def test_short_feed_card_does_not_clip_menu(self):
        page = self.page
        page.goto("about:blank")
        page.set_content('''<html data-skin="romantic" data-theme="dark"><body>
          <article class="cfeed-card" style="width:300px">
            <div class="cfeed-body"><h3 class="cfeed-ttl">Короткая карточка</h3>
              <div class="menu-wrap cfeed-menu cfeed-menu--body">
                <button class="more">⋯</button><div class="menu">
                  <button>Скопировать ссылку</button><button>Пожаловаться</button>
                </div>
              </div><button class="btn">Добавить</button>
            </div>
          </article></body></html>''')
        page.add_style_tag(content=(APP / "static/admin.css").read_text("utf-8"))
        page.add_script_tag(content=(APP / "static/ui.js").read_text("utf-8"))
        page.evaluate("UI.cardMenu(document)")
        page.locator(".more").click()
        last = page.get_by_role("button", name="Пожаловаться", exact=True)
        self.assertTrue(last.evaluate('''el => {
            const r = el.getBoundingClientRect();
            return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
        }'''))

    def test_media_menu_opacity_matches_feed_in_both_skins_and_themes(self):
        page = self.page
        page.goto("about:blank")
        page.set_content('''<html><body>
          <article class="dcard"><div class="menu-wrap card-menu"><button class="more">⋯</button></div></article>
          <article class="cat-card"><div class="cat-card-menu"><button class="more">⋯</button></div></article>
          <article class="cfeed-card"><div class="cfeed-menu cfeed-menu--media"><button class="more">⋯</button></div></article>
        </body></html>''')
        page.add_style_tag(content=(APP / "static/admin.css").read_text("utf-8"))
        page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
        for skin in ("friends", "romantic"):
            for theme in ("light", "dark"):
                page.locator("html").evaluate("(el, values) => {el.dataset.skin=values[0];el.dataset.theme=values[1]}", [skin, theme])
                for expanded in ("false", "true"):
                    page.locator(".more").evaluate_all("(els, value) => els.forEach(el => el.setAttribute('aria-expanded', value))", expanded)
                    backgrounds = page.locator(".more").evaluate_all("els => els.map(el => getComputedStyle(el).backgroundColor)")
                    self.assertEqual(backgrounds, ["rgba(24, 25, 31, 0.38)"] * 3, (skin, theme, expanded))

    def test_menu_last_action_remains_clickable_below_short_card(self):
        page = self.page
        page.goto(self.backend.url + "/admin/dates")
        page.locator(".dcard").first.locator(".more").click()
        menu = page.locator(".menu.open")
        last = menu.locator("button").last
        expect(last).to_be_visible()
        self.assertTrue(last.evaluate("""el => {
            const r = el.getBoundingClientRect();
            return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
        }"""), "Нижний пункт меню перекрыт или обрезан карточкой")

    def test_profile_mobile_image_keeps_feed_proportions(self):
        page = self.page
        page.set_viewport_size({"width": 390, "height": 844})
        page.set_content('<html data-theme="dark" data-skin="friends"><body><section class="pub-dates profile-public-owner"><div class="pub-grid"><a class="pub-card"><div class="ph"></div><div class="cb"><h3 class="t">Публичное событие</h3></div></a></div></section></body></html>')
        for name in ("admin.css", "profile.css"):
            page.add_style_tag(content=(APP / "static" / name).read_text("utf-8"))
        ratio = page.locator(".pub-card .ph").evaluate("el => el.clientWidth / el.clientHeight")
        self.assertAlmostEqual(ratio, 16 / 9, delta=.03)


if __name__ == "__main__":
    unittest.main(verbosity=2)
