import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import Settings
from gateway.main import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

RAG_OK = {
    "answer": "Adults need seven hours [1].", "mode": "llm",
    "sources": [{"rank": 1, "score": 0.9, "source": "sleep.txt", "page": 1,
                 "text": "Adults need at least seven hours of sleep."}],
    "latency_ms": {"retrieval": 3.0, "generation": 20.0},
}
RECEIPT_OK = {"items": [{"name": "Tea", "price": 1000.0}], "totals": {"total": 1000.0},
              "needs_review": False, "review_reasons": [], "checks": []}


class Upstreams:
    """Records calls and serves canned responses for both backends."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.rag_response = lambda req: httpx.Response(200, json=RAG_OK)
        self.receipt_response = lambda req: httpx.Response(200, json=RECEIPT_OK)
        self.health = {"rag": "ok", "receipts": "ok"}

    def handler(self, name):
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": self.health[name]})
            self.calls.append(request)
            return (self.rag_response if name == "rag" else self.receipt_response)(request)
        return handle

    def paths(self):
        return [c.url.path for c in self.calls]


@pytest.fixture()
def upstreams():
    return Upstreams()


@pytest.fixture()
def make_client(upstreams):
    clients = []

    def build(**settings) -> TestClient:
        rag = httpx.AsyncClient(transport=httpx.MockTransport(upstreams.handler("rag")), base_url="http://rag")
        rec = httpx.AsyncClient(transport=httpx.MockTransport(upstreams.handler("receipts")),
                                base_url="http://receipts")
        client = TestClient(create_app(Settings(rate_limit_per_min=0, **settings), rag, rec))
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for c in clients:
        c.__exit__(None, None, None)
