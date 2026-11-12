# 电商经营分析 Agent Demo 数据

- 数据范围：2025-01 至 2025-06
- 订单数：180
- 数据结构：参考 Olist 巴西电商公开数据集
- 销售额口径：已送达订单的 order_items.price，不含运费
- 延迟订单：delivered_at > estimated_delivery_at
- 低分评价：review_score <= 2
- 随机种子：20260921，可重复生成
