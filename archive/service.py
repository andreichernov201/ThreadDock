"""Archive ingestion and a durable outbox, independent of downloader jobs."""

import asyncio
import html
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from threading import Event

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from archive.config import PREFETCH_TTL, ArchiveSettings
from archive.content import message_payload, visible_content
from archive.exports import bundle_export, write_export
from archive.repository import RETAINED_MESSAGE, ArchiveRepository
from core import texts as T
from core.errors import MediaError
from services.processes import remove_directory

logger = logging.getLogger(__name__)


class LimitedWriter:
    def __init__(self, file, limit: int):
        self.file, self.limit, self.written = file, limit, 0

    def write(self, data):
        self.written += len(data)
        if self.written > self.limit:
            raise MediaError(T.ARCHIVE_SERVICE_MEDIA_PREVIEW_TOO_LARGE, code="archive_media_size")
        return self.file.write(data)

    def __getattr__(self, name):
        return getattr(self.file, name)


def quiet_delay(owner: dict, now: float | None = None) -> float:
    now = time.time() if now is None else now
    start, end = owner["quiet_start"], owner["quiet_end"]
    if start < 0 or end < 0 or start == end:
        return 0
    minute = int((now + owner["utc_offset"] * 60) // 60) % 1440
    quiet = start <= minute < end if start < end else minute >= start or minute < end
    return ((end - minute) % 1440) * 60 + 1 if quiet else 0


class ArchiveService:
    def __init__(self, settings, database, bot, config: ArchiveSettings | None = None):
        self.settings, self.database, self.bot = settings, database, bot
        self.config = config or ArchiveSettings.from_environment()
        self.repository = ArchiveRepository(database)
        self.root = settings.data_dir / "archive"
        self.blobs, self.exports, self.uploads = (self.root / name for name in ("blobs", "exports", "uploads"))
        self.prefetch_cache = self.root / "cache"
        for path in (self.blobs, self.exports, self.uploads, self.prefetch_cache):
            path.mkdir(parents=True, exist_ok=True)
        self.stopping = asyncio.Event()
        self.task: asyncio.Task | None = None
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_maintenance = 0.0
        self._reserved_bytes = 0
        self._media_in_flight: set[str] = set()
        self._active_exports: dict[int, Event] = {}

    def owner_lock(self, owner_id: int) -> asyncio.Lock:
        return self._locks.setdefault(owner_id, asyncio.Lock())

    def connection_changed(self, connection) -> None:
        rights = connection.rights.model_dump(exclude_none=True) if connection.rights else {}
        self.repository.save_connection(connection.id, connection.user.id, connection.is_enabled, rights)

    async def ensure_connection(self, identifier: str) -> dict | None:
        info = self.database.connection_info(identifier)
        if info is None:
            connection = await self.bot.get_business_connection(identifier)
            self.database.save_connection(
                connection.id,
                connection.user.id,
                connection.is_enabled,
                bool(connection.rights and connection.rights.can_reply),
            )
            self.connection_changed(connection)
            info = self.database.connection_info(identifier)
        elif self.repository.connection(info["owner_id"], identifier) is None:
            connection = await self.bot.get_business_connection(identifier)
            self.connection_changed(connection)
        return dict(info) if info and info["enabled"] else None

    async def capture(self, message: Message, *, edited: bool = False, update_id: int | None = None) -> None:
        if (
            not message.business_connection_id
            or message.chat.type != "private"
            or not self.settings.business_enabled
            or (self.settings.business_chats and message.chat.id not in self.settings.business_chats)
        ):
            return
        if message.sender_business_bot:
            return
        info = await self.ensure_connection(message.business_connection_id)
        if not info:
            return
        owner_id = info["owner_id"]
        if message.from_user and message.from_user.id != owner_id and not message.from_user.is_bot:
            self.database.touch_business_chat(message.business_connection_id, message.chat.id, message.date.timestamp())
        owner = self.repository.owner(owner_id)
        if not owner or not owner["enabled"] or owner["paused"]:
            return
        title = (
            " ".join(part for part in (message.chat.first_name, message.chat.last_name) if part)
            or message.chat.title
            or str(message.chat.id)
        )
        for item in (message, message.reply_to_message):
            if (
                item is None
                or item.chat.id != message.chat.id
                or item.sender_business_bot
                or item.message_id <= 0
                or item.business_connection_id not in {None, message.business_connection_id}
            ):
                continue
            if self.repository.author_excluded(item.from_user.id if item.from_user else None):
                continue
            sent_at = item.date.timestamp()
            edited_at = item.edit_date
            if isinstance(edited_at, datetime):
                edited_at = edited_at.timestamp()
            self.repository.ingest(
                owner_id=owner_id,
                connection_id=message.business_connection_id,
                source_chat_id=message.chat.id,
                title=title[:250],
                username=message.chat.username or "",
                telegram_id=item.message_id,
                sent_at=sent_at,
                changed_at=edited_at if edited_at is not None else sent_at,
                outgoing=bool(item.from_user and item.from_user.id == owner_id),
                sender_id=item.from_user.id if item.from_user else None,
                media_group=item.media_group_id,
                payload=message_payload(item),
                captured_from_reply=item is message.reply_to_message,
                edited_update=edited if item is message else item.edit_date is not None,
                update_id=update_id if item is message else None,
            )

    async def deleted(self, event) -> None:
        if (
            event.chat.type != "private"
            or not self.settings.business_enabled
            or (self.settings.business_chats and event.chat.id not in self.settings.business_chats)
        ):
            return
        info = await self.ensure_connection(event.business_connection_id)
        if info:
            title = " ".join(part for part in (event.chat.first_name, event.chat.last_name) if part) or str(
                event.chat.id
            )
            self.repository.delete_messages(
                info["owner_id"],
                event.business_connection_id,
                event.chat.id,
                event.message_ids,
                title,
                event.chat.username or "",
            )

    def web_button(self, text: str = T.ARCHIVE_SERVICE_OPEN_ARCHIVE, *, chat_id: int = 0, message_id: int = 0):
        if not self.config.public_url:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=text,
                        web_app=WebAppInfo(
                            url=self.config.public_url
                            + (
                                f"/?chat={chat_id}&message={message_id}"
                                if message_id
                                else f"/?chat={chat_id}"
                                if chat_id
                                else "/"
                            )
                        ),
                    )
                ]
            ]
        )

    def start(self) -> None:
        removed = self.repository.stage_untriggered_media()
        for relative_path in removed:
            self.safe_path(relative_path, self.blobs).unlink(missing_ok=True)
        self._migrate_legacy_prefetch()
        self._maintain_prefetch_cache(time.time(), reconcile=True)
        cancelled = self.repository.retire_caption_downloads()
        logger.info("archive_policy retired_blobs=%s cancelled_caption_jobs=%s", len(removed), cancelled)
        self.repository.recover()
        self.task = asyncio.create_task(self._run_workers(), name="archive-outbox")

    async def close(self) -> None:
        self.stopping.set()
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), self.settings.shutdown_timeout)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

    async def _idle(self, seconds: float):
        try:
            await asyncio.wait_for(self.stopping.wait(), seconds)
        except TimeoutError:
            pass

    async def _run_workers(self):
        async with asyncio.TaskGroup() as group:
            group.create_task(self._worker("urgent"), name="archive-urgent-media")
            group.create_task(self._vault_cleanup(), name="archive-vault-cleanup")
            for index in range(2):
                group.create_task(self._worker("standard"), name=f"archive-worker-{index + 1}")

    async def _worker(self, lane: str):
        while not self.stopping.is_set():
            if time.time() - self._last_maintenance > 60:
                self.maintain()
                self._last_maintenance = time.time()
            busy = {owner for owner, lock in self._locks.items() if lock.locked()} if lane == "standard" else set()
            task = self.repository.claim(lane=lane, busy_owners=busy)
            if task is None:
                await self._idle(0.1 if lane == "urgent" else 0.25)
                continue
            if lane == "urgent":
                await self._process(task)
            else:
                async with self.owner_lock(task["owner_id"]):
                    await self._process(task)

    async def _vault_cleanup(self):
        while not self.stopping.is_set():
            row = self.repository.db.execute(
                """SELECT * FROM archive_vault_copies WHERE state='pending' AND available_at<=?
                ORDER BY available_at,chat_id,telegram_id LIMIT 1""",
                (time.time(),),
            ).fetchone()
            if row is None:
                await self._idle(1)
                continue
            key = row["chat_id"], row["telegram_id"]
            try:
                async with asyncio.timeout(30):
                    await self.bot.delete_message(*key)
            except TelegramRetryAfter as error:
                self._delay_vault_cleanup(key, error.retry_after + 1)
            except (TelegramNetworkError, TelegramServerError, TimeoutError):
                if row["attempts"] < 4:
                    self._delay_vault_cleanup(key, 2 ** row["attempts"])
                else:
                    self._mark_vault_manual(key, "network")
            except TelegramBadRequest as error:
                if "message to delete not found" in error.message.lower():
                    self._finish_vault_cleanup(key)
                else:
                    self._mark_vault_manual(key, "rejected")
            except TelegramAPIError:
                self._mark_vault_manual(key, "forbidden")
            else:
                self._finish_vault_cleanup(key)

    def _delay_vault_cleanup(self, key: tuple[int, int], delay: float):
        with self.repository.db:
            self.repository.db.execute(
                "UPDATE archive_vault_copies SET attempts=attempts+1,available_at=? WHERE chat_id=? AND telegram_id=?",
                (time.time() + delay, *key),
            )

    def _mark_vault_manual(self, key: tuple[int, int], code: str):
        with self.repository.db:
            self.repository.db.execute(
                "UPDATE archive_vault_copies SET state='manual' WHERE chat_id=? AND telegram_id=?",
                key,
            )
        self.repository.record_error(f"vault_cleanup:{code}")

    def _finish_vault_cleanup(self, key: tuple[int, int]):
        with self.repository.db:
            self.repository.db.execute("DELETE FROM archive_vault_copies WHERE chat_id=? AND telegram_id=?", key)

    async def _process(self, task: dict):
        identifier, owner_id = task["id"], task["owner_id"]
        started = time.monotonic()
        logger.info("archive_task=%s kind=%s attempt=%s state=started", identifier, task["kind"], task["attempts"])
        try:
            owner = self.repository.owner(owner_id)
            if owner is None:
                return
            if (
                (not owner["enabled"] or owner["paused"])
                and task["kind"] in {"notify", "read", "sync_copies", "media", "prefetch"}
                and not task["payload"].get("explicit")
            ):
                self.repository.task_status(identifier, "done", code="paused")
                return
            result = None
            async with asyncio.timeout(180):
                if task["kind"] == "notify":
                    delay = quiet_delay(owner)
                    if delay:
                        self.repository.task_status(identifier, "pending", delay=delay)
                        return
                    await self._notify(task, owner)
                elif task["kind"] in {"media", "prefetch"}:
                    media_id = task["payload"]["media_id"]
                    if media_id in self._media_in_flight:
                        self.repository.task_status(identifier, "pending", delay=0.5)
                        return
                    self._media_in_flight.add(media_id)
                    try:
                        await self._save_media(task, owner)
                    finally:
                        self._media_in_flight.remove(media_id)
                elif task["kind"] == "sync_copies":
                    await self._sync_copies(task, owner)
                elif task["kind"] == "read":
                    await self._read(task, owner)
                elif task["kind"] == "send_media":
                    await self._send_media(task, owner)
                elif task["kind"] == "export":
                    result = await self._export(task, owner)
                elif task["kind"] == "reply":
                    self.repository.task_status(identifier, "failed", code="reply_retired")
                    if upload := task["payload"].get("upload"):
                        self.safe_path(upload, self.uploads).unlink(missing_ok=True)
                    return
                elif task["kind"] == "vault_prompt":
                    await self._vault_prompt(task, owner)
                else:
                    raise ValueError("Unknown archive task")
            self.repository.task_status(identifier, "done", result=result)
            logger.info(
                "archive_task=%s kind=%s state=done elapsed_ms=%s",
                identifier,
                task["kind"],
                round((time.monotonic() - started) * 1000),
            )
        except asyncio.CancelledError:
            current = self.repository.task(owner_id, identifier)
            if current:
                self.repository.task_status(
                    identifier,
                    "uncertain" if current["status"] in {"sending", "uncertain"} else "pending",
                    code="shutdown",
                )
            raise
        except TelegramRetryAfter as error:
            self.repository.task_status(identifier, "pending", code="rate_limit", delay=error.retry_after + 1)
        except (TelegramNetworkError, TelegramServerError, TimeoutError):
            current = self.repository.task(owner_id, identifier)
            status = "uncertain" if current and current["status"] in {"sending", "uncertain"} else "pending"
            if status == "pending" and task["attempts"] >= 3:
                status = "failed"
            if status != "pending":
                self.repository.record_error(f"{task['kind']}:telegram_network:{status}")
            if status == "failed" and task["kind"] == "media":
                media = self.repository.media(owner_id, task["payload"]["media_id"])
                if media and not media["local_path"]:
                    self._media_status(media, "unavailable", False)
            self.repository.task_status(identifier, status, code="telegram_network", delay=5 * task["attempts"])
        except Exception as error:
            code = error.code if isinstance(error, MediaError) else type(error).__name__
            self.repository.task_status(identifier, "failed", code=code)
            self.repository.record_error(code)
            logger.exception("archive_task=%s kind=%s code=%s", identifier, task["kind"], code)

    async def _notify(self, task: dict, owner: dict):
        if not owner["notify_enabled"] or owner["paused"]:
            return
        event_ids = task["payload"]["event_ids"]
        rows = [
            self.repository.db.execute(
                "SELECT e.*,c.title,c.muted,COALESCE(v.body,m.body) AS body,v.payload AS version_payload FROM archive_events e JOIN archive_chats c ON c.id=e.chat_id LEFT JOIN archive_messages m ON m.id=e.message_id LEFT JOIN archive_versions v ON v.id=e.version_id AND v.owner_id=e.owner_id WHERE e.owner_id=? AND e.id=?",
                (owner["owner_id"], event),
            ).fetchone()
            for event in event_ids
        ]
        rows = [row for row in rows if row and not row["muted"]]
        field = {
            "edited": "notify_edits",
            "deleted": "notify_deletes",
            "unknown_deleted": "notify_deletes",
            "keyword": "notify_keywords",
            "profile": "notify_profiles",
        }
        rows = [row for row in rows if owner.get(field.get(row["kind"], "notify_enabled"))]
        if not rows:
            return
        # Coalesce pending deletion notices from this chat into the current request.
        batch_ids = [task["id"]]
        if rows[0]["kind"] in {"deleted", "unknown_deleted"}:
            more = self.repository.db.execute(
                "SELECT * FROM archive_tasks WHERE owner_id=? AND kind='notify' AND status='pending' AND available_at<=? ORDER BY id LIMIT 100",
                (owner["owner_id"], time.time() + 1),
            ).fetchall()
            for pending in more:
                events = json.loads(pending["payload"])["event_ids"]
                candidates = [
                    self.repository.db.execute(
                        "SELECT e.*,c.title,c.muted,COALESCE(v.body,m.body) AS body FROM archive_events e JOIN archive_chats c ON c.id=e.chat_id LEFT JOIN archive_messages m ON m.id=e.message_id LEFT JOIN archive_versions v ON v.id=e.version_id AND v.owner_id=e.owner_id WHERE e.owner_id=? AND e.id=?",
                        (owner["owner_id"], event),
                    ).fetchone()
                    for event in events
                ]
                if candidates and all(
                    row and row["chat_id"] == rows[0]["chat_id"] and row["kind"] in {"deleted", "unknown_deleted"}
                    for row in candidates
                ):
                    rows.extend(candidates)
                    batch_ids.append(pending["id"])
        labels = {
            "edited": T.ARCHIVE_SERVICE_MESSAGE_EDITED,
            "deleted": T.ARCHIVE_SERVICE_MESSAGE_DELETED,
            "unknown_deleted": T.ARCHIVE_SERVICE_UNKNOWN_DELETION,
            "keyword": T.ARCHIVE_SERVICE_KEYWORD_MATCH,
            "profile": T.ARCHIVE_SERVICE_PROFILE_CHANGED,
            "reply_media": T.ARCHIVE_SERVICE_EPHEMERAL,
            "protected_media": T.ARCHIVE_SERVICE_PROTECTED_ATTACHMENT,
        }
        title = (
            labels.get(rows[0]["kind"], T.ARCHIVE_SERVICE_ARCHIVE_EVENT)
            if len(rows) == 1
            else T.ARCHIVE_SERVICE_DELETED_COUNT.format(value1=len(rows))
        )
        text = T.ARCHIVE_SERVICE_NOTIFICATION_HEADER.format(
            title=html.escape(title), chat=html.escape(rows[0]["title"])
        )
        reason = {
            "deleted": T.ARCHIVE_REASON_DELETED,
            "unknown_deleted": T.ARCHIVE_REASON_UNKNOWN,
            "keyword": T.ARCHIVE_REASON_KEYWORD,
            "profile": T.ARCHIVE_REASON_PROFILE,
            "reply_media": T.ARCHIVE_REASON_REPLY_MEDIA,
            "protected_media": T.ARCHIVE_REASON_PROTECTED,
        }.get(rows[0]["kind"], "")
        if len(rows) > 1:
            reason = T.ARCHIVE_REASON_DELETED_BATCH.format(count=len(rows))
        previous = None
        if rows[0]["kind"] == "edited":
            row = rows[0]
            previous = self.repository.previous_version(owner["owner_id"], row["message_id"], row["version_id"])
            reason = T.ARCHIVE_REASON_EDITED if previous else T.ARCHIVE_REASON_EDIT_WITHOUT_ORIGINAL
        if reason:
            text += "\n" + html.escape(reason)
        if not owner["hide_content"] and rows[0]["kind"] == "edited":
            start = 0
            if previous and previous["body"] != row["body"]:
                common = os.path.commonprefix((previous["body"].strip(), row["body"].strip()))
                start = max(0, len(common) - 80)
            before = self._edit_preview(previous, start) if previous else T.ARCHIVE_SERVICE_PREVIOUS_MISSING
            text += T.ARCHIVE_SERVICE_EDIT_COMPARISON.format(
                before=html.escape(before),
                after=html.escape(self._edit_preview(row, start)),
            )
            if (
                previous
                and row["version_payload"]
                and (
                    visible_content(json.loads(previous["payload"]))[1]
                    != visible_content(json.loads(row["version_payload"]))[1]
                )
            ):
                text += "\n" + T.ARCHIVE_SERVICE_ATTACHMENT_CHANGED
        elif not owner["hide_content"]:
            previews = [
                html.escape((row["body"] or "").strip()[:500] or T.ARCHIVE_SERVICE_EMPTY_PREVIEW) for row in rows[:4]
            ]
            text += "\n\n" + "\n\n".join(previews)
        if len(rows) == 1 and rows[0]["kind"] == "keyword":
            text += "\n" + html.escape(
                T.ARCHIVE_REASON_MATCHES.format(words=", ".join(json.loads(rows[0]["payload"])["matches"]))
            )
        buttons = []
        if self.config.public_url and rows[0]["kind"] != "keyword":
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=T.ARCHIVE_SERVICE_OPEN_ARCHIVE,
                        web_app=WebAppInfo(
                            url=f"{self.config.public_url}/?chat={rows[0]['chat_id']}"
                            + (f"&message={rows[0]['message_id']}" if len(rows) == 1 and rows[0]["message_id"] else "")
                        ),
                    )
                ]
            )
        for job_id in batch_ids:
            self.repository.task_status(job_id, "sending")
        try:
            await self.bot.send_message(
                owner["owner_id"],
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
            )
        except TelegramRetryAfter as error:
            for job_id in batch_ids:
                self.repository.task_status(job_id, "pending", code="rate_limit", delay=error.retry_after + 1)
            raise
        except (TelegramNetworkError, TelegramServerError, TimeoutError, asyncio.CancelledError):
            for job_id in batch_ids:
                self.repository.task_status(job_id, "uncertain", code="notification_uncertain")
            raise
        except TelegramAPIError:
            for job_id in batch_ids:
                self.repository.task_status(job_id, "failed", code="notification_rejected")
            raise
        for job_id in batch_ids[1:]:
            self.repository.task_status(job_id, "done")

    @staticmethod
    def _edit_preview(version, start: int) -> str:
        payload = json.loads(
            (version["version_payload"] if "version_payload" in version.keys() else version["payload"]) or "{}"
        )
        body = (version["body"] or "").strip()
        names = ", ".join(item["filename"] for item in payload.get("media", []))
        excerpt = ("…" if start else "") + body[start : start + 320] + ("…" if len(body) > start + 320 else "")
        preview = "\n".join(filter(None, (excerpt, names[:120])))
        return preview or T.ARCHIVE_SERVICE_EMPTY_PREVIEW

    async def _save_media(self, task: dict, owner: dict):
        prefetch = task["kind"] == "prefetch"
        lookup = self.repository.staged_media if prefetch else self.repository.media
        media = lookup(owner["owner_id"], task["payload"]["media_id"])
        if not media or media["kind"] not in owner["media_types"]:
            return
        if prefetch:
            if media["cached_size"] is not None or media["created_at"] <= time.time() - PREFETCH_TTL:
                return
        elif media["cache_pinned"]:
            self._promote_prefetched(media)
            media = lookup(owner["owner_id"], media["id"])
        elif media["cache_expires_at"] is not None and media["cache_expires_at"] <= time.time():
            self._discard_prefetch(media)
        if media["size"] <= self.config.media_download_bytes and not media["local_path"]:
            budget = self._archive_download_budget(media["size"], prefetch)
            reserved = min(budget, media["size"] or self.config.media_download_bytes)
            if budget > 0 and budget >= media["size"]:
                path = self._media_path(media, self.prefetch_cache if prefetch else self.blobs)
                path.parent.mkdir(parents=True, exist_ok=True)
                partial = path.with_suffix(".part")
                self._reserved_bytes += reserved
                try:
                    with partial.open("wb") as output:
                        await self.bot.download(
                            media["file_id"],
                            destination=LimitedWriter(output, reserved),
                            timeout=30 if task["payload"].get("urgent") else 60,
                        )
                    # An event or an opt-out may have arrived while the file was downloading.
                    current = self.repository.media(owner["owner_id"], media["id"])
                    if current:
                        self._store_archive_file(current, partial)
                    elif prefetch and self.repository.staged_media(owner["owner_id"], media["id"]):
                        partial.replace(path)
                        with self.repository.db:
                            self.repository.db.execute(
                                "INSERT INTO archive_media_cache(media_id,size,expires_at) VALUES (?,?,?)",
                                (media["id"], path.stat().st_size, time.time() + PREFETCH_TTL),
                            )
                except (TelegramBadRequest, TelegramForbiddenError, MediaError, OSError) as error:
                    code = "get_file_rejected" if isinstance(error, TelegramAPIError) else type(error).__name__
                    self.repository.record_error(f"archive_media:{code}")
                    self._media_status(media, "protected" if media["protected"] else "remote", prefetch)
                finally:
                    self._reserved_bytes -= reserved
                    partial.unlink(missing_ok=True)
            else:
                self._media_status(media, "storage_full", prefetch)
        elif media["size"] > self.config.media_download_bytes:
            self._media_status(media, "remote", prefetch)
        if prefetch:
            return
        if media["local_path"]:
            self._media_status(media, "ready", prefetch)
        if (
            owner["vault_chat_id"]
            and not media["vault_message_id"]
            and not media["protected"]
            and not media["ephemeral"]
        ):
            if current := self.repository.media(owner["owner_id"], media["id"]):
                await self._copy_to_vault(task, owner, current)

    def _archive_download_budget(self, size: int, prefetch: bool) -> int:
        stored = self.repository.db.execute(
            """SELECT (SELECT coalesce(sum(size),0) FROM archive_media WHERE local_path IS NOT NULL)
            + (SELECT coalesce(sum(size),0) FROM archive_media_cache)"""
        ).fetchone()[0]
        free = shutil.disk_usage(self.root).free
        available = min(self.config.storage_bytes - stored, free - self.settings.min_free_bytes) - self._reserved_bytes
        required = size or self.config.media_download_bytes
        if prefetch or available >= required:
            return min(available, self.config.media_download_bytes)
        candidates = self.repository.db.execute(
            """SELECT a.owner_id,a.id,p.size FROM archive_media_cache p JOIN archive_media a ON a.id=p.media_id
            WHERE p.pinned=0 ORDER BY p.expires_at,p.media_id"""
        ).fetchall()
        for row in candidates:
            if row["id"] in self._media_in_flight:
                continue
            try:
                self._discard_prefetch(row)
            except OSError:
                logger.warning("archive_cache eviction_failed media=%s", row["id"], exc_info=True)
                continue
            stored -= row["size"]
            free = shutil.disk_usage(self.root).free
            available = (
                min(self.config.storage_bytes - stored, free - self.settings.min_free_bytes) - self._reserved_bytes
            )
            if available >= required:
                break
        return min(available, self.config.media_download_bytes)

    def _media_path(self, media: dict, parent: Path) -> Path:
        relative = parent.relative_to(self.root) / str(media["owner_id"]) / media["id"]
        return self.safe_path(relative, parent)

    def _store_archive_file(self, media: dict, source: Path) -> None:
        target = self._media_path(media, self.blobs)
        size = source.stat().st_size
        if source != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
        with self.repository.db:
            self.repository.db.execute(
                "UPDATE archive_media SET local_path=?,size=?,status='ready' WHERE owner_id=? AND id=?",
                (target.relative_to(self.root).as_posix(), size, media["owner_id"], media["id"]),
            )
            self.repository.db.execute("DELETE FROM archive_media_cache WHERE media_id=?", (media["id"],))

    def _promote_prefetched(self, media: dict) -> None:
        cached = self._media_path(media, self.prefetch_cache)
        # Recover a move interrupted between the file rename and the database commit.
        source = cached if cached.is_file() else self._media_path(media, self.blobs)
        if source.is_file():
            self._store_archive_file(media, source)
        else:
            with self.repository.db:
                self.repository.db.execute("DELETE FROM archive_media_cache WHERE media_id=?", (media["id"],))

    def _discard_prefetch(self, media: dict) -> None:
        self._media_path(media, self.prefetch_cache).unlink(missing_ok=True)
        with self.repository.db:
            self.repository.db.execute("DELETE FROM archive_media_cache WHERE media_id=?", (media["id"],))

    def _migrate_legacy_prefetch(self) -> None:
        rows = self.repository.db.execute(
            "SELECT id,owner_id,local_path FROM archive_media WHERE status='staged' AND local_path IS NOT NULL"
        ).fetchall()
        now = time.time()
        for media in rows:
            legacy = self.safe_path(media["local_path"], self.blobs)
            cached = self._media_path(media, self.prefetch_cache)
            source = legacy if legacy.is_file() else cached
            with self.repository.db:
                if source.is_file():
                    file = source.stat()
                    expires = file.st_mtime + PREFETCH_TTL
                    if expires > now:
                        if source != cached:
                            cached.parent.mkdir(parents=True, exist_ok=True)
                            source.replace(cached)
                        self.repository.db.execute(
                            "INSERT OR IGNORE INTO archive_media_cache(media_id,size,expires_at) VALUES (?,?,?)",
                            (media["id"], file.st_size, expires),
                        )
                    else:
                        source.unlink(missing_ok=True)
                self.repository.db.execute("UPDATE archive_media SET local_path=NULL WHERE id=?", (media["id"],))

    def _media_status(self, media: dict, status: str, prefetch: bool):
        if not prefetch:
            with self.repository.db:
                self.repository.db.execute(
                    "UPDATE archive_media SET status=? WHERE owner_id=? AND id=?",
                    (status, media["owner_id"], media["id"]),
                )

    async def _copy_to_vault(self, task: dict, owner: dict, media: dict):
        vault = await self.bot.get_chat(owner["vault_chat_id"])
        member = await self.bot.get_chat_member(owner["vault_chat_id"], owner["owner_id"])
        if (
            vault.username
            or member.status not in {"creator", "administrator"}
            or await self.bot.get_chat_member_count(owner["vault_chat_id"]) > 2
        ):
            raise MediaError(T.ARCHIVE_SERVICE_VAULT_NOT_PRIVATE, code="vault_not_private")
        topic_id = media["topic_id"]
        if topic_id is None:
            topic = await self.bot.create_forum_topic(owner["vault_chat_id"], name=media["chat_title"][:128])
            topic_id = topic.message_thread_id
            with self.repository.db:
                self.repository.db.execute(
                    "UPDATE archive_chats SET topic_id=? WHERE owner_id=? AND id=?",
                    (topic_id, owner["owner_id"], media["chat_id"]),
                )
        message = self.repository.message(owner["owner_id"], media["message_id"])
        current = self.repository.media(owner["owner_id"], media["id"])
        if message is None or current is None or current["protected"] or current["ephemeral"]:
            return
        caption = self._copy_caption(message)
        self.repository.task_status(task["id"], "sending")
        sent = await self._send_file(owner["vault_chat_id"], current, caption=caption, topic_id=topic_id)
        with self.repository.db:
            saved = self.repository.db.execute(
                "UPDATE archive_media SET vault_message_id=? WHERE owner_id=? AND id=?",
                (sent.message_id, owner["owner_id"], media["id"]),
            )
            self.repository.db.execute(
                """INSERT OR IGNORE INTO archive_vault_copies(owner_id,media_id,chat_id,telegram_id,state)
                VALUES (?,?,?,?,?)""",
                (
                    owner["owner_id"],
                    media["id"],
                    owner["vault_chat_id"],
                    sent.message_id,
                    "live" if saved.rowcount else "pending",
                ),
            )

    @staticmethod
    def _copy_caption(message: dict) -> str:
        label = (
            T.ARCHIVE_SERVICE_COPY_DELETED
            if message["deleted_at"]
            else T.ARCHIVE_SERVICE_COPY_EDITED
            if message["edited_at"]
            else T.ARCHIVE_SERVICE_COPY_SAVED
        )
        return f"{label} · {message['chat_title']}\n{message['body']}"[:1000]

    async def _send_file(
        self, chat_id: int, media: dict, *, caption: str = "", topic_id=None, business_id=None, reply_to=None
    ):
        options = {"chat_id": chat_id, "message_thread_id": topic_id}
        if business_id:
            options["business_connection_id"] = business_id
        if reply_to:
            from aiogram.types import ReplyParameters

            options["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        kind, value = media["kind"], media["file_id"]
        local = self.safe_path(media["local_path"], self.blobs) if media["local_path"] else None
        if kind == "live_photo":
            if not local or not local.is_file():
                raise MediaError(T.ARCHIVE_WEB_REMOTE_MEDIA, code="live_photo_unavailable")
            kind, value = "video", FSInputFile(local, filename=media["filename"])
        elif media["ephemeral"] and local is not None:
            value = FSInputFile(local, filename=media["filename"])
        if kind not in {"sticker", "video_note"}:
            options["caption"] = caption
        send = getattr(self.bot, f"send_{kind}")
        try:
            return await send(**options, **{kind: value})
        except TelegramBadRequest as error:
            if (
                not local
                or not local.is_file()
                or not isinstance(value, str)
                or not any(text in error.message.lower() for text in ("file identifier", "file reference"))
            ):
                raise
            return await send(**options, **{kind: FSInputFile(local, filename=media["filename"])})

    async def _sync_copies(self, task: dict, owner: dict):
        if not owner["vault_chat_id"]:
            return
        message = self.repository.message(owner["owner_id"], task["payload"]["message_id"])
        if not message:
            return
        for media in message["media"]:
            if media["vault_message_id"] and media["kind"] not in {"sticker", "video_note"}:
                try:
                    await self.bot.edit_message_caption(
                        chat_id=owner["vault_chat_id"],
                        message_id=media["vault_message_id"],
                        caption=self._copy_caption(message),
                    )
                except TelegramBadRequest:
                    pass

    async def _read(self, task: dict, owner: dict):
        chat = self.repository.chat(owner["owner_id"], task["payload"]["chat_id"])
        if (
            not chat
            or owner["read_mode"] == "none"
            or not self.settings.business_enabled
            or (self.settings.business_chats and chat["telegram_id"] not in self.settings.business_chats)
        ):
            return
        if owner["read_mode"] == "selected" and not chat["mark_read"]:
            return
        connection = self.repository.connection(owner["owner_id"], chat["connection_id"])
        if connection and connection["enabled"] and connection["rights"].get("can_read_messages"):
            await self.bot.read_business_message(
                business_connection_id=chat["connection_id"],
                chat_id=chat["telegram_id"],
                message_id=task["payload"]["telegram_id"],
            )

    async def _send_media(self, task: dict, owner: dict):
        media = self.repository.media(owner["owner_id"], task["payload"]["media_id"])
        if not media:
            raise LookupError("Media not found")
        self.check_media_send(media)
        self.repository.task_status(task["id"], "sending")
        await self._send_file(owner["owner_id"], media, caption=f"{media['chat_title']} · {media['filename']}"[:1000])

    def check_media_send(self, media: dict) -> None:
        if media["protected"]:
            raise MediaError(T.ARCHIVE_WEB_PROTECTED_MEDIA, code="protected_media")
        if media["ephemeral"] and (
            not media["local_path"] or not self.safe_path(media["local_path"], self.blobs).is_file()
        ):
            raise MediaError(T.ARCHIVE_WEB_EPHEMERAL_COPY_MISSING, code="ephemeral_copy_missing")

    async def _export(self, task: dict, owner: dict) -> dict:
        owner_id = owner["owner_id"]
        folder = self.exports / str(owner_id) / uuid.uuid4().hex
        format_name = task["payload"].get("format", "json")
        chat_id = task["payload"].get("chat_id", 0)
        limit = self.settings.max_upload_bytes
        stopped = self._active_exports[owner_id] = Event()
        try:
            files = await write_export(
                self.repository,
                owner_id,
                folder,
                format_name=format_name,
                chat_id=chat_id,
                max_part_bytes=min(10_000_000, limit - 131_072),
            )
            rows = self.repository.db.execute(
                f"""SELECT a.id,a.filename,a.kind,a.status,a.local_path,c.telegram_id AS chat_id,
                m.telegram_id AS message_id FROM archive_media a JOIN archive_messages m ON m.id=a.message_id
                JOIN archive_chats c ON m.chat_id=c.id
                WHERE a.owner_id=? AND {RETAINED_MESSAGE} AND (?=0 OR m.chat_id=?) ORDER BY m.id,a.rowid""",
                (owner_id, chat_id, chat_id),
            ).fetchall()
            attachments = []
            for row in rows:
                item = dict(row)
                relative = item.pop("local_path")
                path = self.safe_path(relative, self.blobs) if relative else None
                item["path"] = path if path and path.is_file() else None
                item["status"] = "included" if item["path"] else "missing_local_file" if path else row["status"]
                attachments.append(item)
            packing = asyncio.create_task(asyncio.to_thread(bundle_export, files, attachments, limit, stopped))
            try:
                files = await asyncio.shield(packing)
            except asyncio.CancelledError:
                stopped.set()
                await asyncio.gather(packing, return_exceptions=True)
                raise
            for index, path in enumerate(files):
                if stopped.is_set():
                    raise MediaError(T.ARCHIVE_SERVICE_EXPORT_INVALIDATED, code="export_invalidated")
                self.repository.task_status(task["id"], "sending")
                try:
                    await self.bot.send_document(
                        owner_id,
                        FSInputFile(path),
                        caption=f"ThreadDock · ZIP · {index + 1}/{len(files)}",
                    )
                except TelegramRetryAfter:
                    if index:
                        raise MediaError(T.ARCHIVE_SERVICE_EXPORT_INCOMPLETE, code="export_incomplete") from None
                    raise
                with self.repository.db:
                    self.repository.db.execute(
                        "UPDATE archive_tasks SET progress=? WHERE id=?", (index + 1, task["id"])
                    )
            return {"parts": len(files)}
        finally:
            self._active_exports.pop(owner_id)
            if folder.exists():
                remove_directory(folder, self.exports)

    def invalidate_export(self, owner_id: int):
        if active := self._active_exports.get(owner_id):
            active.set()

    def safe_path(self, relative: str, parent: Path | None = None) -> Path:
        parent = parent or self.root
        original = self.root / relative
        path = original.resolve()
        if not path.is_relative_to(parent.resolve()) or path == parent.resolve() or original.is_symlink():
            raise ValueError("Invalid archive path")
        return path

    def cleanup_owner_blobs(self, owner_id: int):
        folder = self.blobs / str(owner_id)
        known = {
            row[0]
            for row in self.repository.db.execute(
                "SELECT local_path FROM archive_media WHERE owner_id=? AND local_path IS NOT NULL", (owner_id,)
            )
        }
        for path in folder.glob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and path.relative_to(self.root).as_posix() not in known
                and path.stem not in self._media_in_flight
            ):
                self.safe_path(path.relative_to(self.root).as_posix(), folder).unlink()
        self._clean_prefetch_orphans(owner_id)

    def _clean_prefetch_orphans(self, owner_id: int | None = None) -> None:
        query = """SELECT a.owner_id,a.id FROM archive_media_cache p JOIN archive_media a ON a.id=p.media_id
        WHERE (? IS NULL OR a.owner_id=?)"""
        known = {(str(row["owner_id"]), row["id"]) for row in self.repository.db.execute(query, (owner_id, owner_id))}
        paths = self.prefetch_cache.glob("*/*") if owner_id is None else (self.prefetch_cache / str(owner_id)).glob("*")
        for path in paths:
            if (
                not path.is_symlink()
                and path.is_file()
                and path.stem not in self._media_in_flight
                and (path.parent.name, path.name) not in known
            ):
                self.safe_path(path.relative_to(self.root), self.prefetch_cache).unlink(missing_ok=True)

    def _maintain_prefetch_cache(self, now: float, *, reconcile: bool = False) -> None:
        rows = self.repository.db.execute(
            """SELECT a.owner_id,a.id,p.pinned,p.expires_at FROM archive_media_cache p
            JOIN archive_media a ON a.id=p.media_id WHERE p.pinned=1 OR p.expires_at<=? OR ?""",
            (now, reconcile),
        ).fetchall()
        for row in rows:
            if row["id"] in self._media_in_flight:
                continue
            try:
                if row["pinned"] and (media := self.repository.media(row["owner_id"], row["id"])):
                    self._promote_prefetched(media)
                elif row["expires_at"] <= now or (
                    reconcile and not self._media_path(row, self.prefetch_cache).is_file()
                ):
                    self._discard_prefetch(row)
            except OSError:
                logger.warning("archive_cache maintenance_failed media=%s", row["id"], exc_info=True)
        self._clean_prefetch_orphans()

    async def bind_vault(self, owner_id: int, chat_id: int):
        chat = await self.bot.get_chat(chat_id)
        owner_member = await self.bot.get_chat_member(chat_id, owner_id)
        bot_member = await self.bot.get_chat_member(chat_id, self.bot.id)
        if (
            chat.type != "supergroup"
            or not chat.is_forum
            or chat.username
            or owner_member.status not in {"creator", "administrator"}
            or bot_member.status != "administrator"
            or not bot_member.can_manage_topics
            or await self.bot.get_chat_member_count(chat_id) > 2
        ):
            raise ValueError(T.ARCHIVE_SERVICE_VAULT_REQUIREMENTS)
        if self.repository.owner(owner_id)["vault_chat_id"] == chat_id:
            return
        with self.repository.db:
            self.repository.db.execute(
                "UPDATE archive_owners SET vault_chat_id=? WHERE owner_id=?", (chat_id, owner_id)
            )
            self.repository.db.execute("UPDATE archive_chats SET topic_id=NULL WHERE owner_id=?", (owner_id,))
            self.repository.db.execute("UPDATE archive_media SET vault_message_id=NULL WHERE owner_id=?", (owner_id,))
            for media in self.repository.db.execute(
                "SELECT id FROM archive_media WHERE owner_id=? AND protected=0 AND ephemeral=0", (owner_id,)
            ).fetchall():
                self.repository._enqueue(owner_id, "media", {"media_id": media[0]}, f"vault:{chat_id}:{media[0]}")

    async def _vault_prompt(self, task: dict, owner: dict):
        from aiogram.types import (
            ChatAdministratorRights,
            KeyboardButton,
            KeyboardButtonRequestChat,
            ReplyKeyboardMarkup,
        )

        rights = ChatAdministratorRights(
            is_anonymous=False,
            can_manage_chat=True,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_post_stories=False,
            can_edit_stories=False,
            can_delete_stories=False,
            can_manage_topics=True,
        )
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text=T.ARCHIVE_SERVICE_SELECT_VAULT,
                        request_chat=KeyboardButtonRequestChat(
                            request_id=8142,
                            chat_is_channel=False,
                            chat_is_forum=True,
                            bot_is_member=True,
                            user_administrator_rights=rights,
                            bot_administrator_rights=rights,
                            request_title=True,
                        ),
                    )
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        with self.repository.db:
            self.repository.db.execute(
                "INSERT OR REPLACE INTO archive_vault_requests VALUES (?,?)", (owner["owner_id"], time.time() + 300)
            )
        self.repository.task_status(task["id"], "sending")
        await self.bot.send_message(owner["owner_id"], T.ARCHIVE_SERVICE_VAULT_INSTRUCTIONS, reply_markup=keyboard)

    def maintain(self):
        now = time.time()
        with self.repository.db:
            self.repository.db.execute("DELETE FROM archive_sessions WHERE expires_at<?", (now,))
            self.repository.db.execute("DELETE FROM archive_vault_requests WHERE expires_at<?", (now,))
            for owner in self.repository.db.execute(
                "SELECT owner_id,retention_days,auto_delete_disconnected FROM archive_owners"
            ).fetchall():
                if owner["retention_days"]:
                    deleted = self.repository.expire_messages(owner["owner_id"], now - owner["retention_days"] * 86400)
                    if deleted:
                        self.invalidate_export(owner["owner_id"])
                if owner["auto_delete_disconnected"]:
                    deleted = self.repository.clear_disconnected(owner["owner_id"], now - 7 * 86400)
                    if deleted:
                        self.invalidate_export(owner["owner_id"])
            self.repository.db.execute(
                """DELETE FROM archive_tasks AS t
                WHERE status IN ('done','failed','uncertain','superseded') AND updated_at<?
                AND NOT (t.status='uncertain' AND t.kind='send_media' AND EXISTS (
                    SELECT 1 FROM archive_media a WHERE a.owner_id=t.owner_id AND a.id=json_extract(t.payload,'$.media_id')
                ))""",
                (now - 7 * 86400,),
            )
        self._maintain_prefetch_cache(now)
        known = {
            row[0]
            for row in self.repository.db.execute("SELECT local_path FROM archive_media WHERE local_path IS NOT NULL")
        }
        for path in self.blobs.glob("*/*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and path.relative_to(self.root).as_posix() not in known
                and path.stat().st_mtime < now - 3600
            ):
                path.unlink()
        for parent in (self.exports, self.uploads):
            for path in parent.glob("*/*"):
                if path.stat().st_mtime < now - 86400:
                    if path.is_dir():
                        remove_directory(path, parent)
                    elif not path.is_symlink():
                        path.unlink()
