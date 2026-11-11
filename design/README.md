# design 文档索引与状态

> 最后更新：2026-11-11
> 规则：**源码、tests、运行会话/日志和实测结果优先于任何 design 文档。** 历史文档只作背景参考，不作为实现或验收依据。

## 当前有效（工作笔记，随代码复核）

- [FIXES_2026-11-11.md](FIXES_2026-11-11.md)：本轮修复记录与验证证据
- [TASK_PLAN_2026-11-11.md](TASK_PLAN_2026-11-11.md)：后续修复与演进任务清单

这两份也是工作笔记，不是规格；若与代码冲突，以代码为准。

## 历史文档（本目录，已加“已过时”横幅）

- [FINAL_CODE_ARCHITECTURE.md](FINAL_CODE_ARCHITECTURE.md)
- [PROJECT_ARCHITECTURE_TARGET.md](PROJECT_ARCHITECTURE_TARGET.md)
- [REFACTOR_PLAN.md](REFACTOR_PLAN.md)
- [REFACTOR_STATUS.md](REFACTOR_STATUS.md)
- [INGEST_AGENT_DESIGN.md](INGEST_AGENT_DESIGN.md)
- [entity_count_query_shape.md](entity_count_query_shape.md)

## 历史文档（工作区 `E:\10.23\design`，同一批旧稿的旧副本）

`ARCHITECTURE.md`、`PROJECT_MAP.md`、`GENERALIZATION_DESIGN_REVIEW.md`、
`GENERALIZATION_IMPLEMENTATION_PLAN.md`、`SQL_CORRECTNESS_AND_LANGGRAPH.md`、
`nlp2sql-agent-session-change-report.md`、`02_通用数据源接入与隔离设计.md`、
`project_1.0_深度评估与演进方案.md`。这些文件已加“已过时”横幅，仅作历史背景。

## 已确认与实现不一致的历史描述（不要照做）

- `ARCHITECTURE.md` 中的 LLM-4 `fallback_sql`：当前代码没有该路径，模型不写 SQL。
- 旧 schema card / brief 的“全量塞入 + 尾部截断”：已由 schema 优先、分段预算的 brief v2 取代。
- `PROJECT_MAP.md` 里的文件行数、模块清单与当前代码已有差异。

## 判断顺序

1. 运行测试与会话复现；
2. 读 `app/` 当前实现；
3. 查 `design/` 只用于理解历史决策。
