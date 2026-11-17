"""Message classification shared by ingestion and the downloader."""

MEDIA_FIELDS = ("photo", "video", "animation", "document", "audio", "voice", "video_note", "sticker", "paid_media")


def has_media(message) -> bool:
    return any(getattr(message, field, None) for field in MEDIA_FIELDS)


def is_disappearing(message) -> bool:
    # These flags are not present on every Bot API update. Spoilers do not imply TTL.
    values = [message, *(getattr(message, field, None) for field in MEDIA_FIELDS)]
    return any(
        getattr(value, "ttl_seconds", 0) or getattr(value, "is_view_once", False)
        for value in values
        if value is not None
    )
