# 指标口径

- 商品销售额：状态为 `delivered` 的订单明细 `price` 合计，不含运费。
- 订单数：按 `order_id` 去重。
- 延迟订单：`delivered_at > estimated_delivery_at`。
- 延迟率：延迟的已送达订单数 / 已送达订单数。
- 低分评价：`review_score <= 2`。
- 复购客户：同一 `customer_unique_id` 完成至少 2 笔已送达订单。
- 支付金额：按订单先汇总 `payment_value`，不能与多行订单明细直接关联后求和。
