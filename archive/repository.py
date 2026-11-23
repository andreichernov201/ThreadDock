"""Archive persistence. Every read and mutation is explicitly scoped to owner_id."""

import hashlib
import json
import logging
import re
import time
import uuid

from archive.auth import new_token, token_hash
from archive.config import PREFETCH_TTL
from archive.content import keyword_matches, visible_content, word_diff
from archive.schema import DEFAULT_MEDIA_TYPES, DEFAULT_MEDIA_TYPES_JSON, migrate
from core import texts as T

logger = logging.getLogger(__name__)
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
MEDIA_TYPES = set(DEFAULT_MEDIA_TYPES)
RECORD_KINDS = "('edited','deleted','unknown_deleted','reply_media','protected_media')"
FEED_KINDS = RECORD_KINDS[:-1] + ",'profile')"
LATEST_VERSION = "changed_at DESC,coalesce(json_extract(payload,'$.archive_edit'),1) DESC,id DESC"
RETAINED_MESSAGE = (
    """EXISTS (SELECT 1 FROM archive_events retained
 WHERE retained.message_id=m.id AND retained.owner_id=m.owner_id
 AND retained.kind IN """
    + RECORD_KINDS
    + ")"
)


def _dict(row):
    return dict(row) if row is not None else None


class ArchiveRepository:
    def __init__(self, database):
        self.database = database
        self.db = database.connection
        migrate(self.db)
        if repaired := self.repair_legacy_edits():
            logger.info("archive_migration metadata_only_edits=%s", repaired)

    def repair_legacy_edits(self) -> int:
        """Reclassify old metadata-only changes while keeping the captured versions."""
        with self.db:
            if self.db.execute("SELECT 1 FROM state WHERE key='archive_visible_edits_v1'").fetchone():
                return 0
            edited = self.db.execute(
                """SELECT e.id, e.owner_id, e.message_id, v.id AS version_id, v.payload, v.changed_at, m.sent_at
                FROM archive_events e JOIN archive_versions v ON v.id=e.version_id
                JOIN archive_messages m ON m.id=e.message_id
                WHERE e.kind='edited' AND json_type(v.payload,'$.archive_edit') IS NULL"""
            )
            repaired = set()
            count = 0
            for event in edited.fetchall():
                previous = self.previous_version(event["owner_id"], event["message_id"], event["version_id"])
                if previous:
                    if visible_content(json.loads(event["payload"])) != visible_content(
                        json.loads(previous["payload"])
                    ):
                        continue
                elif event["changed_at"] != event["sent_at"]:
                    continue
                self.db.execute("UPDATE archive_events SET kind='new' WHERE id=?", (event["id"],))
                self.db.execute(
                    "UPDATE archive_tasks SET status='done',error_code='metadata_only' WHERE dedupe=? AND status='pending'",
                    (f"notify:{event['id']}",),
                )
                repaired.add(event["message_id"])
                count += 1
            self.db.executemany(
                """UPDATE archive_messages SET edited_at=(
                    SELECT max(occurred_at) FROM archive_events e WHERE e.message_id=archive_messages.id AND e.kind='edited'
                ) WHERE id=?""",
                [(message_id,) for message_id in repaired],
            )
            self.db.execute("INSERT INTO state(key,value) VALUES ('archive_visible_edits_v1','1')")
        return count

    def ensure_owner(self, owner_id: int, language: str = "ru") -> dict:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO archive_owners(owner_id,language,created_at,media_types) VALUES (?,?,?,?)",
                (owner_id, "ru" if language.startswith("ru") else "en", time.time(), DEFAULT_MEDIA_TYPES_JSON),
            )
        return self.owner(owner_id)

    def owner(self, owner_id: int) -> dict | None:
        row = _dict(self.db.execute("SELECT * FROM archive_owners WHERE owner_id=?", (owner_id,)).fetchone())
        if row:
            for field in JSON_SETTINGS:
                row[field] = json.loads(row[field])
        return row

    def author_excluded(self, sender_id: int | None) -> bool:
        return (
            sender_id is not None
            and self.db.execute("SELECT 1 FROM archive_author_optouts WHERE sender_id=?", (sender_id,)).fetchone()
            is not None
        )

    def set_author_optout(self, sender_id: int, excluded: bool) -> set[int]:
        """Global author policy, independent of the archive owner's preferences."""
        with self.db:
            if not excluded:
                self.db.execute("DELETE FROM archive_author_optouts WHERE sender_id=?", (sender_id,))
                return set()
            self.db.execute("INSERT OR IGNORE INTO archive_author_optouts VALUES (?,?)", (sender_id, time.time()))
            owners = {
                row[0]
                for row in self.db.execute(
                    "SELECT DISTINCT owner_id FROM archive_messages WHERE sender_id=?", (sender_id,)
                )
            }
            self.db.execute("DELETE FROM archive_messages WHERE sender_id=?", (sender_id,))
            self.db.execute("DELETE FROM archive_stat_messages WHERE sender_id=?", (sender_id,))
        return owners

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
            f"""SELECT c.id,c.owner_id,c.connection_id,c.telegram_id,c.title,c.username,c.excluded,c.muted,c.mark_read,c.topic_id,
            (SELECT count(*) FROM archive_messages m WHERE m.chat_id=c.id AND {RETAINED_MESSAGE}) AS message_count,
            (SELECT count(*) FROM archive_messages m WHERE m.chat_id=c.id AND m.deleted_at IS NOT NULL) AS deleted_count,
            coalesce(v.body,m.body) AS preview,e.kind AS preview_kind,e.occurred_at AS last_at
            FROM archive_chats c LEFT JOIN archive_events e ON e.id=(
              SELECT latest.id FROM archive_events latest
              WHERE latest.owner_id=c.owner_id AND latest.chat_id=c.id AND latest.kind IN {RECORD_KINDS}
              ORDER BY latest.occurred_at DESC,latest.id DESC LIMIT 1
            ) LEFT JOIN archive_versions v ON v.id=e.version_id LEFT JOIN archive_messages m ON m.id=e.message_id
            WHERE c.owner_id=? AND (c.title LIKE ? OR c.username LIKE ?)
            ORDER BY last_at DESC,c.id DESC LIMIT 1000""",
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
        captured_from_reply: bool = False,
        edited_update: bool | None = None,
        update_id: int | None = None,
    ) -> int | None:
        owner = self.owner(owner_id)
        if not owner or not owner["enabled"] or owner["paused"] or self.author_excluded(sender_id):
            return None
        body = payload["body"]
        edited_update = changed_at > sent_at if edited_update is None else edited_update
        payload = payload | {"archive_edit": int(edited_update), "archive_from_reply": captured_from_reply}
        snapshot = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content = visible_content(payload)
        digest = hashlib.sha256(
            json.dumps((content, update_id, edited_update), ensure_ascii=False).encode()
        ).hexdigest()
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
            if (
                chat["excluded"]
                or self.db.execute(
                    "SELECT 1 FROM archive_stat_messages WHERE chat_id=? AND telegram_id=?",
                    (chat["id"], telegram_id),
                ).fetchone()
            ):
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
            before = self.db.execute(
                """SELECT * FROM archive_versions WHERE message_id=? AND changed_at<=?
                AND (? OR coalesce(json_extract(payload,'$.archive_edit'),changed_at>?)=0)
                ORDER BY changed_at DESC,coalesce(json_extract(payload,'$.archive_edit'),1) DESC,id DESC LIMIT 1""",
                (message_id, changed_at, int(edited_update), sent_at),
            ).fetchone()
            # Nested replies can omit fields or carry an older copy of a known version.
            # A direct original can also arrive after its edit or deletion.
            before_payload = json.loads(before["payload"]) if before else {}
            if before and captured_from_reply:
                version_id = before["id"]
            elif before and visible_content(before_payload) == content:
                version_id = before["id"]
                if not edited_update and before_payload.get("archive_from_reply"):
                    self.db.execute("UPDATE archive_versions SET payload=? WHERE id=?", (snapshot, version_id))
                    self.db.execute(
                        "UPDATE archive_messages SET reply_to=coalesce(reply_to,?) WHERE id=?",
                        (payload.get("reply_to"), message_id),
                    )
            elif before and not edited_update and not before_payload.get("archive_from_reply"):
                return message_id
            else:
                version_id = self._save_version(
                    owner_id, chat["id"], message_id, changed_at, body, snapshot, digest, edited_update
                )
                if version_id is None:
                    return message_id
                if edited_update:
                    self._enqueue(owner_id, "sync_copies", {"message_id": message_id}, f"sync:{version_id}")
                if not outgoing:
                    matches = keyword_matches(body, owner["keywords"], owner["exclusions"], bool(owner["whole_words"]))
                    if matches and not captured_from_reply:
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
                    if not captured_from_reply and (
                        owner["read_mode"] == "all" or (owner["read_mode"] == "selected" and chat["mark_read"])
                    ):
                        self._enqueue(
                            owner_id, "read", {"chat_id": chat["id"], "telegram_id": telegram_id}, f"read:{message_id}"
                        )
            if old and old["deleted_at"]:
                self.db.execute(
                    f"""UPDATE archive_events SET kind='deleted',version_id=(
                        SELECT id FROM archive_versions WHERE message_id=? ORDER BY {LATEST_VERSION} LIMIT 1
                    ) WHERE message_id=? AND kind IN ('deleted','unknown_deleted')""",
                    (message_id, message_id),
                )
                self.db.execute(
                    "UPDATE archive_messages SET sent_at=?,outgoing=?,sender_id=coalesce(?,sender_id),media_group_id=? WHERE id=?",
                    (sent_at, int(outgoing), sender_id, media_group, message_id),
                )
            special = "reply_media" if payload.get("ephemeral") else "protected_media" if payload["protected"] else None
            if special == "reply_media" or (special and payload["media"]):
                self._event(
                    owner_id,
                    chat["id"],
                    message_id,
                    special,
                    now,
                    f"{special}:{message_id}",
                    version=version_id,
                    payload={"from_reply": captured_from_reply, "ephemeral": special == "reply_media"},
                )
            for item in payload["media"]:
                identifier = uuid.uuid4().hex
                self.db.execute(
                    """INSERT INTO archive_media(id,owner_id,message_id,file_id,unique_id,kind,filename,mime_type,size,protected,ephemeral,created_at,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'staged') ON CONFLICT(message_id,unique_id,kind) DO UPDATE SET
                    file_id=excluded.file_id,protected=max(archive_media.protected,excluded.protected),
                    ephemeral=max(archive_media.ephemeral,excluded.ephemeral)""",
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
                        int(bool(payload.get("ephemeral"))),
                        now,
                    ),
                )
                if not captured_from_reply and not special and item["kind"] in owner["media_types"]:
                    staged = self.db.execute(
                        """SELECT a.id FROM archive_media a WHERE a.message_id=? AND a.unique_id=? AND a.kind=?
                        AND a.status='staged' AND a.local_path IS NULL AND a.created_at>?
                        AND NOT EXISTS (SELECT 1 FROM archive_media_cache p WHERE p.media_id=a.id)""",
                        (message_id, item["unique_id"], item["kind"], now - PREFETCH_TTL),
                    ).fetchone()
                    if staged:
                        self._enqueue(owner_id, "prefetch", {"media_id": staged[0]}, f"prefetch:{staged[0]}")
            urgent = special == "reply_media" or (captured_from_reply and special == "protected_media")
            self._activate_media(owner_id, message_id, owner["media_types"], urgent=urgent)
        return message_id

    def _save_version(
        self, owner_id, chat_id, message_id, changed_at, body, snapshot, digest, edited_update
    ) -> int | None:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO archive_versions(owner_id,message_id,changed_at,body,payload,content_hash) VALUES (?,?,?,?,?,?)",
            (owner_id, message_id, changed_at, body, snapshot, digest),
        )
        if not cursor.rowcount:
            return None
        version_id = cursor.lastrowid
        searchable = body + " " + " ".join(item["filename"] for item in json.loads(snapshot)["media"])
        self.db.execute(
            "INSERT INTO archive_search(rowid,owner_id,message_id,body) VALUES (?,?,?,?)",
            (version_id, owner_id, message_id, searchable),
        )
        if (
            self.db.execute(
                f"SELECT id FROM archive_versions WHERE message_id=? ORDER BY {LATEST_VERSION} LIMIT 1",
                (message_id,),
            ).fetchone()[0]
            == version_id
        ):
            self.db.execute(
                "UPDATE archive_messages SET body=?,current_hash=?,edited_at=?,reply_to=? WHERE id=?",
                (body, digest, changed_at if edited_update else None, json.loads(snapshot).get("reply_to"), message_id),
            )
        self._event(
            owner_id,
            chat_id,
            message_id,
            "edited" if edited_update else "new",
            changed_at,
            f"version:{version_id}",
            version=version_id,
        )
        # A late original can prove that the next alleged edit had identical content.
        next_edit = self.db.execute(
            """SELECT e.id,e.kind,v.payload FROM archive_versions current JOIN archive_versions v
            ON v.message_id=current.message_id JOIN archive_events e ON e.version_id=v.id
            WHERE current.id=? AND e.kind IN ('new','edited')
            AND (v.changed_at,coalesce(json_extract(v.payload,'$.archive_edit'),1),v.id)
                >(current.changed_at,coalesce(json_extract(current.payload,'$.archive_edit'),1),current.id)
            ORDER BY v.changed_at,v.id LIMIT 1""",
            (version_id,),
        ).fetchone()
        if (
            next_edit
            and next_edit["kind"] == "edited"
            and visible_content(json.loads(next_edit["payload"])) == visible_content(json.loads(snapshot))
        ):
            self.db.execute("UPDATE archive_events SET kind='new' WHERE id=?", (next_edit["id"],))
            self.db.execute(
                "UPDATE archive_tasks SET status='done',error_code='metadata_only' WHERE dedupe=? AND status='pending'",
                (f"notify:{next_edit['id']}",),
            )
            self.db.execute(
                """UPDATE archive_messages SET edited_at=(
                    SELECT max(occurred_at) FROM archive_events WHERE message_id=? AND kind='edited'
                ) WHERE id=?""",
                (message_id, message_id),
            )
        return version_id

    def delete_messages(
        self,
        owner_id: int,
        connection_id: str,
        source_chat_id: int,
        telegram_ids: list[int],
        title: str,
        username: str = "",
    ) -> int:
        owner = self.owner(owner_id)
        if not owner or not owner["enabled"] or owner["paused"]:
            return 0
        now, count = time.time(), 0
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO archive_chats(owner_id,connection_id,telegram_id,title,username,last_at) VALUES (?,?,?,?,?,?)",
                (owner_id, connection_id, source_chat_id, title, username, now),
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
                    stat = self.db.execute(
                        "SELECT id,deleted_at FROM archive_stat_messages WHERE chat_id=? AND telegram_id=?",
                        (chat["id"], telegram_id),
                    ).fetchone()
                    if stat:
                        if stat["deleted_at"] is None:
                            self.db.execute(
                                "UPDATE archive_stat_messages SET deleted_at=? WHERE id=?", (now, stat["id"])
                            )
                            self.db.execute(
                                """INSERT INTO archive_stat_events(owner_id,chat_id,message_id,kind,occurred_at)
                                VALUES (?,?,?,'unknown_deleted',?)""",
                                (owner_id, chat["id"], stat["id"], now),
                            )
                            count += 1
                        continue
                    if self.author_excluded(source_chat_id):
                        continue
                    cursor = self.db.execute(
                        "INSERT INTO archive_messages(owner_id,chat_id,telegram_id,sent_at,outgoing,deleted_at) VALUES (?,?,?,?,0,?)",
                        (owner_id, chat["id"], telegram_id, now, now),
                    )
                    message_id, kind = cursor.lastrowid, "unknown_deleted"
                else:
                    message_id, kind = row["id"], "deleted"
                    self.db.execute("UPDATE archive_messages SET deleted_at=? WHERE id=?", (now, message_id))
                version = self.db.execute(
                    f"SELECT id FROM archive_versions WHERE message_id=? ORDER BY {LATEST_VERSION} LIMIT 1",
                    (message_id,),
                ).fetchone()
                self._event(
                    owner_id,
                    chat["id"],
                    message_id,
                    kind,
                    now,
                    f"deleted:{message_id}",
                    version=version[0] if version else None,
                )
                self._activate_media(owner_id, message_id, owner["media_types"])
                self._enqueue(owner_id, "sync_copies", {"message_id": message_id}, f"deleted_copy:{message_id}")
                count += 1
            self.db.execute("UPDATE archive_chats SET last_at=max(last_at,?) WHERE id=?", (now, chat["id"]))
        return count

    def retained(self, owner_id: int, message_id: int) -> bool:
        return (
            self.db.execute(
                f"SELECT 1 FROM archive_messages m WHERE m.owner_id=? AND m.id=? AND {RETAINED_MESSAGE}",
                (owner_id, message_id),
            ).fetchone()
            is not None
        )

    def _activate_media(self, owner_id: int, message_id: int, media_types: list[str], *, urgent: bool = False) -> None:
        if not self.retained(owner_id, message_id):
            return
        rows = self.db.execute(
            "SELECT id,kind FROM archive_media WHERE owner_id=? AND message_id=? AND status='staged'",
            (owner_id, message_id),
        ).fetchall()
        now = time.time()
        for row in rows:
            if row["kind"] in media_types:
                self.db.execute("UPDATE archive_media SET status='pending' WHERE id=?", (row["id"],))
                self.db.execute(
                    "UPDATE archive_media_cache SET pinned=1 WHERE media_id=? AND expires_at>?",
                    (row["id"], now),
                )
                self._enqueue(owner_id, "media", {"media_id": row["id"], "urgent": urgent}, f"media:{row['id']}")

        if urgent:
            self.db.execute(
                """UPDATE archive_tasks SET payload=json_set(payload,'$.urgent',1) WHERE owner_id=?
                AND kind='media' AND status='pending' AND json_extract(payload,'$.media_id') IN (
                    SELECT id FROM archive_media WHERE owner_id=? AND message_id=? AND status='pending'
                )""",
                (owner_id, owner_id, message_id),
            )

    def stage_untriggered_media(self) -> list[str]:
        """Retire old downloads, preserving Telegram references for later deletions."""
        with self.db:
            self.db.execute(
                """UPDATE archive_events SET kind='protected_media' WHERE kind='reply_media' AND EXISTS (
                SELECT 1 FROM archive_versions v WHERE v.id=archive_events.version_id
                AND json_extract(v.payload,'$.protected')=1 AND coalesce(json_extract(v.payload,'$.ephemeral'),0)=0)
                AND coalesce(json_extract(archive_events.payload,'$.ephemeral'),0)=0
                AND NOT EXISTS(SELECT 1 FROM archive_media a WHERE a.message_id=archive_events.message_id AND a.ephemeral=1)"""
            )
            # Earlier versions treated every replied attachment as disappearing.
            self.db.execute(
                """DELETE FROM archive_events WHERE kind='reply_media' AND NOT EXISTS (
                SELECT 1 FROM archive_versions v WHERE v.id=archive_events.version_id
                AND (json_extract(v.payload,'$.protected')=1 OR json_extract(v.payload,'$.ephemeral')=1))
                AND coalesce(json_extract(archive_events.payload,'$.ephemeral'),0)=0
                AND NOT EXISTS(SELECT 1 FROM archive_media a WHERE a.message_id=archive_events.message_id AND a.ephemeral=1)"""
            )
            rows = self.db.execute(
                f"SELECT a.id,a.local_path FROM archive_media a JOIN archive_messages m ON m.id=a.message_id WHERE NOT ({RETAINED_MESSAGE}) AND a.status!='staged'"
            ).fetchall()
            self.db.executemany(
                "UPDATE archive_media SET status='staged',local_path=NULL WHERE id=?",
                [(row["id"],) for row in rows],
            )
            self.db.execute(
                """UPDATE archive_tasks SET status='done',dedupe=NULL,error_code='no_archive_event'
                WHERE kind='media' AND status NOT IN ('sending','uncertain') AND json_extract(payload,'$.media_id') IN (
                  SELECT id FROM archive_media WHERE status='staged')"""
            )
            live_photos = self.db.execute(
                f"""SELECT a.owner_id,a.message_id,max(a.ephemeral) AS urgent FROM archive_media a
                JOIN archive_messages m ON m.id=a.message_id JOIN archive_owners o ON o.owner_id=a.owner_id
                WHERE a.kind='live_photo' AND a.status='staged' AND o.enabled=1 AND o.paused=0
                AND EXISTS(SELECT 1 FROM json_each(o.media_types) WHERE value='live_photo') AND {RETAINED_MESSAGE}
                GROUP BY a.owner_id,a.message_id"""
            ).fetchall()
            for row in live_photos:
                self._activate_media(row["owner_id"], row["message_id"], ["live_photo"], urgent=bool(row["urgent"]))
        return [row["local_path"] for row in rows if row["local_path"]]

    def previous_version(self, owner_id: int, message_id: int, version_id: int) -> dict | None:
        return _dict(
            self.db.execute(
                """SELECT before.* FROM archive_versions before JOIN archive_versions current
            ON current.owner_id=before.owner_id AND current.message_id=before.message_id
            WHERE current.id=? AND current.owner_id=? AND current.message_id=?
            AND (before.changed_at,coalesce(json_extract(before.payload,'$.archive_edit'),1),before.id)
                <(current.changed_at,coalesce(json_extract(current.payload,'$.archive_edit'),1),current.id)
            ORDER BY before.changed_at DESC,coalesce(json_extract(before.payload,'$.archive_edit'),1) DESC,before.id DESC LIMIT 1""",
                (version_id, owner_id, message_id),
            ).fetchone()
        )

    def retire_caption_downloads(self) -> int:
        with self.db:
            return self.db.execute(
                """UPDATE jobs SET status='cancelled',retryable=0,error_code='existing_media'
                WHERE request_key LIKE 'message:%' AND status IN ('queued','running','failed')
                AND EXISTS (
                  SELECT 1 FROM archive_messages m JOIN archive_chats c ON c.id=m.chat_id
                  JOIN archive_media a ON a.message_id=m.id
                  WHERE c.connection_id=jobs.business_id AND c.telegram_id=jobs.chat_id
                  AND m.telegram_id=jobs.message_id)"""
            ).rowcount

    def message(self, owner_id: int, identifier: int) -> dict | None:
        row = _dict(
            self.db.execute(
                "SELECT m.*,c.title AS chat_title,c.connection_id,c.telegram_id AS source_chat_id FROM archive_messages m JOIN archive_chats c ON c.id=m.chat_id WHERE m.owner_id=? AND m.id=?",
                (owner_id, identifier),
            ).fetchone()
        )
        if row:
            row["kind"] = self.db.execute(
                f"""SELECT kind FROM archive_events WHERE owner_id=? AND message_id=? AND kind IN {RECORD_KINDS}
                ORDER BY CASE kind WHEN 'deleted' THEN 0 WHEN 'unknown_deleted' THEN 0 WHEN 'edited' THEN 1 ELSE 2 END,
                occurred_at DESC,id DESC LIMIT 1""",
                (owner_id, identifier),
            ).fetchone()
            row["kind"] = row["kind"][0] if row["kind"] else None
            row["media"] = self.media_for_message(owner_id, identifier)
            versions = self.db.execute(
                """SELECT v.*, e.kind AS event_kind FROM archive_versions v LEFT JOIN archive_events e
                ON e.version_id=v.id AND e.kind IN ('new','edited')
                WHERE v.owner_id=? AND v.message_id=?
                ORDER BY v.changed_at,coalesce(json_extract(v.payload,'$.archive_edit'),1),v.id""",
                (owner_id, identifier),
            ).fetchall()
            before = None
            row["versions"] = []
            for version in versions:
                media = json.loads(version["payload"])["media"]
                row["versions"].append(
                    {
                        "id": version["id"],
                        "changed_at": version["changed_at"],
                        "body": version["body"],
                        "edited": version["event_kind"] == "edited",
                        "has_previous": before is not None,
                        "diff": word_diff(before or "", version["body"]),
                        "attachments": [item["filename"] for item in media],
                    }
                )
                before = version["body"]
        return row

    def messages(self, owner_id: int, chat_id: int, *, cursor: int = 0, mode: str = "all", limit: int = 50) -> dict:
        if not self.chat(owner_id, chat_id):
            raise LookupError("Chat not found")
        conditions = {
            "all": RETAINED_MESSAGE,
            "deleted": "deleted_at IS NOT NULL",
            "edited": "edited_at IS NOT NULL",
            "favorites": f"favorite=1 AND {RETAINED_MESSAGE}",
        }
        if mode not in conditions:
            raise ValueError(T.ARCHIVE_REPOSITORY_UNKNOWN_MESSAGE_FILTER)
        rows = self.db.execute(
            f"SELECT m.* FROM archive_messages m WHERE owner_id=? AND chat_id=? AND {conditions[mode]} AND (?=0 OR id<?) ORDER BY id DESC LIMIT ?",
            (owner_id, chat_id, cursor, cursor, limit + 1),
        ).fetchall()
        selected = [dict(row) for row in rows[:limit]]
        for item in selected:
            item["media"] = self.media_for_message(owner_id, item["id"])
        return {"items": selected, "next_cursor": selected[-1]["id"] if len(rows) > limit else None}

    def events(
        self,
        owner_id: int,
        *,
        cursor: int = 0,
        before: float | None = None,
        kind: str = "all",
        chat_id: int = 0,
        limit: int = 50,
    ) -> dict:
        where = "e.owner_id=? AND (?=0 OR e.chat_id=?)"
        values = [owner_id, chat_id, chat_id]
        if cursor and before is None:
            anchor = self.db.execute(
                "SELECT occurred_at FROM archive_events WHERE owner_id=? AND id=? AND (?=0 OR chat_id=?)",
                (owner_id, cursor, chat_id, chat_id),
            ).fetchone()
            if anchor:
                before = anchor["occurred_at"]
        if cursor and before is not None:
            where += " AND (e.occurred_at,e.id)<(?,?)"
            values.extend((before, cursor))
        elif cursor:
            where += " AND e.id<?"
            values.append(cursor)
        if kind == "favorites":
            where += f" AND m.favorite=1 AND e.kind IN {RECORD_KINDS}"
        elif kind == "all":
            where += f" AND e.kind IN {FEED_KINDS}"
        elif kind != "all":
            if kind not in {"edited", "deleted", "profile", "reply_media", "protected_media"}:
                raise ValueError(T.ARCHIVE_REPOSITORY_UNKNOWN_EVENT_FILTER)
            where += " AND e.kind IN ('deleted','unknown_deleted')" if kind == "deleted" else " AND e.kind=?"
            if kind != "deleted":
                values.append(kind)
        rows = self.db.execute(
            f"SELECT e.*,c.title AS chat_title,c.username,coalesce(v.body,m.body) AS body,m.favorite,m.outgoing FROM archive_events e JOIN archive_chats c ON c.id=e.chat_id LEFT JOIN archive_messages m ON m.id=e.message_id LEFT JOIN archive_versions v ON v.id=e.version_id WHERE {where} ORDER BY e.occurred_at DESC,e.id DESC LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        items = [{**dict(row), "payload": json.loads(row["payload"])} for row in rows[:limit]]
        counts = {
            row["kind"]: row["total"]
            for row in self.db.execute(
                f"SELECT kind,count(*) AS total FROM archive_events WHERE owner_id=? AND (?=0 OR chat_id=?) AND kind IN {FEED_KINDS} GROUP BY kind",
                (owner_id, chat_id, chat_id),
            )
        }
        counts["deleted"] = counts.get("deleted", 0) + counts.pop("unknown_deleted", 0)
        tail = items[-1] if len(rows) > limit else None
        return {
            "items": items,
            "next_cursor": tail["id"] if tail else None,
            "next_before": tail["occurred_at"] if tail else None,
            "counts": counts,
        }

    def search(
        self,
        owner_id: int,
        query: str,
        *,
        chat_id: int = 0,
        cursor: int = 0,
        limit: int = 50,
        mode: str = "all",
        events_only: bool = True,
    ) -> dict:
        words = re.findall(r"\w+", query[:200], re.UNICODE)[:12]
        if not words:
            return {"items": [], "next_cursor": None}
        match = " AND ".join('"' + word + '"*' for word in words)
        conditions = {
            "all": "1",
            "deleted": "m.deleted_at IS NOT NULL",
            "edited": "m.edited_at IS NOT NULL",
            "favorites": "m.favorite=1",
            "reply_media": "EXISTS(SELECT 1 FROM archive_events e WHERE e.message_id=m.id AND e.kind='reply_media')",
            "protected_media": "EXISTS(SELECT 1 FROM archive_events e WHERE e.message_id=m.id AND e.kind='protected_media')",
        }
        if mode not in conditions:
            raise ValueError(T.ARCHIVE_REPOSITORY_UNKNOWN_MESSAGE_FILTER)
        where = conditions[mode]
        if events_only:
            where += f" AND {RETAINED_MESSAGE}"
        rows = self.db.execute(
            f"""SELECT m.*,c.title AS chat_title,v.body AS match_text,
              (SELECT group_concat(json_extract(a.value,'$.filename'),', ')
               FROM json_each(v.payload,'$.media') a) AS match_filenames FROM archive_messages m
            JOIN archive_chats c ON c.id=m.chat_id JOIN archive_versions v ON v.id=(
              SELECT max(s.rowid) FROM archive_search s WHERE archive_search MATCH ?
              AND s.owner_id=? AND s.message_id=m.id)
            WHERE m.owner_id=? AND (?=0 OR m.chat_id=?) AND (?=0 OR m.id<?) AND ({where})
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
            count = self._clear_messages(owner_id, "m.id=?", (identifier,))
        if not count:
            raise LookupError("Message not found")

    def media(self, owner_id: int, identifier: str) -> dict | None:
        return _dict(
            self.db.execute(
                f"""SELECT a.*,m.chat_id,c.topic_id,c.telegram_id AS source_chat_id,c.title AS chat_title,c.connection_id,
                p.pinned AS cache_pinned,p.expires_at AS cache_expires_at
                FROM archive_media a JOIN archive_messages m ON m.id=a.message_id
                JOIN archive_chats c ON c.id=m.chat_id LEFT JOIN archive_media_cache p ON p.media_id=a.id
                WHERE a.owner_id=? AND a.id=? AND a.status!='staged' AND {RETAINED_MESSAGE}""",
                (owner_id, identifier),
            ).fetchone()
        )

    def staged_media(self, owner_id: int, identifier: str) -> dict | None:
        return _dict(
            self.db.execute(
                """SELECT a.*,p.size AS cached_size FROM archive_media a
                LEFT JOIN archive_media_cache p ON p.media_id=a.id
                WHERE a.owner_id=? AND a.id=? AND a.status='staged'""",
                (owner_id, identifier),
            ).fetchone()
        )

    def media_for_message(self, owner_id: int, identifier: int) -> list[dict]:
        # file_id and physical paths are deliberately excluded from API responses.
        return [
            dict(row)
            for row in self.db.execute(
                f"SELECT a.id,a.kind,a.filename,a.mime_type,a.size,a.status,a.protected,a.ephemeral,a.vault_message_id FROM archive_media a JOIN archive_messages m ON m.id=a.message_id WHERE a.owner_id=? AND a.message_id=? AND a.status!='staged' AND {RETAINED_MESSAGE} ORDER BY a.created_at,a.id",
                (owner_id, identifier),
            )
        ]

    def gallery(self, owner_id: int, *, chat_id: int = 0, cursor: int = 0, limit: int = 40) -> dict:
        # A timestamp alone loses media when an album has identical creation times.
        rows = self.db.execute(
            f"SELECT a.rowid AS position,a.id,a.kind,a.filename,a.mime_type,a.size,a.status,a.protected,a.ephemeral,a.created_at,a.message_id,m.chat_id,c.title AS chat_title FROM archive_media a JOIN archive_messages m ON m.id=a.message_id JOIN archive_chats c ON c.id=m.chat_id WHERE a.owner_id=? AND a.status!='staged' AND {RETAINED_MESSAGE} AND (?=0 OR m.chat_id=?) AND (?=0 OR a.rowid<?) ORDER BY a.rowid DESC LIMIT ?",
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

    def request_media(
        self, owner_id: int, media_id: str, *, retry: bool, confirmed_retry: tuple[int, float] | None = None
    ) -> int:
        kind = "media" if retry else "send_media"
        now = time.time()
        with self.db:
            previous = self.db.execute(
                """SELECT id,status,created_at FROM archive_tasks WHERE owner_id=? AND kind=?
                AND json_extract(payload,'$.media_id')=? ORDER BY id DESC LIMIT 1""",
                (owner_id, kind, media_id),
            ).fetchone()
            key = f"request:{kind}:{owner_id}:{media_id}:{int(now // 15)}"
            if confirmed_retry is not None:
                task_id, created_at = confirmed_retry
                original = self.db.execute(
                    """SELECT status FROM archive_tasks WHERE id=? AND created_at=? AND owner_id=?
                    AND kind='send_media' AND json_extract(payload,'$.media_id')=?""",
                    (task_id, created_at, owner_id, media_id),
                ).fetchone()
                if original is None:
                    raise LookupError("Media send task not found")
                key = f"confirmed:{owner_id}:{media_id}:{task_id}:{float(created_at)}"
                replacement = self.db.execute("SELECT id FROM archive_tasks WHERE dedupe=?", (key,)).fetchone()
                if replacement:
                    return replacement["id"]
                if original["status"] != "uncertain" or previous is None or previous["id"] != task_id:
                    raise ValueError(T.ARCHIVE_WEB_SEND_RETRY_STALE)
            elif previous and (
                previous["status"] in {"pending", "running", "sending", "uncertain"}
                or now - previous["created_at"] < 15
            ):
                return previous["id"]
            count = self.db.execute(
                """SELECT count(*) FROM archive_tasks WHERE owner_id=? AND created_at>?
                AND (kind='send_media' OR (kind='media' AND json_extract(payload,'$.explicit')=1))""",
                (owner_id, now - 60),
            ).fetchone()[0]
            if count >= 8:
                return 0
            job = self._enqueue(owner_id, kind, {"media_id": media_id, "explicit": True}, key)
            if job and confirmed_retry is not None:
                self.db.execute(
                    "UPDATE archive_tasks SET status='superseded',updated_at=? WHERE owner_id=? AND id=?",
                    (now, owner_id, task_id),
                )
            return job

    def deletion_status(self, owner_id: int) -> dict:
        return {
            "vault_pending": self.db.execute(
                "SELECT count(*) FROM archive_vault_copies WHERE owner_id=? AND state='pending'",
                (owner_id,),
            ).fetchone()[0],
            "vault_manual": self.db.execute(
                "SELECT count(*) FROM archive_vault_copies WHERE owner_id=? AND state='manual'",
                (owner_id,),
            ).fetchone()[0],
            "external_copies_require_manual_deletion": True,
        }

    def recover(self) -> None:
        with self.db:
            self.db.execute("UPDATE archive_tasks SET status='pending' WHERE status='running'")
            self.db.execute(
                "UPDATE archive_tasks SET status='uncertain',error_code='interrupted_send' WHERE status='sending'"
            )

    def claim(self, *, lane: str = "all", busy_owners: set[int] | None = None) -> dict | None:
        urgent = "(kind='media' AND coalesce(json_extract(payload,'$.urgent'),0)=1)"
        conditions = {
            "urgent": urgent,
            "standard": f"NOT {urgent}",
            "all": "1",
        }
        busy = tuple(busy_owners or ())
        placeholders = ",".join("?" for _ in busy)
        with self.db:
            row = self.db.execute(
                f"""SELECT * FROM archive_tasks t WHERE status='pending' AND available_at<=?
                AND ({conditions[lane]}) AND owner_id NOT IN ({placeholders})
                ORDER BY {"coalesce((SELECT max(updated_at) FROM archive_tasks recent WHERE recent.owner_id=t.owner_id AND recent.attempts>0),0)," if lane == "standard" else ""}
                CASE WHEN {urgent} THEN -1 WHEN kind='media' THEN 0 WHEN kind='notify' THEN 1
                     WHEN kind='prefetch' THEN 3 ELSE 2 END,id LIMIT 1""",
                (time.time(), *busy),
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
            self._clear_messages(owner_id, "m.chat_id=? AND m.sent_at>=?", (chat_id, since))
            self._clear_chat_events(owner_id, "e.chat_id=? AND e.occurred_at>=?", (chat_id, since))
            if exclude:
                self.db.execute("UPDATE archive_chats SET excluded=1 WHERE owner_id=? AND id=?", (owner_id, chat_id))

    def clear_history(self, owner_id: int) -> None:
        with self.db:
            self._clear_messages(owner_id, "1", ())
            self._clear_chat_events(owner_id, "1", ())

    def expire_messages(self, owner_id: int, before: float) -> int:
        return self._clear_messages(owner_id, "m.favorite=0 AND m.sent_at<?", (before,))

    def clear_disconnected(self, owner_id: int, before: float) -> int:
        disconnected = """SELECT c.id FROM archive_chats c JOIN archive_connection_details d
        ON c.connection_id=d.connection_id AND c.owner_id=d.owner_id
        WHERE d.owner_id=? AND d.enabled=0 AND d.disconnected_at<?"""
        count = self._clear_messages(owner_id, f"m.chat_id IN ({disconnected})", (owner_id, before))
        self._clear_chat_events(owner_id, f"e.chat_id IN ({disconnected})", (owner_id, before))
        return count

    def _clear_messages(self, owner_id: int, condition: str, params: tuple) -> int:
        """Move only numeric statistics before cascading removal of archived content."""
        where = f"m.owner_id=? AND ({condition})"
        params = (owner_id, *params)
        self.db.execute(
            f"""INSERT INTO archive_stat_messages(owner_id,chat_id,telegram_id,sender_id,sent_at,deleted_at,
                edited,outgoing,characters,has_link,has_reply,without_text)
            SELECT m.owner_id,m.chat_id,m.telegram_id,m.sender_id,m.sent_at,m.deleted_at,m.edited_at IS NOT NULL,
                m.outgoing,length(m.body),(m.body LIKE '%http://%' OR m.body LIKE '%https://%'),m.reply_to IS NOT NULL,m.body=''
            FROM archive_messages m LEFT JOIN archive_statistics_resets r ON r.owner_id=m.owner_id
            WHERE {where} AND (r.reset_at IS NULL OR m.sent_at>=r.reset_at)""",
            params,
        )
        source = "JOIN archive_stat_messages s ON s.chat_id=m.chat_id AND s.telegram_id=m.telegram_id"
        self.db.execute(
            f"""INSERT INTO archive_stat_events(owner_id,chat_id,message_id,kind,occurred_at)
            SELECT e.owner_id,e.chat_id,s.id,e.kind,e.occurred_at FROM archive_messages m
            {source} JOIN archive_events e ON e.message_id=m.id WHERE {where} AND e.kind!='new'""",
            params,
        )
        self.db.execute(
            f"""INSERT INTO archive_stat_media(message_id,kind,files,declared_bytes)
            SELECT s.id,a.kind,count(*),sum(a.size) FROM archive_messages m
            {source} JOIN archive_media a ON a.message_id=m.id WHERE {where} GROUP BY s.id,a.kind""",
            params,
        )
        return self.db.execute(f"DELETE FROM archive_messages AS m WHERE {where}", params).rowcount

    def _clear_chat_events(self, owner_id: int, condition: str, params: tuple) -> None:
        where = f"e.owner_id=? AND e.message_id IS NULL AND ({condition})"
        params = (owner_id, *params)
        self.db.execute(
            f"""INSERT INTO archive_stat_events(owner_id,chat_id,kind,occurred_at)
            SELECT e.owner_id,e.chat_id,e.kind,e.occurred_at FROM archive_events e
            LEFT JOIN archive_statistics_resets r ON r.owner_id=e.owner_id
            WHERE {where} AND (r.reset_at IS NULL OR e.occurred_at>=r.reset_at)""",
            params,
        )
        self.db.execute(f"DELETE FROM archive_events AS e WHERE {where}", params)

    def reset_statistics(self, owner_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM archive_stat_events WHERE owner_id=?", (owner_id,))
            self.db.execute("DELETE FROM archive_stat_messages WHERE owner_id=?", (owner_id,))
            self.db.execute(
                "INSERT OR REPLACE INTO archive_statistics_resets(owner_id,reset_at) VALUES (?,?)",
                (owner_id, time.time()),
            )

    def purge_owner(self, owner_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM archive_owners WHERE owner_id=?", (owner_id,))
        self.ensure_owner(owner_id)

    def record_error(self, code: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO archive_errors(code,count,last_at) VALUES (?,1,?) ON CONFLICT(code) DO UPDATE SET count=count+1,last_at=excluded.last_at",
                (code[:80], time.time()),
            )
