"""FLOW-05: Chromium → локальный HTTP → SQLite, сбои пагинации и retry."""

import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


class CommunityFeedBrowserTests(unittest.TestCase):
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
        self.uid, cookie = self.backend.user_cookie()
        conn = self.backend.db.connect()
        try:
            owner = conn.execute(
                "INSERT INTO users(telegram_id,display_name,created_at) VALUES(90002,'Автор',?)",
                (self.backend.main.now_iso(),),
            ).lastrowid
            self.ids, self.search_ids = [], []
            for index in range(37):
                name = ("Пикник" if index < 25 else "Театр") + f" {index:02}"
                did = conn.execute(
                    "INSERT INTO dates(owner_id,name,share_token,is_public,is_draft,created_at) "
                    "VALUES(?,?,?,1,0,?)",
                    (owner, name, f"feed-{index}", self.backend.main.now_iso()),
                ).lastrowid
                self.ids.append(did)
                if index < 25:
                    self.search_ids.append(did)
            conn.commit()
            self.before = list(conn.iterdump())
        finally:
            conn.close()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.context.add_cookies([cookie])
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.cards = self.page.locator("#communityFeed .cfeed-card")
        self.sentinel = self.page.locator("#communityFeed .cfeed-sentinel")
        self.retry = self.page.get_by_role("button", name="Попробовать снова", exact=True)

    def open_feed(self, query=""):
        self.page.goto(self.backend.url + "/admin/" + ("?q=" + query if query else ""))
        expect(self.cards).to_have_count(12)

    def card_ids(self):
        return self.cards.evaluate_all("cards => cards.map(card => Number(card.dataset.widget))")

    def assert_db_unchanged(self):
        conn = self.backend.db.connect()
        try:
            self.assertEqual(list(conn.iterdump()), self.before)
        finally:
            conn.close()

    def finish_feed(self, expected, *, search=False):
        while self.cards.count() < len(expected):
            count = self.cards.count()
            self.sentinel.scroll_into_view_if_needed()
            expect(self.cards).to_have_count(min(count + 12, len(expected)))
        expect(self.page.locator("#communityFeed")).to_have_attribute("data-feed-state", "end")
        expect(self.sentinel).to_have_count(0)
        expect(self.retry).to_be_hidden()
        ids = self.card_ids()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), set(expected))
        if not search:
            expect(self.page.locator("#cfeedEnd")).to_be_visible()
        self.assert_db_unchanged()

    def fail_and_retry(self, failure, *, query=""):
        self.open_feed(query)
        initial = self.card_ids()
        cursor = self.sentinel.get_attribute("data-next-cursor")
        urls = []

        def fail(route):
            urls.append(route.request.url)
            if failure == "abort":
                route.abort("failed")
            else:
                route.fulfill(status=500, body="Synthetic failure")

        self.page.route("**/admin/community?*", fail)
        self.sentinel.scroll_into_view_if_needed()
        expect(self.retry).to_be_visible()
        expect(self.page.locator("#cfeedError")).to_contain_text("Не удалось загрузить события")
        expect(self.page.locator("#communityFeed")).to_have_attribute("data-feed-state", "error")
        expect(self.page.locator("#communityFeed")).to_have_attribute("aria-busy", "false")
        expect(self.page.locator("#cfeedEnd")).to_be_hidden()
        self.assertEqual(self.card_ids(), initial)
        self.assertEqual(self.sentinel.get_attribute("data-next-cursor"), cursor)
        self.assertEqual(len(urls), 1)
        self.page.unroute("**/admin/community?*", fail)
        with self.page.expect_response(lambda r: urlsplit(r.url).path == "/admin/community") as retried:
            self.retry.click()
        self.assertEqual(retried.value.status, 200)
        self.assertEqual(retried.value.url, urls[0])
        expect(self.cards).to_have_count(24)
        self.assertEqual(self.card_ids()[:12], initial)
        expect(self.retry).to_be_hidden()
        self.finish_feed(self.search_ids if query else self.ids, search=bool(query))

    def test_http_500_preserves_page_and_retries_without_loss_or_duplicates(self):
        self.fail_and_retry("500")

    def test_network_abort_recovers_on_mobile_romantic(self):
        conn = self.backend.db.connect()
        try:
            conn.execute("UPDATE users SET admin_skin='romantic' WHERE id=?", (self.uid,))
            conn.commit()
            self.before = list(conn.iterdump())
        finally:
            conn.close()
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.context.add_cookies([{"name": "d4y_theme", "value": "dark", "url": self.backend.url}])
        self.fail_and_retry("abort")
        expect(self.page.locator("html")).to_have_attribute("data-skin", "romantic")
        expect(self.page.locator("html")).to_have_attribute("data-theme", "dark")

    def test_search_pagination_failure_retries_current_query_and_cursor(self):
        self.fail_and_retry("500", query="Пикник")
        expect(self.page.locator("#communitySearchStatus")).to_contain_text("Результаты по запросу «Пикник»")

    def test_first_page_network_failure_retries_without_false_empty_state(self):
        self.page.route("**/admin/community", lambda route: route.abort("failed"))
        self.page.goto(self.backend.url + "/admin/")
        expect(self.retry).to_be_visible()
        expect(self.cards).to_have_count(0)
        expect(self.page.locator("#cfeedEmpty")).to_be_hidden()
        expect(self.page.locator("#cfeedEnd")).to_be_hidden()
        self.page.unroute("**/admin/community")
        self.retry.click()
        expect(self.cards).to_have_count(12)
        self.finish_feed(self.ids)

    def hold_page(self, *, query):
        held = []

        def capture(route):
            parameters = parse_qs(urlsplit(route.request.url).query)
            if parameters.get("cursor") and parameters.get("q", [""])[0] == query and not held:
                response = route.fetch()
                held.append((route, response))
                self.page.evaluate("document.body.setAttribute('data-test-held-feed', '1')")
            else:
                route.continue_()

        self.page.route("**/admin/community?*", capture)
        self.sentinel.scroll_into_view_if_needed()
        expect(self.page.locator("body")).to_have_attribute("data-test-held-feed", "1")
        expect(self.page.locator("#communityFeed")).to_have_attribute("data-feed-state", "loading")
        return held

    def ignore_transport_abort(self):
        # Принудительно задержанный transport игнорирует abort: проверяем
        # generation guard даже если старый реальный ответ всё-таки пришёл.
        self.page.add_init_script("""(() => {
          const original = window.fetch;
          window.fetch = function(input, options) {
            if (String(input).startsWith('/admin/community')) {
              options = Object.assign({}, options, {signal: undefined});
            }
            return original.call(this, input, options);
          };
        })();""")

    def test_delayed_normal_page_cannot_append_to_search(self):
        self.ignore_transport_abort()
        self.open_feed()
        held = self.hold_page(query="")
        self.page.locator("#communitySearchInput").fill("Пикник")
        self.page.get_by_role("button", name="Найти", exact=True).click()
        expect(self.cards).to_have_count(12)
        expect(self.page.locator("#communitySearchStatus")).to_contain_text("Результаты")
        search_first = self.card_ids()
        route, response = held[0]
        with self.page.expect_response(lambda r: r.url == route.request.url):
            route.fulfill(response=response)
        # Следующий browser task выполняется после then/finally старого fetch.
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        self.assertEqual(self.card_ids(), search_first)
        expect(self.retry).to_be_hidden()
        self.finish_feed(self.search_ids, search=True)

    def test_delayed_search_failure_cannot_break_reset_normal_feed(self):
        self.ignore_transport_abort()
        self.open_feed("Пикник")
        held = self.hold_page(query="Пикник")
        self.page.get_by_role("button", name="Сбросить поиск", exact=True).click()
        expect(self.cards).to_have_count(12)
        expect(self.page.locator("#communitySearchStatus")).to_be_hidden()
        normal_first = self.card_ids()
        route, _ = held[0]
        with self.page.expect_response(lambda r: r.url == route.request.url):
            route.fulfill(status=500, body="Delayed synthetic failure")
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        self.assertEqual(self.card_ids(), normal_first)
        expect(self.retry).to_be_hidden()
        self.finish_feed(self.ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
