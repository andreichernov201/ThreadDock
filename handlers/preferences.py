"""Personal downloader preferences and administrator-owned group settings."""

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core import texts as T
from services.application import Application

router = Router()


async def editable(message: Message, actor: int, app: Application) -> tuple[str, int] | None:
    if message.business_connection_id:
        return None
    if message.chat.type == "private":
        return ("user", actor) if message.chat.id == actor else None
    if message.chat.type in {"group", "supergroup"}:
        try:
            member = await app.bot.get_chat_member(message.chat.id, actor)
        except TelegramAPIError:
            return None
        if member.status in {"creator", "administrator"}:
            return "group", message.chat.id
    return None


def panel(scope: str, subject: int, app: Application):
    prefs = app.database.preferences(scope, subject)

    def choices(name, values):
        return [
            InlineKeyboardButton(
                text=("✓ " if prefs.get(name) == value else "") + label,
                callback_data=f"pref:{name}:{value}",
            )
            for value, label in values
        ]

    rows = [
        choices("mode", [("slideshow", T.PREF_SLIDESHOW), ("photos", T.PREF_PHOTOS)]),
        choices("photo_seconds", [(value, T.PREF_SECONDS.format(n=value)) for value in (2, 3, 5, 10)]),
        choices("live_limit", [(0, T.PREF_FULL), (5, T.PREF_LIMIT.format(n=5)), (10, T.PREF_LIMIT.format(n=10))]),
    ]
    if scope == "group":
        rows.append(choices("senders", [("all", T.PREF_ALL), ("admins", T.PREF_ADMINS)]))
        rows.append(choices("senders", [("selected", T.PREF_SELECTED), ("mine", T.PREF_SELF)]))
    return T.PREF_GROUP if scope == "group" else T.PREF_PERSONAL, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("settings"))
async def settings_command(message: Message, app: Application) -> None:
    if not message.from_user:
        return
    target = await editable(message, message.from_user.id, app)
    if target is None:
        await message.answer(T.PREF_FORBIDDEN)
        return
    text, markup = panel(*target, app)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("pref:"))
async def change_preference(query: CallbackQuery, app: Application) -> None:
    if not isinstance(query.message, Message):
        await query.answer(T.PREF_FORBIDDEN, show_alert=True)
        return
    target = await editable(query.message, query.from_user.id, app)
    if target is None:
        await query.answer(T.PREF_FORBIDDEN, show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer(T.HANDLERS_CALLBACKS_INVALID_BUTTON)
        return
    scope, subject = target
    name, value = parts[1:]
    try:
        if name == "senders" and scope == "user":
            # Old keyboards can still be clicked after an update. Replace the
            # panel without restoring the removed personal sender rule.
            pass
        elif name == "senders" and value == "mine" and scope == "group":
            app.database.only_sender(subject, query.from_user.id)
        else:
            value = int(value) if name in {"photo_seconds", "live_limit"} else value
            app.database.set_preference(scope, subject, name, value)
    except ValueError:
        await query.answer(T.HANDLERS_CALLBACKS_INVALID_BUTTON)
        return
    text, markup = panel(scope, subject, app)
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in error.message.lower():
            raise
    await query.answer(T.PREF_AUTOMATIC if name == "senders" and scope == "user" else T.PREF_SAVED)


@router.message(Command("allow", "deny"))
async def allow_sender(message: Message, app: Application) -> None:
    target = await editable(message, message.from_user.id, app) if message.from_user else None
    if target is None or target[0] != "group":
        await message.answer(T.PREF_FORBIDDEN)
        return
    reply = message.reply_to_message
    if not reply or not reply.from_user or reply.from_user.is_bot:
        await message.answer(T.PREF_REPLY_REQUIRED)
        return
    enabled = (message.text or "").split()[0].split("@")[0] == "/allow"
    app.database.allow_sender(target[1], reply.from_user.id, enabled)
    await message.answer(T.PREF_ALLOWED if enabled else T.PREF_DENIED)
