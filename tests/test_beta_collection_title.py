"""Ожидание POST-ответа не расходуется на готовность и нативный click."""

import unittest
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend
import test_background_navigation_e2e as navigation


class BetaCollectionTitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = LiveBackend()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, args=navigation.GL_FLAGS)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.backend.close()

    def test_worker_collection_save_waits_for_actionability_before_response_budget(self):
        self.check_worker_collection_save("""button => {
            button.disabled = true;
            setTimeout(() => { button.disabled = false; }, 16000);
        }""")

    def test_worker_collection_save_waits_for_native_click_before_response_budget(self):
        # trial не выполняет обработчики input. Задержка главного потока после
        # mousedown должна расходовать budget click, а не ожидания POST-ответа.
        self.check_worker_collection_save("""button => {
            button.addEventListener('mousedown', () => {
                const until = performance.now() + 16000;
                while (performance.now() < until) {}
            }, {once: true});
        }""")

    def check_worker_collection_save(self, prepare_button):
        uid, cookie = self.backend.user_cookie()
        conn = self.backend.db.connect()
        try:
            conn.execute("UPDATE users SET cursor_effects=1 WHERE id=?", (uid,))
            cid = conn.execute(
                "INSERT INTO categories(owner_id,name,link_token,created_at) VALUES(?,?,?,?)",
                (uid, "Исходное название", "beta-title-synthetic", self.backend.main.now_iso()),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()
        context = self.browser.new_context(viewport={"width": 900, "height": 700})
        self.addCleanup(context.close)
        context.add_cookies([cookie])
        context.add_init_script("(" + navigation.BACKGROUND_PROBE + ")()")
        page = context.new_page()
        page.goto(f"{self.backend.url}/admin/categories/{cid}")
        page.wait_for_function("() => window.__inkStats && window.__inkStats().firstFrameReady")
        self.assertEqual(page.evaluate("window.__inkStats().backend"), "worker")
        page.locator("#categoryAppearance > summary").click()
        page.locator('#categoryEditForm [name="name"]').fill("Название сохранено")
        button = page.locator('button[form="categoryEditForm"][type="submit"]')
        # Настоящие Worker, нативный click, HTTP и SQLite остаются в проверке.
        button.evaluate(prepare_button)
        previous_origin = page.evaluate("performance.timeOrigin")
        previous_inits = page.evaluate("backgroundProbe.inits")
        response = navigation.click_and_wait_response(page, button,
            lambda response: response.request.method == "POST" and
                urlparse(response.url).path == f"/admin/categories/{cid}/rename",
            timeout=navigation.TRANSITION_TIMEOUT)
        self.assertEqual(response.status, 303)
        expect(page.locator("h1")).to_have_text("Название сохранено", timeout=15000)
        self.assertEqual(self.backend.row("SELECT name FROM categories WHERE id=?", (cid,))["name"],
                         "Название сохранено")
        self.assertEqual(page.evaluate("performance.timeOrigin"), previous_origin)
        self.assertEqual(page.evaluate("backgroundProbe.inits"), previous_inits)
        self.assertEqual(page.evaluate("window.__inkStats().backend"), "worker")


if __name__ == "__main__":
    unittest.main(verbosity=2)
