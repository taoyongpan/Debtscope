"""Micro-kernel for the agent shell: tool registry, event bus, guards.

Borrowed from the DeepSeek Harness paradigm but deliberately tiny and
dependency-free:

* tools are registered callables with a compact JSON-schema contract;
* a minimal event bus (``on`` / ``emit``) lets the tracer and future plugins
  observe runs without coupling them to the analysis core;
* every tool is read-only by default and write tools additionally require an
  explicit ``allow_writes`` switch (no write tools ship in v1);
* file access is confined to the repository root (realpath containment).

The deterministic analysis pipeline (indexer, rules, aggregation) never goes
through the kernel — the kernel only serves the agent loop, so an agent can
never bypass the structural checks.
"""
from __future__ import annotations

import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class ToolError(Exception):
    """A tool rejected its arguments or could not produce a result."""


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    input_schema: dict = field(default_factory=lambda: {"type": "object"})
    read_only: bool = True

    def validate(self, args: Any) -> dict:
        if not isinstance(args, dict):
            raise ToolError(f"工具 {self.name} 的参数必须是 JSON 对象")
        schema = self.input_schema or {}
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in args or args[key] in (None, ""):
                raise ToolError(f"工具 {self.name} 缺少必填参数：{key}")
        for key, value in args.items():
            spec = props.get(key)
            if not spec or "type" not in spec:
                continue
            expected = _JSON_TYPES.get(spec["type"])
            if expected and not isinstance(value, expected):
                raise ToolError(
                    f"工具 {self.name} 参数 {key} 类型应为 {spec['type']}")
        return args

    def call(self, args: dict) -> Any:
        return self.fn(**self.validate(args))


def safe_join(root: str, rel: str) -> str:
    """Join ``rel`` under ``root`` and refuse anything that escapes it."""
    if not isinstance(rel, str) or not rel.strip():
        raise ToolError("文件路径不能为空")
    if os.path.isabs(rel) and not rel.startswith(root):
        # allow absolute paths only when already inside root
        candidate = os.path.realpath(rel)
    else:
        candidate = os.path.realpath(os.path.join(root, rel))
    root_real = os.path.realpath(root)
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        raise ToolError(f"路径越界，禁止访问仓库根目录之外：{rel}")
    return candidate


class Kernel:
    """Registry + event bus shared by agent runs against one repository."""

    def __init__(self, root: str, tracer=None, allow_writes: bool = False):
        self.root = os.path.realpath(root)
        self.tracer = tracer
        self.allow_writes = allow_writes
        self._tools: dict[str, Tool] = {}
        self._listeners: dict[str, list[Callable]] = defaultdict(list)

    # -- registry -----------------------------------------------------------

    def register_tool(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise RuntimeError(f"工具已注册：{tool.name}")
        self._tools[tool.name] = tool

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tool(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"工具不存在：{name}")
        return tool

    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def describe_tools(self) -> list[dict]:
        return [
            {"name": t.name, "description": t.description,
             "input_schema": t.input_schema, "read_only": t.read_only}
            for t in self._tools.values()
        ]

    # -- events -------------------------------------------------------------

    def on(self, event: str, cb: Callable[[dict], None]) -> None:
        self._listeners[event].append(cb)

    def emit(self, event: str, payload: dict | None = None) -> None:
        for cb in self._listeners.get(event, []):
            try:
                cb(payload or {})
            except Exception:
                pass  # listeners must never break a run

    # -- invocation ---------------------------------------------------------

    def call_tool(self, name: str, args: dict | None = None,
                  call_id: str | None = None) -> Any:
        tool = self.get_tool(name)
        if not tool.read_only and not self.allow_writes:
            raise ToolError(f"写工具 {name} 当前被禁用（只读模式）")
        cid = call_id or "t_" + uuid.uuid4().hex[:8]
        args = args or {}
        self.emit("tool.call", {"id": cid, "tool": name, "args": args})
        if self.tracer is not None:
            self.tracer.tool_call(cid, name, args)
        started = time.time()
        try:
            result = tool.call(args)
        except Exception as exc:
            ms = int((time.time() - started) * 1000)
            err = exc if isinstance(exc, ToolError) else ToolError(str(exc))
            self.emit("tool.error", {"id": cid, "tool": name,
                                     "error": str(err), "ms": ms})
            if self.tracer is not None:
                self.tracer.tool_result(cid, name, error=str(err), ms=ms)
            raise err from exc
        ms = int((time.time() - started) * 1000)
        size = _json_size(result)
        self.emit("tool.result", {"id": cid, "tool": name, "ms": ms, "bytes": size})
        if self.tracer is not None:
            self.tracer.tool_result(cid, name, ms=ms, bytes_=size)
        return result


def _json_size(value: Any) -> int:
    import json
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0
