"""Tests for JSONL run tracing."""
import json
import os
import tempfile
import unittest

from debtscope.harness.trace import NullTracer, RunRecorder, recorded


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.r = RunRecorder(base_dir=self.base, mode="test")

    def test_start_event_end_roundtrip(self):
        rid = self.r.start(repo="/repo", commit="abc", llm_enabled=False)
        self.assertTrue(rid)
        self.r.tool_call("t1", "codegraph.callers", {"symbol": "f"})
        self.r.tool_result("t1", "codegraph.callers", ms=3, bytes_=42)
        self.r.llm_call("m", prompt_tokens=10, completion_tokens=5, purpose="p")
        self.r.finding_accepted("unused_function", file="a.py", line=1,
                                symbol="f", confidence="high",
                                evidence_tool="codegraph.callers")
        self.r.finding_rejected("unused_function", reason="llm_entry")
        self.r.end(score=79, new=1, resolved=0, endpoints=11)
        self.r.close()

        events = RunRecorder.read_run(rid, base_dir=self.base)
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "run.start")
        self.assertEqual(types[-1], "run.end")
        self.assertIn("tool.call", types)
        self.assertIn("tool.result", types)
        self.assertIn("llm.call", types)
        self.assertIn("finding.accepted", types)
        self.assertIn("finding.rejected", types)
        end = events[-1]
        self.assertEqual(end["score"], 79)
        self.assertEqual(end["endpoints"], 11)
        # seq is contiguous
        self.assertEqual([e["seq"] for e in events], list(range(len(events))))

    def test_llm_error_event(self):
        self.r.start()
        self.r.llm_error("m", "HTTP 401", purpose="dead_code_batch")
        self.r.end()
        self.r.close()
        events = RunRecorder.read_run(self.r.run_id, base_dir=self.base)
        self.assertIn("llm.error", [e["type"] for e in events])

    def test_list_runs_summary(self):
        for _ in range(2):
            r = RunRecorder(base_dir=self.base, mode="headless")
            r.start(repo="/tmp/proj")
            r.end(score=80, new=0, resolved=1, endpoints=3)
            r.close()
        runs = RunRecorder.list_runs(self.base)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["score"], 80)
        self.assertEqual(runs[0]["repo"], "/tmp/proj")

    def test_traversal_run_id_rejected(self):
        with self.assertRaises(FileNotFoundError):
            RunRecorder.read_run("../../etc/passwd", base_dir=self.base)

    def test_unknown_run_id(self):
        with self.assertRaises(FileNotFoundError):
            RunRecorder.read_run("20260101-000000-aaaaaa", base_dir=self.base)

    def test_recorded_context_on_error(self):
        r = RunRecorder(base_dir=self.base)
        with self.assertRaises(RuntimeError):
            with recorded(r, repo="/x"):
                raise RuntimeError("boom")
        events = RunRecorder.read_run(r.run_id, base_dir=self.base)
        types = [e["type"] for e in events]
        self.assertIn("run.error", types)
        self.assertEqual(events[-1]["status"], "error")

    def test_null_tracer_noop(self):
        n = NullTracer()
        self.assertIsNone(n.start(a=1))
        n.event("x")
        n.tool_call("t", "x", {})
        n.tool_result("t", "x")
        n.llm_call("m")
        n.llm_error("m", "e")
        n.finding_accepted("r")
        n.finding_rejected("r")
        n.end()
        n.close()

    def test_payload_clipping(self):
        self.r.start()
        self.r.tool_call("t", "grep", {"pattern": "x" * 5000})
        self.r.end()
        self.r.close()
        path = os.path.join(self.base, "runs", self.r.run_id + ".jsonl")
        raw = open(path, encoding="utf-8").read()
        self.assertIn("chars", json.loads(raw.splitlines()[1])["args"])


if __name__ == "__main__":
    unittest.main()
