"""Scan orchestration: index -> call graph -> entry points -> rules -> review.

Then reconcile findings, snapshot project health, build per-endpoint call
chains and attach debt onto chain nodes (interface radar).
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime

from ..config import Config
from ..harness.trace import RunRecorder
from . import health
from .callgraph import build_all_chains, build_call_graph, blast_radius
from .endpoints import discover_endpoints
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


def _make_tracer(db_path: str, mode: str) -> RunRecorder:
    """Derive the runs directory from the db layout.

    * managed projects: ``<base>/data/<pid>.db`` -> ``<base>/runs``
    * CLI local scans:  ``<dir>/debtscope.db``   -> ``<dir>/runs``
    """
    db_dir = os.path.realpath(os.path.dirname(db_path) or ".")
    if os.path.basename(db_dir) == "data":
        return RunRecorder(base_dir=os.path.dirname(db_dir), mode=mode)
    return RunRecorder(base_dir=db_dir, mode=mode)


def scan_repo(root: str, db_path: str, use_llm: bool = True, *,
              mode: str = "headless", project_id: str | None = None) -> dict:
    cfg = Config.from_env(use_llm=use_llm)

    indexer = PythonIndexer()
    idx = indexer.index(root)

    # static interface layer (never executes project code)
    cg = build_call_graph(idx)
    endpoints = discover_endpoints(idx)
    commit = git_commit(root)

    tracer = _make_tracer(db_path, mode)
    tracer.start(repo=root, project_id=project_id or "", commit=commit or "",
                 llm_enabled=cfg.llm_enabled, model=cfg.model if cfg.llm_enabled else "",
                 files=len(idx.files))

    store = Storage(db_path)
    try:
        store.seed_rules()
        specs = [RuleSpec.from_dict(r) for r in store.list_rules()]
        candidates = run_rules(idx, specs)
        kept, review_stats = review(candidates, idx, cfg, cg=cg, root=root,
                                    tracer=tracer)
        counts = store.reconcile(kept)
        active = store.list_findings(statuses=("open", "confirmed", "wontfix"))
        score = health.health_score(active)
        agg = health.aggregate(active)
        stats = {
            "files": len(idx.files),
            "loc": idx.n_lines,
            "symbols": len(idx.symbols),
            "endpoints": len(endpoints),
            "parse_errors": idx.parse_errors,
            "candidates": review_stats.candidates,
            "llm_enabled": cfg.llm_enabled,
            "llm_reviewed": review_stats.llm_reviewed,
            "agent_reviewed": review_stats.agent_reviewed,
            "agent_dead": review_stats.agent_dead,
            "agent_entry": review_stats.agent_entry,
            "llm_degraded": review_stats.degraded,
            "llm_error": review_stats.llm_error,
            "llm_model": cfg.model if cfg.llm_enabled else None,
            "suppressed_by_review": review_stats.suppressed,
            "rule_candidates": review_stats.by_rule,
            "rules_enabled": sum(1 for s in specs if s.enabled),
            "rules_custom": sum(1 for s in specs if not s.builtin),
            "trace_id": tracer.run_id,
            **agg,
        }
        snapshot = store.add_snapshot(
            commit=commit,
            score=score,
            total_open=len(active),
            new_count=counts["new"],
            resolved_count=counts["resolved"],
            stats=stats,
        )

        # pin debt onto interface chains
        chains = build_all_chains(cg, endpoints)
        blast = blast_radius(chains)
        ep_summary = store.reconcile_endpoints(
            [e.to_dict() for e in endpoints], chains, blast, active, snapshot.id
        )

        store.set_meta("repo_root", root)
        store.set_meta("last_scan", datetime.now().isoformat(timespec="seconds"))
        tracer.end(score=score, total_open=len(active), new=counts["new"],
                   resolved=counts["resolved"], endpoints=len(endpoints),
                   files=len(idx.files), symbols=len(idx.symbols),
                   llm_reviewed=review_stats.llm_reviewed,
                   agent_reviewed=review_stats.agent_reviewed,
                   suppressed=review_stats.suppressed)
    except Exception:
        tracer.event("run.error", error="scan failed")
        tracer.end(status="error")
        raise
    finally:
        store.close()
        tracer.close()

    return {
        "root": root,
        "commit": commit,
        "files": len(idx.files),
        "loc": idx.n_lines,
        "symbols": len(idx.symbols),
        "endpoints": len(endpoints),
        "chains": ep_summary["with_chains"],
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
            "agent_reviewed": review_stats.agent_reviewed,
            "agent_dead": review_stats.agent_dead,
            "agent_entry": review_stats.agent_entry,
            "degraded": review_stats.degraded,
            "llm_error": review_stats.llm_error,
        },
        "trace_id": tracer.run_id,
        "reconcile": counts,
        "score": score,
        "grade": health.grade(score),
        "total_open": len(active),
        "aggregate": agg,
    }
