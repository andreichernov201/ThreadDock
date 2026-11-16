"""An OS-level lock prevents two processes from sharing a bot's data directory."""

import os
from pathlib import Path

from core import texts as T


class InstanceLock:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")

    def __enter__(self):
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise RuntimeError(T.STORAGE_LOCK_INSTANCE_RUNNING) from error
        return self

    def __exit__(self, *args):
        self.file.close()
