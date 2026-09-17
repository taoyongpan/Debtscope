"""End-to-end smoke tests on the seeded demo repository.

Run: python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DEMO = os.path.join(ROOT, "fixtures", "demo_project")

from debtscope.core.scanner import scan_repo  # noqa: E402
from debtscope.core.storage import Storage   # noqa: E402

ACTIVE = ("open", "confirmed", "wontfix")
ALL = ACTIVE + ("false_positive", "resolved")


class DebtscopeSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "debtscope.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_finds_expected_debt(self):
        summary = scan_repo(DEMO, self.db, use_llm=False)
        self.assertLess(summary["score"], 100)

        store = Storage(self.db)
        findings = store.list_findings(statuses=ACTIVE)
        store.close()

        rule_ids = {f["rule_id"] for f in findings}
        for rid in (
            "unused_function", "swallowed_exception", "mutable_default_argument",
            "open_without_context", "long_function", "todo_accumulation",
            "duplicate_function",
        ):
            self.assertIn(rid, rule_ids, f"rule {rid} produced no findings on demo repo")

        # Anti-false-positive: implicit entry points and used helpers stay quiet.
        unused = {f["symbol"] for f in findings if f["rule_id"] == "unused_function"}
        self.assertIn("old_export_format_v1", unused)
        self.assertIn("ReportBuilder.render_html", unused)
        self.assertNotIn("health_check", unused)      # @route entry point
        self.assertNotIn("list_users", unused)       # @route entry point
        self.assertNotIn("flatten_dedup", unused)    # genuinely referenced
        self.assertNotIn("_load_users", unused)      # referenced across module

        mutable = [f for f in findings if f["rule_id"] == "mutable_default_argument"]
        self.assertTrue(any(f["symbol"] == "add_tag" for f in mutable))

        # Every finding is traceable.
        for f in findings:
            self.assertTrue(f["file"])
            self.assertGreater(f["line"], 0)
            self.assertTrue(f["evidence"])

    def test_rescan_is_stable(self):
        first = scan_repo(DEMO, self.db, use_llm=False)
        second = scan_repo(DEMO, self.db, use_llm=False)
        self.assertEqual(second["reconcile"]["new"], 0)
        self.assertEqual(second["reconcile"]["resolved"], 0)
        self.assertEqual(second["score"], first["score"])

    def test_false_positive_stays_suppressed(self):
        scan_repo(DEMO, self.db, use_llm=False)
        store = Storage(self.db)
        target = store.list_findings(statuses=("open",))[0]
        store.set_review(target["id"], "false_positive", None)
        store.close()

        scan_repo(DEMO, self.db, use_llm=False)
        store = Storage(self.db)
        rows = {f["id"]: f for f in store.list_findings(statuses=ALL)}
        self.assertEqual(rows[target["id"]]["status"], "false_positive")
        store.close()


if __name__ == "__main__":
    unittest.main()
