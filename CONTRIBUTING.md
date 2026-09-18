# 参与贡献

感谢你对 Debtscope（债镜）的兴趣！欢迎提交 Issue、改进文档、新增检测器或语言后端。

## 开发环境

Debtscope 刻意保持**零第三方运行时依赖**，只需要 Python 3.10+：

```bash
git clone https://github.com/taoyongpan/Debtscope.git
cd Debtscope
python -m debtscope --version
```

可选地以可编辑模式安装，获得 `debtscope` 命令：

```bash
pip install -e .
```

## 跑测试

全部测试离线运行，不需要任何 API Key：

```bash
python -m unittest discover -s tests -v
```

- `tests/test_smoke.py`：demo 仓库端到端扫描、重复扫描幂等、误报抑制
- `tests/test_rules_engine.py`：13 类检测器在合成代码上的命中 / 反例
- `tests/test_callgraph.py`：导入解析、调用边、DB / HTTP 汇点分类、BFS 链路、防环、影响面
- `tests/test_endpoints.py`：Flask 蓝图双前缀、FastAPI 路由前缀、通用 `@route`、demo 11 接口断言
- `tests/test_llm_parse.py`：模型协议解析与失败兜底（mock，不联网）
- `tests/test_web_api.py`：本地 HTTP API、安全校验、指标生命周期、接口雷达 API
- `tests/test_config_presets.py`：模型预设与配置文件读写
- `tests/test_harness_kernel.py`：工具注册 / 入参校验 / 写保护 / 事件总线 / 路径穿越防护
- `tests/test_harness_tools.py`：8 个只读工具在 demo 工程上的行为与越界拒绝
- `tests/test_harness_trace.py`：JSONL 落盘 / 汇总 / 防穿越 / 异常上下文 / 大参数截断
- `tests/test_harness_agent.py`：ReAct 正常取证、证据缺失打回、无调用方证据拒绝、未知工具纠正、步数耗尽、LLM 异常、reviewer 两阶段集成与开关

提交前请确保：测试全绿、`python -m compileall -q debtscope` 无错。

## 代码结构

```
debtscope/
  core/
    python_indexer.py   # L1：AST 索引（符号、调用名、结构指纹、TODO）
    callgraph.py        # L1：静态调用图（导入解析、调用边、DB/HTTP 汇点、BFS 链路、影响面）
    endpoints.py        # L1：HTTP 入口发现（Flask / FastAPI / 通用 @route、前缀拼接）
    rules.py            # L2：数据驱动的确定性规则引擎（检测器注册表）
    reviewer.py         # L3：批量快判 + uncertain 候选的 Agent 补证据深判（两阶段）
    llm.py              #    OpenAI 兼容客户端（stdlib urllib，chat_json 带 token 用量）
    storage.py          #    SQLite：问题台账 / 快照 / 规则 / 反馈 / 接口与接口快照
    health.py           #    透明加权扣分的健康分
    scanner.py          # 编排：索引 → 入口/调用图 → 规则 → 精判 → 对账 → 快照（贯穿 run trace）
  harness/
    kernel.py           # 微内核：Tool 契约 / 注册中心 / 事件总线 / 写保护 / safe_join 路径防护
    tools.py            # 8 个只读工具（调用图、读文件、grep、规则目录/试跑、接口链路）
    agent.py            # 文本 JSON ReAct loop、证据强制校验、废弃代码调查任务
    trace.py            # JSONL run 记录器（NullTracer / RunRecorder / list_runs / read_run）
    config_store.py     # 模型预设与 ~/.debtscope/config.json（权限 600）
    projects.py         # 多项目注册表
  web/                  # stdlib http.server + 原生 JS 看板（无构建步骤，SVG 手写）
  cli.py                # scan / serve / endpoints / runs / rules / config / doctor
```

## 新增一个确定性检测器

1. 在 `core/rules.py` 的 `KINDS` 注册 `KindMeta`（参数 schema、默认值、文案）。
2. 实现 `check_xxx(idx, spec)` 并登记到 `CHECKS`。
3. 若希望开箱启用，在 `BUILTIN_SPECS` 增加一条内置规则（id 保持稳定，它是旧数据库的兼容键）。
4. 在 `tests/test_rules_engine.py` 补「命中 + 反例」两个用例。
5. 检测器必须是**确定性**的：不访问网络、不执行被扫描代码、不依赖模型。

## 设计原则

- **确定性内核 + Agent 外壳**：AST / 调用图做全量粗筛，LLM 只精判极少数候选，任何模型失败都不得阻断扫描。
- **只做静态分析**：不运行被扫描代码、不插桩、不采集 QPS / 延迟等运行时指标，不做 APM。
- **数据不出本机**：Key 仅存于 `~/.debtscope/config.json`（chmod 600），仓库代码不会被上传。
- **零依赖**：标准库能解决的问题不引入第三方包；前端不引入框架与构建链。
- **证据可追溯**：每条问题都带文件、行号、代码片段与修复建议。

## 提交与 Issue

- 提交信息建议使用 `feat: / fix: / docs: / test: / refactor:` 前缀。
- Bug 报告请附：`debtscope doctor` 输出、Python 版本、复现步骤（Key 请打码）。
- PR 请保持单一主题，并为行为变更补充测试。

## 行为准则

保持友善、就事论事；欢迎不同语言与背景的贡献者。
