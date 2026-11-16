"""Consistent backup of a stopped bot's persistent data; excludes rebuildable cache.

Invoke only after the bot has stopped. The default includes archive blobs,
SQLite, WAL/SHM and operational metadata. Deployments use --database-only;
--keep prunes only timestamped deployment database backups, never full copies.
"""

import argparse
import os
import re
import tarfile
from pathlib import Path


def backup(source: Path, destination: Path, *, database_only: bool = False, keep: int = 0) -> None:
    source = source.resolve(strict=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.resolve().is_relative_to(source):
        raise ValueError("Backup destination must be outside the data volume")
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=1) as archive:
            for path in source.rglob("*"):
                relative = path.relative_to(source)
                if database_only and len(relative.parts) != 1:
                    continue
                if path.is_symlink() or any(part in {"cache", "work", "exports", "uploads"} for part in relative.parts):
                    continue
                if path.is_file() and path.name != "app.lock":
                    archive.add(path, arcname=relative.as_posix(), recursive=False)
        os.chmod(temporary, 0o600)
        temporary.replace(destination)
        if keep:
            pattern = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{40}\.database\.tar\.gz$")
            copies = sorted(
                p
                for p in destination.parent.iterdir()
                if p.is_file() and not p.is_symlink() and pattern.fullmatch(p.name)
            )
            for old in copies[:-keep]:
                if old != destination:
                    old.unlink()
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-only", action="store_true")
    parser.add_argument("--keep", type=int, default=0)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    if arguments.keep < 0:
        parser.error("--keep must be non-negative")
    backup(arguments.source, arguments.destination, database_only=arguments.database_only, keep=arguments.keep)
