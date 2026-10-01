"""Старые ответы профиля и Telegram не прерывают новую страницу кабинета."""

import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


class ProfileNavigationBrowserTests(unittest.TestCase):
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
            self.did = conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,is_public,created_at) "
                "VALUES(?,?,?,1,?)",
                (self.uid, "Собственное событие", "profile-navigation-event", self.backend.main.now_iso()),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.context.add_cookies([cookie])
        self.page = self.context.new_page()
        self.editor_requests = []
        self.page.on("request", self.record_editor_request)
        self.page.goto(self.backend.url + "/admin/profile")
        self.opener = self.page.locator("[data-profile-editor]")
        expect(self.opener).to_have_count(1)
        self.page.wait_for_function("document.querySelector('#profileCollection')._d4yWidgetReady")
        self.page.evaluate("""() => {
          window.profileNavigationOrigin = performance.timeOrigin;
          window.profileNavigationBackground = document.querySelector('#bg-smoke');
        }""")

    def record_editor_request(self, request):
        if request.method == "GET" and urlsplit(request.url).path == f"/admin/dates/{self.did}/edit":
            self.editor_requests.append(request.url)

    def wait_for_held_response(self, held):
        # Синхронный Playwright обрабатывает события во время своих ожиданий.
        deadline = time.monotonic() + 10
        while not held and time.monotonic() < deadline:
            self.page.wait_for_timeout(20)
        self.assertEqual(len(held), 1)

    def hold_save_response(self):
        # Сохранение реально завершается в SQLite, задерживается только ответ
        # браузеру. Поэтому Promise остаётся незавершённым без подмены Turbo.
        self.held_save = []

        def hold(route):
            if route.request.method != "POST":
                route.continue_()
                return
            self.held_save.append((route, route.fetch()))

        self.page.route("**/admin/profile", hold)
        self.page.locator('#profileForm [name="display_name"]').fill("Сохранено перед переходом")
        self.page.wait_for_function("""() => window.d4yProfileSave &&
          document.querySelector('#profileForm').dataset.saving === '1'""")
        self.wait_for_held_response(self.held_save)
        self.assertEqual(self.backend.row(
            "SELECT display_name FROM users WHERE id=?", (self.uid,),
        )["display_name"], "Сохранено перед переходом")
        self.page.evaluate("() => { window.profileNavigationSave = window.d4yProfileSave; }")

    def complete_save(self):
        route, response = self.held_save.pop()
        route.fulfill(response=response)

    def test_double_click_waits_for_save_then_opens_one_editor_without_reload(self):
        self.hold_save_response()
        self.opener.dblclick()
        self.assertEqual(urlsplit(self.page.url).path, "/admin/profile")
        self.assertEqual(self.editor_requests, [])
        expect(self.page.locator("#profileForm")).to_have_attribute("data-saving", "1")

        with self.page.expect_request(
            lambda request: request.method == "GET"
            and urlsplit(request.url).path == f"/admin/dates/{self.did}/edit",
        ):
            self.complete_save()
        self.page.wait_for_url(f"**/admin/dates/{self.did}/edit?**")
        expect(self.page.locator("#edTitle")).to_have_text("Собственное событие")
        self.assertEqual(len(self.editor_requests), 1)
        self.assertTrue(self.page.evaluate("""() =>
          performance.timeOrigin === window.profileNavigationOrigin &&
          document.querySelector('#bg-smoke') === window.profileNavigationBackground
        """))

    def test_completed_save_cannot_open_old_editor_after_leaving_profile(self):
        self.hold_save_response()
        self.opener.click()
        self.page.locator('nav a[href="/admin/dates"]').click()
        self.page.wait_for_url("**/admin/dates")
        expect(self.page.locator("#profileCollection")).to_have_count(0)
        self.complete_save()
        self.page.evaluate("""async () => {
          await window.profileNavigationSave;
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        }""")
        self.assertEqual(self.editor_requests, [])
        self.assertEqual(urlsplit(self.page.url).path, "/admin/dates")
        self.assertTrue(self.page.evaluate("""() =>
          performance.timeOrigin === window.profileNavigationOrigin &&
          document.querySelector('#bg-smoke') === window.profileNavigationBackground
        """))

    def start_telegram_connection(self):
        # Никаких внешних окон и запросов Telegram: проверяем только клиентский
        # lifecycle CTA, настоящий переход кабинета оставляем Turbo.
        self.page.evaluate("""() => {
          window.open = () => ({closed: false, location: {replace() {}}, close() {}});
        }""")
        self.page.route("**/auth/start**", lambda route: route.fulfill(json={
            "code": "navigation-poll", "url": "https://t.me/synthetic_bot?start=navigation-poll",
        }))
        self.page.locator(".telegram-connect-profile [data-tg-connect]").click()

    def test_old_telegram_poll_success_cannot_redirect_after_turbo_navigation(self):
        polls = []
        self.page.route("**/auth/poll**", lambda route: polls.append(route))
        with self.page.expect_request(lambda request: urlsplit(request.url).path == "/auth/poll"):
            self.start_telegram_connection()
        self.wait_for_held_response(polls)
        self.page.locator('nav a[href="/admin/dates"]').click()
        self.page.wait_for_url("**/admin/dates")
        with self.page.expect_response(lambda response: urlsplit(response.url).path == "/auth/poll"):
            polls[0].fulfill(json={"status": "ok", "redirect": "/admin/?msg=old-poll"})
        self.page.evaluate("""async () => {
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        }""")
        self.assertEqual(urlsplit(self.page.url).path, "/admin/dates")
        self.assertTrue(self.page.evaluate("""() =>
          performance.timeOrigin === window.profileNavigationOrigin &&
          document.querySelector('#bg-smoke') === window.profileNavigationBackground
        """))
        # Старый таймер также прекращает поллинг вместо фоновых запросов.
        self.page.wait_for_timeout(2200)
        self.assertEqual(len(polls), 1)

    def test_connected_telegram_poll_keeps_the_success_redirect(self):
        self.page.route("**/auth/poll**", lambda route: route.fulfill(json={
            "status": "ok", "redirect": "/admin/?msg=connected-poll",
        }))
        self.start_telegram_connection()
        self.page.wait_for_url("**/admin/?msg=connected-poll")
        self.assertEqual(urlsplit(self.page.url).path, "/admin/")
        self.assertEqual(urlsplit(self.page.url).query, "msg=connected-poll")


if __name__ == "__main__":
    unittest.main(verbosity=2)
