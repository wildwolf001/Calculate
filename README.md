# 明账 · 对话式经营分析 Agent

一个以语义层和可核验 SQL 为核心的对话式 BI 项目。用户可以导入 CSV、TSV 或 Excel，系统自动识别表结构、生成待确认指标，并通过自然语言完成查询、经营分析和结果复现。

## 核心能力

- 事件溯源状态机：每轮对话都可回放，模型只负责结构化解析和文字组织。
- 数据集隔离：每个数据源拥有独立的语义层、DuckDB 仓库、口径文档和运行上下文。
- 可控 SQL：计划先校验再编译，所有值参数化，DuckDB 只读执行。
- 多问题处理：一条消息中的多个独立问题按顺序执行，单个问题失败不会中断整轮。
- 经营分析：按当前数据集动态发现可用板块，生成趋势、结构、变化、履约、口碑等报告。
- 查询记录：保存计划、SQL、参数、指纹、数据版本和校验结果，可重新执行并比较结果。
- 口径治理：支持草稿确认、发布、版本历史、文档解析和发布就绪检查。
- 语义发布：发布期生成实体注册表和实体计数指标；运行时优先使用已发布语义，必要时可基于已发布列构造 AVG/SUM 等派生指标（标为推断口径），模型始终不写 SQL。
- 来源可追溯：发布只记录 `confirmed_at/confirmed_by`，不修改 `provenance`；重建只恢复人工或文档口径，机器草稿会重新生成。
- 能力单一来源：发布时按 join 图计算每个指标可安全拆分的维度并写回 `supported_grain`，运行时不再与 YAML 白名单并存。
- 上下文编译：从事件日志确定性提取当前结果、筛选范围、实体焦点和最近轮次，再交给语义解析器。
- 数据质量提示：缺失值、空列、负值、覆盖不足等问题不会阻止可计算查询，但会在答案中明确标注。
- 覆盖诊断：按完整自然月序列检查缺失月份，并区分窗口边缘缺失、内部断档与零业务量。
- 比率排名样本量：比率类排名多取几行，按最小分母（`MIN_RATIO_DENOMINATOR`，默认 5）区分小样本异常值与稳定结论；答案先给样本充足组，小样本行保留在结果表。
- 安全边界：默认只监听本机；可选 `API_TOKEN` 保护全部 API 与文档；SPA 兜底路径不可越界；CSV 导出做公式注入转义；PII 列不进入模型上下文。

## 技术栈

- 后端：Python 3.10+、FastAPI、Pydantic、DuckDB
- 前端：Vue 3、TypeScript、Vite、Pinia
- 模型：Anthropic 兼容接口；未配置密钥时使用确定性规则解析和模板回答

## 启动

Windows PowerShell：

```powershell
cd E:\10.23\project_1.0
Copy-Item .env.example .env
# 在 .env 中填写 ANTHROPIC_API_KEY；不填写也可离线运行

.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

前端开发模式：

```powershell
cd web
npm install
npm run dev
```

生产构建产物由后端从 `web/dist` 提供。开发完成后执行：

```powershell
cd web
npm run build
```

## 使用流程

1. 打开 `http://127.0.0.1:8000/manage`。
2. 上传 CSV/XLSX，选择合计行和敏感信息策略。
3. 检查数据画像、质量报告、关系与发布就绪状态。
4. 上传指标/口径文档，确认指标、维度和时间列。
5. 发布数据集。
6. 回到聊天页，选择数据源后开始提问。

经营分析可从聊天区底部的“生成经营分析”进入；查询记录可从结果区右上角的“历史结果”查看，并在记录抽屉中执行“重跑验证”。

## 项目结构

```text
app/
  api/           FastAPI 路由与上传接口
  core/          状态机、事件日志、会话和任务
  datasets/      数据集上下文、注册表、发布与就绪检查
  domain/        计划、编译器、校验器、分析、洞察和记录
  ingest/        文件发现、类型推断、关系推断和语义草稿
  llm/           意图解析、回答、分析和受控兜底
web/src/         聊天、结果画布、数据管理页面
tests/           单元和集成测试
project_2/       当前整合前状态的本机备份（被 Git 忽略）
```

业务数据集、上传文件、会话、日志、`.env` 和 DuckDB 仓库都不会提交到 Git。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_q01_q08_fullchain.py
cd web
.\node_modules\.bin\vue-tsc.cmd --noEmit
```

部分回归测试依赖已导入的默认电商数据集；干净仓库中这类测试会跳过。通用接入、分析和记录相关单元测试不依赖业务数据。

非电商端到端回归（SaaS 订阅 + 中文表头）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_generic_non_ecommerce.py
```

2026-11-11 的口径治理、泛化与边界修复说明见 [design/FIXES_2026-11-11.md](design/FIXES_2026-11-11.md)。

## 部署与安全

默认只监听 `127.0.0.1`，适合单机本地运行。需要从其他机器访问时，必须同时设置：

```powershell
$env:HOST="0.0.0.0"
$env:API_TOKEN="<一个长随机串>"
```

设置 `API_TOKEN` 后，除 `/api/health` 外的所有 `/api`、`/docs`、`/openapi.json` 请求都需要 `X-API-Token` 或 `Authorization: Bearer <token>`；未设置 token 却绑定非本机地址时，应用会拒绝启动。生产环境还应在前置代理层做 TLS、限流与访问日志。

## 当前边界

- 关系接入支持 N:1/1:1，不支持直接分析 N:N，需要先物化宽表。
- 自动推断生成的是草稿口径，必须人工确认后才能作为可信口径发布。
- 实体注册表在发布期从已发布的 `entity_count` 指标或显式 `entities` 配置生成；没有实体元数据的数据集会澄清实体计数问题，不会猜自然键。
- 经营分析只会展示当前语义层实际支持的板块，不会为了凑模块编造指标。
- 当前运行时面向单进程本地部署，尚未实现多租户配额和生产级任务队列。
