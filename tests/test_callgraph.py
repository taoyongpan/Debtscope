"""Static call-graph tests: import resolution, edges, I/O sinks, chains."""
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from debtscope.core.callgraph import (  # noqa: E402
    blast_radius, build_all_chains, build_call_graph, build_chain,
    classify_io, node_key,
)
from debtscope.core.python_indexer import PythonIndexer  # noqa: E402


FILES = {
    "app.py": """
import svc
from helper import ping

class C:
    def m1(self):
        self.m2()
    def m2(self):
        return 2

def handler():
    svc.work()
    ping()
    C().m1()
    return len([])
""",
    "svc.py": """
import requests

def work():
    cur.execute("select 1")
    requests.get("http://x.example")

def shared():
    pass

def a():
    shared()

def b():
    shared()

def cyc_a():
    pass

def cyc_b():
    cyc_a()
    cyc_b()
""",
    "helper.py": """
def ping():
    return 1
""",
}


def make_index(files):
    tmp = tempfile.mkdtemp()
    for rel, src in files.items():
        path = os.path.join(tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
    return PythonIndexer().index(tmp)


class CallGraphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = make_index(FILES)
        cls.cg = build_call_graph(cls.idx)

    def test_module_table(self):
        self.assertEqual(self.cg.module_files.get("svc"), "svc.py")
        self.assertEqual(self.cg.module_files.get("helper"), "helper.py")

    def test_same_file_and_self_edges(self):
        out = {s.callee: s for s in self.cg.out(node_key("app.py", "handler"))}
        self.assertIn(node_key("app.py", "C.m1"), out)
        self.assertIn(node_key("helper.py", "ping"), out)
        self.assertIn(node_key("svc.py", "work"), out)
        # builtins like len() do not create edges
        self.assertNotIn(None, out)
        m1_out = self.cg.out(node_key("app.py", "C.m1"))
        self.assertTrue(
            any(s.callee == node_key("app.py", "C.m2") for s in m1_out))

    def test_external_io_sinks(self):
        labels = {(s.label, s.io) for s in self.cg.out(node_key("svc.py", "work"))}
        self.assertIn(("cur.execute", "db"), labels)
        self.assertIn(("requests.get", "http"), labels)
        # third-party module itself is never treated as an in-repo callee
        for s in self.cg.out(node_key("svc.py", "work")):
            if s.label == "requests.get":
                self.assertIsNone(s.callee)

    def test_chain_bfs_depth(self):
        chain = build_chain(self.cg, node_key("app.py", "handler"))
        self.assertEqual(chain.nodes[node_key("app.py", "handler")], 0)
        self.assertEqual(chain.nodes[node_key("svc.py", "work")], 1)
        self.assertEqual(chain.nodes[node_key("app.py", "C.m2")], 2)
        # external sinks attached
        ios = {v["io"] for v in chain.ext.values()}
        self.assertEqual(ios, {"db", "http"})
        # every ext sink records its caller node
        for v in chain.ext.values():
            self.assertTrue(v["from"])

    def test_chain_terminates_on_cycles(self):
        chain = build_chain(self.cg, node_key("svc.py", "cyc_b"))
        self.assertIn(node_key("svc.py", "cyc_a"), chain.nodes)
        self.assertIn(node_key("svc.py", "cyc_b"), chain.nodes)
        self.assertLessEqual(chain.node_count, 6)

    def test_blast_radius(self):
        eps = [
            types.SimpleNamespace(id="e1", handler_file="svc.py",
                                  handler_qualname="a"),
            types.SimpleNamespace(id="e2", handler_file="svc.py",
                                  handler_qualname="b"),
        ]
        chains = build_all_chains(self.cg, eps)
        blast = blast_radius(chains)
        self.assertEqual(blast[node_key("svc.py", "shared")], 2)
        self.assertEqual(blast[node_key("svc.py", "a")], 1)

    def test_classify_io_conservative(self):
        self.assertEqual(classify_io("cur.execute"), "db")
        self.assertEqual(classify_io("cur.fetchall"), "db")
        self.assertEqual(classify_io("session.query"), "db")
        self.assertEqual(classify_io("conn.commit"), "db")
        self.assertEqual(classify_io("requests.post"), "http")
        self.assertEqual(classify_io("httpx.get"), "http")
        # flask request proxy / dict access are not HTTP
        self.assertIsNone(classify_io("request.args.get"))
        self.assertIsNone(classify_io("data.get"))
        # outer .json() call on a call result is not double-labelled
        self.assertIsNone(classify_io("requests.get(x).json"))


if __name__ == "__main__":
    unittest.main()
