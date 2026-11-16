"""Apply bot descriptions, commands and Mini App menu after configuring HTTPS."""

import asyncio
import logging

from aiogram import Bot

from archive.config import ArchiveSettings
from config import load_settings
from core import texts as T
from core.logging import configure_logging
from services.telegram_setup import configure_telegram


async def main():
    settings = load_settings()
    async with Bot(settings.token) as bot:
        identity = await bot.get_me()
        await configure_telegram(bot, ArchiveSettings.from_environment())
        print(T.SETUP_TELEGRAM_SETUP_COMPLETE.format(username=identity.username))


if __name__ == "__main__":
    configure_logging()
    try:
        asyncio.run(main())
    except Exception as error:
        logging.getLogger(__name__).error("Telegram setup failed type=%s", type(error).__name__)
        raise SystemExit(1)
