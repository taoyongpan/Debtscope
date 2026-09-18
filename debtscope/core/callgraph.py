"""Static call graph for Python repositories (stdlib ``ast`` only).

Nodes are functions/methods with stable keys ``rel/path.py::QualName``.
Edges are calls resolved from imports plus local definitions:

  * same-file top-level functions and ``self/cls`` methods resolve exactly;
  * ``import x`` / ``from x import y`` resolve to in-repo modules/functions;
  * third-party and dynamic calls stay **unresolved** — they are never guessed.

Unresolved calls to recognizable I/O sinks (DB cursor/HTTP clients) are kept
as labeled external sink nodes so chain views can flag N+1-style patterns.
Everything else (builtins, unknown attribute chains) is dropped from the graph
to avoid noise.
"""
from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass, field

from .python_indexer import Index
from .models import Symbol

MAX_CHAIN_DEPTH = 12
MAX_CHAIN_NODES = 120

# I/O sink recognition (conservative — false negatives are preferred to
# false positives here; the LLM chain review in later versions fills semantic
# gaps such as cross-function N+1).
_DB_ATTRS = {
    "execute", "executemany", "fetchone", "fetchall",
    "fetchmany", "executescript",
}
_DB_CHAIN_KEYWORDS = (
    ".query", ".commit", ".rollback", ".flush",
    ".bulk_insert", ".bulk_save",
)
_HTTP_PREFIX = ("requests.", "httpx.", "aiohttp.")
_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "request"}
_HTTP_HINTS = ("requests", "httpx", "aiohttp", "urlopen")


def node_key(file: str, qualname: str) -> str:
    return f"{file}::{qualname}"


def classify_io(chain: str) -> str | None:
    """Label an external call as ``db`` / ``http`` / ``None`` (conservative)."""
    if not chain:
        return None
    # Attribute on a call result, e.g. ``requests.get(...).json()`` — the real
    # sink is the inner call, already seen by ast.walk; don't double-label it.
    if "(" in chain:
        return None
    low = chain.lower()
    attr = chain.rsplit(".", 1)[-1]
    if chain.startswith(_HTTP_PREFIX) or "urlopen" in low:
        return "http"
    if attr in _DB_ATTRS:
        return "db"
    if any(k in low for k in _DB_CHAIN_KEYWORDS):
        return "db"
    if attr in _HTTP_VERBS and any(h in low for h in _HTTP_HINTS):
        return "http"
    return None


@dataclass
class CallSite:
    callee: str | None       # node key when resolved, else None
    line: int
    label: str               # displayed call text (e.g. cur.execute)
    io: str | None = None    # "db" | "http" | None


@dataclass
class FileDefs:
    funcs: dict[str, str] = field(default_factory=dict)    # name -> qualname
    classes: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class CallGraph:
    root: str
    symbols: dict[str, Symbol] = field(default_factory=dict)
    calls: dict[str, list[CallSite]] = field(default_factory=dict)
    module_files: dict[str, str] = field(default_factory=dict)
    file_defs: dict[str, FileDefs] = field(default_factory=dict)

    def out(self, key: str) -> list[CallSite]:
        return self.calls.get(key, [])


def _parse(idx: Index, rel: str):
    try:
        return ast.parse("\n".join(idx.source[rel]), filename=rel)
    except SyntaxError:
        return None


def build_call_graph(idx: Index) -> CallGraph:
    cg = CallGraph(root=idx.root)

    # node table
    for s in idx.symbols:
        cg.symbols[node_key(s.file, s.qualname)] = s

    # module dotted-name -> file
    for rel in idx.files:
        if not rel.endswith(".py"):
            continue
        parts = [p for p in rel[:-3].split("/") if p]
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            cg.module_files[".".join(parts)] = rel

    # pass 1: per-file definitions
    trees: dict[str, ast.Module] = {}
    for rel in idx.files:
        tree = _parse(idx, rel)
        if tree is None:
            continue
        trees[rel] = tree
        fd = FileDefs()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fd.funcs[node.name] = node.name
            elif isinstance(node, ast.ClassDef):
                fd.classes[node.name] = {
                    sub.name: f"{node.name}.{sub.name}"
                    for sub in node.body
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
        cg.file_defs[rel] = fd

    # pass 2: imports + call sites per function
    for rel, tree in trees.items():
        imports = _collect_imports(tree, rel)
        fd = cg.file_defs[rel]

        def handle_function(fn, qualname, class_name):
            key = node_key(rel, qualname)
            sites: list[CallSite] = []
            # Walk the body only — decorator_list and default arguments are
            # evaluated at definition time, not part of the runtime call chain.
            for stmt in fn.body:
                for sub in ast.walk(stmt):
                    if not isinstance(sub, ast.Call):
                        continue
                    site = _resolve_call(sub, rel, imports, fd, cg, class_name)
                    if site is not None:
                        sites.append(site)
            if sites:
                cg.calls[key] = sites

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                handle_function(node, node.name, None)
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        handle_function(sub, f"{node.name}.{sub.name}", node.name)
    return cg


def _collect_imports(tree: ast.Module, rel: str) -> dict[str, tuple[str | None, str | None]]:
    """local name -> (module dotted name, imported attr or None)."""
    pkg_parts = [p for p in rel.split("/")[:-1]]
    imports: dict[str, tuple[str | None, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                imports[local] = (alias.name if alias.asname else local, None)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                drop = node.level - 1
                base = pkg_parts[: len(pkg_parts) - drop] if drop else pkg_parts
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                # imported name may itself be a submodule/package
                sub_mod = f"{module}.{alias.name}" if module else alias.name
                imports[local] = (module, alias.name)
    return imports


def _resolve_module_function(cg: CallGraph, module: str, func: str) -> str | None:
    rel = cg.module_files.get(module)
    if rel is None:
        return None
    qual = cg.file_defs[rel].funcs.get(func)
    return node_key(rel, qual) if qual else None


def _resolve_qualified(cg: CallGraph, module: str, parts: list[str]) -> str | None:
    """Resolve ``module.parts[0]...func`` to an in-repo function."""
    if not parts:
        return None
    full = [module] + parts if module else parts
    func = full[-1]
    mod = ".".join(full[:-1])
    return _resolve_module_function(cg, mod, func)


def _resolve_classmethod(cg: CallGraph, module: str, class_name: str,
                         method: str) -> str | None:
    rel = cg.module_files.get(module)
    if rel is None:
        return None
    methods = cg.file_defs[rel].classes.get(class_name)
    qual = methods.get(method) if methods else None
    return node_key(rel, qual) if qual else None


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _resolve_call(call: ast.Call, rel: str, imports: dict, fd: FileDefs,
                  cg: CallGraph, class_name: str | None) -> CallSite | None:
    func = call.func
    if isinstance(func, ast.Name):
        ident = func.id
        # same-file top-level function
        if ident in fd.funcs:
            return CallSite(node_key(rel, fd.funcs[ident]), call.lineno, ident)
        # imported names
        imp = imports.get(ident)
        if imp is not None:
            module, attr = imp
            if attr is not None:
                # prefer "from pkg.mod import submodule" over attr lookup
                sub_rel = cg.module_files.get(f"{module}.{attr}" if module else attr)
                if sub_rel is not None:
                    return CallSite(None, call.lineno, ident, None)
                callee = _resolve_module_function(cg, module, attr)
                return CallSite(callee, call.lineno, ident, None)
            return CallSite(None, call.lineno, ident, None)
        # local class constructors and builtins: no graph edge
        return None
    if isinstance(func, ast.Attribute):
        chain = _unparse(func)
        if not chain:
            return None
        io = classify_io(chain)
        parts = chain.split(".")
        # base may be a Name (svc.work) or an inline constructor C().work()
        base_expr = func.value
        if isinstance(base_expr, ast.Name):
            base = base_expr.id
        elif (isinstance(base_expr, ast.Call)
              and isinstance(base_expr.func, ast.Name)):
            base = base_expr.func.id
        else:
            base = parts[0]
        callee: str | None = None
        if base in ("self", "cls") and class_name and len(parts) == 2:
            methods = fd.classes.get(class_name, {})
            qual = methods.get(func.attr)
            if qual:
                callee = node_key(rel, qual)
        else:
            imp = imports.get(base)
            if imp is not None:
                module, attr = imp
                if attr is None:
                    # import module [as base] -> module.attr chain
                    callee = _resolve_qualified(cg, module, parts[1:])
                else:
                    # from module import Class/obj; static-method call only
                    callee = _resolve_classmethod(cg, module, attr, func.attr)
            elif base in fd.classes and len(parts) == 2:
                qual = fd.classes[base].get(func.attr)
                if qual:
                    callee = node_key(rel, qual)
        # Only keep unresolved external calls that are recognizable I/O sinks;
        # ordinary attribute calls (dict.get, .append ...) are graph noise.
        if callee is None and io is None:
            return None
        return CallSite(callee, call.lineno, chain, io)
    # calls of calls / subscripts / lambdas — keep only recognizable I/O
    chain = _unparse(func)
    io = classify_io(chain)
    if io:
        return CallSite(None, call.lineno, chain, io)
    return None


# -- chains ------------------------------------------------------------------

@dataclass
class Chain:
    root: str
    nodes: dict[str, int] = field(default_factory=dict)        # key -> depth
    edges: list[dict] = field(default_factory=list)            # {from,to,line}
    ext: dict[str, dict] = field(default_factory=dict)         # key -> {label,io,depth}

    @property
    def depth(self) -> int:
        return max(self.nodes.values(), default=0)

    @property
    def node_count(self) -> int:
        return len(self.nodes)


def build_chain(cg: CallGraph, root_key: str,
                max_depth: int = MAX_CHAIN_DEPTH,
                max_nodes: int = MAX_CHAIN_NODES) -> Chain:
    chain = Chain(root=root_key)
    if root_key not in cg.symbols:
        return chain
    chain.nodes[root_key] = 0
    seen_edges: set[tuple] = set()
    queue: deque[tuple[str, int]] = deque([(root_key, 0)])
    while queue:
        key, depth = queue.popleft()
        if depth >= max_depth or len(chain.nodes) >= max_nodes:
            continue
        for site in cg.out(key):
            if site.callee:
                edge = (key, site.callee, site.line)
                if edge not in seen_edges:
                    seen_edges.add(edge)
                    chain.edges.append({"from": key, "to": site.callee,
                                        "line": site.line})
                if site.callee not in chain.nodes:
                    chain.nodes[site.callee] = depth + 1
                    queue.append((site.callee, depth + 1))
            elif site.io:
                ext_key = f"ext:{site.io}:{site.label}"
                if ext_key not in chain.ext:
                    chain.ext[ext_key] = {"label": site.label, "io": site.io,
                                          "depth": depth + 1, "from": []}
                if key not in chain.ext[ext_key]["from"]:
                    chain.ext[ext_key]["from"].append(key)
    return chain


def build_all_chains(cg: CallGraph, endpoints: list) -> dict[str, Chain]:
    """endpoint.id -> Chain; endpoints whose handler cannot be resolved get none."""
    out: dict[str, Chain] = {}
    for ep in endpoints:
        root = node_key(ep.handler_file, ep.handler_qualname)
        if root in cg.symbols:
            out[ep.id] = build_chain(cg, root)
    return out


def blast_radius(chains: dict[str, Chain]) -> dict[str, int]:
    """How many distinct endpoints reach each internal node."""
    hits: dict[str, set] = {}
    for ep_id, chain in chains.items():
        for key in chain.nodes:
            hits.setdefault(key, set()).add(ep_id)
    return {k: len(v) for k, v in hits.items()}
