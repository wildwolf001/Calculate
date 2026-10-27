"""Plan -> validate -> compile -> validate SQL -> execute -> checks.

Staged helpers are used by the controller (one event per stage). `run_query`
is the convenience wrapper used by tests and scripts.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config.settings import WAREHOUSE_PATH
from app.domain.checks import CheckIssue, run_checks, run_member_list_checks
from app.domain.compiler import Compiled, compile_plan
from app.domain.executor import ResultSet, execute
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import SemanticLayer
from app.domain.validator import Issue, validate_plan, validate_sql


class PipelineError(RuntimeError):
    def __init__(self, message: str, issues: list[Issue] | None = None):
        super().__init__(message)
        self.issues = issues or []


@dataclass
class QueryOutcome:
    ok: bool
    plan: QueryPlan
    compiled: Compiled | None = None
    result: ResultSet | None = None
    plan_issues: list[Issue] = field(default_factory=list)
    sql_issues: list[Issue] = field(default_factory=list)
    checks: list[CheckIssue] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    error: str | None = None


def prepare(plan: QueryPlan, sem: SemanticLayer) -> tuple[Compiled, list[Issue]]:
    """Validate the plan and produce safe SQL. Raises PipelineError on errors."""
    plan_issues = validate_plan(plan, sem)
    if any(i.severity == "error" for i in plan_issues):
        raise PipelineError(
            "; ".join(i.message for i in plan_issues if i.severity == "error"),
            plan_issues,
        )
    compiled = compile_plan(plan, sem)
    sql_issues = validate_sql(compiled.sql, compiled, plan, sem)
    if any(i.severity == "error" for i in sql_issues):
        raise PipelineError(
            "; ".join(i.message for i in sql_issues if i.severity == "error"),
            plan_issues + sql_issues,
        )
    return compiled, plan_issues + sql_issues


def execute_prepared(compiled: Compiled, db_path=WAREHOUSE_PATH) -> ResultSet:
    return execute(compiled, db_path=db_path)


def check(plan: QueryPlan, sem: SemanticLayer, result: ResultSet,
          db_path=WAREHOUSE_PATH, quality: dict | None = None):
    if plan.kind == "member_list":
        return run_member_list_checks(plan, result)
    return run_checks(plan, sem.metric(plan.metric), result, sem,
                      db_path=db_path, quality=quality)


def run_query(plan: QueryPlan, sem: SemanticLayer,
              db_path=WAREHOUSE_PATH, quality: dict | None = None) -> QueryOutcome:
    try:
        compiled, issues = prepare(plan, sem)
    except PipelineError as exc:
        return QueryOutcome(ok=False, plan=plan, plan_issues=exc.issues, error=str(exc))

    try:
        result = execute_prepared(compiled, db_path=db_path)
    except Exception as exc:  # timeout / execution error
        return QueryOutcome(ok=False, plan=plan, compiled=compiled,
                            plan_issues=issues, error=str(exc))

    checks, facts = check(plan, sem, result, db_path=db_path, quality=quality)
    blocked = any(c.severity == "error" for c in checks)
    return QueryOutcome(
        ok=not blocked,
        plan=plan,
        compiled=compiled,
        result=result,
        plan_issues=issues,
        sql_issues=[],
        checks=checks,
        facts=facts,
        error=None if not blocked else "; ".join(c.title for c in checks if c.severity == "error"),
    )
