"""Minimal OpenAI-compatible chat client (stdlib urllib only).

Works with any OpenAI-compatible endpoint: public models, internal gateways,
vLLM/Ollama-style deployments, etc. No SDK dependency by design.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from ..config import Config

REVIEW_PROMPT = """You are a meticulous static-analysis reviewer for a Python repository.
You are given functions/methods that a structural scan found NO calls or references for anywhere in the repo.
Decide whether each is genuinely dead code, or actually an entry point invoked implicitly by a framework.

Classify each item as one of:
- "dead": genuinely unused, safe to delete (no decorators, not a framework hook, not a dunder, not a test)
- "entry": implicit entry point (web route, CLI command, signal/handler hook, dunder, test, abstract interface, callback)
- "uncertain": cannot tell from the snippet; a human should confirm

Be conservative: when a framework-style decorator or hook naming is present, choose "entry".
Respond with ONLY a JSON array of objects: [{"key": "...", "verdict": "dead|entry|uncertain", "reason": "short"}].

Items:
%s
"""


class LLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def classify_dead_code(self, items: list[dict]) -> dict[str, tuple[str, str]]:
        """Returns {key: (verdict, reason)}. Missing keys are treated as uncertain."""
        result: dict[str, tuple[str, str]] = {}
        for i in range(0, len(items), 20):
            batch = items[i : i + 20]
            try:
                result.update(self._call(batch))
            except Exception:
                # Any failure degrades to static-only review upstream; never block a scan.
                break
        return result

    RULE_GEN_PROMPT = """You design deterministic static-analysis rules for a Python technical-debt tool.
Turn the user's natural-language request into ONE rule from the available rule kinds below.
Respond with ONLY a JSON object:
{"name": "<short Chinese rule name>", "kind": "<kind>", "severity": "high|medium|low", "params": {...}, "description": "<one Chinese sentence>"}

Available kinds and params:
- function_too_long: {"max_lines": int}
- file_too_long: {"max_lines": int}
- todo_accumulation: {"max_count": int}
- too_many_args: {"max_args": int}
- nested_too_deep: {"max_depth": int}
- duplicate_function: {"min_lines": int, "min_stmts": int}
- forbidden_call: {"patterns": "comma-separated called names, e.g. print,eval,os.system"}
- name_convention: {"target": "function|class", "regex": "full-match regex", "message": "Chinese hint"}
- swallowed_exception / mutable_default_argument / open_without_context: no params

Examples:
{"name":"禁止 print 调试","kind":"forbidden_call","severity":"low","params":{"patterns":"print"},"description":"代码中不应保留 print 调试输出"}
{"name":"函数不超过 80 行","kind":"function_too_long","severity":"low","params":{"max_lines":80},"description":"函数有效行数超过 80 行"}
{"name":"类名大驼峰","kind":"name_convention","severity":"medium","params":{"target":"class","regex":"^[A-Z][a-zA-Z0-9]*$","message":"类名应使用大驼峰命名"},"description":"类名必须符合大驼峰命名规范"}

User request: %s
"""

    def generate_rule(self, description: str) -> dict | None:
        """Map a natural-language request to a rule DSL object (not persisted)."""
        body = json.dumps({
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": "You output strict JSON only."},
                {"role": "user", "content": self.RULE_GEN_PROMPT % description[:600]},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.cfg.api_base}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                return None
            obj = json.loads(m.group(0))
            if obj.get("kind"):
                return obj
        except Exception:
            return None
        return None

    def ping(self) -> tuple[bool, str]:
        """Minimal connectivity/auth check used by the onboarding wizard."""
        import time

        body = json.dumps({
            "model": self.cfg.model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 5,
            "temperature": 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.cfg.api_base}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}),
            },
            method="POST",
        )
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=min(self.cfg.timeout, 15)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            reply = data["choices"][0]["message"]["content"].strip()
            ms = int((time.time() - started) * 1000)
            return True, f"{ms}ms, model replied: {reply[:40]}"
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            return False, f"HTTP {e.code}: {detail}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def _call(self, batch: list[dict]) -> dict[str, tuple[str, str]]:
        payload_text = json.dumps(batch, ensure_ascii=False, indent=1)
        body = json.dumps(
            {
                "model": self.cfg.model,
                "messages": [
                    {"role": "system", "content": "You output strict JSON only."},
                    {"role": "user", "content": REVIEW_PROMPT % payload_text},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.cfg.api_base}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}),
            },
            method="POST",
        )
        last_err: Exception | None = None
        for _ in range(self.cfg.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                return self._parse(content, batch)
            except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as e:
                last_err = e
        raise RuntimeError(f"LLM call failed: {last_err}")

    @staticmethod
    def _parse(content: str, batch: list[dict]) -> dict[str, tuple[str, str]]:
        valid = {it["key"] for it in batch}
        parsed = None
        # Model may wrap the array in an object; try direct array first.
        m = re.search(r"\[.*\]", content, re.S)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
        if parsed is None:
            obj = re.search(r"\{.*\}", content, re.S)
            if obj:
                data = json.loads(obj.group(0))
                parsed = next(
                    (v for v in data.values() if isinstance(v, list)),
                    [data],
                )
        out: dict[str, tuple[str, str]] = {}
        for row in parsed or []:
            key = str(row.get("key", ""))
            verdict = str(row.get("verdict", "uncertain"))
            if key in valid and verdict in ("dead", "entry", "uncertain"):
                out[key] = (verdict, str(row.get("reason", ""))[:200])
        return out
