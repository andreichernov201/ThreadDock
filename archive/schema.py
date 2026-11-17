"""Migration 2 adds archive tables without changing existing downloader records."""

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
 media_types TEXT NOT NULL DEFAULT '["photo","video","document","audio","voice","video_note","animation","sticker"]',
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
 protected INTEGER NOT NULL DEFAULT 0, local_path TEXT, status TEXT NOT NULL DEFAULT 'pending',
 vault_message_id INTEGER, created_at REAL NOT NULL, UNIQUE(message_id,unique_id,kind)
);
CREATE INDEX IF NOT EXISTS archive_media_owner ON archive_media(owner_id,created_at);
CREATE TABLE IF NOT EXISTS archive_events (
 id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 chat_id INTEGER NOT NULL REFERENCES archive_chats(id) ON DELETE CASCADE,
 message_id INTEGER REFERENCES archive_messages(id) ON DELETE CASCADE,
 version_id INTEGER REFERENCES archive_versions(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, occurred_at REAL NOT NULL, payload TEXT NOT NULL DEFAULT '{}', dedupe TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS archive_events_owner ON archive_events(owner_id,occurred_at,id);
CREATE TABLE IF NOT EXISTS archive_tasks (
 id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES archive_owners(owner_id) ON DELETE CASCADE,
 kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, dedupe TEXT UNIQUE, error_code TEXT,
 result TEXT, progress INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS archive_tasks_claim ON archive_tasks(status,available_at,id);
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
PRAGMA user_version = 2;
"""


def migrate(connection) -> None:
    connection.executescript(SCHEMA)
