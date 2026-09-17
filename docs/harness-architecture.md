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

Agent 不直接 import 内核函数，只通过声明了 JSON Schema 的工具拿证据。首批工具：

| 工具 | 入参（摘要） | 返回 | 只读 |
|---|---|---|---|
| `codegraph.symbols` | path 前缀、kind | 符号列表（名、位置、装饰器、行数） | ✅ |
| `codegraph.callers` | symbol | 全部调用方/引用位置（空数组 = 废弃证据） | ✅ |
| `codegraph.callees` | symbol | 被调用方 | ✅ |
| `read_file` | file、start、end | 带行号代码片段（路径限定仓库根） | ✅ |
| `grep` | regex、glob | 命中位置 | ✅ |
| `rules.catalog` | — | 内置规则与 DSL 说明 + few-shot | ✅ |
| `rule.preview` | DSL | 试跑命中数量与样例（不落库） | ✅ |
| `rule.install` | DSL、名称、严重度 | 持久化自定义规则 | ❌（写，需用户在回显页确认） |
| `findings.llm_review` | finding id 集合 | dead / entry / uncertain + 理由 | ✅ |

关键约束：
- 工具返回值自带 `file/line/snippet`，Agent 的结论 JSON 必须引用工具调用 id，**无工具证据的结论在 kernel 校验阶段直接丢弃**。
- `rule.preview` 强制在 `rule.install` 之前调用——对应产品设计里的"规则回显确认"，防止自然语言误解直接污染看板。

## 4. Agent Loop 的两个使用场景

默认 ReAct（think → act → observe → … → final），但仅在两处启用：

1. **自然语言自定义规则**
   完整形态：`理解需求 → rules.catalog 参考 → 生成 DSL → rule.preview 试跑 →（必要时读样例代码自我修正）→ 回显给用户 → rule.install`。
   **v0.3 已落地单步形态**：一次 LLM 调用把自然语言编译为参数化规则（`{kind, severity, params}`，kind 取自 12 类确定性检测器注册表），必须先对当前索引试跑预览（命中数 + 样例）才能保存。多轮 ReAct 自我修正（读样例代码、调整阈值后重试）留待 v0.4。
   产出物始终是**确定性的规则规格**，之后每次扫描都不再调用模型——一次 agentic，长期确定性。
2. **废弃代码语义精判（v0.1 已具备雏形）**
   粗筛候选 → 批量 LLM 判定 dead/entry/uncertain。harness 化后改为工具式：Agent 对模糊候选可主动 `codegraph.callers` / `read_file` 补证据再下判，而不是只看单函数片段。

其余一切（扫描、计数、分级、环比、渲染）不进 loop。

## 5. Session / Trace（Every run is traceable）

`~/.debtscope/runs/<ts>-<short>.jsonl`，每行一个事件：

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
| v0.3 | 配置前置三态引导（连接模型→选择项目→初始化监控）、多项目注册表与每项目独立台账、数据驱动规则引擎（12 类检测器）、指标管理 UI（增删改/启停/重置）、AI 生成指标 + 试跑预览、取消无模型默认态 | ✅ |
| v0.4 | micro-kernel + tool registry + 多步 ReAct loop（规则生成自我修正、Agent 式补证据精判）、trace JSONL 与运行记录页 |
| v0.5 | 语言后端插件口（tree-sitter 试点第二语言）、规则 DSL 文件化与 rule pack、成本/token 看板 |
| v0.6 | CI headless 模式（`debtscope scan --ci` 输出 JSON/SARIF、退出码门禁）、多仓分组与聚合 rollup |
| later | 自动修复（写工具 + diff 确认）、dsh 生态 bundle（`dsh-plugin-debtscope`）评估 |

## 9. 命名

- 产品、CLI、PyPI/GitHub 名：**Debtscope（债镜）**
- 内部引擎 Python 包：`debtscope.harness`
- 若未来接入 dsh 生态，以插件 `dsh-plugin-debtscope` 形态发布，产品品牌不变。
