"""A11Y-01: настоящая лента, native-контролы, клавиатура, touch и axe."""

import json
from pathlib import Path
import unittest

from PIL import Image
from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


AXE = Path(__file__).resolve().parent / "vendor" / "axe.min.js"


class FeedAccessibilityTests(unittest.TestCase):
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
        conn = self.backend.db.connect()
        try:
            conn.execute("DELETE FROM reports")
            self.owner = conn.execute(
                "INSERT INTO users(telegram_id,display_name,created_at) VALUES(90002,'Автор ленты',?)",
                (self.backend.main.now_iso(),),
            ).lastrowid
            self.ids = []
            for media in (False, True):
                did = conn.execute(
                    "INSERT INTO dates(owner_id,name,place,comment,share_token,is_public,is_draft,created_at) "
                    "VALUES(?,?,?,?,?,1,0,?)",
                    (self.owner, "Событие с фото" if media else "Событие без фото", "Место проверки",
                     f"[Описание маршрута]({self.backend.url}/u/{self.owner}?from=rich)",
                     f"a11y-feed-{int(media)}", self.backend.main.now_iso()),
                ).lastrowid
                self.ids.append(did)
                if media:
                    directory = self.backend.main.images.UPLOAD_DIR
                    directory.mkdir(parents=True, exist_ok=True)
                    Image.new("RGB", (320, 180), (100, 110, 160)).save(directory / "a11y-feed.webp")
                    conn.execute(
                        "INSERT INTO date_images(date_id,filename,position) VALUES(?,'a11y-feed.webp',0)",
                        (did,),
                    )
            conn.commit()
            self.source = [dict(row) for row in conn.execute(
                "SELECT * FROM dates WHERE owner_id=? ORDER BY id", (self.owner,),
            )]
        finally:
            conn.close()
        self.errors = []
        self.widget_requests = []
        self.context = self.new_context()
        self.page = self.context.new_page()
        self.watch_page(self.page)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def new_context(self, *, touch=False, skin="friends", theme="light"):
        context = self.browser.new_context(
            viewport={"width": 390 if touch else 1280, "height": 900},
            is_mobile=touch, has_touch=touch, reduced_motion="reduce",
        )
        self.addCleanup(context.close)
        context.add_cookies([
            self.cookie, {"name": "d4y_theme", "value": theme, "url": self.backend.url},
        ])
        context.add_init_script("""
          window.testCopies = [];
          window.testShares = [];
          Object.defineProperty(navigator, 'clipboard', {configurable: true,
            value: {writeText: async text => { window.testCopies.push(text); }}});
          Object.defineProperty(navigator, 'share', {configurable: true,
            value: async value => { window.testShares.push(value); }});
        """)
        conn = self.backend.db.connect()
        try:
            conn.execute("UPDATE users SET admin_skin=? WHERE id=?", (skin, self.uid))
            conn.commit()
        finally:
            conn.close()
        return context

    def watch_page(self, page):
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("request", lambda request: self.widget_requests.append(request.url)
                if "/admin/community/date/" in request.url else None)

    def open_feed(self, page=None):
        page = page or self.page
        page.goto(self.backend.url + "/admin/")
        expect(page.locator("#communityFeed .cfeed-card")).to_have_count(2)

    def card(self, did, page=None):
        return (page or self.page).locator(f'#communityFeed .cfeed-card[data-widget="{did}"]')

    def assert_widget_open(self, did, page=None):
        page = page or self.page
        expect(page.locator("#communityDlg")).to_be_visible()
        expect(page.locator("#cwidBody .cwid-ttl")).to_have_text(
            "Событие с фото" if did == self.ids[1] else "Событие без фото",
        )

    def close_widget(self, page=None):
        page = page or self.page
        page.locator("#cwidClose").click()
        expect(page.locator("#communityDlg")).to_be_hidden()

    def assert_no_widget(self, count, page=None):
        page = page or self.page
        expect(page.locator("#communityDlg")).to_be_hidden()
        self.assertEqual(len(self.widget_requests), count)

    def assert_sources_unchanged(self):
        conn = self.backend.db.connect()
        try:
            self.assertEqual([dict(row) for row in conn.execute(
                "SELECT * FROM dates WHERE owner_id=? ORDER BY id", (self.owner,),
            )], self.source)
        finally:
            conn.close()

    def test_axe_nested_interactive_both_skins_and_themes(self):
        for skin in ("friends", "romantic"):
            for theme in ("light", "dark"):
                with self.subTest(skin=skin, theme=theme):
                    context = self.new_context(skin=skin, theme=theme)
                    page = context.new_page()
                    self.watch_page(page)
                    self.open_feed(page)
                    expect(page.locator("html")).to_have_attribute("data-skin", skin)
                    expect(page.locator("html")).to_have_attribute("data-theme", theme)
                    page.evaluate(AXE.read_text(encoding="utf-8"))
                    result = page.evaluate("""async () => {
                      const result = await axe.run('#communityFeed', {
                        runOnly: {type: 'rule', values: ['nested-interactive']}
                      });
                      return {violations: result.violations, incomplete: result.incomplete};
                    }""")
                    self.assertEqual(result, {"violations": [], "incomplete": []},
                                     json.dumps(result, ensure_ascii=False))
                    context.close()

    def test_native_details_and_noninteractive_container(self):
        self.open_feed()
        for did in self.ids:
            with self.subTest(did=did):
                card = self.card(did)
                self.assertIsNone(card.get_attribute("role"))
                self.assertIsNone(card.get_attribute("tabindex"))
                details = card.locator("[data-community-open]")
                expect(details).to_have_count(1)
                self.assertEqual(details.evaluate("el => el.tagName"), "BUTTON")
                expect(details).to_have_attribute("type", "button")
                expect(details).to_have_accessible_name(
                    "Открыть «" + card.locator(".cfeed-ttl").inner_text() + "»",
                )
                details.click()
                self.assert_widget_open(did)
                self.close_widget()

    def test_mouse_background_opens_each_card(self):
        self.open_feed()
        for did in self.ids:
            with self.subTest(did=did):
                self.card(did).locator(".cfeed-meta").click()
                self.assert_widget_open(did)
                self.close_widget()
        self.assertEqual(len(self.widget_requests), 2)
        self.assert_sources_unchanged()

    def test_enter_and_space_open_native_details_once(self):
        self.open_feed()
        for did in self.ids:
            for key in ("Enter", "Space"):
                with self.subTest(did=did, key=key):
                    details = self.card(did).locator("[data-community-open]")
                    details.focus()
                    count = len(self.widget_requests)
                    details.press(key)
                    self.assert_widget_open(did)
                    self.assertEqual(len(self.widget_requests), count + 1)
                    self.close_widget()
                    expect(details).to_be_focused()

    def test_author_link_navigates_without_card_activation(self):
        for did in self.ids:
            with self.subTest(did=did):
                self.open_feed()
                count = len(self.widget_requests)
                self.card(did).locator(".cfeed-owner").click()
                expect(self.page).to_have_url(self.backend.url + f"/u/{self.owner}")
                self.assertEqual(len(self.widget_requests), count)
        self.assert_sources_unchanged()

    def test_rich_comment_link_keeps_native_navigation(self):
        for did in self.ids:
            with self.subTest(did=did):
                self.open_feed()
                link = self.card(did).locator(".cfeed-desc a")
                count = len(self.widget_requests)
                with self.page.expect_popup() as popup_info:
                    link.click()
                popup = popup_info.value
                expect(popup).to_have_url(self.backend.url + f"/u/{self.owner}?from=rich")
                popup.close()
                self.assert_no_widget(count)
        self.assert_sources_unchanged()

    def test_independent_actions_preserve_source_and_persist_copy_and_report(self):
        self.open_feed()
        for did in self.ids:
            with self.subTest(did=did):
                card = self.card(did)
                count = len(self.widget_requests)
                share = card.locator("[data-community-share]")
                expected_url = share.get_attribute("data-share-url")
                copies = self.page.evaluate("testCopies.length")
                share.click()
                self.page.wait_for_function("n => testCopies.length === n + 1", arg=copies)
                self.assertEqual(self.page.evaluate("testCopies.at(-1)"), expected_url)
                self.assert_no_widget(count)
                more = card.locator(".more")
                more.click()
                expect(more).to_have_attribute("aria-expanded", "true")
                card.locator("[data-copy]").click()
                self.page.wait_for_function("n => testCopies.length === n + 2", arg=copies)
                self.assertEqual(self.page.evaluate("testCopies.at(-1)"), expected_url)
                self.assert_no_widget(count)
                card.locator("[data-community-report]").click()
                expect(self.page.locator("#communityReportReason")).to_be_focused()
                self.assert_no_widget(count)
                self.page.locator("#communityReportReason").fill("Проверка независимой жалобы")
                with self.page.expect_response(lambda response: response.url.endswith("/report")
                                               and response.request.method == "POST") as response:
                    self.page.locator("#communityReportForm [type=submit]").click()
                self.assertEqual(response.value.status, 200)
                expect(self.page.locator("#communityReportDlg")).to_be_hidden()
                expect(more).to_be_focused()
                report = self.backend.row("SELECT * FROM reports WHERE target_id=?", (did,))
                self.assertEqual((report["reason"], report["status"]),
                                 ("Проверка независимой жалобы", "open"))
                name = card.locator(".cfeed-ttl").inner_text()
                add = card.locator("[data-community-add]")
                add.press("Enter")
                expect(card).to_have_count(0)
                expect(self.page.locator("#communityFeed")).not_to_have_attribute("data-feed-state", "loading")
                self.assert_no_widget(count)
                copied = self.backend.row("SELECT * FROM dates WHERE owner_id=? AND name=?",
                                          (self.uid, name))
                self.assertIsNotNone(copied)
                self.assertNotEqual(copied["id"], did)
                media = self.backend.row(
                    "SELECT filename FROM date_images WHERE date_id=?", (copied["id"],),
                )
                if did == self.ids[1]:
                    self.assertIsNotNone(media)
                    self.assertTrue((self.backend.main.images.UPLOAD_DIR / media["filename"]).is_file())
                else:
                    self.assertIsNone(media)
        self.assert_sources_unchanged()
        self.assertEqual(self.backend.row("SELECT COUNT(*) AS n FROM dates WHERE owner_id=?", (self.uid,))["n"], 2)

    def test_focus_order_and_menu_keyboard_actions(self):
        self.open_feed()
        for did in self.ids:
            with self.subTest(did=did):
                card = self.card(did)
                more = card.locator(".more")
                more.focus()
                for selector in ("[data-community-open]", ".cfeed-owner", ".cfeed-desc a",
                                 "[data-community-add]", "[data-community-share]"):
                    self.page.keyboard.press("Tab")
                    expect(card.locator(selector)).to_be_focused()
                count = len(self.widget_requests)
                more.focus()
                more.press("Space")
                expect(more).to_have_attribute("aria-expanded", "true")
                self.page.keyboard.press("Tab")
                expect(card.locator("[data-copy]")).to_be_focused()
                self.page.keyboard.press("Tab")
                expect(card.locator("[data-community-report]")).to_be_focused()
                self.page.keyboard.press("Enter")
                expect(self.page.locator("#communityReportReason")).to_be_focused()
                self.page.locator("#communityReportCancel").click()
                expect(more).to_be_focused()
                self.assert_no_widget(count)

    def test_mobile_touch_open_and_independent_share(self):
        context = self.new_context(touch=True)
        page = context.new_page()
        self.watch_page(page)
        self.open_feed(page)
        for did in self.ids:
            with self.subTest(did=did):
                card = self.card(did, page)
                card.locator(".cfeed-meta").tap()
                self.assert_widget_open(did, page)
                self.close_widget(page)
                card.locator("[data-community-open]").tap()
                self.assert_widget_open(did, page)
                self.close_widget(page)
                count = len(self.widget_requests)
                shares = page.evaluate("testShares.length")
                share = card.locator("[data-community-share]")
                share.tap()
                page.wait_for_function("n => testShares.length === n + 1", arg=shares)
                self.assertEqual(page.evaluate("testShares.at(-1).url"),
                                 share.get_attribute("data-share-url"))
                self.assert_no_widget(count, page)


if __name__ == "__main__":
    unittest.main()
