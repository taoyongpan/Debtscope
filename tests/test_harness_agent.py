"""Tests for the ReAct agent loop and its evidence enforcement."""
import json
import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from debtscope.config import Config  # noqa: E402
from debtscope.core.callgraph import build_call_graph  # noqa: E402
from debtscope.core.python_indexer import PythonIndexer  # noqa: E402
from debtscope.core.llm import LLMClient  # noqa: E402
from debtscope.core.reviewer import review  # noqa: E402
from debtscope.core.rules import BUILTIN_SPECS, run_rules  # noqa: E402
from debtscope.harness.agent import Agent, investigate_dead_code  # noqa: E402
from debtscope.harness.tools import build_kernel  # noqa: E402

DEMO = os.path.join(ROOT, "fixtures", "demo_project")


def act(tool, **args):
    return json.dumps({"thought": "t", "action": tool, "args": args},
                      ensure_ascii=False)


def final(verdict, tools, reason="r"):
    return json.dumps(
        {"thought": "t",
         "final": {"verdict": verdict, "reason": reason,
                   "evidence": [{"tool": t, "ref": "x"} for t in tools]}},
        ensure_ascii=False)


class FakeLLM:
    cfg = SimpleNamespace(model="fake-model")

    def __init__(self, script):
        self.script = script
        self.i = 0

    def chat_json(self, messages, **_kw):
        if isinstance(self.script, Exception):
            raise self.script
        if callable(self.script):
            return self.script(messages), {"prompt_tokens": 11,
                                           "completion_tokens": 7}
        content = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if isinstance(content, Exception):
            raise content
        return content, {"prompt_tokens": 11, "completion_tokens": 7}


class AgentLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = PythonIndexer().index(DEMO)
        cls.cg = build_call_graph(cls.idx)

    def agent(self, llm, max_steps=6):
        kernel = build_kernel(DEMO, self.idx, self.cg)
        return Agent(kernel, llm, max_steps=max_steps)

    def item(self, symbol="old_export_format_v1"):
        return {"key": f"legacy.py::{symbol}", "file": "legacy.py",
                "symbol": symbol, "decorators": [], "snippet": "def f(): pass"}

    def test_happy_path_gather_then_verdict(self):
        llm = FakeLLM([
            act("codegraph.callers", symbol="old_export_format_v1"),
            act("grep", pattern="old_export_format_v1"),
            final("dead", ["codegraph.callers", "grep"], "无调用方"),
        ])
        res = investigate_dead_code(self.agent(llm), self.item())
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.final["verdict"], "dead")
        called = {c["tool"] for c in res.tool_calls}
        self.assertEqual(called, {"codegraph.callers", "grep"})

    def test_final_without_evidence_sent_back_then_fixed(self):
        llm = FakeLLM([
            json.dumps({"thought": "t", "final": {"verdict": "dead",
                                                  "reason": "x", "evidence": []}}),
            act("codegraph.callers", symbol="old_export_format_v1"),
            final("dead", ["codegraph.callers"]),
        ])
        res = investigate_dead_code(self.agent(llm), self.item())
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.final["verdict"], "dead")

    def test_dead_without_caller_search_rejected(self):
        # read_file alone can never support a deletion claim
        llm = FakeLLM([
            act("read_file", file="legacy.py"),
            final("dead", ["read_file"]),
            final("dead", ["read_file"]),
        ])
        res = investigate_dead_code(self.agent(llm), self.item())
        self.assertEqual(res.status, "incomplete")
        self.assertIn("caller", res.error)

    def test_unknown_tool_corrected(self):
        llm = FakeLLM([
            act("no.such.tool"),
            act("codegraph.callers", symbol="old_export_format_v1"),
            final("dead", ["codegraph.callers"]),
        ])
        res = investigate_dead_code(self.agent(llm), self.item())
        self.assertEqual(res.status, "ok")

    def test_max_steps_exhausted(self):
        llm = FakeLLM([act("codegraph.callers", symbol="old_export_format_v1")])
        res = investigate_dead_code(self.agent(llm, max_steps=3), self.item())
        self.assertEqual(res.status, "incomplete")
        self.assertIn("最大步数", res.error)

    def test_llm_error_surfaces(self):
        llm = FakeLLM(RuntimeError("HTTP 401"))
        res = investigate_dead_code(self.agent(llm), self.item())
        self.assertEqual(res.status, "error")
        self.assertIn("401", res.error)

    def test_uncertain_accepted_with_evidence(self):
        llm = FakeLLM([
            act("codegraph.callers", symbol="old_export_format_v1"),
            final("uncertain", ["codegraph.callers"], "可能被反射调用"),
        ])
        res = investigate_dead_code(self.agent(llm), self.item())
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.final["verdict"], "uncertain")


class ReviewerAgentIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = PythonIndexer().index(DEMO)
        cls.cg = build_call_graph(cls.idx)
        cls.cands = [f for f in run_rules(cls.idx, BUILTIN_SPECS)
                     if f.rule_id == "unused_function"]
        cls.cfg = Config(llm_enabled=True, provider="custom",
                         api_base="http://127.0.0.1:9/v1", api_key="x",
                         model="fake", timeout=1, max_retries=0)

    def _dynamic_llm(self):
        """callers first; then dead when 0 callers, entry when >0."""
        def script(messages):
            last = messages[-1]["content"]
            m = re.search(r"符号: (\S+)", messages[1]["content"])
            symbol = m.group(1) if m else "f"
            if "工具 codegraph.callers 返回" in last:
                verdict = "dead" if '"caller_count": 0' in last else "entry"
                return final(verdict, ["codegraph.callers"])
            return act("codegraph.callers", symbol=symbol)
        return FakeLLM(script)

    def test_uncertain_candidates_escalated_to_agent(self):
        llm = self._dynamic_llm()
        keys = [f"{f.file}:{f.line}:{f.symbol}" for f in self.cands]
        with patch.object(LLMClient, "classify_dead_code",
                          return_value=({k: ("uncertain", "") for k in keys}, None)), \
             patch.object(LLMClient, "chat_json", llm.chat_json):
            kept, stats = review(self.cands, self.idx, self.cfg,
                                 cg=self.cg, root=DEMO)
        self.assertEqual(stats.agent_reviewed, len(self.cands))
        self.assertGreaterEqual(stats.agent_dead + stats.agent_entry, 1)
        self.assertLessEqual(len(kept), len(self.cands))

    def test_agent_review_disabled_by_env(self):
        os.environ["DEBTSCOPE_AGENT_REVIEW"] = "0"
        try:
            keys = [f"{f.file}:{f.line}:{f.symbol}" for f in self.cands]
            with patch.object(LLMClient, "classify_dead_code",
                              return_value=({k: ("uncertain", "") for k in keys}, None)):
                _, stats = review(self.cands, self.idx, self.cfg,
                                  cg=self.cg, root=DEMO)
            self.assertEqual(stats.agent_reviewed, 0)
            self.assertEqual(stats.llm_uncertain, len(self.cands))
        finally:
            os.environ.pop("DEBTSCOPE_AGENT_REVIEW", None)


if __name__ == "__main__":
    unittest.main()
