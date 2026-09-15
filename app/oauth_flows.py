"""Одноразовые OAuth flows; cookie доказывает браузер, registry — аккаунт/сессию."""
import hashlib
import re
import secrets
import time

import sessions

TTL_SECONDS = 600
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_COOKIE_KEY = "oauth_flows"


def _digest(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return None
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def context_matches(conn, session, flow):
    """Повторяется под write-lock после обмена code, перед изменением аккаунтов."""
    uid = session.get("user_id")
    session_hash = sessions._id_hash(session.get("session_id"))
    if uid != flow["user_id"] or session_hash != flow["session_id_hash"]:
        return False
    if uid is None:
        return flow["mode"] == "login" and session_hash is None
    return conn.execute(
        "SELECT 1 FROM auth_sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.id_hash=? AND s.user_id=? AND s.expires_at>? AND u.is_active=1",
        (session_hash, uid, int(time.time())),
    ).fetchone() is not None


def create(conn, session, provider, mode, next_url=None):
    state, proof = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = int(time.time())
    flow = dict(provider=provider, mode=mode, user_id=session.get("user_id"),
                session_id_hash=sessions._id_hash(session.get("session_id")))
    if not context_matches(conn, session, flow):
        return None
    conn.execute("DELETE FROM oauth_flows WHERE expires_at<=?", (now,))
    conn.execute(
        "INSERT INTO oauth_flows(state_hash,browser_hash,provider,mode,user_id,"
        "session_id_hash,created_at,expires_at,next_url) VALUES(?,?,?,?,?,?,?,?,?)",
        (_digest(state), _digest(proof), provider, mode, flow["user_id"],
         flow["session_id_hash"], now, now + TTL_SECONDS, next_url),
    )
    conn.commit()
    flows = session.get(_COOKIE_KEY, {})
    flows[_digest(state)] = {"proof": proof, "mode": mode}
    session[_COOKIE_KEY] = dict(list(flows.items())[-10:])
    return state


def consume(conn, session, state, provider):
    """Атомарное удаление исключает replay, включая восстановленную signed cookie.

    Любой terminal callback с browser proof расходует flow, в том числе ошибка,
    отмена или неверный контекст. Без browser proof чужой flow не расходуется.
    """
    state_hash = _digest(state)
    flows = session.get(_COOKIE_KEY, {})
    proof = flows.pop(state_hash, None)
    session[_COOKIE_KEY] = flows
    if not state_hash or not isinstance(proof, dict):
        return None
    flow = conn.execute(
        "DELETE FROM oauth_flows WHERE state_hash=? AND browser_hash=? RETURNING *",
        (state_hash, _digest(proof.get("proof"))),
    ).fetchone()
    conn.commit()
    if (flow is None or flow["provider"] != provider or flow["mode"] != proof.get("mode")
            or not flow["created_at"] <= int(time.time()) < flow["expires_at"]
            or not context_matches(conn, session, flow)):
        return None
    return flow
