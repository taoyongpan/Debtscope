"""L1 indexer for Python repositories (stdlib ``ast`` only).

Builds, deterministically and without executing any code:
  * a symbol table (module functions + class methods)
  * the set of every name that is called or referenced anywhere
  * normalized structural fingerprints of function bodies (for dup detection)
  * TODO/FIXME comments
  * source-line cache for evidence snippets

The language backend is intentionally pluggable: everything downstream
(rules, reviewer, storage, UI) consumes the :class:`Index` shape, so a
tree-sitter backend for Java/Go can be added without touching L2+.
"""
from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass, field

from .models import Symbol

EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
    "build", "dist", "node_modules", ".tox", ".mypy_cache", ".pytest_cache",
    "site-packages", ".debtscope", "eggs", ".eggs",
}


@dataclass
class TodoItem:
    line: int
    kind: str       # TODO | FIXME
    text: str


@dataclass
class Index:
    root: str
    files: list[str] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    called_names: set[str] = field(default_factory=set)
    referenced_names: set[str] = field(default_factory=set)
    todos: dict[str, list[TodoItem]] = field(default_factory=dict)
    source: dict[str, list[str]] = field(default_factory=dict)
    file_lines: dict[str, int] = field(default_factory=dict)
    parse_errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def n_lines(self) -> int:
        return sum(len(lines) for lines in self.source.values())


def _decorator_name(node: ast.expr) -> str:
    """@route -> 'route', @app.route -> 'route', @router.get('/x') -> 'get'."""
    cur = node
    if isinstance(cur, ast.Call):
        cur = cur.func
    if isinstance(cur, ast.Attribute):
        return cur.attr
    if isinstance(cur, ast.Name):
        return cur.id
    return ""


def _shape(node: ast.AST) -> tuple:
    """Structural shape: node types only, ignoring names, literals and lines.

    Copy-pasted functions with renamed variables/strings still match.
    """
    children = list(ast.iter_child_nodes(node))
    return (type(node).__name__,) + tuple(_shape(c) for c in children)


_FLOW_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
)


def _arg_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    positional = [a.arg for a in node.args.args]
    if positional and positional[0] in ("self", "cls"):
        positional = positional[1:]
    return len(positional) + len(node.args.kwonlyargs)


def _max_nesting(node: ast.AST, depth: int = 0) -> int:
    """Deepest control-flow nesting (if/for/while/with/try)."""
    best = depth
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _FLOW_NODES):
            best = max(best, _max_nesting(child, depth + 1))
        else:
            best = max(best, _max_nesting(child, depth))
    return best


def _is_abstract(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in method.decorator_list:
        if _decorator_name(dec) in {"abstractmethod", "abc"}:
            return True
    body = [s for s in method.body if not isinstance(s, ast.Expr)
            or not isinstance(getattr(s, "value", None), ast.Constant)]
    if len(body) == 1 and isinstance(body[0], ast.Raise):
        return True
    return False


class PythonIndexer:
    extensions = (".py",)

    def index(self, root: str) -> Index:
        idx = Index(root=os.path.abspath(root))
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
            for fn in sorted(filenames):
                if not fn.endswith(self.extensions):
                    continue
                abspath = os.path.join(dirpath, fn)
                rel = os.path.relpath(abspath, root)
                try:
                    with open(abspath, "r", encoding="utf-8", errors="replace") as fh:
                        source = fh.read()
                except OSError:
                    continue
                lines = source.splitlines()
                idx.files.append(rel)
                idx.source[rel] = lines
                idx.file_lines[rel] = len(lines)
                idx.todos[rel] = self._extract_todos(lines)
                try:
                    tree = ast.parse(source, filename=rel)
                except SyntaxError as e:
                    idx.parse_errors.append((rel, str(e)))
                    continue
                self._collect_names(tree, idx)
                self._collect_symbols(tree, rel, lines, idx)
        return idx

    # -- names / calls -----------------------------------------------------

    def _collect_names(self, tree: ast.AST, idx: Index) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    idx.called_names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    idx.called_names.add(func.attr)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                idx.referenced_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                # method/attribute references such as callbacks passed around
                idx.referenced_names.add(node.attr)

    # -- symbols -----------------------------------------------------------

    def _collect_symbols(
        self, tree: ast.Module, rel: str, lines: list[str], idx: Index
    ) -> None:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                idx.symbols.append(
                    self._make_symbol(node, node.name, node.name, "function", rel, lines)
                )
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        qual = f"{node.name}.{sub.name}"
                        idx.symbols.append(
                            self._make_symbol(sub, sub.name, qual, "method", rel, lines)
                        )

    def _make_symbol(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        name: str,
        qualname: str,
        kind: str,
        rel: str,
        lines: list[str],
    ) -> Symbol:
        start, end = node.lineno, node.end_lineno or node.lineno
        snippet = "\n".join(lines[start - 1 : end])
        body = node.body
        # skip a pure docstring when measuring real body size
        real_body = [
            s for s in body
            if not (
                isinstance(s, ast.Expr)
                and isinstance(getattr(s, "value", None), ast.Constant)
            )
        ]
        shape = tuple(_shape(s) for s in real_body)
        digest = hashlib.sha1(repr(shape).encode()).hexdigest()[:16]
        return Symbol(
            name=name,
            qualname=qualname,
            kind=kind,
            file=rel,
            lineno=start,
            end_lineno=end,
            decorators=[_decorator_name(d) for d in node.decorator_list],
            snippet=snippet,
            shape_digest=digest,
            n_lines=end - start + 1,
            n_body_stmts=len(real_body),
            is_abstract=_is_abstract(node),
            n_args=_arg_count(node),
            max_depth=_max_nesting(node),
        )

    # -- comments ----------------------------------------------------------

    def _extract_todos(self, lines: list[str]) -> list[TodoItem]:
        out: list[TodoItem] = []
        for i, line in enumerate(lines, start=1):
            stripped = line.lstrip("# \t")
            for kind in ("FIXME", "TODO", "XXX", "HACK"):
                if kind in stripped.upper():
                    # only treat as a marker when it reads like a comment tag
                    upper = stripped.upper()
                    pos = upper.find(kind)
                    prefix_ok = pos == 0 or not upper[pos - 1].isalnum()
                    suffix = stripped[pos + len(kind) :].strip(" :#-\t")
                    if prefix_ok and (suffix or kind in ("TODO", "FIXME")):
                        out.append(TodoItem(i, kind if kind in ("TODO", "FIXME") else "TODO", suffix or "(no description)"))
                    break
        return out
