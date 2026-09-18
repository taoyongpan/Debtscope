"""Tests for the agent micro-kernel: registry, guards, events, path safety."""
import os
import tempfile
import unittest

from debtscope.harness.kernel import Kernel, Tool, ToolError, safe_join


def _tool(name="echo", read_only=True):
    return Tool(
        name, "echo tool",
        lambda text="", n=1: {"echo": text, "n": n},
        {"type": "object", "required": ["text"],
         "properties": {"text": {"type": "string"}, "n": {"type": "integer"}}},
        read_only=read_only)


class KernelTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.k = Kernel(self.root)

    def test_register_and_call(self):
        self.k.register_tool(_tool())
        self.assertEqual(self.k.call_tool("echo", {"text": "hi"})["echo"], "hi")
        self.assertIn("echo", self.k.tool_names())
        desc = self.k.describe_tools()[0]
        self.assertTrue(desc["read_only"])

    def test_duplicate_registration_rejected(self):
        self.k.register_tool(_tool())
        with self.assertRaises(RuntimeError):
            self.k.register_tool(_tool())

    def test_missing_required_arg(self):
        self.k.register_tool(_tool())
        with self.assertRaises(ToolError):
            self.k.call_tool("echo", {})

    def test_wrong_type(self):
        self.k.register_tool(_tool())
        with self.assertRaises(ToolError):
            self.k.call_tool("echo", {"text": "x", "n": "not-int"})

    def test_args_must_be_object(self):
        self.k.register_tool(_tool())
        with self.assertRaises(ToolError):
            self.k.call_tool("echo", ["text"])  # type: ignore[arg-type]

    def test_unknown_tool(self):
        with self.assertRaises(ToolError):
            self.k.call_tool("nope", {})

    def test_write_tool_blocked_in_readonly_mode(self):
        self.k.register_tool(_tool("write", read_only=False))
        with self.assertRaises(ToolError):
            self.k.call_tool("write", {"text": "x"})
        k2 = Kernel(self.root, allow_writes=True)
        k2.register_tool(_tool("write", read_only=False))
        self.assertEqual(k2.call_tool("write", {"text": "ok"})["echo"], "ok")

    def test_events_emitted(self):
        seen = []
        self.k.on("tool.call", lambda p: seen.append(("call", p["tool"])))
        self.k.on("tool.result", lambda p: seen.append(("result", p["tool"], p["ms"])))
        self.k.register_tool(_tool())
        self.k.call_tool("echo", {"text": "x"})
        self.assertEqual(seen[0], ("call", "echo"))
        self.assertEqual(seen[1][:2], ("result", "echo"))
        self.assertGreaterEqual(seen[1][2], 0)

    def test_tool_error_event_and_raise(self):
        def boom():
            raise ValueError("kaboom")
        self.k.register_tool(Tool("boom", "raises", boom, {"type": "object"}))
        errors = []
        self.k.on("tool.error", lambda p: errors.append(p["error"]))
        with self.assertRaises(ToolError):
            self.k.call_tool("boom", {})
        self.assertIn("kaboom", errors[0])

    def test_listener_exception_is_isolated(self):
        self.k.on("tool.call", lambda p: 1 / 0)
        self.k.register_tool(_tool())
        # listener raising must not break the call
        self.assertEqual(self.k.call_tool("echo", {"text": "x"})["echo"], "x")

    def test_safe_join_allows_inside(self):
        p = safe_join(self.root, os.path.join("a", "b.py"))
        self.assertTrue(p.startswith(os.path.realpath(self.root)))

    def test_safe_join_blocks_traversal(self):
        with self.assertRaises(ToolError):
            safe_join(self.root, os.path.join("..", "..", "etc", "passwd"))

    def test_safe_join_blocks_absolute_escape(self):
        with self.assertRaises(ToolError):
            safe_join(self.root, "/etc/passwd")

    def test_safe_join_rejects_empty(self):
        with self.assertRaises(ToolError):
            safe_join(self.root, "  ")


if __name__ == "__main__":
    unittest.main()
