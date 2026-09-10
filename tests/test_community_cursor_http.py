"""FLOW-04: числовые курсоры через HTTP, настоящую сессию и SQLite."""

import re
import unittest
from datetime import datetime

import test_bulk_http as bulk

import community_feed
import community_search

INT64_MAX = 9_223_372_036_854_775_807
INT64_MIN = -9_223_372_036_854_775_808
CURSOR_STAMP = "20300101120000"


class CommunityCursorHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bulk.db.init_db()

    def setUp(self):
        self.flow = bulk.BulkHttpTests()
        self.addCleanup(self.flow.doCleanups)
        self.flow.setUp()
        self.conn, self.client = self.flow.conn, self.flow.client
        self.ids = [self.flow.date("make_private", owner=self.flow.foreign_id) for _ in range(26)]
        for index, did in enumerate(self.ids):
            self.conn.execute("UPDATE dates SET name=? WHERE id=?",
                              (f"{'Кино' if index < 18 else 'Прогулка'} {index}", did))
        # Ранжирование отличается от хронологии: полная старая карточка выше.
        self.conn.execute("UPDATE dates SET place='Парк' WHERE id=?", (self.ids[0],))
        self.conn.commit()

    def page(self, cursor=None, *, query=None):
        parameters = {}
        if cursor is not None:
            parameters["cursor"] = cursor
        if query is not None:
            parameters["q"] = query
        response = self.client.get("/admin/community", params=parameters)
        self.assertEqual(response.status_code, 200, f"cursor={str(cursor)[:100]}: {response.text[:200]}")
        return response

    def card_ids(self, response):
        return [int(value) for value in re.findall(r'<article\b[^>]*data-widget="(\d+)"', response.text)]

    def next_cursor(self, response):
        match = re.search(r'data-next-cursor="([^"]+)"', response.text)
        return match.group(1) if match else None

    def snapshot(self):
        return [tuple(row) for row in self.conn.execute("SELECT * FROM dates ORDER BY id")]

    def assert_resets(self, cursors, *, query=None):
        expected = self.card_ids(self.page(query=query))
        before = self.snapshot()
        for cursor in cursors:
            with self.subTest(cursor=str(cursor)[:100], query=query):
                self.assertEqual(self.card_ids(self.page(cursor, query=query)), expected)
                self.assertEqual(self.snapshot(), before)

    def test_out_of_range_components_do_not_escape_as_http_500(self):
        for cursor in (str(INT64_MAX + 1), f"c1.{INT64_MAX + 1}",
                       f"r1.{CURSOR_STAMP}.{INT64_MAX + 1}.0"):
            with self.subTest(cursor=cursor):
                self.page(cursor)

    def test_every_id_component_resets_outside_positive_int64_range(self):
        invalid = (INT64_MIN - 1, INT64_MIN, -1, 0, INT64_MAX + 1, 10 ** 100)
        cursors = []
        for value in invalid:
            cursors.extend((str(value), f"c1.{value}", f"c1.{self.ids[-1]}.{value}",
                            f"r1.{CURSOR_STAMP}.{value}.0"))
        self.assert_resets(cursors)

    def test_boundary_ids_and_existing_numeric_formats_remain_valid(self):
        all_latest = list(reversed(self.ids))[:community_feed.PAGE_SIZE]
        for cursor in (str(INT64_MAX), f"c1.{INT64_MAX}", f"c1.{INT64_MAX}.{INT64_MAX}"):
            with self.subTest(cursor=cursor):
                self.assertEqual(self.card_ids(self.page(cursor)), all_latest)
        ranked = [self.ids[0], *reversed(self.ids[1:])]
        self.assertEqual(self.card_ids(self.page(f"r1.{CURSOR_STAMP}.{INT64_MAX}.0")), ranked[:12])
        before_id = self.ids[15]
        expected = list(reversed(self.ids[:15]))[:12]
        for cursor in (str(before_id), f"000{before_id}", f"  {before_id}  ",
                       f"c1.{before_id}", f"c1.{before_id}.{self.flow.foreign_id}"):
            with self.subTest(cursor=cursor):
                self.assertEqual(self.card_ids(self.page(cursor)), expected)
        self.assertEqual(self.card_ids(self.page("1")), [])
        self.assertEqual(self.card_ids(self.page("c1.1.1")), [])

    def test_ranked_offset_boundaries_and_malformed_composite_reset(self):
        max_id = self.ids[-1]
        ranked = [self.ids[0], *reversed(self.ids[1:])]
        self.assertEqual(self.card_ids(self.page(f"r1.{CURSOR_STAMP}.{max_id}.12")), ranked[12:24])
        self.assertEqual(self.card_ids(self.page(f"r1.{CURSOR_STAMP}.{max_id}.{community_feed.RANKING_POOL_SIZE}")), [])
        self.assert_resets([
            cursor for value in (INT64_MIN - 1, INT64_MIN, INT64_MAX, INT64_MAX + 1)
            for cursor in (f"r1.{CURSOR_STAMP}.{max_id}.{value}", f"r1.{value}.{max_id}.0")
        ])
        self.assert_resets((
            f"r1.{CURSOR_STAMP}.{max_id}.{community_feed.RANKING_POOL_SIZE + 1}",
            f"r1.{CURSOR_STAMP}.{max_id}.{INT64_MAX}",
            f"r1.{CURSOR_STAMP}.{max_id}.-1", f"r1.{CURSOR_STAMP}.{max_id}.",
            f"r1.20309999120000.{max_id}.0", f"r1.{CURSOR_STAMP}.{max_id}",
            f"r1.{CURSOR_STAMP}.{max_id}.0.extra", f"c1.{max_id}.",
            f"c1.{max_id}.1.extra", "c1..1", "r1..1.0", "abc", "1.5", "1e10",
            "²", "9" * 10000, "c1." + "9" * 10000,
            f"c1.{max_id}." + "9" * 10000,
            f"r1.{CURSOR_STAMP}." + "9" * 10000 + ".0",
        ))

    def test_invalid_cursor_reset_can_continue_ranked_pagination_without_losses(self):
        response = self.page(str(INT64_MAX + 1))
        seen = self.card_ids(response)
        for _ in range(5):
            cursor = self.next_cursor(response)
            if cursor is None:
                break
            self.assertTrue(cursor.startswith("r1."))
            response = self.page(cursor)
            seen.extend(self.card_ids(response))
        self.assertEqual(seen, [self.ids[0], *reversed(self.ids[1:])])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertIsNone(self.next_cursor(response))

    def test_search_components_are_bounded_and_keep_query_signature(self):
        query = community_search.prepare_query("Кино")
        signature = community_feed._search_signature(query)
        maximum = self.ids[-1]
        invalid = []
        for value in (INT64_MIN - 1, INT64_MIN, -1, 0, INT64_MAX + 1, "9" * 10000):
            invalid.append(f"s1.{CURSOR_STAMP}.{value}.0.{signature}")
        invalid.extend([
            cursor for value in (INT64_MIN - 1, INT64_MIN, INT64_MAX, INT64_MAX + 1)
            for cursor in (f"s1.{CURSOR_STAMP}.{maximum}.{value}.{signature}",
                           f"s1.{value}.{maximum}.0.{signature}")
        ])
        invalid.extend((
            f"s1.{CURSOR_STAMP}.{maximum}.{community_feed.SEARCH_POOL_SIZE + 1}.{signature}",
            f"s1.{CURSOR_STAMP}.{maximum}.{INT64_MAX}.{signature}",
            f"s1.{CURSOR_STAMP}.{maximum}.-1.{signature}",
            f"s1.{CURSOR_STAMP}.{maximum}.0.wrong", f"s1.20309999120000.{maximum}.0.{signature}",
            f"s1.{CURSOR_STAMP}.{maximum}.0", f"s1.{CURSOR_STAMP}.{maximum}.0.{signature}.extra",
        ))
        self.assert_resets(invalid, query="Кино")
        expected = [self.ids[0], *reversed(self.ids[1:18])]
        self.assertEqual(self.card_ids(self.page(f"s1.{CURSOR_STAMP}.{INT64_MAX}.0.{signature}", query="Кино")), expected[:12])
        self.assertEqual(self.card_ids(self.page(f"s1.{CURSOR_STAMP}.1.0.{signature}", query="Кино")), [])
        self.assertEqual(self.card_ids(self.page(f"s1.{CURSOR_STAMP}.{maximum}.{community_feed.SEARCH_POOL_SIZE}.{signature}", query="Кино")), [])

    def test_search_pagination_and_changed_query_reset_have_no_duplicate_or_missing_cards(self):
        first = self.page(query="Кино")
        cursor = self.next_cursor(first)
        self.assertTrue(cursor.startswith("s1."))
        second = self.page(cursor, query="Кино")
        seen = self.card_ids(first) + self.card_ids(second)
        self.assertEqual(seen, [self.ids[0], *reversed(self.ids[1:18])])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertIsNone(self.next_cursor(second))
        self.assertEqual(self.card_ids(self.page(cursor, query="Прогулка")), list(reversed(self.ids[18:])))

    def test_parser_handles_direct_integer_callers_and_rejects_unbindable_ids(self):
        self.assertEqual(community_feed._parse_cursor(INT64_MAX), ("chronological", (INT64_MAX, None)))
        for raw in (INT64_MIN - 1, INT64_MIN, -1, 0, INT64_MAX + 1, 10 ** 100):
            with self.subTest(raw=raw):
                self.assertEqual(community_feed._parse_cursor(raw), ("fresh", None))
        self.assertEqual(community_feed._parse_cursor(10 ** 10000), ("fresh", None))
        self.assertEqual(community_feed._parse_cursor(f"c1.1.{INT64_MAX}"),
                         ("chronological", (1, INT64_MAX)))
        parsed = community_feed._parse_cursor(f"r1.{CURSOR_STAMP}.{INT64_MAX}.0")
        self.assertEqual(parsed, ("ranked", (datetime(2030, 1, 1, 12), INT64_MAX, 0)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
