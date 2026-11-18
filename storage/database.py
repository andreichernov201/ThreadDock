"""Bounded SQLite transactions owned by the application's event loop.

No awaits inside transactions. WAL permits independent health reads.
Downloader tables store operational metadata and public URLs. The separate archive
repository owns opt-in message storage in the same SQLite database.
"""

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from core.errors import QueueFull
from core.models import Destination, Job, MediaBundle, RenderOptions

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
 cache_key TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
 requester_id INTEGER NOT NULL, thread_id INTEGER, business_id TEXT NOT NULL DEFAULT '',
 mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 progress INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
 retryable INTEGER NOT NULL DEFAULT 0, requested_at REAL NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS download_preferences (
 scope TEXT NOT NULL, subject INTEGER NOT NULL, mode TEXT NOT NULL DEFAULT 'slideshow',
 photo_seconds INTEGER NOT NULL DEFAULT 3, live_limit INTEGER NOT NULL DEFAULT 0,
 senders TEXT NOT NULL DEFAULT 'all', PRIMARY KEY(scope,subject)
);
CREATE TABLE IF NOT EXISTS sender_allowlist (
 group_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY(group_id,user_id)
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
        group_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(groups)")}
        if "photo_mode" not in group_columns:
            self.connection.execute("ALTER TABLE groups ADD COLUMN photo_mode TEXT NOT NULL DEFAULT 'slideshow'")
        cache_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(cache)")}
        if "saved_at" not in cache_columns:
            self.connection.execute("ALTER TABLE cache ADD COLUMN saved_at REAL NOT NULL DEFAULT 0")
            # Old entries had no birth timestamp. Expire conservatively; early
            # eviction is allowed, extending old media retention is not.
            self.connection.execute("UPDATE cache SET saved_at=min(accessed_at,expires_at-7*86400)")
        self.connection.execute("UPDATE cache SET expires_at=min(expires_at,saved_at+72*3600)")
        self.connection.commit()
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(jobs)")}
        if "retryable" not in columns:
            self.connection.execute("ALTER TABLE jobs ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0")
            self.connection.commit()
        if "requested_at" not in columns:
            with self.connection:
                self.connection.execute("ALTER TABLE jobs ADD COLUMN requested_at REAL NOT NULL DEFAULT 0")
                self.connection.execute("UPDATE jobs SET requested_at=created_at")
        for name, definition in (
            ("active_elapsed", "REAL NOT NULL DEFAULT 0"),
            ("active_since", "REAL NOT NULL DEFAULT 0"),
            ("render_options", "TEXT NOT NULL DEFAULT '{}'"),
            ("phase", "TEXT NOT NULL DEFAULT 'prepare'"),
        ):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        self.connection.commit()
        with self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO telegram_cache(key,bundle,accessed_at)
                SELECT c.key,c.bundle,c.accessed_at FROM cache c
                WHERE EXISTS (SELECT 1 FROM telegram_files f WHERE f.cache_key=c.key)"""
            )
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
            heartbeat = self.connection.execute("SELECT value FROM state WHERE key='heartbeat'").fetchone()
            until = min(time.time(), float(heartbeat[0]) + 10) if heartbeat else time.time()
            for active in self.connection.execute("SELECT id,active_since FROM jobs WHERE active_since>0").fetchall():
                self._pause(active["id"], max(active["active_since"], until))
            self.connection.execute(
                "UPDATE jobs SET status=CASE WHEN phase='deliver' THEN 'ready' ELSE 'queued' END WHERE status='running'"
            )
            result = self.connection.execute(
                "UPDATE jobs SET status='uncertain',error_code='interrupted_delivery' WHERE status='sending'"
            )
            self.connection.execute("""UPDATE jobs SET status='cancelled',retryable=0,error_code='duplicate_delivered'
                WHERE status='queued' AND EXISTS (
                  SELECT 1 FROM jobs delivered WHERE delivered.status='done'
                  AND delivered.cache_key=jobs.cache_key AND delivered.chat_id=jobs.chat_id
                  AND delivered.business_id=jobs.business_id AND delivered.mode=jobs.mode
                  AND delivered.updated_at>=jobs.created_at)""")
            # Old callback IDs created one job per tap. Retire only those duplicate
            # actions; links sent in distinct Telegram messages remain distinct requests.
            self.connection.execute(
                """UPDATE jobs AS j SET status='cancelled',error_code='duplicate_action'
                WHERE j.status='queued' AND j.request_key LIKE 'callback:%'
                AND EXISTS (SELECT 1 FROM jobs p WHERE p.id<j.id
                  AND p.business_id=j.business_id AND p.chat_id=j.chat_id
                  AND p.message_id=j.message_id AND p.cache_key=j.cache_key AND p.mode=j.mode
                  AND p.status IN ('queued','running','ready','sending','done','uncertain'))"""
            )
        return result.rowcount

    def enqueue(
        self,
        requests: list[tuple[str, str, str]],
        destination: Destination,
        mode: str,
        capacity: int,
        render: RenderOptions = RenderOptions(),
    ) -> int:
        """Admit the entire message atomically, preserving URL order."""
        now = time.time()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            new = [
                item
                for item in requests
                if not self.connection.execute("SELECT 1 FROM jobs WHERE request_key=?", (item[0],)).fetchone()
                and not self.connection.execute(
                    "SELECT 1 FROM jobs WHERE cache_key=? AND business_id=? AND chat_id=? AND mode=? AND status IN ('queued','running','ready','sending')",
                    (item[2], destination.business_id, destination.chat_id, mode),
                ).fetchone()
            ]
            count = self.connection.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','running','ready','sending')"
            ).fetchone()[0]
            if count + len(new) > capacity:
                raise QueueFull()
            self.connection.executemany(
                "INSERT INTO jobs(request_key,url,cache_key,chat_id,message_id,requester_id,thread_id,business_id,mode,created_at,updated_at,requested_at,render_options) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        now,
                        json.dumps(asdict(render)),
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
            if row and row["status"] in {"queued", "running", "ready", "sending", "done", "uncertain"}:
                return row["status"]
            active = self.connection.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','running','ready','sending')"
            ).fetchone()[0]
            if active >= capacity:
                raise QueueFull()
            if row:
                # Keep accepted album checkpoints: retry only the remaining pieces.
                self.connection.execute(
                    """UPDATE jobs SET status='queued',attempts=0,error_code=NULL,
                    available_at=0,active_elapsed=0,active_since=0,updated_at=?,requested_at=? WHERE id=?""",
                    (now, now, row["id"]),
                )
            else:
                key = f"media:{source['business_id']}:{source['chat_id']}:{source['message_id']}:{source['cache_key']}:{mode}"
                self.connection.execute(
                    """INSERT INTO jobs(request_key,url,cache_key,chat_id,message_id,requester_id,
                    thread_id,business_id,mode,created_at,updated_at,requested_at,render_options) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key,
                        source["url"],
                        source["cache_key"],
                        source["chat_id"],
                        source["message_id"],
                        source["requester_id"],
                        source["thread_id"],
                        source["business_id"],
                        mode,
                        now,
                        now,
                        now,
                        source["render_options"],
                    ),
                )
        return "accepted"

    @staticmethod
    def _job(row) -> Job:
        return Job(
            row["id"],
            row["url"],
            row["cache_key"],
            Destination(row["chat_id"], row["message_id"], row["requester_id"], row["thread_id"], row["business_id"]),
            row["mode"],
            row["attempts"],
            row["progress"],
            RenderOptions(**json.loads(row["render_options"])),
        )

    def claim(self, *, parallel: bool = False) -> Job | None:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """SELECT j.* FROM jobs j WHERE j.status='queued' AND j.available_at<=?
                AND (? OR NOT EXISTS (SELECT 1 FROM jobs p WHERE p.chat_id=j.chat_id
                  AND p.business_id=j.business_id AND p.status IN ('queued','running','ready','sending') AND p.id<j.id))
                ORDER BY j.id LIMIT 1""",
                (time.time(), int(parallel)),
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            self.connection.execute(
                "UPDATE jobs SET status='running',phase='prepare',attempts=attempts+1,updated_at=?,active_since=CASE WHEN active_since=0 THEN ? ELSE active_since END WHERE id=?",
                (now, now, row["id"]),
            )
            return self._job(self.get_job(row["id"]))

    def claim_ready(self) -> Job | None:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """SELECT j.* FROM jobs j WHERE j.status='ready' AND NOT EXISTS (
                SELECT 1 FROM jobs p WHERE p.chat_id=j.chat_id AND p.business_id=j.business_id
                AND p.id<j.id AND p.status IN ('queued','running','ready','sending')) ORDER BY j.id LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                "UPDATE jobs SET status='running',phase='deliver',active_since=?,updated_at=? WHERE id=?",
                (time.time(), time.time(), row["id"]),
            )
            return self._job(self.get_job(row["id"]))

    def _pause(self, job_id: int, now: float) -> None:
        self.connection.execute(
            """UPDATE jobs SET active_elapsed=active_elapsed+CASE WHEN active_since>0 THEN max(0,?-active_since) ELSE 0 END,
            active_since=0 WHERE id=?""",
            (now, job_id),
        )

    def pause_request(self, job_id: int) -> None:
        with self.connection:
            self._pause(job_id, time.time())

    def status(self, job_id: int, status: str, code: str | None = None, delay: float = 0) -> None:
        with self.connection:
            if status in {"ready", "failed", "uncertain", "done", "cancelled"}:
                self._pause(job_id, time.time())
            elif status in {"running", "sending"}:
                self.connection.execute(
                    "UPDATE jobs SET active_since=? WHERE id=? AND active_since=0", (time.time(), job_id)
                )
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
                "UPDATE jobs SET progress=?,status='running',updated_at=? WHERE id=?", (piece + 1, time.time(), job_id)
            )

    def get_job(self, job_id: int):
        return self.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def request_remaining(self, job_id: int, timeout: float) -> float:
        row = self.get_job(job_id)
        if row is None:
            return 0
        elapsed = row["active_elapsed"]
        if row["active_since"]:
            elapsed += max(0, time.time() - row["active_since"])
        return max(0, timeout - elapsed)

    def offer_retry(self, job_id: int) -> None:
        with self.connection:
            self.connection.execute("UPDATE jobs SET retryable=1 WHERE id=? AND status='failed'", (job_id,))

    def complete_job(self, job_id: int) -> None:
        """A confirmed delivery supersedes queued retries of the same media in this chat."""
        with self.connection:
            row = self.get_job(job_id)
            if row is None:
                return
            self._pause(job_id, time.time())
            self.connection.execute(
                "UPDATE jobs SET status='done',retryable=0,error_code=NULL,updated_at=? WHERE id=?",
                (time.time(), job_id),
            )
            self.connection.execute(
                """UPDATE jobs SET status='cancelled',retryable=0,error_code='duplicate_delivered',updated_at=?
                WHERE id!=? AND cache_key=? AND chat_id=? AND business_id=? AND mode=?
                AND status IN ('queued','failed')""",
                (time.time(), job_id, row["cache_key"], row["chat_id"], row["business_id"], row["mode"]),
            )

    def retry_job(self, job_id: int, attempt: int, capacity: int) -> bool:
        """Consume one failure's retry button atomically; keep delivery checkpoints."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.get_job(job_id)
            if not row or row["status"] != "failed" or not row["retryable"] or row["attempts"] != attempt:
                return False
            count = self.connection.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','running','ready','sending')"
            ).fetchone()[0]
            if count >= capacity:
                raise QueueFull()
            self.connection.execute(
                "UPDATE jobs SET status='queued',retryable=0,error_code=NULL,available_at=0,active_elapsed=0,active_since=0,updated_at=?,requested_at=? WHERE id=?",
                (time.time(), time.time(), job_id),
            )
        return True

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
        except (ValueError, KeyError, TypeError):
            self.delete_cache(key)
            return None

    def put_cache(self, key: str, bundle: MediaBundle, ttl: int) -> None:
        now = time.time()
        with self.connection:
            self.connection.execute(
                """INSERT INTO cache(key,bundle,expires_at,accessed_at,saved_at) VALUES (?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET bundle=excluded.bundle,accessed_at=excluded.accessed_at,
                expires_at=CASE WHEN cache.expires_at<=excluded.saved_at THEN excluded.expires_at
                ELSE min(cache.expires_at,excluded.expires_at) END,
                saved_at=CASE WHEN cache.expires_at<=excluded.saved_at THEN excluded.saved_at ELSE cache.saved_at END""",
                (key, bundle.serialize(self.data_root), now + min(ttl, 72 * 3600), now, now),
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
        if bundle.motion_pending and mode != "audio":
            return None
        items = tuple(part for asset in bundle.items(mode) for part in asset.parts)
        if not items or not all(self.file_id(bot_id, key, asset.filename, asset.kind) for asset in items):
            return None
        with self.connection:
            self.connection.execute("UPDATE telegram_cache SET accessed_at=? WHERE key=?", (time.time(), key))
        return bundle

    def cache_candidates(self):
        return self.connection.execute("""SELECT key,bundle,expires_at FROM cache c
            WHERE NOT EXISTS(SELECT 1 FROM jobs j WHERE j.cache_key=c.key AND j.status IN ('running','ready','sending'))
            ORDER BY accessed_at""").fetchall()

    def cache_keys(self) -> set[Path]:
        paths = set()
        for row in self.connection.execute("SELECT bundle FROM cache"):
            try:
                paths.add(MediaBundle.deserialize(row[0], self.data_root).directory)
            except (ValueError, TypeError, KeyError):
                continue
        return paths

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

    def preferences(self, scope: str, subject: int) -> dict:
        if scope not in {"user", "group"}:
            raise ValueError("Invalid preference scope")
        row = self.connection.execute(
            "SELECT * FROM download_preferences WHERE scope=? AND subject=?", (scope, subject)
        ).fetchone()
        values = {"mode": "slideshow", "photo_seconds": 3, "live_limit": 0, "senders": "all"}
        if row:
            values.update({key: row[key] for key in values})
        if scope == "group":
            values["mode"] = self.group_photo_mode(subject)
        return values

    def set_preference(self, scope: str, subject: int, name: str, value) -> None:
        allowed = {
            "mode": {"photos", "slideshow"},
            "photo_seconds": {2, 3, 5, 10},
            "live_limit": {0, 5, 10},
            "senders": {"all", "self", "peer"} if scope == "user" else {"all", "admins", "selected"},
        }
        if scope not in {"user", "group"} or name not in allowed or value not in allowed[name]:
            raise ValueError("Invalid preference")
        if scope == "group" and name == "mode":
            self.set_group_photo_mode(subject, value)
            return
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO download_preferences(scope,subject) VALUES (?,?)", (scope, subject)
            )
            self.connection.execute(
                f"UPDATE download_preferences SET {name}=? WHERE scope=? AND subject=?", (value, scope, subject)
            )

    def allow_sender(self, group_id: int, user_id: int, enabled: bool) -> None:
        with self.connection:
            if enabled:
                self.connection.execute("INSERT OR IGNORE INTO sender_allowlist VALUES (?,?)", (group_id, user_id))
            else:
                self.connection.execute(
                    "DELETE FROM sender_allowlist WHERE group_id=? AND user_id=?", (group_id, user_id)
                )

    def sender_allowed(self, group_id: int, user_id: int) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM sender_allowlist WHERE group_id=? AND user_id=?", (group_id, user_id)
            ).fetchone()
            is not None
        )

    def only_sender(self, group_id: int, user_id: int) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM sender_allowlist WHERE group_id=?", (group_id,))
            self.connection.execute("INSERT INTO sender_allowlist VALUES (?,?)", (group_id, user_id))
            self.connection.execute(
                "INSERT OR IGNORE INTO download_preferences(scope,subject) VALUES ('group',?)", (group_id,)
            )
            self.connection.execute(
                "UPDATE download_preferences SET senders='selected' WHERE scope='group' AND subject=?", (group_id,)
            )

    def handles_own_links(self, user_id: int, peer_id: int) -> bool:
        return (
            self.preferences("user", user_id)["senders"] == "self"
            and self.connection.execute(
                """SELECT 1 FROM connections c JOIN business_chats b ON b.connection_id=c.id
                WHERE c.owner_id=? AND c.enabled=1 AND c.can_reply=1 AND b.chat_id=? AND b.incoming_at>?""",
                (user_id, peer_id, time.time() - 86400),
            ).fetchone()
            is not None
        )

    def group_enabled(self, chat_id: int, defaults: frozenset[int]) -> bool:
        row = self.connection.execute("SELECT enabled FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row[0]) if row else chat_id in defaults

    def set_group(self, chat_id: int, enabled: bool) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO groups(chat_id,enabled) VALUES (?,?)
                ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled""",
                (chat_id, int(enabled)),
            )

    def group_photo_mode(self, chat_id: int) -> str:
        row = self.connection.execute("SELECT photo_mode FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else "slideshow"

    def set_group_photo_mode(self, chat_id: int, mode: str) -> None:
        if mode not in {"photos", "slideshow"}:
            raise ValueError("Invalid group photo mode")
        with self.connection:
            self.connection.execute(
                """INSERT INTO groups(chat_id,enabled,photo_mode) VALUES (?,0,?)
                ON CONFLICT(chat_id) DO UPDATE SET photo_mode=excluded.photo_mode""",
                (chat_id, mode),
            )

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
                AND j.status IN ('queued','running','ready','sending'))""",
                (time.time() - 30 * 86400,),
            )
            self.connection.execute(
                """DELETE FROM telegram_files WHERE cache_key NOT IN
                (SELECT key FROM cache UNION SELECT key FROM telegram_cache)"""
            )
