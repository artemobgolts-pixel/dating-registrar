"""Профиль: один multipart POST, порядок автосохранения и непрерывный фон."""

import io
import unittest
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


class ProfileAvatarNavigationTests(unittest.TestCase):
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
        self.context = self.browser.new_context(reduced_motion="reduce")
        self.context.add_cookies([cookie])
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.posts = []
        self.errors = []
        self.page.on("request", lambda request: self.posts.append(request)
                     if request.method == "POST" and request.url.endswith("/admin/profile") else None)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.backend.url + "/admin/profile")
        self.page.wait_for_function("document.getElementById('profileForm')._d4yProfileReady === true")
        self.page.evaluate("""() => {
          window.profileNavigationOrigin = performance.timeOrigin;
          window.profileNavigationBackground = document.getElementById('bg-smoke');
          window.profileNavigationController = profileNavigationBackground.__d4yInkController;
          for (let i = 0; i < 3; i++) document.dispatchEvent(new CustomEvent('live-search:render'));
        }""")

    def attach_avatar(self, color="indigo", filename="synthetic-avatar.png"):
        image = io.BytesIO()
        Image.new("RGB", (32, 32), color).save(image, "PNG")
        self.page.locator("#avatarInput").set_input_files({
            "name": filename, "mimeType": "image/png", "buffer": image.getvalue(),
        })

    def assert_avatar_saved(self, name="Исходное имя", skin="friends"):
        expect(self.page.locator(".avatar-img")).to_be_visible()
        self.page.wait_for_function("document.getElementById('profileForm')._d4yProfileReady === true")
        # Проверяем и отложенные дубли, которые раньше создавал каждый render.
        self.page.wait_for_timeout(1100)
        row = self.backend.row("SELECT display_name,admin_skin,avatar_path FROM users WHERE id=?", (self.uid,))
        self.assertEqual(row["display_name"], name)
        self.assertEqual(row["admin_skin"], skin)
        self.assertTrue((Path(self.backend.data.name) / "uploads" / row["avatar_path"]).is_file())
        self.assertTrue(self.page.evaluate("""() =>
          performance.timeOrigin === window.profileNavigationOrigin &&
          document.getElementById('bg-smoke') === window.profileNavigationBackground &&
          document.getElementById('bg-smoke').__d4yInkController === window.profileNavigationController
        """))
        self.assertEqual(self.errors, [])

    def test_avatar_after_repeated_render_sends_one_post_and_preserves_background(self):
        self.attach_avatar()
        self.assert_avatar_saved()
        self.assertEqual(len(self.posts), 1)
        self.assertIn("multipart/form-data", self.posts[0].headers["content-type"])

    def test_avatar_waits_for_pending_skin_save_and_keeps_newer_name(self):
        delayed = False

        def pending_profile_save(route):
            nonlocal delayed
            if route.request.method != "POST" or delayed:
                route.continue_()
                return
            delayed = True
            response = route.fetch()
            # Ответ первого снимка ещё не дошёл до браузера. Аватар обязан
            # дождаться этого запроса, а затем отправить текущее имя.
            self.page.locator("#profileName").fill("Имя после смены темы")
            self.attach_avatar()
            self.assertEqual(len(self.posts), 1)
            route.fulfill(response=response)

        self.page.route("**/admin/profile", pending_profile_save)
        self.page.locator('.skin-option:has([name="admin_skin"][value="romantic"])').click()
        self.assert_avatar_saved(name="Имя после смены темы", skin="romantic")
        self.assertTrue(delayed)
        self.assertEqual(len(self.posts), 2)

    def test_replacing_avatar_during_pending_save_submits_only_latest_selection(self):
        self.page.evaluate("""() => {
          window.avatarSubmissions = 0;
          document.addEventListener('submit', event => {
            if (event.target.id === 'profileForm') window.avatarSubmissions++;
          });
        }""")
        delayed = False

        def pending_profile_save(route):
            nonlocal delayed
            if route.request.method != "POST" or delayed:
                route.continue_()
                return
            delayed = True
            response = route.fetch()
            self.attach_avatar(color="indigo", filename="first-avatar.png")
            self.attach_avatar(color="gold", filename="latest-avatar.png")
            route.fulfill(response=response)

        self.page.route("**/admin/profile", pending_profile_save)
        self.page.locator('.skin-option:has([name="admin_skin"][value="romantic"])').click()
        self.assert_avatar_saved(skin="romantic")
        self.assertEqual(self.page.evaluate("window.avatarSubmissions"), 1)
        self.assertEqual(len(self.posts), 2)
        avatar = self.backend.row("SELECT avatar_path FROM users WHERE id=?", (self.uid,))["avatar_path"]
        with Image.open(Path(self.backend.data.name) / "uploads" / avatar) as saved:
            actual = saved.convert("RGB").getpixel((1, 1))
        self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, (255, 215, 0))), 25)

    def test_avatar_waits_for_skin_save_added_after_file_selection(self):
        first = True

        def pending_profile_save(route):
            nonlocal first
            if route.request.method != "POST" or not first:
                route.continue_()
                return
            first = False
            response = route.fetch()
            self.attach_avatar()
            self.page.locator('.skin-option:has([name="admin_skin"][value="friends"])').click()
            self.page.locator("#profileName").fill("Новое имя после выбора аватара")
            route.fulfill(response=response)

        self.page.route("**/admin/profile", pending_profile_save)
        self.page.locator('.skin-option:has([name="admin_skin"][value="romantic"])').click()
        self.assert_avatar_saved(name="Новое имя после выбора аватара")
        self.assertEqual(len(self.posts), 3)
        # Все более ранние текстовые снимки подтверждены до отправки аватара:
        # последним записывается актуальное имя из полной multipart-формы.
        self.assertEqual([request.headers.get("x-requested-with") for request in self.posts],
                         ["fetch", "fetch", None])

    def test_detached_profile_debounce_cannot_save_after_internal_navigation(self):
        self.page.locator("#profileName").fill("Отложенный снимок покинутой формы")
        self.page.locator('nav.glass-nav a[href="/admin/dates"]').click()
        expect(self.page).to_have_url(self.backend.url + "/admin/dates")
        self.page.wait_for_timeout(1100)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.backend.row("SELECT display_name FROM users WHERE id=?", (self.uid,))["display_name"], "Исходное имя")
        self.assertEqual(self.errors, [])

    def test_detached_profile_cannot_start_a_save_already_waiting_in_queue(self):
        held = []

        def hold_first_save(route):
            if route.request.method != "POST":
                route.continue_()
                return
            # Первый запрос уже записан сервером; задерживаем только ответ.
            held.append((route, route.fetch()))
            self.page.evaluate("window.profileHeldResponse = true")

        self.page.route("**/admin/profile", hold_first_save)
        self.addCleanup(self.page.unroute_all, behavior="ignoreErrors")
        self.page.locator('.skin-option:has([name="admin_skin"][value="romantic"])').click()
        self.page.wait_for_function("() => window.profileHeldResponse === true && !!window.d4yProfileSave")
        self.assertEqual(len(held), 1)
        self.assertEqual(self.backend.row("SELECT admin_skin FROM users WHERE id=?", (self.uid,))["admin_skin"], "romantic")

        # Второй снимок создан на живой форме, но ещё ждёт ответа первого.
        self.page.locator('.skin-option:has([name="admin_skin"][value="friends"])').click()
        self.page.evaluate("() => { window.detachedQueuedProfileSave = window.d4yProfileSave; }")
        self.page.locator('nav.glass-nav a[href="/admin/dates"]').click()
        expect(self.page).to_have_url(self.backend.url + "/admin/dates")
        expect(self.page.locator("#profileForm")).to_have_count(0)

        route, response = held.pop()
        route.fulfill(response=response)
        self.page.evaluate("async () => { await window.detachedQueuedProfileSave; }")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(held, [])
        # Разрешённая уже завершившаяся запись A сохраняется, B не стартует.
        self.assertEqual(self.backend.row("SELECT admin_skin FROM users WHERE id=?", (self.uid,))["admin_skin"], "romantic")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
