# Ingest Agent 设计：事件驱动的数据建模 agent

> 版本：2026-10-29 v1
> 关联：REFACTOR_PLAN.md Phase 4
> 定位：ingest 的主控制器，不是 generic_pipeline 后面的 enrich

## 0. 为什么需要它

现在的 ingest 是一条线性 pipeline：

~~~text
上传 → 读文件 → 检测表头 → 推断类型 → 检测 grain → 推断关系
→ 画像/质量 → 草稿 → 一次 LLM refine → 文档 → 发布
~~~

问题：

- 每一步都被写死，遇到脏表（多行表头、合计行、合并单元格、一表多块）只能靠启发式；
- LLM 只在最后被调用一次，看不到中间证据，也不能回头改前面的判断；
- 错了没有“重试/验证”的闭环，只能靠人肉发现；
- 决策没有事件记录，无法回放、无法解释“为什么把这一列当 grain”。

目标：把 ingest 变成一个**完整的 agent**：

- LLM 决定下一步做什么；
- 工具执行确定性操作；
- 每个动作写事件，reducer 可回放/恢复；
- 每个候选都要过 probe/质量 gate；
- 人可以回答问题、确认高风险决策；
- 最终产出一份可审计的语义草稿 + 索引。

## 1. 两个 agent

| | Chat Agent（在线） | Ingest Agent（离线） |
|---|---|---|
| 目标 | 回答问题 | 建语义模型 |
| 输入 | 用户问题 + 语义索引 | 原始文件 + 文档 |
| 工具 | 检索、校验、执行 | 读样本、检测表头、提 grain、提关系、probe |
| 输出 | QueryIR + 答案 | semantic_draft + semantic_index + issues |
| 事件 | session events | ingest events |
| 预算 | 每轮 1~2 次 LLM | 每数据集 30~60 步 |
| 人工 | 澄清一次 | 审核队列 |

两者共享同一套模式：事件溯源、reducer、ToolResult、bounded loop。不要为 ingest 另造一套框架。

## 2. 总体流程

~~~text
上传文件
  → prepare（复制、hash、打开工作簿、列出 sheet）
  → Ingest Agent loop
       observe（当前状态 + 上一个工具结果）
       decide（LLM：调工具 / 问人 / finalize）
       act（确定性工具）
       verify（probe / gate）
       emit（事件）
  → gates 全过 / 预算耗尽 / 需要人审
  → semantic_draft.json + semantic_index.json + issues + events
  → 人审确认
  → publish
~~~

## 3. 事件模型

事件追加到 data/datasets/<id>/ingest/events.jsonl，一行一个 JSON。

~~~json
{"seq": 12, "ts": "...", "type": "grain.proposed",
 "payload": {"table": "t_003", "columns": ["c01"], "confidence": 0.93,
             "evidence": {"rows": 11890, "distinct": 11890, "duplicates": 0}},
 "caused_by": 11, "trace_id": "..."}
~~~

核心事件：

| 事件 | 作用 |
|---|---|
| ingest.started | 文件、选项、数据 hash |
| file.discovered | 文件路径、类型、大小 |
| workbook.opened | sheet 列表、维度 |
| sheet.profiled | 行数、列数、空行、合并单元格 |
| header.detected | 表头行、置信度、证据 |
| totals.detected | 合计行、置信度、证据 |
| table.split | 一表多块时的切分范围 |
| column.profiled | 类型、空值率、distinct、样本、min/max |
| column.role.set | measure/dimension/id/time/pii |
| grain.proposed | 主键候选、唯一性证据 |
| relation.proposed | from/to/left/right/card、证据 |
| metric.proposed | 名称、表达式、fact、scope、time_via |
| probe.started / probe.result | 指标/关系/一致性 probe |
| doc.parsed / doc.resolved | 文档规则、列解析 |
| issue.raised | 阻断/警告、选项 |
| human.requested / human.answered | 人审问题与回答 |
| decision.made | LLM 的决策、理由、工具参数 |
| draft.updated | 草稿 revision |
| ingest.completed / ingest.failed | 终止 |

reducer：

~~~text
reduce(events) -> IngestState
~~~

要求：

- 纯函数、无 IO、无模型调用；
- 同一事件流永远得到同一状态；
- 进程重启后从最后一个 seq 恢复；
- UI 通过 SSE 订阅事件，展示进度和证据。

## 4. 状态

~~~text
IngestState
  dataset_id, data_version
  phase: discover | profile | model | verify | review | done | failed
  files[], sheets[]
  tables[]: {id, sheet, range, header_row, rows, grain, time_columns}
  columns[]: {table, name, label, type, role, pii, null_rate, distinct, samples}
  relations[]: {from, to, left, right, card, confidence, evidence}
  metrics[]: {id, name, expr, fact, scope, time_via, unit, provenance, confidence}
  issues[]: {id, severity, code, message, options, blocking}
  gates: {grain: pass|fail, join: ..., metric: ..., docs: ...}
  budget: {steps_used, tokens_used, started_at, max_steps, max_tokens}
  pending_human: {id, question, options} | null
  decisions[]: {seq, action, rationale}
  draft_revision: int
~~~

## 5. 工具

所有工具返回统一结构：

~~~json
{"ok": true, "facts": {}, "evidence": {}, "issues": [], "artifacts": {}}
~~~

### 5.1 只读检查

| 工具 | 输入 | 输出 |
|---|---|---|
| files.list | upload dir | 文件、hash、类型 |
| workbook.open | file | sheet 列表、维度 |
| sheet.sample | sheet, n | 前 n 行原始值 |
| sheet.shape | sheet | 行数、列数、空行、合并区 |
| header.detect | sheet | 表头行候选、置信度、证据 |
| totals.detect | sheet | 合计行候选、置信度 |
| table.split | sheet | 一表多块的切分范围 |
| column.profile | table, column | 类型、空值、distinct、样本、min/max |
| column.type | table, column | 推断类型、候选类型、证据 |
| column.role | table, column | measure/dimension/id/time/pii |
| grain.detect | table | 主键候选、唯一性、重复率 |
| relations.infer | tables | N:1/1:1 候选、join key、置信度 |
| join.probe | left, right, card | 实际行数、distinct、cardinality 是否匹配 |
| metric.probe | expr, fact, scope, time_via | 执行结果、空值、范围、是否 0..1 |
| consistency.check | table, rules | 合计、状态分布、日期范围 |
| docs.parse | text | 规则、公式、表/列引用 |
| docs.resolve | rule, schema | 解析到 table.column、冲突 |

### 5.2 确定性变换

| 工具 | 作用 |
|---|---|
| table.set_header | 确认表头行 |
| rows.drop | 删除标题/合计/空行 |
| column.set_role | 设置 measure/dimension/id/time/pii |
| column.set_pii | 标记敏感列 |
| table.set_grain | 确认主键 |
| relation.set_card | 确认 N:1/1:1 |
| metric.upsert | 写入候选指标 |
| issue.raise | 记录阻断/警告 |

### 5.3 写入

| 工具 | 作用 |
|---|---|
| draft.write | 写 semantic_draft.json / binding |
| index.build | 写 semantic_index.json |
| review.write | 写待确认问题 |

原则：

- 没有“任意 SQL”工具；probe 是模板化的，参数化，只读；
- 工具结果截断后给 LLM，避免把整表塞进上下文；
- 每个工具的参数和结果都进事件，保证可复现。

## 6. 质量 gate

agent 不能自己宣布“完成”，必须过 gate：

| Gate | 检查 | 阻断条件 |
|---|---|---|
| G1 GRAIN_UNIQUE | COUNT(*) = COUNT(DISTINCT key) | 不唯一且未选复合键 |
| G2 JOIN_CARDINALITY | probe 的 distinct/rows 符合声明的 N:1/1:1 | 实际 1:N 却声明 N:1 |
| G3 METRIC_PROBE | 候选指标可执行、非空、ratio 在 0..1、key 无空 | 执行失败/比例越界 |
| G4 DOC_RESOLVE | 文档每条规则都能落到真实列/公式 | 有规则无法解析 |
| G5 COLUMN_COMPLETE | 每列有 type/role/PII | 有列未定角色 |
| G6 TABLE_TIME | 每表有时间轴或显式 none | 时间列不明 |
| G7 TOTALS_RECONCILE | 合计行已排除；有合计时能对上 | 合计行混入明细 |
| G8 STATUS_VALUES | 状态/分类列取值一致 | 同义值未归一 |

- 阻断问题未解决时，finalize 只能产出 needs_review，不能发布；
- 人可以在 review 里 waive，但必须写理由，记为事件；
- 每个 gate 的结论都要有 evidence（行数、distinct、样本、SQL 结果摘要）。

## 7. LLM 决策循环

~~~text
while not terminal:
    state = reduce(events)
    if gates.pass and no blocking issues:
        finalize()
        break
    if budget.exhausted or loop_detected:
        finalize(partial=True)
        break
    summary = compact(state, last_tool_result)
    action = llm.decide(summary, tool_catalog)
    if action.kind == "tool":
        result = execute(action.tool, action.args)
        emit(action.tool + ".result", result)
    elif action.kind == "ask_human":
        emit("human.requested", action.question)
        break
    elif action.kind == "finalize":
        if gates.pass: emit("ingest.completed")
        else: emit("issue.raised", blocking)
    emit("decision.made", action)
~~~

Prompt 原则：

1. 只能基于工具返回的事实，不 invent 列/值/关系；
2. 每个 proposal 必须带 evidence；
3. 优先 probe，不要“看起来像”；
4. 一次只做一个动作；
5. 不确定时 ask_human，不猜；
6. 不允许跳过 gate。

Loop 防护：

- 同一 tool + 相同参数连续两次 → 注入“换一种方法/问人”；
- 单表最多 N 次 profile，超过则升级到人审；
- 总步数/ token 上限。

## 8. 人审

agent 可以主动问人：

~~~json
{"type": "human.requested",
 "payload": {"id": "h1",
             "question": "成交单数是否包含已退单？",
             "options": ["不包含，退单率=退单/成交",
                         "包含，退单率=退单/(成交+退单)"],
             "evidence": {"total_orders": 1240, "completed": 1086, "refunded": 154}}}
~~~

规则：

- 高风险决策必须人审：grain、join cardinality、指标分母、PII、文档冲突；
- 低风险可自动通过：同义词、显示名、单位；
- 人的回答写事件，agent 继续，不重启；
- review 队列在 ManageApp 展示，支持接受/修改/拒绝。

## 9. 预算与可复现

- 每数据集：max steps 60、max tokens 200k、max wall time 10min；
- 预算耗尽：输出 partial draft + issues，状态 needs_review；
- 事件里存：tool 参数/结果、LLM decision、prompt_version、agent_version、data_hash；
- 回放：只用事件重建状态，不再调 LLM；
- 同一输入 + 同一版本，审计以事件为准，不保证 LLM 重新生成完全一致。

## 10. 输出物

~~~text
data/datasets/<id>/
  ingest/events.jsonl        # 全量事件
  ingest/draft.json          # 语义草稿（表/列/关系/指标/维度）
  ingest/issues.json         # 阻断/警告/待确认
  ingest/review.json         # 人审队列
  semantic_index.json        # 查询时读模型
~~~

## 11. 与现有模块的映射

| 现有模块 | 变成 |
|---|---|
| header.py | header.detect 工具 |
| infer.py | column.profile / column.type |
| keys.py | grain.detect |
| relations.py | relations.infer |
| profile.py | column profile / catalog |
| quality_generic.py | consistency.check |
| semantic_draft.py | draft.structure 工具（结构草稿） |
| llm_semantic.py | 候选指标 prompt + metric.probe（可复用） |
| documentation.py | docs.parse / docs.resolve |
| generic_pipeline.py | prepare（复制/hash/打开）+ 启动 controller |

不要丢掉这些确定性模块：它们是 agent 的工具，不是被 agent 替代。agent 只负责“下一步做什么、结果意味着什么、要不要问人”。

## 12. 测试

fixture：一个故意做脏的工作簿

- 标题行 + 两行表头；
- 合计行；
- 合并单元格；
- 一个 sheet 两块表；
- 重复主键；
- N:1 和 1:N 各一组；
- 成交单数 / 退单数量；
- 时间列有缺口。

断言：

- sheet/table 切分正确；
- header/totals 识别正确；
- grain 唯一性 probe 通过；
- 关系 cardinality 与 probe 一致；
- 候选指标 probe 通过；
- 退单率候选公式正确，provenance=inferred；
- 所有 blocking issue 都有 evidence；
- 事件回放得到同一 draft hash；
- 预算耗尽时输出 partial draft，不崩。

## 13. 落地顺序

1. skeleton：events + state + reducer + controller + budget；
2. tools v1：open/sample/header/profile/grain；
3. gates/probes：G1/G2/G3；
4. LLM loop：decision/tool/ask_human/finalize；
5. docs/relations/metrics 工具；
6. human review UI；
7. 质量评估与调参。

每一步都能单独跑 fixture，不依赖查询链路。

## 14. 验收指标

| 指标 | 目标 |
|---|---|
| grain 正确率 | ≥95% fixture |
| 关系 cardinality 正确率 | ≥90% |
| 候选指标 probe 通过率 | ≥90% |
| blocking issue 漏报 | 0 |
| 人审修改率 | 逐版下降 |
| 单数据集步数/耗时 | 在预算内 |
| 事件回放一致性 | 100% |

## 15. 风险

| 风险 | 缓解 |
|---|---|
| LLM 循环/发呆 | 步数/token 上限、loop 检测、升级人审 |
| 成本高 | 采样、缓存、只读工具、按需 profile |
| 幻觉列/关系 | 只允许工具返回的事实；proposal 必须带 evidence |
| 不可复现 | 事件存 decision/tool result/version；回放不调 LLM |
| PII 泄漏 | sample 前脱敏；日志 redact；pii 策略 |
| 过度拟合 fixture | 多套脏表 fixture + 真实上传回归 |
