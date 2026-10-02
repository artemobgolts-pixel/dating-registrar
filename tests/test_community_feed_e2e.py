"""FLOW-05: Chromium → локальный HTTP → SQLite, сбои пагинации и retry."""

import re
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

    def test_preview_collection_switch_preserves_loaded_feed_and_search(self):
        conn = self.backend.db.connect()
        try:
            collection_ids = []
            for index in range(2):
                collection_ids.append(conn.execute(
                    "INSERT INTO categories(owner_id,name,link_token,link_enabled,created_at) "
                    "VALUES(?,?,?,1,?)",
                    (self.uid, f"Подборка {index}", f"preview-collection-{index}",
                     self.backend.main.now_iso()),
                ).lastrowid)
            conn.commit()
            self.before = list(conn.iterdump())
        finally:
            conn.close()
        self.open_feed("Пикник")
        self.sentinel.scroll_into_view_if_needed()
        expect(self.cards).to_have_count(24)
        self.page.locator("#shareCollection").scroll_into_view_if_needed()
        before_ids = self.card_ids()
        before_cursor = self.sentinel.get_attribute("data-next-cursor")
        self.page.evaluate("""() => {
          window.previewTestFeed = document.getElementById('communityFeed');
          window.previewTestFirstCard = window.previewTestFeed.firstElementChild;
        }""")
        requests = []
        self.page.on("request", lambda request: requests.append(request.url)
                     if urlsplit(request.url).path == "/admin/community" else None)
        selected = self.page.locator("#shareCollection").input_value()
        target = next(cid for cid in collection_ids if str(cid) != selected)
        for collection_id in (target, int(selected), target):
            with self.page.expect_response(lambda response:
                                          urlsplit(response.url).path == "/admin/" and
                                          parse_qs(urlsplit(response.url).query).get("share") == [str(collection_id)]):
                self.page.locator("#shareCollection").select_option(str(collection_id))
            expect(self.page.locator(".dashboard-og-preview .og-img")).to_have_attribute(
                "src", re.compile(rf"/admin/categories/{collection_id}/og-preview\?.*"))
        self.assertTrue(self.page.evaluate("""() =>
          document.getElementById('communityFeed') === window.previewTestFeed &&
          document.getElementById('communityFeed').firstElementChild === window.previewTestFirstCard
        """), "Changing preview must keep the existing feed DOM")
        self.assertEqual(self.card_ids(), before_ids)
        self.assertEqual(self.sentinel.get_attribute("data-next-cursor"), before_cursor)
        expect(self.page.locator("#communitySearchInput")).to_have_value("Пикник")
        self.assertEqual(requests, [])
        expect(self.page.locator("#qrDownload")).to_have_attribute("href", re.compile(r"blob:.*"))
        expect(self.page.locator("#qrShare")).to_have_attribute(
            "data-url", re.compile(rf".*/c/preview-collection-{collection_ids.index(target)}"))
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.locator("#qrToggle").click()
        expect(self.page.locator(".share-qr-col")).to_have_class(re.compile(r".*\bqr-open\b.*"))
        self.page.locator("#qrToggle").click()
        expect(self.page.locator(".share-qr-col")).not_to_have_class(re.compile(r".*\bqr-open\b.*"))
        self.assert_db_unchanged()

    def test_adding_event_removes_original_and_sibling_from_current_search(self):
        # Изолируем сохранение от случайной загрузки следующей страницы при
        # прокрутке к кнопке: оно само должно обновить рекомендации.
        self.page.add_init_script("delete window.IntersectionObserver;")
        root = self.search_ids[0]
        conn = self.backend.db.connect()
        try:
            copier = conn.execute(
                "INSERT INTO users(display_name,created_at) VALUES('Автор копии',?)",
                (self.backend.main.now_iso(),),
            ).lastrowid
            sibling = conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,is_public,is_draft,created_at,origin,source_date_id) "
                "VALUES(?,'Пикник копия','feed-sibling',1,0,?,'copy',?)",
                (copier, self.backend.main.now_iso(), root),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()
        self.open_feed("Пикник")
        saved_card = self.page.locator(f"#communityFeed .cfeed-card[data-widget='{root}']")
        expect(saved_card).to_be_visible()
        with self.page.expect_response(lambda response: response.url.endswith("/add")
                                       and response.request.method == "POST") as response:
            saved_card.locator("[data-community-add]").press("Enter")
        self.assertEqual(response.value.status, 200)
        expect(saved_card).to_have_count(0)
        expect(self.page.locator(f"#communityFeed .cfeed-card[data-widget='{sibling}']")).to_have_count(0)
        expect(self.cards).to_have_count(12)
        expect(self.page.locator("#communitySearchInput")).to_have_value("Пикник")
        copied = self.backend.row("SELECT COUNT(*) AS n FROM dates WHERE owner_id=? AND source_date_id=?",
                                  (self.uid, root))
        self.assertEqual(copied["n"], 1)

    def test_delayed_save_response_preserves_destination_after_turbo_navigation(self):
        conn = self.backend.db.connect()
        try:
            conn.execute(
                "INSERT INTO categories(owner_id,name,link_token,created_at) VALUES(?,'Выходные','saved-late',?)",
                (self.uid, self.backend.main.now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
        self.open_feed("Пикник")
        source_id = self.card_ids()[0]
        held = []

        def delay_save(route):
            held.append((route, route.fetch()))
            self.page.evaluate("document.body.dataset.heldSave = '1'")

        self.page.route("**/d/*/add", delay_save)
        self.cards.first.locator("[data-community-add]").click()
        expect(self.page.locator("body")).to_have_attribute("data-held-save", "1")
        # Turbo сохраняет окружение JS: отложенный ответ старой страницы всё
        # ещё может вызвать callback после замены её DOM.
        self.page.locator("nav.glass-nav a[href='/admin/categories']").click()
        expect(self.page.locator(".cat-card")).to_have_count(1)
        destination = self.page.url
        requests = []
        self.page.on("request", lambda request: requests.append(request.url)
                     if urlsplit(request.url).path == "/admin/community" else None)
        route, response = held[0]
        route.fulfill(response=response)
        expect(self.page.locator("#adminToast")).to_have_text("Событие добавлено в твою коллекцию")
        self.assertEqual(self.page.url, destination)
        self.assertEqual(requests, [], "Detached feed must not reload after a completed save")
        self.assertEqual(self.backend.row(
            "SELECT COUNT(*) AS n FROM dates WHERE owner_id=? AND source_date_id=?",
            (self.uid, source_id),
        )["n"], 1)

    def archive_seen_item_and_finish(self, *, query=""):
        self.open_feed(query)
        initial = self.card_ids()
        archived = initial[0]
        conn = self.backend.db.connect()
        try:
            conn.execute(
                "UPDATE dates SET archived_at=? WHERE id=?",
                (self.backend.main.now_iso(), archived),
            )
            conn.commit()
            self.before = list(conn.iterdump())
        finally:
            conn.close()

        # Ответ приходит от настоящего backend. Возвращаем viewport наверх до
        # его доставки, чтобы новая первая страница не запустила ещё одну
        # автоматическую загрузку до проверки замены устаревших карточек.
        held = self.hold_page(query=query)
        route, response = held[0]
        self.page.evaluate("window.scrollTo(0, 0)")
        with self.page.expect_response(lambda r: r.url == route.request.url) as refreshed:
            route.fulfill(response=response)
        self.assertEqual(refreshed.value.status, 200)
        self.assertEqual(refreshed.value.headers.get("x-feed-reset"), "1")
        expect(self.page.locator("#communityFeed")).to_have_attribute("data-feed-state", "idle")
        expect(self.cards).to_have_count(12)
        expect(self.page.locator(
            f"#communityFeed .cfeed-card[data-widget='{archived}']",
        )).to_have_count(0)
        refreshed_ids = self.card_ids()
        self.assertEqual(len(refreshed_ids), len(set(refreshed_ids)))
        self.assertNotEqual(refreshed_ids, initial)
        expect(self.retry).to_be_hidden()

        expected = [did for did in (self.search_ids if query else self.ids) if did != archived]
        self.finish_feed(expected, search=bool(query))

    def test_ranked_mutation_replaces_stale_page_without_loss_or_duplicates(self):
        self.archive_seen_item_and_finish()

    def test_search_mutation_replaces_stale_page_without_loss_or_duplicates(self):
        self.archive_seen_item_and_finish(query="Пикник")
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
        # Ввод больше не кликает кнопку наверху страницы. Возвращаем viewport
        # сами, чтобы не загрузить корректную вторую страницу новой выдачи.
        self.page.evaluate("window.scrollTo(0, 0)")
        self.page.locator("#communitySearchInput").fill("Пикник")
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
