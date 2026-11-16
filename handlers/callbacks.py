from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from core import texts as T
from core.errors import MediaError
from core.models import Destination
from services.application import Application

router = Router()


@router.callback_query(F.data.startswith("m:"))
async def media_action(query: CallbackQuery, app: Application) -> None:
    parts = (query.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"video", "photos", "audio"}:
        await query.answer(T.HANDLERS_CALLBACKS_INVALID_BUTTON)
        return
    row = app.database.get_job(int(parts[1]))
    if row is None or not isinstance(query.message, Message):
        await query.answer(T.HANDLERS_CALLBACKS_JOB_EXPIRED, show_alert=True)
        return
    connection = app.database.connection_info(row["business_id"]) if row["business_id"] else None
    allowed = {row["requester_id"]}
    if connection:
        allowed.add(connection["owner_id"])
    if (
        query.from_user.id not in allowed
        or query.message.chat.id != row["chat_id"]
        or (query.message.business_connection_id or "") != row["business_id"]
    ):
        await query.answer(T.HANDLERS_CALLBACKS_SENDER_ONLY, show_alert=True)
        return
    destination = Destination(
        row["chat_id"], row["message_id"], query.from_user.id, row["thread_id"], row["business_id"]
    )
    if not app.accepting or not app.delivery.permitted(destination):
        await query.answer(T.HANDLERS_CALLBACKS_DELIVERY_UNAVAILABLE, show_alert=True)
        return
    try:
        outcome = app.database.request_media(row["id"], parts[2], app.settings.queue_capacity)
    except MediaError as error:
        await query.answer(str(error), show_alert=True)
        return
    answers = {
        "accepted": T.HANDLERS_CALLBACKS_QUEUED,
        "queued": T.HANDLERS_CALLBACKS_ALREADY_QUEUED,
        "running": T.HANDLERS_CALLBACKS_ALREADY_RUNNING,
        "sending": T.HANDLERS_CALLBACKS_ALREADY_RUNNING,
        "done": T.HANDLERS_CALLBACKS_ALREADY_DONE,
        "expired": T.HANDLERS_CALLBACKS_JOB_EXPIRED,
    }
    await query.answer(answers[outcome])
