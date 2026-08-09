#!/usr/bin/env python3
"""Контракт хранения и валидации независимых оформлений."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import ImageChops
from starlette.datastructures import UploadFile


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

# db создаёт DATA_DIR при импорте, поэтому изолируем его заранее.
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-theme-import-")
os.environ.update(
    {
        "DATA_DIR": _IMPORT_DATA.name,
        "COOKIE_SECURE": "false",
        "DOMAIN": "theme.test",
        "SECRET_KEY": "theme-test-secret",
        "TG_BOT_TOKEN": "",
        "TG_CHAT_ID": "",
        "TG_BOT_USERNAME": "date4you_theme_test_bot",
        "TG_WEBHOOK_SECRET": "theme-hook-secret",
        "OPERATOR_TG_IDS": "26002",
    }
)

import admin_routes  # noqa: E402
import appearance  # noqa: E402
import db  # noqa: E402
import images  # noqa: E402


class ThemeMigrationTests(unittest.TestCase):
    def test_v26_preserves_old_categories_and_defaults_new_records_to_friends(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    display_name TEXT
                );
                CREATE TABLE categories (
                    id INTEGER PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL
                );
                INSERT INTO users(id, display_name) VALUES(1, 'Старый пользователь');
                INSERT INTO categories(id, owner_id, name)
                    VALUES(1, 1, 'Существующая категория');
                """
            )
            conn.executescript(db.MIGRATIONS[26])

            self.assertEqual(
                conn.execute(
                    "SELECT category_skin FROM categories WHERE id=1"
                ).fetchone()[0],
                "romantic",
            )
            self.assertEqual(
                conn.execute("SELECT admin_skin FROM users WHERE id=1").fetchone()[0],
                "friends",
            )

            conn.execute("INSERT INTO users(id, display_name) VALUES(2, 'Новый')")
            conn.execute(
                "INSERT INTO categories(id, owner_id, name) VALUES(2, 2, 'Новая')"
            )
            self.assertEqual(
                conn.execute(
                    "SELECT category_skin FROM categories WHERE id=2"
                ).fetchone()[0],
                "friends",
            )
            self.assertEqual(
                conn.execute("SELECT admin_skin FROM users WHERE id=2").fetchone()[0],
                "friends",
            )

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE categories SET category_skin='evil' WHERE id=2")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE users SET admin_skin='evil' WHERE id=2")
        finally:
            conn.close()

    def test_fresh_schema_uses_friends_defaults(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            uid = conn.execute(
                "INSERT INTO users(telegram_id, display_name, created_at) VALUES(?,?,?)",
                (26001, "Новый пользователь", "2030-01-01T00:00:00"),
            ).lastrowid
            cid = conn.execute(
                "INSERT INTO categories(owner_id, name, created_at) VALUES(?,?,?)",
                (uid, "Новая категория", "2030-01-01T00:00:00"),
            ).lastrowid
            self.assertEqual(
                conn.execute(
                    "SELECT category_skin FROM categories WHERE id=?", (cid,)
                ).fetchone()[0],
                "friends",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT admin_skin FROM users WHERE id=?", (uid,)
                ).fetchone()[0],
                "friends",
            )
        finally:
            conn.close()


class ThemePreviewTests(unittest.TestCase):
    def test_themes_use_dedicated_default_previews(self):
        static = APP / "static"
        paths = [static / "og-friends.jpg", static / "og-default.jpg"]
        for path in paths:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())
                self.assertLess(path.stat().st_size, 300_000)
                with images.Image.open(path) as preview:
                    self.assertEqual(preview.format, "JPEG")
                    self.assertEqual(preview.size, (1200, 630))
        with images.Image.open(paths[0]) as friends, images.Image.open(paths[1]) as romantic:
            self.assertIsNotNone(ImageChops.difference(friends, romantic).getbbox())

        for relative in (
            "templates/public/category.html",
            "templates/public/share.html",
            "templates/public/profile_review.html",
            "templates/admin/dashboard.html",
            "templates/admin/category_detail.html",
        ):
            with self.subTest(template=relative):
                source = (APP / relative).read_text("utf-8")
                self.assertIn("og-default.jpg", source)
                self.assertIn("og-friends.jpg", source)

        for filename in ("date-default-friends.jpg", "date-default.jpg"):
            with self.subTest(filename=filename), images.Image.open(
                    static / filename) as placeholder:
                self.assertEqual(placeholder.format, "JPEG")
                self.assertEqual(placeholder.size, (640, 360))

    def test_browser_favicons_are_dedicated_tightly_cropped_assets(self):
        static = APP / "static"
        for filename in ("favicon-standard.png", "favicon-romantic.png"):
            with self.subTest(filename=filename), images.Image.open(
                    static / filename) as icon:
                self.assertEqual(icon.size, (128, 128))
                self.assertEqual(icon.mode, "RGBA")
                alpha = icon.getchannel("A")
                self.assertEqual(alpha.getextrema(), (0, 255))
                self.assertGreater(alpha.histogram()[0], 128 * 128 * 0.2)
                # Знак плотно заполняет favicon, но не касается его краёв.
                visible = alpha.point(lambda value: 255 if value >= 8 else 0).getbbox()
                self.assertIsNotNone(visible)
                left, top, right, bottom = visible
                self.assertLessEqual(left, 10)
                self.assertLessEqual(top, 10)
                self.assertGreaterEqual(right, 118)
                self.assertGreaterEqual(bottom, 116)

        login = (APP / "templates/auth/login.html").read_text("utf-8")
        self.assertIn("favicon-standard.png", login)
        self.assertIn("favicon-romantic.png", login)
        self.assertIn('href="{{ asset(\'favicon-standard.png\') }}"', login)

    def test_friends_install_icons_do_not_fall_back_to_romantic_assets(self):
        static = APP / "static"
        expected = {
            "icon-friends-192.png": (192, 192),
            "icon-friends-512.png": (512, 512),
            "apple-touch-icon-friends.png": (180, 180),
        }
        for filename, size in expected.items():
            with self.subTest(filename=filename), images.Image.open(
                    static / filename) as icon:
                self.assertEqual(icon.size, size)
                self.assertEqual(icon.mode, "RGBA")
                # В исходнике были растровые чёрные углы. В install-иконках
                # они должны быть прозрачными, а не чёрной каймой.
                self.assertEqual(icon.getpixel((0, 0))[3], 0)

        with images.Image.open(static / "logo-standard.png") as logo, \
                images.Image.open(static / "icon-friends-512.png") as install:
            self.assertEqual(logo.size, (512, 512))
            self.assertEqual(logo.mode, "RGBA")
            self.assertIsNone(ImageChops.difference(logo, install).getbbox())

        manifest = json.loads((static / "manifest-friends.json").read_text("utf-8"))
        sources = {entry["src"] for entry in manifest["icons"]}
        self.assertEqual(
            {urlsplit(source).path for source in sources},
            {f"/static/{filename}" for filename in expected},
        )
        for source in sources:
            parsed = urlsplit(source)
            filename = Path(parsed.path).name
            version = parse_qs(parsed.query).get("v", [""])[0]
            digest = hashlib.sha256((static / filename).read_bytes()).hexdigest()[:12]
            self.assertEqual(version, digest)
        self.assertEqual(manifest["theme_color"], "#554eae")

    def test_collage_cache_is_separate_for_each_skin(self):
        filenames = ["first.webp", "second.webp"]
        friends = images.og_collage_name(filenames, appearance.FRIENDS)
        romantic = images.og_collage_name(filenames, appearance.ROMANTIC)
        romantic_digest = hashlib.sha256(
            (f"{images.OG_BRAND_VERSION}:romantic\n" +
             "\n".join(f"{filename}|0.5000,0.5000" for filename in filenames)).encode()
        ).hexdigest()[:24]
        self.assertEqual(romantic, f"og_{romantic_digest}.webp")
        self.assertNotEqual(friends, romantic)
        self.assertEqual(
            images.og_collage_name(filenames, "invalid-skin"),
            romantic,
        )

    def test_collage_overlay_is_skin_specific_and_applied(self):
        base = images.Image.new("RGB", (images.OG_W, images.OG_H), "#ffffff")
        romantic = images._draw_og_brand_overlay(base, appearance.ROMANTIC)
        friends = images._draw_og_brand_overlay(base, appearance.FRIENDS)
        self.assertEqual(romantic.size, base.size)
        self.assertEqual(friends.size, base.size)
        self.assertIsNotNone(ImageChops.difference(base, romantic).getbbox())
        self.assertIsNotNone(ImageChops.difference(base, friends).getbbox())
        self.assertIsNotNone(ImageChops.difference(friends, romantic).getbbox())

        # Знак стал примерно на 27% крупнее прежнего, но мягче:
        # игнорируем единичные отклонения от Lanczos по краям матта.
        difference = ImageChops.difference(base, friends)
        visible = difference.convert("L").point(
            lambda value: 255 if value > 2 else 0).getbbox()
        self.assertIsNotNone(visible)
        left, top, right, bottom = visible
        self.assertGreaterEqual(right - left, 375)
        self.assertGreaterEqual(bottom - top, 305)
        strongest = max(high for _low, high in difference.getextrema())
        self.assertGreaterEqual(strongest, 100)
        self.assertLessEqual(strongest, 140)
        self.assertEqual(images.OG_BRAND_MAX_SIZE, (590, 465))
        self.assertEqual(images.OG_BRAND_OPACITY, 0.52)
        self.assertEqual(images.OG_BRAND_REVISION, "10")

        overlay = images.OG_BRAND_OVERLAYS[appearance.FRIENDS]
        with images.Image.open(overlay) as mark:
            self.assertEqual(mark.mode, "RGBA")
            self.assertEqual(mark.getchannel("A").getextrema(), (0, 255))

        category = (APP / "templates/public/category.html").read_text("utf-8")
        self.assertIn("&amp;v={{ preview_revision }}", category)

    def test_preview_url_revision_tracks_every_content_mode(self):
        first = images.og_preview_revision(
            ["a.webp", "b.webp"], appearance.FRIENDS)
        self.assertNotEqual(
            first,
            images.og_preview_revision(["b.webp", "a.webp"], appearance.FRIENDS),
        )
        self.assertNotEqual(
            first,
            images.og_preview_revision(["a.webp"], appearance.FRIENDS),
        )
        focused = [("a.webp", "25% 80%"), ("b.webp", None)]
        self.assertNotEqual(
            first,
            images.og_preview_revision(focused, appearance.FRIENDS),
        )
        self.assertNotEqual(
            images.og_collage_name(focused, appearance.FRIENDS),
            images.og_collage_name(
                [("a.webp", "75% 20%"), ("b.webp", None)],
                appearance.FRIENDS,
            ),
        )
        self.assertNotEqual(
            first,
            images.og_preview_revision(
                ["a.webp", "b.webp"], appearance.ROMANTIC),
        )
        custom = images.og_preview_revision(
            [], appearance.FRIENDS,
            custom_image="custom.webp", custom_focus="50% 50%",
        )
        self.assertNotEqual(first, custom)
        self.assertNotEqual(
            custom,
            images.og_preview_revision(
                [], appearance.FRIENDS,
                custom_image="custom.webp", custom_focus="20% 70%",
            ),
        )
        self.assertNotEqual(
            custom,
            images.og_preview_revision([], appearance.FRIENDS, use_default=True),
        )

    def test_saved_custom_preview_editor_uses_raw_protected_image(self):
        template = (APP / "templates/admin/category_detail.html").read_text("utf-8")
        self.assertIn(
            "{% elif custom_og_available %}/admin/uploads/{{ cat['og_image'] }}?w=960",
            template,
        )
        self.assertIn(
            "custom_og_available and not cat['use_default_preview']",
            template,
        )
        # Списки и публичная карточка продолжают использовать 1200×630 endpoint;
        # только focus-редактору нужен исходник, по которому двигается object-position.
        self.assertIn(
            "{% elif auto_og %}/admin/categories/{{ cat['id'] }}/og-preview",
            template,
        )

    def test_parallel_collage_and_crop_publish_atomically(self):
        """Одинаковый preview разрешено одновременно строить многим воркерам."""
        workers = 8
        with tempfile.TemporaryDirectory(prefix="date4you-og-race-") as tmp:
            root = Path(tmp)
            uploads = root / "uploads"
            cache = root / "cache"
            uploads.mkdir()
            cache.mkdir()
            filename = "parallel-source.webp"
            images.Image.new("RGB", (1800, 900), "#72a58c").save(
                uploads / filename, "WEBP",
            )

            with patch.object(images, "UPLOAD_DIR", uploads), \
                    patch.object(images, "OG_CACHE_DIR", cache):
                collage_barrier = threading.Barrier(workers)
                original_overlay = images._draw_og_brand_overlay

                def delayed_overlay(canvas, skin=appearance.ROMANTIC):
                    collage_barrier.wait(timeout=10)
                    return original_overlay(canvas, skin)

                with patch.object(
                        images, "_draw_og_brand_overlay",
                        side_effect=delayed_overlay):
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        collage_paths = list(pool.map(
                            lambda _index: images.build_og_collage(
                                [(filename, "20% 70%")], appearance.FRIENDS,
                            ),
                            range(workers),
                        ))

                self.assertEqual(len(set(collage_paths)), 1)
                with images.Image.open(collage_paths[0]) as generated:
                    generated.load()
                    self.assertEqual(generated.size, (images.OG_W, images.OG_H))

                crop_barrier = threading.Barrier(workers)
                original_fit = images.ImageOps.fit

                def delayed_fit(*args, **kwargs):
                    crop_barrier.wait(timeout=10)
                    return original_fit(*args, **kwargs)

                with patch.object(images.ImageOps, "fit", side_effect=delayed_fit):
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        crop_paths = list(pool.map(
                            lambda _index: images.build_og_crop(
                                filename, "80% 30%",
                            ),
                            range(workers),
                        ))

                self.assertEqual(len(set(crop_paths)), 1)
                with images.Image.open(crop_paths[0]) as generated:
                    generated.load()
                    self.assertEqual(generated.size, (images.OG_W, images.OG_H))
                self.assertEqual(list(cache.glob("*.tmp")), [])
                self.assertEqual(list(cache.glob(".*.tmp")), [])


class ThemeBrowserInteractionTests(unittest.TestCase):
    """Гонка живёт в View Transition API, поэтому строковый тест JS
    её не видит. Минимальную страницу запускаем в настоящем Chromium."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - зависит от dev-окружения
            raise unittest.SkipTest(f"playwright недоступен: {exc!r}") from exc
        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - браузер может быть не установлен
            if getattr(cls, "playwright", None):
                cls.playwright.stop()
            raise unittest.SkipTest(f"playwright chromium недоступен: {exc!r}") from exc

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()

    def make_page(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        theme_source = (APP / "static/theme.js").read_text("utf-8")
        html = """<!doctype html>
<html lang="ru" data-skin="friends" data-skin-switchable>
<head><meta name="theme-color" content="#554eae"><script src="/theme.js"></script></head>
<body>
  <button id="themeToggle" data-theme-toggle style="width:100px;height:100px">
    Тема <span data-theme-icon></span>
  </button>
  <button id="friendsSkin" data-skin-set="friends">Стандартная</button>
  <button id="romanticSkin" data-skin-set="romantic">Романтическая</button>
</body></html>"""

        def serve(route):
            if route.request.url.endswith("/theme.js"):
                route.fulfill(status=200, content_type="application/javascript",
                              body=theme_source)
            else:
                route.fulfill(status=200, content_type="text/html", body=html)

        page.route("**/*", serve)
        page.goto("https://theme.test/")
        if page.evaluate("typeof document.startViewTransition") != "function":
            page.close()
            self.skipTest("Chromium без View Transition API")
        return page

    def test_double_click_keeps_toggle_parity_during_view_transition(self):
        page = self.make_page()
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "light")

        # Это именно физический dblclick, а не два DOM click(): в Chromium
        # второй click во время снимка может прийти с target=<html>.
        page.dblclick("#themeToggle", delay=5)
        page.wait_for_function("""() =>
          document.cookie.includes('d4y_theme=light') &&
          !document.documentElement.classList.contains('d4y-theme-transition')
        """, timeout=5000)

        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "light")
        self.assertIn("d4y_theme=light", page.evaluate("document.cookie"))
        self.assertEqual(page.get_attribute("#themeToggle", "aria-pressed"), "false")

    def test_mixed_fast_clicks_coalesce_theme_and_skin_independently(self):
        page = self.make_page()
        page.evaluate("""() => {
          document.querySelector('#themeToggle').click();
          document.querySelector('#romanticSkin').click();
          document.querySelector('#themeToggle').click();
        }""")
        page.wait_for_function("""() =>
          document.cookie.includes('d4y_theme=light') &&
          document.cookie.includes('d4y_skin=romantic') &&
          !document.documentElement.classList.contains('d4y-theme-transition')
        """, timeout=5000)

        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "light")
        self.assertEqual(page.evaluate("document.documentElement.dataset.skin"), "romantic")
        cookies = page.evaluate("document.cookie")
        self.assertIn("d4y_theme=light", cookies)
        self.assertIn("d4y_skin=romantic", cookies)

    def test_category_reorder_refreshes_only_auto_preview(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content("""<!doctype html>
          <html><body data-skin="friends" data-csrf="csrf-test">
            <fieldset class="category-skin-setting"><div class="skin-pick">
              <label><input type="radio" name="category_skin"
                            value="friends" checked>Стандартная</label>
              <label><input type="radio" name="category_skin"
                            value="romantic">Романтическая</label>
            </div></fieldset>
            <input id="ogInput" type="file">
            <div id="ogPreview" data-cid="42" data-has-image="0"
                 data-preview-skin="friends" data-auto-image="1"
                 data-default-image="0" data-preview-revision="old-revision"
                 data-friends-src="/friends.jpg" data-romantic-src="/romantic.jpg">
              <div id="ogImgPick"><img id="ogPreviewImg"
                   src="https://theme.test/old.webp"></div>
              <span id="ogFocusHint"></span>
            </div>
            <table><tbody id="catRows" data-cid="42">
              <tr class="drag-row" data-did="2"><td>Второе</td></tr>
              <tr class="drag-row" data-did="1"><td>Первое</td></tr>
            </tbody></table>
          </body></html>
        """)
        page.evaluate("""() => {
          window.__fetchCount = 0;
          window.fetch = () => {
            window.__fetchCount += 1;
            return Promise.resolve({
              ok: true,
              json: () => Promise.resolve({
                ok: true, preview_revision: 'server-revision-' + window.__fetchCount
              })
            });
          };
          window.UI = {
            sortable: (_element, options) => { window.__sortableOptions = options; }
          };
        }""")
        page.add_script_tag(content=(APP / "static/admin.js").read_text("utf-8"))
        page.wait_for_function(
            "() => typeof window.__sortableOptions?.onChange === 'function'"
        )

        page.evaluate("window.__sortableOptions.onChange()")
        page.wait_for_function("""() =>
          document.querySelector('#ogPreview').dataset.previewRevision ===
            'server-revision-1'
        """)
        auto_src = page.locator("#ogPreviewImg").get_attribute("src")
        self.assertIn("/admin/categories/42/og-preview?skin=friends", auto_src)
        self.assertIn("server-revision-1", auto_src)

        # Фиксированное фирменное превью reorder не меняет.
        page.evaluate("""() => {
          const preview = document.querySelector('#ogPreview');
          preview.dataset.autoImage = '0';
          preview.dataset.defaultImage = '1';
          document.querySelector('#ogPreviewImg').src = 'https://theme.test/fixed.webp';
          window.__sortableOptions.onChange();
        }""")
        page.wait_for_function("window.__fetchCount === 2")
        self.assertEqual(
            page.locator("#ogPreviewImg").get_attribute("src"),
            "https://theme.test/fixed.webp",
        )

        # Локально выбранный файл тоже не должен быть затёрт ни сменой skin,
        # ни завершившимся reorder-запросом.
        page.locator("#ogInput").set_input_files({
            "name": "new-preview.webp",
            "mimeType": "image/webp",
            "buffer": b"preview-test",
        })
        selected_src = page.locator("#ogPreviewImg").get_attribute("src")
        self.assertTrue(selected_src.startswith("blob:"))
        self.assertEqual(page.locator("#ogPreview").get_attribute("data-auto-image"), "0")
        self.assertEqual(page.locator("#ogPreview").get_attribute("data-default-image"), "0")
        page.locator('[name="category_skin"][value="romantic"]').check()
        page.evaluate("window.__sortableOptions.onChange()")
        page.wait_for_function("window.__fetchCount === 3")
        self.assertEqual(page.locator("#ogPreviewImg").get_attribute("src"), selected_src)

    def test_custom_preview_click_does_not_move_focus_but_drag_does(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content("""<!doctype html>
          <html><body data-skin="friends" data-csrf="csrf-test">
            <input id="ogInput" type="file">
            <div id="ogPreview" class="has-image" data-cid="42"
                 data-has-image="1" data-preview-skin="friends"
                 data-auto-image="0" data-default-image="0">
              <div id="ogImgPick">
                <img id="ogPreviewImg" draggable="false"
                     style="display:block;width:380px;height:200px;object-fit:cover;object-position:50% 50%">
              </div>
              <span id="ogFocusHint"></span>
            </div>
          </body></html>
        """)
        page.evaluate("""() => {
          window.__focusPosts = 0;
          window.fetch = () => {
            window.__focusPosts += 1;
            return Promise.resolve({
              ok: true,
              json: () => Promise.resolve({ ok: true, preview_revision: 'focus' })
            });
          };
          window.UI = { sortable: () => {} };
        }""")
        page.add_script_tag(content=(APP / "static/admin.js").read_text("utf-8"))
        page.wait_for_function(
            "document.querySelector('#ogPreview').dataset.ready === '1'",
        )

        # Обычный click открывает picker, но не меняет crop и не пишет focus.
        with page.expect_file_chooser():
            page.locator("#ogPreviewImg").click(position={"x": 30, "y": 30})
        self.assertEqual(
            page.locator("#ogPreviewImg").evaluate("img => img.style.objectPosition"),
            "50% 50%",
        )
        self.assertEqual(page.evaluate("window.__focusPosts"), 0)

        # Настоящее перемещение дальше порога обновляет crop и сохраняет его.
        box = page.locator("#ogPreviewImg").bounding_box()
        self.assertIsNotNone(box)
        page.mouse.move(box["x"] + 190, box["y"] + 100)
        page.mouse.down()
        page.mouse.move(box["x"] + 320, box["y"] + 150, steps=4)
        page.mouse.up()
        page.wait_for_function("window.__focusPosts === 1")
        self.assertNotEqual(
            page.locator("#ogPreviewImg").evaluate("img => img.style.objectPosition"),
            "50% 50%",
        )


class ThemeRouteDataTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizer_never_returns_arbitrary_skin(self):
        self.assertEqual(appearance.normalize_skin("friends"), "friends")
        self.assertEqual(appearance.normalize_skin("romantic"), "romantic")
        self.assertEqual(
            appearance.normalize_skin("injected-class", default="romantic"),
            "romantic",
        )
        with self.assertRaises(ValueError):
            appearance.normalize_skin("injected-class", default="also-invalid")

    async def test_json_import_preserves_skin_and_legacy_defaults_to_romantic(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.executescript(db.SCHEMA)
            uid = conn.execute(
                "INSERT INTO users(telegram_id, display_name, is_operator, created_at) "
                "VALUES(?,?,1,?)",
                (26002, "Оператор", "2030-01-01T00:00:00"),
            ).lastrowid
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            request = SimpleNamespace(state=SimpleNamespace(user=user))
            payload = {
                "categories": [
                    {"id": 10, "name": "Старый экспорт"},
                    {"id": 11, "name": "Дружеская", "category_skin": "friends"},
                    {"id": 12, "name": "Романтическая", "category_skin": "romantic"},
                    {"id": 13, "name": "Некорректная", "category_skin": "arbitrary"},
                ],
                "dates": [],
            }
            for category in payload["categories"]:
                category.update(
                    choice_mode="multiple",
                    voting_deadline="2030-02-01T12:00:00",
                )
            upload = UploadFile(
                filename="export.json",
                file=io.BytesIO(json.dumps(payload).encode("utf-8")),
            )

            response = await admin_routes.import_json(request, upload, conn)
            self.assertEqual(response.status_code, 303)
            skins = dict(
                conn.execute("SELECT name, category_skin FROM categories ORDER BY id")
            )
            self.assertEqual(skins["Старый экспорт"], "romantic")
            self.assertEqual(skins["Дружеская"], "friends")
            self.assertEqual(skins["Романтическая"], "romantic")
            self.assertEqual(skins["Некорректная"], "romantic")

            exported = admin_routes._full_dump(conn, uid)
            exported_skins = {
                category["name"]: category["category_skin"]
                for category in exported["categories"]
            }
            self.assertEqual(exported_skins, skins)
        finally:
            conn.close()


class ThemeFormPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        self.uid = self.conn.execute(
            "INSERT INTO users(telegram_id, display_name, is_operator, created_at) "
            "VALUES(?,?,1,?)",
            (26003, "Владелец", "2030-01-01T00:00:00"),
        ).lastrowid
        self.cid = self.conn.execute(
            "INSERT INTO categories(owner_id, name, created_at) VALUES(?,?,?)",
            (self.uid, "Категория", "2030-01-01T00:00:00"),
        ).lastrowid
        self.request = SimpleNamespace(
            state=SimpleNamespace(
                user=self.conn.execute(
                    "SELECT * FROM users WHERE id=?", (self.uid,)
                ).fetchone()
            )
        )

    def tearDown(self):
        self.conn.close()

    def test_category_save_accepts_skin_and_rejects_arbitrary_value(self):
        admin_routes.category_rename(
            self.cid,
            self.request,
            name="Категория",
            description="",
            og_title="",
            og_desc="",
            category_skin="romantic",
            og_image=None,
            conn=self.conn,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT category_skin FROM categories WHERE id=?", (self.cid,)
            ).fetchone()[0],
            "romantic",
        )

        admin_routes.category_rename(
            self.cid,
            self.request,
            name="Категория",
            description="",
            og_title="",
            og_desc="",
            category_skin="arbitrary-class",
            og_image=None,
            conn=self.conn,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT category_skin FROM categories WHERE id=?", (self.cid,)
            ).fetchone()[0],
            "romantic",
        )

    def test_profile_save_accepts_skin_and_rejects_arbitrary_value(self):
        admin_routes.profile_save(
            self.request,
            display_name="Владелец",
            birth_date="",
            gender="",
            cursor_effects=None,
            admin_skin="romantic",
            avatar=None,
            conn=self.conn,
        )
        self.request.state.user = self.conn.execute(
            "SELECT * FROM users WHERE id=?", (self.uid,)
        ).fetchone()
        self.assertEqual(self.request.state.user["admin_skin"], "romantic")

        admin_routes.profile_save(
            self.request,
            display_name="Владелец",
            birth_date="",
            gender="",
            cursor_effects=None,
            admin_skin="arbitrary-class",
            avatar=None,
            conn=self.conn,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT admin_skin FROM users WHERE id=?", (self.uid,)
            ).fetchone()[0],
            "romantic",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
