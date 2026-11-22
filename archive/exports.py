"""Portable exports split into valid independent files below Telegram's limit."""

import asyncio
import csv
import html
import io
import json
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

from archive.content import clean_filename
from archive.repository import RETAINED_MESSAGE
from core import texts as T

FORMATS = {"json", "csv", "md", "html"}


def safe_csv(value) -> str:
    text = str(value if value is not None else "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _record(message, version, chat, media: dict) -> dict:
    payload = json.loads(version["payload"]) if version else {}
    return {
        "chat": chat["title"],
        "chat_id": chat["telegram_id"],
        "message_id": message["telegram_id"],
        "sent_at": message["sent_at"],
        "changed_at": version["changed_at"] if version else None,
        "deleted_at": message["deleted_at"],
        "outgoing": bool(message["outgoing"]),
        "favorite": bool(message["favorite"]),
        "text": version["body"] if version else message["body"],
        "attachments": [
            media[key] for item in payload.get("media", []) if (key := (item["kind"], item["unique_id"])) in media
        ],
    }


def _render(record: dict, format_name: str) -> str:
    if format_name == "json":
        return json.dumps(record, ensure_ascii=False)
    if format_name == "csv":
        buffer = io.StringIO(newline="")
        csv.writer(buffer).writerow(
            safe_csv(json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value)
            for value in record.values()
        )
        return buffer.getvalue()
    date = datetime.fromtimestamp(record["changed_at"] or record["sent_at"], UTC).isoformat()
    direction = "→" if record["outgoing"] else "←"
    state = " · DELETED" if record["deleted_at"] else ""
    if format_name == "md":
        title = str(record["chat"]).replace("\n", " ").replace("#", "\\#")
        quoted = "\n".join("> " + line for line in record["text"].splitlines())
        return f"### {title} · {date} {direction}{state}\n\n{quoted}\n\n"
    return f"<article><header>{html.escape(record['chat'])} · {date} {direction}{state}</header><pre>{html.escape(record['text'])}</pre></article>\n"


async def write_export(
    repository,
    owner_id: int,
    directory: Path,
    *,
    format_name: str = "json",
    chat_id: int = 0,
    max_part_bytes: int = 10_000_000,
) -> list[Path]:
    if format_name not in FORMATS:
        raise ValueError(T.ARCHIVE_EXPORTS_UNKNOWN_FORMAT)
    if chat_id and not repository.chat(owner_id, chat_id):
        raise LookupError("Chat not found")
    directory.mkdir(parents=True, exist_ok=True)
    messages = repository.db.execute(
        f"SELECT m.* FROM archive_messages m WHERE owner_id=? AND {RETAINED_MESSAGE} AND (?=0 OR chat_id=?) ORDER BY chat_id,sent_at,id",
        (owner_id, chat_id, chat_id),
    )
    files, file, size, first, part = [], None, 0, True, 0

    def close():
        if file:
            file.write("]}" if format_name == "json" else "</main>" if format_name == "html" else "")
            file.close()

    try:
        for index, message in enumerate(messages):
            chat = repository.chat(owner_id, message["chat_id"])
            versions = repository.db.execute(
                """SELECT changed_at,body,payload FROM archive_versions WHERE owner_id=? AND message_id=?
                ORDER BY changed_at,coalesce(json_extract(payload,'$.archive_edit'),1),id""",
                (owner_id, message["id"]),
            ).fetchall() or [None]
            media = {
                (row["kind"], row["unique_id"]): row["id"]
                for row in repository.db.execute(
                    "SELECT id,kind,unique_id FROM archive_media WHERE owner_id=? AND message_id=?",
                    (owner_id, message["id"]),
                )
            }
            for version in versions:
                record = _record(message, version, chat, media)
                rendered = _render(record, format_name)
                if file is None or size + len(rendered.encode()) > max_part_bytes:
                    close()
                    part += 1
                    path = directory / f"threaddock-archive-{part:03}.{format_name}"
                    files.append(path)
                    file = path.open("w", encoding="utf-8-sig" if format_name == "csv" else "utf-8", newline="")
                    header = (
                        '{"format":"ThreadDock archive v1","records":['
                        if format_name == "json"
                        else (
                            '<!doctype html><meta charset="utf-8"><title>ThreadDock archive</title><style>body{font:15px system-ui;background:#f2f4f8;margin:40px;color:#182237}main{max-width:900px;margin:auto}article{background:white;padding:20px;border-radius:16px;margin:12px 0}header{color:#607089}pre{white-space:pre-wrap;font:inherit}</style><main>'
                            if format_name == "html"
                            else ""
                        )
                    )
                    if format_name == "csv":
                        buffer = io.StringIO()
                        csv.writer(buffer).writerow(record.keys())
                        header = buffer.getvalue()
                    file.write(header)
                    size, first = len(header.encode()), True
                if format_name == "json" and not first:
                    file.write(",")
                file.write(rendered)
                first, size = False, size + len(rendered.encode()) + 1
            if index % 100 == 0:
                await asyncio.sleep(0)
        if file is None:
            path = directory / f"threaddock-archive-001.{format_name}"
            path.write_text(
                '{"format":"ThreadDock archive v1","records":[]}' if format_name == "json" else "", encoding="utf-8"
            )
            files.append(path)
    finally:
        close()
    return files


def bundle_export(records: list[Path], attachments: list[dict], limit: int, stopped) -> list[Path]:
    """Write independent ZIP parts, each with its own manifest."""
    files = []
    overhead = 65_536
    capacity = limit - overhead
    if capacity <= 0:
        raise ValueError("ZIP export limit is too small")
    manifest = []
    content_size = 0

    with ExitStack() as stack:
        archive = None

        def next_part():
            nonlocal archive, content_size
            if archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                stack.close()
            manifest.clear()
            content_size = 0
            target = records[0].parent / f"threaddock-{len(files) + 1:03}.zip"
            files.append(target)
            archive = stack.enter_context(ZipFile(target, "w", compression=ZIP_STORED))

        def ensure_space(size):
            if stopped.is_set():
                raise InterruptedError("Export stopped")
            if archive is None or content_size + size > capacity or len(manifest) >= 15:
                next_part()

        for record in records:
            size = record.stat().st_size
            ensure_space(size)
            archive.write(record, f"records/{record.name}")
            content_size += size
        for item in attachments:
            item = dict(item)
            path = item.pop("path")
            name = f"attachments/{item['id']}/{clean_filename(item['filename'], 'attachment')}"
            if path is None:
                ensure_space(0)
                manifest.append(item)
                continue
            try:
                remaining = path.stat().st_size
                source = path.open("rb")
            except OSError:
                ensure_space(0)
                manifest.append(item | {"status": "missing_local_file"})
                continue
            total = remaining
            with source:
                while remaining or total == 0:
                    size = min(remaining, capacity)
                    ensure_space(size)
                    offset = total - remaining
                    entry = name if size == total else f"{name}.part-{offset:012}"
                    with archive.open(entry, "w", force_zip64=True) as destination:
                        count = size
                        while count:
                            if stopped.is_set():
                                raise InterruptedError("Export stopped")
                            block = source.read(min(count, 1024 * 1024))
                            if not block:
                                raise OSError("Archive file truncated during export")
                            destination.write(block)
                            count -= len(block)
                    manifest.append(item | {"entry": entry, "offset": offset, "bytes": size, "total_bytes": total})
                    content_size += size
                    remaining -= size
                    if total == 0:
                        break
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return files
