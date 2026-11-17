import logging

from aiogram import F, Router
from aiogram.types import Message

from core.errors import MediaError
from core.messages import has_media
from services.application import Application

router = Router()
logger = logging.getLogger(__name__)


@router.message(F.text | F.caption)
@router.business_message()
async def handle_links(message: Message, app: Application) -> None:
    destination = await app.destination(message)
    if destination is None:
        return
    if has_media(message):
        logger.info(
            "download_skip reason=existing_media chat=%s message=%s business=%s forwarded=%s",
            message.chat.id,
            message.message_id,
            bool(destination.business_id),
            bool(message.forward_origin),
        )
        return
    text = message.text or message.caption or ""
    if text.startswith("/"):
        return
    entities = message.entities if message.text else message.caption_entities
    try:
        _, errors = app.submit(destination, text, entities or ())
    except MediaError as error:
        await app.delivery.text(destination, str(error))
        return
    # No acknowledgements or unsupported-site chatter in groups and managed chats.
    if message.chat.type == "private" and not destination.business_id:
        if errors:
            await app.delivery.text(destination, "\n".join(errors))
