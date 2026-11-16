"""Convert only fields the Bot API actually delivered; never fetch old history."""

import difflib
import re
from pathlib import PurePath

from aiogram.types import Message


def clean_filename(name: str, fallback: str) -> str:
    name = PurePath(name.replace("\\", "/")).name
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name)[:160].strip(" .")
    return name or fallback


def message_payload(message: Message) -> dict:
    media = []
    if message.photo:
        item = message.photo[-1]
        media.append(
            {
                "file_id": item.file_id,
                "unique_id": item.file_unique_id,
                "kind": "photo",
                "filename": f"photo-{message.message_id}.jpg",
                "mime_type": "image/jpeg",
                "size": item.file_size or 0,
            }
        )
    for kind, extension, default_mime in (
        ("video", "mp4", "video/mp4"),
        ("animation", "mp4", "video/mp4"),
        ("document", "bin", "application/octet-stream"),
        ("audio", "m4a", "audio/mp4"),
        ("voice", "ogg", "audio/ogg"),
        ("video_note", "mp4", "video/mp4"),
        ("sticker", "webp", "image/webp"),
    ):
        item = getattr(message, kind, None)
        if item:
            name = getattr(item, "file_name", None) or f"{kind}-{message.message_id}.{extension}"
            media.append(
                {
                    "file_id": item.file_id,
                    "unique_id": item.file_unique_id,
                    "kind": kind,
                    "filename": clean_filename(name, f"file.{extension}"),
                    "mime_type": getattr(item, "mime_type", None) or default_mime,
                    "size": getattr(item, "file_size", None) or 0,
                }
            )
    body = message.text or message.caption or ""
    if message.contact:
        body = f"{message.contact.first_name} {message.contact.last_name or ''}\n{message.contact.phone_number}".strip()
    if message.location:
        body = f"📍 {message.location.latitude}, {message.location.longitude}"
    if message.poll:
        body = message.poll.question + "\n" + "\n".join(option.text for option in message.poll.options)
    return {
        "body": body,
        "media": media,
        "protected": bool(message.has_protected_content),
        "reply_to": message.reply_to_message.message_id if message.reply_to_message else None,
    }


def word_diff(before: str, after: str) -> list[dict]:
    a, b = re.findall(r"\s+|[^\s]+", before), re.findall(r"\s+|[^\s]+", after)
    result = []
    for tag, start_a, end_a, start_b, end_b in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag in {"equal", "delete", "replace"}:
            result.append({"kind": "same" if tag == "equal" else "removed", "text": "".join(a[start_a:end_a])})
        if tag in {"insert", "replace"}:
            result.append({"kind": "added", "text": "".join(b[start_b:end_b])})
    return result


def keyword_matches(text: str, keywords: list[str], exclusions: list[str], whole_words: bool) -> list[str]:
    if any(word.casefold() in text.casefold() for word in exclusions):
        return []
    if whole_words:
        return [word for word in keywords if re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.IGNORECASE)]
    return [word for word in keywords if word.casefold() in text.casefold()]
