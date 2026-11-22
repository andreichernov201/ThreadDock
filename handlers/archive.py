import time

from aiogram import F, Router
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters import Command
from aiogram.types import BusinessMessagesDeleted, CallbackQuery, Message, ReplyKeyboardRemove, Update

from archive.analytics import statistics_for
from archive.exports import FORMATS
from core import texts as T
from services.application import Application

router = Router()


@router.message(F.chat.type == "private", Command("optout", "optin"))
async def author_policy(message: Message, app: Application):
    if not message.from_user or message.from_user.is_bot:
        return
    excluded = (message.text or "").split()[0].split("@")[0] == "/optout"
    owners = app.archive.repository.set_author_optout(message.from_user.id, excluded)
    for owner_id in owners:
        app.archive.invalidate_export(owner_id)
    for owner_id in owners:
        async with app.archive.owner_lock(owner_id):
            app.archive.cleanup_owner_blobs(owner_id)
    await message.answer(T.ARCHIVE_AUTHOR_OPTOUT if excluded else T.ARCHIVE_AUTHOR_OPTIN)


@router.business_message()
async def archive_message(message: Message, app: Application, event_update: Update):
    await app.archive.capture(message, update_id=event_update.update_id)
    return UNHANDLED


@router.edited_business_message()
async def archive_edit(message: Message, app: Application, event_update: Update):
    await app.archive.capture(message, edited=True, update_id=event_update.update_id)


@router.deleted_business_messages()
async def archive_deleted(event: BusinessMessagesDeleted, app: Application):
    await app.archive.deleted(event)


@router.message(
    F.chat.type == "private", Command("archive", "archive_on", "pause", "mute", "stats", "export", "delete_me")
)
async def archive_command(message: Message, app: Application):
    if not message.from_user or message.from_user.is_bot:
        return
    repository = app.archive.repository
    owner = repository.ensure_owner(message.from_user.id, message.from_user.language_code or "ru")
    arguments = (message.text or "").split()
    command = arguments[0].split("@")[0]
    if owner["pin_hash"] and command != "/archive":
        await message.answer(T.HANDLERS_ARCHIVE_PIN_REQUIRED, reply_markup=app.archive.web_button())
        return
    if command == "/archive_on":
        repository.update_owner(owner["owner_id"], {"enabled": True, "paused": False})
        await message.answer(T.HANDLERS_ARCHIVE_ARCHIVE_ENABLED, reply_markup=app.archive.web_button())
    elif command == "/pause":
        repository.update_owner(owner["owner_id"], {"paused": not bool(owner["paused"])})
        await message.answer(
            T.HANDLERS_ARCHIVE_ARCHIVE_RESUMED if owner["paused"] else T.HANDLERS_ARCHIVE_ARCHIVE_PAUSED,
            reply_markup=app.archive.web_button(),
        )
    elif command == "/mute":
        repository.update_owner(owner["owner_id"], {"notify_enabled": not bool(owner["notify_enabled"])})
        await message.answer(
            T.HANDLERS_ARCHIVE_NOTIFICATIONS_ENABLED
            if not owner["notify_enabled"]
            else T.HANDLERS_ARCHIVE_NOTIFICATIONS_DISABLED
        )
    elif command == "/stats":
        totals = statistics_for(repository, owner["owner_id"])["totals"]
        await message.answer(
            T.HANDLERS_ARCHIVE_STATS_SUMMARY.format(
                chats=totals["chats"],
                messages=totals["messages"],
                deleted=totals["deleted"],
                edited=totals["edited"],
                favorites=totals["favorites"],
            ),
            reply_markup=app.archive.web_button(T.HANDLERS_ARCHIVE_STATS_BUTTON),
        )
    elif command == "/export":
        format_name = arguments[1].lower() if len(arguments) > 1 else "json"
        if format_name not in FORMATS:
            await message.answer(T.HANDLERS_ARCHIVE_EXPORT_FORMATS)
            return
        repository.enqueue(owner["owner_id"], "export", {"format": format_name})
        await message.answer(T.HANDLERS_ARCHIVE_EXPORT_QUEUED)
    elif command == "/delete_me":
        await message.answer(
            T.HANDLERS_ARCHIVE_DELETE_INSTRUCTIONS,
            reply_markup=app.archive.web_button(T.HANDLERS_ARCHIVE_SETTINGS_BUTTON),
        )
    else:
        state = (
            T.HANDLERS_ARCHIVE_STATE_PAUSED
            if owner["paused"]
            else T.HANDLERS_ARCHIVE_STATE_ENABLED
            if owner["enabled"]
            else T.HANDLERS_ARCHIVE_STATE_DISABLED
        )
        connected = any(item["enabled"] for item in repository.connections(owner["owner_id"]))
        text = T.HANDLERS_ARCHIVE_OVERVIEW.format(
            state=state,
            connection=T.HANDLERS_ARCHIVE_CONNECTED if connected else T.HANDLERS_ARCHIVE_NOT_CONNECTED,
        )
        if not app.archive.config.public_url:
            text += "\n\n" + T.HANDLERS_ARCHIVE_ARCHIVE_NO_WEB
        await message.answer(text, reply_markup=app.archive.web_button())


@router.message(F.chat.type == "private", F.chat_shared)
async def vault_selected(message: Message, app: Application):
    if not message.from_user or message.chat_shared.request_id != 8142:
        return UNHANDLED
    app.archive.repository.ensure_owner(message.from_user.id)
    pending = app.archive.repository.db.execute(
        "SELECT expires_at FROM archive_vault_requests WHERE owner_id=?", (message.from_user.id,)
    ).fetchone()
    if not pending or pending[0] < time.time():
        await message.answer(T.HANDLERS_ARCHIVE_VAULT_REQUEST_REQUIRED, reply_markup=ReplyKeyboardRemove())
        return
    try:
        async with app.archive.owner_lock(message.from_user.id):
            await app.archive.bind_vault(message.from_user.id, message.chat_shared.chat_id)
            with app.archive.repository.db:
                app.archive.repository.db.execute(
                    "DELETE FROM archive_vault_requests WHERE owner_id=?", (message.from_user.id,)
                )
    except ValueError as error:
        await message.answer(str(error), reply_markup=ReplyKeyboardRemove())
        return
    await message.answer(T.HANDLERS_ARCHIVE_VAULT_BOUND, reply_markup=ReplyKeyboardRemove())


@router.callback_query(F.data.startswith("a:"))
async def archive_action(query: CallbackQuery, app: Application):
    if not isinstance(query.message, Message) or query.message.chat.id != query.from_user.id:
        await query.answer(T.HANDLERS_ARCHIVE_PRIVATE_ARCHIVE_REQUIRED, show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await query.answer(T.HANDLERS_ARCHIVE_INVALID_BUTTON)
        return
    owner, identifier = query.from_user.id, int(parts[2])
    repository = app.archive.repository
    profile = repository.owner(owner)
    if profile and profile["pin_hash"]:
        await query.answer(T.HANDLERS_ARCHIVE_UNLOCK_REQUIRED, show_alert=True)
        return
    try:
        if parts[1] == "fav":
            record = repository.message(owner, identifier)
            if not record:
                raise LookupError()
            repository.favorite(owner, identifier, not bool(record["favorite"]))
            await query.answer(
                T.HANDLERS_ARCHIVE_FAVORITE_REMOVED if record["favorite"] else T.HANDLERS_ARCHIVE_FAVORITE_ADDED
            )
        elif parts[1] == "mute":
            chat = repository.chat(owner, identifier)
            if not chat:
                raise LookupError()
            repository.update_chat(owner, identifier, {"muted": not bool(chat["muted"])})
            await query.answer(T.HANDLERS_ARCHIVE_CHAT_UNMUTED if chat["muted"] else T.HANDLERS_ARCHIVE_CHAT_MUTED)
        else:
            await query.answer(T.HANDLERS_ARCHIVE_INVALID_BUTTON)
    except LookupError:
        await query.answer(T.HANDLERS_ARCHIVE_RECORD_MISSING, show_alert=True)
