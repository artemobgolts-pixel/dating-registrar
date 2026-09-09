"""FLOW-07: черновик редактора переживает серверную ошибку и навигацию."""

import io
import unittest

from PIL import Image
from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


class EditorDraftBrowserTests(unittest.TestCase):
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
        self.page.goto(self.backend.url + "/admin/dates/new")

    def fill(self, title="Черновик встречи", description="Описание, которое нельзя потерять"):
        self.page.locator("#edTitle").fill(title)
        self.page.locator("#edDesc").fill(description)

    def records(self):
        return self.backend.row("SELECT COUNT(*) AS n FROM dates WHERE owner_id=?", (self.uid,))["n"]

    @staticmethod
    def is_save(response):
        return "/admin/dates/" in response.url and response.request.method == "POST"

    def save(self):
        self.page.locator('button[form="dateForm"]').click()

    def attach_photo(self):
        image = io.BytesIO()
        Image.new("RGB", (32, 24), "navy").save(image, "PNG")
        self.page.locator("#mediaInput").set_input_files({"name": "synthetic.png", "mimeType": "image/png", "buffer": image.getvalue()})
        expect(self.page.locator("#edSlides .ed-slide")).to_have_count(1)

    def test_overlong_title_client_and_server_fallback_preserve_draft(self):
        title = "Д" * 201
        description = "Сохранить всё описание после ошибки названия"
        self.fill(title, description)
        self.attach_photo()
        self.save()
        expect(self.page.locator("#editorError")).to_be_visible()
        expect(self.page.locator("#edTitle")).to_have_text(title)
        self.assertEqual(self.records(), 0)
        # Native submit намеренно обходит JS-проверку, проверяя серверный контракт.
        with self.page.expect_navigation(wait_until="domcontentloaded") as navigation:
            self.page.locator("#dateForm").evaluate("form => form.submit()")
        self.assertIn(navigation.value.status, (400, 422))
        expect(self.page).to_have_url(self.backend.url + "/admin/dates/new")
        expect(self.page.locator("#editorError")).to_be_visible()
        expect(self.page.locator("#edTitle")).to_have_text(title)
        expect(self.page.locator("#edDesc")).to_have_text(description)
        expect(self.page.locator("#descInput")).to_have_value(description)
        expect(self.page.locator("#editorMediaNotice")).to_be_visible()
        self.assertEqual(self.page.locator("#imagesInput").evaluate("field => field.files.length"), 0)
        self.assertEqual(list((self.backend.db.DATA_DIR / "uploads").iterdir()), [])
        self.assertEqual(self.records(), 0)

        self.page.locator("#edTitle").fill("Исправленное название")
        with self.page.expect_response(self.is_save) as saved:
            self.save()
        self.assertTrue(saved.value.json()["ok"])
        expect(self.page).not_to_have_url(self.backend.url + "/admin/dates/new")
        row = self.backend.row("SELECT name,comment FROM dates WHERE owner_id=?", (self.uid,))
        self.assertEqual(row, {"name": "Исправленное название", "comment": description})

    def test_server_error_keeps_media_then_correction_and_edit_succeed(self):
        self.fill()
        self.attach_photo()
        self.page.locator("#fStart").evaluate("field => field.value = 'not-a-date'")
        with self.page.expect_response(self.is_save) as rejected:
            self.save()
        self.assertIn(rejected.value.status, (400, 422))
        expect(self.page.locator("#editorError")).to_be_visible()
        expect(self.page.locator("#edDesc")).to_have_text("Описание, которое нельзя потерять")
        self.assertEqual(self.page.locator("#imagesInput").evaluate("field => field.files.length"), 1)
        self.assertEqual(self.records(), 0)

        self.page.locator("#fStart").evaluate("field => field.value = ''")
        with self.page.expect_response(self.is_save) as saved:
            self.save()
        self.assertTrue(saved.value.json()["ok"])
        expect(self.page).not_to_have_url(self.backend.url + "/admin/dates/new")
        row = self.backend.row("SELECT id,name,comment FROM dates WHERE owner_id=?", (self.uid,))
        self.assertEqual(self.backend.row("SELECT COUNT(*) AS n FROM date_images WHERE date_id=?", (row["id"],))["n"], 1)
        self.page.goto(f"{self.backend.url}/admin/dates/{row['id']}/edit")
        self.fill("Изменённое событие", "Изменённое описание")
        self.page.locator("#fStart").evaluate("field => field.value = 'bad-edit-date'")
        with self.page.expect_response(self.is_save):
            self.save()
        expect(self.page.locator("#editorError")).to_be_visible()
        self.assertEqual(self.backend.row("SELECT name FROM dates WHERE id=?", (row["id"],))["name"], "Черновик встречи")
        self.page.locator("#fStart").evaluate("field => field.value = ''")
        with self.page.expect_response(self.is_save) as edited:
            self.save()
        self.assertTrue(edited.value.json()["ok"])
        self.assertEqual(self.records(), 1)
        self.assertEqual(self.backend.row("SELECT name,comment FROM dates WHERE id=?", (row["id"],)), {"name": "Изменённое событие", "comment": "Изменённое описание"})

    def test_back_forward_after_rejected_submit_restores_text(self):
        self.fill("Черновик для истории", "Описание для возврата")
        self.page.locator("#fStart").evaluate("field => field.value = 'bad-date'")
        with self.page.expect_response(self.is_save):
            self.save()
        expect(self.page.locator("#editorError")).to_be_visible()
        self.page.locator(".date-editor-back").click()
        expect(self.page).to_have_url(self.backend.url + "/admin/dates")
        self.page.go_back()
        expect(self.page.locator("#edTitle")).to_have_text("Черновик для истории")
        expect(self.page.locator("#edDesc")).to_have_text("Описание для возврата")
        self.page.go_forward()
        expect(self.page).to_have_url(self.backend.url + "/admin/dates")
        self.page.go_back()
        expect(self.page.locator("#edDesc")).to_have_text("Описание для возврата")
        self.assertEqual(self.records(), 0)

    def test_framework_validation_keeps_text_and_allows_correction(self):
        self.fill("Проверка типов", "Описание после ошибки categories")
        self.page.locator("#dateForm").evaluate("""form => {
            const field = document.createElement('input');
            field.type = 'hidden'; field.name = 'categories'; field.value = 'invalid-id';
            form.appendChild(field);
        }""")
        with self.page.expect_navigation(wait_until="domcontentloaded") as navigation:
            self.page.locator("#dateForm").evaluate("form => form.submit()")
        self.assertEqual(navigation.value.status, 422)
        expect(self.page.locator("#editorError")).to_be_visible()
        expect(self.page.locator("#edTitle")).to_have_text("Проверка типов")
        expect(self.page.locator("#edDesc")).to_have_text("Описание после ошибки categories")
        self.assertEqual(self.records(), 0)
        with self.page.expect_response(self.is_save) as saved:
            self.save()
        self.assertTrue(saved.value.json()["ok"])
        self.assertEqual(self.backend.row("SELECT name,comment FROM dates WHERE owner_id=?", (self.uid,)),
                         {"name": "Проверка типов", "comment": "Описание после ошибки categories"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
