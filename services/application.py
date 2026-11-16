"""Composition of use cases. Telegram handlers only validate context and submit jobs."""

from aiogram import Bot
from aiogram.types import BusinessConnection, Message

from archive.service import ArchiveService
from config import Settings
from core import texts as T
from core.models import Destination
from services.delivery import TelegramDelivery
from services.http import PublicHttp
from services.jobs import JobQueue
from services.links import LinkError, extract_urls, normalize_url
from services.pipeline import MediaPipeline
from storage.database import Database


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
        return Destination(
            message.chat.id, message.message_id, message.from_user.id, message.message_thread_id, identifier
        )

    def submit(self, destination: Destination, text: str, entities=()) -> tuple[int, list[str]]:
        errors: list[str] = []
        requests = []
        seen: set[str] = set()
        for url in extract_urls(text, entities):
            try:
                link = normalize_url(url)
            except LinkError as error:
                errors.append(str(error))
                continue
            if link.cache_key not in seen:
                seen.add(link.cache_key)
                request_key = (
                    f"message:{destination.business_id}:{destination.chat_id}:{destination.message_id}:{link.cache_key}"
                )
                requests.append((request_key, link.url, link.cache_key))
        if not self.accepting:
            return 0, [T.SERVICES_APPLICATION_RESTARTING]
        count = self.database.enqueue(requests, destination, "video", self.settings.queue_capacity) if requests else 0
        return count, list(dict.fromkeys(errors))
