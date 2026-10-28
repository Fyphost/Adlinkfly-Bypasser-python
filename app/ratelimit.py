"""In-process sliding-window rate limiter.

A small, dependency-free (standard library only) async rate limiter used by the
API layer. It is intentionally simple and process-local: for a multi-instance
deployment, place a shared limiter (e.g. Redis) in front, or run behind an API
gateway that enforces limits. The pure design keeps it unit-testable without
FastAPI.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of a rate-limit check."""

    allowed: bool
    remaining: int
    retry_after: float


class SlidingWindowRateLimiter:
    """Allow at most ``max_requests`` per ``window_seconds`` per key.

    Parameters
    ----------
    max_requests:
        Maximum number of permitted requests within the window.
    window_seconds:
        Length of the sliding window, in seconds.
    time_func:
        Monotonic clock source (injectable for deterministic tests).
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._time = time_func
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> RateLimitResult:
        """Record a hit for ``key`` and report whether it is permitted."""

        now = self._time()
        cutoff = now - self.window_seconds
        async with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self.max_requests:
                retry_after = max(0.0, bucket[0] + self.window_seconds - now)
                return RateLimitResult(allowed=False, remaining=0, retry_after=retry_after)

            bucket.append(now)
            if not bucket:  # pragma: no cover - defensive
                del self._hits[key]
            remaining = self.max_requests - len(bucket)
            return RateLimitResult(allowed=True, remaining=remaining, retry_after=0.0)

    async def prune(self) -> int:
        """Drop empty/expired buckets to bound memory. Returns buckets removed."""

        now = self._time()
        cutoff = now - self.window_seconds
        removed = 0
        async with self._lock:
            for key in list(self._hits.keys()):
                bucket = self._hits[key]
                while bucket and bucket[0] <= cutoff:
                    bucket.popleft()
                if not bucket:
                    del self._hits[key]
                    removed += 1
        return removed

    def reset(self) -> None:
        """Clear all recorded state (primarily for tests)."""

        self._hits.clear()
