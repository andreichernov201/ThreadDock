"""Same-origin Mini App API. Owner identity is derived only from signed Telegram data."""

import asyncio
import hmac
import logging
import time
import uuid
from pathlib import Path

from aiohttp import web

from archive.analytics import statistics_for
from archive.auth import AuthenticationError, hash_pin, new_token, token_hash, validate_init_data, verify_pin
from archive.exports import FORMATS
from core import texts as T
from core.errors import MediaError
from services.processes import remove_directory

ASSETS = Path(__file__).parent / "web"
OPEN_WHEN_LOCKED = {"/api/session", "/api/unlock", "/api/unlock-biometric", "/api/lock", "/api/logout"}
logger = logging.getLogger(__name__)


def number(request, key, default=0, maximum=10**16):
    try:
        value = int(request.query.get(key, default))
        if not 0 <= value <= maximum:
            raise ValueError
        return value
    except (TypeError, ValueError) as error:
        raise ValueError(T.ARCHIVE_WEB_INVALID_QUERY) from error


async def body_json(request) -> dict:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
        return body
    except (ValueError, TypeError) as error:
        raise ValueError(T.ARCHIVE_WEB_JSON_OBJECT_REQUIRED) from error


def create_web_app(service) -> web.Application:
    repository, config = service.repository, service.config
    auth_attempts: dict[str, tuple[float, int]] = {}

    @web.middleware
    async def boundary(request, handler):
        operation = uuid.uuid4().hex
        try:
            origin = request.headers.get("Origin")
            if request.path.startswith("/api/") and origin:
                allowed = (
                    {config.public_url}
                    if config.public_url
                    else {f"http://127.0.0.1:{config.port}", f"http://localhost:{config.port}"}
                )
                if origin not in allowed:
                    raise web.HTTPForbidden(reason="Unexpected origin")
            if request.path.startswith("/api/") and request.path != "/api/auth":
                authorization = request.headers.get("Authorization", "")
                if not authorization.startswith("Bearer ") or len(authorization) > 200:
                    raise web.HTTPUnauthorized()
                session = repository.session(authorization[7:])
                if session is None:
                    raise web.HTTPUnauthorized()
                owner = repository.owner(session["owner_id"])
                if owner is None:
                    raise web.HTTPUnauthorized()
                request["session"], request["owner"] = session, owner
                if request.method not in {"GET", "HEAD"}:
                    csrf = request.headers.get("X-CSRF-Token", "")
                    if not csrf or not hmac.compare_digest(token_hash(csrf), session["csrf_hash"]):
                        raise web.HTTPForbidden(reason="Invalid CSRF token")
                if (
                    owner["pin_hash"]
                    and session["unlocked_until"] <= time.time()
                    and request.path not in OPEN_WHEN_LOCKED
                ):
                    return web.json_response({"error": T.ARCHIVE_WEB_ARCHIVE_LOCKED, "code": "locked"}, status=423)
            response = await handler(request)
        except AuthenticationError as error:
            response = web.json_response({"error": str(error), "code": "authentication"}, status=401)
        except LookupError:
            response = web.json_response({"error": T.ARCHIVE_WEB_OBJECT_NOT_FOUND, "code": "not_found"}, status=404)
        except (ValueError, MediaError) as error:
            response = web.json_response(
                {"error": str(error), "code": getattr(error, "code", "invalid_request")}, status=400
            )
        except web.HTTPException as error:
            response = web.json_response(
                {
                    "error": {
                        401: T.ARCHIVE_WEB_REOPEN_TELEGRAM,
                        403: T.ARCHIVE_WEB_FORBIDDEN,
                        405: T.ARCHIVE_WEB_REQUEST_FAILED,
                        429: T.ARCHIVE_WEB_TOO_MANY_ATTEMPTS,
                    }.get(error.status, T.ARCHIVE_WEB_REQUEST_FAILED),
                    "code": str(error.status),
                },
                status=error.status,
            )
        except Exception as error:
            resource = request.match_info.route.resource
            route = resource.canonical if resource else "unmatched"
            code = type(error).__name__
            logger.exception(
                "archive_http operation=%s method=%s route=%s code=%s", operation, request.method, route, code
            )
            try:
                repository.record_error(f"http:{code}")
            except Exception:
                logger.error("archive_http operation=%s state=diagnostic_write_failed", operation)
            response = web.json_response(
                {"error": T.ARCHIVE_WEB_INTERNAL_ERROR, "code": "internal", "operation_id": operation},
                status=500,
            )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Operation-Id": operation,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        return response

    app = web.Application(middlewares=[boundary], client_max_size=service.settings.max_upload_bytes + 100_000)
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(request):
        return web.FileResponse(ASSETS / "index.html")

    @routes.get("/assets/{name}")
    async def asset(request):
        name = request.match_info["name"]
        if name not in {"app.js", "style.css", "demo.js", "texts.js", "icon.svg"}:
            raise web.HTTPNotFound()
        return web.FileResponse(ASSETS / name)

    @routes.get("/healthz")
    async def health(request):
        return web.json_response({"ok": True})

    @routes.post("/api/auth")
    async def authenticate(request):
        body = await body_json(request)
        user = validate_init_data(body.get("init_data", ""), service.settings.token, max_age=config.init_data_ttl)
        key, now = str(user["id"]), time.time()
        since, count = auth_attempts.get(key, (now, 0))
        if now - since >= 60:
            since, count = now, 0
        if count >= 30:
            raise web.HTTPTooManyRequests()
        auth_attempts[key] = (since, count + 1)
        if len(auth_attempts) > 10000:
            for old_key, (at, _) in list(auth_attempts.items()):
                if now - at > 60:
                    del auth_attempts[old_key]
        owner = repository.ensure_owner(user["id"], user.get("language_code", "ru"))
        token, csrf = repository.create_session(user["id"], config.session_ttl)
        return web.json_response(
            {
                "token": token,
                "csrf": csrf,
                "locked": bool(owner["pin_hash"]),
                "has_pin": bool(owner["pin_hash"]),
                "language": owner["language"],
                "user": {"id": user["id"], "first_name": user.get("first_name", "")},
            }
        )

    @routes.get("/api/session")
    async def session_info(request):
        owner, session = request["owner"], request["session"]
        locked = bool(owner["pin_hash"] and session["unlocked_until"] <= time.time())
        result = {
            "locked": locked,
            "has_pin": bool(owner["pin_hash"]),
            "language": owner["language"],
            "auto_lock": owner["auto_lock"],
        }
        if not locked:
            result.update(
                owner=repository.public_owner(owner["owner_id"]),
                connections=repository.connections(owner["owner_id"]),
                bot_username=getattr(service, "bot_username", ""),
                operator=owner["owner_id"] in config.operators,
                deletion=repository.deletion_status(owner["owner_id"]),
            )
        return web.json_response(result)

    def check_unlock_limit(owner_id):
        row = repository.db.execute(
            "SELECT blocked_until FROM archive_unlock_limits WHERE owner_id=?", (owner_id,)
        ).fetchone()
        if row and row[0] > time.time():
            raise web.HTTPTooManyRequests()

    def unlock_result(owner_id, session_hash, valid):
        if not valid:
            with repository.db:
                repository.db.execute(
                    "INSERT INTO archive_unlock_limits(owner_id,failures) VALUES (?,1) ON CONFLICT(owner_id) DO UPDATE SET failures=failures+1",
                    (owner_id,),
                )
                row = repository.db.execute(
                    "SELECT failures FROM archive_unlock_limits WHERE owner_id=?", (owner_id,)
                ).fetchone()
                if row[0] >= 5:
                    repository.db.execute(
                        "UPDATE archive_unlock_limits SET failures=0,blocked_until=? WHERE owner_id=?",
                        (time.time() + 300, owner_id),
                    )
            raise web.HTTPForbidden()
        with repository.db:
            repository.db.execute("DELETE FROM archive_unlock_limits WHERE owner_id=?", (owner_id,))
            owner = repository.owner(owner_id)
            repository.db.execute(
                "UPDATE archive_sessions SET unlocked_until=? WHERE token_hash=? AND owner_id=?",
                (time.time() + owner["auto_lock"], session_hash, owner_id),
            )

    @routes.post("/api/unlock")
    async def unlock(request):
        owner, session = request["owner"], request["session"]
        check_unlock_limit(owner["owner_id"])
        body = await body_json(request)
        valid = not owner["pin_hash"] or await asyncio.to_thread(
            verify_pin, body.get("pin", ""), owner["pin_hash"], owner["pin_salt"]
        )
        unlock_result(owner["owner_id"], session["token_hash"], valid)
        return web.json_response({"ok": True})

    @routes.post("/api/unlock-biometric")
    async def unlock_biometric(request):
        owner, session = request["owner"], request["session"]
        check_unlock_limit(owner["owner_id"])
        body = await body_json(request)
        credential = body.get("token", "")
        if not isinstance(credential, str) or len(credential) > 200:
            raise ValueError(T.ARCHIVE_WEB_INVALID_DEVICE_KEY)
        valid = repository.db.execute(
            "SELECT 1 FROM archive_biometrics WHERE owner_id=? AND device_id=? AND token_hash=?",
            (owner["owner_id"], str(body.get("device_id", ""))[:128], token_hash(credential)),
        ).fetchone()
        unlock_result(owner["owner_id"], session["token_hash"], bool(valid))
        return web.json_response({"ok": True})

    @routes.post("/api/lock")
    async def lock(request):
        with repository.db:
            repository.db.execute(
                "UPDATE archive_sessions SET unlocked_until=0 WHERE token_hash=?", (request["session"]["token_hash"],)
            )
        return web.json_response({"ok": True})

    @routes.post("/api/activity")
    async def activity(request):
        with repository.db:
            repository.db.execute(
                "UPDATE archive_sessions SET unlocked_until=? WHERE token_hash=?",
                (time.time() + request["owner"]["auto_lock"], request["session"]["token_hash"]),
            )
        return web.json_response({"ok": True})

    @routes.post("/api/logout")
    async def logout(request):
        with repository.db:
            repository.db.execute(
                "DELETE FROM archive_sessions WHERE token_hash=?", (request["session"]["token_hash"],)
            )
        return web.json_response({"ok": True})

    @routes.post("/api/pin")
    async def pin(request):
        owner, session = request["owner"], request["session"]
        body = await body_json(request)
        check_unlock_limit(owner["owner_id"])
        if owner["pin_hash"]:
            valid = await asyncio.to_thread(
                verify_pin, body.get("current_pin", ""), owner["pin_hash"], owner["pin_salt"]
            )
            unlock_result(owner["owner_id"], session["token_hash"], valid)
        new_pin = body.get("new_pin", "")
        encoded, salt = await asyncio.to_thread(hash_pin, new_pin) if new_pin else (None, None)
        with repository.db:
            repository.db.execute(
                "UPDATE archive_owners SET pin_hash=?,pin_salt=? WHERE owner_id=?", (encoded, salt, owner["owner_id"])
            )
            repository.db.execute(
                "DELETE FROM archive_sessions WHERE owner_id=? AND token_hash<>?",
                (owner["owner_id"], session["token_hash"]),
            )
            repository.db.execute("DELETE FROM archive_biometrics WHERE owner_id=?", (owner["owner_id"],))
            repository.db.execute(
                "UPDATE archive_sessions SET unlocked_until=? WHERE token_hash=?",
                (time.time() + owner["auto_lock"], session["token_hash"]),
            )
        return web.json_response({"ok": True})

    @routes.post("/api/biometric")
    async def biometric(request):
        owner = request["owner"]
        if not owner["pin_hash"]:
            raise ValueError(T.ARCHIVE_WEB_PIN_REQUIRED)
        body = await body_json(request)
        device = body.get("device_id", "")
        if not isinstance(device, str) or not 1 <= len(device) <= 128:
            raise ValueError(T.ARCHIVE_WEB_INVALID_DEVICE_ID)
        token = new_token()
        with repository.db:
            repository.db.execute(
                "INSERT OR REPLACE INTO archive_biometrics VALUES (?,?,?)",
                (owner["owner_id"], device, token_hash(token)),
            )
        return web.json_response({"token": token})

    @routes.get("/api/chats")
    async def chats(request):
        return web.json_response(
            {"items": repository.chats(request["owner"]["owner_id"], query=request.query.get("q", ""))}
        )

    @routes.get("/api/events")
    async def events(request):
        try:
            before = float(request.query["before"]) if "before" in request.query else None
            if before is not None and not 0 <= before <= 10**11:
                raise ValueError
        except ValueError as error:
            raise ValueError(T.ARCHIVE_WEB_INVALID_QUERY) from error
        return web.json_response(
            repository.events(
                request["owner"]["owner_id"],
                cursor=number(request, "cursor"),
                before=before,
                kind=request.query.get("kind", "all"),
                chat_id=number(request, "chat"),
            )
        )

    @routes.get("/api/search")
    async def search(request):
        return web.json_response(
            repository.search(
                request["owner"]["owner_id"],
                request.query.get("q", ""),
                chat_id=number(request, "chat"),
                cursor=number(request, "cursor"),
                mode=request.query.get("kind", "all"),
                events_only=True,
            )
        )

    @routes.get("/api/chats/{id:[0-9]+}/messages")
    async def messages(request):
        return web.json_response(
            repository.messages(
                request["owner"]["owner_id"],
                int(request.match_info["id"]),
                cursor=number(request, "cursor"),
                mode=request.query.get("mode", "all"),
            )
        )

    @routes.get("/api/messages/{id:[0-9]+}")
    async def message(request):
        owner, identifier = request["owner"]["owner_id"], int(request.match_info["id"])
        if not repository.retained(owner, identifier):
            raise LookupError()
        return web.json_response(repository.message(owner, identifier))

    @routes.post("/api/messages/{id:[0-9]+}/favorite")
    async def favorite(request):
        body = await body_json(request)
        if type(body.get("value")) is not bool:
            raise ValueError(T.ARCHIVE_WEB_BOOLEAN_REQUIRED)
        owner, identifier = request["owner"]["owner_id"], int(request.match_info["id"])
        if not repository.retained(owner, identifier):
            raise LookupError()
        repository.favorite(owner, identifier, body["value"])
        return web.json_response({"ok": True})

    @routes.post("/api/messages/{id:[0-9]+}/delete")
    async def delete_message(request):
        body = await body_json(request)
        if body.get("confirmation") != T.ARCHIVE_WEB_DELETE_CONFIRMATION:
            raise ValueError(T.ARCHIVE_WEB_DELETE_CONFIRMATION_REQUIRED)
        owner = request["owner"]["owner_id"]
        async with service.owner_lock(owner):
            repository.purge_message(owner, int(request.match_info["id"]))
            service.cleanup_owner_blobs(owner)
        return web.json_response({"ok": True, **repository.deletion_status(owner)})

    @routes.patch("/api/chats/{id:[0-9]+}")
    async def chat_settings(request):
        body = await body_json(request)
        if body.get("excluded"):
            raise ValueError(T.ARCHIVE_WEB_EXCLUDE_REQUIRES_CONFIRMATION)
        return web.json_response(
            repository.update_chat(request["owner"]["owner_id"], int(request.match_info["id"]), body)
        )

    @routes.post("/api/chats/{id:[0-9]+}/delete")
    async def delete_chat(request):
        body = await body_json(request)
        if (
            body.get("confirmation") != T.ARCHIVE_WEB_DELETE_CONFIRMATION
            or type(body.get("days", 0)) is not int
            or body.get("days", 0) not in {0, 1, 3}
        ):
            raise ValueError(T.ARCHIVE_WEB_DELETE_CONFIRMATION_REQUIRED)
        owner, chat_id = request["owner"]["owner_id"], int(request.match_info["id"])
        if body.get("exclude"):
            repository.update_chat(owner, chat_id, {"excluded": True})
        async with service.owner_lock(owner):
            repository.purge_chat(
                owner,
                chat_id,
                since=time.time() - body["days"] * 86400 if body.get("days") else 0,
                exclude=bool(body.get("exclude")),
            )
            service.cleanup_owner_blobs(owner)
        return web.json_response({"ok": True, **repository.deletion_status(owner)})

    @routes.get("/api/gallery")
    async def gallery(request):
        return web.json_response(
            repository.gallery(
                request["owner"]["owner_id"], chat_id=number(request, "chat"), cursor=number(request, "cursor")
            )
        )

    @routes.get("/api/media/{id:[a-f0-9]{32}}/file")
    async def media_file(request):
        media = repository.media(request["owner"]["owner_id"], request.match_info["id"])
        if media is None:
            raise LookupError()
        if not media["local_path"]:
            return web.json_response(
                {
                    "error": T.ARCHIVE_WEB_PROTECTED_MEDIA if media["protected"] else T.ARCHIVE_WEB_REMOTE_MEDIA,
                    "code": "protected_media" if media["protected"] else "remote_media",
                },
                status=409,
            )
        path = service.safe_path(media["local_path"], service.blobs)
        if not path.is_file():
            raise LookupError()
        safe_mime = (
            media["mime_type"]
            if media["mime_type"]
            in {
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/gif",
                "image/avif",
                "video/mp4",
                "video/webm",
                "audio/ogg",
                "audio/mpeg",
                "audio/mp4",
            }
            else "application/octet-stream"
        )
        response = web.FileResponse(path)
        response.content_type = safe_mime
        if safe_mime == "application/octet-stream":
            response.headers["Content-Disposition"] = "attachment"
        return response

    @routes.post("/api/media/{id:[a-f0-9]{32}}/send")
    async def send_media(request):
        return queue_media_send(request)

    @routes.post("/api/media/{id:[a-f0-9]{32}}/send/confirm")
    async def confirm_media_send(request):
        body = await body_json(request)
        previous, created_at = body.get("task_id"), body.get("created_at")
        if (
            body.get("checked_chat") is not True
            or type(previous) is not int
            or not 0 < previous <= 10**16
            or type(created_at) not in {int, float}
            or not 0 < created_at < 10**11
        ):
            raise ValueError(T.ARCHIVE_WEB_SEND_RETRY_CHECK_CHAT)
        return queue_media_send(request, (previous, created_at))

    def queue_media_send(request, confirmed_retry=None):
        owner, identifier = request["owner"]["owner_id"], request.match_info["id"]
        media = repository.media(owner, identifier)
        if media is None:
            raise LookupError()
        service.check_media_send(media)
        job = repository.request_media(owner, identifier, retry=False, confirmed_retry=confirmed_retry)
        if not job:
            raise web.HTTPTooManyRequests(headers={"Retry-After": "60"})
        return web.json_response({"task_id": job, "status": repository.task(owner, job)["status"]}, status=202)

    @routes.post("/api/media/{id:[a-f0-9]{32}}/retry")
    async def retry_media(request):
        owner, identifier = request["owner"]["owner_id"], request.match_info["id"]
        media = repository.media(owner, identifier)
        if media is None:
            raise LookupError()
        job = repository.request_media(owner, identifier, retry=True)
        if not job:
            raise web.HTTPTooManyRequests(headers={"Retry-After": "60"})
        return web.json_response({"task_id": job}, status=202)

    @routes.get("/api/stats")
    async def stats(request):
        options = {"days": number(request, "days", 30, 3660), "chat_id": number(request, "chat")}
        if "start" in request.query or "end" in request.query:
            options.update(start=float(request.query["start"]), end=float(request.query["end"]))
        return web.json_response(statistics_for(repository, request["owner"]["owner_id"], **options))

    @routes.post("/api/stats/delete")
    async def delete_stats(request):
        body = await body_json(request)
        if body.get("confirmation") != T.ARCHIVE_WEB_DELETE_CONFIRMATION:
            raise ValueError(T.ARCHIVE_WEB_DELETE_CONFIRMATION_REQUIRED)
        repository.reset_statistics(request["owner"]["owner_id"])
        return web.json_response({"ok": True})

    @routes.post("/api/history/delete")
    async def clear_history(request):
        body = await body_json(request)
        if body.get("confirmation") != T.ARCHIVE_WEB_DELETE_CONFIRMATION:
            raise ValueError(T.ARCHIVE_WEB_DELETE_CONFIRMATION_REQUIRED)
        owner = request["owner"]["owner_id"]
        async with service.owner_lock(owner):
            repository.clear_history(owner)
            service.cleanup_owner_blobs(owner)
        return web.json_response({"ok": True, **repository.deletion_status(owner)})

    @routes.patch("/api/settings")
    async def settings(request):
        body = await body_json(request)
        if body.get("enabled") and not request["owner"]["enabled"]:
            if body.pop("consent", "") != "archive-permitted-chats":
                raise ValueError(T.ARCHIVE_WEB_ARCHIVE_CONSENT_REQUIRED)
        else:
            body.pop("consent", None)
        return web.json_response(repository.update_owner(request["owner"]["owner_id"], body))

    @routes.post("/api/export")
    async def export(request):
        body = await body_json(request)
        format_name, chat_id = body.get("format", "json"), body.get("chat_id", 0)
        if format_name not in FORMATS or type(chat_id) is not int:
            raise ValueError(T.ARCHIVE_WEB_EXPORT_FORMAT_REQUIRED)
        owner = request["owner"]["owner_id"]
        if chat_id and not repository.chat(owner, chat_id):
            raise LookupError()
        return web.json_response(
            {"task_id": repository.enqueue(owner, "export", {"format": format_name, "chat_id": chat_id})}, status=202
        )

    @routes.get("/api/tasks/{id:[0-9]+}")
    async def task(request):
        row = repository.task(request["owner"]["owner_id"], int(request.match_info["id"]))
        if row is None:
            raise LookupError()
        return web.json_response(row)

    @routes.post("/api/vault")
    async def vault(request):
        body = await body_json(request)
        if type(body.get("chat_id")) is not int or body["chat_id"] >= 0:
            raise ValueError(T.ARCHIVE_WEB_VAULT_ID_REQUIRED)
        async with service.owner_lock(request["owner"]["owner_id"]):
            await service.bind_vault(request["owner"]["owner_id"], body["chat_id"])
        return web.json_response({"ok": True})

    @routes.post("/api/vault/request")
    async def vault_request(request):
        return web.json_response(
            {"task_id": repository.enqueue(request["owner"]["owner_id"], "vault_prompt", {})}, status=202
        )

    @routes.post("/api/delete-me")
    async def delete_me(request):
        body = await body_json(request)
        if body.get("confirmation") != T.ARCHIVE_WEB_DELETE_CONFIRMATION:
            raise ValueError(T.ARCHIVE_WEB_DELETE_CONFIRMATION_REQUIRED)
        owner = request["owner"]["owner_id"]
        repository.update_owner(owner, {"enabled": False})
        async with service.owner_lock(owner):
            repository.purge_owner(owner)
            service.cleanup_owner_blobs(owner)
            for root in (service.exports, service.uploads):
                path = root / str(owner)
                if path.exists():
                    remove_directory(path, root)
        return web.json_response({"ok": True, **repository.deletion_status(owner)})

    @routes.get("/api/operator")
    async def operator(request):
        if request["owner"]["owner_id"] not in config.operators:
            raise web.HTTPForbidden()
        now = time.time()
        return web.json_response(
            {
                "owners": repository.db.execute("SELECT count(*) FROM archive_owners").fetchone()[0],
                "messages": repository.db.execute("SELECT count(*) FROM archive_messages").fetchone()[0],
                "tasks": [
                    dict(row)
                    for row in repository.db.execute(
                        """SELECT kind,status,count(*) AS count,max(attempts) AS max_attempts,
                        max(0,cast(?-min(created_at) AS INTEGER)) AS oldest_seconds
                        FROM archive_tasks GROUP BY kind,status ORDER BY kind,status""",
                        (now,),
                    )
                ],
                "media": [
                    dict(row)
                    for row in repository.db.execute(
                        """SELECT status,count(*) AS count,sum(CASE WHEN local_path IS NULL THEN 0 ELSE size END) AS bytes
                        FROM archive_media GROUP BY status"""
                    )
                ],
                "vault_cleanup": [
                    dict(row)
                    for row in repository.db.execute(
                        "SELECT state,count(*) AS count FROM archive_vault_copies WHERE state!='live' GROUP BY state"
                    )
                ],
                "errors": [
                    dict(row)
                    for row in repository.db.execute(
                        "SELECT code,count,last_at FROM archive_errors ORDER BY last_at DESC LIMIT 30"
                    )
                ],
            }
        )

    app.add_routes(routes)
    return app


async def start_web_server(service):
    runner = web.AppRunner(create_web_app(service), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, service.config.host, service.config.port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
