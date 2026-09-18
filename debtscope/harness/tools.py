"""Read-only tools that expose the deterministic core to the agent loop.

An agent never imports core functions directly; it may only gather evidence
through these registered tools (the architecture doc's tool contract). All
tools are read-only and all file access is confined to the repository root.

Tools return plain JSON-serializable values so observations can be fed back
to the model verbatim.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re

from ..core.callgraph import node_key
from ..core.rules import BUILTIN_SPECS, KINDS, RuleSpec, run_rules
from .kernel import Kernel, Tool, ToolError, safe_join

_SNIPPET_LINES = 120
_GREP_SKIP = {".git", "__pycache__", ".debtscope", "node_modules", ".venv", "venv"}


def register_tools(kernel: Kernel, idx, cg, *, endpoints=None, chains=None,
                   blast=None, findings=None) -> None:
    """Register the standard read-only toolkit onto ``kernel``."""
    findings = findings or []
    findings_by_node: dict[str, list] = {}
    for f in findings:
        symbol = getattr(f, "symbol", None) or (f.get("symbol") if isinstance(f, dict) else "")
        if not symbol:
            continue
        rel = getattr(f, "file", None) or f.get("file", "")
        status = getattr(f, "status", None) or f.get("status", "open")
        if status not in ("open", "confirmed", "wontfix"):
            continue
        findings_by_node.setdefault(node_key(rel, symbol), []).append(f)

    # -- codegraph -----------------------------------------------------------

    def t_symbols(prefix: str = "", kind: str = "", file: str = "",
                  limit: int = 50) -> dict:
        rows = []
        for key, sym in cg.symbols.items():
            rel, qual = key.split("::", 1)
            if file and rel != file:
                continue
            if kind and sym.kind != kind:
                continue
            if prefix and prefix not in qual:
                continue
            rows.append({
                "key": key, "symbol": qual, "kind": sym.kind, "file": rel,
                "line": sym.lineno, "end_line": sym.end_lineno,
                "n_lines": sym.n_lines, "n_args": sym.n_args,
                "decorators": sym.decorators,
            })
        return {"count": len(rows), "symbols": rows[:max(1, min(int(limit), 200))]}

    def _resolve_keys(target: str) -> list[str]:
        if "::" in target and target in cg.symbols:
            return [target]
        return [k for k in cg.symbols if k.split("::", 1)[1] == target
                or k.split("::", 1)[1].endswith("." + target)]

    def t_callers(symbol: str) -> dict:
        """Every resolved in-repo call site of a symbol (empty = dead-code evidence)."""
        keys = _resolve_keys(symbol)
        if not keys:
            raise ToolError(f"符号不存在：{symbol}（先用 codegraph.symbols 确认名字）")
        sites = []
        for owner, calls in cg.calls.items():
            for site in calls:
                if site.callee in keys:
                    sites.append({"caller": owner, "line": site.line,
                                  "call": site.label})
        return {"symbol": symbol, "keys": keys, "caller_count": len(sites),
                "callers": sites,
                "note": ("caller_count=0 且 grep 无引用时，才是废弃代码的强证据；"
                         "回调/装饰器/反射入口需结合 read_file 判断")}

    def t_callees(symbol: str) -> dict:
        keys = _resolve_keys(symbol)
        if not keys:
            raise ToolError(f"符号不存在：{symbol}")
        key = keys[0]
        rows = [{"callee": s.callee, "line": s.line, "label": s.label,
                 "io": s.io} for s in cg.out(key)]
        return {"symbol": key, "callees": rows}

    # -- source access -------------------------------------------------------

    def _source_lines(rel: str) -> list[str]:
        if rel in idx.source:
            return idx.source[rel]
        path = safe_join(kernel.root, rel)
        if not os.path.isfile(path):
            raise ToolError(f"文件不存在：{rel}")
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()

    def t_read_file(file: str, start: int = 1, end: int = 0) -> dict:
        lines = _source_lines(file)
        start = max(1, int(start))
        end = min(len(lines), int(end) or start + _SNIPPET_LINES - 1)
        if start > end:
            raise ToolError("行号范围非法")
        body = [{"n": n, "text": lines[n - 1]} for n in range(start, end + 1)]
        return {"file": file, "start": start, "end": end,
                "total_lines": len(lines), "lines": body}

    def t_grep(pattern: str, glob: str = "*.py", limit: int = 100) -> dict:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"正则非法：{exc}") from exc
        cap = max(1, min(int(limit), 500))
        hits = []
        for rel in idx.files:
            if glob and not fnmatch.fnmatch(os.path.basename(rel), glob) \
                    and not fnmatch.fnmatch(rel, glob):
                continue
            if any(part in _GREP_SKIP for part in rel.split(os.sep)):
                continue
            for n, text in enumerate(idx.source.get(rel, []), 1):
                if rx.search(text):
                    hits.append({"file": rel, "line": n, "text": text.strip()[:200]})
                    if len(hits) >= cap:
                        return {"pattern": pattern, "truncated": True,
                                "count": len(hits), "hits": hits}
        return {"pattern": pattern, "truncated": False, "count": len(hits), "hits": hits}

    # -- rules ---------------------------------------------------------------

    def t_rules_catalog() -> dict:
        kinds, creatable = [], []
        for kid, meta in KINDS.items():
            row = {"kind": kid, "label": meta.label,
                   "default_severity": meta.default_severity,
                   "params_schema": meta.params_schema,
                   "creatable": meta.creatable,
                   "suggestion": meta.suggestion}
            kinds.append(row)
            if meta.creatable:
                creatable.append(row)
        builtins = [{"id": s.id, "name": s.name, "kind": s.kind,
                     "severity": s.severity, "params": s.params}
                    for s in BUILTIN_SPECS]
        return {"builtin_rules": builtins, "creatable_kinds": creatable,
                "all_kinds": kinds}

    def t_rule_preview(kind: str, params: dict = None,
                       severity: str = "medium") -> dict:
        if kind not in KINDS:
            raise ToolError(f"未知指标类型：{kind}；先用 rules.catalog 查看可用类型")
        meta = KINDS[kind]
        clean_params = {}
        for name, ptype in meta.params_schema.items():
            if params and name in params:
                raw = params[name]
                if ptype == "int":
                    try:
                        clean_params[name] = int(raw)
                    except (TypeError, ValueError):
                        raise ToolError(f"参数 {name} 必须是整数") from None
                else:
                    clean_params[name] = raw
            else:
                clean_params[name] = meta.default_params.get(name)
        spec = RuleSpec(id="preview", name="preview", kind=kind,
                        severity=severity or meta.default_severity,
                        params=clean_params, builtin=False)
        try:
            hits = run_rules(idx, [spec])
        except Exception as exc:
            raise ToolError(f"试跑失败：{exc}") from exc
        return {"kind": kind, "params": clean_params, "count": len(hits),
                "samples": [{"file": f.file, "line": f.line, "symbol": f.symbol,
                             "severity": f.severity, "message": f.message[:200]}
                            for f in hits[:20]]}

    # -- interface chains ----------------------------------------------------

    def t_endpoint_chain(method: str = "", path: str = "") -> dict:
        if not endpoints or not chains:
            raise ToolError("当前索引未构建接口链路")
        if not path:
            raise ToolError("请提供 path（可选 method 消歧）")
        matched = None
        for ep in endpoints:
            if ep.path != path:
                continue
            if method and ep.method != method.upper() and method.upper() not in ep.method:
                continue
            matched = ep
            break
        if matched is None:
            raise ToolError(f"未找到接口：{method} {path}")
        chain = chains.get(matched.id)
        if chain is None:
            return {"endpoint": matched.to_dict(), "resolvable": False}
        nodes = []
        for key, depth in sorted(chain.nodes.items(), key=lambda kv: (kv[1], kv[0])):
            sym = cg.symbols.get(key)
            rel, qual = key.split("::", 1)
            fs = findings_by_node.get(key, [])
            nodes.append({
                "key": key, "qualname": qual, "file": rel,
                "line": sym.lineno if sym else 0,
                "n_lines": sym.n_lines if sym else 0,
                "depth": depth,
                "blast": (blast or {}).get(key, 1),
                "findings": [{"rule": f.rule_id, "severity": f.severity,
                              "confidence": f.confidence,
                              "message": f.message[:160]} for f in fs],
            })
        ext = [{"key": k, **v} for k, v in chain.ext.items()]
        return {"endpoint": matched.to_dict(), "resolvable": True,
                "depth": chain.depth, "node_count": chain.node_count,
                "nodes": nodes, "ext_sinks": ext, "edges": chain.edges}

    kernel.register_tool(Tool(
        "codegraph.symbols",
        "列出仓库内函数/方法符号，可按名字前缀(prefix)、类型(kind=function|method)、文件(file)过滤",
        t_symbols,
        {"type": "object",
         "properties": {"prefix": {"type": "string"}, "kind": {"type": "string"},
                        "file": {"type": "string"}, "limit": {"type": "integer"}}}))
    kernel.register_tool(Tool(
        "codegraph.callers",
        "查询某函数/方法在仓库内的全部静态调用方与行号；返回空表示没有任何调用（废弃代码核心证据）。symbol 可用限定名 Class.method 或短名",
        t_callers,
        {"type": "object", "required": ["symbol"],
         "properties": {"symbol": {"type": "string"}}}))
    kernel.register_tool(Tool(
        "codegraph.callees",
        "查询某函数调用了哪些项目内函数与数据库/HTTP 外部汇点",
        t_callees,
        {"type": "object", "required": ["symbol"],
         "properties": {"symbol": {"type": "string"}}}))
    kernel.register_tool(Tool(
        "read_file",
        "读取仓库内文件的带行号片段（只读，禁止访问仓库根目录之外）",
        t_read_file,
        {"type": "object", "required": ["file"],
         "properties": {"file": {"type": "string"}, "start": {"type": "integer"},
                        "end": {"type": "integer"}}}))
    kernel.register_tool(Tool(
        "grep",
        "在仓库源码中按正则搜索文本（用于查装饰器、字符串引用、注册回调等调用图覆盖不到的引用）",
        t_grep,
        {"type": "object", "required": ["pattern"],
         "properties": {"pattern": {"type": "string"}, "glob": {"type": "string"},
                        "limit": {"type": "integer"}}}))
    kernel.register_tool(Tool(
        "rules.catalog",
        "列出全部内置规则与可创建指标类型（kind、参数、默认严重度）",
        t_rules_catalog, {"type": "object"}))
    kernel.register_tool(Tool(
        "rule.preview",
        "用指定 kind 与参数在当前索引上试跑规则，返回命中数量与样例；不会落库",
        t_rule_preview,
        {"type": "object", "required": ["kind"],
         "properties": {"kind": {"type": "string"}, "params": {"type": "object"},
                        "severity": {"type": "string"}}}))
    kernel.register_tool(Tool(
        "endpoint.chain",
        "按 HTTP 方法+路径获取接口的静态调用链：节点、数据库/HTTP 汇点、影响面与每个节点上的技术债",
        t_endpoint_chain,
        {"type": "object", "required": ["path"],
         "properties": {"path": {"type": "string"}, "method": {"type": "string"}}}))


def build_kernel(root: str, idx, cg, *, tracer=None, endpoints=None,
                 chains=None, blast=None, findings=None,
                 allow_writes: bool = False) -> Kernel:
    kernel = Kernel(root, tracer=tracer, allow_writes=allow_writes)
    register_tools(kernel, idx, cg, endpoints=endpoints, chains=chains,
                   blast=blast, findings=findings)
    return kernel
