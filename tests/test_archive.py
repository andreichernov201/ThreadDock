import hashlib
import hmac
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage
from aiohttp.test_utils import make_mocked_request

from archive.auth import AuthenticationError, hash_pin, validate_init_data, verify_pin
from archive.config import ArchiveSettings
from archive.exports import safe_csv, write_export
from archive.repository import ArchiveRepository
from archive.service import ArchiveService, LimitedWriter, quiet_delay
from archive.web import create_web_app
from core.errors import MediaError
from tests.support import DatabaseFixture


def signed_data(token, user_id=10, at=None, **extra):
    values = {
        "auth_date": str(int(time.time() if at is None else at)),
        "user": json.dumps({"id": user_id, "first_name": "Test"}),
        **extra,
    }
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class ArchiveFixture(DatabaseFixture):
    def setUp(self):
        super().setUp()
        self.bot = SimpleNamespace(id=123456, send_message=AsyncMock(), download=AsyncMock())
        self.service = ArchiveService(self.settings, self.database, self.bot, ArchiveSettings())
        self.repo = self.service.repository
        for owner in (10, 20):
            self.repo.ensure_owner(owner)
            self.repo.update_owner(owner, {"enabled": True})
            self.repo.save_connection(f"connection-{owner}", owner, True, {"can_reply": True})

    def ingest(
        self, owner=10, telegram_id=1, body="Original text", at=None, changed=None, media=None, archived=False, **kwargs
    ):
        at = time.time() - 100 if at is None else at
        identifier = self.repo.ingest(
            owner_id=owner,
            connection_id=f"connection-{owner}",
            source_chat_id=30,
            title="Test chat",
            username="test",
            telegram_id=telegram_id,
            sent_at=at,
            changed_at=at if changed is None else changed,
            outgoing=False,
            sender_id=30,
            media_group=None,
            payload={"body": body, "media": media or [], "protected": kwargs.pop("protected", False), "reply_to": None},
            **kwargs,
        )

        if archived:
            self.repo.delete_messages(owner, f"connection-{owner}", 30, [telegram_id], "Test chat")
        return identifier

    @staticmethod
    def photo(identifier="photo-1", **values):
        return {
            "file_id": identifier,
            "unique_id": identifier,
            "filename": "picture.jpg",
            "mime_type": "image/jpeg",
            "kind": "photo",
            "size": 4,
            **values,
        }


class ArchivePolicyTests(ArchiveFixture, unittest.TestCase):
    def test_global_author_optout_prevents_content_in_all_owners(self):
        first = self.ingest(owner=10)
        second = self.ingest(owner=20, media=[self.photo()])
        self.assertEqual(self.repo.set_author_optout(30, True), {10, 20})
        self.assertIsNone(self.repo.message(10, first))
        self.assertIsNone(self.repo.message(20, second))
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM archive_versions").fetchone()[0], 0)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM archive_search").fetchone()[0], 0)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM archive_media").fetchone()[0], 0)
        self.assertIsNone(self.ingest(owner=10, body="Must not be stored"))
        self.assertIsNone(self.ingest(owner=20, body="Must not be stored"))
        self.repo.set_author_optout(30, False)
        self.assertIsNotNone(self.ingest(owner=10, telegram_id=2))

    def test_feed_shows_changes_and_deletions_instead_of_normal_messages(self):
        at = time.time() - 100
        self.ingest(body="First", at=at)
        self.assertEqual(self.repo.events(10)["items"], [])
        self.ingest(body="Second", at=at, changed=at + 1)
        self.ingest(body="Third", at=at, changed=at + 2)
        events = self.repo.events(10)["items"]
        self.assertEqual([item["body"] for item in events], ["Third", "Second"])
        self.assertEqual({item["kind"] for item in events}, {"edited"})

    def test_reply_media_capture_has_an_event(self):
        self.ingest(media=[self.photo()], captured_from_reply=True, protected=True)
        events = self.repo.events(10)["items"]
        self.assertEqual([item["kind"] for item in events], ["reply_media"])

    def test_archive_search_respects_event_scope_and_filter(self):
        self.ingest(body="ordinary word")
        self.ingest(telegram_id=2, body="deleted word")
        self.repo.delete_messages(10, "connection-10", 30, [2], "Test chat")
        result = self.repo.search(10, "word", events_only=True)
        self.assertEqual([item["body"] for item in result["items"]], ["deleted word"])
        self.assertEqual(self.repo.search(10, "word", events_only=True, mode="edited")["items"], [])


class EventMediaPolicyTests(ArchiveFixture, unittest.TestCase):
    def test_ordinary_replied_media_has_no_archive_task(self):
        identifier = self.ingest(media=[self.photo()], captured_from_reply=True)
        self.assertEqual(self.repo.message(10, identifier)["media"], [])
        self.assertEqual(self.repo.gallery(10)["items"], [])
        self.assertEqual(self.repo.events(10)["items"], [])
        self.assertIsNone(self.repo.claim())

    def test_deletion_promotes_a_file_only_once(self):
        identifier = self.ingest(media=[self.photo()])
        self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat")
        self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat")
        self.assertTrue(self.repo.retained(10, identifier))
        self.assertEqual(len(self.repo.gallery(10)["items"]), 1)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM archive_tasks WHERE kind='media'").fetchone()[0], 1)

    def test_edit_retains_original_and_replacement(self):
        at = time.time() - 100
        identifier = self.ingest(at=at, media=[self.photo("original")])
        self.ingest(at=at, changed=at + 1, media=[self.photo("replacement")])
        self.assertEqual(len(self.repo.message(10, identifier)["media"]), 2)

    def test_keyword_does_not_copy_ordinary_media(self):
        self.repo.update_owner(10, {"keywords": ["meeting"]})
        identifier = self.ingest(body="meeting", media=[self.photo()])
        self.assertEqual(self.repo.message(10, identifier)["media"], [])
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM archive_tasks WHERE kind='media'").fetchone()[0], 0)

    def test_legacy_copy_is_retired_while_deleted_file_remains(self):
        identifier = self.ingest(media=[self.photo()])
        self.ingest(telegram_id=2, media=[self.photo("deleted")], archived=True)
        with self.repo.db:
            self.repo.db.execute(
                "UPDATE archive_media SET status='ready',local_path='blobs/10/old' WHERE message_id=?", (identifier,)
            )
        self.assertEqual(self.repo.stage_untriggered_media(), ["blobs/10/old"])
        self.assertEqual(len(self.repo.gallery(10)["items"]), 1)

    def test_old_reply_media_event_does_not_keep_an_ordinary_file(self):
        identifier = self.ingest(media=[self.photo()])
        message = self.repo.message(10, identifier)
        with self.repo.db:
            version = self.repo.db.execute(
                "SELECT id FROM archive_versions WHERE message_id=?", (identifier,)
            ).fetchone()[0]
            self.repo._event(10, message["chat_id"], identifier, "reply_media", time.time(), "legacy", version=version)
            self.repo._activate_media(10, identifier, self.repo.owner(10)["media_types"])
        self.repo.stage_untriggered_media()
        self.assertEqual(self.repo.gallery(10)["items"], [])
        self.assertEqual(self.repo.events(10)["items"], [])

    def test_retired_copy_task_does_not_block_a_later_real_deletion(self):
        identifier = self.ingest(media=[self.photo()])
        media_id = self.repo.db.execute("SELECT id FROM archive_media WHERE message_id=?", (identifier,)).fetchone()[0]
        task_id = self.repo.enqueue(10, "media", {"media_id": media_id}, dedupe=f"media:{media_id}")
        self.repo.task_status(task_id, "done")
        self.repo.stage_untriggered_media()
        self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat")
        task = self.repo.claim()
        self.assertEqual(task["kind"], "media")
        self.assertNotEqual(task["id"], task_id)

    def test_retired_download_is_enqueued_again_after_later_deletion(self):
        identifier = self.ingest(media=[self.photo()])
        with self.repo.db:
            media_id = self.repo.db.execute(
                "SELECT id FROM archive_media WHERE message_id=?", (identifier,)
            ).fetchone()[0]
            self.repo.db.execute("UPDATE archive_media SET status='ready' WHERE id=?", (media_id,))
            self.repo._enqueue(10, "media", {"media_id": media_id}, f"media:{media_id}")
        self.repo.stage_untriggered_media()
        self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat")
        self.assertEqual(
            self.repo.db.execute(
                "SELECT count(*) FROM archive_tasks WHERE kind='media' AND status='pending'"
            ).fetchone()[0],
            1,
        )

    def test_first_seen_edit_is_retained(self):
        at = time.time() - 100
        identifier = self.ingest(at=at, changed=at + 1, media=[self.photo()])
        self.assertTrue(self.repo.retained(10, identifier))

    def test_ordinary_snapshots_are_absent_from_message_listing(self):
        identifier = self.ingest(media=[self.photo()])
        chat_id = self.repo.message(10, identifier)["chat_id"]
        self.assertEqual(self.repo.messages(10, chat_id)["items"], [])


class AuthTests(unittest.TestCase):
    token = "12345:token"

    def test_hmac_includes_signature_and_unicode(self):
        raw = signed_data(self.token, signature="opaque-signature")
        self.assertEqual(validate_init_data(raw, self.token)["id"], 10)
        with self.assertRaises(AuthenticationError):
            validate_init_data(raw, self.token + "wrong")

    def test_expired_future_duplicate_and_invalid_user(self):
        for raw in (
            signed_data(self.token, at=1),
            signed_data(self.token, at=time.time() + 60),
            signed_data(self.token) + "&auth_date=1",
            signed_data(self.token, user_id=True),
        ):
            with self.subTest(raw=raw), self.assertRaises(AuthenticationError):
                validate_init_data(raw, self.token)

    def test_pin_hash_is_salted_and_compared(self):
        hashed, salt = hash_pin("123456")
        self.assertTrue(verify_pin("123456", hashed, salt))
        self.assertFalse(verify_pin("654321", hashed, salt))
        self.assertFalse(verify_pin(None, hashed, salt))
        self.assertNotEqual(hashed, hash_pin("123456")[0])


class RepositoryTests(ArchiveFixture, unittest.TestCase):
    def test_owner_isolation_and_historical_search(self):
        now = time.time() - 100
        identifier = self.ingest(at=now, body="secret pineapple")
        self.ingest(at=now, changed=now + 20, body="different text")
        foreign = self.ingest(owner=20, body="foreign pineapple")
        self.assertIsNone(self.repo.message(20, identifier))
        self.assertEqual([x["id"] for x in self.repo.search(10, "pineapple")["items"]], [identifier])
        self.assertEqual(len(self.repo.message(10, identifier)["versions"]), 2)
        self.assertIsNone(self.repo.task(20, self.repo.enqueue(10, "export", {})))
        with self.assertRaises(LookupError):
            self.repo.favorite(20, identifier, True)
        with self.assertRaises(LookupError):
            self.repo.purge_message(20, identifier)
        self.assertIsNotNone(self.repo.message(20, foreign))

    def test_repeated_and_out_of_order_versions(self):
        at = time.time() - 100
        identifier = self.ingest(at=at, changed=at + 20, body="Latest")
        self.ingest(at=at, changed=at + 10, body="Older")
        self.ingest(at=at, changed=at + 20, body="Latest")
        message = self.repo.message(10, identifier)
        self.assertEqual(message["body"], "Latest")
        self.assertEqual(len(message["versions"]), 2)

    def test_rotated_file_id_does_not_create_edit(self):
        at = time.time() - 100
        identifier = self.ingest(at=at, media=[self.photo()])
        self.ingest(at=at, changed=at + 10, media=[self.photo(file_id="rotated")])
        self.assertEqual(len(self.repo.message(10, identifier)["versions"]), 1)

    def test_deletion_tombstone_and_idempotency(self):
        identifier = self.ingest()
        self.assertEqual(self.repo.delete_messages(10, "connection-10", 30, [1, 2, 2], "Test chat"), 2)
        self.assertEqual(self.repo.delete_messages(10, "connection-10", 30, [1, 2], "Test chat"), 0)
        self.assertIsNotNone(self.repo.message(10, identifier)["deleted_at"])
        kinds = {x["kind"] for x in self.repo.events(10, kind="deleted")["items"]}
        self.assertEqual(kinds, {"deleted", "unknown_deleted"})

    def test_album_pagination_never_loses_equal_timestamps(self):
        self.ingest(media=[self.photo(str(index)) for index in range(5)], archived=True)
        with self.repo.db:
            self.repo.db.execute("UPDATE archive_media SET created_at=100")
        found, cursor = [], 0
        while True:
            page = self.repo.gallery(10, limit=2, cursor=cursor)
            found.extend(x["id"] for x in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(set(found)), 5)
        self.assertEqual(self.repo.gallery(20)["items"], [])

    def test_purge_cascades_search_sessions_media_and_tasks(self):
        self.ingest(body="findme", media=[self.photo()])
        self.ingest(owner=20, body="findme")
        token, _ = self.repo.create_session(10, 3600)
        self.repo.purge_owner(10)
        self.assertIsNone(self.repo.session(token))
        self.assertFalse(self.repo.owner(10)["enabled"])
        self.assertEqual(self.repo.search(10, "findme")["items"], [])
        self.assertEqual(len(self.repo.search(20, "findme", events_only=False)["items"]), 1)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM archive_tasks WHERE owner_id=10").fetchone()[0], 0)

    def test_paused_and_excluded_are_not_ingested(self):
        self.repo.update_owner(10, {"paused": True})
        self.assertIsNone(self.ingest())
        self.repo.update_owner(10, {"paused": False})
        identifier = self.ingest()
        chat = self.repo.message(10, identifier)["chat_id"]
        self.repo.update_chat(10, chat, {"excluded": True})
        self.assertIsNone(self.ingest(telegram_id=2))
        self.assertEqual(self.repo.delete_messages(10, "connection-10", 30, [3], "Test chat"), 0)

    def test_retention_keeps_favorites(self):
        old = self.ingest(telegram_id=1, at=time.time() - 40 * 86400)
        favorite = self.ingest(telegram_id=2, at=time.time() - 40 * 86400)
        self.repo.favorite(10, favorite, True)
        self.repo.update_owner(10, {"retention_days": 30})
        self.service.maintain()
        self.assertIsNone(self.repo.message(10, old))
        self.assertIsNotNone(self.repo.message(10, favorite))

    def test_recovery_preserves_unknown_send_outcome(self):
        first = self.repo.enqueue(10, "export", {})
        second = self.repo.enqueue(20, "export", {})
        self.repo.task_status(first, "sending")
        self.repo.task_status(second, "running")
        self.repo.recover()
        self.assertEqual(self.repo.task(10, first)["status"], "uncertain")
        self.assertEqual(self.repo.task(20, second)["status"], "pending")
        self.assertEqual(self.repo.db.execute("PRAGMA user_version").fetchone()[0], 2)
        ArchiveRepository(self.database)
        self.assertEqual(self.repo.db.execute("PRAGMA user_version").fetchone()[0], 2)


class WebTests(ArchiveFixture, unittest.IsolatedAsyncioTestCase):
    async def test_owner_cannot_read_or_mutate_foreign_resources(self):
        own = self.ingest(body="OWN", archived=True)
        foreign = self.ingest(owner=20, body="FOREIGNSECRET", media=[self.photo()], archived=True)
        chat = self.repo.message(20, foreign)["chat_id"]
        media = self.repo.message(20, foreign)["media"][0]["id"]
        task = self.repo.enqueue(20, "export", {})
        for route in (
            f"/api/messages/{foreign}",
            f"/api/tasks/{task}",
            f"/api/media/{media}/file",
            f"/api/stats?chat={chat}",
        ):
            self.assertEqual((await self.request("GET", route)).status, 404, route)
        for route, body in (
            (f"/api/media/{media}/send", {}),
            (f"/api/media/{media}/retry", {}),
            (f"/api/messages/{foreign}/favorite", {"value": True}),
            ("/api/export", {"format": "json", "chat_id": chat}),
            (f"/api/chats/{chat}/reply", {}),
        ):
            # Multipart reply validation may reject before resolving its chat.
            response = await self.request("POST", route, body)
            self.assertIn(response.status, {400, 404}, route)
        for route in (f"/api/events?chat={chat}", f"/api/gallery?chat={chat}", "/api/search?q=FOREIGNSECRET"):
            response = await self.request("GET", route)
            self.assertNotIn("FOREIGNSECRET", response.text)
            self.assertEqual(json.loads(response.body)["items"], [])
        response = await self.request("GET", "/api/stats?owner_id=20")
        self.assertEqual(json.loads(response.body)["totals"]["messages"], 1)
        self.assertIsNotNone(self.repo.message(10, own))
        self.assertFalse(self.repo.message(20, foreign)["favorite"])

    def setUp(self):
        super().setUp()
        self.app = create_web_app(self.service)
        self.app.freeze()
        self.token, self.csrf = self.repo.create_session(10, 3600)

    async def request(self, method, path, body=None, *, token=None, csrf=True, origin=None):
        headers = {"Authorization": "Bearer " + (token or self.token)}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        if body is not None:
            headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = origin
        request = make_mocked_request(method, path, headers=headers, app=self.app)
        request._read_bytes = json.dumps(body or {}).encode()
        match = await self.app.router.resolve(request)
        request._match_info = match
        match.add_app(self.app)
        return await self.app.middlewares[0](request, match.handler)

    async def test_signed_auth_and_consent(self):
        response = await self.request("POST", "/api/auth", {"init_data": signed_data(self.settings.token, user_id=40)})
        self.assertEqual(response.status, 200)
        self.assertFalse(self.repo.owner(40)["enabled"])
        self.repo.update_owner(10, {"enabled": False})
        response = await self.request("PATCH", "/api/settings", {"enabled": True})
        self.assertEqual(response.status, 400)
        response = await self.request("PATCH", "/api/settings", {"enabled": True, "consent": "archive-permitted-chats"})
        self.assertEqual(response.status, 200)

    async def test_csrf_origin_and_foreign_object_denied(self):
        foreign = self.ingest(owner=20, media=[self.photo()], archived=True)
        media = self.repo.message(20, foreign)["media"][0]["id"]
        for path in (f"/api/messages/{foreign}", f"/api/media/{media}/file"):
            self.assertEqual((await self.request("GET", path)).status, 404)
        self.assertEqual(
            (await self.request("POST", f"/api/messages/{foreign}/favorite", {"value": True}, csrf=False)).status, 403
        )
        self.assertEqual((await self.request("GET", "/api/chats", origin="https://evil.test")).status, 403)
        self.assertEqual((await self.request("GET", "/api/chats", token="invalid")).status, 401)

    async def test_pin_gate_and_failure_counter_shared_by_sessions(self):
        hashed, salt = hash_pin("123456")
        with self.repo.db:
            self.repo.db.execute("UPDATE archive_owners SET pin_hash=?,pin_salt=? WHERE owner_id=10", (hashed, salt))
        self.token, self.csrf = self.repo.create_session(10, 3600)
        self.assertEqual((await self.request("GET", "/api/chats")).status, 423)
        response = await self.request("GET", "/api/session")
        self.assertNotIn("owner", json.loads(response.body))
        for _ in range(5):
            response = await self.request("POST", "/api/unlock", {"pin": "invalid"})
            self.assertEqual(response.status, 403)
        self.token, self.csrf = self.repo.create_session(10, 3600)
        response = await self.request("POST", "/api/unlock", {"pin": "123456"})
        self.assertEqual(response.status, 429)

    async def test_unlock_lock_and_biometric_credential(self):
        hashed, salt = hash_pin("123456")
        with self.repo.db:
            self.repo.db.execute("UPDATE archive_owners SET pin_hash=?,pin_salt=? WHERE owner_id=10", (hashed, salt))
        self.token, self.csrf = self.repo.create_session(10, 3600)
        response = await self.request("POST", "/api/unlock", {"pin": "123456"})
        self.assertEqual(response.status, 200)
        response = await self.request("POST", "/api/biometric", {"device_id": "test-device"})
        credential = json.loads(response.body)["token"]
        await self.request("POST", "/api/lock")
        response = await self.request(
            "POST", "/api/unlock-biometric", {"device_id": "test-device", "token": credential}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.request("GET", "/api/chats")).status, 200)
        response = await self.request("GET", "/api/session")
        self.assertNotIn("pin_hash", json.loads(response.body)["owner"])

    async def test_delete_requires_confirmation_and_removes_local_file(self):
        identifier = self.ingest(media=[self.photo()], archived=True)
        media = self.repo.message(10, identifier)["media"][0]["id"]
        path = self.service.blobs / "10" / media
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"jpeg")
        with self.repo.db:
            self.repo.db.execute(
                "UPDATE archive_media SET local_path=?,status='ready' WHERE id=?",
                (path.relative_to(self.service.root).as_posix(), media),
            )
        route = f"/api/messages/{identifier}/delete"
        self.assertEqual((await self.request("POST", route, {})).status, 400)
        self.assertEqual((await self.request("POST", route, {"confirmation": "УДАЛИТЬ"})).status, 200)
        self.assertFalse(path.exists())
        self.assertIsNone(self.repo.message(10, identifier))


class ServiceTests(ArchiveFixture, unittest.IsolatedAsyncioTestCase):
    async def test_exports_escape_html_csv_and_isolate_owners(self):
        self.ingest(body="=SUM(1,2)<script>alert(1)</script>", archived=True)
        self.ingest(owner=20, body="FOREIGN")
        for format_name in ("json", "csv", "html", "md"):
            files = await write_export(self.repo, 10, self.root / format_name, format_name=format_name)
            text = files[0].read_text(encoding="utf-8-sig")
            self.assertNotIn("FOREIGN", text)
            if format_name == "json":
                self.assertEqual(len(json.loads(text)["records"]), 1)
            if format_name == "html":
                self.assertNotIn("<script>", text)
            if format_name == "csv":
                self.assertIn("'=SUM", text)
        self.assertTrue(safe_csv("\t=1").startswith("'"))

    async def test_split_exports_are_valid_independent_documents(self):
        for index in range(6):
            self.ingest(telegram_id=index + 1, body="message" * 30, archived=True)
        files = await write_export(self.repo, 10, self.root / "parts", max_part_bytes=650)
        self.assertGreater(len(files), 1)
        self.assertEqual(sum(len(json.loads(p.read_text(encoding="utf-8"))["records"]) for p in files), 6)

    async def test_grouped_notification_unknown_result_not_retried(self):
        self.ingest()
        self.ingest(telegram_id=2)
        self.repo.delete_messages(10, "connection-10", 30, [1, 2], "Test chat")
        self.bot.send_message.side_effect = TelegramNetworkError(
            method=SendMessage(chat_id=10, text="test"), message="offline"
        )
        task = self.repo.claim()
        self.assertEqual(task["kind"], "notify")
        await self.service._process(task)
        rows = self.repo.db.execute("SELECT status FROM archive_tasks WHERE kind='notify'").fetchall()
        self.assertEqual([r[0] for r in rows], ["uncertain", "uncertain"])
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_protected_media_attempts_bot_api_download(self):
        identifier = self.ingest(media=[self.photo()], protected=True)
        media = self.repo.message(10, identifier)["media"][0]["id"]

        async def download(file_id, *, destination, **kwargs):
            destination.write(b"file")

        self.bot.download.side_effect = download
        await self.service._save_media({"payload": {"media_id": media}}, self.repo.owner(10))
        self.bot.download.assert_awaited_once()
        self.assertEqual(self.repo.media(10, media)["status"], "ready")

    async def test_telegram_rejected_protected_media_has_explicit_status(self):
        identifier = self.ingest(media=[self.photo()], protected=True)
        media = self.repo.message(10, identifier)["media"][0]["id"]
        self.bot.download.side_effect = TelegramBadRequest(method=SendMessage(chat_id=10, text=""), message="protected")
        await self.service._save_media({"payload": {"media_id": media}}, self.repo.owner(10))
        self.assertEqual(self.repo.media(10, media)["status"], "protected")

    async def test_keyword_notice_explains_match_and_links_to_record(self):
        from dataclasses import replace

        self.service.config = replace(self.service.config, public_url="https://archive.example.org")
        self.repo.update_owner(10, {"keywords": ["meeting"]})
        message_id = self.ingest(body="meeting at six")
        task = self.repo.claim()
        self.assertEqual(task["kind"], "notify")
        await self.service._notify(task, self.repo.owner(10))
        call = self.bot.send_message.call_args
        self.assertIn("meeting", call.args[1])
        self.assertNotIn("meeting at six", call.args[1])  # hide_content remains enforced
        buttons = call.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(len(buttons), 1)
        self.assertIn(f"message={message_id}", buttons[0][0].web_app.url)

    async def test_download_is_bounded_and_saved_without_public_url(self):
        identifier = self.ingest(media=[self.photo()], archived=True)
        media = self.repo.message(10, identifier)["media"][0]["id"]

        async def download(*args, destination, **kwargs):
            destination.write(b"jpeg")

        self.bot.download.side_effect = download
        await self.service._save_media({"payload": {"media_id": media}}, self.repo.owner(10))
        row = self.repo.media(10, media)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(self.service.safe_path(row["local_path"]).read_bytes(), b"jpeg")
        self.assertNotIn("file_id", self.repo.message(10, identifier)["media"][0])

    async def test_staged_file_cannot_be_downloaded_by_a_stale_task(self):
        identifier = self.ingest(media=[self.photo()])
        media_id = self.repo.db.execute("SELECT id FROM archive_media WHERE message_id=?", (identifier,)).fetchone()[0]
        await self.service._save_media({"payload": {"media_id": media_id}}, self.repo.owner(10))
        self.bot.download.assert_not_awaited()

    async def test_export_contains_no_ordinary_message_snapshots(self):
        self.ingest(body="ordinary message")
        files = await write_export(self.repo, 10, self.root / "ordinary-export")
        self.assertEqual(json.loads(files[0].read_text())["records"], [])

    async def test_no_disk_space_stops_download(self):
        identifier = self.ingest(media=[self.photo()], archived=True)
        media = self.repo.message(10, identifier)["media"][0]["id"]
        with patch("archive.service.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            await self.service._save_media({"payload": {"media_id": media}}, self.repo.owner(10))
        self.bot.download.assert_not_awaited()
        self.assertEqual(self.repo.media(10, media)["status"], "storage_full")

    async def test_vault_rejects_other_members(self):
        self.bot.get_chat = AsyncMock(return_value=SimpleNamespace(type="supergroup", is_forum=True, username=None))
        self.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status="administrator", can_manage_topics=True)
        )
        self.bot.get_chat_member_count = AsyncMock(return_value=3)
        with self.assertRaises(ValueError):
            await self.service.bind_vault(10, -100123)
        self.assertIsNone(self.repo.owner(10)["vault_chat_id"])

    def test_quiet_hours_cross_midnight_and_writer_limit(self):
        owner = self.repo.owner(10) | {"quiet_start": 23 * 60, "quiet_end": 7 * 60, "utc_offset": 0}
        self.assertGreater(quiet_delay(owner, 24 * 3600), 0)
        self.assertEqual(quiet_delay(owner, 12 * 3600), 0)
        import io

        writer = LimitedWriter(io.BytesIO(), 4)
        with self.assertRaises(MediaError):
            writer.write(b"12345")
        with self.assertRaises(ValueError):
            self.service.safe_path("../../outside", self.service.blobs)


class ArchiveNoticeTests(ArchiveFixture, unittest.IsolatedAsyncioTestCase):
    async def test_notice_uses_the_event_snapshot_not_the_latest_edit(self):
        self.repo.update_owner(10, {"hide_content": False})
        at = time.time() - 100
        self.ingest(body="original", at=at)
        self.ingest(body="first edit", at=at, changed=at + 10)
        self.ingest(body="later edit", at=at, changed=at + 20)
        task = self.repo.claim()
        await self.service._process(task)
        notice = self.bot.send_message.call_args.args[1]
        self.assertIn("first edit", notice)
        self.assertNotIn("later edit", notice)
        self.assertIsNone(self.bot.send_message.call_args.kwargs["reply_markup"])

    async def test_notice_only_offers_the_published_archive(self):
        self.service.config = ArchiveSettings(public_url="https://archive.example.org")
        self.ingest()
        self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat")
        await self.service._process(self.repo.claim())
        keyboard = self.bot.send_message.call_args.kwargs["reply_markup"]
        self.assertEqual(len(keyboard.inline_keyboard), 1)
        self.assertEqual(len(keyboard.inline_keyboard[0]), 1)
        button = keyboard.inline_keyboard[0][0]
        self.assertIsNotNone(button.web_app)
        self.assertIsNone(button.callback_data)
