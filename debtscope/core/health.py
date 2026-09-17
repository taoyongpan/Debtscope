"""Health score and aggregation.

The score is a deliberately transparent weighted deduction (not a black-box
AI number): only active, non-low-confidence findings count.
"""
from __future__ import annotations

from collections import Counter

from .models import Confidence, SCORED_STATUSES, Severity

WEIGHTS = {Severity.HIGH: 5, Severity.MEDIUM: 2, Severity.LOW: 1}


def health_score(findings: list[dict]) -> int:
    penalty = 0
    for f in findings:
        if f["status"] not in SCORED_STATUSES:
            continue
        if f["confidence"] == Confidence.LOW:
            continue
        penalty += WEIGHTS.get(f["severity"], 1)
    return max(0, 100 - penalty)


def grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    return "D"


def aggregate(findings: list[dict]) -> dict:
    by_rule = Counter(f["rule_id"] for f in findings)
    by_severity = Counter(f["severity"] for f in findings)
    by_confidence = Counter(f["confidence"] for f in findings)
    by_file = Counter(f["file"] for f in findings)
    by_status = Counter(f["status"] for f in findings)
    return {
        "by_rule": dict(by_rule.most_common()),
        "by_severity": dict(by_severity),
        "by_confidence": dict(by_confidence),
        "by_file": dict(by_file.most_common(10)),
        "by_status": dict(by_status),
    }
