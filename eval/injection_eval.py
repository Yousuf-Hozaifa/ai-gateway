"""Measure the injection filter on a labelled set: detection rate and false-positive rate.

Usage: python -m eval.injection_eval [--cases eval/injection_cases.jsonl] [--threshold 0.8]

'blocked' means the gateway would reject the request. 'flagged' means score >= 0.5 (annotated, not rejected).
The bundled set is small and hand-written; add your own cases (published datasets, real traffic) to make
the numbers meaningful.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from gateway.injection import InjectionFilter


def sweep(rows: list[dict]) -> list[str]:
    scored = [(r["label"], InjectionFilter().check(r["text"]).score) for r in rows]
    n_a = sum(1 for label, _ in scored if label == "attack")
    n_b = len(scored) - n_a
    out = []
    for t in (0.5, 0.6, 0.7, 0.8, 0.9):
        caught = sum(1 for label, s in scored if label == "attack" and s >= t)
        wrong = sum(1 for label, s in scored if label == "benign" and s >= t)
        out.append(f"| {t} | {caught}/{n_a} ({caught / n_a:.0%}) | {wrong}/{n_b} ({wrong / n_b:.0%}) |")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", default="eval/injection_cases.jsonl")
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--out", default="eval/injection_results.md")
    args = ap.parse_args()

    f = InjectionFilter(args.threshold)
    rows = [json.loads(line) for line in Path(args.cases).read_text(encoding="utf-8").splitlines() if line.strip()]
    attacks = [r for r in rows if r["label"] == "attack"]
    benign = [r for r in rows if r["label"] == "benign"]

    per_cat = defaultdict(lambda: [0, 0])  # category -> [blocked, total]
    missed, false_blocks, false_flags = [], [], []
    for r in attacks:
        v = f.check(r["text"])
        per_cat[r["category"]][1] += 1
        per_cat[r["category"]][0] += v.blocked
        if not v.blocked:
            missed.append((r["category"], v.score, r["text"]))
    for r in benign:
        v = f.check(r["text"])
        if v.blocked:
            false_blocks.append((v.score, r["text"]))
        elif v.suspicious:
            false_flags.append((v.score, r["text"]))

    n_a, n_b = len(attacks), len(benign)
    blocked = n_a - len(missed)
    flagged_or_blocked = sum(f.check(r["text"]).suspicious for r in attacks)
    lines = [
        f"Cases: {n_a} attacks, {n_b} benign. Threshold {args.threshold}.", "",
        "| Metric | Value |", "|---|---|",
        f"| Attacks blocked | {blocked}/{n_a} ({blocked / n_a:.0%}) |",
        f"| Attacks blocked or flagged | {flagged_or_blocked}/{n_a} ({flagged_or_blocked / n_a:.0%}) |",
        f"| Benign wrongly blocked | {len(false_blocks)}/{n_b} ({len(false_blocks) / n_b:.0%}) |",
        f"| Benign wrongly flagged (not blocked) | {len(false_flags)}/{n_b} ({len(false_flags) / n_b:.0%}) |",
        "", "| Attack category | Blocked |", "|---|---|",
        *[f"| {c} | {b}/{t} |" for c, (b, t) in per_cat.items()],
        "", "Threshold trade-off (higher = fewer false blocks, more missed attacks):", "",
        "| Threshold | Attacks blocked | Benign wrongly blocked |", "|---|---|---|",
        *sweep(rows),
        "", "Missed attacks:", *[f"- [{c}] score {s}: {t!r}" for c, s, t in missed],
        "", "Benign wrongly blocked:", *[f"- score {s}: {t!r}" for s, t in false_blocks],
        "", "Benign wrongly flagged:", *[f"- score {s}: {t!r}" for s, t in false_flags],
    ]
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
