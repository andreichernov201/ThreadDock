import tempfile
from pathlib import Path

from config import Settings
from core.models import Asset, Destination, MediaBundle
from services.links import normalize_url
from storage.database import Database


class DatabaseFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(
            token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            data_dir=self.root,
            min_free_bytes=1,
            max_download_bytes=1024**2,
        )
        self.database = Database(self.root / "test.sqlite3", self.root)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.database.close)

    def job(
        self, *, chat_id=10, requester_id=20, business_id="", mode="video", url="https://youtu.be/abc", message_id=1
    ):
        link = normalize_url(url)
        destination = Destination(chat_id, message_id, requester_id, business_id=business_id)
        self.database.enqueue(
            [(f"{business_id}:{chat_id}:{message_id}:{url}:{mode}", link.url, link.cache_key)], destination, mode, 100
        )
        return self.database.claim()

    def bundle(self, *, photos=0):
        directory = self.root / "cache" / "fixture"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "video.mp4").write_bytes(b"fixture-video")
        originals = []
        for index in range(photos):
            filename = f"original-{index}.jpg"
            (directory / filename).write_bytes(b"fixture-image")
            originals.append(Asset(filename, "document"))
        return MediaBundle(
            directory,
            "https://youtu.be/abc",
            "YouTube",
            "Example",
            Asset("video.mp4", "video", 1, 320, 240),
            tuple(originals),
        )
