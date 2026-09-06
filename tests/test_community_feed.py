#!/usr/bin/env python3
"""Ранжирование, приватность и пагинация рекомендованной ленты."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-community-feed-")
os.environ["DATA_DIR"] = _IMPORT_DATA.name

import community_feed  # noqa: E402
import db  # noqa: E402


NOW = datetime(2030, 1, 1, 10, 0, 0)
STAMP = "2030-01-01T10:00:00"


class CommunityFeedTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        self.viewer = self._user("Зритель")

    def tearDown(self):
        self.conn.close()

    def _user(self, name: str) -> int:
        return int(self.conn.execute(
            "INSERT INTO users(display_name,created_at) VALUES(?,?)",
            (name, STAMP),
        ).lastrowid)

    def _event(
        self,
        owner_id: int,
        name: str,
        *,
        starts_at: str | None = "2030-01-15T18:00:00",
        created_at: str = STAMP,
        is_public: int = 1,
        comment: str | None = None,
        place: str | None = None,
        source_date_id: int | None = None,
        origin: str = "admin",
    ) -> int:
        return int(self.conn.execute(
            "INSERT INTO dates("
            "owner_id,name,starts_at,created_at,is_public,comment,place,"
            "source_date_id,origin,share_token"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                owner_id, name, starts_at, created_at, is_public, comment, place,
                source_date_id, origin, f"token-{name}-{owner_id}",
            ),
        ).lastrowid)

    def _names(self, page: community_feed.FeedPage) -> list[str]:
        return [str(row["name"]) for row in page.rows]

    def test_general_ranking_prefers_upcoming_and_excludes_started(self):
        owner = self._user("Автор")
        self._event(owner, "Далёкое", starts_at="2030-09-01T18:00:00")
        self._event(owner, "Скоро", starts_at="2030-01-04T18:00:00")
        self._event(owner, "Уже началось", starts_at="2030-01-01T09:59:00")

        result = community_feed.page(self.conn, self.viewer, now=NOW)

        self.assertEqual(result.mode, "general")
        self.assertEqual(self._names(result), ["Скоро", "Далёкое"])

    def test_other_users_wants_and_copies_raise_popularity(self):
        owner = self._user("Автор")
        popular = self._event(owner, "Популярное")
        self._event(owner, "Просто новое")
        fan_one = self._user("Первый участник")
        fan_two = self._user("Второй участник")
        for fan in (fan_one, fan_two):
            self.conn.execute(
                "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
                "VALUES(?,?,?,?)",
                (fan, popular, STAMP, STAMP),
            )
        self._event(
            fan_one,
            "Личная копия",
            is_public=0,
            source_date_id=popular,
            origin="copy",
        )

        result = community_feed.page(self.conn, self.viewer, now=NOW)

        self.assertEqual(self._names(result)[0], "Популярное")

    def test_previous_author_interaction_personalizes_order(self):
        preferred = self._user("Знакомый автор")
        other = self._user("Другой автор")
        history = self._event(preferred, "История", is_public=0)
        preferred_candidate = self._event(preferred, "От знакомого")
        other_candidate = self._event(other, "От другого")
        cold_viewer = self._user("Новый зритель")
        self.conn.execute(
            "INSERT INTO date_wants(user_id,date_id,created_at,updated_at) "
            "VALUES(?,?,?,?)",
            (self.viewer, history, STAMP, STAMP),
        )

        cold = community_feed.page(self.conn, cold_viewer, now=NOW)
        personalized = community_feed.page(self.conn, self.viewer, now=NOW)

        self.assertEqual(int(cold.rows[0]["id"]), other_candidate)
        self.assertEqual(cold.mode, "general")
        self.assertEqual(int(personalized.rows[0]["id"]), preferred_candidate)
        self.assertEqual(personalized.mode, "personalized")

    def test_complete_card_with_image_outranks_empty_card(self):
        owner = self._user("Автор")
        complete = self._event(
            owner, "Заполненное", comment="Подробности", place="Парк",
        )
        self.conn.execute(
            "INSERT INTO date_images(date_id,filename) VALUES(?,?)",
            (complete, "cover.webp"),
        )
        self._event(owner, "Пустое")

        result = community_feed.page(self.conn, self.viewer, now=NOW)

        self.assertEqual(self._names(result)[0], "Заполненное")

    def test_fresh_event_outranks_old_equivalent(self):
        owner = self._user("Автор")
        self._event(owner, "Старое", created_at="2029-01-01T10:00:00")
        self._event(owner, "Свежее", created_at="2029-12-31T10:00:00")

        result = community_feed.page(self.conn, self.viewer, now=NOW)

        self.assertEqual(self._names(result)[0], "Свежее")

    def test_adjacent_events_use_different_authors_when_possible(self):
        second_owner = self._user("Второй автор")
        first_owner = self._user("Первый автор")
        self._event(second_owner, "Запасной автор")
        self._event(first_owner, "Первый автор — 2")
        self._event(first_owner, "Первый автор — 1")

        result = community_feed.page(self.conn, self.viewer, now=NOW)

        owners = [int(row["owner_id"]) for row in result.rows]
        self.assertEqual(owners, [first_owner, second_owner, first_owner])

    def test_ranked_cursor_then_chronological_tail_has_no_losses(self):
        expected_ids: set[int] = set()
        for index in range(25):
            owner = self._user(f"Автор {index}")
            expected_ids.add(self._event(owner, f"Событие {index}"))

        seen: list[int] = []
        cursor = None
        modes: list[str] = []
        for _ in range(10):
            result = community_feed.page(
                self.conn,
                self.viewer,
                cursor,
                now=NOW,
                page_size=5,
                pool_size=20,
            )
            seen.extend(int(row["id"]) for row in result.rows)
            modes.append(result.mode)
            cursor = result.next_cursor
            if cursor is None:
                break

        self.assertEqual(set(seen), expected_ids)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertIn("chronological", modes)

    def test_author_diversity_survives_ranked_to_tail_page_boundary(self):
        owner_a = self._user("Автор A")
        owner_b = self._user("Автор B")
        self._event(owner_b, "Старое B")
        self._event(owner_a, "Старое A")
        self._event(owner_a, "Новое A")
        self._event(owner_b, "Новое B")

        first = community_feed.page(
            self.conn, self.viewer, now=NOW, page_size=2, pool_size=2,
        )
        second = community_feed.page(
            self.conn, self.viewer, first.next_cursor,
            now=NOW, page_size=2, pool_size=2,
        )

        owners = [int(row["owner_id"]) for row in first.rows + second.rows]
        self.assertEqual(owners, [owner_b, owner_a, owner_b, owner_a])

    def test_invalid_cursor_is_bounded_and_restarts_safely(self):
        owner = self._user("Автор")
        event_id = self._event(owner, "Доступное")

        result = community_feed.page(
            self.conn,
            self.viewer,
            "r1.20300101100000.1.999999 OR 1=1",
            now=NOW,
        )

        self.assertEqual([int(row["id"]) for row in result.rows], [event_id])


if __name__ == "__main__":
    unittest.main(verbosity=2)
