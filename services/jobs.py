"""Parallel preparation with persistent, ordered delivery per conversation."""

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
from aiohttp import ClientConnectorError

from config import Settings
from core import texts as T
from core.errors import MediaError, MissingAsset
from core.logging import log_context
from core.models import Job, RenderOptions
from services.delivery import TelegramDelivery
from services.pipeline import KeyedLocks, MediaPipeline
from storage.database import Database

logger = logging.getLogger(__name__)


class JobQueue:
    def __init__(self, settings: Settings, database: Database, pipeline: MediaPipeline, delivery: TelegramDelivery):
        self.settings, self.database, self.pipeline, self.delivery = settings, database, pipeline, delivery
        self.stopping = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self.prepared = {}
        self.processing: dict[int, asyncio.Task] = {}
        self.delivery_locks = KeyedLocks()

    def start(self) -> None:
        self.database.recover()
        self.pipeline.clean_work()
        self.tasks = [
            asyncio.create_task(self._worker(), name=f"download-{index}") for index in range(self.settings.workers)
        ]
        self.tasks.extend(
            asyncio.create_task(self._delivery_worker(), name=f"deliver-{i}") for i in range(self.settings.workers)
        )
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
            job = self.database.claim(parallel=True)
            if job is None:
                await self._idle(0.3)
                continue
            task = asyncio.create_task(self._process(job, prepare_only=True))
            self.processing[job.id] = task
            try:
                await task
            except asyncio.CancelledError:
                row = self.database.get_job(job.id)
                if self.stopping.is_set() or row is None or row["status"] != "cancelled":
                    raise
            finally:
                self.processing.pop(job.id, None)

    async def _delivery_worker(self) -> None:
        while not self.stopping.is_set():
            job = self.database.claim_ready()
            if job is None:
                await self._idle(0.1)
                continue
            await self._deliver_ready(job)

    async def _deliver_ready(self, job: Job) -> None:
        self.database.pause_request(job.id)
        key = self.database.business_lock(job.id, f"{job.destination.business_id}:{job.destination.chat_id}")
        async with self.delivery_locks.hold(key):
            row = self.database.get_job(job.id)
            if row["status"] == "cancelled":
                self.prepared.pop(job.id, None)
                return
            if row["error_code"]:
                await self._notify(
                    job, self._failure_text(row["error_code"], bool(row["retryable"])), retry=bool(row["retryable"])
                )
                self.database.status(job.id, "failed", row["error_code"])
                return
            bundle = (
                self.prepared.pop(job.id, None) or self.delivery.cached(job) or self.database.get_cache(job.cache_key)
            )
            if bundle is None:
                self.database.status(job.id, "queued")
                return
            self.database.status(job.id, "running")
            await self._process(job, bundle=bundle)

    def _failure_text(self, code: str, retryable: bool) -> str:
        if code == "live_unsupported":
            return T.DOWNLOAD_LIVE_UNSUPPORTED
        if code == "request_expired":
            return T.DOWNLOAD_TIME_LIMIT.format(seconds=int(self.settings.request_timeout))
        if code == "internal_error":
            return T.SERVICES_JOBS_INTERNAL_ERROR
        if code == "file_too_large":
            return T.DOWNLOAD_TOO_LARGE
        if retryable:
            return T.DOWNLOAD_TRY_LATER
        if code in {"unavailable", "authentication", "source_blocked", "source_http", "photo_metadata_missing"}:
            return T.DOWNLOAD_UNAVAILABLE
        return T.DOWNLOAD_FAILED

    async def _process(self, job: Job, *, prepare_only: bool = False, bundle=None) -> None:
        with log_context(
            job=job.id,
            chat=job.destination.chat_id,
            message=job.destination.message_id,
            business=bool(job.destination.business_id),
            mode=job.mode,
            attempt=job.attempts,
            media=job.cache_key[:12],
        ):
            await self._run_job(job, prepare_only=prepare_only, bundle=bundle)

    async def _run_job(self, job: Job, *, prepare_only: bool = False, bundle=None) -> None:
        started = time.monotonic()
        stage = "permission"
        try:
            if self.database.get_job(job.id)["status"] == "cancelled":
                return
            remaining = self.database.request_remaining(job.id, self.settings.request_timeout)
            if remaining <= 0:
                await self._expire(job, defer=prepare_only)
                return
            async with asyncio.timeout(remaining):
                if not self.delivery.permitted(job.destination):
                    raise MediaError(T.SERVICES_JOBS_CHAT_DISABLED, code="permission_revoked")
                bundle = bundle or self.delivery.cached(job)
                logger.info("download_start cache=%s progress=%s", "telegram" if bundle else "miss", job.progress)
                if bundle is None:
                    stage = "download"
                    bundle = await self._fetch(job)
                if self.database.get_job(job.id)["status"] == "cancelled":
                    return
                if prepare_only:
                    self.prepared[job.id] = bundle
                    self.database.status(job.id, "ready")
                    return
                try:
                    stage = "delivery"
                    with self.pipeline.using(bundle):
                        await self.delivery.deliver(job, bundle)
                except MissingAsset:
                    row = self.database.get_job(job.id)
                    if row["business_request_id"] and not row["progress"]:
                        # Rebuild through a preparation worker. Holding the
                        # conversation lock during a refetch would prevent an
                        # already-ready second connection from winning.
                        self.database.delete_cache(job.cache_key)
                        self.database.status(job.id, "queued", "cache_miss")
                        return
                    bundle = await self._fetch(job, force=True)
                    row = self.database.get_job(job.id)
                    with self.pipeline.using(bundle):
                        await self.delivery.deliver(replace(job, progress=row["progress"]), bundle)
                self.database.complete_job(job.id)
                logger.info("download_done elapsed_ms=%s", round((time.monotonic() - started) * 1000))
        except TimeoutError:
            await self._expire(job, bundle, defer=prepare_only)
        except asyncio.CancelledError:
            row = self.database.get_job(job.id)
            if row["status"] != "sending":
                self.database.pause_request(job.id)
            status = "uncertain" if row["status"] == "sending" else "ready" if not prepare_only and bundle else "queued"
            self.database.status(job.id, status, None if status == "ready" else "shutdown")
            raise
        except TelegramRetryAfter as error:
            delay = error.retry_after + 1
            if self.database.request_remaining(job.id, self.settings.request_timeout) <= delay:
                # Telegram explicitly rejected this request, so no upload is in flight.
                self.database.status(job.id, "running")
                await self._expire(job, bundle, defer=prepare_only)
            elif job.attempts <= self.settings.retries:
                self.database.status(job.id, "queued", "telegram_rate_limit", delay=delay)
            else:
                self.database.status(job.id, "failed", "telegram_rate_limit")
                self.database.offer_retry(job.id)
                await self._notify(job, T.DOWNLOAD_TRY_LATER, retry=True)
        except (TelegramNetworkError, TelegramServerError) as error:
            cause = error.__cause__
            logger.warning(
                "telegram_transport stage=%s type=%s cause=%s", stage, type(error).__name__, type(cause).__name__
            )
            if isinstance(cause, ClientConnectorError):
                # Connection establishment failed: Telegram has received no HTTP
                # request. Retrying this case cannot duplicate an accepted upload.
                if (
                    job.attempts <= self.settings.retries
                    and self.database.request_remaining(job.id, self.settings.request_timeout) > 2
                ):
                    self.database.status(job.id, "queued", "telegram_connect", delay=2)
                else:
                    self.database.status(job.id, "failed", "telegram_connect")
                    self.database.offer_retry(job.id)
                    await self._notify(job, T.DOWNLOAD_TRY_LATER, retry=True)
            else:
                self.database.status(job.id, "uncertain", "delivery_uncertain")
                await self._notify(job, T.SERVICES_JOBS_SEND_UNCERTAIN)
        except (TelegramForbiddenError, TelegramBadRequest) as error:
            self.database.status(job.id, "failed", "telegram_rejected")
            logger.warning("job=%s Telegram rejected delivery type=%s", job.id, type(error).__name__)
            if not isinstance(error, TelegramForbiddenError):
                await self._notify(job, T.SERVICES_JOBS_FILE_REJECTED)
        except MediaError as error:
            if error.code == "business_standby":
                self.database.status(job.id, "ready")
                return
            if error.code == "permission_revoked":
                self.database.status(job.id, "cancelled", error.code)
                return
            remaining = self.database.request_remaining(job.id, self.settings.request_timeout)
            delay = min(30, 2**job.attempts)
            if error.code == "request_expired" or remaining <= 0 or (error.retryable and remaining <= delay):
                await self._expire(job, bundle, defer=prepare_only)
                return
            if error.retryable and job.attempts <= self.settings.retries:
                self.database.status(job.id, "queued", error.code, delay=delay)
            else:
                self.database.status(job.id, "failed", error.code)
                if error.retryable:
                    self.database.offer_retry(job.id)
                unavailable = {
                    "unavailable",
                    "authentication",
                    "source_blocked",
                    "source_http",
                    "photo_metadata_missing",
                }
                message = (
                    T.DOWNLOAD_TOO_LARGE
                    if error.code == "file_too_large"
                    else T.DOWNLOAD_TRY_LATER
                    if error.retryable
                    else T.DOWNLOAD_UNAVAILABLE
                    if error.code in unavailable
                    else T.DOWNLOAD_FAILED
                )
                if prepare_only:
                    self.database.status(job.id, "ready", error.code)
                else:
                    await self._notify(job, message, retry=error.retryable)
            logger.warning(
                "download_failed stage=%s code=%s retryable=%s elapsed_ms=%s",
                stage,
                error.code,
                error.retryable,
                round((time.monotonic() - started) * 1000),
            )
        except Exception:
            # SafeFormatter drops exception text and preserves code locations.
            row = self.database.get_job(job.id)
            self.database.status(job.id, "uncertain" if row["status"] == "sending" else "failed", "internal_error")
            logger.exception("download_failed stage=%s code=internal_error", stage)
            if prepare_only:
                self.database.status(job.id, "ready", "internal_error")
            else:
                await self._notify(job, T.SERVICES_JOBS_INTERNAL_ERROR)
        finally:
            for prepared_id in list(self.prepared):
                row = self.database.get_job(prepared_id)
                if row is None or row["status"] == "cancelled":
                    self.prepared.pop(prepared_id, None)
            for processing_id, task in list(self.processing.items()):
                if task is not asyncio.current_task() and not task.done() and not task.cancelling():
                    row = self.database.get_job(processing_id)
                    if row and row["status"] == "cancelled":
                        task.cancel()

    async def _fetch(self, job: Job, *, force=False):
        options = {"force": True} if force else {}
        if job.render != RenderOptions():
            options["render"] = job.render
        return await self.pipeline.fetch(job.url, **options)

    async def _expire(self, job: Job, bundle=None, *, defer: bool = False) -> None:
        row = self.database.get_job(job.id)
        pieces = (len(bundle.items(job.mode)) + 9) // 10 if bundle else 0
        if pieces and row["progress"] >= pieces:
            # All media has accepted receipts; an optional keyboard edit timed out.
            self.database.complete_job(job.id)
            return
        if row["status"] == "sending":
            # A cancelled upload may already have been accepted by Telegram.
            self.database.status(job.id, "uncertain", "delivery_uncertain")
            await self._notify(job, T.SERVICES_JOBS_SEND_UNCERTAIN)
        else:
            self.database.status(job.id, "failed", "request_expired")
            self.database.offer_retry(job.id)
            if defer:
                self.database.status(job.id, "ready", "request_expired")
            else:
                await self._notify(
                    job, T.DOWNLOAD_TIME_LIMIT.format(seconds=int(self.settings.request_timeout)), retry=True
                )
        logger.info("download_deadline progress=%s", row["progress"])

    async def _notify(self, job: Job, message: str, *, retry: bool = False) -> None:
        if not self.delivery.permitted(job.destination) or not self.database.business_notify(job.id):
            return
        markup = None
        if retry:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=T.SERVICES_JOBS_RETRY, callback_data=f"r:{job.id}:{job.attempts}")]
                ]
            )
        try:
            async with asyncio.timeout(5):
                await self.delivery.text(job.destination, message, markup)
        except (TelegramAPIError, TimeoutError):
            logger.warning("job=%s notification unavailable", job.id)

    async def _maintenance(self) -> None:
        while not self.stopping.is_set():
            if any(task.done() for task in self.tasks if task is not asyncio.current_task()):
                raise RuntimeError("A download worker stopped unexpectedly")
            self.database.set_state("heartbeat", str(time.time()))
            self.pipeline.maintain()
            await self._idle(10)
