"""Small async load test for the gateway: throughput, latency percentiles and error rate.

Usage: python scripts/loadtest.py --url http://localhost:8000 --requests 300 --concurrency 20
Mix: 70% RAG questions, 30% receipt uploads. Writes loadtest_results.md.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time

import httpx

QUESTIONS = ["How many hours of sleep do adults need?", "What exercise does the guideline recommend?",
             "How much water should adults drink?", "What are the signs of dehydration?"]
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
       b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")


async def one(client: httpx.AsyncClient, headers: dict, results: list, sem: asyncio.Semaphore) -> None:
    async with sem:
        started = time.perf_counter()
        try:
            if random.random() < 0.7:
                r = await client.post("/v1/rag/ask", json={"question": random.choice(QUESTIONS)}, headers=headers)
                kind = "rag"
            else:
                r = await client.post("/v1/receipts/extract", files={"file": ("r.png", PNG, "image/png")},
                                      headers=headers)
                kind = "receipts"
            status = r.status_code
        except httpx.HTTPError:
            kind, status = "error", 0
        results.append((kind, status, (time.perf_counter() - started) * 1000))


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(round(p * len(ordered))) - 1)] if ordered else 0.0


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--requests", type=int, default=300)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--out", default="loadtest_results.md")
    args = ap.parse_args()

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    results: list = []
    sem = asyncio.Semaphore(args.concurrency)
    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=args.url, timeout=120) as client:
        await asyncio.gather(*(one(client, headers, results, sem) for _ in range(args.requests)))
    elapsed = time.perf_counter() - started

    lines = [f"{args.requests} requests, concurrency {args.concurrency}, {elapsed:.1f} s, "
             f"{args.requests / elapsed:.1f} requests/s.", "",
             "| Route | Requests | Success | p50 (ms) | p95 (ms) | p99 (ms) | Errors |",
             "|---|---|---|---|---|---|---|"]
    for kind in ("rag", "receipts", "error"):
        rows = [r for r in results if r[0] == kind]
        if not rows:
            continue
        lat = [r[2] for r in rows]
        ok = sum(1 for r in rows if r[1] == 200)
        lines.append(f"| {kind} | {len(rows)} | {ok} | {statistics.median(lat):.0f} | {pct(lat, .95):.0f} | "
                     f"{pct(lat, .99):.0f} | {len(rows) - ok} |")
    codes = {c: sum(1 for r in results if r[1] == c) for c in sorted({r[1] for r in results})}
    lines += ["", f"Status codes: {codes}"]
    text = "\n".join(lines)
    open(args.out, "w", encoding="utf-8").write(text + "\n")
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
