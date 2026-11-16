"""Configure archive HTTPS without replacing private bot settings."""

import ipaddress
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


def archive_origin(values: dict, host: str) -> str:
    origin = (values.get("ARCHIVE_PUBLIC_URL") or "").strip().rstrip("/")
    domain = (values.get("ARCHIVE_DOMAIN") or "").strip().lower()
    if origin:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path
            or parsed.port not in {None, 443}
        ):
            raise ValueError("ARCHIVE_PUBLIC_URL must be an HTTPS origin.")
        if domain and domain != parsed.hostname:
            raise ValueError("ARCHIVE_DOMAIN does not match ARCHIVE_PUBLIC_URL.")
        domain = parsed.hostname
    if not domain:
        address = ipaddress.ip_address(host)
        if not address.is_global:
            raise ValueError("A public VPS address or ARCHIVE_DOMAIN is required.")
        domain = str(address).replace(".", "-").replace(":", "-") + ".sslip.io"
    if "." not in domain or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in domain.split(".")
    ):
        raise ValueError("Invalid archive domain.")
    return origin or "https://" + domain


def configure(path: Path, host: str) -> str:
    source = path.read_text(encoding="utf-8")
    origin = archive_origin(dotenv_values(path, interpolate=False), host)
    changes = {"ARCHIVE_DOMAIN": urlsplit(origin).hostname, "ARCHIVE_PUBLIC_URL": origin}
    lines = []
    for line in source.splitlines(keepends=True):
        match = re.match(r"^\s*(?:export\s+)?([A-Z_]+)\s*=", line)
        if match and match[1] in changes:
            continue
        lines.append(line)
    updated = "".join(lines).rstrip("\n") + "\n"
    updated += "".join(f"{name}={value}\n" for name, value in changes.items())
    previous = path.stat()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(updated)
        temporary.chmod(0o600)
        if hasattr(os, "chown"):
            os.chown(temporary, previous.st_uid, previous.st_gid)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    (path.parent / "web.enabled").touch()
    (path.parent / "web-origin").write_text(origin + "\n", encoding="utf-8")
    return origin


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path)
    parser.add_argument("host")
    arguments = parser.parse_args()
    print("Archive HTTPS configured:", configure(arguments.env_file, arguments.host))
