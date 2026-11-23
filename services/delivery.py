"""Telegram transport: scoped file_id cache and durable delivery checkpoints."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    ReplyParameters,
)

from config import Settings
from core import texts as T
from core.errors import MediaError, MissingAsset
from core.models import Asset, Destination, Job, MediaBundle
from services.access import sender_permitted
from services.comments import comment_caption
from storage.database import Database

logger = logging.getLogger(__name__)


def target_options(destination: Destination) -> dict:
    return {
        "chat_id": destination.chat_id,
        "message_thread_id": destination.thread_id,
        "business_connection_id": destination.business_id or None,
        "reply_parameters": ReplyParameters(message_id=destination.message_id, allow_sending_without_reply=True),
    }


def is_file_id_error(error: TelegramBadRequest) -> bool:
    text = error.message.lower()
    return any(part in text for part in ("wrong file", "file reference", "file_id", "failed to get http url content"))


class TelegramDelivery:
    def __init__(self, bot: Bot, database: Database, settings: Settings):
        self.bot, self.database, self.settings = bot, database, settings

    def permitted(self, destination: Destination) -> bool:
        if destination.business_id:
            return (
                self.settings.business_enabled
                and (not self.settings.business_chats or destination.chat_id in self.settings.business_chats)
                and self.database.business_can_send(destination.business_id, destination.chat_id)
            )
        if destination.chat_id < 0:
            return self.database.group_enabled(destination.chat_id, self.settings.groups_enabled)
        return True

    async def text(self, destination: Destination, text: str, markup=None) -> None:
        if self.permitted(destination):
            await self.bot.send_message(**target_options(destination), text=text, reply_markup=markup)

    def cached(self, job: Job) -> MediaBundle | None:
        return self.database.get_delivery_cache(self.bot.id, job.cache_key, job.mode)

    def _check_deadline(self, job: Job) -> None:
        if self.database.request_remaining(job.id, self.settings.request_timeout) <= 0:
            raise MediaError(
                T.DOWNLOAD_TIME_LIMIT.format(seconds=int(self.settings.request_timeout)), code="request_expired"
            )

    def _input(self, job: Job, bundle: MediaBundle, asset: Asset, *, use_cache: bool = True):
        cached = self.database.file_id(self.bot.id, job.cache_key, asset.filename, asset.kind) if use_cache else None
        if cached:
            return cached
        path = asset.path(bundle.directory)
        if not path.is_file():
            raise MissingAsset()
        if path.stat().st_size > self.settings.max_upload_bytes:
            raise MediaError(T.SERVICES_DELIVERY_FILE_TOO_LARGE, code="file_too_large")
        return FSInputFile(path)

    @staticmethod
    def keyboard(job: Job, bundle: MediaBundle) -> InlineKeyboardMarkup | None:
        buttons = []
        if bundle.originals and job.mode not in {"photos", "album"} and (bundle.video or bundle.media):
            buttons.append(
                InlineKeyboardButton(
                    text=T.SERVICES_DELIVERY_ORIGINAL_PHOTOS.format(value1=len(bundle.originals)),
                    callback_data=f"m:{job.id}:photos",
                )
            )
        if bundle.audio and job.mode != "audio":
            buttons.append(InlineKeyboardButton(text=T.SERVICES_DELIVERY_AUDIO, callback_data=f"m:{job.id}:audio"))
        return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

    async def _send_piece(self, job: Job, bundle: MediaBundle, assets: tuple[Asset, ...], use_cache: bool, caption=""):
        inputs = [self._input(job, bundle, asset, use_cache=use_cache) for asset in assets]
        options = target_options(job.destination)
        caption_options = {"caption": comment_caption(caption), "parse_mode": None} if caption else {}
        if len(assets) > 1:
            media = []
            for index, (asset, value) in enumerate(zip(assets, inputs, strict=True)):
                item_options = caption_options if index == 0 else {}
                if asset.kind == "photo":
                    media.append(InputMediaPhoto(media=value, **item_options))
                elif asset.kind == "video":
                    media.append(
                        InputMediaVideo(
                            media=value,
                            duration=int(asset.duration),
                            width=asset.width,
                            height=asset.height,
                            supports_streaming=True,
                            **item_options,
                        )
                    )
                else:
                    media.append(InputMediaDocument(media=value, **item_options))
            return await self.bot.send_media_group(**options, media=media)
        asset, value = assets[0], inputs[0]
        options.update(caption_options)
        if asset.kind == "video":
            result = await self.bot.send_video(
                **options,
                video=value,
                duration=int(asset.duration),
                width=asset.width,
                height=asset.height,
                supports_streaming=True,
                reply_markup=self.keyboard(job, bundle),
            )
        elif asset.kind == "audio":
            result = await self.bot.send_audio(
                **options,
                audio=value,
                title=bundle.title[:64],
                duration=int(asset.duration),
                reply_markup=self.keyboard(job, bundle),
            )
        elif asset.kind == "photo":
            result = await self.bot.send_photo(**options, photo=value, reply_markup=self.keyboard(job, bundle))
        else:
            result = await self.bot.send_document(**options, document=value, reply_markup=self.keyboard(job, bundle))
        return [result]

    async def deliver(self, job: Job, bundle: MediaBundle) -> None:
        if not self.permitted(job.destination) or not await sender_permitted(self.bot, self.database, job.destination):
            raise MediaError(T.SERVICES_DELIVERY_PERMISSION_REVOKED, code="permission_revoked")
        items = bundle.items(job.mode)
        if not items:
            raise MediaError(T.SERVICES_DELIVERY_REQUESTED_MEDIA_MISSING, code="no_asset")
        pieces = [items[index : index + 10] for index in range(0, len(items), 10)]
        # Preflight before the first API request; a stale disk cache can be refetched safely.
        for piece in pieces[job.progress :]:
            for asset in piece:
                for part in asset.parts:
                    self._input(job, bundle, part)
        for index, piece in enumerate(pieces):
            if index < job.progress:
                continue
            if not self.permitted(job.destination) or not await sender_permitted(
                self.bot, self.database, job.destination
            ):
                raise MediaError(T.SERVICES_DELIVERY_SEND_PERMISSION_REVOKED, code="permission_revoked")
            self._check_deadline(job)
            self.database.status(job.id, "sending")
            try:
                caption = bundle.comment if index == 0 and job.mode != "audio" else ""
                messages = await self._send_piece(job, bundle, piece, use_cache=True, caption=caption)
            except TelegramBadRequest as error:
                if not is_file_id_error(error):
                    raise
                self.database.status(job.id, "running")
                for asset in piece:
                    for part in asset.parts:
                        self.database.forget_file_id(self.bot.id, job.cache_key, part.filename, part.kind)
                self._check_deadline(job)
                self.database.status(job.id, "sending")
                messages = await self._send_piece(job, bundle, piece, use_cache=False, caption=caption)
            # Save checkpoint before cache writes: a crash must not replay an accepted album.
            self.database.delivered_piece(job.id, index, [message.message_id for message in messages])
            for asset, message in zip(piece, messages, strict=True):
                remote = getattr(message, asset.kind, None)
                if asset.kind == "photo" and remote:
                    remote = remote[-1]
                if remote:
                    self.database.save_file_id(self.bot.id, job.cache_key, asset.filename, asset.kind, remote.file_id)
            self.database.remember_delivery(job.cache_key, bundle)
            if (
                len(piece) > 1
                and index == len(pieces) - 1
                and self.keyboard(job, bundle)
                and self.database.request_remaining(job.id, self.settings.request_timeout) > 0
            ):
                # Clients do not display keyboards on media-group members. Keep
                # controls in a separate reply after the confirmed final album.
                # This runs only for a newly accepted piece, never on recovery.
                from aiogram.exceptions import TelegramAPIError

                try:
                    options = target_options(job.destination)
                    options["reply_parameters"] = ReplyParameters(
                        message_id=messages[-1].message_id, allow_sending_without_reply=True
                    )
                    await self.bot.send_message(
                        **options,
                        text=T.SERVICES_DELIVERY_ALBUM_ACTIONS,
                        reply_markup=self.keyboard(job, bundle),
                    )
                except TelegramAPIError as error:
                    logger.info("album_controls_unavailable type=%s", type(error).__name__)
