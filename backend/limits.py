"""Process-local admission control for chat requests and Gemini calls."""

from collections import deque
from collections.abc import Callable
from datetime import datetime
from math import ceil
from threading import Lock
from time import monotonic
from zoneinfo import ZoneInfo


_WINDOW_SECONDS = 600
_PACIFIC = ZoneInfo("America/Los_Angeles")


class RateLimits:
    def __init__(self, turns_per_window: int, model_calls_per_day: int, *, clock: Callable[[], float] = monotonic) -> None:
        if turns_per_window < 1 or model_calls_per_day < 1:
            raise ValueError("Rate limits must be positive integers.")
        self._turn_limit = turns_per_window
        self._model_limit = model_calls_per_day
        self._clock = clock
        self._turns: dict[str, deque[float]] = {}
        self._next_cleanup = float("-inf")
        self._model_day = None
        self._model_count = 0
        self._lock = Lock()

    def request_retry_after(self, address: str) -> int:
        """Admit this request, or return seconds until the address can retry."""
        with self._lock:
            now = self._clock()
            cutoff = now - _WINDOW_SECONDS
            if now >= self._next_cleanup:
                self._turns = {ip: times for ip, times in self._turns.items() if times[-1] > cutoff}
                self._next_cleanup = now + _WINDOW_SECONDS
            times = self._turns.setdefault(address, deque())
            while times and times[0] <= cutoff:
                times.popleft()
            if len(times) >= self._turn_limit:
                return max(1, ceil(times[0] + _WINDOW_SECONDS - now))
            times.append(now)
            return 0

    def claim_model_call(self, now: datetime) -> bool:
        day = now.astimezone(_PACIFIC).date()
        with self._lock:
            if self._model_day is None or day > self._model_day:
                self._model_day, self._model_count = day, 0
            if self._model_count >= self._model_limit:
                return False
            self._model_count += 1
            return True
