"""Bounded SQLite transactions owned by the application's event loop.

No awaits inside transactions. WAL permits independent health reads.
Downloader tables store operational metadata and public URLs. The separate archive
repository owns opt-in message storage in the same SQLite database.
"""

import json
import sqlite3
import time
from pathlib import Path

from core.errors import QueueFull
from core.models import Destination, Job, MediaBundle

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
 cache_key TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
 requester_id INTEGER NOT NULL, thread_id INTEGER, business_id TEXT NOT NULL DEFAULT '',
 mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 progress INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, error_code TEXT
);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(status, available_at, id);
CREATE INDEX IF NOT EXISTS jobs_route ON jobs(business_id, chat_id, id, status);
CREATE TABLE IF NOT EXISTS cache (
 key TEXT PRIMARY KEY, bundle TEXT NOT NULL, expires_at REAL NOT NULL, accessed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_cache (
 key TEXT PRIMARY KEY, bundle TEXT NOT NULL, accessed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_files (
 bot_id INTEGER NOT NULL, cache_key TEXT NOT NULL, filename TEXT NOT NULL, kind TEXT NOT NULL,
 file_id TEXT NOT NULL, PRIMARY KEY(bot_id, cache_key, filename, kind)
);
CREATE TABLE IF NOT EXISTS groups (chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS connections (
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, enabled INTEGER NOT NULL, can_reply INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS business_chats (
 connection_id TEXT NOT NULL, chat_id INTEGER NOT NULL, incoming_at REAL NOT NULL,
 PRIMARY KEY(connection_id, chat_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, piece INTEGER NOT NULL,
 message_ids TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(job_id, piece)
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Database:
    def __init__(self, path: Path, data_root: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.data_root = data_root.resolve()
        self.connection = sqlite3.connect(path, timeout=1)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise RuntimeError("Database schema is newer than this application")
        self.connection.executescript(SCHEMA)
        if version == 0:
            self.connection.execute("PRAGMA user_version=1")

    def close(self) -> None:
        self.connection.close()

    def bind_bot(self, bot_id: int) -> None:
        row = self.connection.execute("SELECT value FROM state WHERE key='bot_id'").fetchone()
        if row and row[0] != str(bot_id):
            raise RuntimeError("This data directory belongs to another bot. Use a separate DATA_DIR.")
        self.set_state("bot_id", str(bot_id))

    def set_state(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))

    def recover(self) -> int:
        with self.connection:
            self.connection.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            result = self.connection.execute(
                "UPDATE jobs SET status='uncertain',error_code='interrupted_delivery' WHERE status='sending'"
            )
            # Old callback IDs created one job per tap. Retire only those duplicate
            # actions; links sent in distinct Telegram messages remain distinct requests.
            self.connection.execute(
                """UPDATE jobs AS j SET status='cancelled',error_code='duplicate_action'
                WHERE j.status='queued' AND j.request_key LIKE 'callback:%'
                AND EXISTS (SELECT 1 FROM jobs p WHERE p.id<j.id
                  AND p.business_id=j.business_id AND p.chat_id=j.chat_id
                  AND p.message_id=j.message_id AND p.cache_key=j.cache_key AND p.mode=j.mode
                  AND p.status IN ('queued','running','sending','done','uncertain'))"""
            )
        return result.rowcount

    def enqueue(self, requests: list[tuple[str, str, str]], destination: Destination, mode: str, capacity: int) -> int:
        """Admit the entire message atomically, preserving URL order."""
        now = time.time()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            new = [
                item
                for item in requests
                if not self.connection.execute("SELECT 1 FROM jobs WHERE request_key=?", (item[0],)).fetchone()
            ]
            count = self.connection.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','running','sending')"
            ).fetchone()[0]
            if count + len(new) > capacity:
                raise QueueFull()
            self.connection.executemany(
                "INSERT INTO jobs(request_key,url,cache_key,chat_id,message_id,requester_id,thread_id,business_id,mode,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        key,
                        url,
                        cache_key,
                        destination.chat_id,
                        destination.message_id,
                        destination.requester_id,
                        destination.thread_id,
                        destination.business_id,
                        mode,
                        now,
                        now,
                    )
                    for key, url, cache_key in new
                ],
            )
        return len(new)

    def request_media(self, source_id: int, mode: str, capacity: int) -> str:
        """Reuse a failed job or admit one alternative format, atomically per source."""
        now = time.time()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            source = self.get_job(source_id)
            if source is None:
                return "expired"
            row = self.connection.execute(
                """SELECT * FROM jobs WHERE business_id=? AND chat_id=?
                AND message_id=? AND cache_key=? AND mode=? AND status!='cancelled'
                ORDER BY (status='done') DESC,id DESC LIMIT 1""",
                (source["business_id"], source["chat_id"], source["message_id"], source["cache_key"], mode),
            ).fetchone()
            if row and row["status"] in {"queued", "running", "sending", "done"}:
                return row["status"]
            active = self.connection.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','running','sending')"
            ).fetchone()[0]
            if active >= capacity:
                raise QueueFull()
            if row:
                # Keep accepted album checkpoints: retry only the remaining pieces.
                self.connection.execute(
                    """UPDATE jobs SET status='queued',attempts=0,error_code=NULL,
                    available_at=0,updated_at=? WHERE id=?""",
                    (now, row["id"]),
                )
            else:
                key = f"media:{source['business_id']}:{source['chat_id']}:{source['message_id']}:{source['cache_key']}:{mode}"
                self.connection.execute(
                    """INSERT INTO jobs(request_key,url,cache_key,chat_id,message_id,requester_id,
                    thread_id,business_id,mode,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key, source["url"], source["cache_key"], source["chat_id"],
                        source["message_id"], source["requester_id"], source["thread_id"],
                        source["business_id"], mode, now, now,
                    ),
                )
        return "accepted"

    def claim(self) -> Job | None:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT j.* FROM jobs j WHERE j.status='queued' AND j.available_at<=?
                AND NOT EXISTS (SELECT 1 FROM jobs p WHERE p.chat_id=j.chat_id
                  AND p.business_id=j.business_id AND p.status IN ('queued','running','sending') AND p.id<j.id)
                ORDER BY j.id LIMIT 1
            """,
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                "UPDATE jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?", (time.time(), row["id"])
            )
        target = Destination(
            row["chat_id"], row["message_id"], row["requester_id"], row["thread_id"], row["business_id"]
        )
        return Job(row["id"], row["url"], row["cache_key"], target, row["mode"], row["attempts"] + 1, row["progress"])

    def status(self, job_id: int, status: str, code: str | None = None, delay: float = 0) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET status=?,error_code=?,available_at=?,updated_at=? WHERE id=?",
                (status, code, time.time() + delay, time.time(), job_id),
            )

    def delivered_piece(self, job_id: int, piece: int, message_ids: list[int]) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO deliveries(job_id,piece,message_ids,created_at) VALUES (?,?,?,?)",
                (job_id, piece, json.dumps(message_ids), time.time()),
            )
            self.connection.execute(
                "UPDATE jobs SET progress=?,updated_at=? WHERE id=?", (piece + 1, time.time(), job_id)
            )

    def get_job(self, job_id: int):
        return self.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def user_counts(self, requester_id: int, chat_id: int) -> dict[str, int]:
        return {
            row[0]: row[1]
            for row in self.connection.execute(
                "SELECT status,count(*) FROM jobs WHERE requester_id=? AND chat_id=? GROUP BY status",
                (requester_id, chat_id),
            )
        }

    def get_cache(self, key: str) -> MediaBundle | None:
        row = self.connection.execute(
            "SELECT bundle FROM cache WHERE key=? AND expires_at>?", (key, time.time())
        ).fetchone()
        if row is None:
            return None
        with self.connection:
            self.connection.execute("UPDATE cache SET accessed_at=? WHERE key=?", (time.time(), key))
        try:
            return MediaBundle.deserialize(row[0], self.data_root)
        except ValueError, KeyError, TypeError:
            self.delete_cache(key)
            return None

    def put_cache(self, key: str, bundle: MediaBundle, ttl: int) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                (key, bundle.serialize(self.data_root), time.time() + ttl, time.time()),
            )

    def delete_cache(self, key: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM cache WHERE key=?", (key,))
            # Telegram file IDs outlive the disk cache. Their manifest is separate;
            # evicting a large local file must not require downloading it again.

    def remember_delivery(self, key: str, bundle: MediaBundle) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO telegram_cache VALUES (?,?,?)",
                (key, bundle.serialize(self.data_root), time.time()),
            )

    def get_delivery_cache(self, bot_id: int, key: str, mode: str) -> MediaBundle | None:
        row = self.connection.execute("SELECT bundle FROM telegram_cache WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            bundle = MediaBundle.deserialize(row[0], self.data_root)
        except (ValueError, KeyError, TypeError):
            return None
        items = bundle.items(mode)
        if not items or not all(self.file_id(bot_id, key, asset.filename, asset.kind) for asset in items):
            return None
        with self.connection:
            self.connection.execute("UPDATE telegram_cache SET accessed_at=? WHERE key=?", (time.time(), key))
        return bundle

    def cache_candidates(self):
        return self.connection.execute("""SELECT key,bundle,expires_at FROM cache c
            WHERE NOT EXISTS(SELECT 1 FROM jobs j WHERE j.cache_key=c.key AND j.status IN ('running','sending'))
            ORDER BY accessed_at""").fetchall()

    def cache_keys(self) -> set[str]:
        return {row[0] for row in self.connection.execute("SELECT key FROM cache")}

    def file_id(self, bot_id: int, key: str, filename: str, kind: str) -> str | None:
        row = self.connection.execute(
            "SELECT file_id FROM telegram_files WHERE bot_id=? AND cache_key=? AND filename=? AND kind=?",
            (bot_id, key, filename, kind),
        ).fetchone()
        return row[0] if row else None

    def save_file_id(self, bot_id: int, key: str, filename: str, kind: str, file_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO telegram_files VALUES (?,?,?,?,?)", (bot_id, key, filename, kind, file_id)
            )

    def forget_file_id(self, bot_id: int, key: str, filename: str, kind: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM telegram_files WHERE bot_id=? AND cache_key=? AND filename=? AND kind=?",
                (bot_id, key, filename, kind),
            )

    def group_enabled(self, chat_id: int, defaults: frozenset[int]) -> bool:
        row = self.connection.execute("SELECT enabled FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row[0]) if row else chat_id in defaults

    def set_group(self, chat_id: int, enabled: bool) -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO groups VALUES (?,?)", (chat_id, int(enabled)))

    def save_connection(self, identifier: str, owner_id: int, enabled: bool, can_reply: bool) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO connections VALUES (?,?,?,?)", (identifier, owner_id, enabled, can_reply)
            )

    def connection_info(self, identifier: str):
        return self.connection.execute("SELECT * FROM connections WHERE id=?", (identifier,)).fetchone()

    def touch_business_chat(self, identifier: str, chat_id: int, timestamp: float) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO business_chats VALUES (?,?,?) ON CONFLICT(connection_id,chat_id) DO UPDATE SET incoming_at=max(incoming_at,excluded.incoming_at)",
                (identifier, chat_id, timestamp),
            )

    def business_can_send(self, identifier: str, chat_id: int) -> bool:
        return (
            self.connection.execute(
                """SELECT 1 FROM connections c JOIN business_chats b ON c.id=b.connection_id
            WHERE c.id=? AND b.chat_id=? AND c.enabled=1 AND c.can_reply=1 AND b.incoming_at>?""",
                (identifier, chat_id, time.time() - 86400),
            ).fetchone()
            is not None
        )

    def prune_jobs(self, retention_days: int) -> None:
        cutoff = time.time() - retention_days * 86400
        with self.connection:
            self.connection.execute(
                "DELETE FROM deliveries WHERE job_id IN (SELECT id FROM jobs WHERE updated_at<? AND status IN ('done','failed','uncertain','cancelled'))",
                (cutoff,),
            )
            self.connection.execute(
                "DELETE FROM jobs WHERE updated_at<? AND status IN ('done','failed','uncertain','cancelled')", (cutoff,)
            )
            self.connection.execute("DELETE FROM business_chats WHERE incoming_at<?", (cutoff,))
            self.connection.execute(
                """DELETE FROM telegram_cache WHERE accessed_at<? AND NOT EXISTS
                (SELECT 1 FROM jobs j WHERE j.cache_key=telegram_cache.key
                AND j.status IN ('queued','running','sending'))""",
                (time.time() - 30 * 86400,),
            )
            self.connection.execute(
                """DELETE FROM telegram_files WHERE cache_key NOT IN
                (SELECT key FROM cache UNION SELECT key FROM telegram_cache)"""
            )
