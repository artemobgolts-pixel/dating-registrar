"""UI-01: реальные границы компонентов и строк текста во всех вариантах UI."""
import itertools
import os
from pathlib import Path
import unittest

from playwright.sync_api import expect, sync_playwright
from live_backend import LiveBackend


class FinalNameOverflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = LiveBackend()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.backend.close()

    def check_bounds(self, page, child, parent):
        values = page.locator(child).evaluate_all("""(nodes, parentSelector) => nodes.map(node => {
          const parent = node.closest(parentSelector).getBoundingClientRect();
          const box = node.getBoundingClientRect();
          const range = document.createRange(); range.selectNodeContents(node);
          return {parent: {left:parent.left,right:parent.right},
            box: {left:box.left,right:box.right,width:box.width}, viewport:innerWidth,
            lines: Array.from(range.getClientRects(), r => ({left:r.left,right:r.right,width:r.width}))};
        })""", parent)
        self.assertTrue(values, child)
        for value in values:
            for box in [value["box"], *value["lines"]]:
                if box["width"] == 0:
                    continue
                self.assertGreaterEqual(box["left"], max(0, value["parent"]["left"]) - 1.5, (child, value))
                self.assertLessEqual(box["right"], min(value["viewport"], value["parent"]["right"]) + 1.5, (child, value))
        return values

    def names_fit(self, width, skin, theme, kind):
        uid, cookie = self.backend.user_cookie(skin=skin)
        name = {"short": "Встреча друзей", "ascii": "W" * 200, "unicode": "Щ" * 200}[kind]
        conn = self.backend.db.connect()
        try:
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (name, uid))
            cid = conn.execute(
                "INSERT INTO categories(owner_id,name,category_skin,link_token,created_at) VALUES(?,?,?,'long-names',?)",
                (uid, name, skin, self.backend.main.now_iso()),
            ).lastrowid
            did = conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,created_at) VALUES(?,?,'long-name-event',?)",
                (uid, name, self.backend.main.now_iso()),
            ).lastrowid
            conn.execute("INSERT INTO date_categories(date_id,category_id) VALUES(?,?)", (did, cid))
            conn.commit()
        finally:
            conn.close()
        context = self.browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        self.addCleanup(context.close)
        context.add_cookies([cookie, {"name": "d4y_theme", "value": theme, "url": self.backend.url}])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        artifacts = os.environ.get("UI_REMEDIATION_ARTIFACTS")
        for surface, path, pairs in (
            # Старый layout=list также открывает единственный вид карточек.
            ("list", "/admin/dates", [(".dcard .ttl a", ".b"), (".dcard .foot a", ".dcard"), (".dcard-select", ".dcard")]),
            ("cards", "/admin/dates", [(".dcard .ttl", ".dcard"), (".dcard .foot a", ".dcard"), (".dcard .more", ".dcard")]),
            ("public", "/c/long-names", [(".hero h1 .display-ink", ".hero"), (".author-name", ".author"), (".author", ".hero"), (".corner-actions .corner-control", ".corner-actions")]),
        ):
            with self.subTest(surface=surface):
                context.add_cookies([{"name": "layout", "value": surface, "url": self.backend.url}])
                response = page.goto(self.backend.url + path)
                self.assertEqual(response.status, 200)
                expect(page.locator("html")).to_have_attribute("data-skin", skin)
                expect(page.locator("html")).to_have_attribute("data-theme", theme)
                if surface in ("list", "cards"):
                    expect(page.locator(".drow, .dlist, .viewtog")).to_have_count(0)
                page.evaluate("document.fonts.ready")
                if artifacts:
                    output = Path(artifacts)
                    output.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(output / f"{width}-{skin}-{theme}-{kind}-{surface}.png"), full_page=True)
                for child, parent in pairs:
                    with self.subTest(component=child):
                        self.check_bounds(page, child, parent)
        self.assertEqual(errors, [])


def make_case(width, skin, theme, kind):
    def test(self):
        self.names_fit(width, skin, theme, kind)
    return test


for _width, _skin, _theme, _kind in itertools.product(
        (390, 1280), ("friends", "romantic"), ("light", "dark"), ("short", "ascii", "unicode")):
    setattr(FinalNameOverflowTests, f"test_{_width}_{_skin}_{_theme}_{_kind}",
            make_case(_width, _skin, _theme, _kind))


if __name__ == "__main__":
    unittest.main(verbosity=2)
