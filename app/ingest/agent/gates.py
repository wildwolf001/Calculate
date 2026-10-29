from __future__ import annotations

from app.agent.contracts import GateResult, Issue


class TablesProfiledGate:
    id = "tables_profiled"
    blocking = True

    def check(self, state) -> GateResult:
        ok = bool(state.tables) and all(table.get("columns") for table in state.tables.values())
        return GateResult(gate_id=self.id, ok=ok, evidence={"tables": list(state.tables)})


class GrainGate:
    id = "grain_unique"
    blocking = True

    def check(self, state) -> GateResult:
        bad = [
            name for name, table in state.tables.items()
            if not table.get("grain") or not table.get("grain_unique")
        ]
        issues = [Issue(code="GRAIN_UNIQUE", message=f"{name} 没有唯一 grain") for name in bad]
        return GateResult(gate_id=self.id, ok=not bad, issues=issues, evidence={"bad": bad})


class MetricGate:
    id = "metric_probe"
    blocking = True

    def check(self, state) -> GateResult:
        bad = [
            metric_id for metric_id, metric in state.metrics.items()
            if not (metric.get("probe") or {}).get("ok")
        ]
        issues = [Issue(code="METRIC_PROBE", message=f"{mid} 未通过 probe") for mid in bad]
        return GateResult(gate_id=self.id, ok=not bad, issues=issues, evidence={"bad": bad})
