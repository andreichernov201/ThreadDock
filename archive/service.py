"""Archive ingestion and a durable outbox, independent of downloader jobs."""

import asyncio
import html
import json
import logging
import shutil
import time
import uuid
from pathlib import Path

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from archive.config import ArchiveSettings
from archive.content import message_payload
from archive.exports import write_export
from archive.repository import ArchiveRepository
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
        for path in (self.blobs, self.exports, self.uploads):
            path.mkdir(parents=True, exist_ok=True)
        self.stopping = asyncio.Event()
        self.task: asyncio.Task | None = None
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_maintenance = 0.0

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

    async def capture(self, message: Message, *, manual_reply: bool = False) -> None:
        if (
            not message.business_connection_id
            or message.chat.type != "private"
            or not self.settings.business_enabled
            or (self.settings.business_chats and message.chat.id not in self.settings.business_chats)
        ):
            return
        if message.sender_business_bot and not manual_reply:
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
        for item in [message.reply_to_message, message] if message.reply_to_message else [message]:
            if item is None or item.chat.id != message.chat.id or (item.sender_business_bot and not manual_reply):
                continue
            self.repository.ingest(
                owner_id=owner_id,
                connection_id=message.business_connection_id,
                source_chat_id=message.chat.id,
                title=title[:250],
                username=message.chat.username or "",
                telegram_id=item.message_id,
                sent_at=item.date.timestamp(),
                changed_at=(item.edit_date or item.date).timestamp(),
                outgoing=manual_reply or bool(item.from_user and item.from_user.id == owner_id),
                sender_id=item.from_user.id if item.from_user else None,
                media_group=item.media_group_id,
                payload=message_payload(item),
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
                info["owner_id"], event.business_connection_id, event.chat.id, event.message_ids, title
            )

    def web_button(self, text: str = T.ARCHIVE_SERVICE_OPEN_ARCHIVE, *, chat_id: int = 0):
        if not self.config.public_url:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=text,
                        web_app=WebAppInfo(url=self.config.public_url + (f"/?chat={chat_id}" if chat_id else "/")),
                    )
                ]
            ]
        )

    def start(self) -> None:
        self.repository.recover()
        self.task = asyncio.create_task(self._worker(), name="archive-outbox")

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

    async def _worker(self):
        while not self.stopping.is_set():
            if time.time() - self._last_maintenance > 60:
                self.maintain()
                self._last_maintenance = time.time()
            task = self.repository.claim()
            if task is None:
                await self._idle(0.5)
                continue
            async with self.owner_lock(task["owner_id"]):
                await self._process(task)

    async def _process(self, task: dict):
        identifier, owner_id = task["id"], task["owner_id"]
        try:
            owner = self.repository.owner(owner_id)
            if owner is None:
                return
            if (
                (not owner["enabled"] or owner["paused"])
                and task["kind"] in {"notify", "read", "sync_copies", "media"}
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
                elif task["kind"] == "media":
                    await self._save_media(task, owner)
                elif task["kind"] == "sync_copies":
                    await self._sync_copies(task, owner)
                elif task["kind"] == "read":
                    await self._read(task, owner)
                elif task["kind"] == "send_media":
                    await self._send_media(task, owner)
                elif task["kind"] == "export":
                    result = await self._export(task, owner)
                elif task["kind"] == "reply":
                    result = await self._reply(task, owner)
                elif task["kind"] == "vault_prompt":
                    await self._vault_prompt(task, owner)
                else:
                    raise ValueError("Unknown archive task")
            self.repository.task_status(identifier, "done", result=result)
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
        except TelegramNetworkError, TelegramServerError, TimeoutError:
            current = self.repository.task(owner_id, identifier)
            status = "uncertain" if current and current["status"] in {"sending", "uncertain"} else "pending"
            if status == "pending" and task["attempts"] >= 3:
                status = "failed"
            self.repository.task_status(identifier, status, code="telegram_network", delay=5 * task["attempts"])
        except Exception as error:
            code = error.code if isinstance(error, MediaError) else type(error).__name__
            self.repository.task_status(identifier, "failed", code=code)
            self.repository.record_error(code)
            logger.warning("archive_task=%s code=%s", identifier, code)

    async def _notify(self, task: dict, owner: dict):
        if not owner["notify_enabled"] or owner["paused"]:
            return
        event_ids = task["payload"]["event_ids"]
        rows = [
            self.repository.db.execute(
                "SELECT e.*,c.title,c.muted,COALESCE(v.body,m.body) AS body FROM archive_events e JOIN archive_chats c ON c.id=e.chat_id LEFT JOIN archive_messages m ON m.id=e.message_id LEFT JOIN archive_versions v ON v.id=e.version_id AND v.owner_id=e.owner_id WHERE e.owner_id=? AND e.id=?",
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
        }
        title = (
            labels.get(rows[0]["kind"], T.ARCHIVE_SERVICE_ARCHIVE_EVENT)
            if len(rows) == 1
            else T.ARCHIVE_SERVICE_DELETED_COUNT.format(value1=len(rows))
        )
        text = T.ARCHIVE_SERVICE_NOTIFICATION_HEADER.format(
            title=html.escape(title), chat=html.escape(rows[0]["title"])
        )
        if not owner["hide_content"]:
            previews = [
                html.escape((row["body"] or "").strip()[:500] or T.ARCHIVE_SERVICE_EMPTY_PREVIEW) for row in rows[:4]
            ]
            text += "\n\n" + "\n\n".join(previews)
        if len(rows) == 1 and rows[0]["kind"] == "keyword":
            text += "\n" + html.escape(", ".join(json.loads(rows[0]["payload"])["matches"]))
        buttons = []
        if self.config.public_url:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=T.ARCHIVE_SERVICE_OPEN_CHAT,
                        web_app=WebAppInfo(url=f"{self.config.public_url}/?chat={rows[0]['chat_id']}"),
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
        except TelegramNetworkError, TelegramServerError, TimeoutError, asyncio.CancelledError:
            for job_id in batch_ids:
                self.repository.task_status(job_id, "uncertain", code="notification_uncertain")
            raise
        except TelegramAPIError:
            for job_id in batch_ids:
                self.repository.task_status(job_id, "failed", code="notification_rejected")
            raise
        for job_id in batch_ids[1:]:
            self.repository.task_status(job_id, "done")

    async def _save_media(self, task: dict, owner: dict):
        media = self.repository.media(owner["owner_id"], task["payload"]["media_id"])
        if not media or media["protected"] or media["kind"] not in owner["media_types"]:
            return
        if media["size"] <= self.config.media_download_bytes and not media["local_path"]:
            stored_bytes = self.repository.db.execute(
                "SELECT coalesce(sum(size),0) FROM archive_media WHERE status='ready'"
            ).fetchone()[0]
            free = shutil.disk_usage(self.root).free
            if (
                stored_bytes + media["size"] < self.config.storage_bytes
                and free - media["size"] >= self.settings.min_free_bytes
            ):
                folder = self.blobs / str(owner["owner_id"])
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / media["id"]
                partial = path.with_suffix(".part")
                try:
                    with partial.open("wb") as output:
                        await self.bot.download(
                            media["file_id"],
                            destination=LimitedWriter(
                                output, min(self.config.media_download_bytes, self.config.storage_bytes - stored_bytes)
                            ),
                            timeout=60,
                        )
                    partial.replace(path)
                    with self.repository.db:
                        self.repository.db.execute(
                            "UPDATE archive_media SET local_path=?,size=?,status='ready' WHERE owner_id=? AND id=?",
                            (
                                path.relative_to(self.root).as_posix(),
                                path.stat().st_size,
                                owner["owner_id"],
                                media["id"],
                            ),
                        )
                except TelegramAPIError, MediaError, OSError:
                    partial.unlink(missing_ok=True)
                    with self.repository.db:
                        self.repository.db.execute(
                            "UPDATE archive_media SET status='remote' WHERE owner_id=? AND id=?",
                            (owner["owner_id"], media["id"]),
                        )
                finally:
                    partial.unlink(missing_ok=True)
            else:
                with self.repository.db:
                    self.repository.db.execute(
                        "UPDATE archive_media SET status='storage_full' WHERE owner_id=? AND id=?",
                        (owner["owner_id"], media["id"]),
                    )
        elif media["size"] > self.config.media_download_bytes:
            with self.repository.db:
                self.repository.db.execute(
                    "UPDATE archive_media SET status='remote' WHERE owner_id=? AND id=?",
                    (owner["owner_id"], media["id"]),
                )
        if owner["vault_chat_id"] and not media["vault_message_id"]:
            await self._copy_to_vault(task, owner, media)

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
        caption = self._copy_caption(message)
        self.repository.task_status(task["id"], "sending")
        sent = await self._send_file(owner["vault_chat_id"], media, caption=caption, topic_id=topic_id)
        with self.repository.db:
            self.repository.db.execute(
                "UPDATE archive_media SET vault_message_id=? WHERE owner_id=? AND id=?",
                (sent.message_id, owner["owner_id"], media["id"]),
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
        kind = media["kind"]
        value = media["file_id"]
        if kind not in {"sticker", "video_note"}:
            options["caption"] = caption
        return await getattr(self.bot, f"send_{kind}")(**options, **{kind: value})

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
        if not media or media["protected"]:
            raise MediaError(T.ARCHIVE_SERVICE_PROTECTED_MEDIA, code="protected_media")
        self.repository.task_status(task["id"], "sending")
        await self._send_file(owner["owner_id"], media, caption=f"{media['chat_title']} · {media['filename']}"[:1000])

    async def _export(self, task: dict, owner: dict) -> dict:
        folder = self.exports / str(owner["owner_id"]) / uuid.uuid4().hex
        format_name = task["payload"].get("format", "json")
        files = await write_export(
            self.repository,
            owner["owner_id"],
            folder,
            format_name=format_name,
            chat_id=task["payload"].get("chat_id", 0),
            max_part_bytes=min(10_000_000, self.settings.max_upload_bytes - 10000),
        )
        try:
            progress = task["progress"]
            for index, path in enumerate(files):
                if index < progress:
                    continue
                self.repository.task_status(task["id"], "sending")
                await self.bot.send_document(
                    owner["owner_id"],
                    FSInputFile(path),
                    caption=f"ThreadDock · {format_name.upper()} · {index + 1}/{len(files)}",
                )
                with self.repository.db:
                    self.repository.db.execute(
                        "UPDATE archive_tasks SET progress=? WHERE id=?", (index + 1, task["id"])
                    )
            return {"parts": len(files)}
        finally:
            remove_directory(folder, self.exports)

    async def _reply(self, task: dict, owner: dict) -> dict:
        payload = task["payload"]
        chat = self.repository.chat(owner["owner_id"], payload["chat_id"])
        if (
            not chat
            or not self.settings.business_enabled
            or (self.settings.business_chats and chat["telegram_id"] not in self.settings.business_chats)
            or not self.database.business_can_send(chat["connection_id"], chat["telegram_id"])
        ):
            raise MediaError(T.ARCHIVE_SERVICE_REPLY_NOT_ALLOWED, code="reply_not_allowed")
        from aiogram.types import ReplyParameters

        options = {"chat_id": chat["telegram_id"], "business_connection_id": chat["connection_id"]}
        if payload.get("reply_to"):
            target = self.repository.message(owner["owner_id"], payload["reply_to"])
            if not target or target["chat_id"] != chat["id"]:
                raise LookupError("Reply target not found")
            options["reply_parameters"] = ReplyParameters(
                message_id=target["telegram_id"], allow_sending_without_reply=True
            )
        self.repository.task_status(task["id"], "sending")
        if payload.get("upload"):
            path = self.safe_path(payload["upload"], self.uploads)
            try:
                sent = await self.bot.send_document(
                    **options, document=FSInputFile(path, filename=payload["filename"]), caption=payload.get("text", "")
                )
            finally:
                path.unlink(missing_ok=True)
        else:
            sent = await self.bot.send_message(**options, text=payload["text"])
        if not sent.business_connection_id:
            sent = sent.model_copy(update={"business_connection_id": chat["connection_id"]})
        await self.capture(sent, manual_reply=True)
        return {"message_id": sent.message_id}

    def safe_path(self, relative: str, parent: Path | None = None) -> Path:
        parent = parent or self.root
        original = self.root / relative
        path = original.resolve()
        if not path.is_relative_to(parent.resolve()) or path == parent.resolve() or original.is_symlink():
            raise ValueError("Invalid archive path")
        return path

    def cleanup_owner_blobs(self, owner_id: int):
        folder = self.blobs / str(owner_id)
        if not folder.is_dir():
            return
        known = {
            row[0]
            for row in self.repository.db.execute(
                "SELECT local_path FROM archive_media WHERE owner_id=? AND local_path IS NOT NULL", (owner_id,)
            )
        }
        for path in folder.iterdir():
            if path.is_file() and not path.is_symlink() and path.relative_to(self.root).as_posix() not in known:
                self.safe_path(path.relative_to(self.root).as_posix(), folder).unlink()

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
                "SELECT id FROM archive_media WHERE owner_id=? AND protected=0", (owner_id,)
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
                    self.repository.db.execute(
                        "DELETE FROM archive_messages WHERE owner_id=? AND favorite=0 AND sent_at<?",
                        (owner["owner_id"], now - owner["retention_days"] * 86400),
                    )
                if owner["auto_delete_disconnected"]:
                    self.repository.db.execute(
                        "DELETE FROM archive_chats WHERE owner_id=? AND connection_id IN (SELECT connection_id FROM archive_connection_details WHERE owner_id=? AND enabled=0 AND disconnected_at<?)",
                        (owner["owner_id"], owner["owner_id"], now - 7 * 86400),
                    )
            self.repository.db.execute(
                "DELETE FROM archive_tasks WHERE status IN ('done','failed','uncertain') AND updated_at<?",
                (now - 7 * 86400,),
            )
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
