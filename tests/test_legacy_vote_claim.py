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
            "VALUES(1,1,'Свидание',2,?)", (NOW,),
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
