#!/usr/bin/env python3
"""Контракты доступности и прямого управления Apple-design слоя."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


class PublicDialogAccessibilityTests(unittest.TestCase):
    def test_every_public_dialog_has_an_accessible_name(self):
        for relative in ("templates/public/category.html", "templates/public/share.html"):
            source = (APP / relative).read_text("utf-8")
            dialogs = re.findall(r"<dialog\b([^>]*)>", source, re.DOTALL)
            self.assertGreaterEqual(len(dialogs), 6, relative)
            for attributes in dialogs:
                with self.subTest(template=relative, dialog=attributes):
                    labelled = re.search(r'aria-labelledby="([^"]+)"', attributes)
                    label = re.search(r'aria-label="([^"]+)"', attributes)
                    self.assertTrue(labelled or label, attributes)
                    if labelled:
                        self.assertIn(f'id="{labelled.group(1)}"', source)

    def test_lightbox_and_gallery_are_keyboard_and_screen_reader_ready(self):
        for relative in ("templates/public/category.html", "templates/public/share.html"):
            source = (APP / relative).read_text("utf-8")
            with self.subTest(template=relative):
                self.assertIn('<dialog class="lightbox" id="lightbox"', source)
                self.assertIn('aria-labelledby="lbTitle"', source)
                self.assertIn('role="button" tabindex="0" draggable="false"', source)
                self.assertIn('aria-haspopup="dialog" aria-controls="lightbox"', source)
                self.assertIn('id="lbCount" aria-live="polite" aria-atomic="true"', source)
                self.assertIn('id="toast" role="status" aria-live="polite"', source)

    def test_login_has_a_real_heading_and_an_explicit_return_path(self):
        source = (APP / "templates/auth/login.html").read_text("utf-8")
        self.assertIn('<h1 class="login-brand">', source)
        self.assertNotIn('<h1 class="login-mark" aria-hidden="true">', source)
        self.assertIn('{% if next_url %}', source)
        self.assertIn('class="oauth-btn login-return" href="{{ next_url }}"', source)
        self.assertIn("Вернуться без входа", source)

    def test_shared_review_report_is_labelled_and_available_without_login(self):
        template = (APP / "templates/public/profile_review.html").read_text("utf-8")
        script = (APP / "static/public_review.js").read_text("utf-8")
        self.assertIn('aria-describedby="reviewReportDescription"', template)
        self.assertIn('id="reviewReportDescription">Вход не требуется.', template)
        self.assertIn('<label class="sr-only" for="reportReason">', template)
        self.assertNotIn("if (!authenticated)", script)


class FluidInteractionContractTests(unittest.TestCase):
    def test_public_gestures_track_project_resist_and_reduce(self):
        source = (APP / "static/guest.js").read_text("utf-8")
        self.assertIn("GESTURE_HYSTERESIS = 10", source)
        self.assertIn('window.matchMedia("(prefers-reduced-motion: reduce)")', source)
        self.assertIn("function projectedDistance", source)
        self.assertIn("function rubberband", source)
        self.assertIn("function recentVelocity", source)
        self.assertIn('addEventListener("pointerdown"', source)
        self.assertIn("setPointerCapture", source)
        self.assertIn("projectedDistance(velocity)", source)
        self.assertIn("springValue", source)
        self.assertNotIn('addEventListener("touchstart"', source)
        self.assertNotIn('addEventListener("touchend"', source)

    def test_public_async_actions_expose_busy_state_and_auth_context(self):
        source = (APP / "static/guest.js").read_text("utf-8")
        self.assertIn('control.setAttribute("aria-busy", "true")', source)
        for action in ("vote", "question", "propose"):
            with self.subTest(action=action):
                self.assertRegex(source, rf"\b{action}:\s*\{{")
        self.assertIn("{ allowAnonymous: true }", source)
        self.assertIn("Вход не требуется", (APP / "templates/public/share.html").read_text("utf-8"))
        self.assertIn("openModal(lb, img", source)
        self.assertIn('lb.addEventListener("close"', source)

    def test_operator_drawer_is_modal_focus_safe_and_draggable(self):
        script = (APP / "static/operator.js").read_text("utf-8")
        template = (APP / "templates/operator/base.html").read_text("utf-8")
        self.assertIn('data-op-nav-edge', template)
        self.assertIn('data-op-main', template)
        self.assertIn('main.inert = true', script)
        self.assertIn('sidebar.setAttribute("aria-modal", "true")', script)
        self.assertIn('event.key === "Tab"', script)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn("setPointerCapture", script)
        self.assertIn("projectedDistance(velocity)", script)
        self.assertIn("rubberband", script)
        self.assertIn('body.classList.add("op-nav-gesturing")', script)


class PublicPrivacyNavigationContractTests(unittest.TestCase):
    def setUp(self):
        self.category = (APP / "templates/public/category.html").read_text("utf-8")
        self.share = (APP / "templates/public/share.html").read_text("utf-8")
        self.script = (APP / "static/guest.js").read_text("utf-8")

    def test_public_templates_remain_valid_jinja(self):
        environment = Environment()
        for name, source in (("category", self.category), ("share", self.share)):
            with self.subTest(template=name):
                environment.parse(source)

    def test_account_disclosure_and_owner_edit_have_clear_semantics(self):
        for name, source in (("category", self.category), ("share", self.share)):
            with self.subTest(template=name):
                self.assertIn('<details class="public-account-menu">', source)
                self.assertIn('<summary aria-label="Меню аккаунта — {{', source)
                self.assertIn('class="public-account-popover"', source)
                self.assertIn('<a href="/admin/">Кабинет</a>', source)
                self.assertIn('<a href="/admin/profile">Профиль</a>', source)
                self.assertRegex(
                    source,
                    r'<form method="post" action="/admin/logout">\s*'
                    r'<input type="hidden" name="csrf" value="\{\{ csrf \}\}">',
                )
        self.assertIn('aria-label="Редактировать подборку «{{ cat[\'name\'] }}»"',
                      self.category)
        self.assertIn('aria-label="Редактировать событие «{{ d.name }}»"', self.share)

    def test_roster_visibility_is_gated_in_markup_and_live_updates(self):
        for name, source in (("category", self.category), ("share", self.share)):
            with self.subTest(template=name):
                self.assertRegex(source, r"set show_roster = show_participants and\s*\(")
                self.assertIn("if show_roster and d.participants", source)
                self.assertIn("'участников' if show_roster else 'голосов'", source)
        self.assertIn("if (update.show_participants === false) return;", self.script)
        self.assertIn(
            'rosterLabel.textContent = update.show_participants ? "участников" : "голосов";',
            self.script,
        )
        self.assertNotIn("показан участникам этой подборки", self.script)

    def test_share_want_visibility_remove_and_category_context_are_explicit(self):
        self.assertIn('class="collection-context" href="/c/{{ active_category.link_token }}"',
                      self.share)
        self.assertIn("active_category_event_count | plural", self.share)
        save_form = re.search(
            r'<form method="post" action="/d/\{\{ token \}\}/want" class="want-form">'
            r'(.*?)</form>',
            self.share,
            re.DOTALL,
        )
        remove_form = re.search(
            r'<form method="post" action="/d/\{\{ token \}\}/want" '
            r'class="want-remove-form">(.*?)</form>',
            self.share,
            re.DOTALL,
        )
        self.assertIsNotNone(save_form)
        self.assertIsNotNone(remove_form)
        self.assertIn('name="visibility" value="private"', save_form.group(1))
        self.assertIn('name="visibility" value="public"', save_form.group(1))
        self.assertNotIn('name="visibility"', remove_form.group(1))
        self.assertIn("Убрать из «Хочу сходить»", remove_form.group(1))

    def test_telegram_prompt_is_deferred_then_revealed_without_focus_theft(self):
        self.assertIn("not viewer_has_vote %}hidden data-deferred-notify", self.category)
        self.assertIn("hidden data-deferred-notify", self.share)
        self.assertIn('document.querySelectorAll("[data-deferred-notify]")', self.script)
        self.assertIn("box.hidden = false", self.script)
        self.assertIn('box.classList.add("is-revealed")', self.script)
        self.assertIn("notifyRevealed", self.script)
        reveal_block = re.search(
            r'document\.querySelectorAll\("\[data-deferred-notify\]"\).*?\n\s*\}\);',
            self.script,
            re.DOTALL,
        )
        self.assertIsNotNone(reveal_block)
        self.assertNotIn(".focus(", reveal_block.group(0))


class FluidInteractionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - optional dev dependency
            raise unittest.SkipTest(f"playwright недоступен: {exc!r}") from exc
        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - browser may be absent
            if getattr(cls, "playwright", None):
                cls.playwright.stop()
            raise unittest.SkipTest(f"playwright chromium недоступен: {exc!r}") from exc

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()

    def test_native_lightbox_moves_focus_restores_it_and_hands_off_velocity(self):
        page = self.browser.new_page(viewport={"width": 500, "height": 700})
        self.addCleanup(page.close)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        page.set_content(f"""
          <body data-token="test" data-auth="1" data-csrf="csrf" data-skin="friends">
            <style>
              .gallery {{ display:flex; width:320px; height:180px; overflow:hidden; }}
              .gallery img {{ width:320px; height:180px; flex:none; }}
              dialog.lightbox {{ width:100vw; max-width:none; height:100vh; max-height:none;
                margin:0; padding:0; border:0; background:#111; color:white; }}
              dialog.lightbox[open] {{ display:flex; align-items:center; justify-content:center; }}
              .lightbox img {{ width:300px; height:220px; }}
            </style>
            <div id="toast"></div>
            <dialog id="askDlg"><form id="askForm"><button type="submit">ok</button></form></dialog>
            <button id="askCancel" type="button"></button>
            <dialog id="reportDlg"><form id="reportForm"><button type="submit">ok</button></form></dialog>
            <button id="reportCancel" type="button"></button>
            <dialog id="timeDlg"><form id="timeForm">
              <input id="timeStart" type="datetime-local"><input id="timeEnd" type="datetime-local">
              <button type="submit">ok</button></form></dialog>
            <button id="timeCancel" type="button"></button>
            <dialog id="calDlg"></dialog><button id="calCancel" type="button"></button>
            <a id="calGoogle"></a><a id="calIcs"></a>
            <article class="card"><h2 class="title">Тест</h2><div class="gal-wrap">
              <div class="gallery"><img src="{pixel}" data-full="{pixel}"><img src="{pixel}" data-full="{pixel}"></div>
            </div></article>
            <dialog class="lightbox" id="lightbox" aria-labelledby="lbTitle">
              <h2 id="lbTitle">Фото</h2><button id="lbX" type="button">Закрыть</button>
              <button id="lbPrev" type="button">Назад</button><img src="{pixel}">
              <button id="lbNext" type="button">Вперёд</button><div id="lbCount"></div>
            </dialog>
          </body>
        """)
        page.add_script_tag(content=(APP / "static/ui.js").read_text("utf-8"))
        page.add_script_tag(content=(APP / "static/guest.js").read_text("utf-8"))

        first = page.locator(".gallery img").first
        first.focus()
        page.keyboard.press("Enter")
        page.wait_for_selector("#lightbox[open]")
        page.wait_for_function("document.activeElement.id === 'lbX'")
        self.assertEqual(page.locator("#lbCount").text_content(), "1/2")

        page.keyboard.press("ArrowRight")
        page.wait_for_function("document.querySelector('#lbCount').textContent === '2/2'")
        page.keyboard.press("Escape")
        page.wait_for_function("!document.querySelector('#lightbox').open")
        page.wait_for_function("document.activeElement === document.querySelector('.gallery img')")

        first.focus()
        page.keyboard.press("Enter")
        page.wait_for_selector("#lightbox[open]")
        box = page.locator("#lightbox img").bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.9, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] - box["width"] * 0.1, box["y"] + box["height"] / 2, steps=5)
        page.mouse.up()
        page.wait_for_function(
            "document.querySelector('#lbCount').textContent === '2/2'", timeout=5000)
        page.wait_for_function(
            "!document.querySelector('#lightbox img').style.transform", timeout=5000)
        self.assertEqual(errors, [])

    def test_operator_drawer_traps_and_restores_focus_and_accepts_edge_drag(self):
        page = self.browser.new_page(viewport={"width": 390, "height": 760})
        self.addCleanup(page.close)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content("""
          <style>
            .op-sidebar { position:fixed; left:0; top:0; width:280px; height:100vh;
              transform:translateX(calc(-100% - 24px)); }
            body.op-nav-open .op-sidebar { transform:translateX(0); }
            .op-nav-overlay { display:none; position:fixed; inset:0; }
            body.op-nav-open .op-nav-overlay { display:block; }
            .op-nav-edge { position:fixed; inset:0 auto 0 0; width:24px; }
          </style>
          <aside class="op-sidebar" id="operatorSidebar" aria-label="Навигация">
            <a id="firstNav" href="#one">Первый</a><a href="#two">Второй</a>
            <button id="lastNav" type="button">Последний</button>
          </aside>
          <button class="op-nav-overlay" data-op-nav-close type="button">Закрыть</button>
          <div class="op-nav-edge" data-op-nav-edge aria-hidden="true"></div>
          <div class="op-main" data-op-main><button id="menu" type="button"
            data-op-nav-toggle aria-controls="operatorSidebar" aria-expanded="false">Меню</button></div>
        """)
        page.add_script_tag(content=(APP / "static/operator.js").read_text("utf-8"))

        page.locator("#menu").click()
        page.wait_for_function("document.body.classList.contains('op-nav-open')")
        self.assertEqual(page.locator("#operatorSidebar").get_attribute("aria-modal"), "true")
        self.assertTrue(page.locator("[data-op-main]").evaluate("node => node.inert"))
        page.locator("#lastNav").focus()
        page.keyboard.press("Tab")
        self.assertEqual(page.evaluate("document.activeElement.id"), "firstNav")
        page.keyboard.press("Escape")
        page.wait_for_function("!document.body.classList.contains('op-nav-open')")
        self.assertEqual(page.evaluate("document.activeElement.id"), "menu")
        self.assertFalse(page.locator("[data-op-main]").evaluate("node => node.inert"))

        page.mouse.move(5, 300)
        page.mouse.down()
        page.mouse.move(260, 300, steps=6)
        page.mouse.up()
        page.wait_for_function("document.body.classList.contains('op-nav-open')")
        page.mouse.move(220, 320)
        page.mouse.down()
        page.mouse.move(12, 320, steps=6)
        page.mouse.up()
        page.wait_for_function("!document.body.classList.contains('op-nav-open')")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
