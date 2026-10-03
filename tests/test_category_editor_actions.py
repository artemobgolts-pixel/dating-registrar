"""Добавление и удаление событий в редакторе, геометрия и обучение подборки."""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


class CategoryEditorActionsTests(unittest.TestCase):
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
        self.uid, cookie = self.backend.user_cookie()
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 900}, reduced_motion="reduce",
        )
        self.context.add_cookies([cookie])
        self.context.set_default_timeout(5000)
        self.context.set_default_navigation_timeout(15000)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        conn = self.backend.db.connect()
        try:
            stamp = self.backend.main.now_iso()
            self.cid = conn.execute(
                "INSERT INTO categories(owner_id,name,link_token,created_at) "
                "VALUES(?,'Выходные','category-actions-test',?)",
                (self.uid, stamp),
            ).lastrowid
            self.did = conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,created_at) "
                "VALUES(?,'Прогулка','category-actions-event',?)",
                (self.uid, stamp),
            ).lastrowid
            other_uid = conn.execute(
                "INSERT INTO users(telegram_id,display_name,created_at) "
                "VALUES(90002,'Другой организатор',?)", (stamp,),
            ).lastrowid
            self.foreign_did = conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,created_at) "
                "VALUES(?,'Чужое событие','category-actions-foreign',?)",
                (other_uid, stamp),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def goto_editor(self, *, tour=False):
        response = self.page.goto(
            self.backend.url + f"/admin/categories/{self.cid}" +
            ("#tour=category-editor" if tour else ""),
        )
        self.assertEqual(response.status, 200)

    def attached(self):
        return self.backend.row(
            "SELECT date_id FROM date_categories WHERE category_id=? AND date_id=?",
            (self.cid, self.did),
        ) is not None

    def denial_message(self, response):
        self.assertEqual(response.status, 303)
        return parse_qs(urlsplit(response.headers["location"]).query).get("msg", [""])[0]

    def fill_events(self, count):
        conn = self.backend.db.connect()
        try:
            conn.execute("DELETE FROM date_categories WHERE category_id=?", (self.cid,))
            ids = []
            for index in range(count):
                did = self.did if index == 0 else conn.execute(
                    "INSERT INTO dates(owner_id,name,created_at) VALUES(?,?,?)",
                    (self.uid, f"Событие {index + 1}", self.backend.main.now_iso()),
                ).lastrowid
                ids.append(did)
                conn.execute(
                    "INSERT INTO date_categories(date_id,category_id,position) VALUES(?,?,?)",
                    (did, self.cid, index),
                )
            conn.commit()
            return ids
        finally:
            conn.close()

    def test_more_events_are_offered_only_above_five(self):
        for count in (0, 5, 6, 12):
            with self.subTest(count=count):
                self.fill_events(count)
                self.goto_editor()
                expect(self.page.locator("#categoryDates tbody tr")).to_have_count(count)
                expect(self.page.locator("#categoryDates tbody tr:visible")).to_have_count(min(count, 5))
                toggle = self.page.get_by_role("button", name="Показать ещё")
                expect(toggle).to_have_count(1 if count > 5 else 0)
                if count > 5:
                    expect(toggle).to_have_attribute("aria-expanded", "false")
                    expect(toggle).to_have_attribute("aria-controls", "categoryEventsList")
                    expect(toggle.locator("[data-events-remaining]")).to_have_text(str(count - 5))

    def test_reveal_and_collapse_work_with_keyboard_in_each_appearance(self):
        ids = self.fill_events(12)
        self.goto_editor()
        toggle = self.page.locator(".category-events-toggle")
        for width in (320, 390, 1280):
            self.page.set_viewport_size({"width": width, "height": 900})
            for skin in ("friends", "romantic"):
                for theme in ("light", "dark"):
                    with self.subTest(width=width, skin=skin, theme=theme):
                        self.page.locator("html").evaluate(
                            "(el, v) => {el.dataset.skin=v[0];el.dataset.theme=v[1]}",
                            [skin, theme],
                        )
                        toggle.scroll_into_view_if_needed()
                        box = toggle.bounding_box()
                        self.assertGreaterEqual(box["height"], 44)
                        self.assertGreaterEqual(box["x"], 0)
                        self.assertLessEqual(box["x"] + box["width"], width)
                        toggle.focus()
                        self.page.keyboard.press("Enter")
                        expect(toggle).to_have_attribute("aria-expanded", "true")
                        expect(self.page.get_by_role("button", name="Свернуть", exact=True)).to_be_visible()
                        expect(self.page.locator("#categoryDates tbody tr:visible")).to_have_count(12)
                        expect(self.page.locator(f'tr[data-did="{ids[5]}"] .category-event-copy > a')).to_be_focused()
                        self.assertEqual(self.page.locator("#categoryEventsList").evaluate(
                            "el => el.getAnimations({subtree:true}).length",
                        ), 0)
                        toggle.click()
                        expect(toggle).to_have_attribute("aria-expanded", "false")
                        expect(self.page.locator("#categoryDates tbody tr:visible")).to_have_count(5)
                        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))

    def test_expanded_events_stay_open_after_editing_membership(self):
        ids = self.fill_events(12)
        self.goto_editor()
        self.page.get_by_role("button", name="Показать ещё").click()
        row = self.page.locator(f'tr[data-did="{ids[5]}"]')
        row.locator(".category-event-copy > a").click()
        expect(self.page.locator("#dateForm")).to_be_visible()
        self.page.go_back()
        expect(self.page.locator("#categoryDates tbody tr:visible")).to_have_count(12)
        row.get_by_role("button", name="Убрать событие «Событие 6» из подборки").click()
        self.page.get_by_role("alertdialog").get_by_role("button", name="Подтвердить", exact=True).click()
        expect(self.page.locator("#categoryDates tbody tr:visible")).to_have_count(11)
        expect(self.page.get_by_role("button", name="Свернуть", exact=True)).to_be_visible()
        self.page.locator(".category-add-menu summary").click()
        self.page.get_by_role("combobox", name="Существующее событие").select_option(str(ids[5]))
        self.page.get_by_role("button", name="Добавить выбранное событие в подборку").click()
        expect(self.page.locator("#categoryDates tbody tr:visible")).to_have_count(12)
        expect(self.page.get_by_role("button", name="Свернуть", exact=True)).to_be_visible()

    def test_sorting_reveals_the_tail_and_saves_every_event(self):
        ids = self.fill_events(12)
        self.goto_editor()
        handle = self.page.locator(f'tr[data-did="{ids[4]}"] [data-sort-handle]')
        handle.focus()
        self.page.keyboard.press("Space")
        expect(self.page.locator("#categoryDates tbody tr:visible")).to_have_count(12)
        self.page.keyboard.press("ArrowDown")
        expect(handle).to_be_focused()
        with self.page.expect_response(lambda response: "/dates_reorder" in response.url) as saved:
            self.page.keyboard.press("Space")
        self.assertEqual(saved.value.status, 200)
        ids[4], ids[5] = ids[5], ids[4]
        conn = self.backend.db.connect()
        try:
            actual = [row["date_id"] for row in conn.execute(
                "SELECT date_id FROM date_categories WHERE category_id=? ORDER BY position", (self.cid,),
            )]
        finally:
            conn.close()
        self.assertEqual(actual, ids)
        self.page.get_by_role("button", name="Свернуть", exact=True).click()
        expect(self.page.locator(f'tr[data-did="{ids[4]}"]')).to_be_visible()
        expect(self.page.locator(f'tr[data-did="{ids[5]}"]')).to_be_hidden()

    def test_closed_collection_can_reveal_events_without_sorting(self):
        self.fill_events(6)
        conn = self.backend.db.connect()
        try:
            conn.execute("UPDATE categories SET voting_status='resolved' WHERE id=?", (self.cid,))
            conn.commit()
        finally:
            conn.close()
        self.goto_editor()
        expect(self.page.locator("#catRows")).to_have_count(0)
        expect(self.page.locator("#categoryDates tbody tr:visible")).to_have_count(5)
        self.page.get_by_role("button", name="Показать ещё").click()
        expect(self.page.locator("#categoryDates tbody tr:visible")).to_have_count(6)

    def test_add_existing_event_and_remove_only_its_membership(self):
        self.goto_editor()
        menu = self.page.locator(".category-add-menu")
        menu.locator("summary").focus()
        self.page.keyboard.press("Enter")
        expect(menu).to_have_attribute("open", "")
        self.page.get_by_role("combobox", name="Существующее событие").select_option(str(self.did))
        self.page.get_by_role("button", name="Добавить выбранное событие в подборку").click()
        row = self.page.locator(f'#catRows tr[data-did="{self.did}"]')
        expect(row).to_be_visible()
        self.assertTrue(self.attached())
        row.get_by_role("button", name="Убрать событие «Прогулка» из подборки").click()
        self.page.get_by_role("alertdialog").get_by_role("button", name="Подтвердить", exact=True).click()
        expect(self.page.locator("#catRows tr")).to_have_count(0)
        self.assertFalse(self.attached())
        self.assertIsNotNone(self.backend.row("SELECT id FROM dates WHERE id=?", (self.did,)))
        menu = self.page.locator(".category-add-menu")
        menu.locator("summary").click()
        expect(self.page.get_by_role("combobox", name="Существующее событие")).to_contain_text("Прогулка")
        self.page.keyboard.press("Escape")
        expect(menu).not_to_have_attribute("open", "")
        expect(menu.locator("summary")).to_be_focused()

    def test_membership_posts_keep_csrf_ownership_and_completed_voting_guards(self):
        self.goto_editor()
        csrf = self.page.locator("body").get_attribute("data-csrf")
        base = self.backend.url + f"/admin/categories/{self.cid}"
        request = self.context.request
        for action in ("attach", "detach"):
            with self.subTest(action=action, guard="csrf"):
                response = request.post(
                    base + "/" + action, form={"date_id": str(self.did)},
                    max_redirects=0,
                )
                self.assertIn("Сессия устарела", self.denial_message(response))
        for action in ("attach", "detach"):
            with self.subTest(action=action, guard="owner"):
                response = request.post(
                    base + "/" + action,
                    form={"date_id": str(self.foreign_did), "csrf": csrf},
                    max_redirects=0,
                )
                self.assertIn("Событие не найдено", self.denial_message(response))
        self.assertFalse(self.attached())
        conn = self.backend.db.connect()
        try:
            conn.execute(
                "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
                (self.did, self.cid),
            )
            conn.execute("UPDATE categories SET voting_status='resolved' WHERE id=?", (self.cid,))
            conn.commit()
        finally:
            conn.close()
        for action in ("attach", "detach"):
            with self.subTest(action=action, guard="closed"):
                response = request.post(
                    base + "/" + action,
                    form={"date_id": str(self.did), "csrf": csrf},
                    max_redirects=0,
                )
                self.assertIn("Голосование уже завершено", self.denial_message(response))
        self.assertTrue(self.attached())
        self.goto_editor()
        expect(self.page.locator(".category-add-menu, .category-event-remove")).to_have_count(0)

    def test_deadline_follows_choice_count_and_compact_actions_fit_all_appearances(self):
        self.goto_editor()
        self.page.locator("#categoryAppearance").evaluate("el => el.open = true")
        for width in (320, 390, 1280):
            self.page.set_viewport_size({"width": width, "height": 900})
            for skin in ("friends", "romantic"):
                for theme in ("light", "dark"):
                    with self.subTest(width=width, skin=skin, theme=theme):
                        self.page.locator("html").evaluate(
                            "(node, appearance) => {node.dataset.skin = appearance.skin; node.dataset.theme = appearance.theme;}",
                            {"skin": skin, "theme": theme},
                        )
                        choice = self.page.locator("#categoryVoting .choice-pick").bounding_box()
                        deadline = self.page.locator("#categoryVoting .deadline-picker").bounding_box()
                        self.assertGreaterEqual(deadline["y"], choice["y"] + choice["height"])
                        preview = self.page.locator("#categoryEditForm").evaluate("""form => {
                          const center = el => {const r=el.getBoundingClientRect();return r.x+r.width/2};
                          return {form:center(form),label:center(form.querySelector('.category-preview-label')),
                            image:center(form.querySelector('#ogPreview')),
                            labelAlign:getComputedStyle(form.querySelector('.category-preview-label')).textAlign};
                        }""")
                        self.assertAlmostEqual(preview["form"], preview["label"], delta=1)
                        self.assertAlmostEqual(preview["form"], preview["image"], delta=1)
                        self.assertEqual(preview["labelAlign"], "center")
                        menu = self.page.locator(".category-add-menu")
                        menu.locator("summary").click()
                        self.assertTrue(self.page.locator(".category-add-panel").evaluate("""panel => {
                          const r = panel.getBoundingClientRect();
                          return panel.contains(document.elementFromPoint(r.left + 5, r.top + 5));
                        }"""))
                        for selector in (
                            ".category-add-panel", ".category-attach-submit", "#votingDeadline",
                            "#categoryVoting .voting-form > .btn", "#categoryShare",
                        ):
                            box = self.page.locator(selector).bounding_box()
                            self.assertIsNotNone(box, selector)
                            self.assertGreaterEqual(box["x"], -1, (selector, box))
                            self.assertLessEqual(box["x"] + box["width"], width + 1, (selector, box))
                        self.page.keyboard.press("Escape")

    def test_tour_highlights_add_action_and_teaches_privacy_after_voting(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.goto_editor(tour=True)
        expect(self.page.locator("#tourTitle")).to_have_text("Сначала добавь события")
        self.page.wait_for_function("""() => {
          const spot = document.querySelector('.tour-spot').getBoundingClientRect();
          const add = document.querySelector('.category-add-toggle').getBoundingClientRect();
          return spot.left <= add.left && spot.right >= add.right &&
                 spot.top <= add.top && spot.bottom >= add.bottom;
        }""")
        self.page.locator(".tour-next").click()
        expect(self.page.locator("#tourTitle")).to_have_text("Правила голосования")
        self.page.locator(".tour-next").click()
        expect(self.page.locator("#tourTitle")).to_have_text("Приватность")
        expect(self.page.locator("#tourText")).to_contain_text("PIN-коду")
        self.page.keyboard.press("Escape")
        expect(self.page.locator(".tour-overlay")).to_have_count(0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
