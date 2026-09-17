"""End-to-end HTTP API tests against the real stdlib server (no LLM, no network).

The server binds 127.0.0.1 on an ephemeral port; model calls are disabled via
a patched Config so scans stay fully deterministic and offline.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEMO = os.path.join(ROOT, "fixtures", "demo_project")

from debtscope.config import Config            # noqa: E402
from debtscope.harness.projects import ProjectRegistry  # noqa: E402
from debtscope.web.server import build_handler  # noqa: E402

DISABLED = Config(llm_enabled=False, provider="custom", api_base="",
                  api_key=None, model="", timeout=1, max_retries=0)


class WebApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.registry = ProjectRegistry(base_dir=cls.tmp.name)
        handler = build_handler(cls.registry)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.cfg_patch = patch.object(Config, "load", return_value=DISABLED)
        cls.cfg_patch.start()
        # Register (without scanning) so every test method gets a stable pid;
        # test_05 exercises the scan-on-create API path itself.
        cls.pid = cls.registry.add("demo", DEMO).id

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        cls.cfg_patch.stop()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, raw_host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        if raw_host is not None:
            conn.putrequest(method, path, skip_host=True,
                            skip_accept_encoding=True)
            conn.putheader("Host", raw_host)
            if body is not None:
                payload = json.dumps(body)
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(payload)))
            conn.endheaders()
            if body is not None:
                conn.send(payload.encode())
        else:
            headers = {}
            payload = None
            if body is not None:
                payload = json.dumps(body)
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except json.JSONDecodeError:
            return resp.status, raw

    def test_01_boot_payload(self):
        status, data = self.req("GET", "/api/boot")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data["providers"]), 16)
        self.assertIn("forbidden_call", data["rule_kinds"])

    def test_02_static_asset_served(self):
        status, data = self.req("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("Debtscope", data)

    def test_03_static_traversal_blocked(self):
        for evil in ("/static/../../../../etc/passwd",
                     "/static/..%2f..%2f..%2fetc%2fpasswd"):
            status, _ = self.req("GET", evil)
            self.assertIn(status, (400, 404), f"{evil} must not escape static dir")

    def test_04_spoofed_host_rejected(self):
        status, data = self.req("GET", "/api/boot", raw_host="evil.example.com")
        self.assertEqual(status, 403)

    def test_05_project_create_and_overview(self):
        status, data = self.req("POST", "/api/projects",
                                {"path": DEMO, "name": "demo"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["project"]["id"], self.pid)
        self.assertLess(data["summary"]["score"], 100)

        status, overview = self.req("GET", f"/api/projects/{self.pid}/overview")
        self.assertEqual(status, 200)
        self.assertGreater(overview["total_open"], 0)

        status, findings = self.req("GET", f"/api/projects/{self.pid}/findings")
        self.assertEqual(status, 200)
        self.assertEqual(findings["count"], overview["total_open"])

    def test_06_rule_preview_validation(self):
        # invalid regex -> 400 with a Chinese reason
        status, data = self.req(
            "POST", f"/api/projects/{self.pid}/rules/preview",
            {"name": "x", "kind": "name_convention",
             "params": {"target": "function", "regex": "([unclosed", "message": "x"}})
        self.assertEqual(status, 400)
        self.assertIn("正则", data["error"])

        # valid forbidden_call preview -> 200
        status, data = self.req(
            "POST", f"/api/projects/{self.pid}/rules/preview",
            {"name": "禁 print", "kind": "forbidden_call",
             "params": {"patterns": "print"}})
        self.assertEqual(status, 200)
        self.assertIn("count", data)

    def test_07_rule_lifecycle_and_builtin_protection(self):
        # create a custom rule
        status, data = self.req(
            "POST", f"/api/projects/{self.pid}/rules",
            {"name": "函数不超过 20 行", "kind": "function_too_long",
             "severity": "low", "params": {"max_lines": 20}})
        self.assertEqual(status, 200, data)
        rid = data["rule"]["id"]
        self.assertTrue(rid.startswith("custom."))

        # built-in rules cannot be deleted
        status, _ = self.req("DELETE", f"/api/projects/{self.pid}/rules/long_function")
        self.assertEqual(status, 400)

        # built-in rule can be disabled via PUT
        status, data = self.req("PUT", f"/api/projects/{self.pid}/rules/long_function",
                                {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(data["rule"]["enabled"])

        # custom rule can be deleted
        status, _ = self.req("DELETE", f"/api/projects/{self.pid}/rules/{rid}")
        self.assertEqual(status, 200)

    def test_08_code_api_validates_params(self):
        status, data = self.req(
            "GET", f"/api/projects/{self.pid}/code?file=api.py&around=notanint")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
