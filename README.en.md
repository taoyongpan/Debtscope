# 🔭 Debtscope

<p>
  <b>English</b> · <a href="README.md">简体中文</a>
</p>

[![CI](https://github.com/taoyongpan/Debtscope/actions/workflows/ci.yml/badge.svg)](https://github.com/taoyongpan/Debtscope/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**AI-powered technical debt dashboard for your codebase.**
Connect a model, pick a project, define metrics in plain language — then watch debt trend with every commit.

> Debtscope answers three questions teams cannot today: **how much** debt do we have, **where** exactly is it, and are we getting **better or worse**?

- **Zero third-party dependencies** — pure Python standard library; just `git clone` and run, no database / Docker / server
- **Deterministic core + agent shell** — AST/call-graph rules do the whole-repo sweep; the LLM only refines 1–5% of candidates: cheap, fast, verifiable
- **Metrics as data** — disable built-in rules, tune thresholds/severity, add 8 kinds of custom metrics in the UI, or **describe one in a sentence and let AI compile it**, with a dry-run preview before saving
- **Multi-project** — switch between monitored local repositories in one dashboard; data stays per-project and on your machine
- **Config-first onboarding** — first run is a 3-step guide (connect model → pick project → initialize monitoring), defaulting to Doubao Seed-Evolving with 16 built-in OpenAI-compatible presets

---

## Why Debtscope

| | SonarQube | AI coding assistants | Manual review | **Debtscope** |
|---|---|---|---|---|
| Whole-repo inventory | ✅ | ❌ (edit-time only) | ❌ | ✅ |
| Natural-language custom metrics | ❌ write plugins | partial | ❌ | ✅ AI → rule, with dry-run preview |
| Custom rules editable in the UI | ❌ | ❌ | ❌ | ✅ create / edit / disable / delete |
| Semantic dead-vs-entry-point judgment | ❌ syntax rules | ✅ but no repo-wide view | ✅ not repeatable | ✅ hybrid |
| Iteration-over-iteration trend narrative | weak | ❌ | ❌ | ✅ first-class |
| Multi-project dashboard | ❌ heavyweight server | ❌ | ❌ | ✅ local registry |
| Traceable evidence + confidence levels | partial | weak | ✅ | ✅ |
| Zero-dependency, runs locally | ❌ | ✅ | — | ✅ stdlib only |

## Screenshots

<p align="center">
  <img src="docs/images/onboarding.png" alt="Connect a model" width="920">
</p>

*① First launch is a 3-step guide: connect a model → pick a project → initialize monitoring. Defaults to **Doubao / Volcano Ark with the unified `doubao-seed-evolving` model** (Agent & Coding optimized, always the latest release); 15 other presets — DeepSeek, Qwen, GLM, Kimi, Hunyuan, ERNIE, MiniMax, MiMo, SiliconFlow, OpenAI, Gemini, Grok, Mistral, Ollama, custom — are one dropdown away, with a live connection test. Local endpoints need no key.*

<p align="center">
  <img src="docs/images/project-setup.png" alt="Initialize project monitoring" width="920">
</p>

*② Give a local repository path; Debtscope builds the index and runs the first scan (initializes monitoring). Monitored projects appear as cards and stay in the top-bar switcher.*

<p align="center">
  <img src="docs/images/dashboard-v2.png" alt="Debtscope dashboard" width="920">
</p>

*③ Health score ring, this-scan delta (new vs eliminated), severity donut, rule-type distribution, score trend and a filterable findings table — with project switcher, metric manager and model badge in the top bar.*

<p align="center">
  <img src="docs/images/rules-manager.png" alt="Metric manager" width="920">
</p>

*④ Metric manager: toggle built-in metrics, tune thresholds and severity, or add team-specific ones. Built-in metrics reset to defaults; custom metrics can be deleted.*

<p align="center">
  <img src="docs/images/rule-editor.png" alt="AI-assisted metric editor" width="760">
</p>

*⑤ Describe a metric in one sentence — “ban print debugging”, “functions must not exceed 80 lines”, “class names must be PascalCase” — AI compiles it into a parameterized rule. **Dry-run it against the current index before saving** (hit count + sample locations); the scan reruns automatically afterwards.*

<p align="center">
  <img src="docs/images/finding-detail.png" alt="Finding detail with code evidence" width="920">
</p>

*Click any finding to expand code context with the offending line highlighted, review verdict, fix suggestion and one-click triage (confirm / false-positive / wontfix). Deep-linkable via `#finding-<id>`.*

## How it works

Debtscope does **not** feed your whole repository to an LLM. Deterministic work stays deterministic; the model only judges a small candidate set — which keeps scans cheap, fast, and trustworthy.

```mermaid
flowchart TB
    UI["Web Dashboard · onboarding · projects · metrics · overview · evidence"]
    L5["L5 Render · chart selection & layout"]
    L4["L4 Aggregate · counts · severity · snapshot diff"]
    L3["L3 Review · LLM semantic verdict on candidates only · NL→rule generation"]
    L2["L2 Rules · data-driven AST/call-graph checks produce candidates"]
    L1["L1 Index · symbols · args · nesting · call graph · references · git history"]
    REPO[("Git repository")]
    REPO --> L1 --> L2 --> L3 --> L4 --> L5 --> UI
    DB[("SQLite · one ledger per project · rules & snapshots")]
    L1 -.-> DB
    L4 -.-> DB
```

**Anti-hallucination defenses**

1. **Structure first** — no finding without structural evidence from the index/call graph.
2. **Three-part evidence** — every finding links to file, line, code snippet and reference chain.
3. **Confidence levels** — high / medium (needs a human glance) / low (folded, excluded from score).
4. **Feedback loop** — marking a false positive suppresses it on every future scan.
5. **AI-generated rules are never trusted blindly** — they compile into the same deterministic checkers as built-in rules, and you must dry-run them on your code before saving.
6. **Model failures never block a scan** — on network/auth/quota/model errors the scan degrades to static-only and **surfaces the reason explicitly** in the CLI, dashboard badge and scan summary; it never fails silently.

Dead-code findings are honestly worded as *“no call/reference found by static analysis, please confirm”* — reflection, dynamic dispatch and framework callbacks can never be fully ruled out statically.

### Deterministic core, agentic shell

The agent shell (`debtscope.harness`) follows the **Agent = Model + Harness** paradigm: pluggable model adapters, a project registry, tool contracts, and traceable runs — but the built-in rules stay on the deterministic fast path (fast, free, zero-hallucination). Model calls are reserved for what only a model can do: semantic dead-code review and turning natural language into parameterized rules. See **[docs/harness-architecture.md](docs/harness-architecture.md)** for the full blueprint (micro-kernel, tool registry, ReAct loop, session traces, plugin strategy, security model).

## Quickstart

Requires Python 3.10+. **Zero third-party dependencies** (standard library only).

```bash
git clone https://github.com/taoyongpan/Debtscope.git
cd Debtscope

# start the web app — the 3-step guide walks you through model → project → first scan
python -m debtscope serve
```

1. **Connect a model** — the guide defaults to Doubao / Volcano Ark with `doubao-seed-evolving`; paste your Ark API key (or switch to DeepSeek / OpenAI / a local Ollama endpoint, which needs no key). The guide tests the connection before continuing.
2. **Pick a project** — enter an absolute path to a local repository to initialize monitoring (the first scan runs immediately).
3. **Dashboard** — switch between monitored projects from the top bar any time; **＋ Add project…** adds more.

> If the default port 8787 is busy, the server walks forward to the next free port and opens that URL.

Installing as a command:

```bash
pip install -e .
debtscope serve
```

CLI-only / CI usage:

```bash
debtscope config                          # one-time interactive model setup
debtscope scan /path/to/repo              # requires a configured model (AI review on)
debtscope scan /path/to/repo --no-llm     # static-only fallback for CI / offline
debtscope serve [path]                    # web app, optionally registering & focusing a project
```

> A model is required by design — Debtscope is an AI-first product and refuses to silently present unreviewed static results as the default experience. `--no-llm` remains an explicit offline/CI escape hatch.

### Model configuration

```bash
debtscope config        # guided wizard, saves to ~/.debtscope/config.json (chmod 600)
debtscope doctor        # verify config and connectivity any time
debtscope config --show # show the effective config (key masked)
```

16 built-in presets, all speaking the OpenAI Chat Completions protocol. The guide defaults to **Doubao / Volcano Ark**; pick any other from the dropdown — base URL and model are pre-filled and editable.

| Provider | API base | Default model |
|---|---|---|
| **Doubao / Volcano Ark** (default) | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-seed-evolving` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Alibaba Qwen / Bailian | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen3-coder-plus` |
| Zhipu GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-5.3` |
| Kimi / Moonshot | `https://api.moonshot.cn/v1` | `kimi-k2.6` |
| Tencent Hunyuan | `https://api.hunyuan.cloud.tencent.com/v1` | `hunyuan-turbo` |
| Baidu Qianfan / ERNIE | `https://qianfan.baidubce.com/v2` | `ernie-4.5-turbo-128k` |
| MiniMax | `https://api.minimax.chat/v1` | `MiniMax-M2.5` |
| Xiaomi MiMo | `https://api.xiaomimimo.com/v1` | `mimo-v2.5-pro` |
| SiliconFlow (aggregator) | `https://api.siliconflow.cn/v1` | `Qwen/Qwen3-Coder-480B-A35B-Instruct` |
| OpenAI | `https://api.openai.com/v1` | `gpt-5.5` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-3.5-flash` |
| xAI Grok | `https://api.x.ai/v1` | `grok-4.6` |
| Mistral | `https://api.mistral.ai/v1` | `mistral-large-latest` |
| Ollama (local, no key) | `http://127.0.0.1:11434/v1` | `qwen2.5-coder:7b` |
| Custom | any OpenAI-compatible endpoint (gateway, vLLM, LM Studio…) | — |

Model IDs are common current releases as of 2026-09 — type any newer/dated model name (or an Ark `ep-xxxx` endpoint id) into the model field. Local endpoints (`127.0.0.1` / `localhost`) work without a key.

Environment variables override the file (handy for CI; `OPENAI_API_KEY` / `OPENAI_API_BASE` are also honored):

```bash
export DEBTSCOPE_API_BASE="https://ark.cn-beijing.volces.com/api/v3"
export DEBTSCOPE_API_KEY="your-ark-api-key"
export DEBTSCOPE_MODEL="doubao-seed-evolving"
```

In the web app, click the model badge in the top bar to edit configuration at any time; if the latest scan's AI review failed, the badge switches to a “degraded” warning with the reason.

## Metrics & rules

### 7 built-in metrics (Python)

| Rule | Severity | What it catches |
|---|---|---|
| `unused_function` | medium | functions/methods with no call or reference anywhere in the repo (framework decorators, dunders, tests, abstract methods excluded; LLM-refined when configured) |
| `swallowed_exception` | medium | bare `except`, or `except: pass` that silently swallows errors |
| `mutable_default_argument` | medium | `def f(x=[])` and friends — shared mutable defaults |
| `open_without_context` | medium | `open()` outside a `with` block, leaking handles on error paths |
| `long_function` | low | functions over 50 lines |
| `todo_accumulation` | low | files with 5+ TODO/FIXME comments |
| `duplicate_function` | low | structurally identical function bodies (copy-paste), robust to renamed variables |

### 8 metric types you can add yourself

| Kind | Parameters | Example |
|---|---|---|
| `function_too_long` | max lines | functions ≤ 80 lines |
| `file_too_long` | max lines | files ≤ 500 lines |
| `too_many_args` | max args | at most 5 parameters |
| `nested_too_deep` | max depth | nesting ≤ 4 levels |
| `todo_accumulation` | max count | at most 3 TODOs per file |
| `duplicate_function` | min lines / stmts | catch shorter copy-paste |
| `forbidden_call` | comma-separated names | ban `print,eval,os.system` |
| `name_convention` | target + regex + message | PascalCase classes, snake_case functions |

Every custom metric is a row in the same rule engine as built-in ones — no special execution path. The **AI assist** box compiles a sentence into `{kind, severity, params}`; you then **dry-run preview** it against the current index before saving. Invalid regexes and empty call lists are rejected at preview time.

Inspect from the CLI: `debtscope rules` lists built-in metrics and creatable kinds.

## Multi-project storage

```
~/.debtscope/
├── config.json            # model config (chmod 600)
├── projects.json          # monitored project registry (atomic writes)
└── data/<project-id>.db   # one SQLite ledger per project (rules, findings, snapshots)
```

Project IDs are content-derived from the absolute path, so registering the same path twice is idempotent. Root and home directories are refused to prevent whole-disk scans. Nothing is uploaded anywhere.

## CLI

```bash
debtscope serve [path]    # web dashboard; optionally register & focus a project
      --port <port>       # preferred starting port (auto-walks when busy)
      --no-browser        # don't open a browser
debtscope scan <path>     # index, analyze, reconcile findings, record a snapshot
      --no-llm            # static-only mode, no model calls (CI / offline)
      --db <path>         # override ledger path
debtscope config          # interactive model/endpoint wizard (also --show, --provider …)
debtscope doctor          # check config and model connectivity
debtscope rules           # list built-in metrics and creatable kinds
```

## Roadmap

- **v0.1** ✅ Python AST backend, 7 built-in metrics, SQLite ledger, web dashboard
- **v0.2** ✅ model config wizard (CLI + web), provider presets, connectivity test, `debtscope.harness` package
- **v0.3** ✅ config-first 3-step onboarding, multi-project registry, data-driven rule engine, UI metric manager (create/edit/disable/delete/reset), 8 creatable metric kinds, AI rule generation with dry-run preview, 16 provider presets
- **v0.3.1** ✅ local-server hardening (static-dir traversal blocked, Host allow-list), unified AI review JSON protocol with tolerant parsing, visible degradation reasons, port self-healing, 40 offline tests, CI
- **v0.4** — micro-kernel + tool registry + multi-step ReAct loop (evidence-fetching review, conversational metric tuning), run/trace JSONL & traces page
- **v0.5** — language-backend plugin seam + tree-sitter (Java / Go / JS/TS), token-cost dashboard
- **v0.6** — CI headless mode (`--ci`, SARIF output, quality-gate exit codes); repo groups & aggregate rollups
- **later** — runtime-coverage cross-check for dead code, autofix with diff review, optional plugin bundle

See [docs/design-v2.md](docs/design-v2.md) for the product/technical design and
[docs/harness-architecture.md](docs/harness-architecture.md) for the harness blueprint.

## Privacy

Everything runs on your machine. Source code is read locally; the only network
calls are LLM requests to the endpoint you configure. Point it at an internal
gateway and no code ever leaves your network. Keys are stored only in
`~/.debtscope/config.json` with `chmod 600`.

## Contributing

Issues and PRs are welcome! Development only needs Python 3.10+, and all 40
tests run fully offline without any API key:

```bash
python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
