# 明账最终代码架构与增删改清单

> 版本：2026-10-29 v1
> 关联：PROJECT_ARCHITECTURE_TARGET.md、REFACTOR_PLAN.md、INGEST_AGENT_DESIGN.md
> 标记：A=新增，M=修改，D=删除，R=移动/重命名，K=保留

## 0. 最终态一句话

~~~text
Chat Agent（在线）  +  Ingest Agent（离线）
        ↓                       ↓
   共享 app/agent 内核（loop/tools/events/budget/gates/review）
        ↓                       ↓
   QueryIR → 确定性执行内核   semantic_draft/index
        ↓                       ↓
   compiler → SQL → DuckDB   publish → semantic_index
~~~

- Chat 负责“理解”，输出 QueryIR；
- Ingest 负责“建模”，输出 draft/index；
- Domain 负责“执行”，只做 plan/validate/compile/execute/check；
- LLM 永远不写 SQL。

## 1. 分层图

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
│  - QueryIR                   │  │  - draft/index                │
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

## 2. 最终目录树

~~~text
project_1.0/
├─ app/
│  ├─ main.py                       K
│  ├─ factory.py                    M  装配 chat/ingest/datasets
│  ├─ config/
│  │  └─ settings.py                M  预算/flag/index 路径
│  ├─ api/
│  │  ├─ routes.py                  M  会话/数据集/记录；可拆 routes/
│  │  ├─ uploads.py                 M  启动 ingest job + SSE
│  │  ├─ schemas.py                 M  provenance/alternatives
│  │  └─ sse.py                     K
│  ├─ agent/                        A  通用内核
│  │  ├─ contracts.py               A  Tool/ToolResult/Decision/Issue
│  │  ├─ controller.py              A  observe→decide→act→verify→emit
│  │  ├─ state.py                   A  AgentState 协议
│  │  ├─ registry.py                A  ToolRegistry
│  │  ├─ budget.py                  A
│  │  ├─ policy.py                  A  loop 检测/升级人审
│  │  ├─ events.py                  A  AgentEvent/EventLog/JSONL
│  │  ├─ prompts.py                 A  Prompt/版本 hash
│  │  ├─ review.py                  A  HumanQuestion
│  │  └─ artifacts.py               A  ArtifactWriter
│  ├─ chat/                         A
│  │  ├─ controller.py              R  从 core/controller.py 迁入
│  │  ├─ state.py                   R  从 core/state.py 迁入
│  │  ├─ events.py                  R  从 core/events.py 迁入
│  │  ├─ agent/
│  │  │  ├─ spec.py                 A  build_chat_agent()
│  │  │  ├─ tools/
│  │  │  │  ├─ schema_search.py     A
│  │  │  │  ├─ describe_metric.py   A
│  │  │  │  ├─ profile_column.py    A
│  │  │  │  ├─ sample_values.py     A
│  │  │  │  └─ validate_ir.py       A
│  │  │  └─ prompts/
│  │  │     ├─ parse_system.txt     A
│  │  │     └─ parse_user.txt       A
│  │  └─ turn/
│  │     ├─ parse.py                A  从 controller 拆出
│  │     ├─ plan.py                 A
│  │     ├─ execute.py              A
│  │     ├─ compose.py              A
│  │     ├─ multi.py                A
│  │     ├─ analysis.py             A
│  │     └─ interpretation.py       A
│  ├─ ingest/
│  │  ├─ prepare.py                 A  复制/hash/打开工作簿
│  │  ├─ agent/                     A
│  │  │  ├─ spec.py                 A
│  │  │  ├─ state.py                A
│  │  │  ├─ events.py               A
│  │  │  ├─ policy.py               A
│  │  │  ├─ review.py               A
│  │  │  ├─ artifacts.py            A
│  │  │  ├─ tools/
│  │  │  │  ├─ files.py             A
│  │  │  │  ├─ sheets.py            A
│  │  │  │  ├─ header.py            A
│  │  │  │  ├─ columns.py           A
│  │  │  │  ├─ grain.py             A
│  │  │  │  ├─ relations.py         A
│  │  │  │  ├─ metrics.py           A
│  │  │  │  ├─ docs.py              A
│  │  │  │  ├─ probes.py            A
│  │  │  │  └─ write.py             A
│  │  │  ├─ gates/
│  │  │  │  ├─ base.py              A
│  │  │  │  ├─ structural.py        A
│  │  │  │  ├─ relational.py        A
│  │  │  │  ├─ metric.py            A
│  │  │  │  └─ docs.py              A
│  │  │  └─ prompts/
│  │  │     ├─ system.txt           A
│  │  │     └─ decide.txt           A
│  │  ├─ header.py                  K  工具库
│  │  ├─ infer.py                   K
│  │  ├─ keys.py                    K
│  │  ├─ relations.py               K
│  │  ├─ profile.py                 K
│  │  ├─ quality_generic.py         K
│  │  ├─ semantic_draft.py          M  变成 draft.structure 工具
│  │  ├─ llm_semantic.py            D  逻辑迁入 tools/metrics.py
│  │  └─ documentation.py           K  工具库
│  ├─ domain/
│  │  ├─ query/
│  │  │  ├─ ir.py                   M  唯一契约
│  │  │  ├─ metric.py               A  MetricSpec/AST lowering
│  │  │  └─ resolve.py              A  口径优先级/候选/替代
│  │  ├─ plan.py                    M  metric_spec/metric_source
│  │  ├─ planner.py                 M  支持 derived spec
│  │  ├─ compiler.py                M  synthetic metric/ratio
│  │  ├─ validator.py               M
│  │  ├─ capability.py              M  表达式校验/RATIO_SELF_FILTER
│  │  ├─ executor.py                K
│  │  ├─ checks.py                  M
│  │  ├─ diagnosis.py               K
│  │  ├─ recovery.py                K
│  │  ├─ repair.py                  M  用 capability graph
│  │  ├─ insights.py                K
│  │  ├─ presentation.py            M  显示 provenance
│  │  ├─ records.py                 M  存 expr/provenance/index_version
│  │  ├─ analysis.py                M  模块动态发现
│  │  ├─ semantic/
│  │  │  ├─ loader.py               K
│  │  │  ├─ graph.py                K
│  │  │  └─ timeaxis.py             M  去硬编码中文
│  │  └─ model/
│  │     ├─ types.py                K
│  │     ├─ expr.py                 M  补 Filtered/EntityCount
│  │     ├─ model.py                K
│  │     ├─ validate.py             K
│  │     ├─ sql.py                  M  接进 compiler
│  │     └─ draft.py                D
│  ├─ datasets/
│  │  ├─ registry.py                K
│  │  ├─ context.py                 M  ctx.index
│  │  ├─ index.py                   A  semantic_index
│  │  ├─ publish.py                 M  写 index/读 reviewed draft
│  │  ├─ readiness.py               M  索引检查
│  │  ├─ health.py                  K
│  │  ├─ merge.py                   K
│  │  └─ manifest.py                K
│  ├─ core/
│  │  ├─ event_store.py             M  实现 EventLog
│  │  ├─ session.py                 K  会话边界
│  │  ├─ jobs.py                    K
│  │  ├─ trace.py                   K
│  │  ├─ logging.py                 K
│  │  ├─ controller.py              R  迁到 chat/controller.py
│  │  ├─ state.py                   R  迁到 chat/state.py
│  │  ├─ events.py                  R  迁到 chat/events.py
│  │  ├─ interfaces.py              D  拆到 agent/contracts
│  │  ├─ compactor.py               D
│  │  └─ policy.py                  D
│  ├─ llm/
│  │  ├─ client.py                  K
│  │  ├─ compose_answer.py          M  provenance/alternatives
│  │  ├─ analyze.py                 K
│  │  ├─ rule_parser.py             R  迁到 chat/agent/fallback.py，缩减
│  │  ├─ parse_intent.py            D  由 chat/agent 取代
│  │  ├─ schema_card.py             D  由 semantic_brief 取代
│  │  └─ propose_values.py          D
│  └─ tools/
│     └─ harness.py                 M  chat/ingest 共用只读工具
├─ web/
│  └─ src/
│     ├─ main.ts                    K
│     ├─ App.vue                    K
│     ├─ manage/
│     │  ├─ ManageApp.vue           M  拆 panel
│     │  ├─ UploadPanel.vue         A
│     │  ├─ DraftEditor.vue         A
│     │  ├─ ReviewQueue.vue         A
│     │  └─ PublishPanel.vue        A
│     ├─ components/
│     │  ├─ ChatPanel.vue           M
│     │  ├─ ResultCanvas.vue        M
│     │  ├─ ProvenanceBadge.vue     A
│     │  ├─ AlternativesCard.vue    A
│     │  ├─ RecordDrawer.vue        K
│     │  ├─ AnalysisView.vue        K
│     │  └─ ChartView.vue           K
│     ├─ stores/
│     │  ├─ chat.ts                 A  从 session.ts 拆
│     │  ├─ events.ts               A
│     │  ├─ results.ts              A
│     │  └─ session.ts              D  迁移后删
│     ├─ api.ts                     M
│     └─ types.ts                   M
└─ tests/
   ├─ agent/                        A  内核单测
   ├─ chat_agent/                   A  parser/tools/IR
   ├─ ingest_agent/                 A  fixture/replay/gates
   └─ integration/                  M  官方 8 题/非电商
~~~

## 3. 新增

| 文件/目录 | 作用 | 阶段 |
|---|---|---|
| app/agent/* | 通用 loop/契约/registry/budget/events/prompts/review | 1 |
| app/chat/agent/* | Chat Agent spec/tools/prompts | 3 |
| app/chat/turn/* | parse/plan/execute/compose/multi/analysis/interpretation | 3 |
| app/chat/controller.py | 会话状态机 | 3 |
| app/chat/state.py | SessionState/Slots | 3 |
| app/chat/events.py | session 事件类型 | 3 |
| app/ingest/agent/* | Ingest Agent spec/state/events/tools/gates/prompts | 1/4 |
| app/ingest/prepare.py | 复制/hash/打开工作簿 | 1 |
| app/datasets/index.py | semantic_index 构建/加载 | 1 |
| app/domain/query/metric.py | MetricSpec/AST lowering | 2 |
| app/domain/query/resolve.py | 口径优先级/候选/替代 | 2 |
| app/llm/semantic_brief.py | 检索 brief | 1 |
| app/llm/semantic_tools.py | parser 侧工具（或合入 chat/agent/tools） | 3 |
| web/src/manage/UploadPanel.vue 等 | 数据管理拆 panel | 5 |
| web/src/components/ProvenanceBadge.vue | 口径来源 | 3 |
| web/src/components/AlternativesCard.vue | 替代口径 | 3 |
| web/src/stores/chat.ts/events.ts/results.ts | 拆 store | 5 |
| tests/agent/* chat_agent/* ingest_agent/* | 新测试 | 1-4 |

## 4. 修改

| 文件 | 改什么 | 阶段 |
|---|---|---|
| app/factory.py | 装配 ChatAgent/IngestAgent/DatasetIndex | 1/3 |
| app/config/settings.py | 预算/flag/index 路径 | 1 |
| app/api/routes.py | 返回 provenance/alternatives；/index | 1/3 |
| app/api/uploads.py | ingest job + SSE | 1/4 |
| app/api/schemas.py | 新字段 | 3 |
| app/core/event_store.py | 实现通用 EventLog | 1 |
| app/core/session.py | 依赖 chat/controller | 3 |
| app/tools/harness.py | 暴露给 chat/ingest | 3 |
| app/domain/query/ir.py | 唯一契约；metric_spec/provenance | 2 |
| app/domain/plan.py | metric_spec/metric_source | 2 |
| app/domain/planner.py | IR/derived spec | 2 |
| app/domain/compiler.py | synthetic metric/ratio | 2 |
| app/domain/validator.py | synthetic metric | 2 |
| app/domain/capability.py | 表达式/RATIO_SELF_FILTER | 2 |
| app/domain/checks.py | derived ratio 提示 | 2 |
| app/domain/repair.py | 用 capability graph | 3 |
| app/domain/presentation.py | provenance | 3 |
| app/domain/records.py | 存 expr/provenance/index_version | 3 |
| app/domain/analysis.py | 模块动态发现 | 5 |
| app/domain/semantic/timeaxis.py | 去硬编码中文 | 5 |
| app/domain/model/expr.py | Filtered/EntityCount | 2 |
| app/domain/model/sql.py | 接进 compiler | 2 |
| app/datasets/context.py | ctx.index | 1 |
| app/datasets/publish.py | 写 index；读 reviewed draft | 1/4 |
| app/datasets/readiness.py | 索引检查 | 1 |
| app/ingest/semantic_draft.py | 变成 draft.structure 工具 | 4 |
| app/llm/compose_answer.py | provenance/alternatives | 3 |
| web/src/types.ts | provenance/alternatives | 3 |
| web/src/api.ts | /index、review、SSE | 3/4 |
| web/src/components/ChatPanel.vue | 来源 badge | 3 |
| web/src/components/ResultCanvas.vue | 来源/替代口径 | 3 |
| web/src/manage/ManageApp.vue | 拆 panel/review queue | 4/5 |

## 5. 删除

| 文件 | 原因 | 替代 |
|---|---|---|
| app/ingest/build_warehouse.py | 遗留电商建库 | prepare.py + agent |
| app/ingest/normalize.py | TABLE_SPECS 写死电商 | infer.py |
| app/ingest/catalog.py | 被 profile.py 取代 | profile.py |
| app/ingest/catalogs.py | 被 generic_pipeline._dim_values 取代 | tools/columns.py |
| app/ingest/quality.py | 被 quality_generic.py 取代 | quality_generic.py |
| app/ingest/drift.py | 只服务遗留建库 | manifest/index |
| app/ingest/bootstrap.py | 0 引用 | agent draft |
| app/ingest/generic_pipeline.py | 线性 pipeline | prepare.py + agent |
| app/ingest/llm_semantic.py | 一次性 refine | tools/metrics.py |
| scripts/ingest.py | 调遗留建库 | scripts/ingest_generic.py |
| app/core/controller.py | 迁到 chat/controller.py | chat/controller.py |
| app/core/state.py | 迁到 chat/state.py | chat/state.py |
| app/core/events.py | 迁到 chat/events.py | chat/events.py |
| app/core/interfaces.py | 拆到 agent/contracts.py | agent/contracts.py |
| app/core/compactor.py | 0 引用 | 无 |
| app/core/policy.py | 0 引用 | agent/budget.py |
| app/domain/model/draft.py | 未接线 | semantic_index/review |
| app/domain/policy.py | allow-all 占位 | 后续 policy 层 |
| app/domain/rewrite.py | 表层清洗 | chat/turn/parse.py |
| app/llm/parse_intent.py | 由 chat/agent 取代 | chat/agent |
| app/llm/schema_card.py | 由 semantic_brief 取代 | semantic_brief |
| app/llm/propose_values.py | 0 引用 | sample_values 工具 |
| web/src/stores/session.ts | 拆成 chat/events/results | 三个新 store |

## 6. 移动/重命名

| 现在 | 目标 | 说明 |
|---|---|---|
| app/core/controller.py | app/chat/controller.py | 保留状态机，缩到 <600 行 |
| app/core/state.py | app/chat/state.py | Slots/SessionState/reduce |
| app/core/events.py | app/chat/events.py | session 事件 |
| app/llm/rule_parser.py | app/chat/agent/fallback.py | 缩减为通用兜底 |
| app/ingest/generic_pipeline.py | app/ingest/prepare.py | 只保留准备阶段 |
| app/ingest/llm_semantic.py | app/ingest/agent/tools/metrics.py | 候选指标逻辑 |
| app/domain/analysis.py | 保持 | 改成数据驱动 |

## 7. 保留不动

- app/domain/executor.py、diagnosis.py、recovery.py、insights.py、lookup.py；
- app/domain/semantic/loader.py、graph.py；
- app/domain/model/types.py、model.py、validate.py；
- app/datasets/registry.py、health.py、merge.py、manifest.py；
- app/core/session.py、jobs.py、trace.py、logging.py；
- app/llm/client.py、analyze.py；
- app/api/sse.py；
- web/src/components/RecordDrawer.vue、AnalysisView.vue、ChartView.vue。

## 8. 迁移顺序

1. Phase 0：止血（白名单/子问题/自谓词过滤），不删代码。
2. Phase 1：app/agent 内核 + ingest agent skeleton + semantic_index。
3. Phase 2：QueryIR/MetricSpec 接进 plan/compiler。
4. Phase 3：chat/agent 切换；删 salvage；拆 controller。
5. Phase 4：ingest agent 完整化（tools/gates/review）。
6. Phase 5：web 拆组件；删遗留栈；更新文档。

每个 Phase 结束跑同一套回归：

- pytest；
- vue-tsc；
- 官方 8 题；
- 非电商退单 fixture；
- 事件回放。

## 9. 验收

- Q05 91.53%、Q06 1.999/4.019、Q07 49.03%/76；
- 非电商退单率 inferred 回答；
- 无 _salvage_*；
- 无静默 explain；
- 无自谓词过滤；
- brief <=4k；
- 常规 1 次 LLM；
- ingest 事件可回放；
- 删除文件后无 import 残留。
