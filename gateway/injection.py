"""Rule-based prompt-injection detector.

This is a baseline, not a complete defence: it catches common, well-known phrasings after
normalising Unicode tricks (zero-width characters, full-width letters, look-alike letters).
Paraphrased or novel attacks can bypass it. Use it as one layer next to least-privilege
tools, output checks and human review. See README for measured detection and false-positive rates.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

MAX_SCAN_CHARS = 20_000  # bound CPU time on very long retrieved passages

# Common look-alike letters (Cyrillic and Greek) mapped to Latin.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ѕ": "s",
    "ј": "j", "ԁ": "d", "ɡ": "g", "ο": "o", "α": "a", "ε": "e", "ι": "i", "ν": "v", "ρ": "p",
})

# (rule name, weight 0-1, regex). Weights combine with noisy-OR: 1 - prod(1 - w).
_RULES: list[tuple[str, float, str]] = [
    ("ignore_previous", 0.90,
     r"\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}"
     r"\b(?:previous|prior|above|earlier|preceding|all|any|your|system)\b[^.\n]{0,30}"
     r"\b(?:instructions?|prompts?|rules?|directions?|guidelines?|constraints?)\b"),
    ("multilingual_ignore", 0.85,
     r"\bignor[ae]\w*\b[^.\n]{0,25}\b(?:instrucciones|instrucciones anteriores)\b"
     r"|\bignorez?\b[^.\n]{0,25}\binstructions? pr[ée]c[ée]dentes?\b"
     r"|\bignorier\w*\b[^.\n]{0,25}\b(?:anweisungen|vorherigen)\b"),
    ("reveal_system_prompt", 0.85,
     r"\b(?:reveal|show|print|repeat|display|leak|output|recite|copy)\b[^.\n]{0,40}"
     r"\b(?:system|hidden|initial|original|secret|internal)\b[^.\n]{0,20}"
     r"\b(?:prompt|instructions?|message|rules)\b"
     r"|\b(?:what(?:'s| is| are)|tell me)\b[^.\n]{0,20}\byour\b[^.\n]{0,15}"
     r"\b(?:system prompt|instructions|initial prompt|hidden prompt)\b"),
    ("role_override", 0.60,
     r"\byou are (?:now|no longer)\b|\bfrom now on,? you\b|\bpretend (?:to be|you are|that you)\b"
     r"|\bact as (?:if you|though you|an? (?:unrestricted|unfiltered|evil|jailbroken))\b"),
    ("new_instructions", 0.55,
     r"\b(?:new|updated|real|actual|additional|important) (?:instructions?|task|directives?)\b\s*[:\-]"),
    ("fake_system_tags", 0.85,
     r"<\|?(?:system|im_start|im_end|endoftext)\|?>|\[/?(?:inst|system)\]|<<sys>>"
     r"|^\s*###\s*(?:system|instructions?)\b|\bbegin (?:system|hidden) (?:prompt|instructions?)\b"),
    ("jailbreak_terms", 0.70,
     r"\b(?:dan mode|developer mode|jailbreak(?:ed)?|do anything now|god mode)\b"
     r"|\bwithout (?:any )?(?:restrictions|filters|limitations|censorship)\b"),
    ("exfiltration", 0.70,
     r"\b(?:send|post|forward|email|upload|exfiltrate|transmit|leak)\b[^.\n]{0,60}"
     r"(?:https?://|www\.|\b[\w.+-]+@[\w-]+\.[\w.]+)"),
    ("answer_override", 0.45,
     r"\b(?:respond|reply|answer|say|output)\b[^.\n]{0,20}\b(?:only|exactly|always|just)\b[^.\n]{0,15}\bwith\b"),
    ("encoded_payload", 0.50, r"[a-z0-9+/]{100,}={0,2}"),
]
_COMPILED = [(name, weight, re.compile(pattern)) for name, weight, pattern in _RULES]


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text[:MAX_SCAN_CHARS])
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")  # zero-width, bidi marks
    text = text.translate(_CONFUSABLES).lower()
    return re.sub(r"[ \t\r\f\v]+", " ", text)


@dataclass(frozen=True)
class Verdict:
    score: float = 0.0
    rules: list[str] = field(default_factory=list)
    threshold: float = 0.8

    @property
    def blocked(self) -> bool:
        return self.score >= self.threshold

    @property
    def suspicious(self) -> bool:
        return self.score >= 0.5


class InjectionFilter:
    def __init__(self, threshold: float = 0.8):
        self.threshold = threshold

    def check(self, text: str) -> Verdict:
        clean = normalise(text)
        matched = [(name, w) for name, w, rx in _COMPILED if rx.search(clean)]
        survive = 1.0
        for _, weight in matched:
            survive *= 1.0 - weight
        return Verdict(round(1.0 - survive, 3), [name for name, _ in matched], self.threshold)
