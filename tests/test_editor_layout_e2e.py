"""Геометрия полей редактора и доступность действий со страницы события."""

import unittest

from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


class EditorLayoutBrowserTests(unittest.TestCase):
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

    def test_typing_keeps_field_widths_and_contents_inside_card(self):
        for skin in ("friends", "romantic"):
            _, cookie = self.backend.user_cookie(skin=skin)
            for theme in ("light", "dark"):
                for width in (320, 390, 1280):
                    with self.subTest(skin=skin, theme=theme, width=width):
                        context = self.browser.new_context(viewport={"width": width, "height": 900})
                        context.add_cookies([cookie])
                        try:
                            page = context.new_page()
                            page.goto(self.backend.url + "/admin/dates/new")
                            page.locator("html").evaluate("(el, theme) => el.dataset.theme = theme", theme)
                            for selector, value in (("[data-tr-dd]", "21"), ("[data-tr-mo]", "10"),
                                                    ("[data-tr-yy]", "2030"), ("[data-tr-hh]", "18")):
                                field = page.locator(selector)
                                empty = field.bounding_box()
                                field.fill(value)
                                filled = field.bounding_box()
                                self.assertAlmostEqual(empty["width"], filled["width"], delta=1)
                                self.assertAlmostEqual(empty["height"], filled["height"], delta=1)
                            fields = ("#edTitle", "#edPlace", "#edDesc", "#edLinks")
                            for selector in fields:
                                page.locator(selector).fill("Короткий текст")
                            before = {selector: page.locator(selector).bounding_box() for selector in fields}
                            for selector in fields:
                                value = ("ДлинноеСлово" * 16 if selector == "#edTitle"
                                         else "https://example.com/" + "длинныйадрес" * 45)
                                if selector in ("#edDesc", "#edLinks"):
                                    value += "\nВторая строка\nТретья строка"
                                page.locator(selector).fill(value)
                            for selector in fields:
                                box = page.locator(selector).bounding_box()
                                self.assertAlmostEqual(box["x"], before[selector]["x"], delta=1)
                                self.assertAlmostEqual(box["width"], before[selector]["width"], delta=1)
                                self.assertTrue(page.locator(selector).evaluate(
                                    "el => el.scrollWidth <= el.clientWidth + 1"), selector)
                            self.assertTrue(page.evaluate(
                                "document.documentElement.scrollWidth <= window.innerWidth"))
                        finally:
                            context.close()

    def test_editor_menu_has_share_first_and_all_event_actions(self):
        uid, cookie = self.backend.user_cookie()
        conn = self.backend.db.connect()
        try:
            did = conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,created_at) VALUES(?,?,?,?)",
                (uid, "Событие с меню", "editor-layout-event", self.backend.main.now_iso()),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()
        context = self.browser.new_context(viewport={"width": 390, "height": 844})
        context.add_cookies([cookie])
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(self.backend.url + "/admin/dates")
        page.locator(".dcard .more").click()
        list_actions = page.locator(".dcard .menu button").all_text_contents()
        page.goto(f"{self.backend.url}/admin/dates/{did}/edit")
        page.get_by_role("button", name="Другие действия с событием").click()
        menu = page.locator(".cat-editor-menu")
        expect(menu).to_be_visible()
        expect(menu.locator("button").first).to_have_text("Поделиться")
        self.assertEqual(menu.locator("button").all_text_contents(), [
            "Поделиться", "Скопировать событие", "В архив", "Сделать публичным", "Удалить",
        ])
        self.assertEqual(menu.locator("button").all_text_contents(), list_actions)
        self.assertEqual(menu.locator("button").first.locator("svg").count(), 0)
        box = menu.bounding_box()
        self.assertGreaterEqual(box["y"], 0)
        self.assertLessEqual(box["y"] + box["height"], 844)
        with page.expect_navigation():
            menu.get_by_role("button", name="Сделать публичным", exact=True).click()
        self.assertEqual(self.backend.row("SELECT is_public FROM dates WHERE id=?", (did,))["is_public"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
