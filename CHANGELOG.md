# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.4.0] - 2026-09-18

### 新增 — 接口雷达（代码维度的静态监控）

- **HTTP 入口发现**（`core/endpoints.py`）：支持 Flask（`@app.route` / `@app.get` 等动词、`Blueprint(url_prefix=...)` 与 `register_blueprint(url_prefix=...)` 双前缀拼接、无 `methods` 默认 GET）、FastAPI（动词装饰器、`APIRouter(prefix=...)` + `include_router(prefix=...)`）以及通用裸 `@route` 装饰器（识别为 ANY）；路径多斜杠自动归一。
- **跨文件静态调用图**（`core/callgraph.py`）：两遍 AST 扫描，解析 `import x` / `import x as` / `from x import y` / 相对导入 / 同文件顶层函数 / `self`、`cls` 同类方法 / 类静态方法与构造后立即调用（`Client().get()`）；不可解析的第三方调用不猜边；数据库（`execute` / `executemany` / `fetch*` / `query` / `commit` / `rollback` / `flush` / `bulk_*`）与 HTTP（`requests` / `httpx` / `aiohttp` / `urlopen`）汇点识别为外部节点；BFS 构建链路（深度 / 节点数双上限，天然防环）。
- **债务挂链路**：函数级问题挂到调用链节点，文件级问题挂到链上触达的文件；每个接口独立健康分、高 / 中危计数与快照趋势（分数涨跌、问题涨跌）。
- **影响面（blast radius）**：统计每个函数被多少个接口的链路触达，列表页对多接口共用的热点函数高亮排序。
- **接口监控界面**：默认首页 Tab「接口监控」——接口列表（方法徽章、框架标签、健康分、问题数、链深、影响面、涨跌箭头）与接口详情（KPI、分数迷你趋势、纯手写 SVG 分层调用链 DAG、DB / HTTP 外部调用胶囊、节点点击筛选优化点、代码证据展开、确认 / 误报 / 暂不处理分诊、文件级问题分组）；支持 `?ep=<接口ID>` 深链直达。原看板保留为「项目总览」Tab。
- **新内置规则 `db_call_in_loop`（第 8 条）**：循环体内直接执行数据库 / HTTP 调用（典型 N+1），只报最内层循环，嵌套函数 / lambda 定义内的调用不误报，同循环多处调用分别给出带行号的证据。
- **CLI**：新增 `debtscope endpoints <path>`，纯静态列出 HTTP 入口、框架、链路深度 / 节点数与处理函数（不写库、不调模型）；`scan` 摘要新增接口数。
- **Web API**：`GET /api/projects/<pid>/endpoints` 与 `/api/projects/<pid>/endpoints/<eid>`（详情含节点、边、外部汇点、全量优化点、文件级分组与趋势）。
- demo 工程扩为 6 个文件：新增 Flask 蓝图 + FastAPI 路由的 Web 层与 service / DAO 分层，植入循环内 DB + HTTP 双 N+1、裸 except、共用 DAO 等样例，共 11 个接口。

### 修复

- `swallowed_exception` 检测器现在正确写入所属函数符号；此前吞错问题缺少符号、被当作文件级问题挂到所有触达该文件的接口上。
- 调用图不再把路由装饰器（`@app.route(...)`）本身误连为函数调用边。
- 修复相对导入层级切片的 `-0` 边界错误。

### 变更

- 问题类型（KINDS）12 → 13，内置指标 7 → 8；demo 基线：6 文件 / 381 LOC / 37 符号 / 11 接口 / 12 问题 / 79 分（B）。
- 存储层新增 `endpoints` / `endpoint_findings` / `endpoint_snapshots` 三张表，旧库打开时自动升级；接口每次扫描全量对账（消失的接口物理删除、快照 append-only 保留历史）。
- 测试套件 40 → 56：新增调用图 7 例、入口发现 5 例、N+1 规则正反例 3 例、接口 API 端到端 1 例。
- 文档：双语 README 新增「接口雷达」章节（含支持矩阵与「不做运行时监控 / APM」的明确边界声明）、两张新截图；路线图更新为 v0.5 监控模式 → v0.6 链路 AI 体检 → v0.7 多语言与 CI → v1.0 可选 OTel trace 离线导入。

### 已知限制（路线图内）

- Django `urls.py`、Flask `MethodView` 与 FastAPI 类视图暂不识别；动态拼接的路由表无法静态发现。
- 实例属性上的方法调用（`self.client.fetch()`，其中 `client` 为组合对象）受限于不做跨过程值流分析，暂不连边；`self.方法()`、类静态调用与构造调用不受影响。

## [0.3.1] - 2026-09-17

### 修复（安全与可靠性）

- **安全**：修复本地 Web 服务静态文件接口的路径穿越风险，`/static/` 请求被严格限制在静态资源目录内。
- **安全**：本地服务增加 Host 头白名单校验（仅允许 `127.0.0.1` / `localhost` / `[::1]`），阻断 DNS-rebinding 类访问。
- **AI 精判**：修复精判提示词要求「JSON 数组」却同时强制 `response_format=json_object` 的协议矛盾，统一为 `{"results": [...]}` 对象协议，并大幅增强模型返回内容的容错解析（markdown 代码块、前后缀散文、包装对象均可解析）。
- **AI 生成指标**：模型调用失败或返回非法 JSON 时，界面展示具体原因而不是笼统失败。
- **Web 服务**：默认端口 8787 被占用时自动顺延寻找可用端口，不再直接崩溃。
- **Web 服务**：服务端版本号不再硬编码，随包版本自动更新。
- **代码接口**：`/code` 的行号参数非法时返回 400 而不是 500。

### 新增

- AI 精判失败（网络 / 鉴权 / 限流 / 模型异常）时，扫描摘要、CLI 输出与看板顶部徽章会明确提示「AI 精判降级」及原因，不再静默退回静态模式。
- 新建「命名规范」指标时，后端会校验正则合法性，非法正则在试跑阶段即给出中文报错。
- 新建「禁用调用」指标时，校验调用名非空。
- 测试套件从 3 个扩充到 40 个：规则引擎 13 例、LLM 协议解析 10 例、Web API 8 例、模型预设 6 例，全部离线可跑。
- GitHub Actions CI：Python 3.10–3.13 矩阵编译与测试。
- `CHANGELOG.md`、`CONTRIBUTING.md`、双语 README（中文主版 + 英文版）。

### 变更

- `pyproject.toml` 补全仓库地址（Homepage / Repository / Issues / Changelog）与 Python 版本分类器。

## [0.3.0] - 2026-09-17

### 新增

- **配置前置三步引导**：首次进入依次完成「连接模型 → 选择项目 → 初始化监控」，不再提供无模型降级态（CI / 离线场景使用显式 `--no-llm`）。
- **16 套 OpenAI 兼容模型预设**：默认豆包 / 火山方舟 Seed-Evolving，另含 DeepSeek、通义千问、智谱 GLM、Kimi、混元、千帆、MiniMax、小米 MiMo、硅基流动、OpenAI、Gemini、Grok、Mistral、本地 Ollama 与任意自定义兼容端点。
- **多项目托管**：`~/.debtscope/projects.json` 注册表 + 每项目独立 SQLite，顶栏一键切换；禁止监控根目录 / 用户主目录。
- **指标管理**：内置指标可在看板内启用 / 停用、调严重度、重置；8 类指标可自助新增、编辑、删除。
- **AI 生成指标**：一句话描述生成规则 DSL，保存前必须在当前项目上试跑预览命中样例。
- 12 类确定性检测器（AST / 调用图 / 结构指纹），7 条内置指标。

## [0.2.0]

- 模型与端点配置系统（`debtscope config` 向导 + `doctor` 连通性自检）。
- 规则数据化：规则落库、参数可调、自定义规则。

## [0.1.0]

- 确定性五层管线闭环：索引 → 粗筛 → LLM 精判 → 对账 → 渲染。
- 本地 Web 看板：健康分、严重度 / 类型分布、趋势、问题明细、代码上下文、一键分诊。
- 纯 Python 标准库实现，零第三方依赖。
