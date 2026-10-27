from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.api.schemas import (
    AnalysisRequest,
    ClarificationRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    DraftEditRequest,
    PublishRequest,
    TurnRequest,
)
from app.api.sse import event_stream
from app.config.settings import (
    DEFAULT_DATASET_ID,
    LLM_BASE_URL,
    LLM_ENABLED,
    LLM_MODEL,
    MAX_ROW_LIMIT,
    TRASH_DIR,
)
from app.core.session import SessionService
from app.datasets.publish import PublishError, publish_dataset
from app.datasets.registry import DatasetError
from app.domain.analysis import analysis_hints, module_catalog
from app.domain.executor import warehouse_version
from app.llm.schema_card import build_schema_card

router = APIRouter(prefix="/api")


def get_service(request: Request) -> SessionService:
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="服务尚未初始化")
    return service


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/meta")
def meta(request: Request, dataset_id: str | None = None):
    service = get_service(request)
    try:
        ctx = service.context(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    sem = ctx.sem
    quality = ctx.quality
    catalog = ctx.catalog
    return {
        "dataset_id": ctx.dataset_id,
        "dataset": sem.dataset,
        "mode": ctx.mode,
        "data_version": ctx.data_version,
        "semantic_version": sem.version,
        "compiler_version": sem.raw.get("compiler_version", 1),
        "time_range": list(sem.time_range),
        "tables": {t: c.get("rows") for t, c in catalog.get("tables", {}).items()},
        "metrics": [
            {"id": m.id, "name": m.name, "definition": m.raw.get("definition"),
             "unit": m.raw.get("unit"), "supported_grain": m.raw.get("supported_grain", [])}
            for m in sem.metrics.values()
        ],
        "dimensions": [
            {"id": d.id, "name": d.raw.get("name_zh", d.id)}
            for d in sem.dimensions.values()
        ],
        "quality": [
            {"id": c["id"], "severity": c["severity"], "title": c["title"], "count": c["count"]}
            for c in quality.get("checks", [])
        ],
        "llm": {
            "enabled": LLM_ENABLED,
            "model": LLM_MODEL if LLM_ENABLED else None,
            "base_url": LLM_BASE_URL if LLM_ENABLED else None,
        },
        "limits": {"max_rows": MAX_ROW_LIMIT},
        "warehouse": str(ctx.warehouse),
    }


@router.get("/datasets")
def list_datasets(request: Request):
    service = get_service(request)
    if service.registry is None:
        return {"datasets": [], "default": service.default_dataset_id}
    return {
        "datasets": [info.as_dict() for info in service.registry.list()],
        "default": service.default_dataset_id,
    }


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, request: Request):
    service = get_service(request)
    try:
        ctx = service.context(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "dataset_id": ctx.dataset_id,
        "name": ctx.name,
        "mode": ctx.mode,
        "status": ctx.status,
        "engine": ctx.engine,
        "data_version": ctx.data_version,
        "semantic_version": ctx.semantic_version,
        "dataset": ctx.sem.dataset,
        "capabilities": ctx.capabilities,
        "readiness": {
            "status": (ctx.readiness or {}).get("status"),
            "blockers": (ctx.readiness or {}).get("blockers", []),
            "warnings": (ctx.readiness or {}).get("warnings", []),
        },
        "warehouse": str(ctx.warehouse),
        "tables": {t: c.get("rows") for t, c in (ctx.catalog.get("tables") or {}).items()},
        "metrics": [
            {"id": m.id, "name": m.name, "definition": m.raw.get("definition"),
             "unit": m.raw.get("unit"),
             "supported_grain": m.raw.get("supported_grain", [])}
            for m in ctx.sem.metrics.values()
        ],
        "dimensions": [
            {"id": d.id, "name": d.raw.get("name_zh", d.id)}
            for d in ctx.sem.dimensions.values()
        ],
        "quality": [
            {"id": c["id"], "severity": c["severity"], "title": c["title"],
             "count": c["count"]}
            for c in (ctx.quality.get("checks") or [])
        ],
    }


@router.patch("/datasets/{dataset_id}/draft")
def edit_dataset_draft(dataset_id: str, request: Request,
                       body: DraftEditRequest):
    service = get_service(request)
    try:
        ctx = service.context(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    import yaml
    from datetime import date, timedelta
    from app.domain.semantic.loader import load_semantic
    from app.datasets.manifest import read_manifest, write_manifest

    semantic = yaml.safe_load(ctx.semantic_path.read_text(encoding="utf-8")) or {}
    original = yaml.safe_dump(semantic, allow_unicode=True, sort_keys=False)
    metrics = semantic.get("metrics") or {}
    dimensions = semantic.get("dimensions") or {}

    for edit in body.metrics or []:
        metric = metrics.get(edit.id)
        if not metric:
            raise HTTPException(status_code=400, detail=f"未知指标: {edit.id}")
        if edit.name_zh is not None:
            metric["name_zh"] = edit.name_zh
        if edit.synonyms is not None:
            metric["synonyms"] = edit.synonyms
        if edit.unit is not None:
            metric["unit"] = edit.unit
        if edit.definition is not None:
            metric["definition"] = edit.definition
    for edit in body.dimensions or []:
        dim = dimensions.get(edit.id)
        if not dim:
            raise HTTPException(status_code=400, detail=f"未知维度: {edit.id}")
        if edit.name_zh is not None:
            dim["name_zh"] = edit.name_zh
        if edit.synonyms is not None:
            dim["synonyms"] = edit.synonyms

    if body.time_column:
        table = str(body.time_column.get("table") or "")
        column = str(body.time_column.get("column") or "")
        table_spec = (semantic.get("tables") or {}).get(table)
        if not table_spec or column not in (table_spec.get("columns") or []):
            raise HTTPException(status_code=400, detail="时间列不存在")
        table_spec["time"] = column
        for metric in metrics.values():
            if metric.get("fact") == table:
                metric["time_via"] = f"{table}.{column}"
                grains = metric.get("supported_grain") or ["none"]
                if "month" not in grains:
                    metric["supported_grain"] = ["none", "month"] + [
                        g for g in grains if g != "none"
                    ]
        if "month" in dimensions:
            dimensions["month"]["column_from"] = f"{table}.{column}"
        try:
            import duckdb
            con = duckdb.connect(str(ctx.warehouse), read_only=True)
            try:
                qcol = '"' + column.replace('"', '""') + '"'
                qtab = '"' + table.replace('"', '""') + '"'
                minv, maxv = con.execute(
                    f"SELECT MIN({qcol}), MAX({qcol}) FROM {qtab}"
                ).fetchone()
            finally:
                con.close()
            if minv is not None and maxv is not None:
                start = str(minv)[:10]
                end = (date.fromisoformat(str(maxv)[:10]) + timedelta(days=1)).isoformat()
                semantic["time_range"] = [start, end]
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"时间列统计失败: {exc}")

    semantic["status"] = "draft"
    semantic_path = ctx.semantic_path
    semantic_path.write_text(
        yaml.safe_dump(semantic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    try:
        load_semantic(semantic_path, ctx.binding_path, catalog_path=ctx.catalog_path)
    except Exception as exc:
        semantic_path.write_text(original, encoding="utf-8")
        raise HTTPException(status_code=400, detail=f"草稿校验失败，已回滚: {exc}")

    descriptor_path = ctx.root / "dataset.json"
    descriptor = {}
    if descriptor_path.exists():
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    descriptor.update({"status": "draft", "edited_at": datetime.now().isoformat(timespec="seconds")})
    descriptor_path.write_text(
        json.dumps(descriptor, ensure_ascii=False, indent=2), encoding="utf-8")

    draft_path = ctx.root / "semantic_draft.json"
    if draft_path.exists():
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            for edit in body.metrics or []:
                for item in draft.get("metrics", []):
                    if item.get("id") == edit.id:
                        if edit.name_zh is not None:
                            item["name_zh"] = edit.name_zh
                            item["name"] = edit.name_zh
                        if edit.synonyms is not None:
                            item["synonyms"] = edit.synonyms
                        if edit.definition is not None:
                            item["definition"] = edit.definition
            for edit in body.dimensions or []:
                for item in draft.get("dimensions", []):
                    if item.get("id") == edit.id:
                        if edit.name_zh is not None:
                            item["name_zh"] = edit.name_zh
                        if edit.synonyms is not None:
                            item["synonyms"] = edit.synonyms
            if body.time_column:
                draft["time_column"] = body.time_column
            draft_path.write_text(
                json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        except json.JSONDecodeError:
            pass

    manifest = read_manifest(ctx.root)
    manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_manifest(ctx.root, manifest)
    service.registry.invalidate(dataset_id) if service.registry else None
    service.invalidate_dataset(dataset_id)
    return {
        "dataset_id": dataset_id,
        "status": "draft",
        "metrics_edited": len(body.metrics or []),
        "dimensions_edited": len(body.dimensions or []),
        "time_column": body.time_column,
    }


@router.get("/datasets/{dataset_id}/profile")
def dataset_profile(dataset_id: str, request: Request):
    service = get_service(request)
    try:
        ctx = service.context(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    profile = dict(ctx.profile or {})
    profile.update({
        "dataset_id": ctx.dataset_id,
        "name": ctx.name,
        "mode": ctx.mode,
        "data_version": ctx.data_version,
        "semantic_version": ctx.semantic_version,
    })
    return profile


@router.get("/datasets/{dataset_id}/readiness")
def dataset_readiness(dataset_id: str, request: Request):
    service = get_service(request)
    try:
        ctx = service.context(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    readiness = ctx.readiness or {}
    return {
        "dataset_id": ctx.dataset_id,
        "status": readiness.get("status", "unknown"),
        "checks": readiness.get("checks", []),
        "blockers": readiness.get("blockers", []),
        "warnings": readiness.get("warnings", []),
        "metrics": readiness.get("metrics", []),
        "dimensions": readiness.get("dimensions", []),
        "probes": readiness.get("probes", 0),
        "capabilities": ctx.capabilities,
        "generated_at": readiness.get("generated_at"),
    }


@router.get("/datasets/{dataset_id}/draft")
def dataset_draft(dataset_id: str, request: Request):
    service = get_service(request)
    try:
        ctx = service.context(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    path = ctx.root / "semantic_draft.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="语义草稿不存在")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"草稿损坏: {exc}")


@router.post("/datasets/{dataset_id}/publish")
def dataset_publish(dataset_id: str, request: Request,
                    body: PublishRequest | None = None):
    service = get_service(request)
    if service.registry is None:
        raise HTTPException(status_code=503, detail="数据集注册表未初始化")
    try:
        result = publish_dataset(
            service.registry, dataset_id,
            selected_metrics=body.metrics if body else None,
            selected_dimensions=body.dimensions if body else None,
        )
    except (PublishError, DatasetError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    service.invalidate_dataset(dataset_id)
    return result


@router.delete("/datasets/{dataset_id}")
def dataset_delete(dataset_id: str, request: Request):
    service = get_service(request)
    if dataset_id == DEFAULT_DATASET_ID:
        raise HTTPException(status_code=400, detail="内置数据集不能删除")
    try:
        ctx = service.context(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    target = Path(TRASH_DIR) / f"{dataset_id}-{datetime.now():%Y%m%d%H%M%S}"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(ctx.root), str(target))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"删除失败: {exc}")
    if service.registry is not None:
        service.registry.invalidate()
        service.registry.refresh()
    service.invalidate_dataset(dataset_id)
    return {"dataset_id": dataset_id, "status": "deleted", "trash": str(target)}


@router.get("/schema-card")
def schema_card(request: Request, dataset_id: str | None = None):
    service = get_service(request)
    try:
        ctx = service.context(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"card": build_schema_card(ctx.sem, dim_values_path=ctx.dim_values_path)}


@router.get("/analysis/modules")
def analysis_modules(request: Request, dataset_id: str | None = None):
    service = get_service(request)
    try:
        ctx = service.context(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"modules": module_catalog(ctx.sem)}


@router.get("/analysis/hints")
def analysis_hints_endpoint(request: Request, dataset_id: str | None = None):
    service = get_service(request)
    try:
        ctx = service.context(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return analysis_hints(ctx.sem, ctx.dim_values_path)


@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(request: Request, body: CreateSessionRequest | None = None):
    service = get_service(request)
    dataset_id = body.dataset_id if body else None
    try:
        sid = await service.create(body.session_id if body else None, dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return CreateSessionResponse(
        session_id=sid,
        dataset_id=dataset_id or service.default_dataset_id,
    )


@router.get("/sessions")
def list_sessions(request: Request, limit: int = 50):
    """Recent conversations, newest first. Title = first user message."""
    service = get_service(request)
    root = service.store.root
    items = []
    paths = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[: max(1, min(limit, 200))]:
        title = None
        turns = 0
        dataset_id = None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "turn.started":
                    turns += 1
                elif ev.get("type") == "session.created":
                    dataset_id = (ev.get("payload") or {}).get("dataset_id")
                elif ev.get("type") == "user.message" and title is None:
                    title = (ev.get("payload") or {}).get("text")
        except OSError:
            continue
        items.append({
            "session_id": path.stem,
            "title": (title or "新对话")[:60],
            "turns": turns,
            "dataset_id": dataset_id,
            "updated_at": datetime.fromtimestamp(
                path.stat().st_mtime
            ).isoformat(timespec="seconds"),
        })
    return {"sessions": items}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request):
    service = get_service(request)
    try:
        removed = service.store.delete(session_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"deleted": session_id}


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request):
    service = get_service(request)
    if not service.store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return service.state(session_id).to_dict()


@router.get("/sessions/{session_id}/events")
def get_events(session_id: str, request: Request):
    service = get_service(request)
    if not service.store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"events": [e.to_dict() for e in service.events(session_id)]}


@router.post("/sessions/{session_id}/turns")
async def run_turn(session_id: str, body: TurnRequest, request: Request):
    service = get_service(request)
    if not service.store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    stream = event_stream(service.turn(session_id, body.message))
    return StreamingResponse(stream, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@router.post("/sessions/{session_id}/analysis")
async def run_analysis(session_id: str, request: Request,
                       body: AnalysisRequest | None = None):
    service = get_service(request)
    if not service.store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    stream = event_stream(service.analysis(
        session_id, body.model_dump() if body else None
    ))
    return StreamingResponse(stream, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@router.post("/sessions/{session_id}/clarifications")
async def clarify(session_id: str, body: ClarificationRequest, request: Request):
    """A clarification answer is just the next user message; the reducer binds it."""
    service = get_service(request)
    if not service.store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    stream = event_stream(service.turn(session_id, body.value))
    return StreamingResponse(stream, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@router.get("/sessions/{session_id}/history")
def history(session_id: str, request: Request):
    service = get_service(request)
    if not service.store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    state = service.state(session_id)
    response = {
        "history": service.history(session_id),
    }
    try:
        controller = service.controller_for(state.dataset_id)
    except DatasetError:
        return response
    response.update({
        "dataset": controller.sem.dataset,
        "semantic_version": controller.sem.version,
        "compiler_version": controller.sem.raw.get("compiler_version", 1),
        "warehouse_version": warehouse_version(controller.warehouse),
    })
    return response


@router.post("/sessions/{session_id}/records/{result_id}/verify")
def verify_record(session_id: str, result_id: str, request: Request):
    service = get_service(request)
    if not service.store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        return service.verify(session_id, result_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="查询记录不存在")
    except DatasetError as exc:
        raise HTTPException(status_code=409, detail=f"数据源不可用：{exc}")


@router.get("/sessions/{session_id}/results/{result_id}")
def get_result(session_id: str, result_id: str, request: Request):
    service = get_service(request)
    artifact = service.result(session_id, result_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="结果不存在")
    return artifact


@router.get("/sessions/{session_id}/results/{result_id}/export.csv")
def export_result(session_id: str, result_id: str, request: Request):
    service = get_service(request)
    try:
        filename, content = service.export_csv(session_id, result_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="结果不存在")
    return Response(content=content, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
    })


@router.get("/quality")
def quality(request: Request, dataset_id: str | None = None):
    service = get_service(request)
    try:
        ctx = service.context(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not ctx.quality:
        raise HTTPException(status_code=404, detail="尚未生成质量报告")
    return ctx.quality
