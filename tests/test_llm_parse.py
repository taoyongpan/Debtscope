"""LLM protocol parsing and failure handling (no real network calls)."""
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from debtscope.config import Config               # noqa: E402
from debtscope.core import llm as llm_module       # noqa: E402
from debtscope.core.llm import LLMClient, parse_verdicts  # noqa: E402

KEYS = {"a.py:1:f", "a.py:2:g"}


def client():
    cfg = Config(llm_enabled=True, provider="custom",
                 api_base="http://127.0.0.1:1/v1", api_key="k",
                 model="m", timeout=1, max_retries=0)
    return LLMClient(cfg)


class ParseVerdictsTest(unittest.TestCase):
    def test_object_envelope(self):
        content = '{"results":[{"key":"a.py:1:f","verdict":"dead","reason":"unused"}]}'
        out = parse_verdicts(content, KEYS)
        self.assertEqual(out["a.py:1:f"][0], "dead")

    def test_bare_array(self):
        content = '[{"key":"a.py:2:g","verdict":"entry","reason":"route"}]'
        out = parse_verdicts(content, KEYS)
        self.assertEqual(out["a.py:2:g"], ("entry", "route"))

    def test_markdown_fence_and_prose(self):
        content = ('Here you go:\n```json\n'
                   '{"results":[{"key":"a.py:1:f","verdict":"uncertain"}]}\n```')
        out = parse_verdicts(content, KEYS)
        self.assertEqual(out["a.py:1:f"][0], "uncertain")

    def test_invalid_rows_are_filtered(self):
        content = ('{"results":['
                   '{"key":"unknown","verdict":"dead"},'
                   '{"key":"a.py:1:f","verdict":"weird"},'
                   '{"key":"a.py:2:g","verdict":"dead"}]}')
        out = parse_verdicts(content, KEYS)
        self.assertEqual(set(out), {"a.py:2:g"})

    def test_garbage_yields_empty(self):
        self.assertEqual(parse_verdicts("no json at all", KEYS), {})


class ClassifyTest(unittest.TestCase):
    def test_success_returns_verdicts(self):
        reply = '{"results":[{"key":"a.py:1:f","verdict":"dead","reason":"x"}]}'
        with patch.object(LLMClient, "_post", return_value=reply):
            verdicts, err = client().classify_dead_code(
                [{"key": "a.py:1:f"}, {"key": "a.py:2:g"}])
        self.assertIn("a.py:1:f", verdicts)
        self.assertIsNone(err)

    def test_transport_failure_is_reported_not_raised(self):
        with patch.object(LLMClient, "_post", side_effect=RuntimeError("HTTP 401")):
            verdicts, err = client().classify_dead_code([{"key": "a.py:1:f"}])
        self.assertEqual(verdicts, {})
        self.assertIn("401", err)


class GenerateRuleTest(unittest.TestCase):
    def test_success(self):
        reply = ('{"name":"禁止 print","kind":"forbidden_call","severity":"low",'
                 '"params":{"patterns":"print"},"description":"x"}')
        with patch.object(LLMClient, "_post", return_value=reply):
            obj, err = client().generate_rule("禁止 print")
        self.assertIsNone(err)
        self.assertEqual(obj["kind"], "forbidden_call")

    def test_prose_reply_returns_reason(self):
        with patch.object(LLMClient, "_post", return_value="I cannot help with that"):
            obj, err = client().generate_rule("whatever")
        self.assertIsNone(obj)
        self.assertIsNotNone(err)

    def test_transport_error_returns_reason(self):
        with patch.object(LLMClient, "_post", side_effect=RuntimeError("boom")):
            obj, err = client().generate_rule("whatever")
        self.assertIsNone(obj)
        self.assertIn("模型调用失败", err)


if __name__ == "__main__":
    unittest.main()
