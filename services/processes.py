"""Cancellable subprocesses with bounded output and process-tree termination."""

import asyncio
import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from core import texts as T
from core.errors import MediaError


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def worker_environment() -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "SSL_CERT_FILE"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(DENO_NO_UPDATE_CHECK="1", DENO_NO_PROMPT="1", PYTHONUTF8="1")
    environment["DENO_DIR"] = os.getenv("DENO_DIR", str(Path(tempfile.gettempdir()) / "threaddock-deno"))
    return environment


def directory_size(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            continue  # yt-dlp may rename a .part file during this scan.
    return total


def remove_directory(directory: Path, parent: Path) -> None:
    import shutil

    parent = parent.resolve()
    target = directory.resolve()
    if directory.is_symlink() or target == parent or not target.is_relative_to(parent):
        raise ValueError("Refusing to remove a directory outside its parent")
    if target.exists():
        shutil.rmtree(target)


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/taskkill.exe"
        killer = await asyncio.create_subprocess_exec(
            str(taskkill),
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            await asyncio.wait_for(killer.wait(), timeout=5)
        except TimeoutError:
            killer.kill()
            await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def _read_bounded(reader: asyncio.StreamReader, limit: int) -> bytes:
    output = bytearray()
    while chunk := await reader.read(65536):
        output.extend(chunk)
        if len(output) > limit:
            raise MediaError(T.SERVICES_PROCESSES_RESPONSE_TOO_LARGE, code="output_limit")
    return bytes(output)


async def run_process(
    command: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    output_limit: int = 8 * 1024**2,
    watch_directory: Path | None = None,
    max_disk_bytes: int | None = None,
) -> ProcessResult:
    flags = (
        {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=worker_environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **flags,
    )
    tasks = [
        asyncio.create_task(_read_bounded(process.stdout, output_limit)),
        asyncio.create_task(_read_bounded(process.stderr, output_limit)),
        asyncio.create_task(process.wait()),
    ]
    try:
        async with asyncio.timeout(timeout):
            while not all(task.done() for task in tasks):
                for task in tasks:
                    if task.done() and not task.cancelled() and task.exception():
                        raise task.exception()
                if watch_directory and max_disk_bytes and directory_size(watch_directory) > max_disk_bytes:
                    raise MediaError(T.SERVICES_PROCESSES_DOWNLOAD_TOO_LARGE, code="file_too_large")
                await asyncio.wait(tasks, timeout=0.2, return_when=asyncio.FIRST_EXCEPTION)
            stdout, stderr, returncode = await asyncio.gather(*tasks)
            if watch_directory and max_disk_bytes and directory_size(watch_directory) > max_disk_bytes:
                raise MediaError(T.SERVICES_PROCESSES_DOWNLOAD_TOO_LARGE, code="file_too_large")
            return ProcessResult(returncode, stdout, stderr)
    except TimeoutError as error:
        raise MediaError(T.SERVICES_PROCESSES_TIMEOUT, code="timeout", retryable=True) from error
    finally:
        await stop_process(process)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
