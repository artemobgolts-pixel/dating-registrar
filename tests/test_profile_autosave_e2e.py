"""FLOW-03: Chromium → локальный FastAPI → настоящая SQLite."""

import unittest

from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


class ProfileAutosaveBrowserTests(unittest.TestCase):
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
        self.context = self.browser.new_context()
        self.context.add_cookies([cookie])
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.goto(self.backend.url + "/admin/profile")
        self.name = self.page.locator('#profileForm [name="display_name"]')
        self.birth = self.page.locator('#profileForm [name="birth_date"]')
        self.note = self.page.locator("#autosaveNote")
        self.form = self.page.locator("#profileForm")

    def row(self):
        return self.backend.row("SELECT display_name,birth_date FROM users WHERE id=?", (self.uid,))

    @staticmethod
    def is_save(response):
        return response.url.endswith("/admin/profile") and response.request.method == "POST"

    def test_valid_save_validation_failure_and_correction(self):
        with self.page.expect_response(self.is_save) as saved:
            self.name.fill("Сохранённое имя")
        self.assertEqual(saved.value.status, 200)
        self.assertTrue(saved.value.json()["ok"])
        expect(self.note).to_have_text("Сохранено ✓")
        expect(self.form).to_have_attribute("data-dirty", "0")
        self.assertEqual(self.row(), {"display_name": "Сохранённое имя", "birth_date": None})

        with self.page.expect_response(self.is_save) as rejected:
            self.name.fill("Черновик имени")
            self.birth.fill("2100-01-01")
        self.assertIn(rejected.value.status, (400, 422))
        expect(self.note).to_have_class("autosave-note err")
        expect(self.note).not_to_contain_text("Сохранено ✓")
        self.assertGreater(len(self.note.inner_text()), 10)
        expect(self.birth).to_have_value("2100-01-01")
        expect(self.name).to_have_value("Черновик имени")
        expect(self.form).to_have_attribute("data-dirty", "1")
        self.assertEqual(self.row(), {"display_name": "Сохранённое имя", "birth_date": None})

        with self.page.expect_response(self.is_save):
            self.birth.fill("2000-01-01")
        expect(self.note).to_have_text("Сохранено ✓")
        expect(self.form).to_have_attribute("data-saving", "0")
        self.assertEqual(self.row(), {"display_name": "Черновик имени", "birth_date": "2000-01-01"})

    def test_network_failure_preserves_dirty_values_and_retry_succeeds(self):
        self.page.route("**/admin/profile", lambda route: route.abort("failed") if route.request.method == "POST" else route.continue_())
        self.name.fill("Без сети")
        expect(self.note).to_have_class("autosave-note err")
        expect(self.note).to_contain_text("Нет связи")
        expect(self.form).to_have_attribute("data-dirty", "1")
        expect(self.form).to_have_attribute("data-saving", "0")
        self.assertEqual(self.row()["display_name"], "Исходное имя")
        expect(self.name).to_have_value("Без сети")
        self.page.unroute("**/admin/profile")
        self.name.fill("Связь восстановлена")
        expect(self.note).to_have_text("Сохранено ✓")
        self.assertEqual(self.row()["display_name"], "Связь восстановлена")

    def test_old_success_cannot_mark_a_newer_draft_saved(self):
        # Первый ответ задержан уже после реального commit на backend.
        first = True
        def delay_response(route):
            nonlocal first
            if route.request.method != "POST" or not first:
                route.continue_()
                return
            first = False
            response = route.fetch()
            self.name.fill("Новый черновик")
            route.fulfill(response=response)

        self.page.route("**/admin/profile", delay_response)
        self.name.fill("Первый снимок")
        expect(self.name).to_have_value("Новый черновик")
        expect(self.form).to_have_attribute("data-dirty", "1")
        expect(self.note).not_to_have_text("Сохранено ✓")
        expect(self.note).to_have_text("Сохранено ✓", timeout=10000)
        self.assertEqual(self.row()["display_name"], "Новый черновик")


if __name__ == "__main__":
    unittest.main(verbosity=2)
