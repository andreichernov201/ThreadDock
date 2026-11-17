from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

DeliveryMode = Literal["video", "album", "photos", "audio"]


@dataclass(frozen=True)
class Destination:
    chat_id: int
    message_id: int
    requester_id: int
    thread_id: int | None = None
    business_id: str = ""


@dataclass(frozen=True)
class Job:
    id: int
    url: str
    cache_key: str
    destination: Destination
    mode: DeliveryMode
    attempts: int
    progress: int = 0


@dataclass(frozen=True)
class Asset:
    filename: str
    kind: Literal["video", "photo", "live_photo", "document", "audio"]
    duration: float = 0
    width: int = 0
    height: int = 0
    photo_filename: str = ""

    @property
    def parts(self) -> tuple[Asset, ...]:
        return (self, Asset(self.photo_filename, "photo")) if self.photo_filename else (self,)

    def path(self, directory: Path) -> Path:
        if Path(self.filename).name != self.filename or self.filename in {"", ".", ".."}:
            raise ValueError("Invalid asset filename")
        result = directory / self.filename
        if result.is_symlink() or result.resolve().parent != directory.resolve():
            raise ValueError("Asset escapes its directory")
        return result


@dataclass(frozen=True)
class MediaBundle:
    directory: Path
    source_url: str
    platform: str
    title: str
    video: Asset | None
    originals: tuple[Asset, ...] = ()
    audio: Asset | None = None
    has_audio: bool = True
    media: tuple[Asset, ...] = ()
    motion_pending: bool = False

    @property
    def assets(self) -> tuple[Asset, ...]:
        items = (
            ((self.video,) if self.video else ()) + self.originals + self.media + ((self.audio,) if self.audio else ())
        )
        return tuple(dict.fromkeys(part for asset in items for part in asset.parts))

    def items(self, mode: DeliveryMode) -> tuple[Asset, ...]:
        if mode == "photos":
            items = self.originals
        elif mode == "audio":
            items = (self.audio,) if self.audio else ()
        elif mode == "album" and self.originals and not self.media:
            items = self.originals
        else:
            items = self.media or ((self.video,) if self.video else self.originals)
        # Some clients show native Live Photo messages as stills despite Telegram
        # storing their moving track. Deliver that full track as a regular video;
        # keep the original pair in the bundle for retention and serialization.
        return tuple(
            replace(asset, kind="video", photo_filename="") if asset.kind == "live_photo" else asset for asset in items
        )

    def serialize(self, data_root: Path) -> str:
        data = asdict(self)
        data["directory"] = self.directory.resolve().relative_to(data_root.resolve()).as_posix()
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def deserialize(cls, payload: str, data_root: Path) -> MediaBundle:
        data = json.loads(payload)
        directory = (data_root / data.pop("directory")).resolve()
        if not directory.is_relative_to(data_root.resolve()) or directory == data_root.resolve():
            raise ValueError("Invalid cache directory")
        data["video"] = Asset(**data["video"]) if data["video"] else None
        data["originals"] = tuple(Asset(**item) for item in data["originals"])
        data["audio"] = Asset(**data["audio"]) if data["audio"] else None
        data["media"] = tuple(Asset(**item) for item in data.get("media", ()))
        return cls(directory=directory, **data)
