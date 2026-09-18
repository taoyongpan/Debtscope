# Debtscope Harness 架构蓝图

> 借鉴 DeepSeek Harness（dsh，2026-08 开源，MIT，基于 Cordis 的通用 Agent 运行时）的工程范式，
> 但坚持一条自己的原则：**确定性管线是内核，Agent 是外壳（deterministic core, agentic shell）**。

## 1. 我们向 dsh 借什么，不借什么

dsh 的公式是 **Agent = Model + Harness**：模型负责推理，Harness 负责模型之外的一切——环境交互、工具契约、上下文流、沙箱、可追溯运行记录。它的三个设计精髓：

1. **Everything is a plugin**：模型、工具、skills、sessions、sandboxes、storage、loop、scheduling、UI 全部是可替换插件（Cordis 微内核 + services/events）。
2. **Every run is traceable**：每次 agent 运行有完整 session trace（步骤、工具调用、token、耗时）。
3. **双形态交付**：headless 一次性执行（CI/脚本）+ web workbench（交互）。

| dsh 做法 | Debtscope 的取舍 |
|---|---|
| 通用 Agent 底座，对标 Claude Code/Codex | **不做通用底座**，只做代码技术债垂直场景；不 fork dsh，避免背上一个生态的复杂度 |
| 一切皆插件，Cordis 内核 | 借"微内核 + 注册表 + 事件"思想，用零依赖的轻量 registry 落地（Python 生态无需引入 Cordis/Node 栈） |
| Agent loop 驱动所有行为 | **内置规则不走 loop**：AST 索引/规则/聚合保持确定性（快、免费、零幻觉）；loop 只承担自然语言建规则与语义精判 |
| 模型适配器可热替换 | 直接采用：provider 预设 + OpenAI 兼容缝 + 配置向导（v0.2 已落地） |
| Session JSONL trace | 采用：每次扫描/agent 运行落 trace，成为"可信"与排障的底座，未来在看板可视化 |
| 沙箱 + 权限分级 | 采用并收敛：v1 工具**只读**且限定仓库根；未来自动修复类写操作才引入授权 |

**为什么不让 Agent 直接读全仓下判断？** 那正是 v1 设计文档里被否决的"全 LLM 扫描"：成本不可控、上下文装不下、幻觉无法收敛。Harness 化不是把确定性管线推翻，而是给它套一层会用工具的外壳——**Agent 拿到的每一条结论都必须来自注册工具返回的证据**，低幻觉从"prompt 要求"升级为"架构强制"。

## 2. 目标架构

```
┌────────────────────────────────────────────────────────────┐
│  形态层  debtscope scan (headless)  ·  debtscope serve (web) │
├────────────────────────────────────────────────────────────┤
│  Harness 外壳 (debtscope.harness)                            │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────┐  │
│  │ micro-kernel │  │ agent loop    │  │ session/trace    │  │
│  │ registry+bus │  │ plan→act→obs  │  │ JSONL runs/      │  │
│  │ (plugins)    │  │               │  │ token/timing     │  │
│  └──────┬───────┘  └───────┬───────┘  └──────────────────┘  │
│         │ tool contract (JSON schema, read-only by default) │
│  ┌──────▼───────────────────────────────────────────────┐   │
│  │ Tools（把确定性能力暴露给 Agent）                       │   │
│  │ codegraph.query · codegraph.callers · read_file · grep │   │
│  │ rules.list · rules.run · findings.save · review.submit │   │
│  │ nl_to_rule (DSL 生成 + 试跑 + 回显)                    │   │
│  └──────┬───────────────────────────────────────────────┘   │
│  ┌──────▼───────────────────────────────────────────────┐   │
│  │ Model adapter seam  (deepseek/openai/doubao/ollama/…)  │  │
│  └──────────────────────────────────────────────────────┘   │
├────────────────────────────────────────────────────────────┤
│  确定性内核 (debtscope.core，v0.1–v0.3 已落地)                │
│  indexer (AST/调用图) → 数据驱动 rules (RuleSpec/KINDS) →    │
│  LLM 精判 (未配置模型则拒绝默认扫描，--no-llm 显式兜底) →     │
│  per-project storage (SQLite 台账/规则/快照) → aggregate →    │
│  render；harness.projects 多项目注册表                        │
└────────────────────────────────────────────────────────────┘
```

## 3. 工具契约（Tool Contract）

Agent 不直接 import 内核函数，只通过声明了 JSON Schema 的工具拿证据。v0.4.1 已注册 8 个只读工具（`debtscope/harness/tools.py`）：

| 工具 | 入参（摘要） | 返回 | 只读 |
|---|---|---|---|
| `codegraph.symbols` | prefix、kind、file、limit | 符号列表（key、位置、装饰器、行数、参数数） | ✅ |
| `codegraph.callers` | symbol（短名或 Class.method） | 全部静态调用方与行号（空数组 = 废弃核心证据） | ✅ |
| `codegraph.callees` | symbol | 被调用方与 DB/HTTP 汇点标记 | ✅ |
| `read_file` | file、start、end | 带行号代码片段（realpath 限定仓库根，越界即拒） | ✅ |
| `grep` | pattern（正则）、glob、limit | 命中文件/行号/文本（自动跳过 .git/__pycache__ 等） | ✅ |
| `rules.catalog` | — | 内置规则、可创建指标类型（kind/参数/默认严重度） | ✅ |
| `rule.preview` | kind、params、severity | 试跑命中数量与样例（不落库，参数按 schema 强类型转换） | ✅ |
| `endpoint.chain` | method、path | 接口静态调用链：节点、DB/HTTP 汇点、影响面、节点技术债 | ✅ |

协议说明：考虑到要兼容 16+ 家 OpenAI 兼容厂商（含不支持原生 function-calling 的本地小模型），Agent 与模型之间采用**文本 JSON 协议**（每轮输出 `{"thought","action","args"}` 或 `{"thought","final"}`），而非某一家的 tools API。

关键约束（在 loop 与 kernel 中强制，而非只写进 prompt）：
- 最终结论的 `evidence` 必须引用**本轮真实调用成功**的工具名，否则 loop 打回补证据（最多 2 次纠正，仍不合规则返回 `incomplete`，该候选保持中置信，绝不写入高置信结论）。
- 判 `dead`（建议删除）前必须调用过 `codegraph.callers` 或 `grep`——只读过代码片段不构成删除证据，由 `dead_code_validator` 架构级拦截。
- v1 全部工具只读且根目录限定；写工具（如未来的 `rule.install`）在 `allow_writes=False` 时由 kernel 直接拒绝，且必须配合产品侧的"试跑回显确认"，防止自然语言误解直接污染台账。

## 4. Agent Loop 的两个使用场景

默认 ReAct（think → act → observe → … → final），但仅在两处启用：

1. **自然语言自定义规则**
   完整形态：`理解需求 → rules.catalog 参考 → 生成 DSL → rule.preview 试跑 →（必要时读样例代码自我修正）→ 回显给用户 → rule.install`。
   **v0.3 已落地单步形态**：一次 LLM 调用把自然语言编译为参数化规则（`{kind, severity, params}`，kind 取自 12 类确定性检测器注册表），必须先对当前索引试跑预览（命中数 + 样例）才能保存。多轮 ReAct 自我修正（读样例代码、调整阈值后重试）留待 v0.4。
   产出物始终是**确定性的规则规格**，之后每次扫描都不再调用模型——一次 agentic，长期确定性。
2. **废弃代码语义精判（v0.4.1 已 harness 化，两阶段）**
   阶段一：粗筛候选 → 批量 LLM 快判 dead/entry/uncertain（便宜、一次调用）。
   阶段二：仅对快判为 `uncertain` 的候选（每次扫描上限 5 个、每个最多 5 步）启动 Agent，让它主动 `codegraph.callers` → `grep` → `read_file` 补证据后再下判；`dead` 结论没有调用方证据会被 loop 直接拒绝。环境变量 `DEBTSCOPE_AGENT_REVIEW=0` 可关闭阶段二，退回纯批量快判。

其余一切（扫描、计数、分级、环比、渲染）不进 loop。

## 5. Session / Trace（Every run is traceable）

run id 为 `YYYYMMDD-HHMMSS-<6hex>`。Web 多项目模式下落 `~/.debtscope/runs/`，CLI 本地扫描（db 在项目 `.debtscope/`）下落 `<项目>/.debtscope/runs/`；`debtscope runs` 默认合并展示两处，`debtscope runs <id>` 查看事件流（也可 `--json`），Web 端对应只读接口 `GET /api/runs` 与 `GET /api/runs/<id>`。每行一个事件：

```json
{"ts":"...","type":"run.start","repo":"...","commit":"...","mode":"headless"}
{"ts":"...","type":"tool.call","id":"t1","tool":"codegraph.callers","args":{"symbol":"old_export_format_v1"}}
{"ts":"...","type":"tool.result","id":"t1","ms":12,"bytes":412}
{"ts":"...","type":"llm.call","model":"deepseek-chat","prompt_tokens":1830,"completion_tokens":240,"ms":3100}
{"ts":"...","type":"finding.accepted","rule":"unused_function","evidence_tool":"t1","confidence":"high"}
{"ts":"...","type":"finding.rejected","reason":"no_tool_evidence"}
{"ts":"...","type":"run.end","new":3,"resolved":1,"score":86}
```

用途：误判归因（是粗筛漏了还是模型错了）、token 成本核算（对应 PRD 的成本看板）、企业审计、离线回放。

## 6. 插件策略

- **规则即插件（最高优先级）**：v0.3 已将规则引擎重构为数据驱动——`RuleSpec`（id/name/kind/severity/params/…）+ `KINDS` 检测器注册表，规则持久化在每项目 SQLite 中并支持 UI 增删改启停；自定义规则与内置规则走同一条确定性执行路径。后续演进为 DSL 文件（`.debtscope/rules/`，可随仓库版本化、支持 rule pack 与社区规则市场）。
- **语言后端即插件**：`LanguageBackend` 接口（`extensions / index(root) -> Index`），v0.1 的 Python AST 是第一个实现；tree-sitter 后端（Java/Go）按同一接口注册，L2 以上零改动。
- **模型即插件**：provider 注册表 + OpenAI 兼容缝（v0.2 已落地配置与向导）。
- 内核提供 `register_tool / register_backend / register_rule / on(event)` 四个注册口与最小事件总线，不追求 dsh/Cordis 的完整能力面，YAGNI。

## 7. 安全与权限

- 所有工具默认**只读**，根目录限定（沿用 v0.1 的 realpath 穿越防护）。
- 写操作（rule.install、未来的 autofix）分级：配置/规则写入需用户显式确认；代码修改在 vN 才出现，且必须走 diff 预览 + 确认。
- 模型端点默认指向用户配置；内网用户配置内网 base URL 即代码不出网，不依赖专门的私有化版本。

## 8. 分阶段落地

| 版本 | 内容 | 状态 |
|---|---|---|
| v0.1 | 确定性五层管线、7 规则、SQLite 台账、Web 看板、静态/LLM 精判 | ✅ |
| v0.2 | 模型配置向导（CLI + Web）、provider 适配、连接测试、`debtscope.harness` 包 | ✅ |
| v0.3 | 配置前置三态引导（连接模型→选择项目→初始化监控）、多项目注册表与每项目独立台账、数据驱动规则引擎（检测器注册表）、指标管理 UI（增删改/启停/重置）、AI 生成指标 + 试跑预览、取消无模型默认态 | ✅ |
| v0.4 | 接口雷达：Flask/FastAPI/通用 @route 入口发现、跨文件静态调用图、接口调用链 DAG、DB/HTTP 汇点、影响面（blast radius）、N+1 内置规则、链路详情页 | ✅ |
| v0.4.1 | harness 内核落地：micro-kernel + tool registry + 事件总线、8 个只读工具、证据强制的文本 JSON ReAct loop、uncertain 候选 Agent 补证据深判、JSONL run trace（CLI `debtscope runs` + `/api/runs`） | ✅ |
| v0.5 | 语言后端插件口（tree-sitter 试点第二语言）、规则 DSL 文件化与 rule pack、规则生成的多轮 ReAct 自我修正、成本/token 看板、Web 运行记录页 |
| v0.6 | CI headless 模式（`debtscope scan --ci` 输出 JSON/SARIF、退出码门禁）、多仓分组与聚合 rollup |
| later | 自动修复（写工具 + diff 确认）、dsh 生态 bundle（`dsh-plugin-debtscope`）评估 |

## 9. 命名

- 产品、CLI、PyPI/GitHub 名：**Debtscope（债镜）**
- 内部引擎 Python 包：`debtscope.harness`
- 若未来接入 dsh 生态，以插件 `dsh-plugin-debtscope` 形态发布，产品品牌不变。
