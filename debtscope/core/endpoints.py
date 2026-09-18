"""HTTP entry-point discovery for Python web frameworks (stdlib ``ast`` only).

Recognized, fully statically:

  * Flask — ``app = Flask(...)`` / ``bp = Blueprint(..., url_prefix=...)``,
    ``@app.route`` / ``@bp.get`` / ``register_blueprint(..., url_prefix=)``
  * FastAPI — ``app = FastAPI()`` / ``router = APIRouter(prefix=...)``,
    ``@app.get`` / ``@router.post`` / ``include_router(..., prefix=)``
  * generic — a ``@route("/path")`` style decorator on a plain function
    (the demo project's hand-rolled router uses this)

Class-based views (``MethodView`` / Django ``urls.py``) are not parsed yet.
Decorator and variable names alone drive recognition (the framework package
does not need to be installed because code is never executed or imported).
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field

from .python_indexer import Index

HTTP_METHOD_DECORATORS = {
    "get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE",
    "patch": "PATCH", "head": "HEAD", "options": "OPTIONS",
}

FLASK_APP_CTORS = {"Flask"}
FLASK_BP_CTORS = {"Blueprint"}
FASTAPI_APP_CTORS = {"FastAPI"}
FASTAPI_ROUTER_CTORS = {"APIRouter", "APIRoute"}


@dataclass
class Endpoint:
    id: str
    method: str            # GET / POST / ... / ANY
    path: str
    framework: str         # flask | fastapi | generic
    handler_file: str
    handler_qualname: str
    handler_line: int
    decorators: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "method": self.method, "path": self.path,
            "framework": self.framework, "handler_file": self.handler_file,
            "handler_qualname": self.handler_qualname,
            "handler_line": self.handler_line,
        }


def _eid(method: str, path: str, file: str, qualname: str) -> str:
    basis = f"{method}|{path}|{file}|{qualname}"
    return "e_" + hashlib.sha1(basis.encode()).hexdigest()[:12]


def _join_path(*parts: str) -> str:
    raw = "".join(p or "" for p in parts)
    if not raw.startswith("/"):
        raw = "/" + raw
    return re.sub(r"/+", "/", raw)


def _decorator_name_parts(dec: ast.expr) -> tuple[str | None, str | None]:
    """Return (variable, attr) for ``@var.attr(...)`` decorators."""
    cur = dec
    if isinstance(cur, ast.Call):
        cur = cur.func
    if isinstance(cur, ast.Attribute) and isinstance(cur.value, ast.Name):
        return cur.value.id, cur.attr
    if isinstance(cur, ast.Name):
        return None, cur.id
    return None, None


def _first_str_arg(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(
        call.args[0].value, str
    ):
        return call.args[0].value
    return None


def _methods_kw(call: ast.Call) -> list[str]:
    for kw in call.keywords:
        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
            out = []
            for elt in kw.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append(elt.value.upper())
            return out
    return []


def _prefix_kw(call: ast.Call, name: str) -> str:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(
            kw.value.value, str
        ):
            return kw.value.value
    return ""


@dataclass
class RouterVar:
    kind: str           # flask_app | flask_bp | fastapi_app | fastapi_router
    own_prefix: str = ""


def _parse_tree(tree: ast.Module, rel: str) -> list[Endpoint]:
    # 1) router variables and their own prefixes
    routers: dict[str, RouterVar] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        call = node.value if isinstance(node, ast.AnnAssign) else node.value
        if not isinstance(call, ast.Call):
            continue
        ctor = call.func.id if isinstance(call.func, ast.Name) else ""
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if not targets:
            continue
        if ctor in FLASK_APP_CTORS:
            kind, prefix_kw = "flask_app", ""
        elif ctor in FLASK_BP_CTORS:
            kind, prefix_kw = "flask_bp", _prefix_kw(call, "url_prefix")
        elif ctor in FASTAPI_APP_CTORS:
            kind, prefix_kw = "fastapi_app", ""
        elif ctor in FASTAPI_ROUTER_CTORS:
            kind, prefix_kw = "fastapi_router", _prefix_kw(call, "prefix")
        else:
            continue
        for t in targets:
            routers[t] = RouterVar(kind, prefix_kw)

    # 2) blueprint/router registration prefixes (may register more than once)
    extra_prefix: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr not in ("register_blueprint", "include_router"):
            continue
        if not node.args:
            continue
        target = node.args[0]
        if not isinstance(target, ast.Name) or target.id not in routers:
            continue
        if attr == "register_blueprint":
            prefix = _prefix_kw(node, "url_prefix")
        else:
            prefix = _prefix_kw(node, "prefix")
        extra_prefix.setdefault(target.id, []).append(prefix)

    # 3) decorated handlers
    out: list[Endpoint] = []
    seen: set[tuple] = set()

    def add(method, full_path, framework, fn, decs):
        key = (method, full_path, qualname_of(fn))
        if key in seen:
            return
        seen.add(key)
        qual = qualname_of(fn)
        out.append(Endpoint(
            id=_eid(method, full_path, rel, qual), method=method, path=full_path,
            framework=framework, handler_file=rel, handler_qualname=qual,
            handler_line=fn.lineno, decorators=decs,
        ))

    def qualname_of(fn):
        return fn.name  # top-level handlers only in v0.4

    def iter_functions(body, class_ctx=None):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield node, class_ctx
            elif isinstance(node, ast.ClassDef):
                # MethodView-style: methods named after HTTP verbs, no route
                # decorator — skipped for now; nested classes still traversed
                yield from iter_functions(node.body, node.name)

    for fn, class_ctx in iter_functions(tree.body):
        for dec in fn.decorator_list:
            var, attr = _decorator_name_parts(dec)
            if not isinstance(dec, ast.Call):
                # bare @route (no call) — generic only if named route
                if attr == "route" and var is None:
                    full = _join_path()
                    add("ANY", "/", "generic", fn, ["route"])
                continue
            route_path = _first_str_arg(dec)
            if route_path is None:
                continue
            dec_names = [attr] if attr else []
            if var is not None and attr is not None:
                rv = routers.get(var)
                if rv is None:
                    # attribute decorator on an unknown object: only accept
                    # well-known method names to avoid random false positives
                    if attr not in HTTP_METHOD_DECORATORS and attr not in (
                        "route", "api_route"
                    ):
                        continue
                    framework, method, prefixes = "generic", None, [""]
                else:
                    framework = "flask" if rv.kind.startswith("flask") else "fastapi"
                    prefixes = (extra_prefix.get(var) if rv.kind.endswith("bp")
                                or rv.kind.endswith("router") else None)
                    if prefixes is None:
                        prefixes = [""]          # the application object itself
                    method = None
                if attr in HTTP_METHOD_DECORATORS:
                    method = HTTP_METHOD_DECORATORS[attr]
                elif attr in ("route", "api_route"):
                    methods = _methods_kw(dec)
                    if methods:
                        method = "/".join(methods)
                    elif framework == "flask":
                        method = "GET"          # Flask's default
                    else:
                        method = "ANY"
                else:
                    continue
                own = rv.own_prefix if rv is not None and rv.kind.endswith(
                    ("bp", "router")
                ) else ""
                for reg in prefixes:
                    full = _join_path(reg, own, route_path)
                    add(method, full, framework, fn, dec_names)
            elif attr == "route":
                # @route("/x") generic decorator
                methods = _methods_kw(dec)
                method = "/".join(methods) if methods else "ANY"
                add(method, route_path, "generic", fn, dec_names)
    return out


def discover_endpoints(idx: Index) -> list[Endpoint]:
    out: list[Endpoint] = []
    for rel in idx.files:
        try:
            tree = ast.parse("\n".join(idx.source[rel]), filename=rel)
        except SyntaxError:
            continue
        out.extend(_parse_tree(tree, rel))
    out.sort(key=lambda e: (e.framework, e.path, e.method))
    return out
