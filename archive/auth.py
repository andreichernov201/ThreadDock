"""Telegram initData verification. Credentials are never put in media URLs."""

import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl

from core import texts as T


class AuthenticationError(ValueError):
    pass


def validate_init_data(raw: str, bot_token: str, *, max_age: int = 300, now: float | None = None) -> dict:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 16384:
        raise AuthenticationError(T.ARCHIVE_AUTH_OPEN_FROM_TELEGRAM)
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        values = dict(pairs)
        if len(values) != len(pairs):
            raise ValueError("Duplicate fields")
        received_hash = values.pop("hash")
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            raise ValueError("Signature mismatch")
        age = (time.time() if now is None else now) - int(values["auth_date"])
        if not -30 <= age <= max_age:
            raise ValueError("Expired initData")
        user = json.loads(values["user"])
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
            raise ValueError("Invalid user")
        return user
    except (ValueError, KeyError, TypeError) as error:
        raise AuthenticationError(T.ARCHIVE_AUTH_INVALID_TELEGRAM_SESSION) from error


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_pin(pin: str, salt: str | None = None) -> tuple[str, str]:
    if not isinstance(pin, str) or not pin.isascii() or not pin.isdigit() or not 6 <= len(pin) <= 12:
        raise ValueError(T.ARCHIVE_AUTH_PIN_LENGTH)
    salt = salt or secrets.token_hex(16)
    result = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 600_000)
    return result.hex(), salt


def verify_pin(pin: str, expected: str, salt: str) -> bool:
    try:
        actual, _ = hash_pin(pin, salt)
        return hmac.compare_digest(actual, expected)
    except ValueError:
        return False
