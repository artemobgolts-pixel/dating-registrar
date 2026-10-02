"""Фон кабинета сохраняет DOM и графический backend при реальной навигации."""

from io import BytesIO
import re
import unittest
from urllib.parse import urlparse

from PIL import Image
from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


GL_FLAGS = ["--use-gl=angle", "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]

BACKGROUND_PROBE = """() => {
    const probe = window.backgroundProbe = {workers:0, inits:0, stops:0,
        contexts:0, programs:0, turboLoads:0, skinChanges:0};
    const listen = document.addEventListener;
    document.addEventListener = function(type, ...options) {
        if (type === 'turbo:load') probe.turboLoads++;
        if (type === 'd4y:skinchange') probe.skinChanges++;
        return listen.call(this, type, ...options);
    };
    const OriginalWorker = window.Worker;
    window.Worker = function(url, options) {
        const worker = new OriginalWorker(url, options);
        if (String(url).includes('ink-worker.js')) {
            probe.workers++;
            const post = worker.postMessage.bind(worker);
            worker.postMessage = function(message, transfer) {
                if (message.type === 'init') probe.inits++;
                return post(message, transfer);
            };
            const stop = worker.terminate.bind(worker);
            worker.terminate = function() {probe.stops++; return stop();};
        }
        return worker;
    };
    window.Worker.prototype = OriginalWorker.prototype;
    const context = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, ...options) {
        if (type === 'webgl2') probe.contexts++;
        return context.call(this, type, ...options);
    };
    const program = WebGL2RenderingContext.prototype.createProgram;
    WebGL2RenderingContext.prototype.createProgram = function() {
        probe.programs++; return program.call(this);
    };
}"""


class BackgroundNavigationBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = LiveBackend()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, args=GL_FLAGS)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.backend.close()

    def setUp(self):
        self.uid, self.cookie = self.backend.user_cookie()
        conn = self.backend.db.connect()
        try:
            conn.execute("UPDATE users SET cursor_effects=1 WHERE id=?", (self.uid,))
            self.cid = conn.execute(
                "INSERT INTO categories(owner_id,name,link_token,choice_mode,"
                "voting_deadline,voting_status,created_at) "
                "VALUES(?,?,?,'single','2099-10-21T23:00','open',?)",
                (self.uid, "Подборка для переходов", "background-category",
                 self.backend.main.now_iso()),
            ).lastrowid
            self.did = conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,is_public,is_draft,created_at) "
                "VALUES(?,?,?,1,0,?)",
                (self.uid, "Событие для переходов", "background-event",
                 self.backend.main.now_iso()),
            ).lastrowid
            conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,is_public,is_draft,created_at) "
                "VALUES(?,?,?,0,0,?)",
                (self.uid, "Непубличное событие", "background-private",
                 self.backend.main.now_iso()),
            )
            conn.execute(
                "INSERT INTO date_categories(date_id,category_id,position) VALUES(?,?,0)",
                (self.did, self.cid),
            )
            conn.commit()
        finally:
            conn.close()
        image = BytesIO()
        Image.new("RGB", (320, 180), "#436487").save(image, "WEBP")
        self.photo = {"name": "background-upload.webp", "mimeType": "image/webp",
                      "buffer": image.getvalue()}

    def assert_background_retained(self, page, initial):
        state = page.evaluate("""() => {
            const old = window.keptBackground;
            const host = document.getElementById('bg-smoke');
            return {origin:performance.timeOrigin, kept:!!old && old.host===host &&
                old.controller===host.__d4yInkController && old.media.isConnected &&
                old.media.parentNode===host,
                probe:{...window.backgroundProbe}, stats:window.__inkStats && window.__inkStats(),
                canvases:host.querySelectorAll('.ink-canvas').length,
                posters:host.querySelectorAll('.ink-static-frame').length};
        }""")
        self.assertEqual(state["origin"], initial["origin"], "Заново загружен документ")
        self.assertTrue(state["kept"], "Заменён фон, canvas/постер или его контроллер")
        self.assertEqual(state["probe"], initial["probe"], "Повторная инициализация графики")
        self.assertEqual(state["stats"]["backend"], initial["backend"])
        self.assertTrue(state["stats"]["firstFrameReady"])
        self.assertEqual(state["canvases"], 0 if initial["backend"] == "poster" else 1)
        self.assertEqual(state["posters"], 1 if initial["backend"] == "poster" else 0)
        self.assertTrue(page.evaluate("""() => {
            const csrf=document.body.dataset.csrf;
            return !!csrf && csrf !== 'устаревшее-значение' &&
                [...document.querySelectorAll('input[name=csrf]')].every(el=>el.value===csrf);
        }"""), "После перехода CSRF форм расходится с новым body")

    def run_navigation(self, *, force_main=False, mobile=False, reduced=False):
        context = self.browser.new_context(
            viewport={"width": 390 if mobile else 900, "height": 700},
            is_mobile=mobile, has_touch=mobile,
            reduced_motion="reduce" if reduced else "no-preference",
        )
        self.addCleanup(context.close)
        context.add_cookies([self.cookie])
        context.add_init_script("(" + BACKGROUND_PROBE + ")()")
        if force_main:
            context.add_init_script("window.__INK_FORCE_MAIN=true")
        page = context.new_page()
        posts = []
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: posts.append(request)
                if request.method == "POST" else None)
        page.goto(self.backend.url + "/admin/dates")
        page.wait_for_function("() => window.__inkStats && window.__inkStats().firstFrameReady")
        if not reduced:
            page.wait_for_function("() => window.__inkStats().mode === 'interactive'")
        initial = page.evaluate("""() => {
            const host=document.getElementById('bg-smoke');
            window.keptBackground={host, controller:host.__d4yInkController,
                media:host.querySelector('.ink-canvas,.ink-static-frame')};
            return {origin:performance.timeOrigin, probe:{...window.backgroundProbe},
                backend:window.__inkStats().backend};
        }""")
        self.assertEqual(initial["backend"], "poster" if reduced else "main" if force_main else "worker")
        expect(page.locator(".dcard")).to_have_count(2)

        # GET-фильтр должен сменить серверную страницу и CSRF, сохранив документ.
        page.evaluate("document.body.dataset.csrf='устаревшее-значение'")
        page.get_by_label("Фильтр событий", exact=True).select_option("public")
        page.wait_for_url(re.compile(r"[?&]f=public(?:&|$)"))
        expect(page.locator(".dcard")).to_have_count(1)
        self.assert_background_retained(page, initial)

        page.locator(".dcard-link").click()
        expect(page.locator("#dateForm")).to_be_visible()
        self.assert_background_retained(page, initial)
        page.locator("#edTitle").fill("Сохранено без перезапуска фона")
        page.locator("#mediaInput").set_input_files(self.photo)
        with page.expect_response(lambda response: response.request.method == "POST" and
                                  urlparse(response.url).path == f"/admin/dates/{self.did}/edit") as saved:
            page.locator('button[form="dateForm"]').filter(has_text="Сохранить").click()
        self.assertEqual(saved.value.status, 200)
        self.assertTrue(saved.value.json()["ok"])
        expect(page.locator(".flash").filter(has_text="Сохранено")).to_be_visible(timeout=15000)
        expect(page.locator("#edTitle")).to_have_text("Сохранено без перезапуска фона")
        expect(page.locator(".ed-slide[data-pid] img")).to_have_count(1)
        self.assert_background_retained(page, initial)
        self.assertEqual(self.backend.row("SELECT name FROM dates WHERE id=?", (self.did,))["name"],
                         "Сохранено без перезапуска фона")
        self.assertEqual(self.backend.row("SELECT COUNT(*) AS n FROM date_images WHERE date_id=?",
                                         (self.did,))["n"], 1)
        editor_posts = [request for request in posts
                        if urlparse(request.url).path == f"/admin/dates/{self.did}/edit"]
        self.assertEqual(len(editor_posts), 1, "Сохранение отправлено повторным обработчиком")
        self.assertIn("multipart/form-data", editor_posts[0].headers["content-type"])

        page.locator('header nav a[href="/admin/categories"]').click()
        expect(page.locator(".cat-card")).to_have_count(1)
        self.assert_background_retained(page, initial)
        page.locator(".cat-link").click()
        expect(page.locator("#categoryAppearance")).to_be_visible()
        expect(page.locator('head link[href*="category-settings.css"]')).to_have_count(1)
        self.assert_background_retained(page, initial)
        page.locator("#categoryAppearance > summary").click()
        page.locator('#categoryEditForm [name="name"]').fill("Подборка сохранена")
        with page.expect_response(
                lambda response: response.request.method == "POST" and
                urlparse(response.url).path == f"/admin/categories/{self.cid}/rename",
                timeout=15000) as renamed:
            page.locator('button[form="categoryEditForm"][type="submit"]').click()
        self.assertEqual(renamed.value.status, 303)
        expect(page.locator("h1")).to_have_text("Подборка сохранена", timeout=15000)
        self.assert_background_retained(page, initial)
        self.assertEqual(self.backend.row("SELECT name FROM categories WHERE id=?", (self.cid,))["name"],
                         "Подборка сохранена")

        # Возврат не должен показать старое имя из кэша или старый контроллер.
        body = page.query_selector("body")
        page.go_back()
        page.wait_for_function("body => document.body !== body", arg=body)
        expect(page.locator("#categoryAppearance")).to_be_visible()
        self.assert_background_retained(page, initial)
        body = page.query_selector("body")
        page.go_back()
        page.wait_for_function("body => document.body !== body", arg=body)
        expect(page.locator(".cat-card .cat-name")).to_have_text("Подборка сохранена")
        expect(page.locator('head link[href*="category-settings.css"]')).to_have_count(0)
        self.assert_background_retained(page, initial)

        page.locator('header nav a[href="/admin/profile"]').click()
        expect(page.locator("#profileForm")).to_be_visible()
        self.assert_background_retained(page, initial)
        # Повторный render не должен регистрировать второй upload listener.
        page.evaluate("document.dispatchEvent(new Event('live-search:render'))")
        page.locator("#avatarInput").set_input_files(self.photo)
        expect(page.locator("#profileForm .avatar-img")).to_be_visible()
        self.assert_background_retained(page, initial)
        profile_posts = [request for request in posts
                         if urlparse(request.url).path == "/admin/profile"]
        self.assertEqual(len(profile_posts), 1, "Аватар загружен повторным обработчиком")
        self.assertTrue(self.backend.row("SELECT avatar_path FROM users WHERE id=?", (self.uid,))["avatar_path"])
        page.locator(f'[data-profile-editor*="/admin/dates/{self.did}/edit"]').click()
        expect(page.locator("#dateForm")).to_be_visible()
        self.assert_background_retained(page, initial)
        self.assertEqual(errors, [], "Ошибка JavaScript при внутренних переходах")

    def test_worker_keeps_running_through_filters_uploads_and_history(self):
        self.run_navigation()

    def test_main_renderer_keeps_running_through_filters_uploads_and_history(self):
        self.run_navigation(force_main=True)

    def test_mobile_reduced_motion_keeps_poster_through_filters_uploads_and_history(self):
        self.run_navigation(mobile=True, reduced=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
