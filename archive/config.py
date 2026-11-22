import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from core import texts as T

PREFETCH_TTL = 3 * 86400


@dataclass(frozen=True)
class ArchiveSettings:
    host: str = "127.0.0.1"
    port: int = 8080
    public_url: str = ""
    init_data_ttl: int = 300
    session_ttl: int = 3600
    media_download_bytes: int = 20_000_000
    storage_bytes: int = 20 * 1024**3
    operators: frozenset[int] = frozenset()

    @classmethod
    def from_environment(cls):
        domain = os.getenv("ARCHIVE_DOMAIN", "").strip()
        url = (os.getenv("ARCHIVE_PUBLIC_URL", "").strip() or (f"https://{domain}" if domain else "")).rstrip("/")
        parts = urlsplit(url)
        if url and (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise ValueError(T.ARCHIVE_CONFIG_HTTPS_ORIGIN_REQUIRED)
        if domain and parts.hostname != domain:
            raise ValueError(T.ARCHIVE_CONFIG_DOMAIN_MISMATCH)
        try:
            port = int(os.getenv("ARCHIVE_PORT", "8080"))
            storage = int(os.getenv("ARCHIVE_STORAGE_MB", "20480")) * 1024**2
            media_limit = int(os.getenv("ARCHIVE_MEDIA_DOWNLOAD_MB", "20")) * 1_000_000
            operators = frozenset(
                int(item.strip()) for item in os.getenv("OPERATOR_IDS", "").split(",") if item.strip()
            )
        except ValueError as error:
            raise ValueError(T.ARCHIVE_CONFIG_NUMERIC_CONFIG_REQUIRED) from error
        if not 1 <= port <= 65535 or storage < 1024**2 or not 0 < media_limit <= 2_000_000_000:
            raise ValueError(T.ARCHIVE_CONFIG_INVALID_CONFIG)
        return cls(
            host=os.getenv("ARCHIVE_HOST", "127.0.0.1"),
            port=port,
            public_url=url,
            storage_bytes=storage,
            media_download_bytes=media_limit,
            operators=operators,
        )
