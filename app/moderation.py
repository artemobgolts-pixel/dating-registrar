"""Policy for holding newly submitted content for an operator review."""

from __future__ import annotations


def requires_operator_review(conn, actor_id: int) -> bool:
    """Read an actor's current risk flag inside the creation transaction.

    The no-op write obtains SQLite's writer lock before the policy read.  This
    serializes content creation with an operator changing the flag and avoids
    trusting the potentially stale user row stored on the request.
    """

    if conn.execute(
        "UPDATE users SET id=id WHERE id=?", (int(actor_id),),
    ).rowcount == 0:
        return False
    actor = conn.execute(
        "SELECT is_suspicious, is_operator FROM users WHERE id=?",
        (int(actor_id),),
    ).fetchone()
    return bool(actor and actor["is_suspicious"] and not actor["is_operator"])
