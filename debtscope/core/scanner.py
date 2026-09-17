"""Scan orchestration: index -> rules -> review -> reconcile -> snapshot."""
from __future__ import annotations

import subprocess
from datetime import datetime

from ..config import Config
from . import health
from .python_indexer import PythonIndexer
from .reviewer import review
from .rules import RuleSpec, run_rules
from .storage import Storage


def git_commit(root: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def scan_repo(root: str, db_path: str, use_llm: bool = True) -> dict:
    cfg = Config.from_env(use_llm=use_llm)

    indexer = PythonIndexer()
    idx = indexer.index(root)

    store = Storage(db_path)
    try:
        store.seed_rules()
        specs = [RuleSpec.from_dict(r) for r in store.list_rules()]
        candidates = run_rules(idx, specs)
        kept, review_stats = review(candidates, idx, cfg)
        counts = store.reconcile(kept)
        active = store.list_findings(statuses=("open", "confirmed", "wontfix"))
        score = health.health_score(active)
        agg = health.aggregate(active)
        commit = git_commit(root)
        stats = {
            "files": len(idx.files),
            "loc": idx.n_lines,
            "symbols": len(idx.symbols),
            "parse_errors": idx.parse_errors,
            "candidates": review_stats.candidates,
            "llm_enabled": cfg.llm_enabled,
            "llm_reviewed": review_stats.llm_reviewed,
            "llm_degraded": review_stats.degraded,
            "llm_error": review_stats.llm_error,
            "llm_model": cfg.model if cfg.llm_enabled else None,
            "suppressed_by_review": review_stats.suppressed,
            "rule_candidates": review_stats.by_rule,
            "rules_enabled": sum(1 for s in specs if s.enabled),
            "rules_custom": sum(1 for s in specs if not s.builtin),
            **agg,
        }
        store.add_snapshot(
            commit=commit,
            score=score,
            total_open=len(active),
            new_count=counts["new"],
            resolved_count=counts["resolved"],
            stats=stats,
        )
        store.set_meta("repo_root", root)
        store.set_meta("last_scan", datetime.now().isoformat(timespec="seconds"))
    finally:
        store.close()

    return {
        "root": root,
        "commit": commit,
        "files": len(idx.files),
        "loc": idx.n_lines,
        "symbols": len(idx.symbols),
        "parse_errors": idx.parse_errors,
        "candidates": review_stats.candidates,
        "kept": len(kept),
        "review": {
            "llm_enabled": cfg.llm_enabled,
            "llm_model": cfg.model if cfg.llm_enabled else None,
            "llm_reviewed": review_stats.llm_reviewed,
            "llm_dead": review_stats.llm_dead,
            "llm_entry_suppressed": review_stats.llm_entry,
            "llm_uncertain": review_stats.llm_uncertain,
            "degraded": review_stats.degraded,
            "llm_error": review_stats.llm_error,
        },
        "reconcile": counts,
        "score": score,
        "grade": health.grade(score),
        "total_open": len(active),
        "aggregate": agg,
    }
