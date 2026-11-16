import logging
import re


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Upstream traceback strings may embed signed CDN URLs or Bot API tokens.
        if record.exc_info:
            record.msg = f"{record.getMessage()} [{record.exc_info[0].__name__}]"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        text = super().format(record)
        text = re.sub(r"https?://\S+", "[url]", text)
        return re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[token]", text)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
