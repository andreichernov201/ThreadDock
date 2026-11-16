"""Persistent queue. One chat is sequential; independent chats run in parallel."""

import asyncio
import logging
import time
from dataclasses import replace

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Settings
from core import texts as T
from core.errors import MediaError, MissingAsset
from core.models import Job
from services.delivery import TelegramDelivery
from services.pipeline import MediaPipeline
from storage.database import Database

logger = logging.getLogger(__name__)


class JobQueue:
    def __init__(self, settings: Settings, database: Database, pipeline: MediaPipeline, delivery: TelegramDelivery):
        self.settings, self.database, self.pipeline, self.delivery = settings, database, pipeline, delivery
        self.stopping = asyncio.Event()
        self.tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self.database.recover()
        self.pipeline.clean_work()
        self.tasks = [
            asyncio.create_task(self._worker(), name=f"download-{index}") for index in range(self.settings.workers)
        ]
        self.tasks.append(asyncio.create_task(self._maintenance(), name="maintenance"))

    async def close(self) -> None:
        self.stopping.set()
        if not self.tasks:
            return
        _, pending = await asyncio.wait(self.tasks, timeout=self.settings.shutdown_timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _idle(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stopping.wait(), seconds)
        except TimeoutError:
            pass

    async def _worker(self) -> None:
        while not self.stopping.is_set():
            job = self.database.claim()
            if job is None:
                await self._idle(0.3)
                continue
            await self._process(job)

    async def _process(self, job: Job) -> None:
        try:
            if not self.delivery.permitted(job.destination):
                raise MediaError(T.SERVICES_JOBS_CHAT_DISABLED, code="permission_revoked")
            bundle = await self.pipeline.fetch(job.url)
            try:
                await self.delivery.deliver(job, bundle)
            except MissingAsset:
                bundle = await self.pipeline.fetch(job.url, force=True)
                row = self.database.get_job(job.id)
                await self.delivery.deliver(replace(job, progress=row["progress"]), bundle)
            self.database.status(job.id, "done")
            logger.info("job=%s completed", job.id)
        except asyncio.CancelledError:
            row = self.database.get_job(job.id)
            self.database.status(job.id, "uncertain" if row["status"] == "sending" else "queued", "shutdown")
            raise
        except TelegramRetryAfter as error:
            self.database.status(job.id, "queued", "telegram_rate_limit", delay=error.retry_after + 1)
        except TelegramNetworkError, TelegramServerError:
            self.database.status(job.id, "uncertain", "delivery_uncertain")
            await self._notify(job, T.SERVICES_JOBS_SEND_UNCERTAIN)
        except (TelegramForbiddenError, TelegramBadRequest) as error:
            self.database.status(job.id, "failed", "telegram_rejected")
            logger.warning("job=%s Telegram rejected delivery type=%s", job.id, type(error).__name__)
            if not isinstance(error, TelegramForbiddenError):
                await self._notify(job, T.SERVICES_JOBS_FILE_REJECTED)
        except MediaError as error:
            if error.retryable and job.attempts <= self.settings.retries:
                self.database.status(job.id, "queued", error.code, delay=min(30, 2**job.attempts))
            else:
                self.database.status(job.id, "failed", error.code)
                await self._notify(job, str(error))
            logger.warning("job=%s code=%s", job.id, error.code)
        except Exception as error:
            # Do not log exception text/tracebacks: upstream exceptions can contain signed URLs.
            row = self.database.get_job(job.id)
            self.database.status(job.id, "uncertain" if row["status"] == "sending" else "failed", "internal_error")
            logger.error("job=%s unexpected type=%s", job.id, type(error).__name__)
            await self._notify(job, T.SERVICES_JOBS_INTERNAL_ERROR)

    async def _notify(self, job: Job, message: str) -> None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=T.SERVICES_JOBS_RETRY, callback_data=f"m:{job.id}:video")]]
        )
        try:
            await self.delivery.text(job.destination, message, markup)
        except TelegramAPIError:
            logger.warning("job=%s notification unavailable", job.id)

    async def _maintenance(self) -> None:
        while not self.stopping.is_set():
            if any(task.done() for task in self.tasks if task is not asyncio.current_task()):
                raise RuntimeError("A download worker stopped unexpectedly")
            self.database.set_state("heartbeat", str(time.time()))
            self.pipeline.maintain()
            await self._idle(10)
