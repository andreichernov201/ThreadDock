"""Archive persistence. Every read and mutation is explicitly scoped to owner_id."""

import hashlib
import json
import re
import time
import uuid

from archive.auth import new_token, token_hash
from archive.content import keyword_matches, word_diff
from archive.schema import migrate
from core import texts as T

JSON_SETTINGS = {"keywords", "exclusions", "media_types"}
BOOLEAN_SETTINGS = {
    "enabled",
    "paused",
    "notify_enabled",
    "notify_edits",
    "notify_deletes",
    "notify_keywords",
    "notify_profiles",
    "hide_content",
    "whole_words",
    "auto_delete_disconnected",
}
MEDIA_TYPES = {"photo", "video", "document", "audio", "voice", "video_note", "animation", "sticker"}


def _dict(row):
    return dict(row) if row is not None else None


class ArchiveRepository:
    def __init__(self, database):
        self.database = database
        self.db = database.connection
        migrate(self.db)

    def ensure_owner(self, owner_id: int, language: str = "ru") -> dict:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO archive_owners(owner_id,language,created_at) VALUES (?,?,?)",
                (owner_id, "ru" if language.startswith("ru") else "en", time.time()),
            )
        return self.owner(owner_id)

    def owner(self, owner_id: int) -> dict | None:
        row = _dict(self.db.execute("SELECT * FROM archive_owners WHERE owner_id=?", (owner_id,)).fetchone())
        if row:
            for field in JSON_SETTINGS:
                row[field] = json.loads(row[field])
        return row

    def public_owner(self, owner_id: int) -> dict:
        row = self.owner(owner_id)
        if row is None:
            raise LookupError("Owner not found")
        row["has_pin"] = bool(row.pop("pin_hash"))
        row.pop("pin_salt")
        return row

    def update_owner(self, owner_id: int, changes: dict) -> dict:
        allowed = (
            BOOLEAN_SETTINGS
            | JSON_SETTINGS
            | {"language", "retention_days", "quiet_start", "quiet_end", "utc_offset", "auto_lock", "read_mode"}
        )
        if set(changes) - allowed:
            raise ValueError(T.ARCHIVE_REPOSITORY_UNKNOWN_SETTING)
        encoded = {}
        for key, value in changes.items():
            if key in BOOLEAN_SETTINGS:
                if type(value) is not bool:
                    raise ValueError(T.ARCHIVE_REPOSITORY_BOOLEAN_SETTING_REQUIRED)
                encoded[key] = int(value)
            elif key in JSON_SETTINGS:
                if (
                    not isinstance(value, list)
                    or len(value) > 50
                    or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 80 for item in value)
                ):
                    raise ValueError(T.ARCHIVE_REPOSITORY_INVALID_LIST)
                value = list(dict.fromkeys(item.strip() for item in value))
                if key == "media_types" and not set(value) <= MEDIA_TYPES:
                    raise ValueError(T.ARCHIVE_REPOSITORY_UNKNOWN_MEDIA_TYPE)
                encoded[key] = json.dumps(value, ensure_ascii=False)
            elif key == "language":
                if value not in {"ru", "en"}:
                    raise ValueError(T.ARCHIVE_REPOSITORY_UNKNOWN_LANGUAGE)
                encoded[key] = value
            elif key == "retention_days":
                if value not in {0, 30, 90, 365} or type(value) is not int:
                    raise ValueError(T.ARCHIVE_REPOSITORY_INVALID_RETENTION)
                encoded[key] = value
            elif key == "read_mode":
                if value not in {"none", "all", "selected"}:
                    raise ValueError(T.ARCHIVE_REPOSITORY_UNKNOWN_READ_MODE)
                encoded[key] = value
            else:
                bounds = {
                    "quiet_start": (-1, 1439),
                    "quiet_end": (-1, 1439),
                    "utc_offset": (-840, 840),
                    "auto_lock": (30, 3600),
                }
                low, high = bounds[key]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(T.ARCHIVE_REPOSITORY_SETTING_RANGE)
                encoded[key] = value
        merged = self.owner(owner_id) | changes
        if (merged["quiet_start"] == -1) != (merged["quiet_end"] == -1):
            raise ValueError(T.ARCHIVE_REPOSITORY_QUIET_HOURS_PAIR)
        if encoded:
            assignments = ",".join(f"{key}=?" for key in encoded)
            with self.db:
                self.db.execute(
                    f"UPDATE archive_owners SET {assignments} WHERE owner_id=?", (*encoded.values(), owner_id)
                )
        return self.public_owner(owner_id)

    def save_connection(self, identifier: str, owner_id: int, enabled: bool, rights: dict) -> None:
        self.ensure_owner(owner_id)
        old = self.db.execute(
            "SELECT disconnected_at FROM archive_connection_details WHERE connection_id=? AND owner_id=?",
            (identifier, owner_id),
        ).fetchone()
        disconnected = None if enabled else ((old[0] if old else None) or time.time())
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO archive_connection_details VALUES (?,?,?,?,?)",
                (identifier, owner_id, json.dumps(rights), int(enabled), disconnected),
            )

    def connections(self, owner_id: int) -> list[dict]:
        rows = self.db.execute("SELECT * FROM archive_connection_details WHERE owner_id=?", (owner_id,)).fetchall()
        return [{**dict(row), "rights": json.loads(row["rights"])} for row in rows]

    def connection(self, owner_id: int, identifier: str) -> dict | None:
        return next((row for row in self.connections(owner_id) if row["connection_id"] == identifier), None)

    def chat(self, owner_id: int, identifier: int) -> dict | None:
        return _dict(
            self.db.execute("SELECT * FROM archive_chats WHERE owner_id=? AND id=?", (owner_id, identifier)).fetchone()
        )

    def chats(self, owner_id: int, *, query: str = "") -> list[dict]:
        rows = self.db.execute(
            """SELECT c.*,
            (SELECT count(*) FROM archive_messages m WHERE m.chat_id=c.id) AS message_count,
            (SELECT count(*) FROM archive_messages m WHERE m.chat_id=c.id AND m.deleted_at IS NOT NULL) AS deleted_count,
            (SELECT body FROM archive_messages m WHERE m.chat_id=c.id ORDER BY sent_at DESC,id DESC LIMIT 1) AS preview
            FROM archive_chats c WHERE c.owner_id=? AND (c.title LIKE ? OR c.username LIKE ?)
            ORDER BY c.last_at DESC,c.id DESC LIMIT 1000""",
            (owner_id, f"%{query[:100]}%", f"%{query[:100]}%"),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_chat(self, owner_id: int, identifier: int, changes: dict) -> dict:
        if not self.chat(owner_id, identifier):
            raise LookupError("Chat not found")
        if set(changes) - {"muted", "mark_read", "excluded"} or any(
            type(value) is not bool for value in changes.values()
        ):
            raise ValueError(T.ARCHIVE_REPOSITORY_INVALID_CHAT_SETTINGS)
        if changes:
            assignments = ",".join(f"{key}=?" for key in changes)
            with self.db:
                self.db.execute(
                    f"UPDATE archive_chats SET {assignments} WHERE owner_id=? AND id=?",
                    (*[int(value) for value in changes.values()], owner_id, identifier),
                )
        return self.chat(owner_id, identifier)

    def _event(
        self,
        owner: int,
        chat: int,
        message: int | None,
        kind: str,
        timestamp: float,
        dedupe: str,
        *,
        version: int | None = None,
        payload: dict | None = None,
    ) -> int | None:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO archive_events(owner_id,chat_id,message_id,version_id,kind,occurred_at,payload,dedupe) VALUES (?,?,?,?,?,?,?,?)",
            (owner, chat, message, version, kind, timestamp, json.dumps(payload or {}, ensure_ascii=False), dedupe),
        )
        if cursor.rowcount:
            identifier = cursor.lastrowid
            if kind != "new":
                self._enqueue(owner, "notify", {"event_ids": [identifier]}, f"notify:{identifier}")
            return identifier
        return None

    def ingest(
        self,
        *,
        owner_id: int,
        connection_id: str,
        source_chat_id: int,
        title: str,
        username: str,
        telegram_id: int,
        sent_at: float,
        changed_at: float,
        outgoing: bool,
        sender_id: int | None,
        media_group: str | None,
        payload: dict,
    ) -> int | None:
        owner = self.owner(owner_id)
        if not owner or not owner["enabled"] or owner["paused"]:
            return None
        body = payload["body"]
        snapshot = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        semantic = {
            **payload,
            "media": [{key: value for key, value in item.items() if key != "file_id"} for item in payload["media"]],
        }
        digest = hashlib.sha256(json.dumps(semantic, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO archive_chats(owner_id,connection_id,telegram_id,title,username,last_at) VALUES (?,?,?,?,?,?)",
                (owner_id, connection_id, source_chat_id, title, username, sent_at),
            )
            chat = self.db.execute(
                "SELECT * FROM archive_chats WHERE owner_id=? AND connection_id=? AND telegram_id=?",
                (owner_id, connection_id, source_chat_id),
            ).fetchone()
            if chat["excluded"]:
                return None
            if (chat["title"], chat["username"]) != (title, username):
                self._event(
                    owner_id,
                    chat["id"],
                    None,
                    "profile",
                    now,
                    f"profile:{chat['id']}:{hashlib.sha256((title + username).encode()).hexdigest()}:{int(now // 60)}",
                    payload={
                        "before": {"title": chat["title"], "username": chat["username"]},
                        "after": {"title": title, "username": username},
                    },
                )
            self.db.execute(
                "UPDATE archive_chats SET title=?,username=?,last_at=max(last_at,?) WHERE id=?",
                (title, username, sent_at, chat["id"]),
            )
            old = self.db.execute(
                "SELECT * FROM archive_messages WHERE chat_id=? AND telegram_id=?", (chat["id"], telegram_id)
            ).fetchone()
            if old is None:
                cursor = self.db.execute(
                    "INSERT INTO archive_messages(owner_id,chat_id,telegram_id,sent_at,outgoing,sender_id,body,media_group_id,reply_to) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        owner_id,
                        chat["id"],
                        telegram_id,
                        sent_at,
                        int(outgoing),
                        sender_id,
                        body,
                        media_group,
                        payload.get("reply_to"),
                    ),
                )
                message_id = cursor.lastrowid
            else:
                message_id = old["id"]
                if old["current_hash"] == digest:
                    return message_id
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO archive_versions(owner_id,message_id,changed_at,body,payload,content_hash) VALUES (?,?,?,?,?,?)",
                (owner_id, message_id, changed_at, body, snapshot, digest),
            )
            if not cursor.rowcount:
                return message_id
            version_id = cursor.lastrowid
            searchable = body + " " + " ".join(item["filename"] for item in payload["media"])
            self.db.execute(
                "INSERT INTO archive_search(rowid,owner_id,message_id,body) VALUES (?,?,?,?)",
                (version_id, owner_id, message_id, searchable),
            )
            is_edit = old is not None and bool(old["current_hash"]) and old["current_hash"] != digest
            if old is None or changed_at >= (old["edited_at"] or old["sent_at"]):
                self.db.execute(
                    "UPDATE archive_messages SET body=?,current_hash=?,edited_at=?,reply_to=? WHERE id=?",
                    (body, digest, changed_at if changed_at > sent_at else None, payload.get("reply_to"), message_id),
                )
            self._event(
                owner_id,
                chat["id"],
                message_id,
                "edited" if is_edit else "new",
                changed_at,
                f"version:{version_id}",
                version=version_id,
            )
            for item in payload["media"]:
                identifier = uuid.uuid4().hex
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO archive_media(id,owner_id,message_id,file_id,unique_id,kind,filename,mime_type,size,protected,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        owner_id,
                        message_id,
                        item["file_id"],
                        item["unique_id"],
                        item["kind"],
                        item["filename"],
                        item["mime_type"],
                        item["size"],
                        int(payload["protected"]),
                        now,
                    ),
                )
                if cursor.rowcount and not payload["protected"] and item["kind"] in owner["media_types"]:
                    self._enqueue(owner_id, "media", {"media_id": identifier}, f"media:{identifier}")
            if is_edit:
                self._enqueue(owner_id, "sync_copies", {"message_id": message_id}, f"sync:{version_id}")
            if not outgoing:
                matches = keyword_matches(body, owner["keywords"], owner["exclusions"], bool(owner["whole_words"]))
                if matches:
                    self._event(
                        owner_id,
                        chat["id"],
                        message_id,
                        "keyword",
                        changed_at,
                        f"keyword:{version_id}",
                        version=version_id,
                        payload={"matches": matches},
                    )
                if owner["read_mode"] == "all" or (owner["read_mode"] == "selected" and chat["mark_read"]):
                    self._enqueue(
                        owner_id, "read", {"chat_id": chat["id"], "telegram_id": telegram_id}, f"read:{message_id}"
                    )
        return message_id

    def delete_messages(
        self, owner_id: int, connection_id: str, source_chat_id: int, telegram_ids: list[int], title: str
    ) -> int:
        owner = self.owner(owner_id)
        if not owner or not owner["enabled"] or owner["paused"]:
            return 0
        now, count = time.time(), 0
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO archive_chats(owner_id,connection_id,telegram_id,title,last_at) VALUES (?,?,?,?,?)",
                (owner_id, connection_id, source_chat_id, title, now),
            )
            chat = self.db.execute(
                "SELECT * FROM archive_chats WHERE owner_id=? AND connection_id=? AND telegram_id=?",
                (owner_id, connection_id, source_chat_id),
            ).fetchone()
            if chat["excluded"]:
                return 0
            for telegram_id in dict.fromkeys(telegram_ids):
                row = self.db.execute(
                    "SELECT * FROM archive_messages WHERE chat_id=? AND telegram_id=?", (chat["id"], telegram_id)
                ).fetchone()
                if row and row["deleted_at"]:
                    continue
                if row is None:
                    cursor = self.db.execute(
                        "INSERT INTO archive_messages(owner_id,chat_id,telegram_id,sent_at,outgoing,deleted_at) VALUES (?,?,?,?,0,?)",
                        (owner_id, chat["id"], telegram_id, now, now),
                    )
                    message_id, kind = cursor.lastrowid, "unknown_deleted"
                else:
                    message_id, kind = row["id"], "deleted"
                    self.db.execute("UPDATE archive_messages SET deleted_at=? WHERE id=?", (now, message_id))
                self._event(owner_id, chat["id"], message_id, kind, now, f"deleted:{message_id}")
                self._enqueue(owner_id, "sync_copies", {"message_id": message_id}, f"deleted_copy:{message_id}")
                count += 1
            self.db.execute("UPDATE archive_chats SET last_at=max(last_at,?) WHERE id=?", (now, chat["id"]))
        return count

    def message(self, owner_id: int, identifier: int) -> dict | None:
        row = _dict(
            self.db.execute(
                "SELECT m.*,c.title AS chat_title,c.connection_id,c.telegram_id AS source_chat_id FROM archive_messages m JOIN archive_chats c ON c.id=m.chat_id WHERE m.owner_id=? AND m.id=?",
                (owner_id, identifier),
            ).fetchone()
        )
        if row:
            row["media"] = self.media_for_message(owner_id, identifier)
            versions = self.db.execute(
                "SELECT id,changed_at,body,payload FROM archive_versions WHERE owner_id=? AND message_id=? ORDER BY changed_at,id",
                (owner_id, identifier),
            ).fetchall()
            before = ""
            row["versions"] = []
            for version in versions:
                row["versions"].append(
                    {
                        "id": version["id"],
                        "changed_at": version["changed_at"],
                        "body": version["body"],
                        "diff": word_diff(before, version["body"]),
                    }
                )
                before = version["body"]
        return row

    def messages(self, owner_id: int, chat_id: int, *, cursor: int = 0, mode: str = "all", limit: int = 50) -> dict:
        if not self.chat(owner_id, chat_id):
            raise LookupError("Chat not found")
        conditions = {
            "all": "1",
            "deleted": "deleted_at IS NOT NULL",
            "edited": "edited_at IS NOT NULL",
            "favorites": "favorite=1",
        }
        if mode not in conditions:
            raise ValueError(T.ARCHIVE_REPOSITORY_UNKNOWN_MESSAGE_FILTER)
        rows = self.db.execute(
            f"SELECT * FROM archive_messages WHERE owner_id=? AND chat_id=? AND {conditions[mode]} AND (?=0 OR id<?) ORDER BY id DESC LIMIT ?",
            (owner_id, chat_id, cursor, cursor, limit + 1),
        ).fetchall()
        selected = [dict(row) for row in rows[:limit]]
        for item in selected:
            item["media"] = self.media_for_message(owner_id, item["id"])
        return {"items": selected, "next_cursor": selected[-1]["id"] if len(rows) > limit else None}

    def events(self, owner_id: int, *, cursor: int = 0, kind: str = "all", chat_id: int = 0, limit: int = 50) -> dict:
        where = "e.owner_id=? AND (?=0 OR e.id<?) AND (?=0 OR e.chat_id=?)"
        values = [owner_id, cursor, cursor, chat_id, chat_id]
        if kind == "favorites":
            where += " AND m.favorite=1"
        elif kind != "all":
            if kind not in {"new", "edited", "deleted", "keyword", "profile"}:
                raise ValueError(T.ARCHIVE_REPOSITORY_UNKNOWN_EVENT_FILTER)
            where += " AND e.kind IN ('deleted','unknown_deleted')" if kind == "deleted" else " AND e.kind=?"
            if kind != "deleted":
                values.append(kind)
        rows = self.db.execute(
            f"SELECT e.*,c.title AS chat_title,c.username,m.body,m.favorite,m.outgoing FROM archive_events e JOIN archive_chats c ON c.id=e.chat_id LEFT JOIN archive_messages m ON m.id=e.message_id WHERE {where} ORDER BY e.id DESC LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        items = [{**dict(row), "payload": json.loads(row["payload"])} for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    def search(self, owner_id: int, query: str, *, chat_id: int = 0, cursor: int = 0, limit: int = 50) -> dict:
        words = re.findall(r"\w+", query[:200], re.UNICODE)[:12]
        if not words:
            return {"items": [], "next_cursor": None}
        match = " AND ".join('"' + word + '"*' for word in words)
        rows = self.db.execute(
            """SELECT m.*,c.title AS chat_title,v.body AS match_text FROM archive_messages m
            JOIN archive_chats c ON c.id=m.chat_id JOIN archive_versions v ON v.id=(
              SELECT max(s.rowid) FROM archive_search s WHERE archive_search MATCH ?
              AND s.owner_id=? AND s.message_id=m.id)
            WHERE m.owner_id=? AND (?=0 OR m.chat_id=?) AND (?=0 OR m.id<?)
            ORDER BY m.id DESC LIMIT ?""",
            (match, owner_id, owner_id, chat_id, chat_id, cursor, cursor, limit + 1),
        ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    def favorite(self, owner_id: int, identifier: int, value: bool) -> None:
        with self.db:
            result = self.db.execute(
                "UPDATE archive_messages SET favorite=? WHERE owner_id=? AND id=?", (int(value), owner_id, identifier)
            )
        if not result.rowcount:
            raise LookupError("Message not found")

    def purge_message(self, owner_id: int, identifier: int) -> None:
        with self.db:
            result = self.db.execute("DELETE FROM archive_messages WHERE owner_id=? AND id=?", (owner_id, identifier))
        if not result.rowcount:
            raise LookupError("Message not found")

    def media(self, owner_id: int, identifier: str) -> dict | None:
        return _dict(
            self.db.execute(
                "SELECT a.*,m.chat_id,c.topic_id,c.telegram_id AS source_chat_id,c.title AS chat_title,c.connection_id FROM archive_media a JOIN archive_messages m ON m.id=a.message_id JOIN archive_chats c ON c.id=m.chat_id WHERE a.owner_id=? AND a.id=?",
                (owner_id, identifier),
            ).fetchone()
        )

    def media_for_message(self, owner_id: int, identifier: int) -> list[dict]:
        # file_id and physical paths are deliberately excluded from API responses.
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id,kind,filename,mime_type,size,status,protected,vault_message_id FROM archive_media WHERE owner_id=? AND message_id=? ORDER BY created_at,id",
                (owner_id, identifier),
            )
        ]

    def gallery(self, owner_id: int, *, chat_id: int = 0, cursor: int = 0, limit: int = 40) -> dict:
        # A timestamp alone loses media when an album has identical creation times.
        rows = self.db.execute(
            "SELECT a.rowid AS position,a.id,a.kind,a.filename,a.mime_type,a.size,a.status,a.protected,a.created_at,a.message_id,m.chat_id,c.title AS chat_title FROM archive_media a JOIN archive_messages m ON m.id=a.message_id JOIN archive_chats c ON c.id=m.chat_id WHERE a.owner_id=? AND (?=0 OR m.chat_id=?) AND (?=0 OR a.rowid<?) ORDER BY a.rowid DESC LIMIT ?",
            (owner_id, chat_id, chat_id, cursor, cursor, limit + 1),
        ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["position"] if len(rows) > limit else None}

    def _enqueue(self, owner_id: int, kind: str, payload: dict, dedupe: str | None = None, delay: float = 0) -> int:
        now = time.time()
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO archive_tasks(owner_id,kind,payload,available_at,created_at,updated_at,dedupe) VALUES (?,?,?,?,?,?,?)",
            (owner_id, kind, json.dumps(payload, ensure_ascii=False), now + delay, now, now, dedupe),
        )
        return cursor.lastrowid if cursor.rowcount else 0

    def enqueue(self, owner_id: int, kind: str, payload: dict, *, dedupe: str | None = None) -> int:
        with self.db:
            return self._enqueue(owner_id, kind, payload, dedupe)

    def recover(self) -> None:
        with self.db:
            self.db.execute("UPDATE archive_tasks SET status='pending' WHERE status='running'")
            self.db.execute(
                "UPDATE archive_tasks SET status='uncertain',error_code='interrupted_send' WHERE status='sending'"
            )

    def claim(self) -> dict | None:
        with self.db:
            row = self.db.execute(
                "SELECT * FROM archive_tasks WHERE status='pending' AND available_at<=? ORDER BY id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE archive_tasks SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
        return {**dict(row), "payload": json.loads(row["payload"]), "attempts": row["attempts"] + 1}

    def task_status(
        self, identifier: int, status: str, *, code: str | None = None, delay: float = 0, result: dict | None = None
    ):
        with self.db:
            self.db.execute(
                "UPDATE archive_tasks SET status=?,error_code=?,available_at=?,updated_at=?,result=? WHERE id=?",
                (status, code, time.time() + delay, time.time(), json.dumps(result) if result else None, identifier),
            )

    def task(self, owner_id: int, identifier: int) -> dict | None:
        row = _dict(
            self.db.execute(
                "SELECT id,kind,status,error_code,result,created_at FROM archive_tasks WHERE owner_id=? AND id=?",
                (owner_id, identifier),
            ).fetchone()
        )
        if row and row["result"]:
            row["result"] = json.loads(row["result"])
        return row

    def session(self, raw_token: str) -> dict | None:
        return _dict(
            self.db.execute(
                "SELECT * FROM archive_sessions WHERE token_hash=? AND expires_at>?",
                (token_hash(raw_token), time.time()),
            ).fetchone()
        )

    def create_session(self, owner_id: int, ttl: int) -> tuple[str, str]:
        token, csrf = new_token(), new_token()
        owner = self.owner(owner_id)
        unlocked = 0 if owner["pin_hash"] else time.time() + ttl
        with self.db:
            self.db.execute(
                "INSERT INTO archive_sessions(token_hash,owner_id,csrf_hash,expires_at,unlocked_until) VALUES (?,?,?,?,?)",
                (token_hash(token), owner_id, token_hash(csrf), time.time() + ttl, unlocked),
            )
            self.db.execute("DELETE FROM archive_sessions WHERE expires_at<?", (time.time(),))
        return token, csrf

    def purge_chat(self, owner_id: int, chat_id: int, *, since: float = 0, exclude: bool = False) -> None:
        if not self.chat(owner_id, chat_id):
            raise LookupError("Chat not found")
        with self.db:
            self.db.execute(
                "DELETE FROM archive_messages WHERE owner_id=? AND chat_id=? AND sent_at>=?", (owner_id, chat_id, since)
            )
            self.db.execute(
                "DELETE FROM archive_events WHERE owner_id=? AND chat_id=? AND message_id IS NULL", (owner_id, chat_id)
            )
            if exclude:
                self.db.execute("UPDATE archive_chats SET excluded=1 WHERE owner_id=? AND id=?", (owner_id, chat_id))

    def purge_owner(self, owner_id: int) -> bool:
        owner = self.owner(owner_id)
        vault_remains = bool(owner and owner["vault_chat_id"])
        with self.db:
            self.db.execute("DELETE FROM archive_owners WHERE owner_id=?", (owner_id,))
        self.ensure_owner(owner_id)
        return vault_remains

    def record_error(self, code: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO archive_errors(code,count,last_at) VALUES (?,1,?) ON CONFLICT(code) DO UPDATE SET count=count+1,last_at=excluded.last_at",
                (code[:80], time.time()),
            )
