"""Every run is traceable: append-only JSONL session records.

A run is one scan or one interactive agent task. Events are written as JSON
Lines under ``~/.debtscope/runs/`` (or a ``runs/`` directory next to an
explicit base, e.g. per-project setups):

    {"ts": ..., "seq": 0, "type": "run.start", "repo": ..., "mode": ...}
    {"ts": ..., "seq": 1, "type": "tool.call", "id": "t1", "tool": ..., "args": ...}
    {"ts": ..., "seq": 2, "type": "tool.result", "id": "t1", "ms": 4, "bytes": 812}
    {"ts": ..., "seq": 3, "type": "llm.call", "model": ..., "prompt_tokens": ...}
    {"ts": ..., "seq": 4, "type": "finding.accepted", "rule": ..., "evidence_tool": ...}
    {"ts": ..., "seq": 5, "type": "run.end", "score": 79, "ms": 3210}

The files support misjudgment attribution, token-cost accounting, audit and
offline replay. Writes are best-effort: tracing must never break a scan.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class NullTracer:
    """No-op tracer used when recording is disabled or in unit tests."""

    run_id = None

    def start(self, **meta): return None
    def event(self, _type, **payload): return None
    def tool_call(self, *a, **k): return None
    def tool_result(self, *a, **k): return None
    def llm_call(self, *a, **k): return None
    def llm_error(self, *a, **k): return None
    def finding_accepted(self, *a, **k): return None
    def finding_rejected(self, *a, **k): return None
    def end(self, **summary): return None
    def close(self): return None


@dataclass
class RunRecorder:
    base_dir: str | None = None
    mode: str = "headless"

    def __post_init__(self):
        self.base = self.base_dir or os.path.join(
            os.path.expanduser("~"), ".debtscope")
        self.runs_dir = os.path.join(self.base, "runs")
        self.run_id: str | None = None
        self.path: str | None = None
        self._fh = None
        self._seq = 0
        self._started = 0.0

    # -- lifecycle ----------------------------------------------------------

    def start(self, **meta) -> str:
        os.makedirs(self.runs_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{stamp}-{secrets.token_hex(3)}"
        self.path = os.path.join(self.runs_dir, f"{self.run_id}.jsonl")
        self._fh = open(self.path, "w", encoding="utf-8")
        self._seq = 0
        self._started = time.time()
        self.event("run.start", mode=self.mode, **meta)
        return self.run_id

    def event(self, _type: str, **payload) -> None:
        if self._fh is None:
            return
        record = {"ts": _now(), "seq": self._seq, "type": _type, **payload}
        self._seq += 1
        try:
            self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
        except (OSError, TypeError, ValueError):
            pass

    def end(self, **summary) -> None:
        if self._fh is None:
            return
        self.event("run.end", ms=int((time.time() - self._started) * 1000), **summary)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    # -- typed events -------------------------------------------------------

    def tool_call(self, cid: str, tool: str, args: dict) -> None:
        self.event("tool.call", id=cid, tool=tool, args=_clip(args))

    def tool_result(self, cid: str, tool: str, *, ms: int = 0,
                    bytes_: int = 0, error: str | None = None) -> None:
        payload = {"id": cid, "tool": tool, "ms": ms, "bytes": bytes_}
        if error:
            payload["error"] = error[:300]
        self.event("tool.result", **payload)

    def llm_call(self, model: str, *, prompt_tokens: int = 0,
                 completion_tokens: int = 0, ms: int = 0,
                 purpose: str = "", error: str | None = None) -> None:
        payload = {"model": model, "prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens, "ms": ms,
                   "purpose": purpose}
        if error:
            payload["error"] = error[:300]
            self.event("llm.error", **payload)
        else:
            self.event("llm.call", **payload)

    def llm_error(self, model: str, error: str, *, purpose: str = "", ms: int = 0):
        self.llm_call(model, ms=ms, purpose=purpose, error=error)

    def finding_accepted(self, rule: str, *, file: str = "", line: int = 0,
                         symbol: str = "", confidence: str = "",
                         evidence_tool: str = "") -> None:
        self.event("finding.accepted", rule=rule, file=file, line=line,
                   symbol=symbol, confidence=confidence,
                   evidence_tool=evidence_tool)

    def finding_rejected(self, rule: str, *, reason: str = "",
                         file: str = "", symbol: str = "") -> None:
        self.event("finding.rejected", rule=rule, reason=reason,
                   file=file, symbol=symbol)

    # -- read side ----------------------------------------------------------

    @classmethod
    def runs_root(cls, base_dir: str | None = None) -> str:
        return os.path.join(base_dir or os.path.join(
            os.path.expanduser("~"), ".debtscope"), "runs")

    @classmethod
    def list_runs(cls, base_dir: str | None = None, limit: int = 30) -> list[dict]:
        runs_dir = cls.runs_root(base_dir)
        if not os.path.isdir(runs_dir):
            return []
        out = []
        names = sorted(
            (n for n in os.listdir(runs_dir) if n.endswith(".jsonl")),
            reverse=True)[:limit]
        for name in names:
            rid = name[:-6]
            head, tail, size = {}, {}, 0
            try:
                path = os.path.join(runs_dir, name)
                size = os.path.getsize(path)
                with open(path, encoding="utf-8") as fh:
                    lines = fh.readlines()
                if lines:
                    head = json.loads(lines[0])
                    tail = json.loads(lines[-1])
            except (OSError, json.JSONDecodeError):
                continue
            out.append({
                "id": rid,
                "repo": head.get("repo", ""),
                "project_id": head.get("project_id", ""),
                "mode": head.get("mode", ""),
                "commit": head.get("commit", ""),
                "started": head.get("ts", ""),
                "events": len(lines),
                "score": tail.get("score"),
                "new": tail.get("new"),
                "resolved": tail.get("resolved"),
                "endpoints": tail.get("endpoints"),
                "agent_reviewed": tail.get("agent_reviewed"),
                "ms": tail.get("ms"),
                "bytes": size,
            })
        return out

    @classmethod
    def read_run(cls, run_id: str, base_dir: str | None = None) -> list[dict]:
        runs_dir = cls.runs_root(base_dir)
        # strict id check prevents path traversal via the CLI/API
        safe = "".join(c for c in run_id if c.isalnum() or c in "-_")
        path = os.path.join(runs_dir, safe + ".jsonl")
        if not os.path.isfile(path) or safe != run_id:
            raise FileNotFoundError(run_id)
        events = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events


@contextmanager
def recorded(recorder: RunRecorder, **meta):
    """Context manager: start/end a run even when the body raises."""
    recorder.start(**meta)
    try:
        yield recorder
    except Exception as exc:
        recorder.event("run.error", error=f"{type(exc).__name__}: {exc}"[:300])
        recorder.end(status="error")
        recorder.close()
        raise
    else:
        recorder.end(status="ok")
        recorder.close()


def _clip(value, limit: int = 2000):
    """Keep trace payloads small (full evidence lives in the ledger)."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "<unserializable>"
    if len(text) <= limit:
        return value
    return text[:limit] + f"…<+{len(text) - limit} chars>"
