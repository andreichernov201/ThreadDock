"""Downloader sender policy, shared by admission and delivery."""

from aiogram.exceptions import TelegramAPIError

from core.models import Destination


async def sender_permitted(bot, database, destination: Destination) -> bool:
    if destination.business_id:
        info = database.connection_info(destination.business_id)
        if not info or not info["enabled"] or not info["can_reply"]:
            return False
        return True
    if destination.chat_id < 0:
        rule = database.preferences("group", destination.chat_id)["senders"]
        if rule == "selected":
            return database.sender_allowed(destination.chat_id, destination.requester_id)
        if rule == "admins":
            try:
                member = await bot.get_chat_member(destination.chat_id, destination.requester_id)
            except TelegramAPIError:
                return False
            return member.status in {"creator", "administrator"}
    return True
