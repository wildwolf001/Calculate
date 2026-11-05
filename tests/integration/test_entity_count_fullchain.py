# Full-chain entity-count tests: user text -> controller -> parser -> planner
# -> compiler -> executor -> checks -> answer, including multi-turn context.
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import duckdb

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.domain.resolver import RuleGrounder
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer, expand_entities
from app.llm.compose_answer import TemplateComposer
from app.llm.parse_intent import LLMIntentParser
from app.llm.rule_parser import RuleParser


def _semantic(
    *,
    entity_table: str,
    entity_pk: str,
    natural_key: str,
    fact_table: str,
    fact_key: str,
    fk: str,
    status_col: str,
    status_value: str,
    date_col: str,
    metric_name: str,
    entity_label: str,
    order_metric_name: str,
    extra_dims: dict[str, Dimension] | None = None,
) -> SemanticLayer:
    tables = {
        entity_table: {
            "grain": [entity_pk],
            "time": None,
            "columns": [entity_pk, natural_key, "segment"],
        },
        fact_table: {
            "grain": [fact_key],
            "time": date_col,
            "columns": [fact_key, fk, status_col, date_col, "seller"],
        },
    }
    joins = {
        fact_table: {
            entity_table: {"left": fk, "right": entity_pk, "card": "N:1"},
        },
    }
    metrics = {
        "order_count": Metric("order_count", {
            "name_zh": order_metric_name,
            "type": "aggregate",
            "fact": fact_table,
            "expr": f"COUNT(DISTINCT {fact_table}.{fact_key})",
            "scope": {f"{fact_table}.{status_col}": status_value},
            "time_via": f"{fact_table}.{date_col}",
            "supported_grain": ["none", "month", "seller"],
            "unit": "笔",
        }),
    }
    dimensions = {
        "status": Dimension("status", {
            "name_zh": "状态", "kind": "categorical",
            "column": f"{fact_table}.{status_col}",
        }),
        "month": Dimension("month", {
            "name_zh": "月份", "kind": "time",
            "column": f"{fact_table}.{date_col}", "format": "%Y-%m",
        }),
    }
    if extra_dims:
        dimensions.update(extra_dims)
    sem = SemanticLayer(
        version=1,
        dataset="entity_fixture",
        tables=tables,
        joins=joins,
        pre_aggregates={},
        metrics=metrics,
        dimensions=dimensions,
        policies={},
        time_range=("2025-01-01", "2025-07-09"),
        binding={"tables": {}},
        capabilities={"entity_count": True, "member_list": True, "ratio": True},
        parser="generic",
        raw={
            "entities": {
                entity_table: {
                    "table": entity_table,
                    "key": natural_key,
                    "label": entity_label,
                    "aliases": [entity_label, f"自然{entity_label}"],
                    "event": {
                        "fact": fact_table,
                        "key": f"{fact_table}.{fact_key}",
                        "scope": {f"{fact_table}.{status_col}": status_value},
                        "time_via": f"{fact_table}.{date_col}",
                        "join": f"{fact_table} -> {entity_table}",
                    },
                }
            }
        },
        time_axes=[],
    )
    expand_entities(sem)
    return sem


def _make_db(tmp_path: Path, *, entity_table: str, entity_pk: str,
             natural_key: str, fact_table: str, fact_key: str, fk: str,
             status_col: str, date_col: str, entities: list[tuple],
             facts: list[tuple]) -> Path:
    path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        f"create table {entity_table} ({entity_pk} varchar, "
        f"{natural_key} varchar, segment varchar)"
    )
    con.execute(
        f"create table {fact_table} ({fact_key} varchar, {fk} varchar, "
        f"{status_col} varchar, {date_col} date, seller varchar)"
    )
    con.executemany(f"insert into {entity_table} values (?,?,?)", entities)
    con.executemany(f"insert into {fact_table} values (?,?,?,?,?)", facts)
    con.close()
    return path


def _dim_values(tmp_path: Path, sem: SemanticLayer, values: dict) -> Path:
    path = tmp_path / "dim_values.json"
    path.write_text(json.dumps({"values": values, "aliases": {}}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _controller(tmp_path: Path, sem: SemanticLayer, dim_values: Path, parser) -> Controller:
    store = EventStore(tmp_path / "events")
    ctrl = Controller(store, sem, parser, RuleGrounder(sem, dim_values), TemplateComposer())
    ctrl.warehouse = tmp_path / "warehouse.duckdb"
    ctrl.dim_values_path = dim_values
    return ctrl


def _turn(ctrl: Controller, session_id: str, question: str) -> list:
    async def run():
        return [event async for event in ctrl.run_turn(session_id, question)]
    return asyncio.run(run())


def _rows(events: list) -> list:
    return next(event.payload["rows"] for event in events if event.type == "sql.executed")


def _answer(events: list) -> str:
    return next(event.payload["text"] for event in events if event.type == "answer.composed")


class FakeLLM:
    available = True

    def __init__(self, answers: dict[str, dict]):
        self.answers = answers

    def complete_json(self, system, user, max_tokens=0, stage="llm"):
        for needle, payload in self.answers.items():
            if needle in user:
                return payload
        raise AssertionError(f"unexpected LLM prompt: {user[:300]}")


def _llm_parser(sem: SemanticLayer, dim_values: Path, answers: dict[str, dict]):
    return LLMIntentParser(
        sem,
        client=FakeLLM(answers),
        fallback=RuleParser(sem, dim_values),
        dim_values_path=dim_values,
        index={},
        use_brief=False,
    )

def _ecommerce_env(tmp_path: Path):
    sem = _semantic(
        entity_table="customers", entity_pk="customer_id",
        natural_key="customer_unique_id",
        fact_table="orders", fact_key="order_id", fk="customer_id",
        status_col="status", status_value="已送达", date_col="order_date",
        metric_name="复购客户数", entity_label="客户",
        order_metric_name="已送达订单数",
        extra_dims={
            "seller": Dimension("seller", {
                "name_zh": "商家名称", "kind": "categorical",
                "column": "orders.seller",
            }),
            "segment": Dimension("segment", {
                "name_zh": "客户分层", "kind": "categorical",
                "column": "customers.segment",
            }),
        },
    )
    _make_db(
        tmp_path,
        entity_table="customers", entity_pk="customer_id",
        natural_key="customer_unique_id",
        fact_table="orders", fact_key="order_id", fk="customer_id",
        status_col="status", date_col="order_date",
        entities=[
            ("c1", "u1", "新客"),
            ("c2", "u2", "老客"),
            ("c3", "u3", "新客"),
            ("c4", "u4", "老客"),
            ("c5", "u5", "新客"),
        ],
        facts=[
            ("o1", "c1", "已送达", "2025-01-05", "A"),
            ("o2", "c1", "已送达", "2025-02-05", "A"),
            ("o3", "c1", "已送达", "2025-05-05", "A"),
            ("o4", "c1", "已送达", "2025-05-06", "A"),
            ("o5", "c2", "已送达", "2025-01-10", "A"),
            ("o6", "c2", "已送达", "2025-05-10", "B"),
            ("o7", "c3", "已送达", "2025-05-15", "B"),
            ("o8", "c4", "已送达", "2025-02-15", "B"),
            ("o9", "c5", "待发货", "2025-05-20", "B"),
            ("o10", "c1", "已取消", "2025-03-01", "A"),
        ],
    )
    dim_values = _dim_values(tmp_path, sem, {
        "orders.status": ["已送达", "待发货", "已取消"],
        "orders.seller": ["A", "B"],
        "customers.segment": ["新客", "老客"],
    })
    return sem, dim_values


QUESTION = "有多少自然客户完成了至少2笔已送达订单？"


def test_entity_count_full_chain_returns_repeat_customers(tmp_path):
    sem, dim_values = _ecommerce_env(tmp_path)
    ctrl = _controller(tmp_path, sem, dim_values, RuleParser(sem, dim_values))
    events = _turn(ctrl, "s1", QUESTION)
    assert _rows(events) == [[2]]
    assert "2" in _answer(events)
    created = next(e.payload for e in events if e.type == "query.ir.created")
    assert created["metric"] == "entity_count_customers"
    assert created["entity_count"] == {"entity": "自然客户", "min_events": 2}
    assert created["nested"] == {"entity": "自然客户", "min_orders": 2}
    assert not any(e.type == "plan.repaired" for e in events)


def test_context_changes_threshold_not_entity_or_scope(tmp_path):
    sem, dim_values = _ecommerce_env(tmp_path)
    ctrl = _controller(tmp_path, sem, dim_values, RuleParser(sem, dim_values))
    first = _turn(ctrl, "s1", QUESTION)
    second = _turn(ctrl, "s1", "那至少3笔呢？")
    assert _rows(first) == [[2]]
    assert _rows(second) == [[1]]
    ctx = next(e.payload for e in second if e.type == "context.compiled")
    assert ctx["previous_scope"]["entity_count"] == {
        "entity": "自然客户", "min_events": 2,
    }
    created = next(e.payload for e in second if e.type == "query.ir.created")
    assert created["entity_count"] == {"entity": "自然客户", "min_events": 3}


def test_context_changes_time_but_keeps_entity_count(tmp_path):
    sem, dim_values = _ecommerce_env(tmp_path)
    ctrl = _controller(tmp_path, sem, dim_values, RuleParser(sem, dim_values))
    first = _turn(ctrl, "s1", QUESTION)
    second = _turn(ctrl, "s1", "只看5月呢？")
    assert _rows(first) == [[2]]
    assert _rows(second) == [[1]]
    created = next(e.payload for e in second if e.type == "query.ir.created")
    assert created["entity_count"] == {"entity": "自然客户", "min_events": 2}
    assert created["time"] == {"start": "2025-05-01", "end": "2025-06-01"}


def test_plain_natural_customer_count_uses_unique_key(tmp_path):
    sem, dim_values = _ecommerce_env(tmp_path)
    ctrl = _controller(tmp_path, sem, dim_values, RuleParser(sem, dim_values))
    events = _turn(ctrl, "s1", "有多少自然客户下过已送达订单？")
    assert _rows(events) == [[4]]
    created = next(e.payload for e in events if e.type == "query.ir.created")
    assert created["entity_count"] == {"entity": "自然客户", "min_events": 1}


def test_order_count_is_not_turned_into_entity_count(tmp_path):
    sem, dim_values = _ecommerce_env(tmp_path)
    parser = _llm_parser(sem, dim_values, {
        "有多少已送达订单": {
            "intent_type": "query", "confidence": 0.9,
            "slots": {"metric": "order_count", "dimensions": [], "filters": {},
                      "time": None, "intent": "query"}, "notes": [],
        },
    })
    ctrl = _controller(tmp_path, sem, dim_values, parser)
    events = _turn(ctrl, "s1", "有多少已送达订单？")
    assert _rows(events) == [[8]]
    created = next(e.payload for e in events if e.type == "query.ir.created")
    assert created["metric"] == "order_count"
    assert created["entity_count"] is None


def test_llm_order_metric_is_corrected_for_entity_question(tmp_path):
    sem, dim_values = _ecommerce_env(tmp_path)
    parser = _llm_parser(sem, dim_values, {
        QUESTION: {
            "intent_type": "query", "confidence": 0.9,
            "slots": {"metric": "order_count", "dimensions": [], "filters": {},
                      "time": None, "nested": {"min_orders": 2},
                      "intent": "query"}, "notes": [],
        },
    })
    ctrl = _controller(tmp_path, sem, dim_values, parser)
    events = _turn(ctrl, "s1", QUESTION)
    assert _rows(events) == [[2]]
    created = next(e.payload for e in events if e.type == "query.ir.created")
    assert created["metric"] == "entity_count_customers"
    assert created["entity_count"] == {"entity": "自然客户", "min_events": 2}
    assert not any(e.type == "plan.repaired" for e in events)


def test_group_filter_with_dimensions_still_uses_having(tmp_path):
    sem, dim_values = _ecommerce_env(tmp_path)
    parser = _llm_parser(sem, dim_values, {
        "不少于2笔的商家": {
            "intent_type": "query", "confidence": 0.9,
            "slots": {
                "metric": "order_count",
                "dimensions": ["seller"],
                "filters": {},
                "time": {"start": "2025-05-01", "end": "2025-06-01"},
                "having": [{"metric": "order_count", "op": "gte", "value": 2}],
                "order_by": [{"field": "value", "direction": "desc"}],
                "intent": "query",
            },
            "notes": [],
        },
    })
    ctrl = _controller(tmp_path, sem, dim_values, parser)
    events = _turn(ctrl, "s1", "5月已送达订单不少于2笔的商家中，谁的单最多？")
    rows = _rows(events)
    assert {row[0] for row in rows} == {"A", "B"}
    sql = next(e.payload["sql"] for e in events if e.type == "sql.compiled")
    assert "HAVING" in sql and "GROUP BY" in sql


def test_entity_count_without_metric_is_unresolved_not_order_count(tmp_path):
    sem, dim_values = _ecommerce_env(tmp_path)
    ctrl = _controller(tmp_path, sem, dim_values, RuleParser(sem, dim_values))
    events = _turn(ctrl, "s1", "有多少商品至少2次售出？")
    assert not any(e.type == "sql.executed" for e in events)
    ambiguity = next(e.payload for e in events if e.type == "ambiguity.detected")
    assert "商品" in ambiguity["message"]
    assert ambiguity["kind"] == "unresolved"


def _saas_env(tmp_path: Path):
    sem = _semantic(
        entity_table="accounts", entity_pk="account_id",
        natural_key="account_unique_id",
        fact_table="invoices", fact_key="invoice_id", fk="account_id",
        status_col="status", status_value="已支付", date_col="paid_at",
        metric_name="复购账户数", entity_label="账户",
        order_metric_name="已支付发票数",
    )
    _make_db(
        tmp_path,
        entity_table="accounts", entity_pk="account_id",
        natural_key="account_unique_id",
        fact_table="invoices", fact_key="invoice_id", fk="account_id",
        status_col="status", date_col="paid_at",
        entities=[
            ("a1", "u1", "SMB"),
            ("a2", "u2", "ENT"),
            ("a3", "u3", "SMB"),
            ("a4", "u4", "ENT"),
        ],
        facts=[
            ("i1", "a1", "已支付", "2025-01-05", "A"),
            ("i2", "a1", "已支付", "2025-02-05", "A"),
            ("i3", "a2", "已支付", "2025-01-10", "A"),
            ("i4", "a3", "待支付", "2025-01-15", "A"),
            ("i5", "a4", "已支付", "2025-03-15", "A"),
        ],
    )
    dim_values = _dim_values(tmp_path, sem, {
        "invoices.status": ["已支付", "待支付"],
        "accounts.segment": ["SMB", "ENT"],
    })
    return sem, dim_values


def test_same_code_path_works_on_a_different_domain(tmp_path):
    sem, dim_values = _saas_env(tmp_path)
    ctrl = _controller(tmp_path, sem, dim_values, RuleParser(sem, dim_values))
    events = _turn(ctrl, "s1", "有多少账户至少2张已支付发票？")
    assert _rows(events) == [[1]]
    created = next(e.payload for e in events if e.type == "query.ir.created")
    assert created["entity_count"] == {"entity": "账户", "min_events": 2}
    assert "账户" in _answer(events)


def test_context_does_not_leak_entity_count_into_a_new_metric(tmp_path):
    sem, dim_values = _ecommerce_env(tmp_path)
    ctrl = _controller(tmp_path, sem, dim_values, RuleParser(sem, dim_values))
    first = _turn(ctrl, "s1", QUESTION)
    second = _turn(ctrl, "s1", "只看5月的已送达订单数是多少？")
    assert _rows(first) == [[2]]
    assert _rows(second) == [[4]]
    created = next(e.payload for e in second if e.type == "query.ir.created")
    assert created["metric"] == "order_count"
    assert created["entity_count"] is None


def test_grouped_entity_count_by_customer_segment(tmp_path):
    sem, dim_values = _ecommerce_env(tmp_path)
    ctrl = _controller(tmp_path, sem, dim_values, RuleParser(sem, dim_values))
    events = _turn(ctrl, "s1", "按客户分层，有多少自然客户至少2笔已送达订单？")
    rows = _rows(events)
    assert {row[0]: row[1] for row in rows} == {"新客": 1, "老客": 1}
    sql = next(e.payload["sql"] for e in events if e.type == "sql.compiled")
    assert sql.count("GROUP BY") >= 2

