"""Calls to a backend service with timeouts, one retry on connection errors, and error mapping."""
from __future__ import annotations

import asyncio
import time

import httpx

from .metrics import Metrics


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str, kind: str):
        super().__init__(detail)
        self.status, self.detail, self.kind = status, detail, kind


class Upstream:
    def __init__(self, name: str, client: httpx.AsyncClient, metrics: Metrics):
        self.name, self.client, self.metrics = name, client, metrics

    async def request(self, method: str, path: str, request_id: str, **kwargs) -> httpx.Response:
        headers = {"X-Request-ID": request_id}
        for attempt in range(2):
            started = time.perf_counter()
            try:
                response = await self.client.request(method, path, headers=headers, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt == 0:  # a refused connection is safe to retry once
                    await asyncio.sleep(0.2)
                    continue
                raise self._fail(502, f"{self.name} is unreachable", "connect") from None
            except httpx.TimeoutException:
                raise self._fail(504, f"{self.name} timed out", "timeout") from None
            except httpx.HTTPError:
                raise self._fail(502, f"{self.name} transport error", "transport") from None
            finally:
                self.metrics.upstream_duration.labels(self.name).observe(time.perf_counter() - started)
            if response.status_code >= 500:
                raise self._fail(502, f"{self.name} returned {response.status_code}", "5xx")
            return response
        raise self._fail(502, f"{self.name} is unreachable", "connect")  # pragma: no cover

    def _fail(self, status: int, detail: str, kind: str) -> UpstreamError:
        self.metrics.upstream_errors.labels(self.name, kind).inc()
        return UpstreamError(status, detail, kind)
