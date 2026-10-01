"""Поиск по вводу: реальный HTTP, сохранение фокуса, сброс и ошибки."""

import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


class LiveSearchBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = LiveBackend()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.backend.close()

    def setUp(self):
        uid, cookie = self.backend.user_cookie()
        conn = self.backend.db.connect()
        try:
            conn.execute("UPDATE users SET is_operator=1 WHERE id=?", (uid,))
            author = conn.execute(
                "INSERT INTO users(display_name,created_at) VALUES('Другой автор',?)",
                (self.backend.main.now_iso(),),
            ).lastrowid
            for owner in (uid, author):
                for index, name in enumerate(("Кино у реки", "Театр")):
                    conn.execute(
                        "INSERT INTO dates(owner_id,name,share_token,is_public,is_draft,created_at) "
                        "VALUES(?,?,?,1,0,?)",
                        (owner, name, f"search-{owner}-{index}", self.backend.main.now_iso()),
                    )
            for index, name in enumerate(("Кино", "Театр")):
                conn.execute(
                    "INSERT INTO categories(owner_id,name,link_token,created_at) VALUES(?,?,?,?)",
                    (uid, name, f"search-cat-{index}", self.backend.main.now_iso()),
                )
            conn.commit()
        finally:
            conn.close()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.context.add_cookies([cookie])
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def test_desktop_search_reaches_right_edge_of_its_content(self):
        page = self.page
        for width in (1280, 1600):
            page.set_viewport_size({"width": width, "height": 900})
            for path, field, content in (
                ("/admin/dates", ".admin-search-input input", "[data-live-search-results]"),
                ("/admin/categories", ".admin-search-input input", "[data-live-search-results]"),
                ("/admin/", "#communitySearchInput", "#communityFeed"),
                ("/operator/categories", ".search-field input", ".toolbar"),
            ):
                with self.subTest(width=width, path=path):
                    page.goto(self.backend.url + path)
                    expect(page.locator(field)).to_be_visible()
                    edges = page.evaluate('''selectors => {
                        const input = document.querySelector(selectors[0]).getBoundingClientRect();
                        const content = document.querySelector(selectors[1]).getBoundingClientRect();
                        return {input: input.right, content: content.right};
                    }''', [field, content])
                    self.assertAlmostEqual(edges["input"], edges["content"], delta=1)

    def test_events_filter_while_typing_keep_focus_and_reinitialize_card_actions(self):
        page = self.page
        page.goto(self.backend.url + "/admin/dates?f=public")
        search = page.locator('input[type="search"]')
        expect(page.get_by_role("button", name="Найти", exact=True)).to_have_count(0)
        search.fill("К")
        expect(page.locator(".dcard")).to_have_count(1)
        expect(search).to_be_focused()
        expect(search).to_have_value("К")
        self.assertEqual(parse_qs(urlsplit(page.url).query)["f"], ["public"])
        search.fill("Кино ")
        page.wait_for_function("() => new URL(location.href).searchParams.get('q') === 'Кино '")
        expect(search).to_have_value("Кино ")
        self.assertEqual(search.evaluate("el => el.selectionStart"), 5)
        page.locator("[data-bulk-item]").check()
        expect(page.locator("#datesBulkForm")).to_be_visible()
        page.locator("[data-live-search-clear]").click()
        expect(page.locator(".dcard")).to_have_count(2)
        expect(search).to_have_value("")
        expect(search).to_be_focused()
        expect(page.locator("#datesBulkForm")).to_be_hidden()
        self.assertEqual(self.errors, [])

    def test_collections_debounce_and_retry_preserve_input_and_old_results(self):
        page = self.page
        page.goto(self.backend.url + "/admin/categories")
        search = page.locator('input[type="search"]')
        requests = []
        page.on("request", lambda request: requests.append(request.url)
                if "/admin/categories?q=" in request.url else None)
        search.press_sequentially("Кино", delay=35)
        expect(page.locator(".cat-card")).to_have_count(1)
        self.assertEqual(len(requests), 1)
        page.route("**/admin/categories?q=*", lambda route: route.fulfill(status=500, body="error"))
        search.fill("Театр")
        expect(page.locator("[data-live-search-status]")).to_contain_text("Не удалось")
        expect(page.locator(".cat-name")).to_have_text("Кино")
        expect(search).to_have_value("Театр")
        page.unroute("**/admin/categories?q=*")
        search.press("Enter")
        expect(page.locator(".cat-name")).to_have_text("Театр")
        expect(search).to_be_focused()
        self.assertEqual(self.errors, [])

    def test_feed_searches_first_character_and_keeps_space_between_words(self):
        page = self.page
        page.goto(self.backend.url + "/admin/")
        search = page.locator("#communitySearchInput")
        cards = page.locator("#communityFeed .cfeed-card")
        expect(cards).to_have_count(2)
        search.fill("К")
        expect(cards).to_have_count(1)
        expect(cards.first).to_contain_text("Кино у реки")
        search.fill("Кино ")
        page.wait_for_function("() => new URL(location.href).searchParams.get('q') === 'Кино'")
        expect(search).to_have_value("Кино ")
        search.press_sequentially("у")
        page.wait_for_function("() => new URL(location.href).searchParams.get('q') === 'Кино у'")
        expect(cards).to_have_count(1)
        expect(search).to_be_focused()
        page.locator("#communitySearchClear").click()
        expect(cards).to_have_count(2)
        expect(search).to_have_value("")
        self.assertEqual(self.errors, [])

    def test_delayed_collection_response_cannot_replace_a_newer_query(self):
        page = self.page
        # Даже транспорт, игнорирующий abort, не должен вернуть старую выдачу.
        page.add_init_script("""(() => {
          const original = window.fetch;
          window.fetch = function(input, options) {
            if (new URL(String(input), location.href).pathname === '/admin/categories') {
              options = Object.assign({}, options, {signal: undefined});
            }
            return original.call(this, input, options);
          };
        })();""")
        page.goto(self.backend.url + "/admin/categories")
        held = []

        def capture(route):
            response = route.fetch()
            held.append((route, response))
            page.evaluate("document.body.dataset.searchResponseHeld = '1'")

        page.route("**/admin/categories?q=%D0%9A*", capture)
        search = page.locator('input[type="search"]')
        search.fill("Кино")
        expect(page.locator("body")).to_have_attribute("data-search-response-held", "1")
        search.fill("Театр")
        expect(page.locator(".cat-name")).to_have_count(1)
        expect(page.locator(".cat-name")).to_have_text("Театр")
        current_url = page.url
        route, response = held[0]
        with page.expect_response(lambda result: result.url == route.request.url):
            route.fulfill(response=response)
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(page.locator(".cat-name")).to_have_text("Театр")
        expect(search).to_have_value("Театр")
        expect(search).to_be_focused()
        self.assertEqual(search.evaluate("el => el.selectionStart"), 5)
        self.assertEqual(page.url, current_url)
        self.assertEqual(self.errors, [])

    def test_operator_updates_results_without_reloading_or_losing_cursor(self):
        page = self.page
        page.goto(self.backend.url + "/operator/dates?flt=active")
        page.evaluate("window.searchPageMarker = 'unchanged'")
        search = page.locator('input[type="search"]')
        search.fill("Кино")
        expect(page.locator("tbody tr")).to_have_count(2)
        expect(search).to_be_focused()
        self.assertEqual(search.evaluate("el => el.selectionStart"), 4)
        self.assertEqual(page.evaluate("window.searchPageMarker"), "unchanged")
        self.assertEqual(parse_qs(urlsplit(page.url).query)["flt"], ["active"])
        page.locator("[data-live-search-clear]").click()
        expect(page.locator("tbody tr")).to_have_count(4)
        expect(search).to_be_focused()
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
