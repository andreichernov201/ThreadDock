"""Downloader sender policy, shared by admission and delivery."""

from aiogram.exceptions import TelegramAPIError

from core.models import Destination


async def sender_permitted(bot, database, destination: Destination) -> bool:
    if destination.business_id:
        info = database.connection_info(destination.business_id)
        if not info or not info["enabled"] or not info["can_reply"]:
            return False
        owner = info["owner_id"]
        own = destination.requester_id == owner
        rule = database.preferences("user", owner)["senders"]
        if (rule == "self" and not own) or (rule == "peer" and own):
            return False
        # Two connections to this same bot must not both process the sender's
        # outgoing link when that sender explicitly chose to handle only their own.
        if not own and database.handles_own_links(destination.requester_id, owner):
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
