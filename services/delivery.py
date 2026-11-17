"""Telegram transport: scoped file_id cache and durable delivery checkpoints."""

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
from storage.database import Database


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

    async def _send_piece(self, job: Job, bundle: MediaBundle, assets: tuple[Asset, ...], use_cache: bool):
        inputs = [self._input(job, bundle, asset, use_cache=use_cache) for asset in assets]
        options = target_options(job.destination)
        if len(assets) > 1:
            media = []
            for asset, value in zip(assets, inputs, strict=True):
                if asset.kind == "photo":
                    media.append(InputMediaPhoto(media=value))
                elif asset.kind == "video":
                    media.append(
                        InputMediaVideo(
                            media=value,
                            duration=int(asset.duration),
                            width=asset.width,
                            height=asset.height,
                            supports_streaming=True,
                        )
                    )
                else:
                    media.append(InputMediaDocument(media=value))
            return await self.bot.send_media_group(**options, media=media)
        asset, value = assets[0], inputs[0]
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
                **options, audio=value, title=bundle.title[:64], duration=int(asset.duration)
            )
        elif asset.kind == "photo":
            result = await self.bot.send_photo(**options, photo=value, reply_markup=self.keyboard(job, bundle))
        else:
            result = await self.bot.send_document(**options, document=value)
        return [result]

    async def deliver(self, job: Job, bundle: MediaBundle) -> None:
        if not self.permitted(job.destination):
            raise MediaError(T.SERVICES_DELIVERY_PERMISSION_REVOKED, code="permission_revoked")
        items = bundle.items(job.mode)
        if not items:
            raise MediaError(T.SERVICES_DELIVERY_REQUESTED_MEDIA_MISSING, code="no_asset")
        pieces = [items[index : index + 10] for index in range(0, len(items), 10)]
        # Preflight before the first API request; a stale disk cache can be refetched safely.
        for piece in pieces[job.progress :]:
            for asset in piece:
                self._input(job, bundle, asset)
        for index, piece in enumerate(pieces):
            if index < job.progress:
                continue
            if not self.permitted(job.destination):
                raise MediaError(T.SERVICES_DELIVERY_SEND_PERMISSION_REVOKED, code="permission_revoked")
            self.database.status(job.id, "sending")
            try:
                messages = await self._send_piece(job, bundle, piece, use_cache=True)
            except TelegramBadRequest as error:
                if not is_file_id_error(error):
                    raise
                for asset in piece:
                    self.database.forget_file_id(self.bot.id, job.cache_key, asset.filename, asset.kind)
                messages = await self._send_piece(job, bundle, piece, use_cache=False)
            # Save checkpoint before cache writes: a crash must not replay an accepted album.
            self.database.delivered_piece(job.id, index, [message.message_id for message in messages])
            for asset, message in zip(piece, messages, strict=True):
                remote = getattr(message, asset.kind, None)
                if asset.kind == "photo" and remote:
                    remote = remote[-1]
                if remote:
                    self.database.save_file_id(self.bot.id, job.cache_key, asset.filename, asset.kind, remote.file_id)
            self.database.remember_delivery(job.cache_key, bundle)
            if len(piece) > 1 and index == len(pieces) - 1 and self.keyboard(job, bundle):
                # Albums cannot include reply_markup. Edit the last accepted member;
                # failure of this optional UI step must never replay accepted media.
                from aiogram.exceptions import TelegramAPIError

                try:
                    await self.bot.edit_message_reply_markup(
                        chat_id=job.destination.chat_id,
                        message_id=messages[-1].message_id,
                        business_connection_id=job.destination.business_id or None,
                        reply_markup=self.keyboard(job, bundle),
                    )
                except TelegramAPIError:
                    pass
