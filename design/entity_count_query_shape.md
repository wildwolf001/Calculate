# EntityCount 查询形状

## 为什么需要它

`nested` 同时承担了两种语义：

1. **组内过滤**：按商家分组后，只保留订单数 >= N 的组。编译为 `GROUP BY seller ... HAVING COUNT(...) >= N`。
2. **实体计数**：统计“每个客户至少 N 笔订单”的客户数。编译为 `GROUP BY customer ... HAVING COUNT(...) >= N`，外层再 `COUNT(*)`。

同一个字段承载两种语义，导致 `repair_nested_plan` 会把实体计数错误地改写成全局 `HAVING`。
例如“有多少自然客户完成了至少2笔已送达订单”会退化成 `COUNT(order_id) HAVING COUNT(order_id) >= 2`，返回 159 而不是 46。

## 当前契约

`QueryIR` / `QueryPlan` 增加 `entity_count`：

```python
class EntityCountSpec(BaseModel):
    entity: str
    min_events: int = Field(default=1, ge=1)
```

parser 输出 `entity_count`，而不是 `having` 或 `nested`。binder 只把实体名词绑定到已发布的 `entity_count` 指标；如果没有匹配指标，返回 unresolved，不回退到订单数。

planner 将 `entity_count` 规范化为 `NestedSpec(min_orders=min_events)`，compiler 复用已有的实体 CTE。validator 禁止实体计数使用全局 `HAVING`，并禁止 `nested` + 无分组维度。

## 边界

- 实体计数不允许按维度拆分（当前 compiler 的 `_compile_entity` 只按实体分组；分组实体计数是后续工作）。
- 实体计数只绑定 `type=entity_count` 的已发布指标；没有则澄清，不猜。
- 组内过滤仍保留 `nested -> HAVING`，但要求存在分组维度。

## 测试

`tests/integration/test_entity_count_fullchain.py` 覆盖：

- 用户输入 → controller → parser → planner → compiler → executor → answer；
- 多轮上下文：阈值变化、时间变化、实体计数不泄漏到新指标；
- 负例：订单数问题不能被改成实体计数；
- 组内过滤仍使用 `HAVING`；
- 缺少实体指标时澄清，而不是回退到订单数；
- 第二套完全不同的 schema（accounts/invoices）零 Python 改动通过。

## 后续

当前实现仍依赖语义层里已有 `entity_count` 指标（如 `复购客户数`）。真正的通用化需要：

- 语义层增加中性的 entity/event/scope 原语；
- ingest 为每个实体表生成自然键和事件关系；
- compiler 从原语组合实体计数，而不是从预置指标里挑；
- resolver 退化为纯 binder，删除 `nested` 和 legacy ecommerce 分支。
