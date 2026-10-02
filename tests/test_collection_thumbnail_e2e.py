"""Фото в компактной подборке: защищённая доставка и устойчивые отступы."""

import unittest

from PIL import Image
from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


class CollectionThumbnailBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = LiveBackend()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.backend.close()

    def setUp(self):
        self.uid, self.cookie = self.backend.user_cookie()
        import images
        Image.new("RGB", (320, 180), "#678976").save(
            images.UPLOAD_DIR / "collection-thumbnail.webp", "WEBP",
        )
        conn = self.backend.db.connect()
        try:
            self.cid = conn.execute(
                "INSERT INTO categories(owner_id,name,link_token,choice_mode,voting_deadline,voting_status,created_at) "
                "VALUES(?,?,?,'single','2030-09-13T23:00','open',?)",
                (self.uid, "Выходные", "collection-thumbnail", self.backend.main.now_iso()),
            ).lastrowid
            conn.execute(
                "INSERT INTO categories(owner_id,name,link_token,created_at) VALUES(?,?,?,?)",
                (self.uid, "Ещё одна", "collection-thumbnail-other", self.backend.main.now_iso()),
            )
            for index in range(2):
                did = conn.execute(
                    "INSERT INTO dates(owner_id,name,created_at) VALUES(?,?,?)",
                    (self.uid, "НазваниеБезПробелов" * 16, self.backend.main.now_iso()),
                ).lastrowid
                conn.execute(
                    "INSERT INTO date_categories(date_id,category_id,position) VALUES(?,?,?)",
                    (did, self.cid, index),
                )
                if index == 0:
                    conn.execute(
                        "INSERT INTO date_images(date_id,filename,position,focus) VALUES(?,?,?,?)",
                        (did, "collection-thumbnail.webp", 0, "30% 40%"),
                    )
                    conn.execute(
                        "INSERT INTO date_images(date_id,filename,position) VALUES(?,?,?)",
                        (did, "unused-second-photo.webp", 1),
                    )
            conn.commit()
        finally:
            conn.close()

    def test_optional_thumbnail_and_long_titles_in_open_and_closed_collections(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.add_cookies([self.cookie])
        page = context.new_page()
        for locked in (False, True):
            if locked:
                conn = self.backend.db.connect()
                try:
                    conn.execute(
                        "UPDATE categories SET voting_deadline='2026-01-01T12:00', "
                        "voting_status='no_winner',closed_at='2026-01-01T12:00' WHERE id=?",
                        (self.cid,),
                    )
                    conn.commit()
                finally:
                    conn.close()
            for width in (320, 390, 1280):
                page.set_viewport_size({"width": width, "height": 900})
                page.goto(self.backend.url + f"/admin/categories/{self.cid}")
                rows = page.locator("#categoryDates tbody tr")
                expect(rows).to_have_count(2)
                expect(rows.nth(0).locator(".category-event-thumb")).to_have_count(1)
                expect(rows.nth(1).locator(".category-event-thumb")).to_have_count(0)
                image = rows.nth(0).locator("img")
                expect(image).to_have_attribute("src", "/admin/uploads/collection-thumbnail.webp?w=96")
                expect(image).to_have_js_property("naturalWidth", 96)
                for skin in ("friends", "romantic"):
                    for theme in ("light", "dark"):
                        with self.subTest(locked=locked, width=width, skin=skin, theme=theme):
                            page.locator("html").evaluate(
                                "(el, v) => {el.dataset.skin=v[0];el.dataset.theme=v[1]}",
                                [skin, theme],
                            )
                            geometry = rows.evaluate_all("""els => els.map(el => {
                              const summary = el.querySelector('.category-event-summary').getBoundingClientRect();
                              const title = el.querySelector('.category-event-copy > a').getBoundingClientRect();
                              const image = el.querySelector('img')?.getBoundingClientRect();
                              const capacity = el.querySelector('.category-event-capacity').getBoundingClientRect();
                              return {titleLeft: title.left, titleRight: title.right, summaryLeft: summary.left,
                                imageRight: image?.right, imageWidth: image?.width, imageHeight: image?.height,
                                capacityLeft: capacity.left};
                            })""")
                            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                            self.assertAlmostEqual(geometry[0]["imageWidth"], 44, delta=1)
                            self.assertAlmostEqual(geometry[0]["imageHeight"], 44, delta=1)
                            self.assertAlmostEqual(geometry[0]["titleLeft"] - geometry[0]["imageRight"], 10, delta=1)
                            self.assertAlmostEqual(geometry[1]["titleLeft"], geometry[1]["summaryLeft"], delta=1)
                            for row in geometry:
                                self.assertLessEqual(row["titleRight"], row["capacityLeft"] - 4)

    def test_share_picker_has_a_name_without_visible_extra_label(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.add_cookies([self.cookie])
        page = context.new_page()
        page.goto(self.backend.url + "/admin/")
        expect(page.get_by_role("combobox", name="Подборка для отправки")).to_be_visible()
        expect(page.locator(".share-pick label")).to_have_count(0)

    def test_voting_controls_align_after_labels_wrap_and_on_mobile(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.add_cookies([self.cookie])
        page = context.new_page()
        for width in (320, 390, 821, 900, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(self.backend.url + f"/admin/categories/{self.cid}")
            expect(page.locator('#categoryVotingForm .choice-opt small')).to_have_count(0)
            expect(page.locator('#categoryVotingForm .choice-opt b')).to_have_text(["Один", "Неограниченное"])
            page.add_style_tag(content="#categoryChoiceLabel { font-size:24px; }")
            for skin in ("friends", "romantic"):
                for theme in ("light", "dark"):
                    with self.subTest(width=width, skin=skin, theme=theme):
                        page.locator("html").evaluate(
                            "(el, v) => {el.dataset.skin=v[0];el.dataset.theme=v[1]}",
                            [skin, theme],
                        )
                        geometry = page.locator("#categoryVotingForm").evaluate("""form => {
                          const choices = form.querySelector('.choice-pick').getBoundingClientRect();
                          const date = form.querySelector('#votingDeadline').getBoundingClientRect();
                          const submit = form.querySelector(':scope > button').getBoundingClientRect();
                          const label = form.querySelector('#categoryChoiceLabel');
                          return {choicesTop: choices.top, choicesBottom: choices.bottom,
                            dateTop: date.top, submitTop: submit.top,
                            dateCenter: date.y + date.height/2, submitCenter: submit.y + submit.height/2,
                            labelHeight: label.getBoundingClientRect().height,
                            labelLine: parseFloat(getComputedStyle(label).lineHeight),
                            radios: [...form.querySelectorAll('.choice-opt')].map(option => {
                              const radio = option.querySelector('input').getBoundingClientRect();
                              const text = option.querySelector('span').getBoundingClientRect();
                              return Math.abs(radio.y + radio.height/2 - text.y - text.height/2);
                            })};
                        }""")
                        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                        self.assertGreater(geometry["labelHeight"], geometry["labelLine"])
                        for difference in geometry["radios"]:
                            self.assertLessEqual(difference, 1)
                        if width >= 821:
                            self.assertAlmostEqual(geometry["choicesTop"], geometry["dateTop"], delta=1)
                            self.assertAlmostEqual(geometry["submitTop"], geometry["dateTop"], delta=1)
                            self.assertAlmostEqual(geometry["submitCenter"], geometry["dateCenter"], delta=1)
                        else:
                            self.assertGreater(geometry["dateTop"], geometry["choicesBottom"])
                            self.assertGreater(geometry["submitTop"], geometry["dateTop"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
