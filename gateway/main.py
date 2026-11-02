"""AI gateway: one secured, observable entry point in front of the RAG and receipt services."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from .config import Settings
from .injection import InjectionFilter
from .logging_setup import request_id_var, setup_logging
from .metrics import Metrics
from .security import RateLimiter, key_id, key_matches
from .upstream import Upstream, UpstreamError

VERSION = "1.0.0"
STATIC = Path(__file__).parent / "static"
log = logging.getLogger("gateway.access")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF")  # PNG, JPEG, WebP container
WITHHELD = "Answer withheld: the retrieved documents contain instruction-like content."
REDACTED = "[redacted: passage contains instruction-like content]"

class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    k: int = Field(default=4, ge=1, le=10)


def _json(data, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=headers)


def create_app(
    settings: Settings | None = None,
    rag_client: httpx.AsyncClient | None = None,
    receipts_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    cfg = settings or Settings.from_env()
    setup_logging(cfg.log_level)
    metrics = Metrics()
    injection = InjectionFilter(cfg.injection_threshold)
    limiter = RateLimiter(cfg.rate_limit_per_min)
    started = time.time()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned: list[httpx.AsyncClient] = []
        if rag_client is None:
            owned.append(httpx.AsyncClient(base_url=cfg.rag_url, timeout=cfg.rag_timeout_s))
        if receipts_client is None:
            owned.append(httpx.AsyncClient(base_url=cfg.receipts_url, timeout=cfg.receipts_timeout_s))
        rag = rag_client or owned[0]
        receipts = receipts_client or owned[-1]
        app.state.rag = Upstream("rag", rag, metrics)
        app.state.receipts = Upstream("receipts", receipts, metrics)
        if not cfg.api_keys:
            logging.getLogger("gateway").warning("GATEWAY_API_KEYS is empty: authentication is disabled")
        yield
        for client in owned:
            await client.aclose()

    app = FastAPI(title="AI Gateway", version=VERSION, lifespan=lifespan)

    # ---- cross-cutting concerns: request id, auth, rate limit, metrics, access log ----
    @app.middleware("http")
    async def gateway_middleware(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request.state.request_id = rid
        token = request_id_var.set(rid)
        started_at = time.perf_counter()
        metrics.in_flight.inc()
        client = "-"
        try:
            denied = None
            if request.url.path.startswith("/v1/"):
                denied, client = _guard(request)
            response = denied or await call_next(request)
        except Exception:
            logging.getLogger("gateway").exception("unhandled error")
            response = _json({"detail": "internal error"}, 500)
        finally:
            metrics.in_flight.dec()

        duration = time.perf_counter() - started_at
        route_obj = request.scope.get("route")
        route = getattr(route_obj, "path", None) or "unmatched"
        metrics.requests.labels(route, request.method, str(response.status_code)).inc()
        metrics.duration.labels(route).observe(duration)
        metrics.rolling.record(route, duration * 1000, response.status_code)
        response.headers["X-Request-ID"] = rid
        log.info("request", extra={"extra_fields": {
            "method": request.method, "route": route, "status": response.status_code,
            "duration_ms": round(duration * 1000, 1), "client": client}})
        request_id_var.reset(token)
        return response

    def _guard(request: Request) -> tuple[JSONResponse | None, str]:
        if cfg.api_keys:
            supplied = request.headers.get("x-api-key", "")
            if not key_matches(supplied, cfg.api_keys):
                metrics.auth_failures.inc()
                metrics.totals["auth_failures"] += 1
                return _json({"detail": "invalid or missing API key"}, 401), "anonymous"
            client = "key:" + key_id(supplied)
        else:
            client = "ip:" + (request.client.host if request.client else "unknown")
        if not limiter.allow(client):
            metrics.rate_limited.inc()
            metrics.totals["rate_limited"] += 1
            retry = limiter.retry_after(client)
            return _json({"detail": "rate limit exceeded"}, 429, {"Retry-After": str(retry)}), client
        return None, client

    @app.exception_handler(UpstreamError)
    async def upstream_error_handler(request: Request, exc: UpstreamError):
        return _json({"detail": exc.detail, "upstream_error": exc.kind}, exc.status)

    def _record_injection(stage: str, action: str, rules: list[str]) -> None:
        for rule in rules:
            metrics.injection.labels(stage, action, rule).inc()
        metrics.totals[f"injection_{action}"] += 1

    # ---- API routes ----
    @app.post("/v1/rag/ask")
    async def rag_ask(body: AskRequest, request: Request):
        rid = request.state.request_id
        verdict = injection.check(body.question) if cfg.injection_mode != "off" else None
        security = {"request_score": verdict.score if verdict else 0.0,
                    "request_rules": verdict.rules if verdict else [],
                    "retrieved_flagged": [], "action": "none"}

        if verdict and verdict.suspicious:
            if verdict.blocked and cfg.injection_mode == "block":
                _record_injection("request", "blocked", verdict.rules)
                return _json({"detail": "Request blocked: possible prompt injection",
                              "error": "prompt_injection_suspected",
                              "score": verdict.score, "rules": verdict.rules}, 400)
            _record_injection("request", "flagged", verdict.rules)
            security["action"] = "flagged"

        response = await app.state.rag.request("POST", "/ask", rid, json=body.model_dump())
        if response.status_code >= 400:
            return _json(_safe_json(response), response.status_code)
        data = _safe_json(response)
        if not isinstance(data, dict) or "answer" not in data:
            raise UpstreamError(502, "rag returned an unexpected response", "bad_response")

        if cfg.injection_mode != "off":
            withheld = False
            for source in data.get("sources", []):
                found = injection.check(str(source.get("text", "")))
                if not found.suspicious:
                    continue
                security["retrieved_flagged"].append(source.get("rank"))
                if found.blocked and cfg.injection_mode == "block":
                    _record_injection("retrieved", "blocked", found.rules)
                    source["text"] = REDACTED  # do not pass injected text on to the client or an agent
                    withheld = True
                else:
                    _record_injection("retrieved", "flagged", found.rules)
            if withheld:
                data["answer"], data["mode"] = WITHHELD, "withheld"
                security["action"] = "withheld"
            elif security["retrieved_flagged"] and security["action"] == "none":
                security["action"] = "flagged"
        data["security"] = security
        return data

    @app.post("/v1/receipts/extract")
    async def receipts_extract(request: Request, file: UploadFile = File(...)):
        limit = cfg.max_upload_mb * 1024 * 1024
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"File larger than {cfg.max_upload_mb} MB")
        if not data.startswith(_IMAGE_MAGIC):
            raise HTTPException(415, "Upload a JPEG, PNG or WebP image")
        response = await app.state.receipts.request(
            "POST", "/extract", request.state.request_id,
            files={"file": (file.filename or "receipt", data, file.content_type or "image/jpeg")})
        body = _safe_json(response)
        if response.status_code < 400 and isinstance(body, dict):
            flag = bool(body.get("needs_review", True))
            metrics.receipts.labels(str(flag).lower()).inc()
            metrics.totals["receipts_review" if flag else "receipts_auto"] += 1
        return _json(body, response.status_code)

    # ---- operations ----
    @app.get("/health")
    async def health():
        return {"status": "ok", "version": VERSION, "uptime_s": round(time.time() - started)}

    async def _probe(name: str, upstream: Upstream) -> tuple[str, dict]:
        t0 = time.perf_counter()
        try:
            resp = await upstream.client.get("/health", timeout=2.0)
            state = resp.json().get("status", "unknown") if resp.status_code == 200 else f"http_{resp.status_code}"
        except (httpx.HTTPError, ValueError):
            state = "unreachable"
        return name, {"status": state, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    @app.get("/ready")
    async def ready():
        results = dict(await asyncio.gather(_probe("rag", app.state.rag), _probe("receipts", app.state.receipts)))
        ok = all(r["status"] == "ok" for r in results.values())
        return _json({"status": "ready" if ok else "not_ready", "upstreams": results}, 200 if ok else 503)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics():
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/stats")
    async def stats():
        t = metrics.totals
        return {
            "uptime_s": round(time.time() - started),
            "routes": metrics.rolling.snapshot(),
            "security": {"injection_blocked": t["injection_blocked"], "injection_flagged": t["injection_flagged"],
                         "rate_limited": t["rate_limited"], "auth_failures": t["auth_failures"]},
            "receipts": {"auto": t["receipts_auto"], "needs_review": t["receipts_review"]},
        }

    @app.get("/", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    async def dashboard():
        return FileResponse(STATIC / "dashboard.html")

    return app


def _safe_json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        raise UpstreamError(502, "upstream returned invalid JSON", "bad_response") from None


app = create_app()
