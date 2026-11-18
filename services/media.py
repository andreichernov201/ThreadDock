"""Local-only FFmpeg operations: probe, compatible MP4, audio and slideshows."""

import json
import math
from dataclasses import dataclass
from pathlib import Path

from config import Settings
from core import texts as T
from core.errors import MediaError
from core.models import Asset
from services.processes import run_process


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    video_codec: str
    audio_codec: str


def parse_probe(payload: bytes) -> MediaInfo:
    try:
        data = json.loads(payload)
        streams = data.get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
        duration = float(data.get("format", {}).get("duration") or video.get("duration") or audio.get("duration") or 0)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError
        return MediaInfo(
            duration,
            int(video.get("width", 0)),
            int(video.get("height", 0)),
            video.get("codec_name", ""),
            audio.get("codec_name", ""),
        )
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise MediaError(T.SERVICES_MEDIA_PROBE_FAILED, code="invalid_media") from error


class MediaTools:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def probe(self, path: Path) -> MediaInfo:
        result = await run_process(
            [
                self.settings.ffprobe,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe,crypto",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height,duration",
                "-of",
                "json",
                str(path),
            ],
            timeout=30,
            output_limit=1024**2,
        )
        if result.returncode:
            raise MediaError(T.SERVICES_MEDIA_INVALID_FILE, code="invalid_media")
        return parse_probe(result.stdout)

    async def ffmpeg(self, arguments: list[str], directory: Path) -> None:
        try:
            result = await run_process(
                [
                    self.settings.ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-threads",
                    "2",
                    "-filter_threads",
                    "1",
                    *arguments,
                ],
                timeout=self.settings.process_timeout,
                cwd=directory,
                watch_directory=directory,
                max_disk_bytes=self.settings.max_download_bytes * 4,
            )
        except MediaError as error:
            if error.code == "timeout":
                raise MediaError(T.SERVICES_MEDIA_PREPARATION_FAILED, code="processing_timeout") from error
            raise
        if result.returncode:
            raise MediaError(T.SERVICES_MEDIA_PREPARATION_FAILED, code="conversion_failed")

    def _fit_options(self, info: MediaInfo) -> list[str]:
        budget = int(self.settings.max_upload_bytes * 8 * 0.90 / info.duration)
        audio_bitrate = (128000 if budget >= 400000 else 64000) if info.audio_codec else 0
        bitrate = budget - audio_bitrate
        if bitrate < 80000:
            raise MediaError(T.SERVICES_MEDIA_VIDEO_TOO_LONG, code="file_too_large")
        scale = (
            "fps=24,scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=fast_bilinear,setsar=1"
            if bitrate < 400000
            else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        )
        return [
            "-vf",
            scale,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast" if bitrate < 400000 else "veryfast",
            "-threads",
            "2",
            "-b:v",
            str(bitrate),
            "-maxrate",
            str(bitrate),
            "-bufsize",
            str(bitrate * 2),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            str(audio_bitrate or 64000),
        ]

    async def compatible_video(self, source: Path, directory: Path) -> tuple[Asset, bool]:
        info = await self.probe(source)
        if not info.video_codec or not 0 < info.duration <= self.settings.max_duration:
            raise MediaError(T.SERVICES_MEDIA_NO_VIDEO_OR_DURATION, code="duration_limit")
        target = directory / "video.mp4"
        compatible = info.video_codec == "h264" and info.audio_codec in {"", "aac"}
        arguments = [
            "-protocol_whitelist",
            "file,pipe,crypto",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-sn",
            "-dn",
        ]
        if source.stat().st_size > self.settings.max_upload_bytes:
            # Fit oversized sources in the first encode instead of converting twice.
            arguments += self._fit_options(info)
        elif compatible:
            arguments += ["-c", "copy"]
        else:
            arguments += [
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
            ]
        await self.ffmpeg(arguments + ["-movflags", "+faststart", str(target)], directory)
        if target.stat().st_size > self.settings.max_upload_bytes:
            smaller = directory / "fitted.mp4"
            await self.ffmpeg(
                [
                    "-protocol_whitelist",
                    "file,pipe,crypto",
                    "-i",
                    str(target),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    *self._fit_options(info),
                    "-movflags",
                    "+faststart",
                    str(smaller),
                ],
                directory,
            )
            smaller.replace(target)
        verified = await self.probe(target)
        if (
            not verified.video_codec
            or verified.video_codec != "h264"
            or (info.audio_codec and not verified.audio_codec)
            or target.stat().st_size > self.settings.max_upload_bytes
        ):
            raise MediaError(T.SERVICES_MEDIA_COMPATIBLE_VIDEO_FAILED, code="invalid_output")
        # A readable container header alone does not establish that its frames decode.
        await self.ffmpeg(
            [
                "-xerror",
                "-protocol_whitelist",
                "file,pipe,crypto",
                "-i",
                str(target),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-f",
                "null",
                "-",
            ],
            directory,
        )
        return Asset(target.name, "video", verified.duration, verified.width, verified.height), bool(
            verified.audio_codec
        )

    async def extract_audio(self, source: Path, directory: Path) -> Asset | None:
        info = await self.probe(source)
        if not info.audio_codec:
            return None
        target = directory / "audio.m4a"
        await self.ffmpeg(
            [
                "-protocol_whitelist",
                "file,pipe,crypto",
                "-i",
                str(source),
                "-vn",
                "-c:a",
                "copy" if info.audio_codec == "aac" else "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(target),
            ],
            directory,
        )
        if target.stat().st_size > self.settings.max_upload_bytes:
            raise MediaError(T.SERVICES_MEDIA_AUDIO_TOO_LARGE, code="file_too_large")
        return Asset(target.name, "audio", info.duration)

    async def photo(self, source: Path, directory: Path) -> Asset:
        info = await self.probe(source)
        if not info.video_codec or min(info.width, info.height) <= 0 or info.width * info.height > 80_000_000:
            raise MediaError(T.SERVICES_TIKTOK_INVALID_IMAGE, code="invalid_image")
        suitable = (
            source.suffix.lower() in {".jpg", ".jpeg", ".png"}
            and source.stat().st_size <= 10_000_000
            and info.width + info.height <= 10000
            and max(info.width, info.height) / min(info.width, info.height) <= 20
        )
        target = source
        if not suitable:
            target = directory / (source.stem + "-photo.jpg")
            await self.ffmpeg(
                [
                    "-protocol_whitelist",
                    "file,pipe,crypto",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=4096:4096:force_original_aspect_ratio=decrease,"
                    "pad=max(iw\\,ceil(ih/20)):max(ih\\,ceil(iw/20)):(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-q:v",
                    "2",
                    str(target),
                ],
                directory,
            )
            info = await self.probe(target)
        if target.stat().st_size > min(10_000_000, self.settings.max_upload_bytes):
            raise MediaError(T.SERVICES_DELIVERY_FILE_TOO_LARGE, code="file_too_large")
        return Asset(target.name, "photo", width=info.width, height=info.height)

    async def slideshow(
        self, images: list[Path], audio: Path | None, directory: Path, *, motion: dict[int, Path] | None = None
    ) -> tuple[Asset, bool]:
        slide_duration = 3.0
        if motion:
            durations = [(await self.probe(path)).duration for path in motion.values()]
            if not all(math.isfinite(value) and value > 0 for value in durations):
                raise MediaError(T.SERVICES_MEDIA_INVALID_SLIDESHOW_DURATION, code="duration_limit")
            # Every slide follows the longest live photo, with a two-second floor.
            slide_duration = max(2.0, *durations)
        duration = len(images) * slide_duration
        if audio and len(images) == 1 and not motion:
            duration = min(duration, (await self.probe(audio)).duration)
        if not images or not 0 < duration <= self.settings.max_duration:
            raise MediaError(T.SERVICES_MEDIA_INVALID_SLIDESHOW_DURATION, code="duration_limit")
        if motion:
            return await self._motion_slideshow(images, motion, audio, directory, duration)
        frames: list[str] = []
        for index, image in enumerate(images):
            frame = f"frame-{index:03}.jpg"
            await self.ffmpeg(
                [
                    "-protocol_whitelist",
                    "file,pipe,crypto",
                    "-i",
                    str(image),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-q:v",
                    "2",
                    frame,
                ],
                directory,
            )
            frames.append(frame)
        lines = [f"file '{frame}'\nduration {duration / len(frames):.9f}" for frame in frames]
        lines.append(f"file '{frames[-1]}'")
        (directory / "slides.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        raw = directory / "slideshow.mp4"
        args = ["-protocol_whitelist", "file,pipe,crypto", "-f", "concat", "-safe", "1", "-i", "slides.txt"]
        if audio:
            args += [
                "-protocol_whitelist",
                "file,pipe,crypto",
                "-stream_loop",
                "-1",
                "-i",
                str(audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
            ]
        args += [
            "-vf",
            "fps=30,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            str(duration),
            "-movflags",
            "+faststart",
            str(raw),
        ]
        await self.ffmpeg(args, directory)
        return await self.compatible_video(raw, directory)

    async def _motion_slideshow(
        self, images: list[Path], motion: dict[int, Path], audio: Path | None, directory: Path, duration: float
    ) -> tuple[Asset, bool]:
        segments = []
        for index, image in enumerate(images):
            name = f"slide-{index:03}.mp4"
            source = motion.get(index, image)
            loop = ["-stream_loop", "-1"] if index in motion else ["-loop", "1"]
            await self.ffmpeg(
                [
                    "-protocol_whitelist",
                    "file,pipe,crypto",
                    *loop,
                    "-i",
                    str(source),
                    "-t",
                    str(duration / len(images)),
                    "-an",
                    "-vf",
                    "scale=1080:1920:force_original_aspect_ratio=decrease,"
                    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "20",
                    name,
                ],
                directory,
            )
            segments.append(f"file '{name}'")
        (directory / "motion-slides.txt").write_text("\n".join(segments) + "\n", encoding="utf-8")
        raw = directory / "slideshow.mp4"
        args = ["-protocol_whitelist", "file,pipe,crypto", "-f", "concat", "-safe", "1", "-i", "motion-slides.txt"]
        if audio:
            args += [
                "-protocol_whitelist",
                "file,pipe,crypto",
                "-stream_loop",
                "-1",
                "-i",
                str(audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
            ]
        args += ["-c:v", "copy", "-t", str(duration), "-movflags", "+faststart", str(raw)]
        await self.ffmpeg(args, directory)
        return await self.compatible_video(raw, directory)
