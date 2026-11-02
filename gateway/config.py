"""Gateway configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    rag_url: str = "http://localhost:8001"
    receipts_url: str = "http://localhost:8002"
    api_keys: tuple[str, ...] = ()  # empty = authentication disabled (local development only)
    rate_limit_per_min: int = 60
    rag_timeout_s: float = 30.0
    receipts_timeout_s: float = 90.0  # OCR + LayoutLMv3 on CPU is slower
    injection_mode: str = "block"  # block | flag | off
    injection_threshold: float = 0.8
    max_upload_mb: int = 10
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        keys = tuple(k.strip() for k in env("GATEWAY_API_KEYS", "").split(",") if k.strip())
        mode = env("INJECTION_MODE", cls.injection_mode).lower()
        if mode not in {"block", "flag", "off"}:
            raise ValueError("INJECTION_MODE must be block, flag or off")
        return cls(
            rag_url=env("RAG_URL", cls.rag_url).rstrip("/"),
            receipts_url=env("RECEIPTS_URL", cls.receipts_url).rstrip("/"),
            api_keys=keys,
            rate_limit_per_min=int(env("RATE_LIMIT_PER_MIN", str(cls.rate_limit_per_min))),
            rag_timeout_s=float(env("RAG_TIMEOUT_S", str(cls.rag_timeout_s))),
            receipts_timeout_s=float(env("RECEIPTS_TIMEOUT_S", str(cls.receipts_timeout_s))),
            injection_mode=mode,
            injection_threshold=float(env("INJECTION_THRESHOLD", str(cls.injection_threshold))),
            max_upload_mb=int(env("MAX_UPLOAD_MB", str(cls.max_upload_mb))),
            log_level=env("LOG_LEVEL", cls.log_level).upper(),
        )
