import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from core.errors import MediaError
from services.processes import remove_directory, run_process, worker_environment
from tests.support import DatabaseFixture


class FakeProcess:
    def __init__(self, stdout=b"output", stderr=b"", delay=0, code=0):
        self.returncode = None
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.delay, self.code = delay, code

    async def wait(self):
        await asyncio.sleep(self.delay)
        self.returncode = self.code
        return self.code


class ProcessTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def test_output_and_exit_status_are_returned(self):
        process = FakeProcess(stderr=b"diagnostic", code=3)
        with patch("services.processes.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with patch("services.processes.stop_process", AsyncMock()):
                result = await run_process(["program"], timeout=1)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, b"output", b"diagnostic"))

    async def test_timeout_stops_process(self):
        process = FakeProcess(delay=60)
        with patch("services.processes.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with patch("services.processes.stop_process", AsyncMock()) as stop:
                with self.assertRaisesRegex(MediaError, "время"):
                    await run_process(["program"], timeout=0.01)
                stop.assert_awaited_once_with(process)

    async def test_cancellation_stops_process(self):
        process = FakeProcess(delay=60)
        with patch("services.processes.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as launch:
            with patch("services.processes.stop_process", AsyncMock()) as stop:
                task = asyncio.create_task(run_process(["program"], timeout=60))
                while not launch.called:
                    await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                stop.assert_awaited_once_with(process)

    async def test_output_limit_stops_noisy_process(self):
        process = FakeProcess(stdout=b"x" * 1024)
        with patch("services.processes.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with patch("services.processes.stop_process", AsyncMock()):
                with self.assertRaisesRegex(MediaError, "большой ответ"):
                    await run_process(["program"], timeout=1, output_limit=100)

    async def test_disk_limit_stops_unknown_length_download(self):
        process = FakeProcess(delay=60)
        (self.root / "part").write_bytes(b"x" * 1024)
        with patch("services.processes.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with patch("services.processes.stop_process", AsyncMock()):
                with self.assertRaisesRegex(MediaError, "размер"):
                    await run_process(["program"], timeout=1, watch_directory=self.root, max_disk_bytes=100)

    def test_cleanup_rejects_parent_or_outside_directory(self):
        with self.assertRaises(ValueError):
            remove_directory(self.root, self.root)
        with self.assertRaises(ValueError):
            remove_directory(self.root.parent, self.root)

    def test_worker_does_not_inherit_tokens_or_proxy_bypass(self):
        with patch.dict(os.environ, {"BOT_TOKEN": "secret", "NO_PROXY": "*", "HTTPS_PROXY": "http://other"}):
            environment = worker_environment()
        self.assertNotIn("BOT_TOKEN", environment)
        self.assertNotIn("NO_PROXY", environment)
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertIn("DENO_DIR", environment)
