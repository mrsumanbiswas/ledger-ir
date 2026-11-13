"""OCR-confusion candidate generation and the repair-uniqueness rule (SPEC.md §1.5).

The candidate set for a low-confidence token is every variant obtained by
substituting any subset of its confusable characters (plus the as-read
original). The set is fully enumerated — no heuristics — which is what makes
the "exactly one survivor" uniqueness rule, and therefore the zero-hallucination
claim, honest. Pure Python, zero AWS imports.
"""
from __future__ import annotations

REPAIR_CONF_THRESHOLD = 0.9

# Bidirectional confusion alphabet (SPEC.md §1.5):
# 8<->B, 5<->S, 3<->8, 0<->O, 1<->I, 1<->7
CONFUSION: dict[str, str] = {
    "8": "3B", "3": "8", "B": "8",
    "5": "S", "S": "5",
    "0": "O", "O": "0",
    "1": "I7", "I": "1", "7": "1",
}

# Safety valve: a token with more variants than this is declared unrepairable
# (deterministically), which surfaces as NEEDS_REVIEW if an invariant is broken.
MAX_VARIANTS = 4096


def variants(text: str) -> list[str]:
    """All subset-substitution variants of ``text``, original first, deduplicated,
    in a deterministic (left-to-right positional) order."""
    options = [ch + CONFUSION.get(ch, "") for ch in text]
    count = 1
    for opt in options:
        count *= len(opt)
        if count > MAX_VARIANTS:
            return [text]
    out: list[str] = []
    seen: set[str] = set()
    # itertools.product semantics, spelled out so order is self-evidently
    # deterministic: position 0 varies slowest, original char always first.
    stack = [""]
    for opt in options:
        stack = [prefix + ch for prefix in stack for ch in opt]
    for s in stack:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def hamming(a: str, b: str) -> int:
    """Character positions where two equal-length strings differ."""
    assert len(a) == len(b)
    return sum(1 for x, y in zip(a, b, strict=True) if x != y)


def is_repair_eligible(conf: float) -> bool:
    return conf < REPAIR_CONF_THRESHOLD
