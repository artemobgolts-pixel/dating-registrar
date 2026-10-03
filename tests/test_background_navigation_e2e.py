"""Фон кабинета сохраняет DOM и графический backend при реальной навигации."""

from io import BytesIO
import json
import re
import time
import unittest
from urllib.parse import urlparse

from PIL import Image
from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


GL_FLAGS = ["--use-gl=angle", "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
TRANSITION_TIMEOUT = 15000

BACKGROUND_PROBE = """() => {
    const probe = window.backgroundProbe = {workers:0, inits:0, stops:0,
        contexts:0, programs:0, turboLoads:0, skinChanges:0};
    const listen = document.addEventListener;
    window.navigationLifecycle = {loads: [], events: []};
    ['turbo:visit', 'turbo:render', 'turbo:load', 'mousedown', 'mouseup', 'click',
        'submit', 'invalid', 'turbo:submit-start', 'turbo:submit-end',
        'turbo:before-fetch-request', 'turbo:before-fetch-response'].forEach(type => {
        listen.call(document, type, event => {
            const link = event.target.closest && event.target.closest('a[href]');
            const entry = {type, path:location.pathname,
                href:link ? new URL(link.href).pathname : null,
                scroll:scrollY, restoring:!!sessionStorage.getItem('d4y_editor_scroll'),
                time:performance.now(), defaultPrevented:event.defaultPrevented,
                target:event.target.tagName, id:event.target.id || null,
                form:event.target.form ? event.target.form.id : null,
                x:event.clientX, y:event.clientY};
            navigationLifecycle.events.push(entry);
            setTimeout(() => { entry.defaultPrevented = event.defaultPrevented; }, 0);
            if (navigationLifecycle.events.length > 30) navigationLifecycle.events.shift();
            if (type === 'turbo:load') navigationLifecycle.loads.push(location.pathname);
        }, true);
    });
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


def click_and_wait_response(page, button, predicate, *, timeout=TRANSITION_TIMEOUT):
    # trial не выполняет input handlers: сам click тоже может задержаться.
    # Ловим ранний ответ заранее, но сетевой budget начинается после click.
    started = time.monotonic()
    button.click(trial=True, timeout=30000)
    actionable = time.monotonic()
    clicked = None
    captured = None

    def capture(response):
        nonlocal captured
        if captured is None and predicate(response):
            captured = response

    page.on("response", capture)
    try:
        button.click(timeout=30000)
        clicked = time.monotonic()
        if captured is not None:
            return captured
        # В sync API проверка captured и установка waiter не отдают управление
        # event loop: поздний ответ не теряется между двумя listeners.
        with page.expect_response(predicate, timeout=timeout) as response:
            pass
        return response.value
    except Exception:
        print("Save action timing: " + json.dumps({
            "actionability_seconds": round(actionable - started, 3),
            "native_click_seconds": round(clicked - actionable, 3) if clicked else None,
            "click_response_seconds": round(time.monotonic() - actionable, 3),
        }), flush=True)
        raise
    finally:
        page.remove_listener("response", capture)


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

    def wait_completed(self, page, path, previous_loads=0):
        # Новый HTML появляется до turbo:load и двух кадров восстановления scroll.
        # Следующий реальный клик делаем лишь после полного завершения перехода.
        page.wait_for_function("""({path, previous}) => {
            const loads = window.navigationLifecycle.loads;
            return loads.length > previous && loads[loads.length - 1] === path &&
                !document.documentElement.classList.contains('turbo-loading') &&
                !sessionStorage.getItem('d4y_editor_scroll');
        }""", arg={"path": path, "previous": previous_loads}, timeout=TRANSITION_TIMEOUT)

    def open_route(self, page, action, path):
        # Software WebGL делит CPU с Turbo. Проверяем ответ и завершённый render,
        # а не используем появление первой карточки как конец навигации.
        previous_body = page.query_selector("body")
        previous_loads = page.evaluate("navigationLifecycle.loads.length")
        with page.expect_response(
                lambda response: response.request.method == "GET" and
                urlparse(response.url).path == path,
                timeout=TRANSITION_TIMEOUT) as loaded:
            action()
        self.assertEqual(loaded.value.status, 200, path)
        page.wait_for_url(lambda url: urlparse(url).path == path,
                          timeout=TRANSITION_TIMEOUT)
        page.wait_for_function("body => document.body !== body", arg=previous_body,
                               timeout=TRANSITION_TIMEOUT)
        self.wait_completed(page, path, previous_loads)

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
        responses = []
        failed_requests = []
        requests = []
        request_started = time.monotonic()
        page.on("request", lambda request: requests.append({
            "method": request.method, "path": urlparse(request.url).path,
            "seconds": round(time.monotonic() - request_started, 3),
        }))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: posts.append(request)
                if request.method == "POST" else None)
        page.on("response", lambda response: responses.append({
            "method": response.request.method, "path": urlparse(response.url).path,
            "status": response.status,
            "seconds": round(time.monotonic() - request_started, 3),
        }))
        page.on("requestfailed", lambda request: failed_requests.append({
            "method": request.method, "path": urlparse(request.url).path,
            "failure": request.failure,
        }))

        def report_failure():
            result = self._outcome.result
            if any(test is self for test, _ in result.failures + result.errors):
                # Только пути и статусы: токены, query и тела запросов не выводим.
                print("Navigation failure: " + json.dumps({
                    "path": urlparse(page.url).path, "pageerrors": errors,
                    "responses": responses[-30:], "requestfailed": failed_requests[-10:],
                    "requests": requests[-30:],
                    "category_name": self.backend.row("SELECT name FROM categories WHERE id=?", (self.cid,)),
                    "ink": page.evaluate("window.__inkStats && window.__inkStats()"),
                    "lifecycle": page.evaluate("window.navigationLifecycle"),
                }, ensure_ascii=False), flush=True)

        self.addCleanup(report_failure)
        page.goto(self.backend.url + "/admin/dates")
        page.wait_for_function("() => window.__inkStats && window.__inkStats().firstFrameReady")
        if not reduced:
            page.wait_for_function("() => window.__inkStats().mode === 'interactive'")
        self.wait_completed(page, "/admin/dates")
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
        self.open_route(page, lambda: page.get_by_label("Фильтр событий", exact=True)
                        .select_option("public"), "/admin/dates")
        page.wait_for_url(re.compile(r"[?&]f=public(?:&|$)"))
        expect(page.locator(".dcard")).to_have_count(1)
        self.assert_background_retained(page, initial)

        self.open_route(page, lambda: page.locator(".dcard-link").click(),
                        f"/admin/dates/{self.did}/edit")
        expect(page.locator("#dateForm")).to_be_visible()
        self.assert_background_retained(page, initial)
        page.locator("#edTitle").fill("Сохранено без перезапуска фона")
        page.locator("#mediaInput").set_input_files(self.photo)
        previous_body = page.query_selector("body")
        previous_loads = page.evaluate("navigationLifecycle.loads.length")
        with (
            page.expect_response(lambda response: response.request.method == "POST" and
                urlparse(response.url).path == f"/admin/dates/{self.did}/edit",
                timeout=TRANSITION_TIMEOUT) as saved,
            page.expect_response(lambda response: response.request.method == "GET" and
                urlparse(response.url).path == f"/admin/dates/{self.did}/edit",
                timeout=TRANSITION_TIMEOUT) as reloaded,
        ):
            page.locator('button[form="dateForm"]').filter(has_text="Сохранить").click()
        self.assertEqual(saved.value.status, 200)
        self.assertTrue(saved.value.json()["ok"])
        self.assertEqual(reloaded.value.status, 200)
        page.wait_for_function("body => document.body !== body", arg=previous_body,
                               timeout=TRANSITION_TIMEOUT)
        self.wait_completed(page, f"/admin/dates/{self.did}/edit", previous_loads)
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

        self.open_route(page, lambda: page.locator('header nav a[href="/admin/categories"]').click(),
                        "/admin/categories")
        expect(page.locator(".cat-card")).to_have_count(1)
        self.assert_background_retained(page, initial)
        self.open_route(page, lambda: page.locator(".cat-link").click(),
                        f"/admin/categories/{self.cid}")
        expect(page.locator("#categoryAppearance")).to_be_visible()
        expect(page.locator('head link[href*="category-settings.css"]')).to_have_count(1)
        self.assert_background_retained(page, initial)
        page.locator("#categoryAppearance > summary").click()
        page.locator('#categoryEditForm [name="name"]').fill("Подборка сохранена")
        previous_body = page.query_selector("body")
        previous_loads = page.evaluate("navigationLifecycle.loads.length")
        renamed = click_and_wait_response(page,
            page.locator('button[form="categoryEditForm"][type="submit"]'),
            lambda response: response.request.method == "POST" and
                urlparse(response.url).path == f"/admin/categories/{self.cid}/rename")
        self.assertEqual(renamed.status, 303)
        page.wait_for_function("body => document.body !== body", arg=previous_body,
                               timeout=TRANSITION_TIMEOUT)
        self.wait_completed(page, f"/admin/categories/{self.cid}", previous_loads)
        expect(page.locator("h1")).to_have_text("Подборка сохранена", timeout=TRANSITION_TIMEOUT)
        self.assert_background_retained(page, initial)
        self.assertEqual(self.backend.row("SELECT name FROM categories WHERE id=?", (self.cid,))["name"],
                         "Подборка сохранена")

        # Возврат не должен показать старое имя из кэша или старый контроллер.
        body = page.query_selector("body")
        previous_loads = page.evaluate("navigationLifecycle.loads.length")
        page.go_back()
        page.wait_for_function("body => document.body !== body", arg=body)
        self.wait_completed(page, f"/admin/categories/{self.cid}", previous_loads)
        expect(page.locator("#categoryAppearance")).to_be_visible()
        self.assert_background_retained(page, initial)
        body = page.query_selector("body")
        previous_loads = page.evaluate("navigationLifecycle.loads.length")
        page.go_back()
        page.wait_for_function("body => document.body !== body", arg=body)
        self.wait_completed(page, "/admin/categories", previous_loads)
        expect(page.locator(".cat-card .cat-name")).to_have_text("Подборка сохранена")
        expect(page.locator('head link[href*="category-settings.css"]')).to_have_count(0)
        self.assert_background_retained(page, initial)

        self.open_route(page, lambda: page.locator('header nav a[href="/admin/profile"]').click(),
                        "/admin/profile")
        expect(page.locator("#profileForm")).to_be_visible()
        self.assert_background_retained(page, initial)
        # Повторный render не должен регистрировать второй upload listener.
        page.evaluate("document.dispatchEvent(new Event('live-search:render'))")
        previous_loads = page.evaluate("navigationLifecycle.loads.length")
        with page.expect_response(lambda response: response.request.method == "POST" and
                                  urlparse(response.url).path == "/admin/profile",
                                  timeout=TRANSITION_TIMEOUT) as avatar_saved:
            page.locator("#avatarInput").set_input_files(self.photo)
        self.assertEqual(avatar_saved.value.status, 303)
        self.wait_completed(page, "/admin/profile", previous_loads)
        expect(page.locator("#profileForm .avatar-img")).to_be_visible(timeout=TRANSITION_TIMEOUT)
        self.assert_background_retained(page, initial)
        profile_posts = [request for request in posts
                         if urlparse(request.url).path == "/admin/profile"]
        self.assertEqual(len(profile_posts), 1, "Аватар загружен повторным обработчиком")
        self.assertTrue(self.backend.row("SELECT avatar_path FROM users WHERE id=?", (self.uid,))["avatar_path"])
        self.open_route(page, lambda: page.locator(
            f'[data-profile-editor*="/admin/dates/{self.did}/edit"]').click(),
            f"/admin/dates/{self.did}/edit")
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
