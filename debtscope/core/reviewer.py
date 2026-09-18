"""L3 reviewer: refines candidate findings and assigns confidence.

Only ``unused_function`` candidates need semantic review:

1. A cheap batched LLM call classifies dead / entry / uncertain.
2. Candidates the batch call finds *uncertain* are escalated to the harness
   agent loop (bounded per scan): the agent must gather tool evidence
   (callers, grep, source) before a verdict is accepted — a "dead" claim
   without a caller-search tool call is rejected by construction.
3. When no model is configured (or everything fails), candidates honestly
   stay at medium confidence with a "please confirm" message.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..config import Config
from ..harness.agent import Agent, investigate_dead_code
from ..harness.tools import build_kernel
from .llm import LLMClient
from .models import Confidence, Finding
from .python_indexer import Index

SNIPPET_LIMIT = 1800
AGENT_INVESTIGATE_LIMIT = 5
AGENT_MAX_STEPS = 5


@dataclass
class ReviewStats:
    candidates: int = 0
    suppressed: int = 0
    llm_reviewed: int = 0
    llm_dead: int = 0
    llm_entry: int = 0
    llm_uncertain: int = 0
    agent_reviewed: int = 0
    agent_dead: int = 0
    agent_entry: int = 0
    degraded: bool = False
    llm_error: str | None = None
    by_rule: dict[str, int] = field(default_factory=dict)


def _agent_enabled(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    flag = os.getenv("DEBTSCOPE_AGENT_REVIEW", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def review(
    findings: list[Finding], idx: Index, cfg: Config, *,
    cg=None, root: str | None = None, tracer=None,
    agent_deep: bool | None = None,
) -> tuple[list[Finding], ReviewStats]:
    stats = ReviewStats()
    stats.by_rule = {}
    for f in findings:
        stats.by_rule[f.rule_id] = stats.by_rule.get(f.rule_id, 0) + 1
    stats.candidates = len(findings)

    dead_candidates = [f for f in findings if f.rule_id == "unused_function"]
    others = [f for f in findings if f.rule_id != "unused_function"]
    kept = list(others)
    for f in others:
        if tracer:
            tracer.finding_accepted(f.rule_id, file=f.file, line=f.line,
                                    symbol=f.symbol or "", confidence=f.confidence)

    if not dead_candidates:
        return kept, stats

    verdicts: dict[str, tuple[str, str]] = {}
    client = LLMClient(cfg, tracer=tracer) if cfg.llm_enabled else None
    uncertain: list[Finding] = []
    key_map = {f"{f.file}:{f.line}:{f.symbol}": f for f in dead_candidates}

    if cfg.llm_enabled:
        items = []
        for f in dead_candidates:
            key = f"{f.file}:{f.line}:{f.symbol}"
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
            # Total failure (network/auth/quota) or no usable verdict.
            stats.degraded = True

    # Stage 2: bounded agent investigation for batch-uncertain candidates.
    use_agent = (
        client is not None and cg is not None
        and _agent_enabled(agent_deep)
    )
    agent_kernel = None
    if use_agent:
        for key, (label, _reason) in verdicts.items():
            if label == "uncertain" and key in key_map:
                uncertain.append(key_map[key])
        uncertain = uncertain[:AGENT_INVESTIGATE_LIMIT]
        if uncertain:
            agent_kernel = build_kernel(root or idx.root, idx, cg, tracer=tracer)
            agent = Agent(agent_kernel, client, max_steps=AGENT_MAX_STEPS,
                          tracer=tracer)
    for f in uncertain:
        key = f"{f.file}:{f.line}:{f.symbol}"
        item = {
            "key": key, "file": f.file, "symbol": f.symbol,
            "decorators": f.evidence.get("decorators", []),
            "snippet": f.evidence.get("snippet", "")[:SNIPPET_LIMIT],
        }
        result = investigate_dead_code(agent, item)
        if result.status != "ok" or not result.final:
            continue
        stats.agent_reviewed += 1
        verdict = result.final.get("verdict")
        reason = str(result.final.get("reason", ""))[:200]
        if verdict in ("dead", "entry", "uncertain"):
            verdicts[key] = (verdict, f"[agent] {reason}")
            if verdict == "dead":
                stats.agent_dead += 1
            elif verdict == "entry":
                stats.agent_entry += 1

    for f in dead_candidates:
        key = f"{f.file}:{f.line}:{f.symbol}"
        verdict = verdicts.get(key)
        if verdict is None:
            # No LLM verdict: conservative, honest wording.
            f.confidence = Confidence.MEDIUM
            f.evidence["review"] = "static_only"
            kept.append(f)
            if tracer:
                tracer.finding_accepted(f.rule_id, file=f.file, line=f.line,
                                        symbol=f.symbol or "", confidence="medium")
            continue
        label, reason = verdict
        if label == "entry":
            stats.suppressed += 1
            stats.llm_entry += 1
            if tracer:
                tracer.finding_rejected(f.rule_id, reason="llm_entry",
                                        file=f.file, symbol=f.symbol or "")
            continue
        if label == "dead":
            f.confidence = Confidence.HIGH
            f.message = f"AI 研判为疑似废弃代码：{f.symbol}（{reason or '全仓无调用且非框架入口'}）"
            f.evidence["review"] = f"llm_dead: {reason}"
            stats.llm_dead += 1
            kept.append(f)
            if tracer:
                tracer.finding_accepted(f.rule_id, file=f.file, line=f.line,
                                        symbol=f.symbol or "", confidence="high",
                                        evidence_tool="codegraph.callers")
        else:
            f.confidence = Confidence.MEDIUM
            f.evidence["review"] = f"llm_uncertain: {reason}"
            stats.llm_uncertain += 1
            kept.append(f)
            if tracer:
                tracer.finding_accepted(f.rule_id, file=f.file, line=f.line,
                                        symbol=f.symbol or "", confidence="medium")

    return kept, stats
