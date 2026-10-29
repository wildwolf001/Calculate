# 明账重构方案：离线建语义，在线编译查询

> 版本：2026-10-29 v1
> 依据：2026-10-29 关于 rewriter / 语义层 / agent / 推断口径 的讨论
> 现状地图：PROJECT_MAP.md

## 0. 一句话目标

把“文本 → 语义”这一段从多层启发式博弈，改成一次语义决策 + 一次确定性校验：

- 离线：用解析模块 + agent 产出一份可验证、带置信度的 semantic_index.json；
- 在线：LLM 只读这份索引 + 用户问题，输出 QueryIR（可引用已定义指标，也可组合 MetricExpr）；
- SQL 仍然只由编译器生成，LLM 永远不写 SQL。

这不是推翻原设计，而是补完原设计：

1. 原设计假设“语义层足够丰富，parser 只需选一个 metric”；
   实际语义层太薄，选不到就只能猜。
2. 原设计假设“解析后加确定性兜底会更稳”；
   实际兜底在改语义（Q05 的 100%、Q07 的 explain）。

核心资产全部保留：事件溯源、语义层、capability graph、compiler、executor、checks、查询记录。

---

## 1. 目标架构

~~~text
┌──────────────────── 离线（慢，一次） ────────────────────┐
│ 原始 Excel/CSV                                            │
│   → 解析模块（header/infer/keys/relations/profile/quality）│
│   → agent 编排 + probe 验证 + 文档解析                     │
│   → semantic_index.json（表/列/关系/指标/维度/口径/置信度）│
│   → 人工确认 → 发布                                        │
└───────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────── 在线（快，每轮） ────────────────────┐
│ 用户问题 + 检索到的 semantic brief（2~4k tokens）          │
│   → LLM：选已定义指标，或组合 MetricExpr                   │
│   → QueryIR                                               │
│   → capability / validator（fanout、grain、ratio、scope）  │
│   → compiler → 参数化 SQL → 只读执行                       │
│   → 回答 + 口径来源（documented/user/inferred）+ 替代口径  │
└───────────────────────────────────────────────────────────┘
~~~

三条原则：

1. SQL 只由 compiler 生成，LLM 输出 IR/表达式。
2. 物理画像离线做，业务口径在线定；在线只读索引，不扫原始表。
3. 语义只有一个 owner：LLM；确定性层只做“能不能算、算得对不对”，不做“猜意思”。

---

## 2. 数据契约

### 2.1 semantic_index.json

发布时生成，放在 data/datasets/<id>/semantic_index.json。

~~~json
{
  "version": 1,
  "dataset_id": "ds_xxx",
  "data_version": "sha256:...",
  "semantic_version": 3,
  "tables": {
    "t_001": {
      "rows": 11890,
      "grain": ["c01"],
      "time_columns": ["c04", "c07"],
      "columns": [
        {"name": "c01", "label": "order_id", "type": "varchar", "role": "id",
         "distinct": 11890, "null_rate": 0.0, "samples": ["O00001"]}
      ]
    }
  },
  "relations": [
    {"from": "t_006", "to": "t_003", "left": "c02", "right": "c01", "card": "N:1"}
  ],
  "metrics": {
    "m_doc_延迟订单": {
      "name": "延迟订单",
      "type": "aggregate",
      "fact": "t_003",
      "expr": "COUNT(DISTINCT CASE WHEN t_003.c07 > t_003.c08 THEN t_003.c01 END)",
      "scope": {"t_003.c03": "delivered"},
      "time_via": "t_003.c04",
      "unit": "单",
      "supported_dimensions": ["..."],
      "provenance": "documented",
      "confidence": 0.8
    }
  },
  "dimensions": {
    "d_doc_t_003_c07_c08": {
      "name": "是否延迟",
      "kind": "derived",
      "expr": "t_003.c07 > t_003.c08",
      "labels": {"true": "延迟", "false": "准时"},
      "values": ["延迟", "准时"]
    }
  },
  "docs": [{"term": "延迟率", "formula": "延迟的已送达订单数 / 已送达订单数", "source": "metrics.md"}],
  "issues": []
}
~~~

要求：

- 指标必须带表达式、scope、time_via、fact、支持维度，不能只有中文名；
- 维度必须带真实值/样本，不能只有同义词；
- 所有推断项带 provenance（documented / user / inferred）和 confidence；
- 索引是查询时唯一读模型，schema_card 从它生成。

### 2.2 QueryIR

~~~json
{
  "metric": {
    "kind": "defined | derived",
    "id": "m_doc_延迟订单",
    "expr": {"kind": "ratio", "numerator": {}, "denominator": {}},
    "label": "退单率",
    "provenance": "documented | user | inferred",
    "confidence": 0.72
  },
  "dimensions": ["d_004_008_c02"],
  "filters": {"d_004_003_c03": {"op": "eq", "value": "delivered"}},
  "time": {"start": "2025-05-01", "end": "2025-06-01"},
  "having": [{"metric": "order_count", "op": "gte", "value": 10}],
  "order_by": [{"field": "value", "direction": "desc"}],
  "limit": 1,
  "alternatives": [
    {"formula": "退单数量 / (成交单数 + 退单数量)", "label": "按全部下单"}
  ]
}
~~~

规则：

- 有已确认口径：kind=defined，不允许 LLM 改公式；
- 用户显式给公式：kind=derived, provenance=user，提示与已发布口径的差异；
- 没有定义：kind=derived, provenance=inferred，答案必须显示公式；
- 歧义大：alternatives 带多个公式，答案展示或让用户选。

---

## 3. 分阶段实施

每个阶段都能独立发布、独立回滚；不要一次性替换主链路。

> 说明：Ingest Agent 是独立 bounded context，Phase 4 只是落地阶段编号，不代表优先级。建议在 Phase 0 之后立即启动它的 skeleton，与 Phase 1 的索引并行；索引先由现有 semantic.yaml 构建，agent 产出后再替换。详细设计见 INGEST_AGENT_DESIGN.md。

### Phase 0：止血 + 清场

目标：先让 Q05/Q06/Q07 不再被已知 bug 伤害，同时删掉确定无用的代码。

- 修 ALLOWED_SLOT_KEYS：补 ratio_numerator、ratio_denominator、time_axis、share、limit_per_group；加测试“prompt schema 的 slot key 是白名单子集”。
- 修 _validate_subquestions：slots 校验失败不再静默改 explain，保留 query + error，由 controller 显式处理。
- 给 _salvage_derived_filter 加最小闸门：
  - 指标是 ratio，或指标表达式已包含该谓词，不加；
  - 问题里同时出现“延迟/准时”，不加单值过滤；
  - 更推荐：直接删除，Phase 3 由 parser 负责。
- 当前数据集把“平均评分”重新勾选发布（数据操作，不是代码）。
- 删除死代码/遗留栈（见第 4 节），更新 scripts/ingest.py。

验收：Q05 从 100% 回到 91.53%；Q06 至少能出两组；Q07 不再变 explain；pytest 绿。

### Phase 1：语义索引（新增，不改行为）

目标：让在线能看到表达式/列/关系/样本，而不是 12.5k 的 schema card。

新增：

- app/datasets/index.py：build_index(ctx)、load_index(path)、validate_index(index)；
- app/llm/semantic_brief.py：按问题检索 top-k 指标/维度/列，生成 2~4k tokens 的 brief；
- tests/unit/test_semantic_index.py、test_semantic_brief.py。

修改：

- app/datasets/publish.py：发布成功后写 semantic_index.json；
- app/datasets/context.py：加载 index，暴露 ctx.index；旧数据集回退到从 sem 构建；
- app/datasets/readiness.py：检查索引存在/版本/字段完整；
- app/api/routes.py：GET /datasets/{id}/index（调试）；/schema-card 改为从 brief 生成。

验收：同一数据集索引生成两次内容一致；brief 从 12.5k 降到 ≤4k 字符；主链路行为不变。

### Phase 2：表达式 IR（新增能力，parser 暂不改）

目标：让 plan/compiler 能执行“组合出来的指标”，先不接 LLM。

新增：

- app/domain/query/metric.py：MetricSpec（id/label/fact/expr/scope/time_via/unit/provenance/confidence），lower_metric_expr(expr, sem)；
- app/domain/query/resolve.py：口径优先级、候选公式生成、歧义/替代；
- 单测：test_metric_spec.py、test_derived_compiler.py。

修改：

- app/domain/model/expr.py：确保 FilteredExpr、EntityCountExpr 可 lowering；
- app/domain/model/sql.py：补 FilteredExpr、EntityCountExpr 的 compile_metric_expr；
- app/core/state.py：Slots 增加 metric_expr、metric_source、alternatives；
- app/domain/plan.py：QueryPlan 增加 metric_spec: dict | None、metric_source；
- app/domain/planner.py：有 metric_expr 时构造 metric_spec，不走 sem.metric；
- app/domain/compiler.py：_Builder 优先用 plan.metric_spec；_compile_metric_ratio 支持 synthetic metric specs；
- app/domain/capability.py：校验表达式列存在、聚合合法、fanout/grain、ratio 对齐；新增 RATIO_SELF_FILTER 拒绝“用分子谓词过滤比率”；
- app/domain/validator.py：允许 synthetic metric，校验 spec 完整性；
- app/domain/checks.py：derived ratio 的 fanout/空值提示。

验收：单元测试直接构造 slots，能跑：

- AVG(t_006.c03) GROUP BY 是否延迟，结果 1.999 / 4.019；
- 退单数量 / 成交单数，单值；
- 延迟率 + 是否延迟=延迟 被 RATIO_SELF_FILTER 拒绝，而不是 100%。

### Phase 3：解析链切换（行为变化）

目标：parser 从“选 metric_id”变成“读 brief + 输出 IR”；controller 删除 6 个 salvage。

新增：

- app/llm/semantic_tools.py：search_schema、describe_metric、profile_column、sample_values、validate_ir；
- app/core/turn/parse.py、multi.py、analysis.py、interpretation.py：从 controller 拆出的 turn 模块。

修改：

- app/llm/prompts/parse_intent_system.txt：输出 schema 改为 metric + metric_expr + provenance + alternatives；规则从 18 条压到 8~10 条；
- app/llm/parse_intent.py：用 brief/tools，不再用静态 schema_card；校验失败一次修复，不再 regex 兜底；
- app/core/controller.py：_handle_parsing 缩成 rewrite → brief → parse → validate → 一次 repair；删除 _salvage_derived_filter、_salvage_time_axis、_salvage_share、_salvage_ratio_expression、_drop_implicit_dims_for_comparison、_infer_limit_per_group、_match_metric_text；
- app/domain/rewrite.py：只保留 smalltalk/空白清洗，或删除并入 parse；
- app/llm/compose_answer.py：答案增加“口径来源/公式/替代口径”；推断口径必须显示公式；
- app/domain/records.py：查询记录存 metric_expr、provenance、index_version；
- app/api/routes.py：结果返回 provenance/alternatives。

验收：官方 8 题 + Q05/Q06/Q07；无静默 explain；无自谓词过滤；上下文 ≤4k tokens；解析 1 次（失败才 2 次）。

### Phase 4：Ingest Agent（独立 bounded context）

目标：ingest 不再是一条线性 pipeline + 一次 LLM refine，而是一个事件驱动、工具化、可审计的建模 agent。详细设计见 INGEST_AGENT_DESIGN.md。

它不是 generic_pipeline 后面的 enrich，而是 ingest 的主控制器：

- LLM 决定下一步（看样本、检测表头、提 grain、提关系、提指标、跑 probe、问人）；
- 工具执行确定性操作；
- 每个动作写事件，reducer 可回放/恢复；
- 质量 gate 阻塞 finalize，人审回答后继续；
- 输出 draft + index + issues + events + review。

新增：

- app/ingest/agent/：controller.py、state.py、events.py、tools.py、gates.py、prompts/
- app/ingest/probes.py：probe 模板（grain/join/metric/consistency）
- tests/integration/test_ingest_agent.py、tests/fixtures/messy_workbook

修改：

- app/ingest/generic_pipeline.py：缩成 prepare（复制/hash/打开）+ 启动 agent controller；
- app/api/uploads.py：agent job + SSE 进度；
- app/datasets/publish.py：只接受 agent 产出的 reviewed draft；
- web/src/manage/ManageApp.vue：review queue（证据/选项/接受/拒绝/waive）。

验收：脏表 fixture（多行表头/合计行/一表多块/重复键/退单）能产出正确 grain/关系/指标；probe 通过；事件可回放；预算耗尽输出 partial draft。

### Phase 5：清理与前端

- 删除遗留电商栈、硬编码业务词；
- analysis.MODULE_META 改为按指标角色动态发现（规模/趋势/结构/比率/实体），不再写死“履约/口碑”；
- resolver.FAMILY_DIMS={"product"} 删除，改用维度 kind/entity；
- web/src/stores/session.ts 的电商欢迎语/建议问题改为从 /meta 读取；
- README / PROJECT_MAP 更新。

---

## 4. 文件级增删改

### 4.1 新增

| 文件 | 职责 | 阶段 |
|---|---|---|
| app/datasets/index.py | 构建/加载/校验 semantic_index.json | 1 |
| app/llm/semantic_brief.py | 按问题检索 top-k，生成紧凑 brief | 1 |
| app/domain/query/metric.py | MetricSpec、AST lowering | 2 |
| app/domain/query/resolve.py | 口径优先级、候选公式、替代/歧义 | 2 |
| app/llm/semantic_tools.py | parser 侧只读工具 | 3 |
| app/core/turn/parse.py | 单轮解析编排 | 3 |
| app/core/turn/multi.py | 多问题执行 | 3 |
| app/core/turn/analysis.py | 经营分析 | 3 |
| app/core/turn/interpretation.py | 多解释执行 | 3 |
| app/ingest/agent/（controller/state/events/tools/gates/prompts） | 事件驱动 ingest agent | 4 |
| app/ingest/probes.py | grain/join/metric/consistency probe | 4 |
| tests/unit/test_semantic_index.py | 索引契约 | 1 |
| tests/unit/test_semantic_brief.py | brief 预算/检索 | 1 |
| tests/unit/test_metric_spec.py | 表达式 lowering | 2 |
| tests/unit/test_derived_compiler.py | 派生指标编译 | 2 |
| tests/unit/test_parser_schema_contract.py | prompt/白名单一致 | 0/3 |
| tests/unit/test_subquestion_no_downgrade.py | 子问题不静默 explain | 0 |
| tests/integration/test_inferred_metric.py | 推断口径/provenance | 3 |
| tests/integration/test_non_ecommerce_refund.py | 成交/退单 fixture | 4 |

### 4.2 删除

| 文件/组 | 原因 | 前置条件 |
|---|---|---|
| app/ingest/build_warehouse.py | 遗留电商建库 | 用 generic_pipeline 替换 scripts |
| app/ingest/normalize.py | TABLE_SPECS 写死电商表 | 同上 |
| app/ingest/catalog.py | 被 profile.build_catalog 取代 | 同上 |
| app/ingest/catalogs.py | 被 generic_pipeline._dim_values 取代 | 同上 |
| app/ingest/quality.py | 被 quality_generic 取代 | 同上 |
| app/ingest/drift.py | 只服务遗留建库 | 同上 |
| app/ingest/bootstrap.py | 0 引用 | 无 |
| scripts/ingest.py | 调用遗留建库 | 新增 generic CLI |
| app/core/compactor.py | 0 引用 | 无 |
| app/core/policy.py | 0 引用 | 无 |
| app/llm/propose_values.py | 0 引用 | 无 |
| app/domain/model/draft.py | 未接线 | 若 Phase 4 不用则删 |
| app/domain/rewrite.py | 只做表层清洗，合并进 parse | Phase 3 |
| app/llm/schema_card.py | 被 semantic_brief 取代 | Phase 1 |
| app/domain/policy.py | allow-all 占位 | 实现或删除 |

不要删：model/expr.py、model/sql.py、query/ir.py，它们是 Phase 2 的零件，只是没接线。

### 4.3 修改

| 文件 | 改什么 | 阶段 |
|---|---|---|
| app/llm/parse_intent.py | 输出 IR；brief/tools；删静态白名单；一次修复 | 3 |
| app/llm/prompts/parse_intent_system.txt | 新 schema；规则瘦身 | 3 |
| app/core/controller.py | 删 6 个 salvage；拆 turn 模块 | 0/3 |
| app/core/state.py | Slots 加 metric_expr/metric_source/alternatives | 2 |
| app/domain/plan.py | QueryPlan 加 metric_spec/metric_source | 2 |
| app/domain/planner.py | 支持 derived spec | 2 |
| app/domain/compiler.py | 解析 metric_spec；ratio 支持 synthetic | 2 |
| app/domain/capability.py | 表达式校验；RATIO_SELF_FILTER | 2 |
| app/domain/validator.py | synthetic metric 校验 | 2 |
| app/domain/checks.py | derived ratio 提示 | 2 |
| app/domain/records.py | 存 expr/provenance/index_version | 3 |
| app/domain/presentation.py | 派生指标显示标签/来源 | 3 |
| app/datasets/context.py | ctx.index | 1 |
| app/datasets/publish.py | 写索引/候选/问题 | 1/4 |
| app/datasets/readiness.py | 索引检查 | 1 |
| app/api/routes.py | /index、brief、provenance | 1/3 |
| app/api/uploads.py | 文档应用后重建索引 | 1 |
| app/llm/compose_answer.py | 口径来源/公式/替代 | 3 |
| app/domain/analysis.py | 模块动态发现 | 5 |
| app/domain/resolver.py | 删 FAMILY_DIMS | 5 |
| web/src/types.ts | provenance/alternatives | 3 |
| web/src/stores/session.ts | 结果携带口径来源；欢迎语从 meta | 3/5 |
| web/src/components/ResultCanvas.vue | 来源 badge/替代口径 | 3 |
| web/src/manage/ManageApp.vue | 候选指标确认 | 4 |

---

## 5. 测试计划

必跑回归：

- pytest -q；
- vue-tsc --noEmit；
- 官方 8 题（Q01–Q08）；
- 当前数据集 Q05/Q06/Q07。

新增用例：

| 用例 | 断言 |
|---|---|
| test_q05_no_self_filter | 延迟率 SQL 分母 = 已送达 59，不是 54；结果 91.53% |
| test_q06_two_groups | 延迟 1.999 / 准时 4.019 |
| test_q07_ranking | 两个子问题都执行；49.03% / 76 |
| test_subquestion_ratio_keys | 带 ratio_numerator 的子问题不变 explain |
| test_prompt_schema_subset | prompt schema keys 是 Pydantic schema 子集 |
| test_derived_avg_metric | AVG(review_score) 可 plan/compile/execute |
| test_derived_ratio_refund | 退单/成交 可执行，provenance=inferred |
| test_ratio_self_filter_refused | RATIO_SELF_FILTER，不返回 100% |
| test_brief_budget | brief ≤4k 字符；top-k 含目标指标 |
| test_index_deterministic | 两次构建 hash 一致 |
| test_records_replay_derived | 存 expr 后重跑一致 |

---

## 6. 验收标准

1. Q05：广州美妆个护商家004，91.53%（54/59），不是 100%。
2. Q06：延迟 1.999、准时 4.019，两组都出。
3. Q07：两个排名都执行；全量口径 49.03%（76/155）、76 单。
4. 非电商：退单率无定义时回答“按退单/成交=12.4%，替代 11.0%”，带 inferred 标记。
5. 解析上下文 ≤4k tokens；常规 1 次 LLM；工具 ≤2 次。
6. 无静默 explain；无自谓词过滤；无 _salvage_*。
7. pytest 绿；vue-tsc 绿；索引构建确定性。

---

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 索引与 semantic.yaml 不一致 | 发布时同一事务生成；index.semantic_version 校验 |
| 派生指标口径漂移 | provenance + 公式展示；用户可确认/固化 |
| LLM 组合出 fanout 表达式 | capability/validator 拦截；FANOUT_UNSAFE |
| 在线变慢 | brief 缓存；工具只读索引；执行一次 |
| 迁移期双轨 | 每阶段 feature flag；旧路径保留一个版本 |
| 离线 agent 幻觉 | probe 验证 + 人工确认；不直接发布 |

Feature flags：

- SEMANTIC_INDEX=0|1
- DERIVED_METRICS=0|1
- PARSER_MODE=legacy|brief
- INGEST_AGENT=0|1

每个 flag 默认关闭新路径，验收后逐数据集打开。

---

## 8. 不建议做的事

1. 不要让 LLM 直接写 SQL——即使看起来更快，也会丢掉 fanout/ratio/审计。
2. 不要把物理画像（grain/关系/类型）搬到查询时——慢且不稳定。
3. 不要继续加 _salvage_*——每加一个，语义就多一个 owner。
4. 不要长期保留 Slots/IR/Model 三套表示——Phase 2 后以 QueryIR 为准。
5. 不要为了兼容旧测试保留遗留电商栈——用 fixture 替代。

---

## 9. 建议的第一步

先做 Phase 0，然后并行两条线：

- 查询线：Phase 1，发布时生成 semantic_index.json，让 brief 从 12.5k 降到 4k；不改 parser/compiler。
- 数据线：Ingest Agent skeleton（events/state/reducer/controller + open/sample/header/profile/grain 五个工具），先跑脏表 fixture，不接发布。

两条线在 semantic_index.json 汇合：index 先由现有 semantic.yaml 构建，agent 产出 draft 后替换。这样查询侧先拿到更小的上下文，数据侧同时把质量 gate 建起来，不会又做成半截重构。

然后再做 Phase 2 的表达式 IR，最后才切换 parser。这样每一步都能单独验收，不会又做成半截重构。
