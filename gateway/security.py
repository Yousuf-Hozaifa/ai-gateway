"""API-key check and an in-memory token-bucket rate limiter."""
from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable


def key_matches(candidate: str, valid_keys: tuple[str, ...]) -> bool:
    cand = candidate.encode()
    return any(hmac.compare_digest(cand, k.encode()) for k in valid_keys)  # constant-time


def key_id(key: str) -> str:
    """Short non-reversible identifier, safe to log and use as a metrics/limiter label."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


class RateLimiter:
    """Token bucket per client. In-memory, so it applies per process; use Redis to share across replicas."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, client: str) -> bool:
        if self.capacity <= 0:  # 0 disables limiting
            return True
        now = self.clock()
        tokens, last = self._buckets.get(client, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        allowed = tokens >= 1.0
        self._buckets[client] = (tokens - 1.0 if allowed else tokens, now)
        if len(self._buckets) > 10_000:  # drop idle clients so memory stays bounded
            self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < 60}
        return allowed

    def retry_after(self, client: str) -> int:
        tokens, _ = self._buckets.get(client, (self.capacity, 0.0))
        return max(1, int((1.0 - tokens) / self.rate) + 1) if self.rate else 1
