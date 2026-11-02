"""Prometheus metrics plus a small rolling window used by the /stats dashboard."""
from __future__ import annotations

import math
import threading
from collections import Counter as PyCounter
from collections import deque

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


class RollingStats:
    """Last N requests per route, for p50/p95 and error rate without an external database."""

    def __init__(self, size: int = 1000):
        self._data: dict[str, deque] = {}
        self._size = size
        self._lock = threading.Lock()

    def record(self, route: str, duration_ms: float, status: int) -> None:
        with self._lock:
            self._data.setdefault(route, deque(maxlen=self._size)).append((duration_ms, status))

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            copy = {route: list(d) for route, d in self._data.items()}
        out = {}
        for route, rows in copy.items():
            durations = [d for d, _ in rows]
            errors = sum(1 for _, s in rows if s >= 500)
            out[route] = {
                "count": len(rows),
                "p50_ms": round(percentile(durations, 0.50), 1),
                "p95_ms": round(percentile(durations, 0.95), 1),
                "error_rate": round(errors / len(rows), 4),
            }
        return out


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()  # per-app registry keeps tests independent
        r = self.registry
        self.requests = Counter("gateway_requests_total", "Requests handled",
                                ["route", "method", "status"], registry=r)
        self.duration = Histogram("gateway_request_duration_seconds", "End-to-end request latency",
                                  ["route"], buckets=LATENCY_BUCKETS, registry=r)
        self.upstream_duration = Histogram("gateway_upstream_duration_seconds", "Upstream call latency",
                                           ["service"], buckets=LATENCY_BUCKETS, registry=r)
        self.upstream_errors = Counter("gateway_upstream_errors_total", "Failed upstream calls",
                                       ["service", "kind"], registry=r)
        self.injection = Counter("gateway_injection_events_total", "Prompt-injection detections",
                                 ["stage", "action", "rule"], registry=r)
        self.receipts = Counter("gateway_receipts_total", "Receipts processed by review outcome",
                                ["needs_review"], registry=r)
        self.rate_limited = Counter("gateway_rate_limited_total", "Requests rejected by rate limit", registry=r)
        self.auth_failures = Counter("gateway_auth_failures_total", "Requests with a bad API key", registry=r)
        self.in_flight = Gauge("gateway_in_flight_requests", "Requests currently being handled", registry=r)
        self.rolling = RollingStats()
        self.totals: PyCounter = PyCounter()  # plain counters for the dashboard
