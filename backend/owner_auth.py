"""Verify an owner access code without retaining it or its inputs."""

from collections import deque
from collections.abc import Callable
from hashlib import sha256
from hmac import compare_digest, digest
from math import ceil
from threading import Lock
from time import monotonic


_MAX_CODE_LENGTH = 256
_MAX_FAILURES = 5
_FAILURE_WINDOW_SECONDS = 600


def _code_digest(code: object) -> bytes | None:
    if not isinstance(code, str) or not 1 <= len(code) <= _MAX_CODE_LENGTH:
        return None
    try:
        return sha256(code.encode("utf-8")).digest()
    except UnicodeEncodeError:
        return None


class OwnerAccess:
    def __init__(self, code: str, signing_secret: str | bytes, *, clock: Callable[[], float] = monotonic) -> None:
        expected = _code_digest(code)
        if expected is None:
            raise ValueError("Set OWNER_ACCESS_CODE to an access code of 1 to 256 characters.")
        key = signing_secret.encode("utf-8") if isinstance(signing_secret, str) else signing_secret
        self.cookie_key = digest(key, b"livex-owner-cookie-v1\0" + expected, "sha256")
        self._expected = expected
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._next_cleanup = float("-inf")
        self._lock = Lock()

    def attempt(self, address: str, code: object) -> tuple[bool, int]:
        """Return whether the code matches and any required wait in seconds."""
        candidate = _code_digest(code)
        with self._lock:
            now = self._clock()
            cutoff = now - _FAILURE_WINDOW_SECONDS
            if now >= self._next_cleanup:
                self._failures = {key: times for key, times in self._failures.items() if times[-1] > cutoff}
                self._next_cleanup = now + _FAILURE_WINDOW_SECONDS
            failures = self._failures.setdefault(address, deque())
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if len(failures) >= _MAX_FAILURES:
                return False, max(1, ceil(failures[0] + _FAILURE_WINDOW_SECONDS - now))
            matches = compare_digest(candidate if candidate is not None else bytes(32), self._expected)
            if candidate is not None and matches:
                self._failures.pop(address)
                return True, 0
            failures.append(now)
            return False, 0
