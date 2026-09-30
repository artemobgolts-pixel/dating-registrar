"""Дата события одинакова в карточках, ссылках и списках, включая узкий экран."""

import unittest
from datetime import datetime
from unittest.mock import patch

import httpx
from playwright.sync_api import sync_playwright

from live_backend import LiveBackend


class EventDatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = LiveBackend()
        cls.uid, cls.cookie = cls.backend.user_cookie()
        conn = cls.backend.db.connect()
        stamp = cls.backend.main.now_iso()
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
                self.assertIn('Дата уточняется', self.client.get(path).text)
        self.client.cookies.set('layout', 'list')
        try:
            listing = self.client.get('/admin/dates').text
            self.assertIn('class="drow', listing)
            self.assertIn('datetime="2099-12-31T23:30+03:00"', listing)
            self.assertIn('Дата уточняется', listing)
        finally:
            self.client.cookies.delete('layout')
        # Экспорт в календарь и возможность предложить дату остаются доступны.
        self.assertIn('data-ics="/d/other0/ics"', self.client.get('/d/other0').text)
        self.assertIn('предложить дату', self.client.get('/d/other1').text)

    def test_mobile_and_desktop_dates_fit_both_skins_and_themes(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(reduced_motion='reduce')
                context.add_cookies([self.cookie])
                page = context.new_page()
                for width in (320, 390, 1280):
                    page.set_viewport_size({'width': width, 'height': 900})
                    for path in ['/admin/', '/admin/dates', '/c/other', '/d/other0',
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
                context.close()
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
