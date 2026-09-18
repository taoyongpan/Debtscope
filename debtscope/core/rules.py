"""L2 rule engine — data-driven, parameterizable rules.

A :class:`RuleSpec` binds a rule id/name to a *kind* (a registered detector)
plus severity, parameters and an enabled flag. Built-in specs are seeded into
each project database; users can tune them or add custom specs from the
dashboard (including AI-generated ones). Every detector is deterministic —
the LLM only refines ``unused_function`` candidates in L3.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from .callgraph import classify_io
from .models import Confidence, Finding, Severity
from .python_indexer import Index

# Framework entry-point decorators: functions carrying these are invoked
# implicitly, so "no static call found" must NOT be reported as dead code.
ENTRY_DECORATORS = {
    "route", "get", "post", "put", "delete", "patch", "head", "options",
    "command", "cli", "task", "handler", "event_handler", "listener",
    "receiver", "subscribe", "on_event", "setup", "teardown", "fixture",
    "hook", "action", "endpoint", "main", "scheduled", "cron",
}

TEST_LIFECYCLE = {
    "setUp", "tearDown", "setUpClass", "tearDownClass",
    "setUpModule", "tearDownModule", "setup_method", "teardown_method",
}


@dataclass
class RuleSpec:
    id: str
    name: str
    kind: str
    severity: str = Severity.MEDIUM
    description: str = ""
    params: dict = field(default_factory=dict)
    suggestion: str = ""
    enabled: bool = True
    builtin: bool = True
    needs_review: bool = False   # candidates go through the L3 reviewer

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RuleSpec":
        return cls(
            id=d["id"], name=d["name"], kind=d["kind"],
            severity=d.get("severity", Severity.MEDIUM),
            description=d.get("description", ""),
            params=json_loads(d.get("params") or {}),
            suggestion=d.get("suggestion", ""),
            enabled=bool(d.get("enabled", 1)),
            builtin=bool(d.get("builtin", 0)),
            needs_review=bool(d.get("needs_review", 0)),
        )


def json_loads(value):
    import json
    if isinstance(value, str):
        return json.loads(value or "{}")
    return value or {}


# -- kind registry -----------------------------------------------------------

@dataclass
class KindMeta:
    label: str
    description_template: str
    suggestion: str
    default_severity: str
    params_schema: dict            # name -> "int" | "string"
    default_params: dict
    creatable: bool = True         # exposed for user-created rules
    needs_review: bool = False


KINDS: dict[str, KindMeta] = {
    "unused_function": KindMeta(
        label="疑似废弃函数/方法",
        description_template="全仓静态分析未发现任何调用或引用（隐式入口已排除），AI 精判确认",
        suggestion="确认无反射/动态分发后删除；若为框架回调请补充注册方式说明。",
        default_severity=Severity.MEDIUM, params_schema={}, default_params={},
        creatable=False, needs_review=True,
    ),
    "swallowed_exception": KindMeta(
        label="异常被吞没 / 裸 except",
        description_template="except 块仅 pass，或裸 except 捕获全部异常",
        suggestion="至少记录日志；裸 except 改为捕获具体异常类型。",
        default_severity=Severity.MEDIUM, params_schema={}, default_params={},
        creatable=False,
    ),
    "mutable_default_argument": KindMeta(
        label="可变默认参数",
        description_template="函数默认参数使用 list/dict/set，默认值在多次调用间共享",
        suggestion="将默认值改为 None，在函数体内初始化。",
        default_severity=Severity.MEDIUM, params_schema={}, default_params={},
        creatable=False,
    ),
    "open_without_context": KindMeta(
        label="文件打开未使用 with",
        description_template="open() 未放在 with 语句中，异常路径下可能泄漏文件句柄",
        suggestion="使用 with open(...) as f: 自动关闭。",
        default_severity=Severity.MEDIUM, params_schema={}, default_params={},
        creatable=False,
    ),
    "function_too_long": KindMeta(
        label="函数过长",
        description_template="函数有效行数超过 {max_lines} 行，维护成本高",
        suggestion="按职责拆分为多个小函数。",
        default_severity=Severity.LOW,
        params_schema={"max_lines": "int"}, default_params={"max_lines": 50},
    ),
    "todo_accumulation": KindMeta(
        label="TODO/FIXME 堆积",
        description_template="单文件 TODO/FIXME 数量达到 {max_count} 个以上",
        suggestion="集中清理或转为工单跟踪，避免注释债务长期滞留。",
        default_severity=Severity.LOW,
        params_schema={"max_count": "int"}, default_params={"max_count": 5},
    ),
    "duplicate_function": KindMeta(
        label="重复函数（结构级）",
        description_template="与另一处函数体结构完全一致（≥{min_lines} 行且 ≥{min_stmts} 条语句），疑似复制粘贴",
        suggestion="抽取公共函数，消除重复实现。",
        default_severity=Severity.LOW,
        params_schema={"min_lines": "int", "min_stmts": "int"},
        default_params={"min_lines": 8, "min_stmts": 4},
    ),
    "file_too_long": KindMeta(
        label="文件过长",
        description_template="文件总行数超过 {max_lines} 行，建议按模块拆分",
        suggestion="按职责拆分为多个模块，降低单文件复杂度。",
        default_severity=Severity.LOW,
        params_schema={"max_lines": "int"}, default_params={"max_lines": 300},
    ),
    "too_many_args": KindMeta(
        label="函数参数过多",
        description_template="函数参数个数超过 {max_args} 个，接口职责可能过重",
        suggestion="将相关参数封装为对象/dataclass，或拆分函数职责。",
        default_severity=Severity.LOW,
        params_schema={"max_args": "int"}, default_params={"max_args": 5},
    ),
    "nested_too_deep": KindMeta(
        label="嵌套层级过深",
        description_template="函数内部控制流嵌套超过 {max_depth} 层，圈复杂度高",
        suggestion="用提前返回（guard clause）或抽取子函数降低嵌套。",
        default_severity=Severity.LOW,
        params_schema={"max_depth": "int"}, default_params={"max_depth": 4},
    ),
    "forbidden_call": KindMeta(
        label="禁用调用",
        description_template="代码中出现了禁用的调用：{patterns}",
        suggestion="替换为团队约定的安全实现。",
        default_severity=Severity.MEDIUM,
        params_schema={"patterns": "string"}, default_params={"patterns": ""},
    ),
    "name_convention": KindMeta(
        label="命名规范",
        description_template="{target_label}命名不符合约定：{regex}",
        suggestion="按团队命名规范重命名，并更新引用。",
        default_severity=Severity.LOW,
        params_schema={"target": "string", "regex": "string", "message": "string"},
        default_params={"target": "function", "regex": r"^[a-z_][a-z0-9_]*$",
                        "message": "函数名应使用 snake_case"},
    ),
    "db_call_in_loop": KindMeta(
        label="循环内数据库/HTTP 调用（疑似 N+1）",
        description_template="循环体内直接执行数据库或 HTTP 调用，访问次数随数据量线性放大",
        suggestion="改为批量查询（IN 列表 / executemany / join）或批量 HTTP 接口，"
                   "消除每次迭代一次 I/O 的 N+1 模式。",
        default_severity=Severity.MEDIUM, params_schema={}, default_params={},
        creatable=False,
    ),
}


def describe(kind: str, params: dict) -> str:
    meta = KINDS.get(kind)
    if not meta:
        return kind
    values = dict(meta.default_params)
    values.update(params or {})
    values.setdefault("target_label", "类" if values.get("target") == "class" else "函数")
    try:
        return meta.description_template.format(**values)
    except Exception:
        return meta.description_template


BUILTIN_SPECS: list[RuleSpec] = [
    RuleSpec(id="unused_function", name="疑似废弃函数/方法", kind="unused_function",
             severity=Severity.MEDIUM, needs_review=True,
             description=KINDS["unused_function"].description_template,
             suggestion=KINDS["unused_function"].suggestion),
    RuleSpec(id="swallowed_exception", name="异常被吞没 / 裸 except",
             kind="swallowed_exception", severity=Severity.MEDIUM,
             description=KINDS["swallowed_exception"].description_template,
             suggestion=KINDS["swallowed_exception"].suggestion),
    RuleSpec(id="mutable_default_argument", name="可变默认参数",
             kind="mutable_default_argument", severity=Severity.MEDIUM,
             description=KINDS["mutable_default_argument"].description_template,
             suggestion=KINDS["mutable_default_argument"].suggestion),
    RuleSpec(id="open_without_context", name="文件打开未使用 with",
             kind="open_without_context", severity=Severity.MEDIUM,
             description=KINDS["open_without_context"].description_template,
             suggestion=KINDS["open_without_context"].suggestion),
    RuleSpec(id="long_function", name="超长函数", kind="function_too_long",
             severity=Severity.LOW, params={"max_lines": 50},
             description="函数有效行数超过 50 行，维护成本高",
             suggestion=KINDS["function_too_long"].suggestion),
    RuleSpec(id="todo_accumulation", name="TODO/FIXME 堆积", kind="todo_accumulation",
             severity=Severity.LOW, params={"max_count": 5},
             description="单文件 TODO/FIXME 数量达到 5 个以上",
             suggestion=KINDS["todo_accumulation"].suggestion),
    RuleSpec(id="duplicate_function", name="重复函数（结构级）", kind="duplicate_function",
             severity=Severity.LOW, params={"min_lines": 8, "min_stmts": 4},
             description="与另一处函数体结构完全一致，疑似复制粘贴",
             suggestion=KINDS["duplicate_function"].suggestion),
    RuleSpec(id="db_call_in_loop", name="循环内数据库/HTTP 调用（疑似 N+1）",
             kind="db_call_in_loop", severity=Severity.MEDIUM,
             description=KINDS["db_call_in_loop"].description_template,
             suggestion=KINDS["db_call_in_loop"].suggestion),
]


# -- finding helpers ---------------------------------------------------------


def _f(spec: RuleSpec, file: str, line: int, message: str, *,
       symbol: str = "", confidence: str = Confidence.HIGH,
       evidence: dict | None = None, suggestion: str | None = None) -> Finding:
    meta = KINDS.get(spec.kind)
    fallback = meta.suggestion if meta else ""
    return Finding(
        rule_id=spec.id, file=file, line=line, symbol=symbol,
        severity=spec.severity, confidence=confidence, message=message,
        suggestion=suggestion or spec.suggestion or fallback,
        evidence=evidence or {},
    )


def _is_entry_decorator(name: str) -> bool:
    n = name.lower()
    if n in ENTRY_DECORATORS:
        return True
    return ("route" in n) or n.endswith("_command") or n.endswith("_handler")


def _trees(idx: Index):
    """Yield (rel, tree) for parseable files."""
    for rel in idx.files:
        try:
            tree = ast.parse("\n".join(idx.source[rel]), filename=rel)
        except SyntaxError:
            continue
        yield rel, tree


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _effective_body(handler: ast.ExceptHandler) -> list[ast.stmt]:
    return [
        s for s in handler.body
        if not (isinstance(s, ast.Expr)
                and isinstance(getattr(s, "value", None), ast.Constant))
    ]


def _handler_snippet(node: ast.ExceptHandler, lines: list[str]) -> str:
    end = max(s.end_lineno or s.lineno for s in node.body)
    return "\n".join(lines[node.lineno - 1: end])


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _enclosing_function(parents: dict, node: ast.AST) -> ast.FunctionDef | None:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    return None


# -- detectors ---------------------------------------------------------------

def check_unused_function(idx: Index, spec: RuleSpec) -> list[Finding]:
    out: list[Finding] = []
    for s in idx.symbols:
        if s.is_abstract:
            continue
        if s.name.startswith("__") and s.name.endswith("__"):
            continue
        if s.name == "main" and s.kind == "function":
            continue
        if s.name.startswith("test_") and ("test" in s.file.lower()):
            continue
        if any(_is_entry_decorator(d) for d in s.decorators if d):
            continue
        if s.name in idx.called_names or s.name in idx.referenced_names:
            continue
        out.append(_f(
            spec, file=s.file, line=s.lineno, symbol=s.qualname,
            confidence=Confidence.MEDIUM,
            message=f"静态分析未发现对「{s.qualname}」的任何调用或引用，疑似废弃代码",
            evidence={"snippet": s.snippet, "decorators": s.decorators,
                      "kind": s.kind, "n_lines": s.n_lines},
        ))
    return out


def check_swallowed_exception(idx: Index, spec: RuleSpec) -> list[Finding]:
    out: list[Finding] = []
    for rel, tree in _trees(idx):
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            fn = _enclosing_function(parents, node)
            symbol = fn.name if fn else ""
            effective = _effective_body(node)
            only_pass = bool(effective) and all(
                isinstance(s, ast.Pass)
                or (isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant)
                    and s.value.value is Ellipsis)
                for s in effective
            )
            if node.type is None:
                out.append(_f(
                    spec, file=rel, line=node.lineno, symbol=symbol,
                    message="裸 except 会吞掉 SystemExit/KeyboardInterrupt 等所有异常"
                            + ("，且异常被直接 pass 吞没" if only_pass else ""),
                    evidence={"snippet": _handler_snippet(node, idx.source[rel])},
                ))
            elif only_pass:
                exc = ast.unparse(node.type) if hasattr(ast, "unparse") else "异常"
                out.append(_f(
                    spec, file=rel, line=node.lineno, symbol=symbol,
                    message=f"捕获 {exc} 后直接 pass，异常被静默吞没",
                    evidence={"snippet": _handler_snippet(node, idx.source[rel])},
                ))
    return out


def _mutable_expr(expr: ast.expr | None) -> bool:
    if expr is None:
        return False
    if isinstance(expr, (ast.List, ast.Dict, ast.Set)):
        return True
    if isinstance(expr, ast.Call):
        f = expr.func
        name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
        return name in {"list", "dict", "set"}
    return False


def check_mutable_default(idx: Index, spec: RuleSpec) -> list[Finding]:
    out: list[Finding] = []
    for rel, tree in _trees(idx):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            pairs: list[tuple[str, ast.expr]] = []
            positional = args.args[-len(args.defaults):] if args.defaults else []
            pairs += list(zip([a.arg for a in positional], args.defaults))
            pairs += list(zip([a.arg for a in args.kwonlyargs], args.kw_defaults))
            for arg_name, default in pairs:
                if _mutable_expr(default):
                    out.append(_f(
                        spec, file=rel, line=default.lineno, symbol=node.name,
                        message=f"参数「{arg_name}」使用可变默认值 "
                                f"{ast.unparse(default) if hasattr(ast, 'unparse') else ''}，多次调用共享同一对象",
                        evidence={"snippet": "\n".join(idx.source[rel][node.lineno - 1: node.lineno + 2])},
                    ))
    return out


def _inside_with(call: ast.Call, parents: dict[ast.AST, ast.AST], tree: ast.AST) -> bool:
    cur = parents.get(call)
    while cur is not None and cur is not tree:
        if isinstance(cur, ast.With):
            for item in cur.items:
                if call in [x for x in ast.walk(item.context_expr)]:
                    return True
        cur = parents.get(cur)
    return False


def check_open_without_context(idx: Index, spec: RuleSpec) -> list[Finding]:
    out: list[Finding] = []
    for rel, tree in _trees(idx):
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "open":
                continue
            if _inside_with(node, parents, tree):
                continue
            enclosing = _enclosing_function(parents, node)
            if enclosing is not None and any(
                isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                and x.func.attr == "close"
                for x in ast.walk(enclosing)
            ):
                continue
            out.append(_f(
                spec, file=rel, line=node.lineno,
                symbol=enclosing.name if enclosing else "",
                message="open() 未使用 with 语句，异常路径下文件句柄不会关闭",
                evidence={"snippet": idx.source[rel][node.lineno - 1].strip()},
            ))
    return out


def check_function_too_long(idx: Index, spec: RuleSpec) -> list[Finding]:
    max_lines = int(spec.params.get("max_lines", 50))
    out = []
    for s in idx.symbols:
        if s.n_lines > max_lines:
            out.append(_f(
                spec, file=s.file, line=s.lineno, symbol=s.qualname,
                message=f"函数「{s.qualname}」长达 {s.n_lines} 行（阈值 {max_lines} 行）",
                evidence={"snippet": s.snippet[:800], "n_lines": s.n_lines},
            ))
    return out


def check_todo_accumulation(idx: Index, spec: RuleSpec) -> list[Finding]:
    max_count = int(spec.params.get("max_count", 5))
    out = []
    for rel, todos in idx.todos.items():
        if len(todos) >= max_count:
            out.append(_f(
                spec, file=rel, line=todos[0].line,
                message=f"文件中堆积 {len(todos)} 处 TODO/FIXME 注释债务",
                evidence={"todos": [{"line": t.line, "kind": t.kind, "text": t.text}
                                    for t in todos[:20]]},
            ))
    return out


def check_duplicate_function(idx: Index, spec: RuleSpec) -> list[Finding]:
    min_lines = int(spec.params.get("min_lines", 8))
    min_stmts = int(spec.params.get("min_stmts", 4))
    groups: dict[str, list] = defaultdict(list)
    for s in idx.symbols:
        if s.name.startswith("__") and s.name.endswith("__"):
            continue
        if s.n_lines >= min_lines and s.n_body_stmts >= min_stmts:
            groups[s.shape_digest].append(s)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda s: (s.file, s.lineno))
        primary = members[0]
        for s in members[1:]:
            out.append(_f(
                spec, file=s.file, line=s.lineno, symbol=s.qualname,
                message=f"函数「{s.qualname}」与 {primary.file}:{primary.lineno} 的「{primary.qualname}」结构完全重复",
                evidence={"snippet": s.snippet,
                          "duplicate_of": f"{primary.file}:{primary.lineno}"},
            ))
    return out


def check_file_too_long(idx: Index, spec: RuleSpec) -> list[Finding]:
    max_lines = int(spec.params.get("max_lines", 300))
    return [
        _f(spec, file=rel, line=1,
           message=f"文件「{rel}」共 {n} 行（阈值 {max_lines} 行）",
           evidence={"n_lines": n})
        for rel, n in sorted(idx.file_lines.items()) if n > max_lines
    ]


def check_too_many_args(idx: Index, spec: RuleSpec) -> list[Finding]:
    max_args = int(spec.params.get("max_args", 5))
    return [
        _f(spec, file=s.file, line=s.lineno, symbol=s.qualname,
           message=f"函数「{s.qualname}」有 {s.n_args} 个参数（阈值 {max_args} 个）",
           evidence={"snippet": s.snippet[:600], "n_args": s.n_args})
        for s in idx.symbols if s.n_args > max_args
    ]


def check_nested_too_deep(idx: Index, spec: RuleSpec) -> list[Finding]:
    max_depth = int(spec.params.get("max_depth", 4))
    return [
        _f(spec, file=s.file, line=s.lineno, symbol=s.qualname,
           message=f"函数「{s.qualname}」控制流嵌套深达 {s.max_depth} 层（阈值 {max_depth} 层）",
           evidence={"snippet": s.snippet[:600], "max_depth": s.max_depth})
        for s in idx.symbols if s.max_depth > max_depth
    ]


def check_forbidden_call(idx: Index, spec: RuleSpec) -> list[Finding]:
    raw = str(spec.params.get("patterns", ""))
    patterns = {p.strip() for p in raw.replace("，", ",").split(",") if p.strip()}
    if not patterns:
        return []
    out = []
    seen = set()
    for rel, tree in _trees(idx):
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            hit = next((p for p in patterns if name == p or name.endswith("." + p)), None)
            if not hit:
                continue
            key = (rel, node.lineno, hit)
            if key in seen:
                continue
            seen.add(key)
            fn = _enclosing_function(parents, node)
            out.append(_f(
                spec, file=rel, line=node.lineno, symbol=fn.name if fn else "",
                message=f"禁用调用「{hit}」出现在代码中",
                evidence={"snippet": idx.source[rel][node.lineno - 1].strip()},
            ))
    return out


def check_name_convention(idx: Index, spec: RuleSpec) -> list[Finding]:
    target = spec.params.get("target", "function")
    pattern = spec.params.get("regex", "")
    message = spec.params.get("message", "命名不符合规范")
    if not pattern:
        return []
    try:
        rx = re.compile(pattern)
    except re.error:
        return []
    out = []
    if target == "class":
        for rel, tree in _trees(idx):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and not rx.fullmatch(node.name):
                    out.append(_f(
                        spec, file=rel, line=node.lineno, symbol=node.name,
                        message=f"类「{node.name}」{message}（规则 {pattern}）",
                        evidence={"snippet": idx.source[rel][node.lineno - 1].strip()},
                    ))
    else:
        for s in idx.symbols:
            if s.name in TEST_LIFECYCLE and "test" in s.file.lower():
                continue
            if not rx.fullmatch(s.name):
                out.append(_f(
                    spec, file=s.file, line=s.lineno, symbol=s.qualname,
                    message=f"函数「{s.qualname}」{message}（规则 {pattern}）",
                    evidence={"snippet": s.snippet[:400]},
                ))
    return out


def _loop_direct_calls(loop: ast.AST) -> list[ast.Call]:
    """Calls in this loop's own iteration body.

    Nested loops are pruned (their calls belong to the inner loop's finding);
    nested function/lambda defs are pruned too (they are not executed per
    iteration at this point).
    """
    prune = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
             ast.For, ast.AsyncFor, ast.While)
    calls: list[ast.Call] = []

    def walk(node: ast.AST):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, prune):
                continue
            if isinstance(child, ast.Call):
                calls.append(child)
            walk(child)

    for stmt in loop.body:
        if isinstance(stmt, prune):
            continue
        walk(stmt)
    return calls


def check_db_call_in_loop(idx: Index, spec: RuleSpec) -> list[Finding]:
    out: list[Finding] = []
    for rel, tree in _trees(idx):
        parents = _parent_map(tree)
        lines = idx.source[rel]
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                continue
            hits: list[dict] = []
            seen_labels: set[str] = set()
            for call in _loop_direct_calls(node):
                try:
                    chain = ast.unparse(call.func)
                except Exception:
                    chain = _call_name(call)
                io = classify_io(chain)
                if io and chain not in seen_labels:
                    seen_labels.add(chain)
                    hits.append({"line": call.lineno, "io": io, "call": chain})
            if not hits:
                continue
            fn = _enclosing_function(parents, node)
            loop_word = "while" if isinstance(node, ast.While) else "for"
            end = node.end_lineno or node.lineno
            io_zh = {"db": "数据库", "http": "HTTP"}
            kinds = "/".join(sorted({io_zh.get(h["io"], h["io"]) for h in hits}))
            labels = "、".join(h["call"] for h in hits[:6])
            out.append(_f(
                spec, file=rel, line=node.lineno,
                symbol=fn.name if fn else "",
                confidence=Confidence.MEDIUM,
                message=f"第 {node.lineno} 行 {loop_word} 循环体内有 {len(hits)} 处"
                        f"{kinds}调用（{labels}），随迭代次数线性放大，疑似 N+1",
                evidence={
                    "snippet": "\n".join(lines[node.lineno - 1: end])[:900],
                    "io_calls": hits,
                },
                suggestion="将循环内查询改为批量查询（IN 列表 / executemany / JOIN），"
                           "HTTP 调用改为批量接口或一次性并发聚合，避免每次迭代都产生 I/O。",
            ))
    return out


CHECKS = {
    "unused_function": check_unused_function,
    "swallowed_exception": check_swallowed_exception,
    "mutable_default_argument": check_mutable_default,
    "open_without_context": check_open_without_context,
    "function_too_long": check_function_too_long,
    "todo_accumulation": check_todo_accumulation,
    "duplicate_function": check_duplicate_function,
    "file_too_long": check_file_too_long,
    "too_many_args": check_too_many_args,
    "nested_too_deep": check_nested_too_deep,
    "forbidden_call": check_forbidden_call,
    "name_convention": check_name_convention,
    "db_call_in_loop": check_db_call_in_loop,
}


def run_rules(idx: Index, specs: list[RuleSpec]) -> list[Finding]:
    findings: list[Finding] = []
    for spec in specs:
        if not spec.enabled:
            continue
        check = CHECKS.get(spec.kind)
        if check is None:
            continue
        try:
            findings.extend(check(idx, spec))
        except Exception:
            # A broken custom rule must never take down a whole scan.
            continue
    return findings


def custom_spec_id(kind: str) -> str:
    import secrets
    return f"custom.{kind}.{secrets.token_hex(3)}"
