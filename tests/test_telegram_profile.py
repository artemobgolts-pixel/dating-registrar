#!/usr/bin/env python3
"""Focused regressions for Telegram CTA and the profile service tile."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote_plus

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))

_DATA = tempfile.TemporaryDirectory(prefix="date4you-telegram-profile-")
os.environ.update({
    "DATA_DIR": _DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "telegram-profile.test",
    "SECRET_KEY": "telegram-profile-test-secret",
    "TG_BOT_TOKEN": "",
    "TG_BOT_USERNAME": "date4you_telegram_profile_bot",
})

import admin_routes  # noqa: E402
import db  # noqa: E402


STAMP = "2030-01-01T10:00:00"


def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    return conn


class TelegramProfileUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Environment(loader=FileSystemLoader(APP / "templates"))

    def test_profile_renders_a_fourth_telegram_service_for_both_states(self):
        profile = (APP / "templates/admin/profile.html").read_text("utf-8")
        self.assertIn('{% include "admin/_telegram_service.html" %}', profile)

        template = self.env.get_template("admin/_telegram_service.html")
        unlinked = template.render(user={"telegram_id": None}, csrf="csrf-token")
        self.assertIn("admin-icon-telegram", unlinked)
        self.assertIn("social-state-add", unlinked)
        self.assertIn("data-tg-connect", unlinked)
        self.assertIn('data-return-to="/admin/profile"', unlinked)
        self.assertIn('aria-label="Привязать Telegram"', unlinked)
        self.assertNotIn("/admin/profile/telegram/unlink", unlinked)

        linked = template.render(user={"telegram_id": 770001}, csrf="csrf-token")
        self.assertIn("admin-icon-telegram", linked)
        self.assertIn("social-state-remove", linked)
        self.assertIn('/admin/profile/telegram/unlink', linked)
        self.assertIn('name="csrf" value="csrf-token"', linked)
        self.assertIn('aria-label="Отвязать Telegram"', linked)
        self.assertNotIn("data-tg-connect", linked)

        legacy = template.render(user={"telegram_id": 0}, csrf="csrf-token")
        self.assertIn("social-state-remove", legacy)
        self.assertNotIn("data-tg-connect", legacy)

    def test_compact_telegram_trigger_keeps_its_icon_during_link_progress(self):
        auth = (APP / "static/auth.js").read_text("utf-8")
        self.assertIn('box.matches("[data-tg-connect]")', auth)
        self.assertIn('btn.hasAttribute("data-tg-compact")', auth)
        self.assertIn("setConnectLabel", auth)


class TelegramProfileRouteTests(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.user_id = int(self.conn.execute(
            "INSERT INTO users(telegram_id,tg_username,display_name,bot_linked,created_at) "
            "VALUES(770001,'telegram_user','Пользователь',1,?)",
            (STAMP,),
        ).lastrowid)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def request(self):
        user = self.conn.execute(
            "SELECT * FROM users WHERE id=?", (self.user_id,),
        ).fetchone()
        return SimpleNamespace(state=SimpleNamespace(user=user))

    def telegram_state(self):
        return self.conn.execute(
            "SELECT telegram_id,tg_username,bot_linked FROM users WHERE id=?",
            (self.user_id,),
        ).fetchone()

    def test_unlink_refuses_to_remove_the_only_login_method(self):
        response = admin_routes.profile_telegram_unlink(
            self.request(), conn=self.conn,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("единственный", unquote_plus(response.headers["location"]))
        self.assertEqual(tuple(self.telegram_state()), (770001, "telegram_user", 1))

    def test_unlink_clears_telegram_when_oauth_login_remains(self):
        self.conn.execute(
            "INSERT INTO oauth_accounts(provider,provider_uid,user_id,email,created_at) "
            "VALUES('google','google-770001',?,NULL,?)",
            (self.user_id, STAMP),
        )
        self.conn.execute(
            "INSERT INTO login_codes(code,status,created_at,purpose,user_id) "
            "VALUES('pending-link','pending',?,'link',?)",
            (STAMP, self.user_id),
        )
        self.conn.commit()

        response = admin_routes.profile_telegram_unlink(
            self.request(), conn=self.conn,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(tuple(self.telegram_state()), (None, None, 0))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM login_codes WHERE user_id=? AND purpose='link'",
                (self.user_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM oauth_accounts WHERE user_id=?",
                (self.user_id,),
            ).fetchone()[0],
            1,
        )


class TelegramConnectGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - optional dev dependency
            raise unittest.SkipTest(f"playwright unavailable: {exc!r}") from exc
        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - browser may be absent
            if getattr(cls, "playwright", None):
                cls.playwright.stop()
            raise unittest.SkipTest(f"playwright chromium unavailable: {exc!r}") from exc

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()

    def test_banner_and_profile_cta_use_a_full_width_readable_action_row(self):
        env = Environment(loader=FileSystemLoader(APP / "templates"))
        template = env.get_template("admin/_telegram_connect.html")
        css = (APP / "static/admin.css").read_text("utf-8")

        cases = (
            ("telegram-connect-banner-inner", "flash telegram-connect-banner", 1280),
            ("telegram-connect-banner-inner", "flash telegram-connect-banner", 390),
            ("telegram-connect-profile", "card", 1280),
            ("telegram-connect-profile", "card", 390),
        )
        for surface, wrapper, viewport_width in cases:
            with self.subTest(surface=surface, viewport_width=viewport_width):
                markup = template.render(
                    user={"bot_linked": 0},
                    request=SimpleNamespace(url=SimpleNamespace(path="/admin/profile")),
                    telegram_connect_class=surface,
                    telegram_connect_description=(
                        "Получай уведомления о выборе событий, вопросах и "
                        "предложениях прямо в Telegram."
                    ),
                )
                page = self.browser.new_page(
                    viewport={"width": viewport_width, "height": 700},
                )
                self.addCleanup(page.close)
                page.set_content(
                    f'<style>{css}</style><html data-skin="friends" '
                    f'data-theme="light"><body><main class="wrap">'
                    f'<div class="{wrapper}" style="max-width:540px">'
                    f'{markup}</div></main></body></html>'
                )
                geometry = page.evaluate("""() => {
                  const ui = document.querySelector('.telegram-connect-ui').getBoundingClientRect();
                  const heading = document.querySelector('.telegram-connect-heading').getBoundingClientRect();
                  const button = document.querySelector('.telegram-connect-button');
                  const buttonRect = button.getBoundingClientRect();
                  const description = document.querySelector('.telegram-connect-description').getBoundingClientRect();
                  return {
                    uiLeft: ui.left,
                    uiRight: ui.right,
                    headingBottom: heading.bottom,
                    buttonLeft: buttonRect.left,
                    buttonRight: buttonRect.right,
                    buttonTop: buttonRect.top,
                    buttonHeight: buttonRect.height,
                    buttonFont: Number.parseFloat(getComputedStyle(button).fontSize),
                    descriptionLeft: description.left,
                  };
                }""")
                self.assertGreaterEqual(geometry["buttonHeight"], 44)
                self.assertGreaterEqual(geometry["buttonFont"], 14)
                self.assertGreaterEqual(geometry["buttonTop"], geometry["headingBottom"])
                self.assertAlmostEqual(
                    geometry["buttonLeft"], geometry["uiLeft"], delta=1,
                )
                self.assertAlmostEqual(
                    geometry["buttonRight"], geometry["uiRight"], delta=1,
                )
                self.assertAlmostEqual(
                    geometry["descriptionLeft"], geometry["uiLeft"], delta=1,
                )

    def test_telegram_tile_uses_link_flow_without_replacing_its_logo(self):
        env = Environment(loader=FileSystemLoader(APP / "templates"))
        markup = env.get_template("admin/_telegram_service.html").render(
            user={"telegram_id": None}, csrf="csrf-token",
        )
        page = self.browser.new_page(viewport={"width": 390, "height": 700})
        self.addCleanup(page.close)
        page.set_content(markup)
        page.evaluate("""() => {
          window.open = () => ({
            closed: false,
            opener: null,
            close() {},
            location: { replace(url) { window.__telegramUrl = url; } },
          });
          window.fetch = (url, options) => {
            window.__request = { url, method: options && options.method };
            return Promise.resolve({
              ok: true,
              json: () => Promise.resolve({
                code: 'telegram-profile-code',
                url: 'https://t.me/date4you_bot?start=telegram-profile-code',
              }),
            });
          };
        }""")
        page.add_script_tag(
            content=(APP / "static/auth.js").read_text("utf-8"),
        )
        page.locator("[data-tg-connect]").click()
        page.wait_for_function(
            "document.querySelector('[data-tg-connect]').dataset.tgDirect",
        )

        state = page.evaluate("""() => ({
          request: window.__request,
          telegramUrl: window.__telegramUrl,
          hasLogo: Boolean(document.querySelector('.admin-icon-telegram')),
          label: document.querySelector('[data-tg-connect]').getAttribute('aria-label'),
        })""")
        self.assertEqual(
            state["request"],
            {"url": "/auth/start?return_to=%2Fadmin%2Fprofile", "method": "POST"},
        )
        self.assertEqual(
            state["telegramUrl"],
            "https://t.me/date4you_bot?start=telegram-profile-code",
        )
        self.assertTrue(state["hasLogo"])
        self.assertEqual(state["label"], "Открыть Telegram")


if __name__ == "__main__":
    unittest.main(verbosity=2)
