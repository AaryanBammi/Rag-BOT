"""Chat thread store (architecture.md §5): SQLite in WAL mode.

Threads belong to an anonymous client id; every read and write is scoped by it, so one client
can't see or touch another's threads. A user message and its answer are saved in one
transaction, and a message that contained PII is stored only as a placeholder.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.validator import Reply

PII_PLACEHOLDER = "[message removed: contained personal information]"
NEW_CHAT = "New chat"
TITLE_LENGTH = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id  TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS threads_by_client ON threads (client_id, updated_at);
CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL REFERENCES threads (thread_id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    kind        TEXT,
    source_url  TEXT,
    source_date TEXT,
    created_at  TEXT NOT NULL,
    seq         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_thread ON messages (thread_id, seq);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Thread:
    thread_id: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Message:
    message_id: str
    role: str
    content: str
    kind: str | None
    source_url: str | None
    source_date: str | None
    created_at: str


class ThreadStore:
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.Lock()  # one connection, shared across worker threads
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # --- threads --------------------------------------------------------------------------

    def create_thread(self, client_id: str) -> Thread:
        now = _now()
        thread = Thread(str(uuid.uuid4()), NEW_CHAT, now, now)
        with self._lock:
            self._db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                             (thread.thread_id, client_id, thread.title, now, now))
        return thread

    def list_threads(self, client_id: str) -> list[Thread]:
        with self._lock:
            rows = self._db.execute(
                "SELECT thread_id, title, created_at, updated_at FROM threads WHERE client_id = ? "
                "ORDER BY updated_at DESC", (client_id,)).fetchall()
        return [Thread(**dict(r)) for r in rows]

    def get_thread(self, client_id: str, thread_id: str) -> Thread | None:
        with self._lock:
            row = self._db.execute(
                "SELECT thread_id, title, created_at, updated_at FROM threads WHERE client_id = ? AND thread_id = ?",
                (client_id, thread_id)).fetchone()
        return Thread(**dict(row)) if row else None

    def delete_thread(self, client_id: str, thread_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM threads WHERE client_id = ? AND thread_id = ?", (client_id, thread_id))
        return cur.rowcount > 0

    # --- messages -------------------------------------------------------------------------

    def messages(self, client_id: str, thread_id: str) -> list[Message] | None:
        """The thread's messages in order, or None if the client has no such thread."""
        if not self.get_thread(client_id, thread_id):
            return None
        with self._lock:
            rows = self._db.execute(
                "SELECT message_id, role, content, kind, source_url, source_date, created_at FROM messages "
                "WHERE thread_id = ? ORDER BY seq", (thread_id,)).fetchall()
        return [Message(**dict(r)) for r in rows]

    def history(self, client_id: str, thread_id: str, turns: int = 6) -> list[tuple[str, str]]:
        """The last `turns` messages as (role, text) for the classifier. Assistant replies are
        trimmed to the answer itself (no link or footer)."""
        msgs = self.messages(client_id, thread_id) or []
        return [(m.role, m.content.split("\n\nSource:")[0]) for m in msgs[-turns:]]

    def add_exchange(self, client_id: str, thread_id: str, user_text: str, reply: Reply) -> tuple[Message, Message] | None:
        """Save the user message and the reply in one transaction. PII messages are stored as the
        placeholder. Returns None (and saves nothing) if the thread was deleted meanwhile."""
        pii = reply.kind == "pii"
        stored_user = PII_PLACEHOLDER if pii else user_text
        now = _now()
        user = Message(str(uuid.uuid4()), "user", stored_user, None, None, None, now)
        bot = Message(str(uuid.uuid4()), "assistant", reply.text, reply.kind, reply.url, reply.footer_date, now)
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT title, (SELECT COUNT(*) FROM messages WHERE thread_id = ?) AS n "
                                       "FROM threads WHERE client_id = ? AND thread_id = ?",
                                       (thread_id, client_id, thread_id)).fetchone()
                if row is None:
                    self._db.execute("ROLLBACK")
                    return None
                n = row["n"]
                for i, m in enumerate((user, bot)):
                    self._db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                     (m.message_id, thread_id, m.role, m.content, m.kind, m.source_url,
                                      m.source_date, m.created_at, n + i))
                title = row["title"]
                if title == NEW_CHAT and not pii:
                    title = " ".join(user_text.split())
                    title = title if len(title) <= TITLE_LENGTH else title[: TITLE_LENGTH - 1].rstrip() + "…"
                self._db.execute("UPDATE threads SET title = ?, updated_at = ? WHERE thread_id = ?",
                                 (title, now, thread_id))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return user, bot

    def count(self) -> tuple[int, int]:
        """(threads, messages) totals, for tests and health checks."""
        with self._lock:
            t = self._db.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            m = self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return t, m
