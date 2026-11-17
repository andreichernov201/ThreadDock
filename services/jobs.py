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
from core.logging import log_context
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
        with log_context(
            job=job.id, chat=job.destination.chat_id, message=job.destination.message_id,
            business=bool(job.destination.business_id), mode=job.mode, attempt=job.attempts,
            media=job.cache_key[:12],
        ):
            await self._run_job(job)

    async def _run_job(self, job: Job) -> None:
        started = time.monotonic()
        stage = "permission"
        try:
            if not self.delivery.permitted(job.destination):
                raise MediaError(T.SERVICES_JOBS_CHAT_DISABLED, code="permission_revoked")
            bundle = self.delivery.cached(job)
            logger.info("download_start cache=%s progress=%s", "telegram" if bundle else "miss", job.progress)
            if bundle is None:
                stage = "download"
                bundle = await self.pipeline.fetch(job.url)
            try:
                stage = "delivery"
                await self.delivery.deliver(job, bundle)
            except MissingAsset:
                bundle = await self.pipeline.fetch(job.url, force=True)
                row = self.database.get_job(job.id)
                await self.delivery.deliver(replace(job, progress=row["progress"]), bundle)
            self.database.complete_job(job.id)
            logger.info("download_done elapsed_ms=%s", round((time.monotonic() - started) * 1000))
        except asyncio.CancelledError:
            row = self.database.get_job(job.id)
            self.database.status(job.id, "uncertain" if row["status"] == "sending" else "queued", "shutdown")
            raise
        except TelegramRetryAfter as error:
            self.database.status(job.id, "queued", "telegram_rate_limit", delay=error.retry_after + 1)
        except TelegramNetworkError, TelegramServerError:
            self.database.status(job.id, "uncertain", "delivery_uncertain")
            logger.warning("download_uncertain stage=%s reason=telegram_network", stage)
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
                if error.retryable:
                    self.database.offer_retry(job.id)
                await self._notify(job, str(error), retry=error.retryable)
            logger.warning(
                "download_failed stage=%s code=%s retryable=%s elapsed_ms=%s",
                stage, error.code, error.retryable, round((time.monotonic() - started) * 1000),
            )
        except Exception as error:
            # SafeFormatter drops exception text and preserves code locations.
            row = self.database.get_job(job.id)
            self.database.status(job.id, "uncertain" if row["status"] == "sending" else "failed", "internal_error")
            logger.exception("download_failed stage=%s code=internal_error", stage)
            await self._notify(job, T.SERVICES_JOBS_INTERNAL_ERROR)

    async def _notify(self, job: Job, message: str, *, retry: bool = False) -> None:
        markup = None
        if retry:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=T.SERVICES_JOBS_RETRY, callback_data=f"r:{job.id}:{job.attempts}")]
                ]
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
