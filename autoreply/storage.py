"""Persistent bot state (away mode, per-chat flags, rate limits) in SQLite.

Chat history itself is read from Telegram, so it always includes messages
the owner wrote by hand and survives restarts without duplication.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id            INTEGER PRIMARY KEY,
    disabled           INTEGER NOT NULL DEFAULT 0,
    owner_active_until REAL    NOT NULL DEFAULT 0,
    last_notice_at     REAL    NOT NULL DEFAULT 0,
    reset_after_msg_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS replies (
    chat_id INTEGER NOT NULL,
    ts      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_replies_chat_ts ON replies (chat_id, ts);
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    chat_id   INTEGER NOT NULL,
    name      TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    text      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS commands (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    kind    TEXT    NOT NULL,
    payload TEXT    NOT NULL,
    status  TEXT    NOT NULL DEFAULT 'pending',
    result  TEXT
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    token     TEXT PRIMARY KEY,
    csrf      TEXT NOT NULL,
    ip        TEXT NOT NULL,
    created   REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_fails (
    ip TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_locks (
    ip    TEXT PRIMARY KEY,
    until REAL NOT NULL
);
"""


class Storage:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.on_change = None  # called after settings/chat-state writes
        # Shared by the event loop and worker threads (backup, web server);
        # sqlite serialises access itself, so allow use from any thread.
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        # The web app (WSGI) and the bot worker may share this file.
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    # --- global settings -------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str | None, notify: bool = True) -> None:
        if value is None:
            self._db.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        if notify:
            self._changed()

    def get_bool(self, key: str, default: bool) -> bool:
        value = self.get(key)
        return default if value is None else value == "1"

    def set_bool(self, key: str, value: bool) -> None:
        self.set(key, "1" if value else "0")

    # --- per-chat state --------------------------------------------------

    def _ensure_chat(self, chat_id: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,)
        )

    def _chat_value(self, chat_id: int, column: str, default):
        row = self._db.execute(
            f"SELECT {column} FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row[0] if row else default

    def _set_chat_value(self, chat_id: int, column: str, value) -> None:
        self._ensure_chat(chat_id)
        self._db.execute(
            f"UPDATE chats SET {column} = ? WHERE chat_id = ?", (value, chat_id)
        )
        self._changed()

    def is_chat_disabled(self, chat_id: int) -> bool:
        return bool(self._chat_value(chat_id, "disabled", 0))

    def set_chat_disabled(self, chat_id: int, disabled: bool) -> None:
        self._set_chat_value(chat_id, "disabled", int(disabled))

    def disabled_chats(self) -> list[int]:
        rows = self._db.execute("SELECT chat_id FROM chats WHERE disabled = 1")
        return [row[0] for row in rows]

    def owner_active_until(self, chat_id: int) -> float:
        return float(self._chat_value(chat_id, "owner_active_until", 0))

    def set_owner_active_until(self, chat_id: int, until: float) -> None:
        self._set_chat_value(chat_id, "owner_active_until", until)

    def last_notice_at(self, chat_id: int) -> float:
        return float(self._chat_value(chat_id, "last_notice_at", 0))

    def set_last_notice_at(self, chat_id: int, ts: float) -> None:
        self._set_chat_value(chat_id, "last_notice_at", ts)

    def reset_after_msg_id(self, chat_id: int) -> int:
        return int(self._chat_value(chat_id, "reset_after_msg_id", 0))

    def set_reset_after_msg_id(self, chat_id: int, msg_id: int) -> None:
        self._set_chat_value(chat_id, "reset_after_msg_id", msg_id)

    # --- rate limiting ---------------------------------------------------

    def record_reply(self, chat_id: int, ts: float | None = None) -> None:
        self._db.execute(
            "INSERT INTO replies (chat_id, ts) VALUES (?, ?)",
            (chat_id, ts if ts is not None else time.time()),
        )

    def replies_since(self, chat_id: int, since: float) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM replies WHERE chat_id = ? AND ts >= ?",
            (chat_id, since),
        ).fetchone()
        return int(row[0])

    def prune_replies(self, older_than: float) -> None:
        self._db.execute("DELETE FROM replies WHERE ts < ?", (older_than,))

    # --- panel-editable configuration -----------------------------------

    def get_config_all(self) -> dict:
        rows = self._db.execute("SELECT key, value FROM config")
        return {key: json.loads(value) for key, value in rows}

    def set_config(self, key: str, value) -> None:
        self._db.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self._changed()

    # --- activity log for owner reports ----------------------------------

    def add_event(self, chat_id: int, name: str, kind: str, text: str,
                  ts: float | None = None) -> None:
        self._db.execute(
            "INSERT INTO events (ts, chat_id, name, kind, text) VALUES (?, ?, ?, ?, ?)",
            (ts if ts is not None else time.time(), chat_id, name, kind, text[:1000]),
        )

    def events_since(self, since: float) -> list[dict]:
        rows = self._db.execute(
            "SELECT ts, chat_id, name, kind, text FROM events WHERE ts >= ? ORDER BY ts",
            (since,),
        )
        return [dict(zip(("ts", "chat_id", "name", "kind", "text"), r)) for r in rows]

    def count_events_since(self, since: float, kind: str | None = None) -> int:
        if kind:
            row = self._db.execute(
                "SELECT COUNT(*) FROM events WHERE ts >= ? AND kind = ?", (since, kind)
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT COUNT(*) FROM events WHERE ts >= ?", (since,)
            ).fetchone()
        return int(row[0])

    def prune_events(self, older_than: float) -> None:
        self._db.execute("DELETE FROM events WHERE ts < ?", (older_than,))

    def backup_to(self, target: Path) -> None:
        """Consistent copy of the database (safe while it is in use)."""
        dest = sqlite3.connect(str(target))
        try:
            self._db.backup(dest)
        finally:
            dest.close()

    # --- web <-> worker command queue (Flask/WSGI deployments) ----------

    def enqueue_command(self, kind: str, payload: dict | None = None) -> int:
        cur = self._db.execute(
            "INSERT INTO commands (ts, kind, payload) VALUES (?, ?, ?)",
            (time.time(), kind, json.dumps(payload or {}, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    def claim_command(self, max_age: float = 60) -> dict | None:
        """Oldest pending command, marked as running (stale ones expire)."""
        self._db.execute(
            "UPDATE commands SET status = 'expired' WHERE status = 'pending' AND ts < ?",
            (time.time() - max_age,),
        )
        row = self._db.execute(
            "SELECT id, kind, payload FROM commands WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        claimed = self._db.execute(
            "UPDATE commands SET status = 'running' WHERE id = ? AND status = 'pending'", (row[0],)
        ).rowcount
        if not claimed:
            return None
        return {"id": row[0], "kind": row[1], "payload": json.loads(row[2])}

    def finish_command(self, command_id: int, result: dict) -> None:
        self._db.execute(
            "UPDATE commands SET status = 'done', result = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False, default=str), command_id),
        )

    def command_result(self, command_id: int) -> dict | None:
        row = self._db.execute(
            "SELECT status, result FROM commands WHERE id = ?", (command_id,)
        ).fetchone()
        if row is None or row[0] != "done":
            return None
        return json.loads(row[1])

    def abandon_command(self, command_id: int) -> None:
        self._db.execute(
            "UPDATE commands SET status = 'expired' WHERE id = ? AND status = 'pending'",
            (command_id,),
        )

    def prune_commands(self, older_than: float) -> None:
        self._db.execute("DELETE FROM commands WHERE ts < ?", (older_than,))

    # --- admin panel sessions and login lockout -------------------------

    def save_admin_session(self, token: str, csrf: str, ip: str, now: float) -> None:
        self._db.execute(
            "INSERT INTO admin_sessions (token, csrf, ip, created, last_seen) VALUES (?, ?, ?, ?, ?)",
            (token, csrf, ip, now, now),
        )

    def admin_session(self, token: str) -> tuple | None:
        return self._db.execute(
            "SELECT token, csrf, ip, created, last_seen FROM admin_sessions WHERE token = ?",
            (token,),
        ).fetchone()

    def touch_admin_session(self, token: str, now: float) -> None:
        self._db.execute("UPDATE admin_sessions SET last_seen = ? WHERE token = ?", (now, token))

    def delete_admin_session(self, token: str | None = None) -> None:
        if token is None:
            self._db.execute("DELETE FROM admin_sessions")
        else:
            self._db.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))

    def add_auth_fail(self, ip: str, now: float) -> None:
        self._db.execute("INSERT INTO auth_fails (ip, ts) VALUES (?, ?)", (ip, now))

    def count_auth_fails(self, ip: str, since: float) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM auth_fails WHERE ip = ? AND ts >= ?", (ip, since)
        ).fetchone()
        return int(row[0])

    def clear_auth_fails(self, ip: str) -> None:
        self._db.execute("DELETE FROM auth_fails WHERE ip = ?", (ip,))

    def lock_ip(self, ip: str, until: float) -> None:
        self._db.execute(
            "INSERT INTO auth_locks (ip, until) VALUES (?, ?) "
            "ON CONFLICT(ip) DO UPDATE SET until = excluded.until",
            (ip, until),
        )

    def locked_until(self, ip: str) -> float:
        row = self._db.execute("SELECT until FROM auth_locks WHERE ip = ?", (ip,)).fetchone()
        return float(row[0]) if row else 0.0
