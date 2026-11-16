"""Manual download with the exact pipeline used by the Telegram bot."""

import argparse
import asyncio

from config import load_settings
from core import texts as T
from core.errors import MediaError
from services.http import PublicHttp
from services.links import LinkError
from services.pipeline import MediaPipeline
from services.runtime import validate_runtime
from storage.database import Database
from storage.lock import InstanceLock


async def download(url: str) -> None:
    settings = load_settings(require_token=False)
    validate_runtime(settings, require_token=False)
    with InstanceLock(settings.data_dir / "app.lock"):
        database = Database(settings.data_dir / "threaddock.sqlite3", settings.data_dir)
        try:
            async with PublicHttp() as http:
                pipeline = MediaPipeline(settings, database, http)
                pipeline.clean_work()
                bundle = await pipeline.fetch(url)
                print(T.DOWNLOAD_VIDEO_PATH.format(value1=bundle.directory / bundle.video.filename))
                print(
                    T.DOWNLOAD_PHOTO_SUMMARY.format(
                        value1=len(bundle.originals), value2=T.COMMON_PRESENT if bundle.audio else T.COMMON_ABSENT
                    )
                )
        finally:
            database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=T.DOWNLOAD_DOWNLOAD_DESCRIPTION)
    parser.add_argument("url")
    args = parser.parse_args()
    try:
        asyncio.run(download(args.url))
    except (MediaError, LinkError, ValueError, RuntimeError) as error:
        print(str(error))
        raise SystemExit(1)
    except KeyboardInterrupt:
        print(T.DOWNLOAD_DOWNLOAD_STOPPED)
        raise SystemExit(130)
