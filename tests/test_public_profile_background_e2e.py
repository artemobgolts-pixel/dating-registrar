"""Вкладки публичного профиля сохраняют фон, историю и границы доступа."""

import re
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


GL_FLAGS = ["--use-gl=angle", "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
TRANSITION_TIMEOUT = 15000
PROBE = """() => {
  const probe=window.profileGraphicsProbe={workers:0,inits:0,stops:0,contexts:0,programs:0};
  const lifecycle=window.profileNavigationLifecycle={loads:[]};
  document.addEventListener('turbo:load',()=>lifecycle.loads.push(location.href));
  const WorkerBase=window.Worker;
  window.Worker=function(url,options) {
    const worker=new WorkerBase(url,options);
    if(String(url).includes('ink-worker.js')) {
      probe.workers++;
      const post=worker.postMessage.bind(worker),stop=worker.terminate.bind(worker);
      worker.postMessage=function(message,transfer) {if(message.type==='init')probe.inits++;return post(message,transfer)};
      worker.terminate=function(){probe.stops++;return stop()};
    }
    return worker;
  };
  window.Worker.prototype=WorkerBase.prototype;
  const getContext=HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext=function(type,...args) {
    if(type==='webgl2')probe.contexts++;
    return getContext.call(this,type,...args);
  };
  const createProgram=WebGL2RenderingContext.prototype.createProgram;
  WebGL2RenderingContext.prototype.createProgram=function(){probe.programs++;return createProgram.call(this)};
}"""


class PublicProfileBackgroundBrowserTests(unittest.TestCase):
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
            conn.execute("UPDATE users SET cursor_effects=1,display_name='Профиль для вкладок' WHERE id=?", (self.uid,))
            author = conn.execute(
                "INSERT INTO users(telegram_id,display_name,created_at) VALUES(90002,'Автор событий',?)",
                (self.backend.main.now_iso(),),
            ).lastrowid

            def event(owner, name, token, public=True):
                return conn.execute(
                    "INSERT INTO dates(owner_id,name,share_token,is_public,is_draft,starts_at,created_at) "
                    "VALUES(?,?,?,?,0,'2099-01-01T19:00',?)",
                    (owner, name, token, int(public), self.backend.main.now_iso()),
                ).lastrowid

            for number in range(13):
                event(self.uid, f"Событие {number:02}", f"profile-bg-event-{number}")
            event(self.uid, "Секретное событие", "profile-bg-secret", public=False)
            self.wanted = event(author, "План с друзьями", "profile-bg-want")
            private_want = event(author, "Закрытый план", "profile-bg-private-want", public=False)
            reviewed = event(author, "Поход в театр", "profile-bg-review")
            private_review = event(author, "Закрытый спектакль", "profile-bg-private-review", public=False)
            stamp = self.backend.main.now_iso()
            conn.executemany(
                "INSERT INTO date_wants(user_id,date_id,is_public,created_at,updated_at) VALUES(?,?,1,?,?)",
                [(self.uid, did, stamp, stamp) for did in (self.wanted, private_want)],
            )
            conn.executemany(
                "INSERT INTO date_reviews(user_id,date_id,rating,text,is_public,created_at,updated_at) "
                "VALUES(?,?,5,?,1,?,?)",
                [(self.uid, reviewed, "Отличный вечер", stamp, stamp),
                 (self.uid, private_review, "Личный отзыв", stamp, stamp)],
            )
            conn.commit()
        finally:
            conn.close()

    def assert_retained(self, page, initial):
        state = page.evaluate("""() => {
          const host=document.querySelector('.bg-smoke'),old=window.keptProfileBackground;
          return {origin:performance.timeOrigin,kept:!!old&&old.host===host&&
            old.controller===host.__d4yInkController&&old.media.isConnected&&old.media.parentNode===host&&
            old.wrapper===document.getElementById('profile-background')&&old.wrapper.contains(host)&&
            old.decoration===document.querySelector('.bg-gather,.bg-hearts')&&old.decoration.isConnected,
            graphics:window.profileGraphicsProbe,stats:window.__inkStats(),
            mediaCount:host.querySelectorAll('.ink-canvas,.ink-static-frame').length,
            csrf:document.body.dataset.csrf,
            validCsrf:[...document.querySelectorAll('input[name=csrf]')].every(el=>el.value===document.body.dataset.csrf)};
        }""")
        self.assertEqual(state["origin"], initial["origin"], "Вкладка заново загрузила документ и фон")
        self.assertTrue(state["kept"], "Узел фона, графика или контроллер были заменены")
        self.assertEqual(state["graphics"], initial["graphics"], "Графика инициализирована повторно")
        self.assertEqual(state["mediaCount"], 1)
        self.assertEqual(state["stats"]["backend"], initial["backend"])
        self.assertTrue(state["stats"]["firstFrameReady"])
        self.assertNotEqual(state["csrf"], "устаревший-token")
        self.assertTrue(state["validCsrf"])

    def wait_completed(self, page, *, tab, number=1, previous_loads=0):
        # Подмена body предшествует turbo:load; smooth-scroll к якорю может
        # продолжаться после него. Реальные клики ждут обе стадии перехода.
        page.wait_for_function("""({tab,number,previous}) => {
          const loads=window.profileNavigationLifecycle.loads;
          if(loads.length<=previous)return false;
          const url=new URL(loads[loads.length-1]);
          return url.searchParams.get('tab')===tab && Number(url.searchParams.get('page')||1)===number;
        }""", arg={"tab": tab, "number": number, "previous": previous_loads}, timeout=TRANSITION_TIMEOUT)
        # При уменьшении страницы браузер ограничивает scrollTop без scrollend.
        # Проверяем устойчивое положение непосредственно, а не ждём это событие.
        page.evaluate("""() => new Promise(resolve => {
          let x=NaN,y=NaN,stable=0;
          function frame() {
            if(scrollX===x && scrollY===y)stable++;else stable=0;
            x=scrollX;y=scrollY;
            if(stable>=3)resolve();else requestAnimationFrame(frame);
          }
          requestAnimationFrame(frame);
        })""")

    def run_tabs(self, *, authenticated=False, force_main=False, mobile=False, skin="friends"):
        context = self.browser.new_context(
            viewport={"width": 390 if mobile else 1000, "height": 800},
            is_mobile=mobile, has_touch=mobile,
            reduced_motion="reduce" if mobile else "no-preference",
            color_scheme="dark" if authenticated or mobile else "light",
        )
        self.addCleanup(context.close)
        if authenticated:
            context.add_cookies([self.cookie])
        context.add_init_script("(" + PROBE + ")()")
        if force_main:
            context.add_init_script("window.__INK_FORCE_MAIN=true")
        page = context.new_page()
        errors, widget_requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        widget_path = f"/u/{self.uid}/date/{self.wanted}/widget"
        page.on("request", lambda request: widget_requests.append(request)
                if urlparse(request.url).path == widget_path else None)
        page.goto(self.backend.url + f"/u/{self.uid}?tab=events&skin={skin}")
        self.wait_completed(page, tab="events")
        page.wait_for_function("() => window.__inkStats && window.__inkStats().firstFrameReady")
        if authenticated and not mobile:
            page.wait_for_function("() => window.__inkStats().mode==='interactive'")
        initial = page.evaluate("""() => {
          const host=document.querySelector('.bg-smoke');
          window.keptProfileBackground={host,controller:host.__d4yInkController,
            media:host.querySelector('.ink-canvas,.ink-static-frame'),
            wrapper:document.getElementById('profile-background'),decoration:document.querySelector('.bg-gather,.bg-hearts')};
          return {origin:performance.timeOrigin,graphics:{...window.profileGraphicsProbe},backend:window.__inkStats().backend};
        }""")
        self.assertEqual(initial["backend"], "poster" if mobile else "main" if force_main else "worker")
        expect(page.locator(".pub-card")).to_have_count(12)
        expect(page.locator("#profileCollection")).not_to_contain_text("Секретное событие")

        def navigate(tab, action, *, number=1):
            def matches(url):
                parts = urlparse(url)
                query = parse_qs(parts.query)
                return parts.path == f"/u/{self.uid}" and query.get("tab") == [tab] and query.get("page", ["1"]) == [str(number)]

            previous_body = page.query_selector("body")
            previous_loads = page.evaluate("profileNavigationLifecycle.loads.length")
            with page.expect_response(lambda response: response.request.method == "GET" and matches(response.url),
                                      timeout=TRANSITION_TIMEOUT) as loaded:
                action()
            self.assertEqual(loaded.value.status, 200)
            page.wait_for_url(matches, timeout=TRANSITION_TIMEOUT)
            page.wait_for_function("body => document.body !== body", arg=previous_body, timeout=TRANSITION_TIMEOUT)
            self.wait_completed(page, tab=tab, number=number, previous_loads=previous_loads)
            expect(page.locator("#profileCollection")).to_have_class(re.compile(rf"\bprofile-tab-{tab}\b"), timeout=TRANSITION_TIMEOUT)
            self.assertEqual(parse_qs(urlparse(page.url).query).get("skin"), [skin])
            self.assert_retained(page, initial)

        page.evaluate("document.body.dataset.csrf='устаревший-token'")
        navigate("want", lambda: page.locator(".profile-tabs a").filter(has_text="Хочу сходить").click())
        expect(page.locator(".pub-card")).to_have_count(2 if authenticated else 1)
        if not authenticated:
            expect(page.locator("#profileCollection")).not_to_contain_text("Закрытый план")

        def open_widget():
            with page.expect_response(lambda response: response.request.method == "GET" and urlparse(response.url).path == widget_path,
                                      timeout=TRANSITION_TIMEOUT) as widget:
                page.locator(".pub-card").filter(has_text="План с друзьями").click()
            self.assertEqual(widget.value.status, 200)
            dialog = page.get_by_role("dialog", name="Просмотр события")
            expect(dialog).to_be_visible()
            expect(dialog).to_contain_text("План с друзьями", timeout=TRANSITION_TIMEOUT)
            if not authenticated:
                expect(dialog).to_contain_text("Войти или зарегистрироваться")
            page.get_by_role("button", name="Закрыть", exact=True).click()
            expect(dialog).to_be_hidden()
            self.assert_retained(page, initial)

        open_widget()
        navigate("reviews", lambda: page.locator(".profile-tabs a").filter(has_text="Отзывы").click())
        expect(page.locator(".review-card")).to_have_count(2 if authenticated else 1)
        expect(page.locator("#profileCollection")).to_contain_text("Отличный вечер")
        if not authenticated:
            expect(page.locator("#profileCollection")).not_to_contain_text("Личный отзыв")
            expect(page.locator(".review-menu")).to_have_count(0)
        navigate("want", page.go_back)
        open_widget()
        self.assertEqual(len(widget_requests), 2, "Повторные handlers открыли виджет несколько раз")
        navigate("reviews", page.go_forward)
        navigate("events", lambda: page.locator(".profile-tabs a").filter(has_text="Коллекция событий").click())
        expect(page.locator(".pub-card")).to_have_count(12)
        navigate("events", lambda: page.get_by_role("link", name="Следующая страница", exact=True).click(), number=2)
        expect(page.locator(".pub-card")).to_have_count(1)
        expect(page.locator(".pub-card")).to_contain_text("Событие 00")
        self.assert_retained(page, initial)
        navigate("events", page.go_back)
        expect(page.locator(".pub-card")).to_have_count(12)
        self.assert_retained(page, initial)
        self.assertEqual(errors, [])

    def test_anonymous_profile_tabs_keep_worker_and_private_data_hidden(self):
        self.run_tabs()

    def test_owner_profile_tabs_keep_main_graphics_and_widget_handlers(self):
        self.run_tabs(authenticated=True, force_main=True, skin="romantic")

    def test_mobile_profile_tabs_keep_reduced_motion_poster(self):
        self.run_tabs(mobile=True, skin="romantic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
