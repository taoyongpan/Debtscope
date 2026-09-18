"""Minimal OpenAI-compatible chat client (stdlib urllib only).

Works with any OpenAI-compatible endpoint: public models, internal gateways,
vLLM/Ollama-style deployments, etc. No SDK dependency by design.

All structured calls use the JSON-object protocol (``response_format=
json_object``): the model must answer with a JSON *object*, never a bare
array, which some compatible gateways reject. Parsing is deliberately
defensive — models wrap, annotate and leak prose, and a scan must survive.
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
Respond with ONLY a JSON object of the shape:
{"results": [{"key": "...", "verdict": "dead|entry|uncertain", "reason": "short"}]}

Items:
%s
"""


def _extract_json(content: str):
    """Best-effort parse of a model reply that should contain JSON.

    Returns a dict/list or None. Tries the whole reply, then the outermost
    {...} or [...] span, tolerating markdown fences and surrounding prose.
    """
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content).strip()
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        m = re.search(re.escape(opener) + r".*" + re.escape(closer), content, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    return None


def parse_verdicts(content: str, valid_keys: set[str]) -> dict[str, tuple[str, str]]:
    """Parse the dead-code review reply into {key: (verdict, reason)}."""
    data = _extract_json(content)
    rows: list = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        # Preferred shape: {"results": [...]}; tolerate other wrappers.
        inner = data.get("results")
        if isinstance(inner, list):
            rows = inner
        else:
            rows = next((v for v in data.values() if isinstance(v, list)), [data])
    out: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key", ""))
        verdict = str(row.get("verdict", "uncertain"))
        if key in valid_keys and verdict in ("dead", "entry", "uncertain"):
            out[key] = (verdict, str(row.get("reason", ""))[:200])
    return out


class LLMClient:
    def __init__(self, cfg: Config, tracer=None):
        self.cfg = cfg
        self.tracer = tracer

    def _trace(self, usage: dict, purpose: str, error: str | None = None,
               ms: int = 0) -> None:
        if self.tracer is None:
            return
        if error:
            self.tracer.llm_error(self.cfg.model, error, purpose=purpose, ms=ms)
        else:
            self.tracer.llm_call(
                self.cfg.model,
                prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                completion_tokens=(usage or {}).get("completion_tokens", 0),
                purpose=purpose)

    # -- transport ---------------------------------------------------------

    def _post(self, messages: list[dict], *, json_object: bool = True,
              max_tokens: int | None = None, temperature: float = 0,
              return_usage: bool = False):
        """Call chat/completions and return the assistant message content.

        Raises RuntimeError with a human-readable message after retries are
        exhausted; callers decide how visible that error should be. When
        ``return_usage`` is set, returns ``(content, usage_dict)`` for the
        agent loop's token accounting.
        """
        payload: dict = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}),
        }
        last_err: Exception | None = None
        for _ in range(self.cfg.max_retries + 1):
            try:
                req = urllib.request.Request(
                    f"{self.cfg.api_base}/chat/completions",
                    data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"].get("content") or ""
                if return_usage:
                    return content, data.get("usage") or {}
                return content
            except urllib.error.HTTPError as e:
                # Auth/quota/model errors won't heal by retrying.
                detail = e.read().decode("utf-8", errors="replace")[:200]
                raise RuntimeError(f"HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, KeyError, json.JSONDecodeError,
                    TimeoutError, OSError) as e:
                last_err = e
        raise RuntimeError(f"LLM call failed: {type(last_err).__name__}: {last_err}")

    def chat_json(self, messages: list[dict], *, temperature: float = 0,
                  max_tokens: int | None = None) -> tuple[str, dict]:
        """Structured chat used by the harness agent loop.

        Returns ``(content, usage)`` where usage carries prompt/completion
        token counts when the endpoint provides them. A plain-string return
        (e.g. from a test double) is tolerated.
        """
        out = self._post(messages, json_object=True, temperature=temperature,
                         max_tokens=max_tokens, return_usage=True)
        if isinstance(out, tuple):
            return out
        return out, {}

    # -- capabilities -------------------------------------------------------

    def classify_dead_code(
        self, items: list[dict]
    ) -> tuple[dict[str, tuple[str, str]], str | None]:
        """Returns ({key: (verdict, reason)}, error).

        Batches of 20 keep prompts small. A batch failure is recorded but does
        not abort the remaining batches; the un-reviewed items fall back to
        an honest "please confirm" state upstream.
        """
        result: dict[str, tuple[str, str]] = {}
        errors: list[str] = []
        for i in range(0, len(items), 20):
            batch = items[i: i + 20]
            try:
                content, usage = self.chat_json([
                    {"role": "system", "content": "You output strict JSON only."},
                    {"role": "user",
                     "content": REVIEW_PROMPT % json.dumps(batch, ensure_ascii=False, indent=1)},
                ])
                self._trace(usage, "dead_code_batch")
                result.update(parse_verdicts(content, {it["key"] for it in batch}))
            except Exception as e:  # never let the model block a scan
                errors.append(str(e)[:160])
                self._trace({}, "dead_code_batch", error=str(e)[:200])
        err = "; ".join(sorted(set(errors))) if errors else None
        return result, err

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

    def generate_rule(self, description: str) -> tuple[dict | None, str | None]:
        """Map a natural-language request to a rule DSL object.

        Returns (obj, None) on success or (None, reason) on failure; nothing
        is persisted here — the caller dry-runs the rule before saving.
        """
        try:
            content, usage = self.chat_json([
                {"role": "system", "content": "You output strict JSON only."},
                {"role": "user", "content": self.RULE_GEN_PROMPT % description[:600]},
            ], temperature=0.1)
            self._trace(usage, "rule_generation")
        except Exception as e:
            self._trace({}, "rule_generation", error=str(e)[:200])
            return None, f"模型调用失败：{e}"
        obj = _extract_json(content)
        if not isinstance(obj, dict) or not obj.get("kind"):
            return None, "模型未返回合法的指标 JSON，请换个描述或手动创建"
        return obj, None

    def ping(self) -> tuple[bool, str]:
        """Minimal connectivity/auth check used by the onboarding wizard."""
        import time

        started = time.time()
        try:
            content = self._post(
                [{"role": "user", "content": "Reply with the single word: ok"}],
                json_object=False, max_tokens=5,
            )
            ms = int((time.time() - started) * 1000)
            return True, f"{ms}ms, model replied: {content.strip()[:40]}"
        except Exception as e:
            return False, str(e)
