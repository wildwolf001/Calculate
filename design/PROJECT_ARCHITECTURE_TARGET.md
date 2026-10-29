# 明账目标代码架构：双 Agent + 确定性执行内核

> 版本：2026-10-29 v1
> 关联：REFACTOR_PLAN.md、INGEST_AGENT_DESIGN.md
> 结论：对话部分也要调整，但不是推倒重来；把“语义解析”抽成 Chat Agent，把“执行”留在 domain。

## 0. 先回答

对话部分需要“中等幅度”调整，不是“就这样也行”：

必须改：

1. 把 controller 里的语义解析抽成 app/chat/agent；
2. 让 QueryIR 成为 parser → planner → compiler 的唯一契约；
3. 删除 6 个 _salvage_*，不再在解析后改语义；
4. 子问题校验失败不再静默变 explain；
5. 答案带口径来源/替代口径。

不用改：

1. 事件溯源、SessionState、reducer、event store；
2. domain 的 plan/validate/compile/execute/check；
3. capability graph、compiler、executor、checks；
4. 现有 API 的会话/结果/记录接口。

一句话：**Chat 的“理解”变成 agent，“执行”仍然是确定性管线。**

## 1. 总架构

~~~text
┌──────────────────────── 浏览器 (Vue 3) ────────────────────────┐
│ ChatPanel / ResultCanvas / ManageApp / ReviewQueue / RecordDrawer │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTP + SSE
┌──────────────────────────────▼──────────────────────────────────┐
│ app/api                                                         │
│  routes.py 会话/数据集/发布/记录                                  │
│  uploads.py 上传/分析 job/文档/SSE                                │
│  schemas.py / sse.py                                            │
└───────────────┬───────────────────────────────┬─────────────────┘
                │                               │
┌───────────────▼──────────────┐  ┌─────────────▼─────────────────┐
│ app/chat                     │  │ app/ingest                    │
│  Chat Agent（在线）           │  │  Ingest Agent（离线）          │
│  - 语义解析 → QueryIR        │  │  - 事件驱动建模                │
│  - tools: search/describe/   │  │  - tools: header/grain/...     │
│    profile/validate          │  │  - gates: G1..G8              │
│  - turn: plan/execute/compose│  │  - review queue               │
└───────────────┬──────────────┘  └─────────────┬─────────────────┘
                │                               │
┌───────────────▼───────────────────────────────▼─────────────────┐
│ app/agent：通用内核                                              │
│  controller / contracts / registry / budget / policy / events    │
│  prompts / review / artifacts                                    │
└───────────────┬───────────────────────────────┬─────────────────┘
                │                               │
┌───────────────▼──────────────┐  ┌─────────────▼─────────────────┐
│ app/domain（确定性执行）       │  │ app/datasets（数据平台）       │
│  plan/planner/validator      │  │  registry/context/index       │
│  compiler/executor/checks    │  │  publish/readiness/health     │
│  capability/diagnosis/...    │  │  merge/manifest               │
└───────────────┬──────────────┘  └─────────────┬─────────────────┘
                │                               │
        DuckDB warehouse                semantic_index.json
                                        semantic.yaml / draft
~~~

## 2. 分层职责

| 层 | 职责 | 不做什么 |
|---|---|---|
| web | 对话、结果、数据管理、审核 | 不做语义判断 |
| api | HTTP/SSE、任务、序列化 | 不写业务规则 |
| chat | 语义解析、turn 编排 | 不生成 SQL |
| ingest | 建模、探测、质检 | 不服务在线查询 |
| agent | 通用 loop/工具/预算/事件 | 不知道业务 |
| domain | 计划、校验、编译、执行、检查 | 不调用 LLM |
| datasets | 数据集生命周期、索引、发布 | 不做查询 |
| core | 事件存储、会话、任务、日志、追踪 | 不做业务 |

## 3. 完整目录树（目标）

~~~text
project_1.0/
├─ app/
│  ├─ main.py                       FastAPI 入口、静态站点、CORS
│  ├─ factory.py                    组合根：chat/ingest/datasets 的装配
│  ├─ config/
│  │  └─ settings.py                路径、LLM、预算、NO_PROXY 修正
│  ├─ api/
│  │  ├─ routes.py                  会话/数据集/发布/记录/分析
│  │  ├─ uploads.py                 上传/分析 job/文档/SSE
│  │  ├─ schemas.py                 请求/响应模型
│  │  └─ sse.py                     事件流编码
│  ├─ agent/                        # 新增：通用 agent 内核
│  │  ├─ contracts.py               Tool/ToolResult/Decision/Issue
│  │  ├─ controller.py              observe→decide→act→verify→emit
│  │  ├─ state.py                   AgentState 协议 + replay
│  │  ├─ registry.py                ToolRegistry：注册/校验/执行
│  │  ├─ budget.py                  步数/token/时间预算
│  │  ├─ policy.py                  loop 检测、升级人审、终止
│  │  ├─ events.py                  AgentEvent / EventLog / JSONL
│  │  ├─ prompts.py                 Prompt / 版本 hash
│  │  ├─ review.py                  HumanQuestion / ReviewQueue
│  │  └─ artifacts.py               ArtifactWriter 协议
│  ├─ chat/                         # 新增：在线对话
│  │  ├─ controller.py              会话状态机（从 core/controller 拆出）
│  │  ├─ state.py                   SessionState/Slots（从 core/state 迁入）
│  │  ├─ events.py                  session 事件类型
│  │  ├─ agent/
│  │  │  ├─ spec.py                 build_chat_agent()
│  │  │  ├─ tools/
│  │  │  │  ├─ schema_search.py     search_semantics
│  │  │  │  ├─ describe_metric.py   返回表达式/scope/time_via
│  │  │  │  ├─ profile_column.py    列画像/样本
│  │  │  │  ├─ sample_values.py     维度真实值
│  │  │  │  └─ validate_ir.py       capability/compile 预检
│  │  │  └─ prompts/
│  │  │     ├─ parse_system.txt
│  │  │     └─ parse_user.txt
│  │  └─ turn/
│  │     ├─ parse.py               brief + tools → QueryIR
│  │     ├─ plan.py                IR → QueryPlan
│  │     ├─ execute.py             validate/compile/execute/check
│  │     ├─ compose.py             答案 + 口径来源 + 替代口径
│  │     ├─ multi.py               多问题
│  │     ├─ analysis.py            经营分析
│  │     └─ interpretation.py      多解释
│  ├─ ingest/
│  │  ├─ prepare.py                 复制/hash/打开工作簿/列 sheet
│  │  ├─ agent/
│  │  │  ├─ spec.py                 build_ingest_agent()
│  │  │  ├─ state.py                IngestState + reduce
│  │  │  ├─ events.py               ingest.* 事件
│  │  │  ├─ policy.py               预算/终止/高风险
│  │  │  ├─ review.py               人审问题
│  │  │  ├─ artifacts.py            draft/index/issues/review
│  │  │  ├─ tools/
│  │  │  │  ├─ files.py
│  │  │  │  ├─ sheets.py
│  │  │  │  ├─ header.py
│  │  │  │  ├─ columns.py
│  │  │  │  ├─ grain.py
│  │  │  │  ├─ relations.py
│  │  │  │  ├─ metrics.py
│  │  │  │  ├─ docs.py
│  │  │  │  ├─ probes.py
│  │  │  │  └─ write.py
│  │  │  ├─ gates/
│  │  │  │  ├─ base.py
│  │  │  │  ├─ structural.py
│  │  │  │  ├─ relational.py
│  │  │  │  ├─ metric.py
│  │  │  │  └─ docs.py
│  │  │  └─ prompts/
│  │  │     ├─ system.txt
│  │  │     └─ decide.txt
│  │  ├─ header.py                  工具库：表头/合计行
│  │  ├─ infer.py                   工具库：类型推断
│  │  ├─ keys.py                    工具库：grain/PK
│  │  ├─ relations.py               工具库：关系推断
│  │  ├─ profile.py                 工具库：画像/catalog
│  │  ├─ quality_generic.py         工具库：通用质量
│  │  ├─ semantic_draft.py          工具库：结构草稿
│  │  ├─ llm_semantic.py            工具库：候选指标
│  │  └─ documentation.py           工具库：文档口径
│  ├─ domain/                        # 确定性执行内核（保持）
│  │  ├─ query/
│  │  │  ├─ ir.py                   QueryIR（唯一契约）
│  │  │  ├─ metric.py               MetricSpec / AST lowering
│  │  │  └─ resolve.py              口径优先级/候选/替代
│  │  ├─ plan.py
│  │  ├─ planner.py
│  │  ├─ compiler.py                唯一拼 SQL 的地方
│  │  ├─ validator.py
│  │  ├─ capability.py
│  │  ├─ executor.py
│  │  ├─ checks.py
│  │  ├─ diagnosis.py
│  │  ├─ recovery.py
│  │  ├─ insights.py
│  │  ├─ presentation.py
│  │  ├─ records.py
│  │  ├─ analysis.py
│  │  ├─ semantic/
│  │  │  ├─ loader.py
│  │  │  ├─ graph.py
│  │  │  └─ timeaxis.py
│  │  └─ model/
│  │     ├─ types.py
│  │     ├─ expr.py
│  │     ├─ model.py
│  │     ├─ validate.py
│  │     └─ sql.py
│  ├─ datasets/
│  │  ├─ registry.py
│  │  ├─ context.py
│  │  ├─ index.py                   新增：semantic_index
│  │  ├─ publish.py
│  │  ├─ readiness.py
│  │  ├─ health.py
│  │  ├─ merge.py
│  │  └─ manifest.py
│  ├─ core/                          # 运行时原语（逐步通用化）
│  │  ├─ event_store.py             JSONL 事件存储
│  │  ├─ events.py                  事件基类
│  │  ├─ session.py                 会话边界/串行
│  │  ├─ jobs.py                    后台任务
│  │  ├─ trace.py                   追踪
│  │  ├─ logging.py                 结构化日志
│  │  └─ interfaces.py              协议
│  ├─ llm/
│  │  ├─ client.py                  Anthropic 兼容客户端
│  │  ├─ compose_answer.py          答案组合/数字白名单
│  │  ├─ analyze.py                 分析文字
│  │  └─ rule_parser.py             离线兜底（Phase 3 后降级/删除）
│  └─ tools/
│     └─ harness.py                 只读工具后端（chat 复用）
├─ web/
│  └─ src/
│     ├─ main.ts
│     ├─ App.vue
│     ├─ manage/
│     │  ├─ ManageApp.vue
│     │  ├─ UploadPanel.vue
│     │  ├─ DraftEditor.vue
│     │  ├─ ReviewQueue.vue
│     │  └─ PublishPanel.vue
│     ├─ components/
│     │  ├─ ChatPanel.vue
│     │  ├─ ResultCanvas.vue
│     │  ├─ ProvenanceBadge.vue
│     │  ├─ AlternativesCard.vue
│     │  ├─ RecordDrawer.vue
│     │  ├─ AnalysisView.vue
│     │  └─ ChartView.vue
│     ├─ stores/
│     │  ├─ chat.ts
│     │  ├─ events.ts
│     │  └─ results.ts
│     ├─ api.ts
│     └─ types.ts
└─ tests/
   ├─ agent/                        内核单测
   ├─ chat_agent/                   parser/tools/IR
   ├─ ingest_agent/                 fixture/replay/gates
   └─ integration/                  官方 8 题/非电商
~~~

## 4. Chat 怎么改

### 4.1 保留

- SessionState、reducer、event store、SSE；
- turn 状态机（PARSING/GROUNDING/PLANNING/VALIDATING/EXECUTING/CHECKING/COMPOSING）；
- capability/validator/compiler/executor/checks；
- 多问题、分析、多解释作为 turn 的独立模块。

### 4.2 抽出

- controller 里的 _handle_parsing → chat/agent；
- 6 个 _salvage_* → 删除，不再 parse 后改语义；
- schema_card → semantic_brief + tools；
- parse_intent.py 的校验/重试 → chat/agent；
- controller 的 2582 行 → chat/controller + chat/turn/*，目标 <600 行。

### 4.3 对话 agent 的循环

~~~text
turn:
  brief = retrieve(index, question)
  loop (max 2 tool calls):
    decision = LLM(brief, tools, last_result)
    if decision.tool: result = tool.run(...); brief += summary
    else: break
  ir = decision.ir
  if invalid: one repair
  plan = planner.from_ir(ir)
  validate → compile → execute → check
  compose(answer + provenance + alternatives)
~~~

注意：**chat 的 loop 很短**，不是 ingest 那种 30~60 步；它的“工具”只查索引/校验，不扫原始表。

### 4.4 对话 agent 的工具

- search_semantics(query) → top-k 指标/维度；
- describe_metric(id) → 表达式/scope/time_via/支持维度；
- profile_column(table.column) → 类型/空值/distinct/样本；
- sample_values(dim) → 真实值；
- validate_ir(ir) → capability/compile 预检。

没有 execute_sql 工具；执行发生在 IR 定稿之后，由 domain 做。

## 5. Ingest Agent

详见 INGEST_AGENT_DESIGN.md。它和 Chat Agent 共享 app/agent 内核，但：

- 预算更长；
- 工具有写操作（draft/index）；
- 有 G1~G8 质量 gate；
- 有人审队列；
- 输出 draft/index，不输出答案。

## 6. 共享内核

app/agent 只包含：

- 契约：Tool/ToolResult/Decision/Event/State/Gate；
- 循环：observe→decide→act→verify→emit；
- 注册表、预算、loop 检测、prompt 版本、review 协议、artifact 协议。

它不 import chat/ingest/domain/api。这样：

- chat 和 ingest 不复制 controller；
- 以后加 analysis agent、monitor agent 也只写 spec；
- 单测可以 FakeLLM 跑 loop。

## 7. 依赖规则

~~~text
api → chat, ingest, datasets
chat.agent → agent, domain.query, tools
chat.turn → domain.*, llm.compose
ingest.agent → agent, ingest.*（工具库）
datasets → domain.semantic, domain.model
domain → semantic/model（不 import agent/api）
agent → 标准库/Pydantic
~~~

禁止：

- domain import chat/ingest/api；
- agent import 业务；
- 工具直接写 semantic.yaml；
- 前端做语义判断。

## 8. 数据契约

- semantic_index.json：查询时唯一读模型；
- QueryIR：parser → planner 的唯一契约；
- QueryPlan：planner → compiler 的唯一契约；
- semantic_draft.json：ingest agent 的输出；
- ingest/events.jsonl：ingest 的审计与回放；
- session events：chat 的审计与回放。

## 9. 迁移顺序

Phase 0：止血（白名单/子问题/自谓词过滤）。

Phase 1：并行

- 查询线：semantic_index + brief；
- 数据线：app/agent 内核 + ingest agent skeleton。

Phase 2：QueryIR/MetricExpr 接进 plan/compiler。

Phase 3：chat/agent 切换，删 salvage，拆 controller。

Phase 4：ingest agent 完整化（tools/gates/review）。

Phase 5：web 拆组件、清理遗留栈。

## 10. 不做什么

1. 不让 LLM 写 SQL；
2. 不把 compiler/executor 变成 LLM 工具；
3. 不把 chat 的每一步都做成 agent 工具（延迟会炸）；
4. 不长期保留 Slots/IR/Plan/Model 四套表示；
5. 不为兼容旧测试保留遗留电商栈。
