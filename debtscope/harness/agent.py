"""Minimal ReAct agent loop over the tool kernel (text JSON protocol).

Why not native function-calling? Debtscope supports 16+ OpenAI-compatible
providers including small local models that lack tool APIs; a single
JSON-in-text protocol works against all of them. The loop is intentionally
small and only used for semantic tasks the deterministic core cannot do —
scanning, counting and scoring never enter a loop.

Architectural anti-hallucination rule: a final verdict must cite tool calls
that actually happened, and a "dead code" verdict requires a caller-search
tool to have run; verdicts without tool evidence are discarded by the loop
itself (not just requested in the prompt).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..core.llm import _extract_json

OBSERVATION_LIMIT = 8000
CORRECTION_LIMIT = 2


@dataclass
class AgentResult:
    status: str                  # "ok" | "incomplete" | "error"
    final: dict | None = None
    steps: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "final": self.final,
                "steps": self.steps, "tool_calls": self.tool_calls,
                "error": self.error}


class Agent:
    def __init__(self, kernel, llm, *, max_steps: int = 6, tracer=None):
        self.kernel = kernel
        self.llm = llm
        self.max_steps = max_steps
        self.tracer = tracer

    def _system_prompt(self) -> str:
        tools = self.kernel.describe_tools()
        catalog = "\n".join(
            f"- {t['name']}: {t['description']}\n  参数 schema: "
            f"{json.dumps(t['input_schema'], ensure_ascii=False)}"
            for t in tools)
        return (
            "你是 Debtscope（代码技术债静态分析工具）的分析智能体。\n"
            "你只能通过调用下列工具获取证据，禁止凭记忆、命名习惯或猜测下结论；"
            "工具都是只读的，且只能访问当前仓库。\n\n"
            f"可用工具：\n{catalog}\n\n"
            "每一轮你只能输出一个 JSON 对象，不要输出 JSON 以外的任何文字，两种形态：\n"
            '1. 调用工具：{"thought":"简短中文思路","action":"工具名","args":{...}}\n'
            '2. 最终结论：{"thought":"简短中文结论","final":{...任务要求的字段...,'
            '"evidence":[{"tool":"实际调用过的工具名","ref":"关键证据摘要"}]}}\n'
            "要求：final.evidence 至少引用一次你真实调用过的工具及其返回的关键事实；"
            "证据不足时继续调用工具，超过工具能力仍无法判断时如实给出 uncertain。"
        )

    def run(self, task: str, final_hint: str,
            validate=None, purpose: str = "agent") -> AgentResult:
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": task + "\n\nfinal 对象字段要求：\n" + final_hint},
        ]
        called: dict[str, list] = {}   # tool name -> list of observations
        corrections = 0
        model = getattr(getattr(self.llm, "cfg", None), "model", "")
        for step in range(1, self.max_steps + 1):
            try:
                content, usage = self.llm.chat_json(messages, temperature=0)
            except Exception as exc:
                if self.tracer:
                    self.tracer.llm_error(model or "unknown", str(exc), purpose=purpose)
                return AgentResult("error", error=str(exc)[:300], steps=step - 1,
                                   tool_calls=self._calls_summary(called))
            if self.tracer:
                self.tracer.llm_call(
                    model, prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    purpose=purpose)
            messages.append({"role": "assistant", "content": content})
            obj = _extract_json(content)
            if not isinstance(obj, dict):
                corrections += 1
                if corrections > CORRECTION_LIMIT:
                    return self._incomplete("模型未返回可解析的 JSON", called, step)
                messages.append({"role": "user",
                                 "content": "你上一轮没有输出合法 JSON 对象，请严格按协议重新输出。"})
                continue

            if "final" in obj and isinstance(obj["final"], dict):
                final = obj["final"]
                problem = self._check_evidence(final, called, validate)
                if problem:
                    corrections += 1
                    if corrections > CORRECTION_LIMIT:
                        return self._incomplete(problem, called, step, final)
                    messages.append({"role": "user",
                                     "content": f"最终结论未通过证据校验：{problem}。"
                                                "请继续调用工具补证据，或给出不确定结论。"})
                    continue
                return AgentResult("ok", final=final, steps=step,
                                   tool_calls=self._calls_summary(called))

            action = obj.get("action")
            if not isinstance(action, str) or not self.kernel.has_tool(action):
                corrections += 1
                if corrections > CORRECTION_LIMIT:
                    return self._incomplete(f"工具不存在或未指定 action：{action}", called, step)
                messages.append({"role": "user",
                                 "content": f"工具 {action} 不存在。可用工具："
                                            f"{', '.join(self.kernel.tool_names())}"})
                continue

            args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
            cid = f"t{step}"
            try:
                result = self.kernel.call_tool(action, args, call_id=cid)
                observation = json.dumps(result, ensure_ascii=False, default=str)
            except Exception as exc:
                observation = json.dumps({"error": str(exc)}, ensure_ascii=False)
            if len(observation) > OBSERVATION_LIMIT:
                observation = observation[:OBSERVATION_LIMIT] + "…<截断>"
            called.setdefault(action, []).append({"args": args, "result": observation})
            messages.append({"role": "user",
                             "content": f"工具 {action} 返回：\n{observation}"})

        return self._incomplete(f"达到最大步数 {self.max_steps} 仍未给出结论", called,
                                self.max_steps)

    # -- evidence enforcement ----------------------------------------------

    @staticmethod
    def _calls_summary(called: dict) -> list[dict]:
        return [{"tool": name, "calls": len(obs)} for name, obs in called.items()]

    @staticmethod
    def _check_evidence(final: dict, called: dict, validate) -> str | None:
        evidence = final.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return "final.evidence 为空，每个结论都必须引用工具证据"
        cited = {e.get("tool") for e in evidence if isinstance(e, dict)}
        real = cited & set(called)
        if not real:
            return "evidence 引用的工具均未被实际调用"
        if validate:
            return validate(final, called)
        return None

    def _incomplete(self, reason: str, called: dict, steps: int,
                    final: dict | None = None) -> AgentResult:
        return AgentResult("incomplete", final=final, steps=steps,
                           tool_calls=self._calls_summary(called), error=reason)


# -- high-level task: evidence-backed dead-code investigation ---------------

DEAD_CODE_HINT = (
    '{"verdict": "dead|entry|uncertain",\n'
    ' "reason": "中文简述，必须引用调用方/装饰器/文件中的具体事实",\n'
    ' "evidence": [{"tool": "...", "ref": "..."}]}'
)


def dead_code_validator(final: dict, called: dict) -> str | None:
    verdict = final.get("verdict")
    if verdict not in ("dead", "entry", "uncertain"):
        return "verdict 必须是 dead / entry / uncertain 之一"
    if verdict == "dead":
        # A deletion claim requires caller evidence from the graph or grep,
        # never just the model's impression of the snippet.
        searched = set(called) & {"codegraph.callers", "grep"}
        if not searched:
            return "判定 dead 前必须先调用 codegraph.callers（必要时 grep）确认无调用方"
    if not str(final.get("reason", "")).strip():
        return "reason 不能为空"
    return None


def investigate_dead_code(agent: Agent, item: dict) -> AgentResult:
    """Let an agent gather caller/decorator evidence for one dead-code candidate.

    ``item`` carries key/file/symbol/decorators/snippet (same shape as the
    batch reviewer payload).
    """
    decorators = ", ".join(item.get("decorators") or []) or "无"
    task = (
        "请判定下面这个 Python 符号是真的废弃代码，还是被框架隐式调用的入口。\n"
        f"- key: {item.get('key')}\n- 文件: {item.get('file')}\n"
        f"- 符号: {item.get('symbol')}\n- 已知装饰器: {decorators}\n"
        f"- 代码片段:\n```\n{item.get('snippet', '')[:1500]}\n```\n\n"
        "建议步骤：先 codegraph.callers 查调用方；为空时用 grep 搜索符号名，"
        "并用 read_file 查看模块级装饰器、注册语句、测试引用后再下判。"
    )
    return agent.run(task, DEAD_CODE_HINT, validate=dead_code_validator,
                     purpose="dead_code_investigation")
