"""Session D: геометрия длинного текста, доступные имена и ресурсы диалога в браузере."""
import re
import unittest

from playwright.sync_api import expect, sync_playwright
from live_backend import LiveBackend


class SessionDUiTests(unittest.TestCase):
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
        self.cookie = cookie
        self.context = self.browser.new_context(viewport={'width':390, 'height':844}, reduced_motion='reduce')
        self.context.add_cookies([cookie])
        self.context.set_default_timeout(5000)
        self.context.set_default_navigation_timeout(15000)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.long_name = 'Подборка' + 'Ш' * 192
        conn = self.backend.db.connect()
        try:
            self.cats = []
            for index, name in enumerate((self.long_name, 'Кино', 'Закрытая' + 'Ж' * 192)):
                self.cats.append(conn.execute(
                    "INSERT INTO categories(owner_id,name,link_token,choice_mode,voting_status,voting_deadline,created_at) VALUES(?,?,?,'multiple',?,'2099-01-01T12:00',?)",
                    (self.uid, name, 'session-d-' + str(index), 'open', self.backend.main.now_iso()),
                ).lastrowid)
            self.date = conn.execute(
                "INSERT INTO dates(owner_id,name,share_token,is_public,created_at) VALUES(?,?,'session-d-event',1,?)",
                (self.uid, 'Событие' + 'Щ' * 193, self.backend.main.now_iso()),
            ).lastrowid
            for cat in (self.cats[0], self.cats[2]):
                conn.execute('INSERT INTO date_categories(date_id,category_id) VALUES(?,?)', (self.date,cat))
            conn.execute("UPDATE categories SET voting_status='resolved',winner_date_id=? WHERE id=?", (self.date,self.cats[2]))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def goto(self, path):
        response = self.page.goto(self.backend.url + path)
        self.assertEqual(response.status, 200)

    def fits(self, selector, *, decorative_clip=False):
        """Проверяем геометрию узла и текста; clipping страницы не скрывает сбой."""
        for box in self.page.locator(selector).evaluate_all('''nodes => nodes.map(node => {
          const r = node.getBoundingClientRect();
          return {tag:node.tagName, left:r.left, right:r.right, width:r.width, viewport:innerWidth,
                  client:node.clientWidth, scroll:node.scrollWidth};
        })'''):
            self.assertGreater(box['width'], 0, (selector, box))
            self.assertGreaterEqual(box['left'], -1, (selector, box))
            self.assertLessEqual(box['right'], box['viewport'] + 1, (selector, box))
            # Native select сохраняет внутреннюю ширину текста option, даже
            # когда сам control и содержащая его разметка корректно сжимаются.
            if box['tag'] != 'SELECT' and not decorative_clip:
                self.assertLessEqual(box['scroll'], box['client'] + 1, (selector, box))

    def test_selects_and_share_qr_fit_short_and_long_names_at_six_widths(self):
        for width in (320,390,430,768,1280,1440):
            self.page.set_viewport_size({'width':width, 'height':900})
            for cat in self.cats[:2]:
                with self.subTest(width=width, cat=cat):
                    self.goto('/admin/?share=' + str(cat))
                    self.fits('.share-grid, .share-pick, .share-pick select')
                    if width <= 640:
                        self.page.locator('#qrToggle').click()
                    expect(self.page.locator('#shareQr')).to_be_visible()
                    self.fits('#shareQr', decorative_clip=True)
                    self.fits('.qr-code, #qrDownload')
                    self.goto('/admin/dates?cat=' + str(cat))
                    self.fits('.controls .filters, .controls .filters select')

    def test_editor_chips_wrap_and_keep_selection_locked_values_and_keyboard_focus(self):
        for width in (390,1280):
            self.page.set_viewport_size({'width':width, 'height':900})
            self.goto('/admin/dates/' + str(self.date) + '/edit')
            self.fits('.chip-cat, .chip-cat-name')
            selected = self.page.locator('.chip-cat input[value="' + str(self.cats[0]) + '"]')
            unselected = self.page.locator('.chip-cat input[value="' + str(self.cats[1]) + '"]')
            locked = self.page.locator('.chip-cat input[value="' + str(self.cats[2]) + '"]')
            expect(selected).to_be_checked()
            expect(unselected).not_to_be_checked()
            expect(locked).to_be_checked()
            expect(locked).to_be_disabled()
            selected.focus()
            expect(selected).to_be_focused()
            self.assertTrue(selected.evaluate("node => node.closest('label').matches(':focus-within')"))
            self.page.keyboard.press('Space')
            expect(selected).not_to_be_checked()
            self.page.keyboard.press('Tab')
            expect(unselected).to_be_focused()
            self.page.keyboard.press('Space')
            expect(unselected).to_be_checked()
            self.assertIn(str(self.cats[2]), self.page.locator('#dateForm').evaluate("form => new FormData(form).getAll('categories')"))
            self.fits('.chip-cat, .chip-cat-name')

    def test_public_long_title_wraps_and_menu_and_vote_remain_reachable(self):
        self.context.clear_cookies()
        for skin in ('friends','romantic'):
            conn = self.backend.db.connect()
            conn.execute('UPDATE categories SET category_skin=? WHERE id=?', (skin,self.cats[0]))
            conn.commit()
            conn.close()
            for width in (390,1280):
                self.page.set_viewport_size({'width':width, 'height':900})
                self.goto('/c/session-d-0')
                self.fits('.card .title')
                title = self.page.locator('.card .title').first
                self.assertGreater(title.bounding_box()['height'], 60)
                menu = self.page.locator('.event-card-menu').first
                menu.locator('summary').click()
                expect(menu).to_have_attribute('open', '')
                self.fits('.event-card-menu-popover')
                self.page.keyboard.press('Escape')
                vote = self.page.locator('.card .book').first
                expect(vote).to_be_visible()
                self.fits('.card .book')

    def test_dialog_loads_only_needed_skin_and_keeps_focus_and_theme_behavior(self):
        self.context.clear_cookies()
        for skin in ('friends','romantic'):
            with self.subTest(skin=skin):
                conn = self.backend.db.connect()
                conn.execute('UPDATE categories SET category_skin=? WHERE id=?', (skin,self.cats[0]))
                conn.commit()
                conn.close()
                self.goto('/c/session-d-0')
                logos = self.page.locator('#loginDlg [data-auth-logo]')
                self.assertEqual(logos.evaluate_all("nodes => nodes.filter(n => n.hasAttribute('src')).length"), 0)
                self.page.locator('#loginOpen').click()
                expect(self.page.locator('#loginDlg')).to_be_visible()
                expect(self.page.locator('#loginClose')).to_be_focused()
                active = self.page.locator('[data-auth-logo="' + skin + '"]')
                expect(active).to_have_attribute('src', re.compile('logo-'))
                expect(active).to_have_js_property('complete', True)
                self.assertGreater(active.evaluate('n => n.naturalWidth'), 0)
                self.assertEqual(logos.evaluate_all("nodes => nodes.filter(n => n.hasAttribute('src')).length"), 1)
                other = 'romantic' if skin == 'friends' else 'friends'
                self.page.evaluate("skin => {document.documentElement.dataset.skin=skin; document.dispatchEvent(new CustomEvent('d4y:skinchange',{detail:{skin}}));}", other)
                expect(self.page.locator('[data-auth-logo="' + other + '"]')).to_have_attribute('src', re.compile('logo-'))
                for theme in ('dark','light'):
                    self.page.locator('html').evaluate('(node, theme) => node.dataset.theme = theme', theme)
                    expect(self.page.locator('[data-auth-logo="' + other + '"]')).to_be_visible()
                    self.fits('#loginDlg', decorative_clip=True)
                self.page.keyboard.press('Escape')
                expect(self.page.locator('#loginDlg')).not_to_be_visible()
                expect(self.page.locator('#loginOpen')).to_be_focused()

    def test_names_visible_label_focus_keyboard_and_filter_autosubmit(self):
        self.goto('/admin/profile')
        for label, selector in (('Имя', '#profileName'), ('Дата рождения', '#profileBirthDate')):
            self.page.locator('label[for="' + selector[1:] + '"]').click()
            expect(self.page.locator(selector)).to_be_focused()
            expect(self.page.locator(selector)).to_have_accessible_name(label)
        self.page.locator('#profileName').focus()
        self.page.keyboard.press('Tab')
        expect(self.page.locator('#profileBirthDate')).to_be_focused()
        self.goto('/admin/?share=' + str(self.cats[0]))
        self.page.locator('label[for="shareCollection"]').click()
        expect(self.page.get_by_role('combobox', name='Подборка')).to_be_focused()
        self.page.get_by_role('combobox', name='Подборка').select_option(str(self.cats[1]))
        expect(self.page).to_have_url(self.backend.url + '/admin/?share=' + str(self.cats[1]))
        self.goto('/admin/dates')
        for name, value, parameter in (('Сортировка событий','when','sort'), ('Фильтр событий','public','f'), ('Подборка',str(self.cats[0]),'cat')):
            control = self.page.get_by_role('combobox', name=name, exact=True)
            expect(control).to_have_count(1)
            control.select_option(value)
            self.page.wait_for_url(lambda url: parameter + '=' + value in url)
            expect(self.page.get_by_role('combobox', name=name, exact=True)).to_have_value(value)

    def test_touch_mobile_geometry_in_both_skins_and_themes(self):
        self.context.close()
        self.context = self.browser.new_context(viewport={'width':390,'height':844},
                                               is_mobile=True,has_touch=True,reduced_motion='reduce')
        self.context.add_cookies([self.cookie])
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        for skin in ('friends','romantic'):
            conn = self.backend.db.connect()
            conn.execute('UPDATE users SET admin_skin=? WHERE id=?',(skin,self.uid))
            conn.commit()
            conn.close()
            for path, selector in (
                ('/admin/?share=' + str(self.cats[0]),'.share-grid, .share-pick select'),
                ('/admin/dates','.controls .filters, .controls .filters select'),
                ('/admin/dates/' + str(self.date) + '/edit','.chip-cat, .chip-cat-name'),
            ):
                self.goto(path)
                for theme in ('light','dark'):
                    self.page.locator('html').evaluate('(node,theme)=>node.dataset.theme=theme',theme)
                    self.fits(selector)


if __name__ == '__main__':
    unittest.main(verbosity=2)
