"""Дата события одинакова в карточках, ссылках и списках, включая узкий экран."""

import unittest
from datetime import datetime
from unittest.mock import patch

import httpx
from playwright.sync_api import expect, sync_playwright

from live_backend import LiveBackend


class EventDatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = LiveBackend()
        cls.uid, cls.cookie = cls.backend.user_cookie()
        conn = cls.backend.db.connect()
        stamp = cls.backend.main.now_iso()
        cls.compact = {}
        try:
            conn.execute("UPDATE users SET is_operator=1 WHERE id=?", (cls.uid,))
            other = conn.execute(
                "INSERT INTO users(telegram_id,display_name,created_at) VALUES(90002,'Автор',?)",
                (stamp,),
            ).lastrowid
            for owner, prefix in ((cls.uid, 'own'), (other, 'other')):
                cat = conn.execute(
                    "INSERT INTO categories(owner_id,name,link_token,created_at) VALUES(?,?,?,?)",
                    (owner, 'Осенние встречи', prefix, stamp),
                ).lastrowid
                for index, start in enumerate(('2099-12-31T23:30', None)):
                    did = conn.execute(
                        "INSERT INTO dates(owner_id,name,share_token,starts_at,ends_at,is_public,created_at) "
                        "VALUES(?,?,?,?,?,1,?)",
                        (owner, 'Встреча ' + str(index), prefix + str(index), start,
                         '2100-01-01T02:00' if start else None, stamp),
                    ).lastrowid
                    conn.execute("INSERT INTO date_categories(date_id,category_id) VALUES(?,?)", (did, cat))
                    if owner == cls.uid and index == 0:
                        cls.own, cls.cat = did, cat
                        conn.execute(
                            "INSERT INTO questions(date_id,category_id,user_id,text,created_at) VALUES(?,?,?,?,?)",
                            (did, cat, other, 'До скольки встречаемся?', stamp),
                        )
                    if owner == other and index == 0:
                        cls.other_date, cls.other_user = did, other
                        conn.execute(
                            "INSERT INTO date_wants(user_id,date_id,is_public,created_at,updated_at) VALUES(?,?,1,?,?)",
                            (cls.uid, did, stamp, stamp),
                        )
                        reviewed = conn.execute(
                            "INSERT INTO dates(owner_id,name,share_token,starts_at,ends_at,is_public,archived_at,created_at) "
                            "VALUES(?,?,'reviewed','2099-12-31T23:30','2100-01-01T02:00',1,?,?)",
                            (other, 'Встреча с отзывом', stamp, stamp),
                        ).lastrowid
                        cls.review = conn.execute(
                            "INSERT INTO date_reviews(user_id,date_id,rating,is_public,created_at,updated_at) VALUES(?,?,5,1,?,?)",
                            (cls.uid, reviewed, stamp, stamp),
                        ).lastrowid
                cls.compact[prefix] = []
                for suffix, start, end in (
                    ('dated', '2099-12-31T23:30', '2100-01-01T02:00'),
                    ('undated', None, None),
                ):
                    did = conn.execute(
                        "INSERT INTO dates(owner_id,name,comment,share_token,starts_at,ends_at,is_public,created_at) "
                        "VALUES(?,'Стримуха','Залипнуть под любимый стрим.',?,?,?,?,?)",
                        (owner, f'{prefix}-{suffix}', start, end, 1, stamp),
                    ).lastrowid
                    conn.execute("INSERT INTO date_categories(date_id,category_id) VALUES(?,?)", (did, cat))
                    cls.compact[prefix].append(did)
            conn.execute(
                "INSERT INTO dates(owner_id,name,starts_at,ends_at,operator_review_pending,created_at) "
                "VALUES(?,'На проверке','2099-12-31T23:30','2100-01-01T02:00',1,?)",
                (other, stamp),
            )
            conn.execute(
                "INSERT INTO reports(target_type,target_id,created_at) VALUES('date',?,?)",
                (cls.own, stamp),
            )
            conn.execute(
                "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) VALUES(?,?,'test-vote',?,?)",
                (cls.own, cls.cat, other, stamp),
            )
            conn.commit()
        finally:
            conn.close()
        cls.client = httpx.Client(base_url=cls.backend.url, cookies={'admin_s': cls.cookie['value']}, follow_redirects=True)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.backend.close()

    def test_schedule_preserves_calendar_dates_ranges_and_moscow_time(self):
        from helpers import event_schedule
        with patch('helpers.now_naive', return_value=datetime(2026, 9, 30)):
            self.assertIsNone(event_schedule(None))
            self.assertIsNone(event_schedule('invalid', '2026-10-05T21:00'))
            point = event_schedule('2026-10-05T19:00', 'invalid')
            self.assertEqual(point['start']['date'], '5 октября')
            self.assertEqual(point['start']['iso'], '2026-10-05T19:00+03:00')
            self.assertIsNone(point['end'])
            same = event_schedule('2026-10-05T19:00', '2026-10-05T21:00')
            self.assertTrue(same['same_day'])
            self.assertEqual(same['label'], '5 октября 2026, 19:00–21:00 · мск')
            overnight = event_schedule('2026-10-05T23:00', '2026-10-06T02:00')
            self.assertFalse(overnight['same_day'])
            self.assertEqual(overnight['end']['date'], '6 октября')
            year = event_schedule('2026-12-31T23:00', '2027-01-01T02:00')
            self.assertEqual(year['start']['date'], '31 декабря 2026')
            self.assertEqual(year['end']['date'], '1 января 2027')
            self.assertEqual(event_schedule('2025-10-05T19:00')['start']['date'], '5 октября 2025')
            self.assertEqual(event_schedule('2026-10-05T23:00Z')['start']['date'], '6 октября')
            self.assertEqual(event_schedule('2026-10-05T23:00Z')['start']['time'], '02:00')

    def test_dates_reach_all_live_surfaces(self):
        paths = [
            '/admin/community', f'/admin/community/date/{self.other_date}',
            '/admin/dates', f'/admin/categories/{self.cat}',
            '/admin/questions', '/d/other0', '/c/other',
            f'/u/{self.other_user}', f'/u/{self.uid}?tab=want', f'/u/{self.uid}?tab=reviews',
            f'/u/{self.uid}/reviews/{self.review}/widget', f'/d/reviewed/review/{self.review}',
            '/operator/dates', f'/operator/users/{self.uid}',
            '/operator/reports', '/operator/bookings', '/operator/review',
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text[:300])
                self.assertIn('class="event-date', response.text)
                self.assertIn('datetime="2099-12-31T23:30+03:00"', response.text)
                self.assertIn('datetime="2100-01-01T02:00+03:00"', response.text)
        for path in ['/admin/community', '/admin/dates', '/d/other1', '/c/other', f'/u/{self.other_user}']:
            with self.subTest(undated=path):
                self.assertNotIn('Дата уточняется', self.client.get(path).text)
        self.client.cookies.set('layout', 'list')
        try:
            listing = self.client.get('/admin/dates').text
            # Старое предпочтение больше не меняет единственный вид карточек.
            self.assertIn('class="dcard', listing)
            self.assertNotIn('class="drow ', listing)
            self.assertNotIn('class="dlist"', listing)
            self.assertNotIn('class="viewtog"', listing)
            self.assertIn('datetime="2099-12-31T23:30+03:00"', listing)
            self.assertNotIn('Дата уточняется', listing)
        finally:
            self.client.cookies.delete('layout')
        # Экспорт в календарь и возможность предложить дату остаются доступны.
        self.assertIn('data-ics="/d/other0/ics"', self.client.get('/d/other0').text)
        self.assertIn('Предложить дату', self.client.get('/d/other1').text)

    def test_mobile_and_desktop_dates_fit_both_skins_and_themes(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(reduced_motion='reduce')
                context.add_cookies([self.cookie])
                page = context.new_page()
                for width in (320, 390, 1280):
                    page.set_viewport_size({'width': width, 'height': 900})
                    for path in ['/admin/', '/admin/dates', f'/admin/categories/{self.cat}', '/c/other', '/d/other0',
                                 f'/u/{self.other_user}', f'/u/{self.uid}?tab=reviews',
                                 f'/d/reviewed/review/{self.review}']:
                        page.goto(self.backend.url + path)
                        page.locator('.event-date').first.wait_for()
                        for skin in ('friends', 'romantic'):
                            for theme in ('light', 'dark'):
                                page.evaluate("([skin, theme]) => { document.documentElement.dataset.skin = skin; document.documentElement.dataset.theme = theme; }", [skin, theme])
                                failures = page.locator('.event-date').evaluate_all("""dates => dates.flatMap(el => {
                                  const bounds = el.getBoundingClientRect();
                                  if (!bounds.width) return [];
                                  return [...el.querySelectorAll('time')].filter(time => {
                                    const r = time.getBoundingClientRect();
                                    return r.right > bounds.right + 1 || r.left < bounds.left - 1;
                                  }).map(time => time.textContent);
                                })""")
                                self.assertEqual(failures, [], (width, path, skin, theme))
                                self.assertEqual(page.locator('.event-date time').first.get_attribute('datetime'), '2099-12-31T23:30+03:00')
                                if path == f'/admin/categories/{self.cat}' and width <= 720:
                                    dated = page.locator(f'tr[data-did="{self.compact["own"][0]}"]')
                                    undated = page.locator(f'tr[data-did="{self.compact["own"][1]}"]')
                                    expect(undated.locator('.category-event-time')).to_be_hidden()
                                    self.assertGreater(dated.bounding_box()['height'], undated.bounding_box()['height'] + 8)
                context.close()
            finally:
                browser.close()

    def test_undated_cards_remove_date_row_and_shrink_to_content(self):
        own_dated, own_undated = self.compact['own']
        other_dated, other_undated = self.compact['other']
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(reduced_motion='reduce')
                context.add_cookies([self.cookie])
                page = context.new_page()
                surfaces = [
                    ('/admin/', 'cards', f'.cfeed-card[data-widget="{other_dated}"]', f'.cfeed-card[data-widget="{other_undated}"]'),
                    (f'/admin/dates?cat={self.cat}', 'cards', f'.dcard:has(.ttl a[href^="/admin/dates/{own_dated}/"])', f'.dcard:has(.ttl a[href^="/admin/dates/{own_undated}/"])'),
                    ('/admin/dates', 'list', f'.dcard:has(.ttl a[href^="/admin/dates/{own_dated}/"])', f'.dcard:has(.ttl a[href^="/admin/dates/{own_undated}/"])'),
                    ('/c/other', 'cards', f'#date-{other_dated}', f'#date-{other_undated}'),
                    (f'/u/{self.other_user}', 'cards', '.pub-card[href="/d/other-dated"]', '.pub-card[href="/d/other-undated"]'),
                ]
                for width in (320, 390, 1280):
                    page.set_viewport_size({'width': width, 'height': 900})
                    for path, layout, dated_selector, undated_selector in surfaces:
                        # Legacy cookie=list также должен давать карточки на любом экране.
                        context.add_cookies([{'name': 'layout', 'value': layout, 'url': self.backend.url}])
                        page.goto(self.backend.url + path)
                        dated, undated = page.locator(dated_selector), page.locator(undated_selector)
                        dated.wait_for()
                        undated.wait_for()
                        page.evaluate('document.fonts.ready')
                        for skin in ('friends', 'romantic'):
                            for theme in ('light', 'dark'):
                                with self.subTest(width=width, path=path, layout=layout, skin=skin, theme=theme):
                                    page.evaluate("([skin, theme]) => { document.documentElement.dataset.skin = skin; document.documentElement.dataset.theme = theme; }", [skin, theme])
                                    self.assertEqual(undated.locator('.event-date, .when').count(), 0)
                                    self.assertEqual(undated.locator('.meta:not(:has(*))').count(), 0)
                                    dated_height = dated.evaluate('(el) => el.getBoundingClientRect().height')
                                    undated_height = undated.evaluate('(el) => el.getBoundingClientRect().height')
                                    # Прямое действие занимает своё место, но пустой строки даты нет.
                                    undated_height -= undated.evaluate("""el => {
                                      const action = el.querySelector('.date-suggestion');
                                      if (!action) return 0;
                                      const css = getComputedStyle(action);
                                      return action.offsetHeight + parseFloat(css.marginTop) + parseFloat(css.marginBottom);
                                    }""")
                                    self.assertGreater(dated_height - undated_height, 8)
                context.close()
            finally:
                browser.close()

    def test_undated_guest_page_has_direct_and_menu_suggestions(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(reduced_motion='reduce')
                context.add_cookies([self.cookie])
                page = context.new_page()
                for width in (320, 390, 1280):
                    page.set_viewport_size({'width': width, 'height': 900})
                    heights = {}
                    for suffix in ('dated', 'undated'):
                        page.goto(self.backend.url + f'/d/other-{suffix}')
                        page.evaluate('document.fonts.ready')
                        card = page.locator('article.card')
                        for skin in ('friends', 'romantic'):
                            for theme in ('light', 'dark'):
                                page.evaluate("([skin, theme]) => { document.documentElement.dataset.skin = skin; document.documentElement.dataset.theme = theme; }", [skin, theme])
                                heights[suffix, skin, theme] = card.evaluate("""el => {
                                  const action = el.querySelector('.date-suggestion');
                                  const css = action && getComputedStyle(action);
                                  return el.getBoundingClientRect().height - (action
                                    ? action.offsetHeight + parseFloat(css.marginTop) + parseFloat(css.marginBottom) : 0);
                                }""")
                    self.assertEqual(card.locator('.event-date, .meta').count(), 0)
                    for skin in ('friends', 'romantic'):
                        for theme in ('light', 'dark'):
                            with self.subTest(width=width, skin=skin, theme=theme):
                                self.assertGreater(heights['dated', skin, theme] - heights['undated', skin, theme], 8)
                    for path, selector in (
                        ('/d/other-undated', 'article.card'),
                        ('/c/other', f'#date-{self.compact["other"][1]}'),
                    ):
                        page.goto(self.backend.url + path)
                        card = page.locator(selector)
                        direct = card.locator('.date-suggestion .chip-suggest')
                        expect(direct).to_be_visible()
                        expect(direct).to_have_text('Предложить дату')
                        direct.click()
                        expect(page.locator('#timeDlg')).to_be_visible()
                        expect(page.locator('#timeDateId')).to_have_value(str(self.compact['other'][1]))
                        page.locator('#timeCancel').click()
                        expect(direct).to_be_focused()
                        menu = card.locator('.event-card-menu')
                        button = menu.locator('.chip-suggest')
                        expect(button).to_have_text('Предложить дату')
                        expect(button).to_be_hidden()
                        menu.locator('summary').click()
                        for skin in ('friends', 'romantic'):
                            for theme in ('light', 'dark'):
                                page.evaluate("([skin, theme]) => { document.documentElement.dataset.skin = skin; document.documentElement.dataset.theme = theme; }", [skin, theme])
                                self.assertEqual(button.evaluate('(el) => getComputedStyle(el).backgroundColor'), 'rgba(0, 0, 0, 0)')
                                self.assertGreaterEqual(button.evaluate('(el) => el.offsetHeight'), 44)
                        button.click()
                        expect(page.locator('#timeDlg')).to_be_visible()
                        expect(page.locator('#timeDateId')).to_have_value(str(self.compact['other'][1]))
                        self.assertFalse(menu.evaluate('(el) => el.open'))
                        page.locator('#timeCancel').click()
                        expect(menu.locator('summary')).to_be_focused()
                context.close()
            finally:
                browser.close()

    def test_widget_date_has_space_below_author(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(reduced_motion='reduce')
                context.add_cookies([self.cookie])
                page = context.new_page()
                for width in (320, 390, 1280):
                    page.set_viewport_size({'width': width, 'height': 900})
                    for surface in ('feed', 'profile'):
                        if surface == 'feed':
                            page.goto(self.backend.url + '/admin/')
                            page.locator(f'[data-widget="{self.other_date}"] [data-community-open]').click()
                            widget = page.locator('#communityDlg[open] .cwid')
                        else:
                            page.goto(self.backend.url + f'/u/{self.other_user}')
                            page.locator('.pub-card[href="/d/other0"]').click()
                            widget = page.locator('#profileEventDlg[open] .cwid')
                        widget.locator('.event-date').wait_for()
                        for skin in ('friends', 'romantic'):
                            for theme in ('light', 'dark'):
                                with self.subTest(width=width, surface=surface, skin=skin, theme=theme):
                                    page.evaluate("([skin, theme]) => { document.documentElement.dataset.skin = skin; document.documentElement.dataset.theme = theme; }", [skin, theme])
                                    gap = widget.evaluate("""el =>
                                      el.querySelector('.event-date').getBoundingClientRect().top
                                      - el.querySelector('.cfeed-owner').getBoundingClientRect().bottom
                                    """)
                                    self.assertGreaterEqual(gap, 8)
                context.close()
            finally:
                browser.close()

    def test_direct_guest_date_suggestion_sends_to_both_guest_endpoints(self):
        did = self.compact['other'][1]
        start = '2099-10-05T20:00'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(reduced_motion='reduce', viewport={'width': 390, 'height': 900})
            context.add_cookies([self.cookie])
            page = context.new_page()
            try:
                for path, category_token in (('/d/other-undated', None), ('/c/other', 'other')):
                    page.goto(self.backend.url + path)
                    page.locator(f'#date-{did} .date-suggestion .chip-suggest').click()
                    page.locator('#timeStart').fill(start)
                    with page.expect_response(lambda response: response.url.endswith(path + '/suggest_time')) as response, \
                            page.expect_navigation(wait_until='domcontentloaded'):
                        page.locator('#timeForm [type="submit"]').click()
                    self.assertEqual(response.value.status, 200)
                    conn = self.backend.db.connect()
                    try:
                        question = conn.execute(
                            'SELECT * FROM questions WHERE date_id=? AND user_id=? AND suggest_starts=? ORDER BY id DESC LIMIT 1',
                            (did, self.uid, start),
                        ).fetchone()
                        self.assertIsNotNone(question)
                        expected_category = conn.execute(
                            'SELECT id FROM categories WHERE link_token=?', (category_token,),
                        ).fetchone()['id'] if category_token else None
                        self.assertEqual(question['category_id'], expected_category)
                        self.assertIn('Предлагаю назначить', question['text'])
                    finally:
                        conn.close()
                    expect(page.locator(f'#date-{did} .qa-q').last).to_contain_text('Предлагаю назначить')
            finally:
                context.close()
                browser.close()
                conn = self.backend.db.connect()
                try:
                    conn.execute('DELETE FROM questions WHERE date_id=? AND user_id=? AND suggest_starts=?',
                                 (did, self.uid, start))
                    conn.commit()
                finally:
                    conn.close()

    def test_direct_guest_date_suggestion_keeps_anonymous_login_requirement(self):
        did = self.compact['other'][1]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(reduced_motion='reduce')
            page = context.new_page()
            try:
                for path in ('/d/other-undated', '/c/other'):
                    page.goto(self.backend.url + path)
                    page.locator(f'#date-{did} .date-suggestion .chip-suggest').click()
                    expect(page.locator('#loginDlg')).to_be_visible()
                    expect(page.locator('#loginDlgTitle')).to_have_text('Войти, чтобы предложить дату')
                    expect(page.locator('#timeDlg')).to_be_hidden()
            finally:
                context.close()
                browser.close()


if __name__ == '__main__':
    unittest.main()
