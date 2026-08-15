#!/usr/bin/env python3
"""Точечные тесты доменной логики голосования и актуальной схемы.

Запуск из корня репозитория: ``python tests/test_voting.py``.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

# db создаёт DATA_DIR при импорте; тестовый каталог задаём заранее.
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-voting-import-")
os.environ["DATA_DIR"] = _IMPORT_DATA.name

import db  # noqa: E402
import voting  # noqa: E402


NOW = "2030-01-01T10:00:00"
DEADLINE = "2030-01-02T10:00:00"
AFTER_DEADLINE = "2030-01-02T10:00:01"
STARTS = "2030-01-10T19:00:00"


class VotingDomainTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        for telegram_id, name in ((1001, "Владелец"), (1002, "Аня"),
                                  (1003, "Борис"), (1004, "Вера"),
                                  (1005, "Глеб")):
            self.conn.execute(
                "INSERT INTO users(telegram_id, display_name, created_at) VALUES(?,?,?)",
                (telegram_id, name, NOW),
            )
        self.owner_id = 1

    def tearDown(self):
        self.conn.close()

    def category(self, name="Категория") -> int:
        cur = self.conn.execute(
            "INSERT INTO categories(owner_id, name, created_at) VALUES(?,?,?)",
            (self.owner_id, name, NOW),
        )
        return int(cur.lastrowid)

    def date(self, *categories: int, name="Событие", capacity=5,
             starts_at=STARTS) -> int:
        cur = self.conn.execute(
            "INSERT INTO dates(owner_id, name, starts_at, capacity, created_at) "
            "VALUES(?,?,?,?,?)",
            (self.owner_id, name, starts_at, capacity, NOW),
        )
        date_id = int(cur.lastrowid)
        for category_id in categories:
            self.conn.execute(
                "INSERT INTO date_categories(date_id, category_id) VALUES(?,?)",
                (date_id, category_id),
            )
        return date_id

    def assert_error(self, code, fn, *args, **kwargs):
        with self.assertRaises(voting.VotingError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.as_dict()["code"], code)
        return caught.exception

    def configure(self, category_id, mode=voting.CHOICE_MULTIPLE):
        return voting.configure_category(
            self.conn, category_id, self.owner_id, mode, DEADLINE, now=NOW
        )

    def test_manual_configuration_and_deadline_validation(self):
        category_id = self.category()
        date_id = self.date(category_id)

        self.assert_error(
            "voting_unconfigured", voting.cast_vote,
            self.conn, category_id, date_id, 2, now=NOW,
        )
        self.assert_error(
            "invalid_choice_mode", voting.configure_category,
            self.conn, category_id, self.owner_id, "two", DEADLINE, now=NOW,
        )
        self.assert_error(
            "deadline_not_future", voting.configure_category,
            self.conn, category_id, self.owner_id, "single", NOW, now=NOW,
        )
        self.assert_error(
            "deadline_not_before_start", voting.configure_category,
            self.conn, category_id, self.owner_id, "single", STARTS, now=NOW,
        )

        state = self.configure(category_id, voting.CHOICE_SINGLE)
        self.assertEqual(state.status, voting.STATUS_OPEN)
        self.assertEqual(state.choice_mode, voting.CHOICE_SINGLE)
        self.assertEqual(state.voting_deadline, DEADLINE)
        self.assertIsNone(state.closed_at)
        self.assert_error(
            "owner_cannot_vote", voting.cast_vote,
            self.conn, category_id, date_id, self.owner_id, now=NOW,
        )

    def test_moving_open_deadline_preserves_existing_votes(self):
        category_id = self.category()
        first = self.date(category_id, name="Первый")
        second = self.date(category_id, name="Второй")
        self.configure(category_id, voting.CHOICE_MULTIPLE)
        voting.cast_vote(self.conn, category_id, first, 2, now=NOW)
        voting.cast_vote(self.conn, category_id, second, 2, now=NOW)
        booking_ids = [row["id"] for row in self.conn.execute(
            "SELECT id FROM bookings WHERE category_id=? ORDER BY id",
            (category_id,),
        )]

        extended = voting.configure_category(
            self.conn, category_id, self.owner_id, voting.CHOICE_MULTIPLE,
            "2030-01-03T10:00:00", now=NOW,
        )
        self.assertEqual(extended.voting_deadline, "2030-01-03T10:00:00")
        self.assertEqual(extended.total_votes, 2)
        self.assertEqual(extended.vote_counts, {first: 1, second: 1})

        moved_manually = voting.configure_category(
            self.conn, category_id, self.owner_id, voting.CHOICE_MULTIPLE,
            "2030-01-01T20:00:00", now=NOW,
        )
        self.assertEqual(moved_manually.voting_deadline, "2030-01-01T20:00:00")
        self.assertEqual(moved_manually.total_votes, 2)
        self.assertEqual(
            [row["id"] for row in self.conn.execute(
                "SELECT id FROM bookings WHERE category_id=? ORDER BY id",
                (category_id,),
            )],
            booking_ids,
        )

    def test_expired_open_deadline_can_be_moved_to_a_future_moment(self):
        category_id = self.category()
        date_id = self.date(category_id)
        self.configure(category_id, voting.CHOICE_SINGLE)
        voting.cast_vote(self.conn, category_id, date_id, 2, now=NOW)

        reopened = voting.configure_category(
            self.conn, category_id, self.owner_id, voting.CHOICE_SINGLE,
            "2030-01-03T10:00:00", now=AFTER_DEADLINE,
        )

        self.assertEqual(reopened.status, voting.STATUS_OPEN)
        self.assertEqual(reopened.voting_deadline, "2030-01-03T10:00:00")
        self.assertEqual(reopened.vote_counts, {date_id: 1})
        self.assertIsNone(reopened.closed_at)
        self.assert_error(
            "deadline_not_reached", voting.close_category,
            self.conn, category_id, now=AFTER_DEADLINE,
        )

    def test_each_closed_outcome_can_start_a_new_round_without_losing_ballots(self):
        scenarios = (
            ("no_winner", voting.STATUS_NO_WINNER),
            ("resolved", voting.STATUS_RESOLVED),
            ("tie", voting.STATUS_TIE),
        )
        for label, expected_status in scenarios:
            with self.subTest(outcome=label):
                category_id = self.category(f"Повтор: {label}")
                first = self.date(category_id, name=f"{label}: первый")
                second = self.date(category_id, name=f"{label}: второй")
                self.configure(category_id, voting.CHOICE_MULTIPLE)
                if label == "resolved":
                    voting.cast_vote(self.conn, category_id, first, 2, now=NOW)
                    voting.cast_vote(self.conn, category_id, first, 3, now=NOW)
                elif label == "tie":
                    voting.cast_vote(self.conn, category_id, first, 2, now=NOW)
                    voting.cast_vote(self.conn, category_id, second, 3, now=NOW)
                closed = voting.close_category(
                    self.conn, category_id, now=AFTER_DEADLINE,
                )
                self.assertEqual(closed.status, expected_status)
                if label == "resolved":
                    voting.withdraw_participation(
                        self.conn, category_id, 2, now=AFTER_DEADLINE,
                    )
                ballots_before = [tuple(row) for row in self.conn.execute(
                    "SELECT id,date_id,user_id,participation_withdrawn_at "
                    "FROM bookings WHERE category_id=? ORDER BY id",
                    (category_id,),
                )]

                reopened = voting.configure_category(
                    self.conn, category_id, self.owner_id,
                    voting.CHOICE_MULTIPLE, "2030-01-03T10:00:00",
                    now=AFTER_DEADLINE,
                )

                self.assertEqual(reopened.status, voting.STATUS_OPEN)
                self.assertIsNone(reopened.closed_at)
                self.assertIsNone(reopened.resolved_at)
                self.assertIsNone(reopened.winner_date_id)
                self.assertEqual(reopened.total_votes, closed.total_votes)
                self.assertEqual(
                    [tuple(row) for row in self.conn.execute(
                        "SELECT id,date_id,user_id,participation_withdrawn_at "
                        "FROM bookings WHERE category_id=? ORDER BY id",
                        (category_id,),
                    )],
                    ballots_before,
                )

    def test_closed_round_with_a_ballot_for_archived_event_cannot_reopen(self):
        category_id = self.category("Уже завершившаяся встреча")
        date_id = self.date(category_id, starts_at=None)
        self.configure(category_id, voting.CHOICE_SINGLE)
        voting.cast_vote(self.conn, category_id, date_id, 2, now=NOW)
        closed = voting.close_category(
            self.conn, category_id, now=AFTER_DEADLINE,
        )
        self.assertEqual(closed.status, voting.STATUS_RESOLVED)
        # Так выглядит недатированный победитель после обзора и autoarchive:
        # бюллетень остаётся, но само событие уже не показывается в опросе.
        self.conn.execute(
            "UPDATE dates SET archived_at=? WHERE id=?",
            (AFTER_DEADLINE, date_id),
        )

        error = self.assert_error(
            "unavailable_ballots_prevent_reopen", voting.configure_category,
            self.conn, category_id, self.owner_id, voting.CHOICE_SINGLE,
            "2030-01-03T10:00:00", now=AFTER_DEADLINE,
        )

        self.assertEqual(error.details["date_id"], date_id)
        unchanged = voting.get_category_state(self.conn, category_id)
        self.assertEqual(unchanged.status, voting.STATUS_RESOLVED)
        self.assertEqual(unchanged.winner_date_id, date_id)
        self.assertEqual(unchanged.vote_counts, {date_id: 1})

    def test_capacity_is_counted_independently_per_category(self):
        first = self.category("Первая")
        second = self.category("Вторая")
        date_id = self.date(first, second, capacity=2)
        self.configure(first)
        self.configure(second)

        voting.cast_vote(self.conn, first, date_id, 2, now=NOW)
        voting.cast_vote(self.conn, first, date_id, 3, now=NOW)
        self.assert_error(
            "capacity_reached", voting.cast_vote,
            self.conn, first, date_id, 4, now=NOW,
        )

        # Тот же date_id независимо набирает ещё 2/2 во второй категории.
        voting.cast_vote(self.conn, second, date_id, 4, now=NOW)
        voting.cast_vote(self.conn, second, date_id, 5, now=NOW)
        counts = {
            row["category_id"]: row["n"]
            for row in self.conn.execute(
                "SELECT category_id, COUNT(*) AS n FROM bookings "
                "WHERE date_id=? GROUP BY category_id", (date_id,)
            )
        }
        self.assertEqual(counts, {first: 2, second: 2})

        # Повторный запрос того же участника идемпотентен и не ест capacity.
        again = voting.cast_vote(self.conn, second, date_id, 4, now=NOW)
        self.assertFalse(again.created)
        self.assertEqual(again.date_votes, 2)

    def test_single_replaces_choice_and_multiple_keeps_all(self):
        single = self.category("Один")
        multiple = self.category("Несколько")
        first = self.date(single, multiple, name="А")
        second = self.date(single, multiple, name="Б")
        self.configure(single, voting.CHOICE_SINGLE)
        self.configure(multiple, voting.CHOICE_MULTIPLE)

        voting.cast_vote(self.conn, single, first, 2, now=NOW)
        moved = voting.cast_vote(self.conn, single, second, 2, now=NOW)
        self.assertEqual(moved.removed_date_ids, (first,))
        self.assertEqual(moved.current_date_ids, (second,))

        voting.cast_vote(self.conn, multiple, first, 2, now=NOW)
        added = voting.cast_vote(self.conn, multiple, second, 2, now=NOW)
        self.assertEqual(added.current_date_ids, (first, second))
        removed = voting.remove_vote(self.conn, multiple, first, 2, now=NOW)
        self.assertTrue(removed.removed)
        self.assertEqual(removed.current_date_ids, (second,))

    def test_legacy_multiple_ballots_require_compatible_manual_mode(self):
        category_id = self.category("Старая")
        first = self.date(category_id, name="А")
        second = self.date(category_id, name="Б")
        # unconfigured не запрещает сохранённые до v22 строки; новые пишет
        # только voting.cast_vote, который это состояние блокирует.
        for date_id in (first, second):
            self.conn.execute(
                "INSERT INTO bookings(date_id, category_id, guest_token, user_id, created_at) "
                "VALUES(?,?,?,?,?)", (date_id, category_id, "u2", 2, NOW)
            )
        self.assert_error(
            "existing_votes_incompatible", voting.configure_category,
            self.conn, category_id, self.owner_id, "single", DEADLINE, now=NOW,
        )
        state = self.configure(category_id, voting.CHOICE_MULTIPLE)
        self.assertEqual(state.total_votes, 2)

    def test_legacy_unconfigured_ballot_can_be_removed_but_not_added(self):
        category_id = self.category("Старая снимаемая")
        date_id = self.date(category_id)
        self.conn.execute(
            "INSERT INTO bookings(date_id, category_id, guest_token, user_id, created_at) "
            "VALUES(?,?,?,?,?)", (date_id, category_id, "u2", 2, NOW),
        )
        removed = voting.remove_vote(
            self.conn, category_id, date_id, 2, now=NOW,
        )
        self.assertTrue(removed.removed)
        self.assertEqual(removed.current_date_ids, ())
        self.assert_error(
            "voting_unconfigured", voting.cast_vote,
            self.conn, category_id, date_id, 2, now=NOW,
        )

    def test_zero_votes_finishes_without_winner(self):
        category_id = self.category()
        self.date(category_id)
        self.configure(category_id)

        self.assert_error(
            "deadline_not_reached", voting.close_category,
            self.conn, category_id, now=NOW,
        )
        state = voting.close_category(
            self.conn, category_id, now=AFTER_DEADLINE
        )
        self.assertEqual(state.status, voting.STATUS_NO_WINNER)
        self.assertEqual(state.total_votes, 0)
        self.assertIsNone(state.winner_date_id)
        self.assertEqual(state.closed_at, AFTER_DEADLINE)
        self.assertEqual(state.resolved_at, AFTER_DEADLINE)
        self.assertEqual(
            voting.close_category(self.conn, category_id, now=AFTER_DEADLINE), state
        )

    def test_tie_is_resolved_manually_only_among_leaders(self):
        category_id = self.category()
        first = self.date(category_id, name="А")
        second = self.date(category_id, name="Б")
        third = self.date(category_id, name="В")
        self.configure(category_id)
        voting.cast_vote(self.conn, category_id, first, 2, now=NOW)
        voting.cast_vote(self.conn, category_id, second, 3, now=NOW)

        tied = voting.close_category(self.conn, category_id, now=AFTER_DEADLINE)
        self.assertEqual(tied.status, voting.STATUS_TIE)
        self.assertEqual(tied.leader_date_ids, (first, second))
        self.assertIsNone(tied.resolved_at)
        self.assert_error(
            "winner_not_leader", voting.resolve_tie,
            self.conn, category_id, self.owner_id, third, now=AFTER_DEADLINE,
        )
        self.assert_error(
            "category_not_found", voting.resolve_tie,
            self.conn, category_id, 2, first, now=AFTER_DEADLINE,
        )

        result = voting.resolve_tie(
            self.conn, category_id, self.owner_id, second, now=AFTER_DEADLINE
        )
        self.assertEqual(result.status, voting.STATUS_RESOLVED)
        self.assertEqual(result.winner_date_id, second)

    def test_result_is_immutable_and_withdrawal_keeps_ballot(self):
        category_id = self.category()
        winner = self.date(category_id, name="Лидер")
        other = self.date(category_id, name="Другой")
        self.configure(category_id)
        voting.cast_vote(self.conn, category_id, winner, 2, now=NOW)
        voting.cast_vote(self.conn, category_id, winner, 3, now=NOW)
        voting.cast_vote(self.conn, category_id, other, 4, now=NOW)
        result = voting.close_category(
            self.conn, category_id, now=AFTER_DEADLINE
        )
        self.assertEqual(result.winner_date_id, winner)
        self.assertEqual(result.total_votes, 3)
        self.assertEqual(result.active_winner_participants, 2)

        self.assert_error(
            "voting_closed", voting.remove_vote,
            self.conn, category_id, winner, 2, now=AFTER_DEADLINE,
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "voting_closed"):
            self.conn.execute(
                "INSERT INTO bookings(date_id, category_id, guest_token, user_id, created_at) "
                "VALUES(?,?,?,?,?)", (other, category_id, "u5", 5, AFTER_DEADLINE),
            )

        withdrawn = voting.withdraw_participation(
            self.conn, category_id, 2, now=AFTER_DEADLINE
        )
        self.assertFalse(withdrawn.already_withdrawn)
        again = voting.withdraw_participation(
            self.conn, category_id, 2, now=AFTER_DEADLINE
        )
        self.assertTrue(again.already_withdrawn)
        state = voting.get_category_state(self.conn, category_id)
        self.assertEqual(state.vote_counts[winner], 2)
        self.assertEqual(state.total_votes, 3)
        self.assertEqual(state.active_winner_participants, 1)
        self.assert_error(
            "not_winner_participant", voting.withdraw_participation,
            self.conn, category_id, 4, now=AFTER_DEADLINE,
        )

    def test_closed_ballot_does_not_block_account_fk_cleanup(self):
        category_id = self.category()
        date_id = self.date(category_id)
        self.configure(category_id)
        voting.cast_vote(self.conn, category_id, date_id, 2, now=NOW)
        voting.close_category(self.conn, category_id, now=AFTER_DEADLINE)

        # Удаление участника оставляет анонимизированный ballot, удаление
        # владельца затем каскадно удаляет весь его aggregate.
        self.conn.execute("DELETE FROM users WHERE id=2")
        ballot = self.conn.execute(
            "SELECT user_id, guest_token FROM bookings WHERE category_id=?",
            (category_id,),
        ).fetchone()
        self.assertIsNone(ballot["user_id"])
        self.assertEqual(ballot["guest_token"], "u2")
        self.conn.execute("DELETE FROM users WHERE id=?", (self.owner_id,))
        self.assertFalse(self.conn.execute(
            "SELECT 1 FROM bookings WHERE category_id=?", (category_id,)
        ).fetchone())

    def test_capacity_update_cannot_cut_existing_category(self):
        category_id = self.category()
        date_id = self.date(category_id, capacity=4)
        self.configure(category_id)
        voting.cast_vote(self.conn, category_id, date_id, 2, now=NOW)
        voting.cast_vote(self.conn, category_id, date_id, 3, now=NOW)
        self.assert_error(
            "capacity_below_existing_votes", voting.set_date_capacity,
            self.conn, date_id, self.owner_id, 1,
        )
        self.assertEqual(
            voting.set_date_capacity(
                self.conn, date_id, self.owner_id, 2
            ).capacity,
            2,
        )
        self.assert_error(
            "invalid_capacity", voting.set_date_capacity,
            self.conn, date_id, self.owner_id, 101,
        )

    def test_database_guards_configuration_and_candidate_deadlines(self):
        category_id = self.category()
        first = self.date(category_id, name="Первый")
        second = self.date(category_id, name="Второй")

        with self.assertRaisesRegex(sqlite3.IntegrityError,
                                    "invalid_voting_configuration"):
            self.conn.execute(
                "UPDATE categories SET voting_status='open' WHERE id=?",
                (category_id,),
            )

        # Existing legacy ballots cannot be made incompatible by bypassing the
        # domain service and writing category settings directly.
        for date_id in (first, second):
            self.conn.execute(
                "INSERT INTO bookings(date_id, category_id, guest_token, user_id, created_at) "
                "VALUES(?,?,?,?,?)", (date_id, category_id, "u2", 2, NOW),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError,
                                    "existing_votes_incompatible"):
            self.conn.execute(
                "UPDATE categories SET choice_mode='single' WHERE id=?",
                (category_id,),
            )

        self.configure(category_id, voting.CHOICE_MULTIPLE)
        too_early = self.date(name="Слишком рано", starts_at=DEADLINE)
        with self.assertRaisesRegex(sqlite3.IntegrityError,
                                    "candidate_before_deadline"):
            self.conn.execute(
                "INSERT INTO date_categories(date_id, category_id) VALUES(?,?)",
                (too_early, category_id),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError,
                                    "candidate_before_deadline"):
            self.conn.execute(
                "UPDATE dates SET starts_at=? WHERE id=?", (DEADLINE, first),
            )

        voting.close_category(self.conn, category_id, now=AFTER_DEADLINE)
        late_candidate = self.date(name="После закрытия")
        with self.assertRaisesRegex(sqlite3.IntegrityError,
                                    "category_composition_frozen"):
            self.conn.execute(
                "INSERT INTO date_categories(date_id, category_id) VALUES(?,?)",
                (late_candidate, category_id),
            )


class VotingMigrationTests(unittest.TestCase):
    def test_v1_database_reaches_latest_with_legacy_category_unconfigured(self):
        with tempfile.TemporaryDirectory(prefix="date4you-voting-migration-") as tmp:
            path = Path(tmp) / "app.db"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    link_token TEXT UNIQUE,
                    link_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE dates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    place TEXT, starts_at TEXT, ends_at TEXT, comment TEXT,
                    origin TEXT NOT NULL DEFAULT 'admin', guest_token TEXT,
                    is_chosen INTEGER NOT NULL DEFAULT 0, archived_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_id INTEGER NOT NULL, category_id INTEGER NOT NULL,
                    guest_token TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE (date_id, category_id, guest_token)
                );
                CREATE TABLE questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_id INTEGER NOT NULL, category_id INTEGER,
                    guest_token TEXT, text TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                INSERT INTO categories(name, link_token, created_at)
                    VALUES('Старая', 'legacy-token', '2025-01-01T00:00');
                INSERT INTO dates(name, created_at)
                    VALUES('Старое событие', '2025-01-01T00:00');
                INSERT INTO votes(date_id, category_id, guest_token, created_at)
                    VALUES(1, 1, 'legacy-guest', '2025-01-01T10:00');
                INSERT INTO questions(date_id, text, created_at)
                    VALUES(1, 'Вопрос', '2025-01-01T10:00');
            """)
            conn.close()

            old_path = db.DB_PATH
            try:
                db.DB_PATH = path
                db.init_db()
            finally:
                db.DB_PATH = old_path

            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], db.LATEST_VERSION
            )
            category = conn.execute(
                "SELECT choice_mode, voting_deadline, voting_status, closed_at, "
                "resolved_at, winner_date_id FROM categories WHERE id=1"
            ).fetchone()
            self.assertEqual(category["voting_status"], voting.STATUS_UNCONFIGURED)
            self.assertEqual(category["choice_mode"], "multiple")
            self.assertRegex(category["voting_deadline"], r"^\d{4}-\d{2}-\d{2}T")
            for column in ("closed_at", "resolved_at", "winner_date_id"):
                self.assertIsNone(category[column])
            self.assertEqual(
                conn.execute("SELECT capacity FROM dates WHERE id=1").fetchone()[0], 1
            )
            booking = conn.execute(
                "SELECT guest_token, participation_withdrawn_at FROM bookings"
            ).fetchone()
            self.assertEqual(booking["guest_token"], "legacy-guest")
            self.assertIsNone(booking["participation_withdrawn_at"])
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(bookings)")}
            self.assertIn("idx_book_vote", indexes)
            self.assertNotIn("idx_book_date", indexes)
            self.assertIn(
                "cursor_effects",
                {row[1] for row in conn.execute("PRAGMA table_info(users)")},
            )
            login_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(login_codes)")
            }
            self.assertTrue({"purpose", "user_id", "error"} <= login_columns)
            self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
