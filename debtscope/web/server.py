"""Local web server (stdlib http.server, no third-party deps).

Multi-project flow:
  1. configure a model (full-screen onboarding)
  2. add / pick a project to monitor (first scan = initialization)
  3. dashboard, with per-project rule management
"""
from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..core import health
from ..core.python_indexer import PythonIndexer
from ..core.storage import Storage

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTEXT_LINES = 12
ACTIVE = ("open", "confirmed", "wontfix")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _safe_resolve(root: str, rel: str) -> str | None:
    """Resolve rel under root, rejecting path traversal."""
    target = os.path.realpath(os.path.join(root, rel))
    root_real = os.path.realpath(root)
    if target == root_real or target.startswith(root_real + os.sep):
        return target
    return None


def build_handler(registry, initial_pid: str | None = None):
    # one scan lock per project; a short-lived index cache for rule previews
    scan_locks: dict[str, threading.Lock] = {}
    index_cache: dict[str, tuple[float, object]] = {}

    def lock_for(pid: str) -> threading.Lock:
        return scan_locks.setdefault(pid, threading.Lock())

    def get_index(pid: str, project):
        now = time.time()
        cached = index_cache.get(pid)
        if cached and now - cached[0] < 30:
            return cached[1]
        idx = PythonIndexer().index(project.path)
        index_cache[pid] = (now, idx)
        return idx

    class Handler(BaseHTTPRequestHandler):
        server_version = "Debtscope/0.2"

        def log_message(self, *_args):
            pass

        # -- helpers --------------------------------------------------------

        def _json(self, payload, status: int = 200):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

        def _static(self, name: str):
            path = os.path.join(STATIC_DIR, name)
            if not os.path.isfile(path):
                self.send_error(404); return
            with open(path, "rb") as fh:
                body = fh.read()
            ext = os.path.splitext(path)[1]
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _project(self, pid: str):
            project = registry.get(pid)
            if project is None:
                self._json({"error": "project not found"}, status=404)
                return None, None
            return project, Storage(registry.db_path(pid))

        # -- routing --------------------------------------------------------

        def do_GET(self):
            parsed = urlparse(self.path)
            route, qs = parsed.path, parse_qs(parsed.query)
            try:
                if route in ("/", "/index.html"):
                    return self._static("index.html")
                if route.startswith("/static/"):
                    return self._static(route[len("/static/"):])
                if route == "/api/boot":
                    return self._boot(initial_pid)
                if route == "/api/config":
                    return self._config_get()
                if route == "/api/projects":
                    return self._projects()
                if route.startswith("/api/projects/"):
                    return self._project_get(route, qs)
                self.send_error(404)
            except Exception as e:
                self._json({"error": str(e)}, status=500)

        def do_POST(self):
            parsed = urlparse(self.path)
            route = parsed.path
            try:
                payload = self._body()
                if route == "/api/config":
                    return self._config_save(payload)
                if route == "/api/config/test":
                    return self._config_test(payload)
                if route == "/api/projects":
                    return self._project_create(payload)
                if route.startswith("/api/projects/"):
                    return self._project_post(route, payload)
                self.send_error(404)
            except Exception as e:
                self._json({"error": str(e)}, status=500)

        def do_PUT(self):
            parsed = urlparse(self.path)
            try:
                payload = self._body()
                parts = [p for p in parsed.path.split("/") if p]
                # api / projects / <pid> / rules / <rid>
                if (len(parts) == 5 and parts[0] == "api" and parts[1] == "projects"
                        and parts[3] == "rules"):
                    return self._rule_update(parts[2], parts[4], payload)
                self.send_error(404)
            except Exception as e:
                self._json({"error": str(e)}, status=500)

        def do_DELETE(self):
            parsed = urlparse(self.path)
            try:
                parts = [p for p in parsed.path.split("/") if p]
                if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                    qs = parse_qs(parsed.query)
                    return self._project_delete(parts[2], qs)
                if (len(parts) == 5 and parts[:2] == ["api", "projects"]
                        and parts[3] == "rules"):
                    return self._rule_delete(parts[2], parts[4])
                self.send_error(404)
            except Exception as e:
                self._json({"error": str(e)}, status=500)

        # -- boot / config ---------------------------------------------------

        def _boot(self, focus_pid):
            from ..config import Config
            from ..core.rules import KINDS
            from ..harness.config_store import CONFIG_PATH, PROVIDERS, load_config_file, masked_key
            fc = load_config_file()
            cfg = Config.load()
            return self._json({
                "configured": cfg.llm_enabled,
                "config_exists": os.path.isfile(CONFIG_PATH),
                "config_path": CONFIG_PATH,
                "provider": fc.provider,
                "api_base": fc.api_base,
                "model": fc.model,
                "key_masked": masked_key(fc.api_key),
                "providers": {
                    k: {"label": v["label"], "api_base": v["api_base"],
                        "model": v["model"], "key_url": v["key_url"]}
                    for k, v in PROVIDERS.items()
                },
                "projects": [p.to_dict() for p in registry.list()],
                "focus_pid": focus_pid,
                "rule_kinds": {
                    k: {"label": v.label, "description_template": v.description_template,
                        "suggestion": v.suggestion, "default_severity": v.default_severity,
                        "params_schema": v.params_schema, "default_params": v.default_params,
                        "creatable": v.creatable, "needs_review": v.needs_review}
                    for k, v in KINDS.items()
                },
            })

        def _config_get(self):
            from ..config import Config
            from ..harness.config_store import CONFIG_PATH, PROVIDERS, load_config_file, masked_key
            fc = load_config_file()
            cfg = Config.load()
            self._json({
                "exists": os.path.isfile(CONFIG_PATH),
                "config_path": CONFIG_PATH,
                "provider": fc.provider,
                "api_base": fc.api_base,
                "model": fc.model,
                "key_masked": masked_key(fc.api_key),
                "llm_enabled": cfg.llm_enabled,
                "providers": {
                    k: {"label": v["label"], "api_base": v["api_base"],
                        "model": v["model"], "key_url": v["key_url"]}
                    for k, v in PROVIDERS.items()
                },
            })

        def _config_save(self, payload: dict):
            from ..harness.config_store import (
                PROVIDERS, FileConfig, load_config_file, save_config_file,
            )
            current = load_config_file()
            provider = payload.get("provider") or current.provider or "custom"
            preset = PROVIDERS.get(provider, PROVIDERS["custom"])
            fc = FileConfig(
                provider=provider,
                api_base=(payload.get("api_base") or preset["api_base"] or current.api_base).rstrip("/"),
                api_key=payload.get("api_key") or current.api_key,
                model=payload.get("model") or current.model or preset["model"],
            )
            save_config_file(fc)
            from ..config import Config
            self._json({"ok": True, "llm_enabled": Config.load().llm_enabled})

        def _config_test(self, payload: dict):
            # optionally test values posted with the request without saving them
            from ..config import Config
            from ..core.llm import LLMClient
            from ..harness.config_store import (
                PROVIDERS, FileConfig, save_config_file,
            )
            if payload.get("api_base") or payload.get("provider"):
                provider = payload.get("provider", "custom")
                preset = PROVIDERS.get(provider, PROVIDERS["custom"])
                fc = FileConfig(
                    provider=provider,
                    api_base=(payload.get("api_base") or preset["api_base"]).rstrip("/"),
                    api_key=payload.get("api_key"),
                    model=payload.get("model") or preset["model"],
                )
                if not fc.api_base:
                    self._json({"ok": False, "msg": "请填写 API Base"}, status=400); return
                from ..config import Config as _C
                cfg = _C(llm_enabled=bool(fc.api_key or "://127.0.0.1" in fc.api_base),
                         provider=provider, api_base=fc.api_base, api_key=fc.api_key,
                         model=fc.model, timeout=15, max_retries=0)
            else:
                cfg = Config.load()
            if not cfg.llm_enabled:
                self._json({"ok": False, "msg": "缺少 API Key 或可用端点（本地端点除外）"}, status=400)
                return
            ok, msg = LLMClient(cfg).ping()
            self._json({"ok": ok, "msg": msg, "model": cfg.model, "base": cfg.api_base},
                       status=200 if ok else 400)

        # -- projects --------------------------------------------------------

        def _projects(self):
            self._json({"projects": [p.to_dict() for p in registry.list()]})

        def _project_create(self, payload: dict):
            name = (payload.get("name") or "").strip()
            path = (payload.get("path") or "").strip()
            if not path:
                self._json({"error": "请填写项目路径"}, status=400); return
            try:
                project = registry.add(name, path)
            except ValueError as e:
                self._json({"error": str(e)}, status=400); return
            summary = self._run_scan(project)
            if isinstance(summary, tuple):
                return  # error response already written
            self._json({"ok": True, "project": project.to_dict(), "summary": summary})

        def _project_delete(self, pid: str, qs):
            delete_data = qs.get("delete_data", ["0"])[0] in ("1", "true")
            ok = registry.remove(pid, delete_data=delete_data)
            self._json({"ok": ok}, status=200 if ok else 404)

        def _project_get(self, route: str, qs):
            parts = [p for p in route.split("/") if p]
            # api / projects / <pid> / <resource> ...
            if len(parts) < 4:
                self.send_error(404); return
            pid, resource = parts[2], parts[3]
            project, store = self._project(pid)
            if project is None:
                return
            try:
                if resource == "overview":
                    return self._overview(project, store)
                if resource == "findings":
                    return self._findings(store, qs)
                if resource == "snapshots":
                    return self._snapshots(store)
                if resource == "code":
                    return self._code(project, qs)
                if resource == "rules":
                    return self._rules_list(store)
                self.send_error(404)
            finally:
                store.close()

        def _project_post(self, route: str, payload: dict):
            parts = [p for p in route.split("/") if p]
            pid = parts[2] if len(parts) > 2 else ""
            project = registry.get(pid)
            if project is None:
                self._json({"error": "project not found"}, status=404); return

            # api/projects/<pid>/scan
            if len(parts) == 4 and parts[3] == "scan":
                summary = self._run_scan(project)
                if not isinstance(summary, tuple):
                    self._json({"ok": True, "summary": summary})
                return
            # api/projects/<pid>/rules[/...]
            if len(parts) >= 4 and parts[3] == "rules":
                return self._rules_post(project, parts[4:], payload)
            # api/projects/<pid>/findings/<fid>/review
            if len(parts) == 6 and parts[3] == "findings" and parts[5] == "review":
                store = Storage(registry.db_path(pid))
                try:
                    try:
                        store.set_review(int(parts[4]), payload.get("status", "open"),
                                         payload.get("note"))
                    except KeyError:
                        self._json({"error": "finding not found"}, status=404); return
                    self._json({"ok": True})
                finally:
                    store.close()
                return
            self.send_error(404)

        def _run_scan(self, project):
            from ..config import Config
            from ..core.scanner import scan_repo
            if not lock_for(project.id).acquire(blocking=False):
                self._json({"error": "该项目正在扫描中，请稍候"}, status=409)
                return (False,)
            try:
                cfg = Config.load()
                summary = scan_repo(project.path, registry.db_path(project.id),
                                    use_llm=cfg.llm_enabled)
                registry.update_summary(project.id, summary)
                index_cache.pop(project.id, None)
                return summary
            finally:
                lock_for(project.id).release()

        # -- dashboard data --------------------------------------------------

        def _overview(self, project, store):
            active = store.list_findings(statuses=ACTIVE)
            score = health.health_score(active)
            snaps = store.list_snapshots(30)
            latest = snaps[-1] if snaps else None
            trend = [{"scanned_at": s["scanned_at"], "score": s["score"],
                      "total_open": s["total_open"]} for s in snaps]
            stats = latest["stats"] if latest else {}
            self._json({
                "project": project.to_dict(),
                "repo": project.name,
                "repo_root": project.path,
                "last_scan": store.get_meta("last_scan"),
                "commit": latest["commit_sha"] if latest else None,
                "score": score,
                "grade": health.grade(score),
                "total_open": len(active),
                "delta": {
                    "new": latest["new_count"] if latest else 0,
                    "resolved": latest["resolved_count"] if latest else 0,
                },
                "aggregate": health.aggregate(active),
                "trend": trend,
                "scan_stats": stats,
            })

        def _findings(self, store, qs):
            statuses = tuple(qs.get("status", [",".join(ACTIVE)])[0].split(","))
            rows = store.list_findings(
                statuses=statuses,
                rule_id=qs.get("rule", [None])[0],
                severity=qs.get("severity", [None])[0],
                confidence=qs.get("confidence", [None])[0],
                file=qs.get("file", [None])[0],
                q=qs.get("q", [None])[0],
            )
            self._json({"findings": rows, "count": len(rows)})

        def _snapshots(self, store):
            self._json({"snapshots": store.list_snapshots(50)})

        def _code(self, project, qs):
            rel = qs.get("file", [None])[0]
            if not rel:
                self._json({"error": "file required"}, status=400); return
            target = _safe_resolve(project.path, rel)
            if not target or not os.path.isfile(target):
                self._json({"error": "not found"}, status=404); return
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            around = qs.get("around", [None])[0]
            if around is not None:
                center = int(around)
                start = max(1, center - CONTEXT_LINES)
                end = min(len(lines), center + CONTEXT_LINES)
            else:
                start = int(qs.get("start", ["1"])[0])
                end = int(qs.get("end", [str(len(lines))])[0])
            self._json({"file": rel, "start": start, "end": end,
                        "lines": [{"n": i, "text": lines[i - 1]} for i in range(start, end + 1)]})

        # -- rules -----------------------------------------------------------

        def _rules_list(self, store):
            from ..core.rules import KINDS
            specs = store.list_rules()
            for s in specs:
                meta = KINDS.get(s["kind"])
                s["kind_label"] = meta.label if meta else s["kind"]
                s["creatable_kind"] = meta.creatable if meta else False
            self._json({"rules": specs})

        def _spec_from_payload(self, payload: dict, *, rule_id: str | None, builtin: bool):
            from ..core.rules import KINDS, RuleSpec, custom_spec_id, describe
            kind = payload.get("kind", "")
            if kind not in KINDS:
                raise ValueError("未知指标类型")
            meta = KINDS[kind]
            params = dict(meta.default_params)
            params.update(payload.get("params") or {})
            severity = payload.get("severity") or meta.default_severity
            if severity not in ("high", "medium", "low"):
                severity = meta.default_severity
            spec = RuleSpec(
                id=rule_id or custom_spec_id(kind),
                name=(payload.get("name") or meta.label).strip()[:64],
                kind=kind,
                severity=severity,
                description=payload.get("description") or describe(kind, params),
                params=params,
                suggestion=payload.get("suggestion") or meta.suggestion,
                enabled=bool(payload.get("enabled", True)),
                builtin=builtin,
                needs_review=meta.needs_review,
            )
            return spec

        def _rules_post(self, project, sub_parts, payload):
            from ..core.llm import LLMClient
            from ..core.rules import KINDS, RuleSpec, run_rules
            from ..config import Config
            store = Storage(registry.db_path(project.id))
            try:
                store.seed_rules()
                # generate DSL via LLM (not persisted)
                if sub_parts == ["generate"]:
                    cfg = Config.load()
                    if not cfg.llm_enabled:
                        self._json({"error": "请先配置模型"}, status=400); return
                    obj = LLMClient(cfg).generate_rule(payload.get("description", ""))
                    if not obj or obj.get("kind") not in KINDS:
                        self._json({"error": "模型未能生成有效指标，请换个描述或手动创建"}, status=422); return
                    kind = obj["kind"]
                    meta = KINDS[kind]
                    params = dict(meta.default_params)
                    params.update(obj.get("params") or {})
                    self._json({"ok": True, "rule": {
                        "name": obj.get("name", meta.label),
                        "kind": kind, "severity": obj.get("severity", meta.default_severity),
                        "params": params,
                        "description": obj.get("description", ""),
                    }})
                    return
                # dry-run a spec against the current index (not persisted)
                if sub_parts == ["preview"]:
                    try:
                        spec = self._spec_from_payload(
                            {**payload, "name": payload.get("name", "preview")},
                            rule_id="preview", builtin=False)
                    except ValueError as e:
                        self._json({"error": str(e)}, status=400); return
                    idx = get_index(project.id, project)
                    hits = run_rules(idx, [spec])
                    samples = [{"file": f.file, "line": f.line, "symbol": f.symbol,
                                "message": f.message} for f in hits[:8]]
                    self._json({"ok": True, "count": len(hits), "samples": samples,
                                "description": spec.description})
                    return
                # create
                if sub_parts == [] or sub_parts == [""]:
                    try:
                        spec = self._spec_from_payload(payload, rule_id=None, builtin=False)
                    except ValueError as e:
                        self._json({"error": str(e)}, status=400); return
                    store.upsert_rule(spec)
                    self._json({"ok": True, "rule": spec.to_dict()})
                    return
                # reset built-in: rules/<rid>/reset
                if len(sub_parts) == 2 and sub_parts[1] == "reset":
                    ok = store.reset_rule(sub_parts[0])
                    self._json({"ok": ok}, status=200 if ok else 404)
                    return
                self.send_error(404)
            finally:
                store.close()

        def _rule_update(self, pid: str, rid: str, payload: dict):
            project = registry.get(pid)
            if project is None:
                self._json({"error": "project not found"}, status=404); return
            store = Storage(registry.db_path(pid))
            try:
                store.seed_rules()
                rows = {r["id"]: r for r in store.list_rules()}
                current = rows.get(rid)
                if current is None:
                    self._json({"error": "指标不存在"}, status=404); return
                merged = {
                    "name": payload.get("name", current["name"]),
                    "kind": current["kind"],
                    "severity": payload.get("severity", current["severity"]),
                    "description": payload.get("description", current["description"]),
                    "params": {**current["params"], **(payload.get("params") or {})},
                    "suggestion": payload.get("suggestion", current["suggestion"]),
                    "enabled": payload.get("enabled", current["enabled"]),
                }
                spec = _spec_from_row(current, merged)
                store.upsert_rule(spec)
                self._json({"ok": True, "rule": spec.to_dict()})
            finally:
                store.close()

        def _rule_delete(self, pid: str, rid: str):
            project = registry.get(pid)
            if project is None:
                self._json({"error": "project not found"}, status=404); return
            store = Storage(registry.db_path(pid))
            try:
                ok = store.delete_rule(rid)
                self._json({"ok": ok}, status=200 if ok else 400)
            finally:
                store.close()

    return Handler


def _spec_from_row(current: dict, merged: dict):
    from ..core.rules import RuleSpec
    return RuleSpec(
        id=current["id"], name=merged["name"], kind=merged["kind"],
        severity=merged["severity"], description=merged["description"],
        params=merged["params"], suggestion=merged["suggestion"],
        enabled=merged["enabled"], builtin=current["builtin"],
        needs_review=current["needs_review"],
    )


def serve(port: int = 8787, open_browser: bool = True,
          initial_path: str | None = None) -> None:
    from ..harness.projects import ProjectRegistry
    registry = ProjectRegistry()
    initial_pid = None
    if initial_path:
        abspath = os.path.abspath(os.path.expanduser(initial_path))
        if os.path.isdir(abspath):
            project = registry.get_by_path(abspath) or registry.add("", abspath)
            initial_pid = project.id
    handler = build_handler(registry, initial_pid)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}"
    print("Debtscope 债镜 — local technical-debt dashboard")
    print(f"  {url}   (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
