"""Регрессии нажатия на меню и переключения очередей уведомлений."""

import unittest

from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


class RequestedUiPolishTests(unittest.TestCase):
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
        self.uid = uid
        conn = self.backend.db.connect()
        try:
            conn.execute(
                "INSERT INTO categories(owner_id,name,link_token,choice_mode,"
                "voting_deadline,voting_status,created_at) "
                "VALUES(?,'Вечер вместе','polish-category','single',"
                "'2099-01-01T20:00','open',?)", (uid, self.backend.main.now_iso()),
            )
            owner = conn.execute(
                "INSERT INTO users(telegram_id,display_name,created_at) "
                "VALUES(90002,'Автор ленты',?)", (self.backend.main.now_iso(),),
            ).lastrowid
            conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,is_public,is_draft,created_at) "
                "VALUES(?,'Вечер в музее','polish-date',1,0,?)",
                (owner, self.backend.main.now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.context.add_cookies([cookie])
        self.page = self.context.new_page()

    def test_pressing_collection_menu_animates_only_button(self):
        page = self.page
        page.goto(self.backend.url + "/admin/categories")
        button = page.locator(".cat-card .more")
        expect(button).to_be_visible()
        card = page.locator(".cat-card")
        page.wait_for_timeout(300)
        appearance = "el => { const s=getComputedStyle(el); return [s.transform,s.boxShadow,s.borderColor]; }"
        before = card.evaluate(appearance)
        button.hover()
        page.wait_for_timeout(220)
        self.assertEqual(card.evaluate(appearance), before)
        page.mouse.down()
        page.wait_for_timeout(180)
        try:
            self.assertEqual(card.evaluate(appearance), before)
            self.assertNotEqual(button.evaluate("el => getComputedStyle(el).transform"), "none")
        finally:
            page.mouse.up()
        expect(button).to_have_attribute("aria-expanded", "true")
        self.assertEqual(page.url, self.backend.url + "/admin/categories")

    def test_feed_menu_has_no_native_blue_card_highlight_or_selection(self):
        page = self.page
        page.goto(self.backend.url + "/admin/")
        card = page.locator("#communityFeed .cfeed-card")
        expect(card).to_have_count(1)
        self.assertEqual(card.evaluate("el => getComputedStyle(el).webkitTapHighlightColor"),
                         "rgba(0, 0, 0, 0)")
        card.locator(".more").dblclick()
        self.assertEqual(page.evaluate("window.getSelection().toString()"), "")
        expect(page.locator("#communityDlg")).to_be_hidden()

    def test_collection_menu_and_grid_remain_available_on_touch(self):
        context = self.browser.new_context(
            viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
        )
        self.addCleanup(context.close)
        context.add_cookies(self.context.cookies())
        page = context.new_page()
        page.goto(self.backend.url + "/admin/categories")
        card = page.locator(".cat-card")
        expect(card.locator(".more")).to_be_in_viewport()
        self.assertEqual(card.locator("xpath=..").evaluate(
            "el => getComputedStyle(el).display"), "grid")
        card.locator(".more").tap()
        expect(card.locator(".more")).to_have_attribute("aria-expanded", "true")
        expect(card.locator(".menu.open")).to_be_visible()
        self.assertEqual(page.url, self.backend.url + "/admin/categories")

    def test_notification_return_does_not_replay_cached_indicator(self):
        page = self.page
        page.goto(self.backend.url + "/admin/questions")
        page.evaluate("""() => {
          window.notificationPreviews = 0;
          document.addEventListener('turbo:render', () => {
            if (document.documentElement.hasAttribute('data-turbo-preview')) {
              window.notificationPreviews++;
            }
          });
        }""")
        for target in (1, 0, 1, 0):
            with page.expect_response(lambda response: '/admin/questions' in response.url
                                      and response.request.resource_type == 'fetch'):
                page.locator(".notif-controls .tabs a").nth(target).click()
            expect(page.locator(".notif-controls .tabs a.on")).to_have_text(
                "Ждут отзыва 0" if target else "Ждут ответа 0")
            expect(page.locator(".notif-controls .tabs .tab-ind")).to_have_count(1)
            page.wait_for_timeout(350)
        self.assertEqual(page.evaluate("window.notificationPreviews"), 0)

    def test_notification_head_is_compact_at_mobile_and_desktop_sizes(self):
        page = self.page
        for width in (320, 390, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(self.backend.url + "/admin/questions")
            expect(page.locator(".notif-page-head p")).to_have_count(0)
            self.assertLessEqual(page.evaluate(
                "document.documentElement.scrollWidth - innerWidth"), 1)
            expect(page.locator(".notif-controls .tabs a").nth(1)).to_be_in_viewport()

    def test_preview_select_arrow_keeps_its_inset_in_all_appearances(self):
        conn = self.backend.db.connect()
        try:
            conn.execute(
                "INSERT INTO categories(owner_id,name,link_token,created_at) "
                "VALUES(?,'Другая подборка','polish-other-category',?)",
                (self.uid, self.backend.main.now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
        page = self.page
        page.goto(self.backend.url + "/admin/")
        select = page.locator("#shareCollection")
        expect(select).to_be_visible()
        for skin in ("friends", "romantic"):
            for theme in ("light", "dark"):
                page.locator("html").evaluate(
                    "(el, appearance) => {el.dataset.skin=appearance[0];el.dataset.theme=appearance[1];}",
                    [skin, theme],
                )
                style = select.evaluate("""el => {
                  const s=getComputedStyle(el);
                  return {image:s.backgroundImage, position:s.backgroundPosition, repeat:s.backgroundRepeat};
                }""")
                self.assertIn("data:image/svg+xml", style["image"], (skin, theme))
                self.assertEqual(style["position"], "calc(100% - 12px) 50%")
                self.assertEqual(style["repeat"], "no-repeat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
