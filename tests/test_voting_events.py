#!/usr/bin/env python3
"""Точечные тесты fan-out результатов и напоминаний голосования."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
_DATA = tempfile.TemporaryDirectory(prefix="date4you-voting-events-")
os.environ["DATA_DIR"] = _DATA.name
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-voting-events")

import db  # noqa: E402
import admin_routes  # noqa: E402
import notification_outbox as outbox  # noqa: E402
import social_events  # noqa: E402
import voting  # noqa: E402
import voting_events  # noqa: E402


NOW = datetime.fromisoformat("2030-01-01T10:00:00")
DEADLINE = "2030-01-02T10:00:00"
AFTER = datetime.fromisoformat("2030-01-02T10:00:01")
START = "2030-01-10T19:00:00"


class VotingEventTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        for telegram_id, name in ((101, "Владелец"), (102, "Аня"), (103, "Борис")):
            self.conn.execute(
                "INSERT INTO users(telegram_id, display_name, created_at) VALUES(?,?,?)",
                (telegram_id, name, NOW.isoformat()),
            )

    def tearDown(self):
        self.conn.close()

    def category(self, name: str) -> int:
        return int(self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,created_at) VALUES(1,?,?,?)",
            (name, name.lower(), NOW.isoformat()),
        ).lastrowid)

    def date(self, category_ids, name: str) -> int:
        date_id = int(self.conn.execute(
            "INSERT INTO dates(owner_id,name,starts_at,capacity,created_at) "
            "VALUES(1,?,?,5,?)", (name, START, NOW.isoformat()),
        ).lastrowid)
        self.conn.execute(
            "UPDATE dates SET share_token=?,is_public=1 WHERE id=?",
            (f"date-{date_id}", date_id),
        )
        for category_id in category_ids:
            self.conn.execute(
                "INSERT INTO date_categories(date_id,category_id) VALUES(?,?)",
                (date_id, category_id),
            )
        return date_id

    def test_multiple_voter_who_chose_winner_gets_participant_result_once(self):
        category_id = self.category("Лето")
        winner = self.date([category_id], "Пикник")
        loser = self.date([category_id], "Кино")
        voting.configure_category(
            self.conn, category_id, 1, voting.CHOICE_MULTIPLE, DEADLINE, now=NOW,
        )
        voting.cast_vote(self.conn, category_id, winner, 2, now=NOW)
        voting.cast_vote(self.conn, category_id, loser, 2, now=NOW)
        voting.cast_vote(self.conn, category_id, winner, 3, now=NOW)
        state = voting.close_category(self.conn, category_id, now=AFTER)

        voting_events.queue_category_outcome(self.conn, state, now=AFTER)
        rows = self.conn.execute(
            "SELECT * FROM notification_outbox WHERE kind='voting_resolved' "
            "AND user_id=2"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("Ваш вариант победил", rows[0]["text"])
        self.assertEqual(rows[0]["action_label"], "Посмотреть результат")
        self.assertTrue(rows[0]["action_url"].endswith("/c/лето"))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE kind='winner_reminder' AND user_id=2"
            ).fetchone()[0],
            2,
        )
        reminders = self.conn.execute(
            "SELECT event_key, expires_at FROM notification_outbox "
            "WHERE kind='winner_reminder' AND user_id=2 ORDER BY send_at"
        ).fetchall()
        starts = datetime.fromisoformat(START)
        self.assertIn("reminder:24h", reminders[0]["event_key"])
        self.assertEqual(
            datetime.fromisoformat(reminders[0]["expires_at"]),
            starts - timedelta(hours=2),
        )
        self.assertIn("reminder:2h", reminders[1]["event_key"])
        self.assertEqual(datetime.fromisoformat(reminders[1]["expires_at"]), starts)

    def test_removal_cancels_reminders_only_in_its_category(self):
        first = self.category("Первая")
        second = self.category("Вторая")
        date_id = self.date([first, second], "Общее событие")
        for category_id in (first, second):
            outbox.enqueue(
                self.conn, user_id=2, kind="winner_reminder",
                event_key=(f"category:{category_id}:date:{date_id}:"
                           "reminder:24h:at:2030-01-10T19:00:00:user:2"),
                text="Напоминание", send_at="2030-01-09T19:00:00",
            )
        self.conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
            "VALUES(?,?,?,2,?)", (date_id, first, "u2", NOW.isoformat()),
        )

        voting_events.queue_date_removed(
            self.conn, date_id, "Общее событие", first, "Первая", "первая",
            now=NOW,
        )
        first_row = self.conn.execute(
            "SELECT cancelled_at FROM notification_outbox WHERE event_key LIKE ?",
            (f"category:{first}:date:{date_id}:reminder:%",),
        ).fetchone()
        second_row = self.conn.execute(
            "SELECT cancelled_at FROM notification_outbox WHERE event_key LIKE ?",
            (f"category:{second}:date:{date_id}:reminder:%",),
        ).fetchone()
        self.assertIsNotNone(first_row["cancelled_at"])
        self.assertIsNone(second_row["cancelled_at"])

    def test_winner_reminder_is_queued_exactly_at_two_hour_boundary(self):
        category_id = self.category("Граница")
        date_id = self.date([category_id], "Встреча")
        voting.configure_category(
            self.conn, category_id, 1, voting.CHOICE_SINGLE, DEADLINE, now=NOW,
        )
        voting.cast_vote(self.conn, category_id, date_id, 2, now=NOW)
        state = voting.close_category(self.conn, category_id, now=AFTER)
        boundary = datetime.fromisoformat(START) - timedelta(hours=2)

        voting_events.queue_category_outcome(self.conn, state, now=boundary)

        rows = self.conn.execute(
            "SELECT event_key,send_at FROM notification_outbox "
            "WHERE kind='winner_reminder' ORDER BY send_at"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("reminder:2h", rows[0]["event_key"])
        self.assertEqual(datetime.fromisoformat(rows[0]["send_at"]), boundary)

    def test_close_due_uses_the_new_deadline_after_an_expired_open_extension(self):
        category_id = self.category("Продлённая")
        self.date([category_id], "Встреча")
        voting.configure_category(
            self.conn, category_id, 1, voting.CHOICE_SINGLE, DEADLINE, now=NOW,
        )
        voting.configure_category(
            self.conn, category_id, 1, voting.CHOICE_SINGLE,
            "2030-01-03T10:00:00", now=AFTER,
        )

        self.assertEqual(
            voting_events.close_due_once(
                self.conn, category_id=category_id, now=AFTER,
            ),
            0,
        )
        self.assertEqual(
            voting.get_category_state(self.conn, category_id).status,
            voting.STATUS_OPEN,
        )
        new_after = datetime.fromisoformat("2030-01-03T10:00:01")
        self.assertEqual(
            voting_events.close_due_once(
                self.conn, category_id=category_id, now=new_after,
            ),
            1,
        )
        self.assertEqual(
            voting.get_category_state(self.conn, category_id).status,
            voting.STATUS_NO_WINNER,
        )

    def test_reopen_route_reconciles_old_round_and_delivers_same_result_again(self):
        category_id = self.category("Повтор")
        date_id = self.date([category_id], "Встреча")
        voting.configure_category(
            self.conn, category_id, 1, voting.CHOICE_SINGLE, DEADLINE, now=NOW,
        )
        first_vote = voting.cast_vote(self.conn, category_id, date_id, 2, now=NOW)
        voting.cast_vote(self.conn, category_id, date_id, 3, now=NOW)
        first = voting.close_category(self.conn, category_id, now=AFTER)
        voting_events.queue_category_outcome(self.conn, first, now=AFTER)
        social_events.queue_review_prompts_for_date(self.conn, date_id)
        voting.withdraw_participation(self.conn, category_id, 2, now=AFTER)
        voting_events.cancel_user_winner_reminders(self.conn, category_id, 2)
        voting_events.queue_participant_withdrawal(
            self.conn, booking_id=first_vote.booking_id, owner_id=1,
            participant_name="Аня", category_name="Повтор",
            date_name="Встреча", category_id=category_id,
        )

        # Один итог уже доставлен, остальные сообщения первого раунда всё ещё
        # ожидают отправки. Доставленное остаётся историей и не отменяется.
        self.conn.execute(
            "UPDATE notification_outbox SET sent_at=? "
            "WHERE kind='voting_resolved' AND user_id=3",
            (AFTER.isoformat(),),
        )
        owner = self.conn.execute("SELECT * FROM users WHERE id=1").fetchone()
        response = admin_routes.category_voting_configure(
            category_id,
            SimpleNamespace(state=SimpleNamespace(user=owner)),
            voting.CHOICE_SINGLE,
            "2030-01-03T10:00:00",
            self.conn,
        )

        self.assertEqual(response.status_code, 303)
        reopened = voting.get_category_state(self.conn, category_id)
        self.assertEqual(reopened.status, voting.STATUS_OPEN)
        self.assertEqual(reopened.vote_counts, {date_id: 2})
        self.assertIsNone(reopened.closed_at)
        self.assertIsNone(reopened.resolved_at)
        self.assertIsNone(reopened.winner_date_id)

        old_owner_result = self.conn.execute(
            "SELECT cancelled_at,last_error FROM notification_outbox "
            "WHERE kind='voting_resolved' AND user_id=1",
        ).fetchone()
        self.assertIsNotNone(old_owner_result["cancelled_at"])
        self.assertEqual(old_owner_result["last_error"], "voting_reopened")
        delivered_result = self.conn.execute(
            "SELECT sent_at,cancelled_at FROM notification_outbox "
            "WHERE kind='voting_resolved' AND user_id=3",
        ).fetchone()
        self.assertIsNotNone(delivered_result["sent_at"])
        self.assertIsNone(delivered_result["cancelled_at"])
        old_reminders = self.conn.execute(
            "SELECT cancelled_at,last_error FROM notification_outbox "
            "WHERE kind='winner_reminder'",
        ).fetchall()
        self.assertEqual(len(old_reminders), 4)
        self.assertTrue(all(row["cancelled_at"] for row in old_reminders))
        self.assertEqual(
            sorted(row["last_error"] for row in old_reminders),
            ["participant_withdrew", "participant_withdrew",
             "voting_reopened", "voting_reopened"],
        )
        review_prompts = self.conn.execute(
            "SELECT user_id,cancelled_at,last_error FROM notification_outbox "
            "WHERE kind='review_prompt' ORDER BY user_id",
        ).fetchall()
        self.assertEqual(len(review_prompts), 2)
        self.assertTrue(all(row["cancelled_at"] for row in review_prompts))
        self.assertTrue(all(
            row["last_error"] == "review_not_available" for row in review_prompts
        ))
        withdrawal_notice = self.conn.execute(
            "SELECT cancelled_at,last_error FROM notification_outbox "
            "WHERE kind='participant_withdrawal'",
        ).fetchone()
        self.assertIsNotNone(withdrawal_notice["cancelled_at"])
        self.assertEqual(withdrawal_notice["last_error"], "voting_reopened")
        deadline_reminders = self.conn.execute(
            "SELECT event_key,cancelled_at FROM notification_outbox "
            "WHERE kind='voting_deadline'",
        ).fetchall()
        self.assertEqual(len(deadline_reminders), 2)
        self.assertTrue(all(
            "deadline:2030-01-03T10:00:00" in row["event_key"]
            for row in deadline_reminders
        ))
        self.assertTrue(all(
            row["cancelled_at"] is None for row in deadline_reminders
        ))
        preserved_withdrawal = self.conn.execute(
            "SELECT participation_withdrawn_at FROM bookings WHERE id=?",
            (first_vote.booking_id,),
        ).fetchone()
        self.assertIsNotNone(preserved_withdrawal["participation_withdrawn_at"])

        second_close = datetime.fromisoformat("2030-01-03T10:00:01")
        second = voting.close_category(
            self.conn, category_id, now=second_close,
        )
        voting_events.queue_category_outcome(
            self.conn, second, now=second_close,
        )
        social_events.queue_review_prompts_for_date(self.conn, date_id)

        voter_results = self.conn.execute(
            "SELECT event_key,sent_at,cancelled_at FROM notification_outbox "
            "WHERE kind='voting_resolved' AND user_id=3 ORDER BY id",
        ).fetchall()
        self.assertEqual(len(voter_results), 2)
        self.assertNotEqual(voter_results[0]["event_key"], voter_results[1]["event_key"])
        self.assertIn(f"round:{first.closed_at}", voter_results[0]["event_key"])
        self.assertIn(f"round:{second.closed_at}", voter_results[1]["event_key"])
        self.assertIsNone(voter_results[1]["sent_at"])
        self.assertIsNone(voter_results[1]["cancelled_at"])
        active_winner_reminders = self.conn.execute(
            "SELECT event_key FROM notification_outbox "
            "WHERE kind='winner_reminder' AND cancelled_at IS NULL",
        ).fetchall()
        self.assertEqual(len(active_winner_reminders), 2)
        self.assertTrue(all(
            f"round:{second.closed_at}" in row["event_key"]
            for row in active_winner_reminders
        ))
        self.assertTrue(all(
            ":user:3" in row["event_key"] for row in active_winner_reminders
        ))
        review_after_second_round = {
            int(row["user_id"]): row["cancelled_at"]
            for row in self.conn.execute(
                "SELECT user_id,cancelled_at FROM notification_outbox "
                "WHERE kind='review_prompt'",
            )
        }
        self.assertIsNotNone(review_after_second_round[2])
        self.assertIsNone(review_after_second_round[3])
        withdrawn_result = self.conn.execute(
            "SELECT text FROM notification_outbox "
            "WHERE kind='voting_resolved' AND user_id=2 "
            "AND event_key LIKE ?",
            (f"%round:{second.closed_at}:user:2",),
        ).fetchone()
        self.assertIn("ранее отказались", withdrawn_result["text"])

    def test_participant_withdrawal_notice_is_deduplicated_by_booking(self):
        category_id = self.category("Поход")
        date_id = self.date([category_id], "Тропа")
        voting.configure_category(
            self.conn, category_id, 1, voting.CHOICE_MULTIPLE, DEADLINE, now=NOW,
        )
        vote = voting.cast_vote(self.conn, category_id, date_id, 2, now=NOW)
        state = voting.close_category(self.conn, category_id, now=AFTER)
        self.assertEqual(state.winner_date_id, date_id)
        withdrawal = voting.withdraw_participation(
            self.conn, category_id, 2, now=AFTER,
        )
        for _ in range(2):
            voting_events.queue_participant_withdrawal(
                self.conn, booking_id=vote.booking_id, owner_id=1,
                participant_name="Аня", category_name="Поход",
                date_name="Тропа", category_id=category_id,
            )
        rows = self.conn.execute(
            "SELECT user_id, text FROM notification_outbox "
            "WHERE kind='participant_withdrawal'"
        ).fetchall()
        self.assertFalse(withdrawal.already_withdrawn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], 1)
        self.assertIn("Итог голосования не изменился", rows[0]["text"])

    def test_category_removal_cancels_every_pending_message_with_its_link(self):
        category_id = self.category("Удаляемая")
        other_id = self.category("Другая")
        token = self.conn.execute(
            "SELECT link_token FROM categories WHERE id=?", (category_id,),
        ).fetchone()[0]
        public_url = f"{voting_events.BASE_URL}/c/{token}"
        fixtures = (
            (f"category:{category_id}:deadline:x:user:2", "Срок"),
            (f"date:9:changed:x:category:{category_id}:user:2", "Изменено"),
            ("booking:77:removed:user:2", f"Старое сообщение\n{public_url}"),
            (f"category:{other_id}:deadline:x:user:2", "Не относится"),
        )
        for key, text in fixtures:
            outbox.enqueue(
                self.conn, user_id=2, kind="fixture", event_key=key, text=text,
            )

        cancelled = voting_events.cancel_category_notifications(
            self.conn, category_id,
        )

        rows = {
            row["event_key"]: row["cancelled_at"]
            for row in self.conn.execute(
                "SELECT event_key,cancelled_at FROM notification_outbox"
            )
        }
        self.assertEqual(cancelled, 3)
        for key, _text in fixtures[:3]:
            self.assertIsNotNone(rows[key])
        self.assertIsNone(rows[fixtures[3][0]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
