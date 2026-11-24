"""Composition of use cases. Telegram handlers only validate context and submit jobs."""

import hashlib
import json
import logging

from aiogram import Bot
from aiogram.types import BusinessConnection, Message

from archive.service import ArchiveService
from config import Settings
from core import texts as T
from core.models import Destination, RenderOptions
from services.access import sender_permitted
from services.delivery import TelegramDelivery
from services.http import PublicHttp
from services.jobs import JobQueue
from services.links import LinkError, extract_urls, normalize_url
from services.pipeline import MediaPipeline
from storage.database import Database

logger = logging.getLogger(__name__)


class Application:
    def __init__(self, settings: Settings, database: Database, bot: Bot, http: PublicHttp):
        self.settings, self.database, self.bot = settings, database, bot
        self.pipeline = MediaPipeline(settings, database, http)
        self.delivery = TelegramDelivery(bot, database, settings)
        self.queue = JobQueue(settings, database, self.pipeline, self.delivery)
        self.archive = ArchiveService(settings, database, bot)
        self.accepting = True

    def connection_changed(self, connection: BusinessConnection) -> None:
        self.database.save_connection(
            connection.id,
            connection.user.id,
            connection.is_enabled,
            bool(connection.rights and connection.rights.can_reply),
        )
        self.archive.connection_changed(connection)

    async def destination(self, message: Message) -> Destination | None:
        if not message.from_user or message.from_user.is_bot or message.sender_business_bot:
            return None
        identifier = message.business_connection_id or ""
        if identifier:
            if not self.settings.business_enabled or (
                self.settings.business_chats and message.chat.id not in self.settings.business_chats
            ):
                return None
            info = self.database.connection_info(identifier)
            if info is None:
                self.connection_changed(await self.bot.get_business_connection(identifier))
                info = self.database.connection_info(identifier)
            if not info or not info["enabled"] or not info["can_reply"]:
                return None
            if info["owner_id"] != message.from_user.id:
                self.database.touch_business_chat(identifier, message.chat.id, message.date.timestamp())
            else:
                # Outgoing messages do not extend Telegram's incoming-message window.
                reply = message.reply_to_message
                if reply and reply.from_user and reply.from_user.id != info["owner_id"] and not reply.from_user.is_bot:
                    self.database.touch_business_chat(identifier, message.chat.id, reply.date.timestamp())
                if not self.database.business_can_send(identifier, message.chat.id):
                    return None
        elif message.chat.type in {"group", "supergroup"}:
            if not self.database.group_enabled(message.chat.id, self.settings.groups_enabled):
                return None
        elif message.chat.type != "private":
            return None
        destination = Destination(
            message.chat.id,
            message.message_id,
            message.from_user.id,
            message.message_thread_id,
            identifier,
            int(message.date.timestamp()),
        )
        return destination if await sender_permitted(self.bot, self.database, destination) else None

    def submit(self, destination: Destination, text: str, entities=()) -> tuple[int, list[str]]:
        errors: list[str] = []
        requests = []
        seen: set[str] = set()
        connection = self.database.connection_info(destination.business_id) if destination.business_id else None
        scope, subject = (
            ("group", destination.chat_id)
            if destination.chat_id < 0
            else ("user", connection["owner_id"] if connection else destination.requester_id)
        )
        prefs = self.database.preferences(scope, subject)
        render = RenderOptions(prefs["photo_seconds"], prefs["live_limit"])
        for url in extract_urls(text, entities):
            try:
                link = normalize_url(url)
            except LinkError as error:
                # Unsupported links are ordinary chat content, not download requests.
                if str(error) != T.SERVICES_LINKS_UNSUPPORTED_SITE:
                    errors.append(str(error))
                continue
            if link.cache_key not in seen:
                seen.add(link.cache_key)
                request_key = (
                    f"message:{destination.business_id}:{destination.chat_id}:{destination.message_id}:{link.cache_key}"
                )
                requests.append((request_key, link.url, render.cache_key(link.cache_key)))
        if not self.accepting:
            return 0, [T.SERVICES_APPLICATION_RESTARTING]
        mode = "album" if prefs["mode"] == "photos" else "video"
        event = ""
        if connection and destination.sent_at:
            # The two Business accounts have different message IDs. Match the
            # sender, Telegram timestamp and payload; store only a digest.
            event = hashlib.sha256(
                json.dumps(
                    [destination.requester_id, destination.sent_at, text, [url for _, url, _ in requests]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        count = (
            self.database.enqueue(
                requests,
                destination,
                mode,
                self.settings.queue_capacity,
                render,
                business_event=event,
            )
            if requests
            else 0
        )
        if requests:
            logger.info(
                "download_submit chat=%s message=%s business=%s links=%s queued=%s suppressed=%s",
                destination.chat_id,
                destination.message_id,
                bool(destination.business_id),
                len(requests),
                count,
                len(requests) - count,
            )
        return count, list(dict.fromkeys(errors))
