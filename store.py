"""Хранилище: история диалогов, состояние собеседников, настройки (SQLite)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id  INTEGER NOT NULL,
    role     TEXT    NOT NULL,   -- user | assistant | owner
    text     TEXT    NOT NULL,
    ts       REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer_id, id);

CREATE TABLE IF NOT EXISTS peers (
    peer_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    disclosed  INTEGER NOT NULL DEFAULT 0,
    paused     INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL,
    updated_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Отдельный журнал групп и каналов: не попадает в историю личек,
-- которую читает LLM, но сохраняет все поля сообщения.
CREATE TABLE IF NOT EXISTS chat_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    chat_id         INTEGER NOT NULL,
    chat_type       TEXT    NOT NULL,          -- Группа | Канал
    chat_title      TEXT    NOT NULL DEFAULT '',
    chat_username   TEXT    NOT NULL DEFAULT '',
    sender_id       INTEGER,
    sender_name     TEXT    NOT NULL DEFAULT '',
    sender_username TEXT    NOT NULL DEFAULT '',
    direction       TEXT    NOT NULL DEFAULT 'in',  -- in | out
    text            TEXT    NOT NULL DEFAULT '',
    media_url       TEXT    NOT NULL DEFAULT '',
    media_type      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chat_log_chat ON chat_log(chat_id, id);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------- сообщения ----------

    def add_message(self, peer_id: int, role: str, text: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO messages(peer_id, role, text, ts) VALUES (?,?,?,?)",
                (peer_id, role, text, time.time()),
            )
            self._db.commit()

    def log_chat(self, chat_id: int, chat_type: str, chat_title: str = "",
                 chat_username: str = "", sender_id: Optional[int] = None,
                 sender_name: str = "", sender_username: str = "",
                 direction: str = "in", text: str = "",
                 media_url: str = "", media_type: str = "") -> None:
        """Пишет сообщение группы/канала в отдельный журнал chat_log."""
        with self._lock:
            self._db.execute(
                """INSERT INTO chat_log(
                       ts, chat_id, chat_type, chat_title, chat_username,
                       sender_id, sender_name, sender_username, direction,
                       text, media_url, media_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(), chat_id, chat_type, chat_title, chat_username,
                    sender_id, sender_name, sender_username, direction,
                    text, media_url, media_type,
                ),
            )
            self._db.commit()

    def history(self, peer_id: int, limit: int = 20) -> List[Tuple[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT role, text FROM messages WHERE peer_id = ? ORDER BY id DESC LIMIT ?",
                (peer_id, limit),
            ).fetchall()
        return [(row["role"], row["text"]) for row in reversed(rows)]

    def clear_history(self, peer_id: int) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM messages WHERE peer_id = ?", (peer_id,))
            self._db.commit()
        return cur.rowcount

    def last_reply_ts(self, peer_id: int) -> Optional[float]:
        with self._lock:
            row = self._db.execute(
                "SELECT ts FROM messages WHERE peer_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                (peer_id,),
            ).fetchone()
        return float(row["ts"]) if row else None

    def clear_all(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM messages")
            self._db.execute("DELETE FROM peers")
            self._db.commit()

    def replies_last_hour(self, peer_id: Optional[int] = None) -> int:
        since = time.time() - 3600
        with self._lock:
            if peer_id is None:
                row = self._db.execute(
                    "SELECT COUNT(*) AS c FROM messages WHERE role='assistant' AND ts > ?", (since,)
                ).fetchone()
            else:
                row = self._db.execute(
                    "SELECT COUNT(*) AS c FROM messages WHERE role='assistant' AND peer_id=? AND ts > ?",
                    (peer_id, since),
                ).fetchone()
        return int(row["c"])

    # ---------- собеседники ----------

    def upsert_peer(self, peer_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                """INSERT INTO peers(peer_id, username, first_name, created_at, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(peer_id) DO UPDATE SET
                     username   = COALESCE(excluded.username, peers.username),
                     first_name = COALESCE(excluded.first_name, peers.first_name),
                     updated_at = excluded.updated_at""",
                (peer_id, username, first_name, now, now),
            )
            self._db.commit()

    def get_peer(self, peer_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()

    def set_disclosed(self, peer_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE peers SET disclosed = 1, updated_at = ? WHERE peer_id = ?", (time.time(), peer_id))
            self._db.commit()

    def is_disclosed(self, peer_id: int) -> bool:
        row = self.get_peer(peer_id)
        return bool(row and row["disclosed"])

    def set_paused(self, peer_id: int, paused: bool) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE peers SET paused = ?, updated_at = ? WHERE peer_id = ?",
                (1 if paused else 0, time.time(), peer_id),
            )
            self._db.commit()

    def is_paused(self, peer_id: int) -> bool:
        row = self.get_peer(peer_id)
        return bool(row and row["paused"])

    # ---------- key/value ----------

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.commit()

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def delete(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE key = ?", (key,))
            self._db.commit()

    def stats(self) -> dict:
        with self._lock:
            peers = self._db.execute("SELECT COUNT(*) AS c FROM peers").fetchone()["c"]
            msgs = self._db.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            chats = self._db.execute("SELECT COUNT(DISTINCT chat_id) AS c FROM chat_log").fetchone()["c"]
            chat_msgs = self._db.execute("SELECT COUNT(*) AS c FROM chat_log").fetchone()["c"]
            last = self._db.execute("SELECT ts FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "peers": int(peers),
            "messages": int(msgs),
            "chats": int(chats),
            "chat_messages": int(chat_msgs),
            "last_reply": last["ts"] if last else None,
        }
