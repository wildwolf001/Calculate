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

parser 输出 `entity_count`，而不是 `having` 或 `nested`。实体注册表和 `min_orders=1` 的实体指标在**发布期**由 `materialize_semantic` 生成并写入 `semantic.yaml`；运行时 `load_semantic` 只读取，不再发明实体或指标。binder 优先绑定已发布的实体指标；没有匹配实体时澄清，不回退到订单数。

planner 将 `entity_count` 规范化为 `NestedSpec(min_orders=min_events)`。compiler 支持两种形态：

- 无维度：按实体分组、阈值过滤、外层 `COUNT(*)`；
- 有维度：按 `维度 + 实体` 分组、阈值过滤、外层按维度 `COUNT(*)`。

validator 禁止实体计数使用全局 `HAVING`，并禁止 `nested` + 无分组维度。

## 运行时状态

`SessionState.ir` 是唯一查询状态；`state.slots` 只是从 IR 派生的兼容视图，供 UI、旧事件和旧 API 读取。新事件不再把 slots 当第二份真相写入 reducer；旧事件在 replay 边界转换成 IR。

## 边界

- 分组实体计数只允许语义层声明支持的维度；跨事实表的 fanout 维度会被 `allowed_dimensions` 拒绝。
- 实体注册表可以从现有 `entity_count` 指标推导，也可以由 `entities` 配置显式声明；没有实体元数据时澄清，不猜。
- 组内过滤仍保留 `nested -> HAVING`，但要求存在分组维度。

## 测试

`tests/integration/test_entity_count_fullchain.py` 覆盖：

- 用户输入 → controller → parser → planner → compiler → executor → answer；
- 多轮上下文：阈值变化、时间变化、实体计数不泄漏到新指标；
- 负例：订单数问题不能被改成实体计数；
- 组内过滤仍使用 `HAVING`；
- 分组实体计数（按客户分层）使用两层 `GROUP BY`；
- 缺少实体指标时澄清，而不是回退到订单数；
- 第二套完全不同的 schema（accounts/invoices）零 Python 改动通过。

## 后续

当前的实体注册表优先从已有 `entity_count` 指标推导，也支持显式 `entities` 配置。下一步的通用化重点：

- ingest 自动为没有 `entity_count` 指标的数据集推断实体表、自然键和事件关系；
- 把显式 `entities` 作为发布语义的一部分，而不是仅靠指标反推；
- 让 resolver 退化为纯 binder，逐步删除 legacy ecommerce 分支；
- 在兼容旧事件回放的前提下，最终移除 `nested`。
