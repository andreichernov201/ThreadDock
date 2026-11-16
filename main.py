import argparse
import asyncio
import logging
import sqlite3

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import ErrorEvent

from archive.web import start_web_server
from config import load_settings
from core import texts as T
from core.logging import configure_logging
from handlers import archive, business, callbacks, commands, links, start
from services.application import Application
from services.http import PublicHttp
from services.runtime import check_configuration, validate_runtime
from services.telegram_setup import configure_telegram
from storage.database import Database
from storage.lock import InstanceLock

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = load_settings()
    validate_runtime(settings)
    with InstanceLock(settings.data_dir / "app.lock"):
        database = Database(settings.data_dir / "threaddock.sqlite3", settings.data_dir)
        session = (
            AiohttpSession(api=TelegramAPIServer.from_base(settings.api_base, is_local=True))
            if settings.api_base
            else AiohttpSession()
        )
        bot = Bot(settings.token, session=session)
        try:
            identity = await bot.get_me()
            database.bind_bot(identity.id)
            webhook = await bot.get_webhook_info()
            if webhook.url:
                raise RuntimeError(T.MAIN_WEBHOOK_CONFLICT)
            async with PublicHttp() as http:
                app = Application(settings, database, bot, http)
                app.archive.bot_username = identity.username or ""
                dispatcher = Dispatcher(app=app)
                dispatcher.include_routers(
                    start.router, archive.router, commands.router, callbacks.router, business.router, links.router
                )

                @dispatcher.errors()
                async def safe_error(event: ErrorEvent) -> bool:
                    logger.error("Update processing failed type=%s", type(event.exception).__name__)
                    if isinstance(event.exception, sqlite3.Error):
                        # Stop before the next getUpdates offset acknowledges data we could not persist.
                        await dispatcher.stop_polling()
                    return True

                await configure_telegram(bot, app.archive.config)
                web_runner = await start_web_server(app.archive)
                app.queue.start()
                app.archive.start()
                polling = asyncio.create_task(
                    dispatcher.start_polling(
                        bot,
                        close_bot_session=False,
                        allowed_updates=dispatcher.resolve_used_update_types(),
                        handle_as_tasks=False,
                    ),
                    name="telegram-polling",
                )
                logger.info("ThreadDock started workers=%s", settings.workers)
                try:
                    done, _ = await asyncio.wait(
                        [polling, *app.queue.tasks, app.archive.task], return_when=asyncio.FIRST_COMPLETED
                    )
                    if polling not in done:
                        raise RuntimeError("A background service stopped unexpectedly")
                    await polling
                finally:
                    app.accepting = False
                    app.queue.stopping.set()
                    app.archive.stopping.set()
                    polling.cancel()
                    await asyncio.gather(polling, return_exceptions=True)
                    await app.queue.close()
                    await app.archive.close()
                    await web_runner.cleanup()
        finally:
            await bot.session.close()
            database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ThreadDock media bot")
    parser.add_argument("--check", action="store_true", help=T.MAIN_CHECK_DESCRIPTION)
    parser.add_argument("--no-token", action="store_true", help=T.MAIN_NO_TOKEN_DESCRIPTION)
    arguments = parser.parse_args()
    configure_logging()
    if arguments.check:
        raise SystemExit(check_configuration(require_token=not arguments.no_token))
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("ThreadDock stopped")
    except (ValueError, RuntimeError) as error:
        logger.error("Startup stopped: %s", error)
        raise SystemExit(1)
    except Exception as error:
        logger.error("Startup failed type=%s", type(error).__name__)
        raise SystemExit(1)
