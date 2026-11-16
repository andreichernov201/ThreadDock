from core import texts as T


class MediaError(Exception):
    """Safe to display; never contains upstream URLs or credentials."""

    def __init__(self, message: str, *, code: str = "media_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MissingAsset(MediaError):
    def __init__(self):
        super().__init__(T.CORE_ERRORS_CACHE_MISSING, code="cache_miss")


class QueueFull(MediaError):
    def __init__(self):
        super().__init__(T.CORE_ERRORS_QUEUE_FULL, code="queue_full")
