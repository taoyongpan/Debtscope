"""Tests for the read-only agent tools against the demo project."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from debtscope.core.callgraph import build_all_chains, build_call_graph, blast_radius  # noqa: E402
from debtscope.core.endpoints import discover_endpoints  # noqa: E402
from debtscope.core.python_indexer import PythonIndexer  # noqa: E402
from debtscope.core.rules import BUILTIN_SPECS, KINDS, run_rules  # noqa: E402
from debtscope.harness.kernel import ToolError  # noqa: E402
from debtscope.harness.tools import build_kernel  # noqa: E402

DEMO = os.path.join(ROOT, "fixtures", "demo_project")


class ToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = PythonIndexer().index(DEMO)
        cls.cg = build_call_graph(cls.idx)
        cls.eps = discover_endpoints(cls.idx)
        cls.chains = build_all_chains(cls.cg, cls.eps)
        cls.findings = run_rules(cls.idx, BUILTIN_SPECS)
        cls.kernel = build_kernel(DEMO, cls.idx, cls.cg, endpoints=cls.eps,
                                  chains=cls.chains, findings=cls.findings)

    def call(self, name, **args):
        return self.kernel.call_tool(name, args)

    def test_symbols_filter(self):
        out = self.call("codegraph.symbols", prefix="create_order")
        self.assertEqual(out["count"], 2)  # create_order + create_order_endpoint
        keys = {s["key"] for s in out["symbols"]}
        self.assertIn("order_service.py::create_order", keys)
        out2 = self.call("codegraph.symbols", kind="method", limit=5)
        self.assertTrue(all(s["kind"] == "method" for s in out2["symbols"]))

    def test_callers_of_live_symbol_nonempty(self):
        out = self.call("codegraph.callers", symbol="list_user_orders")
        self.assertGreater(out["caller_count"], 0)

    def test_callers_of_dead_symbol_empty(self):
        out = self.call("codegraph.callers", symbol="old_export_format_v1")
        self.assertEqual(out["caller_count"], 0)
        self.assertIn("废弃", out["note"])

    def test_callers_unknown_symbol(self):
        with self.assertRaises(ToolError):
            self.call("codegraph.callers", symbol="does_not_exist_xyz")

    def test_callees(self):
        out = self.call("codegraph.callees", symbol="create_order")
        names = [c["callee"] for c in out["callees"]]
        self.assertIn("order_service.py::_persist_order", names)
        self.assertIn("user_service.py::save_user", names)

    def test_read_file_with_lines(self):
        out = self.call("read_file", file="order_service.py", start=1, end=5)
        self.assertEqual(out["start"], 1)
        self.assertEqual(len(out["lines"]), 5)
        self.assertEqual(out["lines"][0]["n"], 1)

    def test_read_file_path_traversal_blocked(self):
        with self.assertRaises(ToolError):
            self.call("read_file", file="../../etc/passwd")

    def test_read_file_missing(self):
        with self.assertRaises(ToolError):
            self.call("read_file", file="nope.py")

    def test_grep_finds_reference(self):
        out = self.call("grep", pattern="old_export_format_v1")
        self.assertGreaterEqual(out["count"], 1)
        self.assertIn("file", out["hits"][0])

    def test_grep_bad_regex(self):
        with self.assertRaises(ToolError):
            self.call("grep", pattern="(")

    def test_rules_catalog(self):
        out = self.call("rules.catalog")
        ids = {r["id"] for r in out["builtin_rules"]}
        self.assertEqual(len(ids), 8)
        self.assertIn("db_call_in_loop", ids)
        kinds = {k["kind"] for k in out["creatable_kinds"]}
        self.assertIn("forbidden_call", kinds)
        self.assertNotIn("unused_function", kinds)  # not creatable

    def test_rule_preview_forbidden_call(self):
        # the matcher keys on the attribute tail (requests.get -> "get")
        out = self.call("rule.preview", kind="forbidden_call",
                        params={"patterns": "get"})
        self.assertGreater(out["count"], 0)
        self.assertTrue(all("get" in s["message"] for s in out["samples"]))

    def test_rule_preview_unknown_kind(self):
        with self.assertRaises(ToolError):
            self.call("rule.preview", kind="nope")

    def test_rule_preview_bad_int_param(self):
        with self.assertRaises(ToolError):
            self.call("rule.preview", kind="function_too_long",
                      params={"max_lines": "abc"})

    def test_endpoint_chain_create(self):
        out = self.call("endpoint.chain", method="POST",
                        path="/v1/api/orders/create")
        self.assertTrue(out["resolvable"])
        self.assertGreaterEqual(out["depth"], 2)
        quals = {n["qualname"] for n in out["nodes"]}
        self.assertIn("create_order", quals)
        # db sink present in the chain
        self.assertTrue(any(e["io"] == "db" for e in out["ext_sinks"]),
                        out["ext_sinks"])

    def test_endpoint_chain_unknown(self):
        with self.assertRaises(ToolError):
            self.call("endpoint.chain", method="GET", path="/no/such/path")

    def test_all_tools_read_only(self):
        for t in self.kernel.describe_tools():
            self.assertTrue(t["read_only"], t["name"])


if __name__ == "__main__":
    unittest.main()
