import logging
import re
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy
from logging.handlers import RotatingFileHandler
from pathlib import Path

_context = ContextVar("log_context", default={})


@contextmanager
def log_context(**values):
    token = _context.set(_context.get() | values)
    try:
        yield
    finally:
        _context.reset(token)


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record = copy(record)
        record.context = " ".join(f"{key}={value}" for key, value in _context.get().items())
        # Keep code locations; exception text, source lines and locals can contain secrets.
        if record.exc_info:
            frames = traceback.extract_tb(record.exc_info[2])
            location = " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames[-8:])
            record.msg = f"{record.getMessage()} exception={record.exc_info[0].__name__} stack={location}"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        text = super().format(record)
        text = re.sub(r"https?://\S+", "[url]", text)
        text = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[token]", text)
        return text.replace("\r", "\\r").replace("\n", "\\n")


def configure_logging(directory: Path | None = None) -> None:
    formatter = SafeFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s %(context)s")
    root = logging.getLogger()
    for previous in root.handlers:
        previous.close()
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.handlers[:] = [handler]
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(directory / "app.log", maxBytes=10 * 1024**2, backupCount=4, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    root.setLevel(logging.INFO)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
