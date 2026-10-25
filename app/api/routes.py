from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.api.schemas import (
    ClarificationRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    TurnRequest,
)
from app.api.sse import event_stream
from app.config.settings import (
    LLM_BASE_URL,
    LLM_ENABLED,
    LLM_MODEL,
    MAX_ROW_LIMIT,
    SEMANTIC_PATH,
)
from app.core.session import SessionService
from app.domain.semantic.loader import load_semantic

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
def meta(request: Request):
    service = get_service(request)
    sem = service.controller.sem
    from app.config.settings import CATALOG_PATH, QUALITY_PATH, WAREHOUSE_PATH

    quality = {}
    if QUALITY_PATH.exists():
        quality = json.loads(QUALITY_PATH.read_text(encoding="utf-8"))
    catalog = {}
    if CATALOG_PATH.exists():
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return {
        "dataset": sem.dataset,
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
        "warehouse": str(WAREHOUSE_PATH),
    }


@router.get("/schema-card")
def schema_card():
    from app.domain.semantic.loader import load_semantic
    from app.llm.schema_card import build_schema_card
    return {"card": build_schema_card(load_semantic())}


@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(request: Request, body: CreateSessionRequest | None = None):
    service = get_service(request)
    sid = await service.create(body.session_id if body else None)
    return CreateSessionResponse(session_id=sid)


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
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "turn.started":
                    turns += 1
                elif ev.get("type") == "user.message" and title is None:
                    title = (ev.get("payload") or {}).get("text")
        except OSError:
            continue
        items.append({
            "session_id": path.stem,
            "title": (title or "新对话")[:60],
            "turns": turns,
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
    return {"history": service.history(session_id)}


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
def quality(request: Request):
    from app.config.settings import QUALITY_PATH
    if not QUALITY_PATH.exists():
        raise HTTPException(status_code=404, detail="尚未生成质量报告")
    return json.loads(QUALITY_PATH.read_text(encoding="utf-8"))
