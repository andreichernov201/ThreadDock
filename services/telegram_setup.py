"""Idempotent Bot API configuration; never changes account connection permissions."""

from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    MenuButtonWebApp,
    WebAppInfo,
)

from core import texts as T


async def configure_telegram(bot, archive_config):
    private = [
        ("start", T.SERVICES_TELEGRAM_SETUP_COMMAND_START),
        ("archive", T.SERVICES_TELEGRAM_SETUP_COMMAND_ARCHIVE),
        ("status", T.SERVICES_TELEGRAM_SETUP_COMMAND_STATUS),
        ("stats", T.SERVICES_TELEGRAM_SETUP_COMMAND_STATS),
        ("export", T.SERVICES_TELEGRAM_SETUP_COMMAND_EXPORT),
        ("pause", T.SERVICES_TELEGRAM_SETUP_COMMAND_PAUSE),
        ("mute", T.SERVICES_TELEGRAM_SETUP_COMMAND_MUTE),
        ("delete_me", T.SERVICES_TELEGRAM_SETUP_COMMAND_DELETE),
        ("help", T.SERVICES_TELEGRAM_SETUP_COMMAND_HELP),
    ]
    groups = [
        ("enable", T.SERVICES_TELEGRAM_SETUP_COMMAND_ENABLE),
        ("disable", T.SERVICES_TELEGRAM_SETUP_COMMAND_DISABLE),
        ("status", T.SERVICES_TELEGRAM_SETUP_COMMAND_MY_TASKS),
        ("help", T.SERVICES_TELEGRAM_SETUP_COMMAND_HELP),
    ]
    await bot.set_my_commands(
        [BotCommand(command=key, description=value) for key, value in private], scope=BotCommandScopeAllPrivateChats()
    )
    await bot.set_my_commands(
        [BotCommand(command=key, description=value) for key, value in groups], scope=BotCommandScopeAllGroupChats()
    )
    await bot.set_my_description(T.SERVICES_TELEGRAM_SETUP_DESCRIPTION)
    await bot.set_my_short_description(T.SERVICES_TELEGRAM_SETUP_SHORT_DESCRIPTION)
    if archive_config.public_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text=T.SERVICES_TELEGRAM_SETUP_MENU_ARCHIVE, web_app=WebAppInfo(url=archive_config.public_url + "/")
            )
        )
