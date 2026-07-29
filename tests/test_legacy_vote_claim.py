#!/usr/bin/env python3
"""Security regressions for binding pre-account cookie ballots."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
_DATA = tempfile.TemporaryDirectory(prefix="date4you-legacy-claim-")
os.environ["DATA_DIR"] = _DATA.name
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-legacy-claim")

import db  # noqa: E402
import public_routes  # noqa: E402


NOW = "2030-01-01T10:00:00"


def request_with_cookie(token: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "scheme": "https",
        "path": "/c/legacy", "raw_path": b"/c/legacy", "query_string": b"",
        "headers": [(b"cookie", f"bg={token}".encode())],
        "client": ("127.0.0.1", 1), "server": ("test", 443),
    })


class LegacyVoteClaimTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(db.SCHEMA)
        for telegram_id, name in ((101, "Владелец"), (102, "Гость")):
            self.conn.execute(
                "INSERT INTO users(telegram_id,display_name,created_at) VALUES(?,?,?)",
                (telegram_id, name, NOW),
            )
        self.conn.execute(
            "INSERT INTO categories(id,owner_id,name,link_token,created_at) "
            "VALUES(1,1,'Legacy','legacy',?)", (NOW,),
        )
        self.conn.execute(
            "INSERT INTO dates(id,owner_id,name,capacity,created_at) "
            "VALUES(1,1,'Событие',2,?)", (NOW,),
        )
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(1,1)"
        )

    def tearDown(self):
        self.conn.close()

    def ballot(self, token: str) -> None:
        self.conn.execute(
            "INSERT INTO guests(token,name,created_at) VALUES(?,?,?)",
            (token, "Старый гость", NOW),
        )
        self.conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
            "VALUES(1,1,?,NULL,?)", (token, NOW),
        )

    def stable_ballot(self, date_id: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO guests(token,name,created_at) VALUES('u2','Гость',?) "
            "ON CONFLICT(token) DO NOTHING",
            (NOW,),
        )
        self.conn.execute(
            "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
            "VALUES(?,1,'u2',2,?)",
            (date_id, NOW),
        )

    def test_random_legacy_bearer_is_claimed_by_current_account(self):
        self.ballot("secret-random-legacy-token")
        user = self.conn.execute("SELECT * FROM users WHERE id=2").fetchone()
        changed = public_routes.claim_legacy_votes(
            self.conn, request_with_cookie("secret-random-legacy-token"), user, 1,
        )
        row = self.conn.execute(
            "SELECT guest_token,user_id FROM bookings"
        ).fetchone()
        self.assertEqual(changed, 1)
        self.assertEqual(tuple(row), ("u2", 2))

    def test_read_only_view_can_recognise_legacy_ballot(self):
        token = "secret-random-legacy-token"
        self.ballot(token)
        user = self.conn.execute("SELECT * FROM users WHERE id=2").fetchone()
        request = request_with_cookie(token)
        before = self.conn.total_changes

        self.assertEqual(public_routes.legacy_guest_token(request, user), token)
        self.assertEqual(
            public_routes.legacy_vote_date_ids(self.conn, request, user, 1),
            (1,),
        )
        self.assertEqual(self.conn.total_changes, before)

    def test_bounded_roster_keeps_current_participant_visible(self):
        people = [
            {"name": f"Участник {index}", "is_me": index == 12}
            for index in range(13)
        ]
        visible, hidden = public_routes._visible_participants(people)
        self.assertEqual(len(visible), public_routes.PUBLIC_ROSTER_LIMIT)
        self.assertEqual(hidden, 1)
        self.assertTrue(visible[-1]["is_me"])

    def test_single_read_view_hides_legacy_when_account_vote_exists(self):
        self.conn.execute(
            "INSERT INTO dates(id,owner_id,name,capacity,created_at) "
            "VALUES(2,1,'Второе событие',2,?)",
            (NOW,),
        )
        self.conn.execute(
            "INSERT INTO date_categories(date_id,category_id) VALUES(2,1)"
        )
        self.ballot("secret-random-legacy-token")
        self.stable_ballot(2)
        self.conn.execute(
            "UPDATE categories SET choice_mode='single' WHERE id=1"
        )
        user = self.conn.execute("SELECT * FROM users WHERE id=2").fetchone()
        cat = self.conn.execute("SELECT * FROM categories WHERE id=1").fetchone()
        rows = public_routes.view_booking_rows(
            public_routes._booking_rows(self.conn, 1),
            request_with_cookie("secret-random-legacy-token"),
            user,
            cat,
        )
        self.assertEqual([(row["date_id"], row["is_me"]) for row in rows],
                         [(2, True)])

    def test_multiple_read_view_deduplicates_same_legacy_choice(self):
        self.ballot("secret-random-legacy-token")
        self.stable_ballot()
        self.conn.execute(
            "UPDATE categories SET choice_mode='multiple' WHERE id=1"
        )
        user = self.conn.execute("SELECT * FROM users WHERE id=2").fetchone()
        cat = self.conn.execute("SELECT * FROM categories WHERE id=1").fetchone()
        rows = public_routes.view_booking_rows(
            public_routes._booking_rows(self.conn, 1),
            request_with_cookie("secret-random-legacy-token"),
            user,
            cat,
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_me"])

    def test_closed_legacy_ballot_does_not_expose_account_actions(self):
        self.ballot("secret-random-legacy-token")
        self.conn.execute(
            "UPDATE categories SET voting_status='no_winner', closed_at=? "
            "WHERE id=1",
            (NOW,),
        )
        user = self.conn.execute("SELECT * FROM users WHERE id=2").fetchone()
        cat = self.conn.execute("SELECT * FROM categories WHERE id=1").fetchone()
        rows = public_routes.view_booking_rows(
            public_routes._booking_rows(self.conn, 1),
            request_with_cookie("secret-random-legacy-token"),
            user,
            cat,
        )
        self.assertFalse(rows[0]["is_me"])

    def test_predictable_anonymised_account_token_is_never_claimed(self):
        self.ballot("u777")
        user = self.conn.execute("SELECT * FROM users WHERE id=2").fetchone()
        changed = public_routes.claim_legacy_votes(
            self.conn, request_with_cookie("u777"), user, 1,
        )
        row = self.conn.execute(
            "SELECT guest_token,user_id FROM bookings"
        ).fetchone()
        self.assertEqual(changed, 0)
        self.assertEqual(tuple(row), ("u777", None))

    def test_owner_view_does_not_claim_anonymous_ballot(self):
        self.ballot("secret-owner-browser-token")
        owner = self.conn.execute("SELECT * FROM users WHERE id=1").fetchone()
        changed = public_routes.claim_legacy_votes(
            self.conn, request_with_cookie("secret-owner-browser-token"), owner, 1,
        )
        row = self.conn.execute(
            "SELECT guest_token,user_id FROM bookings"
        ).fetchone()
        self.assertEqual(changed, 0)
        self.assertEqual(tuple(row), ("secret-owner-browser-token", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
