"""L3 reviewer: refines candidate findings and assigns confidence.

Only ``unused_function`` candidates need semantic review. The LLM decides
dead vs. framework-entry-point vs. uncertain; when no model is configured
(or the call fails), every candidate conservatively stays at medium
confidence with an honest "please confirm" message.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from .llm import LLMClient
from .models import Confidence, Finding
from .python_indexer import Index

SNIPPET_LIMIT = 1800


@dataclass
class ReviewStats:
    candidates: int = 0
    suppressed: int = 0
    llm_reviewed: int = 0
    llm_dead: int = 0
    llm_entry: int = 0
    llm_uncertain: int = 0
    degraded: bool = False
    llm_error: str | None = None
    by_rule: dict[str, int] = field(default_factory=dict)


def review(
    findings: list[Finding], idx: Index, cfg: Config
) -> tuple[list[Finding], ReviewStats]:
    stats = ReviewStats()
    stats.by_rule = {}
    for f in findings:
        stats.by_rule[f.rule_id] = stats.by_rule.get(f.rule_id, 0) + 1
    stats.candidates = len(findings)

    dead_candidates = [f for f in findings if f.rule_id == "unused_function"]
    others = [f for f in findings if f.rule_id != "unused_function"]
    kept = list(others)

    if not dead_candidates:
        return kept, stats

    verdicts: dict[str, tuple[str, str]] = {}
    if cfg.llm_enabled:
        client = LLMClient(cfg)
        items, key_map = [], {}
        for f in dead_candidates:
            key = f"{f.file}:{f.line}:{f.symbol}"
            key_map[key] = f
            items.append({
                "key": key,
                "file": f.file,
                "symbol": f.symbol,
                "decorators": f.evidence.get("decorators", []),
                "snippet": f.evidence.get("snippet", "")[:SNIPPET_LIMIT],
            })
        verdicts, err = client.classify_dead_code(items)
        stats.llm_error = err
        if verdicts:
            stats.llm_reviewed = len(verdicts)
        if err or not verdicts:
            # Total failure (network/auth/quota) or no usable verdict: the
            # scan stays honest — every candidate keeps a "please confirm"
            # state and the reason surfaces in the scan summary / dashboard.
            stats.degraded = True

    for f in dead_candidates:
        key = f"{f.file}:{f.line}:{f.symbol}"
        verdict = verdicts.get(key)
        if verdict is None:
            # No LLM verdict: conservative, honest wording.
            f.confidence = Confidence.MEDIUM
            f.evidence["review"] = "static_only"
            kept.append(f)
            continue
        label, reason = verdict
        if label == "entry":
            stats.suppressed += 1
            stats.llm_entry += 1
            continue
        if label == "dead":
            f.confidence = Confidence.HIGH
            f.message = f"AI 研判为疑似废弃代码：{f.symbol}（{reason or '全仓无调用且非框架入口'}）"
            f.evidence["review"] = f"llm_dead: {reason}"
            stats.llm_dead += 1
            kept.append(f)
        else:
            f.confidence = Confidence.MEDIUM
            f.evidence["review"] = f"llm_uncertain: {reason}"
            stats.llm_uncertain += 1
            kept.append(f)

    return kept, stats
