from aiogram import F, Router
from aiogram.types import Message

from core import texts as T
from core.errors import MediaError
from services.application import Application

router = Router()


@router.message(F.text | F.caption)
@router.business_message()
async def handle_links(message: Message, app: Application) -> None:
    destination = await app.destination(message)
    if destination is None:
        return
    text = message.text or message.caption or ""
    if text.startswith("/"):
        return
    entities = message.entities if message.text else message.caption_entities
    try:
        count, errors = app.submit(destination, text, entities or ())
    except MediaError as error:
        await app.delivery.text(destination, str(error))
        return
    # No acknowledgements or unsupported-site chatter in groups and managed chats.
    if message.chat.type == "private" and not destination.business_id:
        lines = ([T.HANDLERS_LINKS_LINKS_ACCEPTED.format(count=count)] if count else []) + errors
        if lines:
            await app.delivery.text(destination, "\n".join(lines))
