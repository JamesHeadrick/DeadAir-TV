"""Username/password auth: password hashing, sessions, login throttling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

SESSION_COOKIE = "deadair_session"
SESSION_DAYS = 30
MIN_PASSWORD_LEN = 8

# scrypt parameters (~16 MiB, ~50 ms per hash on a Pi 5).
_N, _R, _P = 2**14, 8, 1


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Lets a login for an unknown username take as long as a real one.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def check_login(password: str, stored: str | None) -> bool:
    if stored is None:
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, stored)


def new_session_token() -> tuple[str, str]:
    """(token for the cookie, hash to store in the database)."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validate_new_credentials(username: str, password: str | None) -> str | None:
    """Error message, or None if acceptable. password=None skips the password check."""
    if not username or len(username) > 64 or not username.isprintable() or username != username.strip():
        return "Username must be 1-64 printable characters without leading/trailing spaces"
    if password is not None and len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters"
    return None


class LoginThrottle:
    """Blocks a client IP after too many failed logins in a window."""

    def __init__(self, max_failures: int = 10, window_s: float = 900):
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._prune(key, time.time())) >= self.max_failures

    def failed(self, key: str) -> None:
        now = time.time()
        self._prune(key, now).append(now)

    def succeeded(self, key: str) -> None:
        self._failures.pop(key, None)
