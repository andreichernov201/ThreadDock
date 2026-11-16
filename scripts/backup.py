"""Consistent backup of a stopped bot's persistent data; excludes rebuildable cache.

Invoke through deploy.sh only after the bot has stopped. Archive blobs, SQLite,
WAL/SHM and operational metadata are included. Transient uploads are omitted.
"""

import os
import sys
import tarfile
from pathlib import Path


def backup(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.resolve().is_relative_to(source):
        raise ValueError("Backup destination must be outside the data volume")
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=1) as archive:
            for path in source.rglob("*"):
                relative = path.relative_to(source)
                if path.is_symlink() or any(part in {"cache", "work", "exports", "uploads"} for part in relative.parts):
                    continue
                if path.is_file() and path.name != "app.lock":
                    archive.add(path, arcname=relative.as_posix(), recursive=False)
        os.chmod(temporary, 0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    backup(Path(sys.argv[1]), Path(sys.argv[2]))
