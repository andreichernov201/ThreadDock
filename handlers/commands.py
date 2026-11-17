from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from core import texts as T
from services.application import Application

router = Router()


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(T.HANDLERS_COMMANDS_HELP)


@router.message(Command("enable", "disable"))
async def group_switch(message: Message, app: Application) -> None:
    if message.chat.type not in {"group", "supergroup"} or not message.from_user:
        await message.answer(T.HANDLERS_COMMANDS_GROUP_ONLY)
        return
    member = await app.bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in {"creator", "administrator"}:
        await message.answer(T.HANDLERS_COMMANDS_GROUP_ADMIN_ONLY)
        return
    enabled = (message.text or "").split()[0].split("@")[0] == "/enable"
    app.database.set_group(message.chat.id, enabled)
    await message.answer(T.HANDLERS_COMMANDS_GROUP_ENABLED if enabled else T.HANDLERS_COMMANDS_GROUP_DISABLED)


@router.message(Command("status"))
async def status_command(message: Message, app: Application) -> None:
    if not message.from_user:
        return
    counts = app.database.user_counts(message.from_user.id, message.chat.id)
    names = {
        "queued": T.HANDLERS_COMMANDS_STATUS_PENDING,
        "running": T.HANDLERS_COMMANDS_STATUS_RUNNING,
        "sending": T.HANDLERS_COMMANDS_STATUS_SENDING,
        "done": T.HANDLERS_COMMANDS_STATUS_DONE,
        "failed": T.HANDLERS_COMMANDS_STATUS_FAILED,
        "uncertain": T.HANDLERS_COMMANDS_STATUS_UNCERTAIN,
    }
    lines = [f"{label}: {counts[status]}" for status, label in names.items() if counts.get(status)]
    await message.answer("\n".join(lines) if lines else T.HANDLERS_COMMANDS_NO_JOBS)


@router.message(Command("photos", "slideshow"))
async def group_photo_mode(message: Message, app: Application) -> None:
    if message.chat.type not in {"group", "supergroup"} or not message.from_user:
        await message.answer(T.HANDLERS_COMMANDS_GROUP_ONLY)
        return
    member = await app.bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in {"creator", "administrator"}:
        await message.answer(T.HANDLERS_COMMANDS_GROUP_ADMIN_ONLY)
        return
    photos = (message.text or "").split()[0].split("@")[0] == "/photos"
    app.database.set_group_photo_mode(message.chat.id, "photos" if photos else "slideshow")
    await message.answer(T.GROUP_MODE_PHOTOS if photos else T.GROUP_MODE_SLIDESHOW)
