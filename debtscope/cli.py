"""Debtscope command line interface.

    debtscope scan ./myrepo            # index, analyze, store a snapshot
    debtscope serve ./myrepo           # open the web dashboard (scans if needed)
    debtscope rules                    # list built-in rules
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .core.rules import BUILTIN_SPECS, KINDS
from .harness.config_store import PROVIDERS

DEFAULT_DB = os.path.join(".debtscope", "debtscope.db")
RULE_NAMES = {s.id: s.name for s in BUILTIN_SPECS}
RULE_SEV = {s.id: s.severity for s in BUILTIN_SPECS}


def _cmd_scan(args: argparse.Namespace) -> int:
    from .config import Config
    from .core.scanner import scan_repo

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    if not args.no_llm and not Config.load().llm_enabled:
        print("error: no model configured. Run `debtscope config` first "
              "(or pass --no-llm for offline/static-only mode, e.g. CI).", file=sys.stderr)
        return 2
    db = os.path.abspath(args.db)
    print(f"Debtscope v{__version__} — scanning {root}")
    summary = scan_repo(root, db, use_llm=not args.no_llm)
    _print_summary(summary)
    print("\nDashboard:  debtscope serve")
    return 0


def _print_summary(s: dict) -> None:
    rev = s["review"]
    print(f"  files {s['files']} | LOC {s['loc']} | symbols {s['symbols']}"
          + (f" | commit {s['commit']}" if s["commit"] else ""))
    if s["parse_errors"]:
        print(f"  ! {len(s['parse_errors'])} file(s) failed to parse")
    if rev["llm_enabled"]:
        mode = f"LLM {rev['llm_model']} (reviewed {rev['llm_reviewed']}," \
               f" dead {rev['llm_dead']}, entry suppressed {rev['llm_entry_suppressed']})"
    else:
        mode = "static-only (--no-llm; run `debtscope config` to enable AI review)"
    print(f"  L2 candidates {s['candidates']} -> kept {s['kept']} | {mode}")
    if rev.get("degraded"):
        reason = rev.get("llm_error") or "模型未返回可用判定"
        print(f"  ! AI 精判降级为静态模式：{reason}")
    c = s["reconcile"]
    print(f"  this scan: +{c['new']} new, {c['resolved']} resolved, "
          f"{c['reopened']} reopened, {c['existing']} unchanged"
          + (f", {c['suppressed_fp']} hidden as false-positive" if c["suppressed_fp"] else ""))
    print(f"  HEALTH SCORE: {s['score']}/100 (grade {s['grade']})")
    agg = s["aggregate"]["by_rule"]
    if agg:
        print("  by rule:")
        for rule_id, n in agg.items():
            name = RULE_NAMES.get(rule_id, rule_id)
            sev = RULE_SEV.get(rule_id, "?")
            print(f"    {name:<22} {n:>4}  [{sev}]")


def _cmd_serve(args: argparse.Namespace) -> int:
    from .web.server import serve
    initial = os.path.abspath(args.path) if args.path else None
    if initial and not os.path.isdir(initial):
        print(f"error: not a directory: {initial}", file=sys.stderr)
        return 2
    serve(port=args.port, open_browser=not args.no_browser, initial_path=initial)
    return 0


def _cmd_rules(_args: argparse.Namespace) -> int:
    print(f"{'KIND':<26} {'SEV':<7} DESCRIPTION")
    seen = set()
    for spec in BUILTIN_SPECS:
        seen.add(spec.kind)
        print(f"{spec.id:<26} {spec.severity:<7} {spec.description}")
    print("\nCreatable metric kinds (dashboard → 指标管理 → 新增指标):")
    for kind, meta in KINDS.items():
        if meta.creatable and kind not in seen:
            print(f"  {kind:<24} {meta.default_severity:<7} {meta.label}")
    return 0


def _prompt(text: str, default: str = "", secret: bool = False) -> str:
    try:
        if secret:
            import getpass
            value = getpass.getpass(text)
        else:
            value = input(text)
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return value.strip()


def _cmd_config(args: argparse.Namespace) -> int:
    from .config import Config
    from .core.llm import LLMClient
    from .harness.config_store import (
        CONFIG_PATH, PROVIDERS, FileConfig, load_config_file,
        masked_key, save_config_file,
    )

    if args.show:
        cfg = Config.load()
        print(f"config file : {CONFIG_PATH} ({'exists' if os.path.isfile(CONFIG_PATH) else 'not created'})")
        print(f"provider    : {cfg.provider}")
        print(f"api_base    : {cfg.api_base}")
        print(f"api_key     : {masked_key(cfg.api_key) or '(none)'}")
        print(f"model       : {cfg.model or '(none)'}")
        print(f"llm enabled : {cfg.llm_enabled}")
        return 0

    current = load_config_file()

    if args.provider:
        preset = PROVIDERS.get(args.provider, PROVIDERS["custom"])
        fc = FileConfig(
            provider=args.provider,
            api_base=(args.api_base or preset["api_base"]).rstrip("/"),
            api_key=args.api_key or current.api_key,
            model=args.model or preset["model"],
        )
    else:
        keys = list(PROVIDERS)
        print("Configure the model endpoint for AI-powered review and rule generation.\n")
        for i, key in enumerate(keys, 1):
            print(f"  {i}) {PROVIDERS[key]['label']}  [{key}]")
        if current.provider in PROVIDERS:
            default_provider = current.provider
        elif not os.path.isfile(CONFIG_PATH):
            default_provider = "doubao"  # first-run default: Doubao Seed-Evolving
        else:
            default_provider = "custom"
        raw = _prompt(f"\nChoose provider [1-{len(keys)}] (default {default_provider}): ")
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            provider = keys[int(raw) - 1]
        elif not raw:
            provider = default_provider
        else:
            print("invalid choice"); return 2
        preset = PROVIDERS[provider]

        default_base = current.api_base or preset["api_base"]
        api_base = _prompt(f"API base [{default_base}]: ", default_base) or default_base
        default_model = current.model or preset["model"]
        model = _prompt(f"Model [{default_model or 'required'}]: ", default_model) or default_model
        api_key = current.api_key
        if provider != "ollama":
            key_hint = f" (enter to keep {masked_key(api_key)})" if api_key else ""
            entered = _prompt(f"API key{key_hint} (hidden): ", secret=True)
            if entered:
                api_key = entered
            if not api_key:
                print("  ! no API key configured; set one or use environment variables")
        fc = FileConfig(provider=provider, api_base=api_base.rstrip("/"),
                        api_key=api_key, model=model)

    save_config_file(fc)
    print(f"\nSaved to {CONFIG_PATH} (chmod 600)")

    if not args.no_test:
        cfg = Config.load()
        if cfg.llm_enabled:
            print("Testing connection ...")
            client = LLMClient(cfg)
            ok, msg = client.ping()
            print(("  OK  " if ok else "  FAIL ") + msg)
            if not ok:
                return 1
        else:
            print("No usable credential/endpoint yet; run `debtscope serve` to configure in the UI.")
    print("\nNext: debtscope serve         (choose a project and initialize monitoring)")
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    import sys as _sys
    from .config import Config
    from .core.llm import LLMClient
    from .harness.config_store import CONFIG_PATH, masked_key

    cfg = Config.load()
    print("Debtscope doctor")
    print(f"  python      : {_sys.version.split()[0]}")
    print(f"  config file : {CONFIG_PATH} ({'exists' if os.path.isfile(CONFIG_PATH) else 'not created'})")
    print(f"  provider    : {cfg.provider}")
    print(f"  api_base    : {cfg.api_base}")
    print(f"  api_key     : {masked_key(cfg.api_key) or '(none)'}")
    print(f"  model       : {cfg.model or '(none)'}")
    if cfg.llm_enabled:
        print("  connection  : testing ...")
        ok, msg = LLMClient(cfg).ping()
        print(f"                {'OK  ' if ok else 'FAIL'} {msg}")
        return 0 if ok else 1
    print("  connection  : skipped (static-only mode; run `debtscope config` to enable AI review)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="debtscope",
        description="AI-powered technical debt dashboard (债镜)",
    )
    p.add_argument("--version", action="version", version=f"debtscope {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="scan a repository and record a snapshot")
    sp.add_argument("path", help="repository root")
    sp.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default: {DEFAULT_DB})")
    sp.add_argument("--no-llm", action="store_true", help="static-only mode: no model calls (for CI / offline use)")
    sp.set_defaults(func=_cmd_scan)

    sv = sub.add_parser("serve", help="start the web dashboard (onboarding + projects)")
    sv.add_argument("path", nargs="?", help="optional repository root to register and open")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--no-browser", action="store_true")
    sv.set_defaults(func=_cmd_serve)

    rl = sub.add_parser("rules", help="list built-in rules")
    rl.set_defaults(func=_cmd_rules)

    cf = sub.add_parser("config", help="configure model endpoint (interactive wizard)")
    cf.add_argument("--show", action="store_true", help="show effective configuration")
    cf.add_argument("--provider", choices=list(PROVIDERS),
                    help="provider preset (default first-run: doubao)")
    cf.add_argument("--api-base")
    cf.add_argument("--api-key")
    cf.add_argument("--model")
    cf.add_argument("--no-test", action="store_true")
    cf.set_defaults(func=_cmd_config)

    doc = sub.add_parser("doctor", help="check configuration and model connectivity")
    doc.set_defaults(func=_cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
