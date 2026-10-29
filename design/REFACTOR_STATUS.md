# 重构进度：第一阶段

> 日期：2026-10-29
> 关联：REFACTOR_PLAN.md、FINAL_CODE_ARCHITECTURE.md

## 已完成

### Phase 0：止血

- ALLOWED_SLOT_KEYS 补齐 5 个字段：ratio_numerator、ratio_denominator、time_axis、share、limit_per_group。
- _validate_subquestions 不再把槽位非法静默改成 explain：保留 query + error，controller 记录 failed。
- _salvage_derived_filter 加闸门：
  - 比率指标不加；
  - 指标表达式已包含谓词时不加；
  - 问题同时出现两个标签时不加单值过滤。
- capability 增加 RATIO_SELF_FILTER：用比率自己的分子谓词过滤时拒绝，不再返回 100%。

### Phase 1：语义索引

- 新增 app/datasets/index.py：
  - build_index / build_index_for_context / write_index / load_index / validate_index；
  - 指标带 expr/scope/time_via/supported_dimensions/provenance；
  - 维度带 labels/values；
  - 表带 grain/rows/columns；
  - 关系、文档口径、时间轴。
- DatasetContext 新增 index_path/index；加载 semantic_index.json。
- publish 成功后生成 semantic_index.json（失败不阻断发布）。
- 新增 app/llm/semantic_brief.py：按问题检索 top-k，生成紧凑 brief（<=4k 字符）；中文按 name/synonym/label 子串打分，避免按字母序截断。
- LLMIntentParser 支持 index/use_brief；USE_SEMANTIC_BRIEF 默认开启（有 index 时用 brief，无 index 回退 schema card）。
- Chat 工具接入 parser：metric.describe 丰富口径，schema.search 兜底 unresolved 指标。
- 新增 GET /datasets/{id}/index。

### Phase 2：派生指标

- 新增 app/domain/query/metric.py：build_metric_spec（SUM/AVG/COUNT/COUNT_DISTINCT）。
- Slots 增加 metric_expr/metric_source；QueryPlan 增加 metric_spec/metric_source。
- planner 支持 derived spec；compiler 使用 plan.metric_spec；validator 允许 synthetic metric。
- 支持 AVG(review_score)、COUNT(DISTINCT ...) 这类未发布指标，不需要改 semantic.yaml。
- LLMIntentParser 接受 metric_expr；capability/planner/compiler/validator/pipeline/insights/presentation 都能解析 synthetic metric。
- 全链路 controller 测试：派生 AVG 指标可走完 parse→capability→plan→compile→execute→compose。
- QueryIR 成为 planner 入口：controller 用 plan_from_ir(QueryIR.from_slots(slots))，IR 含 metric_expr/metric_source。

### Agent 内核

- 新增 app/agent：
  - contracts：Tool/ToolResult/Decision/Issue/GateResult/ToolContext；
  - events：AgentEvent/JsonlEventLog；
  - registry：ToolRegistry；
  - budget：Budget；
  - controller：AgentController/AgentSpec；
  - prompts/review/artifacts/state 协议。
- 支持 FakeLLM/脚本决策、事件回放、预算耗尽、gate 拒绝。

### Ingest Agent 骨架

- 新增 app/ingest/agent：
  - state：IngestState + apply 事件；
  - tools：files.list、table.profile、grain.detect、metric.propose、metric.probe、ratio.propose；
  - gates：tables_profiled、grain_unique、metric_probe；
  - artifacts：写 ingest/draft.json、issues.json；
  - spec：build_ingest_agent；
  - decider：LLMDecider（一次一个 Decision，禁止写 SQL）；
  - service：run_ingest_agent（LLMDecider + controller + 事件日志）。
- 支持 CSV/TSV/XLSX：profiling、grain 唯一性、指标 proposal/probe、ratio proposal/probe。
- API：POST /api/uploads/{id}/agent-profile 启动 job；GET /api/jobs/{id}/events 推送 SSE。
- Review API：GET/POST /api/jobs/{id}/review，读写 ingest/review.json（accept/reject/waive）。
- Review Apply：POST /api/jobs/{id}/review/apply 生成 ingest/semantic_draft.json + approved_metrics.json（候选草稿）。
- Materialize：POST /api/jobs/{id}/materialize 生成 draft 数据集（warehouse.duckdb / binding.yaml / catalog.json / semantic.yaml），可在数据源列表中看到并查询。
- ManageApp 增加“Agent 画像 / 审核”面板：启动 agent-profile、轮询 job、展示表/指标/probe，支持接受/拒绝/应用审核结果/生成数据集草稿。
- 结果画布增加 provenance badge：推断口径 / 用户口径 / 已确认口径。
- 测试用合成数据：orders.csv + channels.csv，含成交/退单，退单率=0.5。

### 死代码清理

已删除（0 引用）：

- app/core/compactor.py
- app/core/policy.py
- app/ingest/bootstrap.py
- app/llm/propose_values.py

### Chat 侧工具

- 新增 app/chat/agent/tools.py：
  - schema.search、metric.describe、column.profile；
  - 只读语义索引，不扫原始表。
- 尚未接入 controller/parser（见下）。

## 测试

- 新增：
  - tests/unit/test_phase0_guards.py（7）
  - tests/unit/test_semantic_index.py（3）
  - tests/unit/test_derived_metric.py（2）
  - tests/unit/test_agent_kernel.py（4）
  - tests/unit/test_chat_agent_tools.py（3）
  - tests/integration/test_ingest_agent.py（1）
  - tests/integration/test_ingest_agent_llm.py（1）
  - tests/integration/test_derived_metric_turn.py（1）
  - tests/integration/test_agent_api.py（1）
  - tests/unit/test_ingest_decider.py（4）
- 全量：263 passed / 0 failed / 92 skipped。
- 未使用电商数据集；测试数据为临时生成的 CSV/DataFrame。

## 尚未接线

1. Chat controller 拆分：
   - 仍保留 core/controller.py 作为状态机；
   - app/chat/agent 工具已接入 parser；
   - semantic_brief 已默认开启（无 index 时回退 schema card）；
   - 还没有拆成 app/chat/turn/*。
2. Ingest Agent 生产化：
   - job/SSE、review API、review/apply、materialize、ManageApp 审核面板已接；
   - materialize 已能生成 draft warehouse/binding/semantic；
   - 还没有替换 generic_pipeline（目前是审计/画像旁路）。
3. 发布链路：
   - semantic_index 已写、已加载，但 publish 的索引生成还没有专门集成测试。
4. 前端：
   - Review queue 已加到 ManageApp；
   - provenance badge 已加到结果页；
   - alternatives 还没有展示组件。

## 下一步

1. 补 publish -> index -> context 的集成测试。
2. 拆 chat/controller 为 app/chat/turn/*，逐步删 6 个 salvage。
3. 前端 alternatives 展示。
4. Ingest Agent 多表关系/joins。
5. 把 Agent materialize 接入正式发布链路。

## 运行

~~~powershell
cd E:\10.23\project_1.0
.\.venv\Scripts\python.exe -m pytest -q
~~~
