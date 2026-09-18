"""Deterministic rule-engine tests on synthetic code (no LLM, no network)."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from debtscope.core.python_indexer import PythonIndexer  # noqa: E402
from debtscope.core.rules import RuleSpec, run_rules       # noqa: E402


def index_code(files: dict):
    tmp = tempfile.mkdtemp()
    for rel, src in files.items():
        path = os.path.join(tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
    return PythonIndexer().index(tmp)


def spec(kind: str, **params) -> RuleSpec:
    return RuleSpec(id=kind, name=kind, kind=kind, params=params,
                    severity="medium", builtin=False)


def rule_ids(findings):
    return {f.rule_id for f in findings}


class RuleEngineTest(unittest.TestCase):
    def test_function_too_long(self):
        idx = index_code({"a.py": "def f():\n" + "    x = 1\n" * 8})
        hits = run_rules(idx, [spec("function_too_long", max_lines=5)])
        self.assertTrue(hits)
        self.assertIn("f", hits[0].symbol)

    def test_file_too_long(self):
        idx = index_code({"a.py": "x = 1\n" * 40})
        hits = run_rules(idx, [spec("file_too_long", max_lines=10)])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].file, "a.py")

    def test_too_many_args(self):
        idx = index_code({"a.py": "def f(a, b, c, d, e):\n    return 1\n"})
        self.assertTrue(run_rules(idx, [spec("too_many_args", max_args=3)]))
        self.assertFalse(run_rules(idx, [spec("too_many_args", max_args=5)]))

    def test_nested_too_deep(self):
        src = (
            "def f(xs):\n"
            "    for x in xs:\n"
            "        if x:\n"
            "            while False:\n"
            "                yield x\n"
        )
        idx = index_code({"a.py": src})
        self.assertTrue(run_rules(idx, [spec("nested_too_deep", max_depth=2)]))
        self.assertFalse(run_rules(idx, [spec("nested_too_deep", max_depth=4)]))

    def test_forbidden_call(self):
        idx = index_code({"a.py": "def f():\n    print('x')\n"})
        hits = run_rules(idx, [spec("forbidden_call", patterns="print,eval")])
        self.assertEqual(len(hits), 1)
        self.assertIn("print", hits[0].message)

    def test_name_convention_function_and_class(self):
        src = "def badName(): ...\nclass lower_case: ...\n"
        idx = index_code({"a.py": src})
        fn = run_rules(idx, [spec("name_convention", target="function",
                                  regex=r"^[a-z_][a-z0-9_]*$", message="snake_case")])
        self.assertTrue(any(f.symbol == "badName" for f in fn))
        cls = run_rules(idx, [spec("name_convention", target="class",
                                   regex=r"^[A-Z][a-zA-Z0-9]*$", message="CamelCase")])
        self.assertTrue(any("lower_case" in (f.symbol or "") for f in cls))

    def test_todo_accumulation(self):
        src = "# TODO a\n# TODO b\n# FIXME c\n"
        idx = index_code({"a.py": src})
        self.assertTrue(run_rules(idx, [spec("todo_accumulation", max_count=2)]))
        self.assertFalse(run_rules(idx, [spec("todo_accumulation", max_count=5)]))

    def test_duplicate_function(self):
        src = (
            "def f(a):\n    x = a + 1\n    y = x * 2\n    return y\n\n"
            "def g(b):\n    x = b + 1\n    y = x * 2\n    return y\n"
        )
        idx = index_code({"a.py": src})
        hits = run_rules(idx, [spec("duplicate_function", min_lines=4, min_stmts=3)])
        self.assertEqual(len(hits), 1)  # only the second copy is reported
        self.assertIn("g", hits[0].symbol)

    def test_swallowed_exception(self):
        idx = index_code({"a.py": "def f():\n    try:\n        x()\n    except Exception:\n        pass\n"})
        self.assertIn("swallowed_exception",
                      rule_ids(run_rules(idx, [spec("swallowed_exception")])))

    def test_mutable_default_argument(self):
        idx = index_code({"a.py": "def f(x=[]):\n    return x\n"})
        self.assertIn("mutable_default_argument",
                      rule_ids(run_rules(idx, [spec("mutable_default_argument")])))

    def test_open_without_context(self):
        idx = index_code({"a.py": "def f():\n    fh = open('x')\n    return fh\n"})
        self.assertIn("open_without_context",
                      rule_ids(run_rules(idx, [spec("open_without_context")])))

    def test_unknown_kind_is_skipped_not_fatal(self):
        idx = index_code({"a.py": "x = 1\n"})
        # A spec whose detector is missing must never take down a scan.
        self.assertEqual(run_rules(idx, [RuleSpec(id="x", name="x", kind="no_such_kind")]), [])

    def test_disabled_rule_emits_nothing(self):
        idx = index_code({"a.py": "def f():\n    print(1)\n"})
        s = spec("forbidden_call", patterns="print")
        s.enabled = False
        self.assertFalse(run_rules(idx, [s]))

    def test_db_call_in_loop_positive(self):
        src = (
            "def list_orders(ids):\n"
            "    for oid in ids:\n"
            "        cur.execute('select * from o where id=?', (oid,))\n"
            "        row = cur.fetchone()\n"
            "        requests.get('http://svc/' + str(oid))\n"
            "    return row\n"
        )
        idx = index_code({"a.py": src})
        hits = run_rules(idx, [spec("db_call_in_loop")])
        msgs = " ".join(h.message for h in hits)
        self.assertTrue(hits)
        for h in hits:
            self.assertEqual(h.symbol, "list_orders")
        self.assertIn("cur.execute", msgs)
        self.assertIn("requests.get", msgs)

    def test_db_call_in_loop_negative_cases(self):
        # pure-computation loop: no I/O, no hit
        idx = index_code({"a.py": "def f(xs):\n    return [len(x) for x in xs]\n"})
        self.assertFalse(run_rules(idx, [spec("db_call_in_loop")]))
        # I/O inside a nested function defined in a loop is not loop I/O
        src = (
            "def f(xs):\n"
            "    for x in xs:\n"
            "        def g():\n"
            "            cur.execute('q')\n"
            "        yield g\n"
        )
        idx = index_code({"a.py": src})
        self.assertFalse(run_rules(idx, [spec("db_call_in_loop")]))
        # I/O outside any loop is fine
        idx = index_code({"a.py": "def f():\n    cur.execute('q')\n"})
        self.assertFalse(run_rules(idx, [spec("db_call_in_loop")]))

    def test_swallowed_exception_has_symbol(self):
        idx = index_code({"a.py": "def f():\n    try:\n        x()\n    except Exception:\n        pass\n"})
        hits = run_rules(idx, [spec("swallowed_exception")])
        self.assertTrue(hits)
        self.assertEqual(hits[0].symbol, "f")


if __name__ == "__main__":
    unittest.main()
