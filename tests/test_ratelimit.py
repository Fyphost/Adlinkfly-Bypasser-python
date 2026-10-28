"""Tests for :mod:`app.ratelimit` (pure, dependency-free)."""

from __future__ import annotations

import asyncio

import pytest

from app.ratelimit import SlidingWindowRateLimiter


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def run(coro):
    return asyncio.run(coro)


def test_allows_up_to_limit():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(3, 60, time_func=clock)
    results = [run(limiter.check("k")) for _ in range(3)]
    assert all(r.allowed for r in results)
    assert [r.remaining for r in results] == [2, 1, 0]


def test_blocks_over_limit():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(2, 60, time_func=clock)
    run(limiter.check("k"))
    run(limiter.check("k"))
    blocked = run(limiter.check("k"))
    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert blocked.retry_after > 0


def test_window_slides():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(1, 10, time_func=clock)
    assert run(limiter.check("k")).allowed is True
    assert run(limiter.check("k")).allowed is False
    clock.advance(11)  # window has fully elapsed
    assert run(limiter.check("k")).allowed is True


def test_keys_are_independent():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(1, 60, time_func=clock)
    assert run(limiter.check("a")).allowed is True
    assert run(limiter.check("b")).allowed is True  # different key, own budget
    assert run(limiter.check("a")).allowed is False


def test_retry_after_decreases_as_time_passes():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(1, 10, time_func=clock)
    run(limiter.check("k"))
    first = run(limiter.check("k")).retry_after
    clock.advance(4)
    second = run(limiter.check("k")).retry_after
    assert second < first


def test_prune_removes_expired_buckets():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(5, 10, time_func=clock)
    run(limiter.check("k"))
    clock.advance(11)
    removed = run(limiter.prune())
    assert removed == 1


def test_invalid_config():
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(0, 60)
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(1, 0)
