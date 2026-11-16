"""Локальный HTTPS-прокси: DNS проверяется при каждом CONNECT, IP фиксируется.

yt-dlp и HTTP-адаптеры получают этот прокси. HTTP, внутренние сети и другие
порты закрыты. FFmpeg читает только локальные файлы с ограничением протоколов.
"""

import asyncio
import base64
import hmac
import ipaddress
import secrets
import socket
from urllib.parse import urlsplit

from core import texts as T


class UnsafeAddressError(ValueError):
    pass


def check_public_ip(address: str) -> None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as error:
        raise UnsafeAddressError(T.SERVICES_NETWORK_INVALID_IP) from error
    if not ip.is_global or ip.is_multicast or "%" in address:
        raise UnsafeAddressError(T.SERVICES_NETWORK_PRIVATE_ADDRESS)
    if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo):
        raise UnsafeAddressError(T.SERVICES_NETWORK_TRANSITION_IPV6)


async def resolve_public_addresses(host: str, port: int) -> list[tuple[int, str]]:
    if port != 443 or not host or "%" in host or "\\" in host:
        raise UnsafeAddressError(T.SERVICES_NETWORK_HTTPS_REQUIRED)
    results = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[tuple[int, str]] = []
    for family, _, _, _, sockaddr in results:
        check_public_ip(sockaddr[0])
        item = (family, sockaddr[0])
        if item not in addresses:
            addresses.append(item)
    if not addresses:
        raise UnsafeAddressError(T.SERVICES_NETWORK_DNS_FAILED)
    # Если DNS вернул смесь публичных и приватных IP, отклоняется весь ответ.
    return addresses


class SafeProxy:
    def __init__(self, connect_timeout: float = 10) -> None:
        self.connect_timeout = connect_timeout
        self._password = secrets.token_urlsafe(24)
        self._authorization = "Basic " + base64.b64encode(f"download:{self._password}".encode()).decode()
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task] = set()

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError(T.SERVICES_NETWORK_PROXY_NOT_STARTED)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://download:{self._password}@127.0.0.1:{port}"

    async def __aenter__(self) -> SafeProxy:
        self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0, limit=8192)
        return self

    async def __aexit__(self, *args) -> None:
        server = self._server
        if server is not None:
            server.close()
        tasks = list(self._clients)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # В новых Python wait_closed ждёт также клиентов: сначала закрываем их.
        if server is not None:
            await server.wait_closed()
        self._server = None

    async def _connect(self, host: str, port: int):
        addresses = await resolve_public_addresses(host, port)
        last_error: OSError | None = None
        for family, address in addresses:
            try:
                # Соединяемся с проверенным ЧИСЛОВЫМ IP: повторного DNS нет.
                return await asyncio.open_connection(address, port, family=family)
            except OSError as error:
                last_error = error
        raise OSError(T.SERVICES_NETWORK_CONNECTION_FAILED) from last_error

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._clients.add(task)
        upstream = None
        pumps: list[asyncio.Task] = []
        tunnel_started = False
        try:
            async with asyncio.timeout(self.connect_timeout):
                header = await reader.readuntil(b"\r\n\r\n")
                lines = header.decode("ascii").split("\r\n")
                method, target, version = lines[0].split()
                if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise UnsafeAddressError(T.SERVICES_NETWORK_HTTPS_TUNNEL_REQUIRED)
                auth = next(
                    (
                        line.split(":", 1)[1].strip()
                        for line in lines[1:]
                        if line.lower().startswith("proxy-authorization:")
                    ),
                    "",
                )
                if not hmac.compare_digest(auth, self._authorization):
                    raise UnsafeAddressError(T.SERVICES_NETWORK_UNKNOWN_PROXY_CLIENT)
                parts = urlsplit("https://" + target)
                if parts.username is not None or parts.path or parts.query or parts.fragment:
                    raise UnsafeAddressError(T.SERVICES_NETWORK_INVALID_TUNNEL)
                port = parts.port if parts.port is not None else 443
                remote_reader, upstream = await self._connect(parts.hostname or "", port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            tunnel_started = True
            pumps = [
                asyncio.create_task(self._relay(reader, upstream)),
                asyncio.create_task(self._relay(remote_reader, writer)),
            ]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
        except ValueError, OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError:
            if not tunnel_started:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                try:
                    await writer.drain()
                except OSError:
                    pass
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for connection in (upstream, writer):
                if connection is not None:
                    connection.close()
                    try:
                        await connection.wait_closed()
                    except OSError:
                        pass
            self._clients.discard(task)

    @staticmethod
    async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
