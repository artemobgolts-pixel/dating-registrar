"""Локальный HTTP backend с временной SQLite, без фоновых внешних интеграций."""

import base64
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

from itsdangerous import TimestampSigner

APP = Path(__file__).resolve().parents[1] / "app"


class LiveBackend:
    def __init__(self):
        self.data = tempfile.TemporaryDirectory(prefix="date4you-phase-a-browser-")
        self.secret = "phase-a-browser-synthetic-secret"
        os.environ.update({
            "DATA_DIR": self.data.name, "SECRET_KEY": self.secret,
            "COOKIE_SECURE": "false", "DOMAIN": "127.0.0.1", "LOG_LEVEL": "ERROR",
            "TG_BOT_TOKEN": "", "TG_CHAT_ID": "", "TG_BACKUP_CHAT_ID": "",
            "TG_BOT_USERNAME": "", "OPERATOR_TG_IDS": "", "SENTRY_DSN": "",
        })
        sys.path.insert(0, str(APP))
        os.chdir(APP)
        import db
        import main
        import sessions
        import uvicorn
        self.db, self.main, self.sessions = db, main, sessions
        db.init_db()
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", log_level="error"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.close()
                raise RuntimeError("Локальный backend не запустился")
            time.sleep(0.02)

    def user_cookie(self, *, skin="friends"):
        self.main._rates.clear()
        conn = self.db.connect()
        try:
            conn.execute("DELETE FROM users")
            uid = conn.execute(
                "INSERT INTO users(telegram_id,display_name,admin_skin,created_at) VALUES(90001,'Исходное имя',?,?)",
                (skin, self.main.now_iso()),
            ).lastrowid
            request = SimpleNamespace(session={})
            self.sessions.issue_session(request, conn, uid)
            payload = base64.b64encode(json.dumps(request.session).encode())
            cookie = TimestampSigner(self.secret).sign(payload).decode()
            return uid, {"name": "admin_s", "value": cookie, "url": self.url}
        finally:
            conn.close()

    def row(self, query, parameters=()):
        conn = self.db.connect()
        try:
            row = conn.execute(query, parameters).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def close(self):
        if hasattr(self, "server"):
            self.server.should_exit = True
            self.thread.join(timeout=10)
        if hasattr(self, "socket"):
            self.socket.close()
        self.data.cleanup()
