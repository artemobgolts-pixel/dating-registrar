"""FUN-02: продолжение ленты через HTTP после изменения видимых событий."""

import unittest

import test_community_cursor_http as cursor_http


class FeedMutationHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cursor_http.CommunityCursorHttpTests.setUpClass()

    def setUp(self):
        self.fixture = cursor_http.CommunityCursorHttpTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.conn = self.fixture.conn
        self.ids = self.fixture.ids

    def page(self, cursor=None, *, query=None):
        return self.fixture.page(cursor, query=query)

    def card_ids(self, response):
        return self.fixture.card_ids(response)

    def complete_feed(self, first, *, query=None):
        """Моделируем HTTP-контракт: reset заменяет карточки, обычная страница дополняет."""
        result = self.card_ids(first)
        resets = 0
        response = first
        for _ in range(10):
            cursor = self.fixture.next_cursor(response)
            if cursor is None:
                return result, resets
            response = self.page(cursor, query=query)
            if response.headers.get("X-Feed-Reset") == "1":
                result = []
                resets += 1
            result.extend(self.card_ids(response))
            self.assertEqual(len(result), len(set(result)),
                             "Appending a page without an explicit reset must not duplicate cards")
        self.fail("Pagination did not terminate within ten continuation pages")

    def expected_ids(self, *, query=None):
        members = self.ids if query is None else self.ids[:18]
        return [members[0], *reversed(members[1:])]

    def assert_recovers(self, first, expected, *, query=None):
        before = self.fixture.snapshot()
        seen, resets = self.complete_feed(first, query=query)
        self.assertEqual(seen, expected,
                         "All currently active results must appear after an explicit restart")
        self.assertEqual(resets, 1, "Changed ranked/search results must signal one safe restart")
        self.assertEqual(self.fixture.snapshot(), before, "Feed reads must not mutate persisted dates")

    def assert_archive_seen_preserves_unseen(self, *, query=None):
        first = self.page(query=query)
        first_ids = self.card_ids(first)
        self.assertEqual(first_ids, self.expected_ids(query=query)[:12])
        archived = first_ids[0]
        self.conn.execute("UPDATE dates SET archived_at=? WHERE id=?",
                          ("2030-01-01T12:00:00", archived))
        self.conn.commit()
        expected = [did for did in self.expected_ids(query=query) if did != archived]
        self.assert_recovers(first, expected, query=query)
        self.assertIsNotNone(self.conn.execute(
            "SELECT archived_at FROM dates WHERE id=?", (archived,),
        ).fetchone()[0])

    def assert_restore_relevant_item_recovers(self, *, query=None):
        restored = self.ids[0]
        self.conn.execute("UPDATE dates SET archived_at=? WHERE id=?",
                          ("2030-01-01T12:00:00", restored))
        self.conn.commit()
        first = self.page(query=query)
        self.assertNotIn(restored, self.card_ids(first))
        self.conn.execute("UPDATE dates SET archived_at=NULL WHERE id=?", (restored,))
        self.conn.commit()
        self.assert_recovers(first, self.expected_ids(query=query), query=query)
        self.assertIsNone(self.conn.execute(
            "SELECT archived_at FROM dates WHERE id=?", (restored,),
        ).fetchone()[0])

    def assert_seen_item_loses_visibility_safely(self, *, query=None):
        first = self.page(query=query)
        hidden = self.card_ids(first)[0]
        self.conn.execute("UPDATE dates SET is_public=0 WHERE id=?", (hidden,))
        self.conn.commit()
        expected = [did for did in self.expected_ids(query=query) if did != hidden]
        self.assert_recovers(first, expected, query=query)
        self.assertEqual(self.conn.execute(
            "SELECT is_public FROM dates WHERE id=?", (hidden,),
        ).fetchone()[0], 0)

    def assert_newer_item_triggers_complete_restart(self, *, query=None):
        first = self.page(query=query)
        newer = self.fixture.flow.date("make_private", owner=self.fixture.flow.foreign_id)
        self.conn.execute("UPDATE dates SET name='Кино новое' WHERE id=?", (newer,))
        self.conn.commit()
        original = self.expected_ids(query=query)
        expected = [original[0], newer, *original[1:]]
        self.assert_recovers(first, expected, query=query)

    def assert_unchanged_order(self, *, query=None):
        first = self.page(query=query)
        expected = self.expected_ids(query=query)
        self.assertEqual(self.card_ids(first), expected[:12])
        seen, resets = self.complete_feed(first, query=query)
        self.assertEqual(seen, expected)
        self.assertEqual(resets, 0)

    def test_ranked_archive_seen_does_not_skip_first_unseen(self):
        self.assert_archive_seen_preserves_unseen()

    def test_search_archive_seen_does_not_skip_first_unseen(self):
        self.assert_archive_seen_preserves_unseen(query="Кино")

    def test_ranked_restored_item_reappears_without_duplicates(self):
        self.assert_restore_relevant_item_recovers()

    def test_search_restored_item_reappears_without_duplicates(self):
        self.assert_restore_relevant_item_recovers(query="Кино")

    def test_ranked_seen_item_becomes_private_without_losing_active_cards(self):
        self.assert_seen_item_loses_visibility_safely()

    def test_search_seen_item_becomes_private_without_losing_active_cards(self):
        self.assert_seen_item_loses_visibility_safely(query="Кино")

    def test_ranked_newer_item_is_included_after_explicit_restart(self):
        self.assert_newer_item_triggers_complete_restart()

    def test_search_newer_item_is_included_after_explicit_restart(self):
        self.assert_newer_item_triggers_complete_restart(query="Кино")

    def test_ranked_unchanged_pages_preserve_deterministic_order(self):
        self.assert_unchanged_order()

    def test_search_unchanged_pages_preserve_deterministic_order(self):
        self.assert_unchanged_order(query="Кино")

    def test_search_seen_item_stops_matching_without_skipping_active_matches(self):
        first = self.page(query="Кино")
        removed = self.card_ids(first)[0]
        self.conn.execute("UPDATE dates SET name='Выставка' WHERE id=?", (removed,))
        self.conn.commit()
        expected = [did for did in self.expected_ids(query="Кино") if did != removed]
        self.assert_recovers(first, expected, query="Кино")

    def test_search_previously_unmatched_item_joins_without_duplicates(self):
        first = self.page(query="Кино")
        added = self.ids[-1]
        self.assertNotIn(added, self.card_ids(first))
        self.conn.execute("UPDATE dates SET name='Кино дополнительное' WHERE id=?", (added,))
        self.conn.commit()
        original = self.expected_ids(query="Кино")
        self.assert_recovers(first, [original[0], added, *original[1:]], query="Кино")

    def test_malformed_ranked_continuation_explicitly_restarts(self):
        first = self.page()
        invalid = self.fixture.next_cursor(first) + ".invalid"
        before = self.fixture.snapshot()
        restarted = self.page(invalid)
        self.assertEqual(restarted.headers.get("X-Feed-Reset"), "1")
        self.assertEqual(self.card_ids(restarted), self.expected_ids()[:12])
        seen, resets = self.complete_feed(restarted)
        self.assertEqual(seen, self.expected_ids())
        self.assertEqual(resets, 0)
        self.assertEqual(self.fixture.snapshot(), before)

    def test_search_cursor_cannot_continue_a_different_query(self):
        first = self.page(query="Кино")
        cursor = self.fixture.next_cursor(first)
        restarted = self.page(cursor, query="Прогулка")
        self.assertEqual(restarted.headers.get("X-Feed-Reset"), "1")
        self.assertEqual(self.card_ids(restarted), list(reversed(self.ids[18:])))
        self.assertIsNone(self.fixture.next_cursor(restarted))

    def assert_v2_components_reset_safely(self, *, query=None):
        first = self.page(query=query)
        parts = self.fixture.next_cursor(first).split(".")
        self.assertEqual(parts[0], "r2" if query is None else "s2")
        limit = (cursor_http.community_feed.RANKING_POOL_SIZE if query is None
                 else cursor_http.community_feed.SEARCH_POOL_SIZE)
        invalid_fields = {
            1: ("20309999120000", "-1", "20300101120000extra"),
            2: ("0", "-1", str(cursor_http.INT64_MAX + 1), "9" * 10000),
            3: ("-1", str(limit + 1), "1.5", "9" * 10000),
            len(parts) - 1: ("invalid", "a" * 23, "я" * 24),
        }
        if query is not None:
            invalid_fields[4] = ("wrong", "я" * 10)
        cursors = []
        for index, values in invalid_fields.items():
            for value in values:
                malformed = list(parts)
                malformed[index] = value
                cursors.append(".".join(malformed))
        self.fixture.assert_resets(cursors, query=query)

    def test_ranked_v2_cursor_fields_are_bounded_and_malformed_input_restarts(self):
        self.assert_v2_components_reset_safely()

    def test_search_v2_cursor_fields_are_bounded_and_malformed_input_restarts(self):
        self.assert_v2_components_reset_safely(query="Кино")

    def assert_score_change_restarts_without_losing_unseen(self, *, query=None):
        first = self.page(query=query)
        boosted = self.ids[1]
        self.assertNotIn(boosted, self.card_ids(first))
        self.conn.execute("UPDATE dates SET place='Парк',comment='Подробности' WHERE id=?",
                          (boosted,))
        self.conn.commit()
        expected = [boosted, *(did for did in self.expected_ids(query=query) if did != boosted)]
        self.assert_recovers(first, expected, query=query)

    def test_ranked_unseen_item_moves_before_seen_cards_after_score_change(self):
        self.assert_score_change_restarts_without_losing_unseen()

    def test_search_unseen_item_moves_before_seen_matches_after_score_change(self):
        self.assert_score_change_restarts_without_losing_unseen(query="Кино")


if __name__ == "__main__":
    unittest.main(verbosity=2)
