"""Migration 2 adds archive tables without changing existing downloader records."""

import json

LEGACY_MEDIA_TYPES = ("photo", "video", "document", "audio", "voice", "video_note", "animation", "sticker")
DEFAULT_MEDIA_TYPES = (*LEGACY_MEDIA_TYPES, "live_photo")
DEFAULT_MEDIA_TYPES_JSON = json.dumps(DEFAULT_MEDIA_TYPES, separators=(",", ":"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_owners (
 owner_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0,
 language TEXT NOT NULL DEFAULT 'ru', retention_days INTEGER NOT NULL DEFAULT 0,
 notify_enabled INTEGER NOT NULL DEFAULT 1, notify_edits INTEGER NOT NULL DEFAULT 1,
 notify_deletes INTEGER NOT NULL DEFAULT 1, notify_keywords INTEGER NOT NULL DEFAULT 1,
 notify_profiles INTEGER NOT NULL DEFAULT 0, hide_content INTEGER NOT NULL DEFAULT 1,
 quiet_start INTEGER NOT NULL DEFAULT -1, quiet_end INTEGER NOT NULL DEFAULT -1,
 utc_offset INTEGER NOT NULL DEFAULT 180, keywords TEXT NOT NULL DEFAULT '[]',
 exclusions TEXT NOT NULL DEFAULT '[]', whole_words INTEGER NOT NULL DEFAULT 0,
 read_mode TEXT NOT NULL DEFAULT 'none', vault_chat_id INTEGER UNIQUE,
 media_types TEXT NOT NULL DEFAULT '__DEFAULT_MEDIA_TYPES__',
 pin_hash TEXT, pin_salt TEXT, auto_lock INTEGER NOT NULL DEFAULT 300,
 auto_delete_disconnected INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_author_optouts (
 sender_id INTEGER PRIMARY KEY, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_connection_details (
 connection_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 rights TEXT NOT NULL, enabled INTEGER NOT NULL, disconnected_at REAL
);
CREATE TABLE IF NOT EXISTS archive_chats (
 id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 connection_id TEXT NOT NULL, telegram_id INTEGER NOT NULL, title TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
 excluded INTEGER NOT NULL DEFAULT 0, muted INTEGER NOT NULL DEFAULT 0, mark_read INTEGER NOT NULL DEFAULT 0,
 topic_id INTEGER, last_at REAL NOT NULL DEFAULT 0,
 UNIQUE(owner_id,connection_id,telegram_id)
);
CREATE INDEX IF NOT EXISTS archive_chats_owner ON archive_chats(owner_id,last_at);
CREATE TABLE IF NOT EXISTS archive_messages (
 id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 chat_id INTEGER NOT NULL REFERENCES archive_chats(id) ON DELETE CASCADE, telegram_id INTEGER NOT NULL,
 sent_at REAL NOT NULL, edited_at REAL, deleted_at REAL, outgoing INTEGER NOT NULL,
 sender_id INTEGER, body TEXT NOT NULL DEFAULT '', media_group_id TEXT, reply_to INTEGER,
 favorite INTEGER NOT NULL DEFAULT 0, current_hash TEXT NOT NULL DEFAULT '',
 UNIQUE(chat_id,telegram_id)
);
CREATE INDEX IF NOT EXISTS archive_messages_chat ON archive_messages(owner_id,chat_id,sent_at,id);
CREATE INDEX IF NOT EXISTS archive_messages_statistics ON archive_messages(owner_id,sent_at);
CREATE TABLE IF NOT EXISTS archive_versions (
 id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 message_id INTEGER NOT NULL REFERENCES archive_messages(id) ON DELETE CASCADE,
 changed_at REAL NOT NULL, body TEXT NOT NULL, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
 UNIQUE(message_id,changed_at,content_hash)
);
CREATE VIRTUAL TABLE IF NOT EXISTS archive_search USING fts5(owner_id UNINDEXED,message_id UNINDEXED,body,tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS archive_version_delete AFTER DELETE ON archive_versions BEGIN
 DELETE FROM archive_search WHERE rowid=old.id;
END;
CREATE TABLE IF NOT EXISTS archive_media (
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 message_id INTEGER NOT NULL REFERENCES archive_messages(id) ON DELETE CASCADE,
 file_id TEXT NOT NULL, unique_id TEXT NOT NULL, kind TEXT NOT NULL,
 filename TEXT NOT NULL, mime_type TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
 protected INTEGER NOT NULL DEFAULT 0, ephemeral INTEGER NOT NULL DEFAULT 0,
 local_path TEXT, status TEXT NOT NULL DEFAULT 'pending',
 vault_message_id INTEGER, created_at REAL NOT NULL, UNIQUE(message_id,unique_id,kind)
);
CREATE INDEX IF NOT EXISTS archive_media_owner ON archive_media(owner_id,created_at);
CREATE TABLE IF NOT EXISTS archive_media_cache (
 media_id TEXT PRIMARY KEY REFERENCES archive_media(id) ON DELETE CASCADE,
 size INTEGER NOT NULL, expires_at REAL NOT NULL, pinned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS archive_media_cache_expiry ON archive_media_cache(pinned,expires_at);
CREATE TABLE IF NOT EXISTS archive_events (
 id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 chat_id INTEGER NOT NULL REFERENCES archive_chats(id) ON DELETE CASCADE,
 message_id INTEGER REFERENCES archive_messages(id) ON DELETE CASCADE,
 version_id INTEGER REFERENCES archive_versions(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, occurred_at REAL NOT NULL, payload TEXT NOT NULL DEFAULT '{}', dedupe TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS archive_events_owner ON archive_events(owner_id,occurred_at,id);
CREATE INDEX IF NOT EXISTS archive_events_chat ON archive_events(owner_id,chat_id,occurred_at,id);
CREATE INDEX IF NOT EXISTS archive_events_message ON archive_events(message_id);
CREATE TABLE IF NOT EXISTS archive_tasks (
 id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, dedupe TEXT UNIQUE, error_code TEXT,
 result TEXT, progress INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS archive_tasks_claim ON archive_tasks(status,available_at,id);
CREATE INDEX IF NOT EXISTS archive_tasks_fair ON archive_tasks(owner_id,updated_at);
CREATE TABLE IF NOT EXISTS archive_vault_copies (
 owner_id INTEGER NOT NULL, media_id TEXT NOT NULL, chat_id INTEGER NOT NULL, telegram_id INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'live', attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(chat_id,telegram_id)
);
CREATE INDEX IF NOT EXISTS archive_vault_copies_media ON archive_vault_copies(media_id);
CREATE INDEX IF NOT EXISTS archive_vault_copies_state ON archive_vault_copies(state,available_at);
INSERT OR IGNORE INTO archive_vault_copies(owner_id,media_id,chat_id,telegram_id)
 SELECT a.owner_id,a.id,o.vault_chat_id,a.vault_message_id FROM archive_media a
 JOIN archive_owners o ON o.owner_id=a.owner_id WHERE o.vault_chat_id IS NOT NULL AND a.vault_message_id IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS archive_vault_cleanup AFTER DELETE ON archive_media BEGIN
 UPDATE archive_vault_copies SET state='pending' WHERE media_id=old.id AND state='live';
END;
CREATE TRIGGER IF NOT EXISTS archive_event_cancel_notice AFTER DELETE ON archive_events BEGIN
 UPDATE archive_tasks SET status='done',dedupe=NULL,error_code='record_removed'
 WHERE dedupe='notify:' || old.id;
END;
CREATE TABLE IF NOT EXISTS archive_sessions (
 token_hash TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 csrf_hash TEXT NOT NULL, expires_at REAL NOT NULL, unlocked_until REAL NOT NULL DEFAULT 0,
 failed_unlocks INTEGER NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS archive_biometrics (
 owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 device_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, PRIMARY KEY(owner_id,device_id)
);
CREATE TABLE IF NOT EXISTS archive_unlock_limits (
 owner_id INTEGER PRIMARY KEY REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 failures INTEGER NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS archive_vault_requests (
 owner_id INTEGER PRIMARY KEY REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_errors (
 id INTEGER PRIMARY KEY, code TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1,
 last_at REAL NOT NULL, UNIQUE(code)
);
CREATE TABLE IF NOT EXISTS archive_statistics_resets (
 owner_id INTEGER PRIMARY KEY REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 reset_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_stat_messages (
 id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 chat_id INTEGER NOT NULL REFERENCES archive_chats(id) ON DELETE CASCADE, telegram_id INTEGER NOT NULL,
 sender_id INTEGER, sent_at REAL NOT NULL, deleted_at REAL, edited INTEGER NOT NULL, outgoing INTEGER NOT NULL,
 characters INTEGER NOT NULL, has_link INTEGER NOT NULL, has_reply INTEGER NOT NULL, without_text INTEGER NOT NULL,
 UNIQUE(chat_id,telegram_id)
);
CREATE INDEX IF NOT EXISTS archive_stat_messages_owner ON archive_stat_messages(owner_id,sent_at);
CREATE INDEX IF NOT EXISTS archive_stat_messages_sender ON archive_stat_messages(sender_id);
CREATE TABLE IF NOT EXISTS archive_stat_events (
 owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 chat_id INTEGER NOT NULL REFERENCES archive_chats(id) ON DELETE CASCADE,
 message_id INTEGER REFERENCES archive_stat_messages(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, occurred_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS archive_stat_events_owner ON archive_stat_events(owner_id,occurred_at);
CREATE INDEX IF NOT EXISTS archive_stat_events_message ON archive_stat_events(message_id);
CREATE TABLE IF NOT EXISTS archive_stat_media (
 message_id INTEGER NOT NULL REFERENCES archive_stat_messages(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, files INTEGER NOT NULL, declared_bytes INTEGER NOT NULL,
 PRIMARY KEY(message_id,kind)
);
CREATE VIEW IF NOT EXISTS archive_message_statistics AS
 SELECT m.owner_id,m.chat_id,m.telegram_id,m.sent_at,m.deleted_at,m.edited_at IS NOT NULL AS edited,m.outgoing,
        length(m.body) AS characters,(m.body LIKE '%http://%' OR m.body LIKE '%https://%') AS has_link,
        m.reply_to IS NOT NULL AS has_reply,m.body='' AS without_text,m.favorite,m.body
 FROM archive_messages m LEFT JOIN archive_statistics_resets r ON r.owner_id=m.owner_id
 WHERE r.reset_at IS NULL OR m.sent_at>=r.reset_at
 UNION ALL
 SELECT owner_id,chat_id,telegram_id,sent_at,deleted_at,edited,outgoing,characters,has_link,has_reply,
        without_text,0,NULL FROM archive_stat_messages;
CREATE VIEW IF NOT EXISTS archive_event_statistics AS
 SELECT e.owner_id,e.chat_id,e.kind,e.occurred_at FROM archive_events e
 LEFT JOIN archive_messages m ON m.id=e.message_id
 LEFT JOIN archive_statistics_resets r ON r.owner_id=e.owner_id
 WHERE e.kind!='new' AND (r.reset_at IS NULL OR coalesce(m.sent_at,e.occurred_at)>=r.reset_at)
 UNION ALL
 SELECT owner_id,chat_id,kind,occurred_at FROM archive_stat_events;
CREATE VIEW IF NOT EXISTS archive_media_statistics AS
 SELECT m.owner_id,m.chat_id,m.sent_at,a.kind,1 AS files,a.size AS declared_bytes,
        a.status='ready' AS saved_files,CASE WHEN a.status='ready' THEN a.size ELSE 0 END AS saved_bytes
 FROM archive_media a JOIN archive_messages m ON m.id=a.message_id AND m.owner_id=a.owner_id
 LEFT JOIN archive_statistics_resets r ON r.owner_id=m.owner_id
 WHERE r.reset_at IS NULL OR m.sent_at>=r.reset_at
 UNION ALL
 SELECT m.owner_id,m.chat_id,m.sent_at,a.kind,a.files,a.declared_bytes,0,0
 FROM archive_stat_media a JOIN archive_stat_messages m ON m.id=a.message_id;
PRAGMA user_version = 2;
"""


def migrate(connection) -> None:
    connection.executescript(SCHEMA.replace("__DEFAULT_MEDIA_TYPES__", DEFAULT_MEDIA_TYPES_JSON))
    with connection:
        if connection.execute("SELECT 1 FROM state WHERE key='archive_media_flags_v1'").fetchone():
            return
        columns = {row[1] for row in connection.execute("PRAGMA table_info(archive_media)")}
        if "ephemeral" not in columns:
            connection.execute("ALTER TABLE archive_media ADD COLUMN ephemeral INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "UPDATE archive_owners SET media_types=? WHERE json(media_types)=?",
            (DEFAULT_MEDIA_TYPES_JSON, json.dumps(LEGACY_MEDIA_TYPES, separators=(",", ":"))),
        )
        # Unblock legacy disappearing attachments only where a saved version identifies the file.
        connection.execute(
            """WITH evidence AS (
                SELECT a.id,max(coalesce(json_extract(v.payload,'$.ephemeral'),0)) AS ephemeral,
                       max(coalesce(json_extract(v.payload,'$.protected'),0)) AS protected
                FROM archive_media a JOIN archive_versions v ON v.message_id=a.message_id
                JOIN json_each(v.payload,'$.media') item
                  ON json_extract(item.value,'$.unique_id')=a.unique_id AND json_extract(item.value,'$.kind')=a.kind
                WHERE json_extract(v.payload,'$.ephemeral')=1 OR json_extract(v.payload,'$.protected')=1
                GROUP BY a.id
            )
            UPDATE archive_media AS a SET ephemeral=1,protected=e.protected,status=CASE
                WHEN e.protected=0 AND a.status='protected' THEN 'remote' ELSE a.status END
            FROM evidence e WHERE e.id=a.id AND e.ephemeral=1"""
        )
        connection.execute("INSERT INTO state(key,value) VALUES ('archive_media_flags_v1','1')")
