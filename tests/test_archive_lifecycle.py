import asyncio
import json
import os
import tarfile
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

from aiohttp.test_utils import TestClient, TestServer

from archive.config import PREFETCH_TTL
from archive.exports import bundle_export
from archive.service import ArchiveService
from archive.web import create_web_app
from scripts.backup import backup
from storage.database import Database
from tests.test_archive import ArchiveFixture, signed_data


class PrefetchLifecycleTests(ArchiveFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.bot.download.side_effect = lambda name, *, destination, **_: destination.write(name.encode())

    def media_row(self, message_id):
        return self.repo.db.execute("SELECT * FROM archive_media WHERE message_id=?", (message_id,)).fetchone()

    def cached_row(self, media_id):
        return self.repo.db.execute("SELECT * FROM archive_media_cache WHERE media_id=?", (media_id,)).fetchone()

    async def test_ordinary_copy_expires_but_late_deletion_can_download_again(self):
        message = self.ingest(media=[self.photo("file")])
        media = self.media_row(message)
        await self.service._process(self.repo.claim())
        path = self.service.prefetch_cache / "10" / media["id"]
        cached = self.cached_row(media["id"])
        self.assertEqual(path.read_bytes(), b"file")
        self.assertAlmostEqual(cached["expires_at"], time.time() + PREFETCH_TTL, delta=2)
        self.assertIsNone(self.media_row(message)["local_path"])
        self.assertIsNone(self.repo.media(10, media["id"]))
        with patch("archive.service.time.time", return_value=cached["expires_at"] + 1):
            self.service.maintain()
            self.assertFalse(path.exists())
            self.assertIsNone(self.cached_row(media["id"]))
            self.assertEqual(self.media_row(message)["file_id"], "file")
            self.assertEqual(self.repo.message(10, message)["body"], "Original text")
            self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat")
            await self.service._process(self.repo.claim())
        self.assertEqual(self.bot.download.await_count, 2)
        local = self.service.safe_path(self.repo.media(10, media["id"])["local_path"], self.service.blobs)
        self.assertEqual(local.read_bytes(), b"file")

    async def test_event_pins_live_copy_until_promotion_even_past_the_deadline(self):
        message = self.ingest(media=[self.photo("file")])
        media = self.media_row(message)
        self.service._copy_to_vault = AsyncMock()
        with self.repo.db:
            self.repo.db.execute("UPDATE archive_owners SET vault_chat_id=-100 WHERE owner_id=10")
        await self.service._process(self.repo.claim())
        self.service._copy_to_vault.assert_not_awaited()
        deadline = self.cached_row(media["id"])["expires_at"]
        with patch("archive.service.time.time", return_value=deadline - 1):
            self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat")
        self.assertEqual(self.cached_row(media["id"])["pinned"], 1)
        with patch("archive.service.time.time", return_value=deadline + 1):
            self.service.maintain()
        self.assertIsNone(self.cached_row(media["id"]))
        self.assertFalse((self.service.prefetch_cache / "10" / media["id"]).exists())
        self.assertEqual((self.service.blobs / "10" / media["id"]).read_bytes(), b"file")
        self.assertEqual(self.repo.media(10, media["id"])["status"], "ready")
        with patch("archive.service.time.time", return_value=deadline + 1):
            await self.service._process(self.repo.claim())
        self.service._copy_to_vault.assert_awaited_once()
        self.bot.download.assert_awaited_once()

    async def test_deletion_during_download_moves_completed_file_directly_into_archive(self):
        message = self.ingest(media=[self.photo("file")])
        media = self.media_row(message)
        started = asyncio.Event()
        finish = asyncio.Event()

        async def download(*_, destination, **__):
            started.set()
            await finish.wait()
            destination.write(b"file")

        self.bot.download.side_effect = download
        download_task = asyncio.create_task(self.service._process(self.repo.claim()))
        try:
            await asyncio.wait_for(started.wait(), 2)
            self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat")
        finally:
            finish.set()
            await download_task
        self.assertIsNone(self.cached_row(media["id"]))
        self.assertEqual((self.service.blobs / "10" / media["id"]).read_bytes(), b"file")
        self.assertEqual(list((self.service.prefetch_cache / "10").iterdir()), [])

    async def test_restart_can_recover_an_interrupted_promotion(self):
        message = self.ingest(media=[self.photo("file")])
        media = self.media_row(message)
        await self.service._process(self.repo.claim())
        self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat")
        local = self.service.blobs / "10" / media["id"]
        local.parent.mkdir()
        (self.service.prefetch_cache / "10" / media["id"]).replace(local)
        self.assertIsNone(self.media_row(message)["local_path"])
        self.service._maintain_prefetch_cache(time.time(), reconcile=True)
        self.assertEqual(
            self.repo.media(10, media["id"])["local_path"], local.relative_to(self.service.root).as_posix()
        )
        self.assertIsNone(self.cached_row(media["id"]))
        self.bot.download.assert_awaited_once()

    async def test_expired_copy_is_not_used_before_the_cleanup_worker_runs(self):
        message = self.ingest(media=[self.photo("file")])
        media = self.media_row(message)
        await self.service._process(self.repo.claim())
        deadline = self.cached_row(media["id"])["expires_at"]
        with patch("archive.service.time.time", return_value=deadline + 1):
            self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat")
            self.assertEqual(self.cached_row(media["id"])["pinned"], 0)
            await self.service._process(self.repo.claim())
        self.assertEqual(self.bot.download.await_count, 2)
        self.assertFalse((self.service.prefetch_cache / "10" / media["id"]).exists())

    async def test_archive_can_evict_an_ordinary_copy_when_the_budget_is_full(self):
        self.service.config = replace(self.service.config, storage_bytes=4)
        message = self.ingest(media=[self.photo("temp")])
        await self.service._process(self.repo.claim())
        media = self.media_row(message)
        self.assertIsNotNone(self.cached_row(media["id"]))
        archived = self.ingest(owner=20, archived=True, media=[self.photo("keep")])
        await self.service._process(self.repo.claim())
        self.assertEqual((self.service.blobs / "20" / self.media_row(archived)["id"]).read_bytes(), b"keep")
        self.assertIsNone(self.cached_row(media["id"]))
        self.assertEqual(self.media_row(message)["file_id"], "temp")

    def test_legacy_files_keep_their_original_age_when_moved_to_cache(self):
        now = time.time()
        recent = self.media_row(self.ingest(media=[self.photo("new")]))
        expired = self.media_row(self.ingest(telegram_id=2, media=[self.photo("old")]))
        for media, age in ((recent, 600), (expired, PREFETCH_TTL + 600)):
            path = self.service.blobs / "10" / media["id"]
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"file")
            os.utime(path, (now - age, now - age))
            with self.repo.db:
                self.repo.db.execute(
                    "UPDATE archive_media SET local_path=? WHERE id=?",
                    (path.relative_to(self.service.root).as_posix(), media["id"]),
                )
        with self.repo.db:
            self.repo.db.execute("DROP TABLE archive_media_cache")
        upgraded = ArchiveService(self.settings, self.database, self.bot)
        upgraded._migrate_legacy_prefetch()
        upgraded._migrate_legacy_prefetch()
        self.assertEqual((self.service.prefetch_cache / "10" / recent["id"]).read_bytes(), b"file")
        self.assertAlmostEqual(self.cached_row(recent["id"])["expires_at"], now - 600 + PREFETCH_TTL, delta=1)
        self.assertIsNone(self.cached_row(expired["id"]))
        self.assertEqual(list((self.service.blobs / "10").iterdir()), [])


class ArchiveRestorationTests(ArchiveFixture, unittest.IsolatedAsyncioTestCase):
    async def test_full_backup_restores_text_versions_search_and_archive_file(self):
        at = int(time.time()) - 100
        attachment = self.photo(filename="Contract.pdf")
        message = self.ingest(at=at, body="Original draft", media=[attachment])
        self.ingest(at=at, changed=at + 20, body="Updated contract", media=[attachment], archived=True)
        self.bot.download.side_effect = lambda *_, destination, **__: destination.write(b"file")
        media = self.repo.message(10, message)["media"][0]["id"]
        await self.service._save_media({"kind": "media", "payload": {"media_id": media}}, self.repo.owner(10))
        staged = self.ingest(telegram_id=2, media=[self.photo("temporary")])
        cached = self.repo.db.execute("SELECT id FROM archive_media WHERE message_id=?", (staged,)).fetchone()[0]
        await self.service._save_media({"kind": "prefetch", "payload": {"media_id": cached}}, self.repo.owner(10))
        self.database.close()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            copy = root / "backup.tar.gz"
            backup(self.root, copy)
            restored_root = root / "restored"
            with tarfile.open(copy) as archive:
                self.assertNotIn(f"archive/cache/10/{cached}", archive.getnames())
                archive.extractall(restored_root, filter="data")
            restored_db = Database(restored_root / "test.sqlite3", restored_root)
            try:
                service = ArchiveService(replace(self.settings, data_dir=restored_root), restored_db, self.bot)
                service._maintain_prefetch_cache(time.time(), reconcile=True)
                restored = service.repository
                self.assertEqual(
                    [item["body"] for item in restored.message(10, message)["versions"]],
                    ["Original draft", "Updated contract"],
                )
                self.assertEqual(restored.search(10, "Original")["items"][0]["id"], message)
                self.assertEqual(restored.search(10, "Contract.pdf")["items"][0]["id"], message)
                local = service.safe_path(restored.media(10, media)["local_path"], service.blobs)
                self.assertEqual(local.read_bytes(), b"file")
                self.assertIsNone(restored.db.execute("SELECT 1 FROM archive_media_cache").fetchone())
                self.assertIsNone(restored.media(10, cached))
            finally:
                restored_db.close()

    def test_split_zip_parts_fit_limit_and_reassemble_original_file(self):
        original = bytes(range(256)) * 384
        source = self.root / "original.mp4"
        source.write_bytes(original)
        records = self.root / "records.json"
        records.write_text('{"records":[]}', encoding="utf-8")
        item = {"id": "a" * 32, "filename": source.name, "path": source, "status": "included"}
        missing = {"id": "b" * 32, "filename": "missing.jpg", "path": None, "status": "remote"}
        limit = 90_000
        parts = bundle_export([records], [item, missing], limit, Event())
        self.assertGreater(len(parts), 2)
        assembled = []
        manifest = []
        for part in parts:
            self.assertLessEqual(part.stat().st_size, limit)
            with ZipFile(part) as bundle:
                self.assertIsNone(bundle.testzip())
                for entry in json.loads(bundle.read("manifest.json")):
                    manifest.append(entry)
                    if entry.get("entry"):
                        data = bundle.read(entry["entry"])
                        self.assertEqual(len(data), entry["bytes"])
                        assembled.append((entry["offset"], data))
        offset = 0
        for start, data in sorted(assembled):
            self.assertEqual(start, offset)
            self.assertEqual(data, original[start : start + len(data)])
            offset += len(data)
        self.assertEqual(offset, len(original))
        self.assertEqual([entry["status"] for entry in manifest if entry["id"] == missing["id"]], ["remote"])


class ArchiveIsolationTests(ArchiveFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_web_app(self.service)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.sessions = {}
        for owner in (10, 20):
            response = await self.client.post(
                "/api/auth", json={"init_data": signed_data(self.settings.token, user_id=owner)}
            )
            self.assertEqual(response.status, 200)
            auth = await response.json()
            self.assertEqual(auth["user"]["id"], owner)
            self.sessions[owner] = {"Authorization": "Bearer " + auth["token"], "X-CSRF-Token": auth["csrf"]}

    async def test_two_signed_in_owners_have_independent_archives_and_deletion(self):
        self.bot.download.side_effect = lambda name, *, destination, **_: destination.write(name.encode())
        a = self.ingest(owner=10, body="Alpha record", media=[self.photo("AAAA")], archived=True)
        b = self.ingest(owner=20, body="Bravo record", media=[self.photo("BBBB")], archived=True)
        self.ingest(owner=20, telegram_id=2, body="Bravo extra", archived=True)
        owners = {10: self.repo.message(10, a), 20: self.repo.message(20, b)}
        for owner, message in owners.items():
            media = message["media"][0]["id"]
            await self.service._save_media({"kind": "media", "payload": {"media_id": media}}, self.repo.owner(owner))

        jobs = {}
        for owner, foreign in ((10, 20), (20, 10)):
            headers = self.sessions[owner]
            own, other = owners[owner], owners[foreign]
            stats = await self.client.get("/api/stats", headers=headers)
            self.assertEqual((await stats.json())["totals"]["messages"], 1 if owner == 10 else 2)
            response = await self.client.get(f"/api/media/{own['media'][0]['id']}/file", headers=headers)
            self.assertEqual(await response.read(), b"AAAA" if owner == 10 else b"BBBB")
            for denied in (
                f"/api/messages/{other['id']}",
                f"/api/media/{other['media'][0]['id']}/file",
                f"/api/stats?chat={other['chat_id']}",
                f"/api/chats/{other['chat_id']}/messages",
            ):
                self.assertEqual((await self.client.get(denied, headers=headers)).status, 404)
            response = await self.client.post("/api/export", headers=headers, json={"chat_id": other["chat_id"]})
            self.assertEqual(response.status, 404)
            response = await self.client.post("/api/export", headers=headers, json={"chat_id": own["chat_id"]})
            self.assertEqual(response.status, 202)
            jobs[owner] = (await response.json())["task_id"]

        for owner, foreign in ((10, 20), (20, 10)):
            own = await self.client.get(f"/api/tasks/{jobs[owner]}", headers=self.sessions[owner])
            other = await self.client.get(f"/api/tasks/{jobs[foreign]}", headers=self.sessions[owner])
            self.assertEqual(own.status, 200)
            self.assertEqual(other.status, 404)

        exports = {}

        async def send_document(owner, document, **_):
            with ZipFile(document.path) as bundle:
                manifest = json.loads(bundle.read("manifest.json"))
                exports[owner] = (
                    bundle.read("records/threaddock-archive-001.json"),
                    [bundle.read(entry["entry"]) for entry in manifest],
                )

        self.bot.send_document = AsyncMock(side_effect=send_document)
        with self.repo.db:
            self.repo.db.execute("UPDATE archive_tasks SET available_at=? WHERE kind!='export'", (time.time() + 60,))
        for _ in jobs:
            await self.service._process(self.repo.claim())
        self.assertIn(b"Alpha record", exports[10][0])
        self.assertNotIn(b"Bravo record", exports[10][0])
        self.assertIn(b"Bravo record", exports[20][0])
        self.assertNotIn(b"Alpha record", exports[20][0])
        self.assertEqual(exports[10][1], [b"AAAA"])
        self.assertEqual(exports[20][1], [b"BBBB"])

        cache_paths = {}
        for owner, label in ((10, "aaaa"), (20, "bbbb")):
            ordinary = self.ingest(owner=owner, telegram_id=9, media=[self.photo(label)])
            media = self.repo.db.execute("SELECT id FROM archive_media WHERE message_id=?", (ordinary,)).fetchone()[0]
            await self.service._save_media({"kind": "prefetch", "payload": {"media_id": media}}, self.repo.owner(owner))
            cache_paths[owner] = self.service.prefetch_cache / str(owner) / media
            self.assertEqual(cache_paths[owner].read_bytes(), label.encode())

        response = await self.client.post(
            "/api/delete-me",
            headers=self.sessions[10],
            json={"confirmation": "УДАЛИТЬ"},
        )
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.repo.message(10, a))
        self.assertIsNone(self.repo.task(10, jobs[10]))
        self.assertEqual((await self.client.get("/api/stats", headers=self.sessions[10])).status, 401)
        stats = await self.client.get("/api/stats", headers=self.sessions[20])
        self.assertEqual((await stats.json())["totals"]["messages"], 3)
        self.assertFalse(cache_paths[10].exists())
        self.assertEqual(cache_paths[20].read_bytes(), b"bbbb")
        surviving_media = owners[20]["media"][0]["id"]
        response = await self.client.get(f"/api/media/{surviving_media}/file", headers=self.sessions[20])
        self.assertEqual(await response.read(), b"BBBB")
        self.assertEqual((await self.client.get(f"/api/messages/{b}", headers=self.sessions[20])).status, 200)
        self.assertEqual((await self.client.get(f"/api/tasks/{jobs[20]}", headers=self.sessions[20])).status, 200)


class ArchiveEventReliabilityTests(ArchiveFixture, unittest.IsolatedAsyncioTestCase):
    async def test_out_of_order_updates_still_display_in_event_order(self):
        at = int(time.time()) - 100
        message = self.ingest(at=at, changed=at + 30, body="Last edit")
        self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat")
        self.ingest(at=at, changed=at + 20, body="First edit")
        self.ingest(at=at, body="Original")
        items = []
        cursor, before = 0, None
        while True:
            page = self.repo.events(10, cursor=cursor, before=before, limit=1)
            items.extend((row["kind"], row["body"]) for row in page["items"])
            cursor = page["next_cursor"]
            before = page["next_before"]
            if cursor is None:
                break
        self.assertEqual(items, [("deleted", "Last edit"), ("edited", "Last edit"), ("edited", "First edit")])
        self.assertEqual(
            [row["body"] for row in self.repo.message(10, message)["versions"]],
            ["Original", "First edit", "Last edit"],
        )

    async def test_search_filters_past_versions_and_attachment_names(self):
        at = int(time.time()) - 100
        original = self.ingest(at=at, body="Archived quote", media=[self.photo(filename="Budget_original.pdf")])
        self.ingest(at=at, changed=at + 10, body="Replacement")
        self.ingest(telegram_id=2, body="Archived quote", media=[self.photo(filename="Budget_ordinary.pdf")])
        self.ingest(owner=20, body="Archived quote", media=[self.photo(filename="Budget_foreign.pdf")], archived=True)
        chat = self.repo.message(10, original)["chat_id"]
        found = self.repo.search(10, "archived quote", chat_id=chat, mode="edited")["items"]
        self.assertEqual([(row["id"], row["match_text"]) for row in found], [(original, "Archived quote")])
        by_filename = self.repo.search(10, "Budget pdf", chat_id=chat)["items"]
        self.assertEqual(
            [(row["id"], row["match_filenames"]) for row in by_filename], [(original, "Budget_original.pdf")]
        )
        self.assertEqual(self.repo.search(10, "Budget", mode="deleted")["items"], [])

    async def test_hidden_edit_notices_use_only_the_event_and_chat(self):
        at = int(time.time()) - 100
        self.ingest(at=at, body="Private original")
        self.ingest(at=at, changed=at + 30, body="Private replacement")
        self.ingest(telegram_id=2, at=at, changed=at + 15, body="Private first edit")
        for _ in range(2):
            await self.service._process(self.repo.claim())
        known, unknown = (call.args[1] for call in self.bot.send_message.await_args_list)
        self.assertEqual(known, "<b>✏️ Сообщение изменено</b>\nTest chat")
        self.assertEqual(unknown, known)

    async def test_profile_event_stays_in_archive_without_a_chat_notice(self):
        self.repo.update_owner(10, {"notify_profiles": True})
        self.ingest()
        self.ingest(telegram_id=2, title="New name")
        event = next(row for row in self.repo.events(10)["items"] if row["kind"] == "profile")
        tasks = [
            json.loads(row["payload"])
            for row in self.repo.db.execute("SELECT payload FROM archive_tasks WHERE kind='notify'")
        ]
        self.assertFalse(any(event["id"] in task["event_ids"] for task in tasks))
        legacy_job = self.repo.enqueue(10, "notify", {"event_ids": [event["id"]]})
        await self.service._process(self.repo.claim())
        self.assertEqual(self.repo.task(10, legacy_job)["status"], "done")
        self.bot.send_message.assert_not_awaited()

    async def test_stalled_fetch_is_bounded_and_uncertain_send_is_not_reclaimed(self):
        original_timeout = asyncio.timeout

        async def stall(*args, **kwargs):
            await asyncio.Future()

        self.bot.download.side_effect = stall
        self.bot.send_photo = AsyncMock(side_effect=stall)
        self.ingest(media=[self.photo()])
        with patch("archive.service.asyncio.timeout", side_effect=lambda _: original_timeout(0.01)):
            for expected in ("pending", "pending", "failed"):
                task = self.repo.claim()
                await self.service._process(task)
                status = self.repo.task(10, task["id"])
                self.assertEqual(status["status"], expected)
                with self.repo.db:
                    self.repo.db.execute("UPDATE archive_tasks SET available_at=0 WHERE id=?", (task["id"],))
            message = self.ingest(telegram_id=2, media=[self.photo("send")], archived=True)
            media = self.repo.message(10, message)["media"][0]["id"]
            send = self.repo.request_media(10, media, retry=False)
            with self.repo.db:
                self.repo.db.execute("UPDATE archive_tasks SET available_at=? WHERE id!=?", (time.time() + 60, send))
            await self.service._process(self.repo.claim())
        self.assertEqual(self.repo.task(10, send)["status"], "uncertain")
        self.repo.recover()
        self.assertEqual(self.repo.request_media(10, media, retry=False), send)
        self.assertIsNone(self.repo.claim())
