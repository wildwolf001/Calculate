"""Upload, analyze (background job) and job status endpoints."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
import uuid

import yaml
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config.settings import (
    DATASETS_DIR,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_FILES,
    RUNTIME_DIR,
    UPLOADS_DIR,
)
from app.llm.client import LLMClient
from app.core.jobs import TERMINAL, JobManager
from app.datasets.registry import DATASET_ID_RE, DatasetError
from app.ingest.documentation import (
    MAX_DOC_BYTES,
    SUPPORTED_DOC_SUFFIXES,
    DocumentationError,
    apply_documentation,
    parse_document,
)
from app.ingest.generic_pipeline import analyze_files
from app.ingest.generic_types import IngestOptions

router = APIRouter(prefix="/api")

ALLOWED_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xlsm"}
DOC_SUFFIXES = {".md", ".markdown", ".txt", ".json", ".yaml", ".yml"}
UPLOAD_ID_RE = re.compile(r"^up_[a-f0-9]{8,32}$")


class AnalyzeRequest(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    dataset_id: str | None = Field(default=None, max_length=64)
    totals_policy: str = "exclude"       # exclude | keep
    pii_policy: str = "mask"             # mask | show
    include_files: list[str] | None = None
    include_suspected: bool = False


class RebuildRequest(BaseModel):
    totals_policy: str = "exclude"
    pii_policy: str = "mask"
    include_files: list[str] | None = None
    include_suspected: bool = False


class ReviewDecision(BaseModel):
    item_id: str
    action: str = "accept"          # accept | reject | waive
    note: str | None = None


class MaterializeRequest(BaseModel):
    name: str | None = Field(default=None, max_length=80)


def _ingest_options(body: AnalyzeRequest | RebuildRequest | None) -> IngestOptions:
    if body is None:
        return IngestOptions()
    if body.totals_policy not in {"exclude", "keep"}:
        raise HTTPException(status_code=400, detail="totals_policy 非法")
    if body.pii_policy not in {"mask", "show"}:
        raise HTTPException(status_code=400, detail="pii_policy 非法")
    return IngestOptions(
        totals_policy=body.totals_policy,
        pii_policy=body.pii_policy,
        include_files=body.include_files,
        include_suspected=body.include_suspected,
    )


def _jobs(request: Request) -> JobManager:
    manager = getattr(request.app.state, "jobs", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="任务服务尚未初始化")
    return manager


def _upload_dir(upload_id: str) -> Path:
    if not UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(status_code=404, detail="上传记录不存在")
    return Path(UPLOADS_DIR) / upload_id


def _read_meta(upload_id: str) -> dict:
    path = _upload_dir(upload_id) / "upload.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="上传记录不存在")
    return json.loads(path.read_text(encoding="utf-8"))


@router.post("/uploads")
async def create_upload(request: Request,
                        files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="没有上传文件")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400,
                            detail=f"一次最多上传 {MAX_UPLOAD_FILES} 个文件")
    upload_id = f"up_{uuid.uuid4().hex[:12]}"
    root = Path(UPLOADS_DIR) / upload_id / "original"
    root.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    total = 0
    try:
        for item in files:
            filename = Path(item.filename or "upload").name
            suffix = Path(filename).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise HTTPException(
                    status_code=400,
                    detail=f"不支持的文件类型: {filename}（支持 CSV/XLSX/TSV）",
                )
            target = root / filename
            digest = hashlib.sha256()
            size = 0
            with target.open("wb") as out:
                while True:
                    chunk = await item.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if size > MAX_UPLOAD_BYTES or total > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"文件超过上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
                        )
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail=f"文件为空: {filename}")
            saved.append({
                "name": filename,
                "size": size,
                "sha256": digest.hexdigest(),
                "path": str(target),
            })
    except Exception:
        shutil.rmtree(Path(UPLOADS_DIR) / upload_id, ignore_errors=True)
        raise

    meta = {
        "upload_id": upload_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "files": saved,
    }
    (_upload_dir(upload_id) / "upload.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "upload_id": upload_id,
        "files": [{"name": f["name"], "size": f["size"], "sha256": f["sha256"]}
                  for f in saved],
    }


@router.get("/uploads")
def list_uploads(limit: int = 50):
    root = Path(UPLOADS_DIR)
    items = []
    if root.exists():
        for path in sorted(root.glob("up_*/upload.json"), reverse=True)[:limit]:
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return {"uploads": items}


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: str):
    return _read_meta(upload_id)


@router.post("/uploads/{upload_id}/analyze")
def analyze_upload(upload_id: str, body: AnalyzeRequest | None,
                   request: Request):
    meta = _read_meta(upload_id)
    files = [Path(f["path"]) for f in meta.get("files", [])]
    if not files:
        raise HTTPException(status_code=400, detail="上传记录没有文件")
    dataset_id = body.dataset_id if body and body.dataset_id else (
        "ds_" + uuid.uuid4().hex[:12])
    if not DATASET_ID_RE.match(dataset_id):
        raise HTTPException(status_code=400, detail="非法 dataset_id")
    name = (body.name if body and body.name else Path(files[0]).stem)[:80]
    root = Path(DATASETS_DIR) / dataset_id
    if root.exists():
        raise HTTPException(status_code=409, detail="数据集已存在")

    manager = _jobs(request)
    service = getattr(request.app.state, "service", None)
    registry = getattr(service, "registry", None)

    def on_success(_result) -> None:
        if registry is not None:
            registry.refresh()
            try:
                registry.get(dataset_id)
            except DatasetError:
                pass

    options = _ingest_options(body)

    def run(*, progress):
        try:
            return analyze_files(files, root, dataset_id, name,
                                 progress=progress, options=options)
        except Exception:
            # Do not leave a half-written dataset that looks listable.
            shutil.rmtree(root, ignore_errors=True)
            raise

    job_id = manager.submit("ingest", run, params={
        "dataset_id": dataset_id,
        "name": name,
        "upload_id": upload_id,
        "files": [f.name for f in files],
    }, on_success=on_success)
    return {"job_id": job_id, "dataset_id": dataset_id, "name": name}


@router.post("/uploads/{upload_id}/agent-profile")
def agent_profile(upload_id: str, request: Request):
    """Run the ingest agent as an audit/profile job (does not publish)."""
    meta = _read_meta(upload_id)
    files = [Path(f["path"]) for f in meta.get("files", [])]
    supported = [
        path for path in files
        if path.suffix.lower() in {".csv", ".tsv", ".xlsx", ".xlsm"}
    ]
    if not supported:
        raise HTTPException(status_code=400, detail="没有可供 agent 解析的表格文件")
    client = getattr(request.app.state, "agent_client", None) or LLMClient()
    if not client.available:
        raise HTTPException(status_code=503, detail="未配置模型，无法运行解析 agent")

    out_dir = Path(RUNTIME_DIR) / "ingest_agent" / (
        f"{upload_id}-{uuid.uuid4().hex[:6]}"
    )
    manager = _jobs(request)

    def run(*, progress):
        from app.ingest.agent.service import run_ingest_agent, summarize_state

        progress("agent", 10, "启动解析 agent")
        state = run_ingest_agent(supported[0].parent, out_dir, client)
        progress("agent", 100, "解析完成")
        return summarize_state(state, out_dir)

    job_id = manager.submit("ingest_agent", run, params={
        "upload_id": upload_id,
        "out_dir": str(out_dir),
    })
    return {"job_id": job_id, "upload_id": upload_id, "out_dir": str(out_dir)}


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request):
    manager = _jobs(request)
    record = manager.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    out_dir = (record.get("params") or {}).get("out_dir")
    if not out_dir:
        raise HTTPException(status_code=404, detail="任务没有事件目录")
    path = Path(out_dir) / "ingest" / "events.jsonl"

    async def generate():
        sent = 0
        started = time.monotonic()
        while True:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            for line in lines[sent:]:
                if line.strip():
                    yield f"data: {line}\n\n"
            sent = len(lines)
            current = manager.get(job_id)
            terminal = bool(current and current.get("status") in TERMINAL)
            if terminal and sent >= len(lines):
                yield "event: done\ndata: {}\n\n"
                break
            if time.monotonic() - started > 300:
                yield "event: timeout\ndata: {}\n\n"
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(generate(), media_type="text/event-stream")


def _review_path(job_id: str, request: Request) -> tuple[Path, dict]:
    manager = _jobs(request)
    record = manager.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    out_dir = (record.get("params") or {}).get("out_dir")
    if not out_dir:
        raise HTTPException(status_code=404, detail="任务没有输出目录")
    return Path(out_dir) / "ingest", record


@router.get("/jobs/{job_id}/review")
def get_job_review(job_id: str, request: Request):
    ingest_dir, record = _review_path(job_id, request)
    draft = {}
    path = ingest_dir / "draft.json"
    if path.exists():
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            draft = {}
    review = {}
    review_path = ingest_dir / "review.json"
    if review_path.exists():
        try:
            review = json.loads(review_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            review = {}
    return {
        "job_id": job_id,
        "status": record.get("status"),
        "draft": draft,
        "review": review,
    }


@router.post("/jobs/{job_id}/review")
def submit_job_review(job_id: str, decisions: list[ReviewDecision],
                      request: Request):
    ingest_dir, _ = _review_path(job_id, request)
    ingest_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job_id,
        "decisions": [decision.model_dump() for decision in decisions],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    (ingest_dir / "review.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return payload


@router.post("/jobs/{job_id}/review/apply")
def apply_job_review(job_id: str, request: Request):
    ingest_dir, _ = _review_path(job_id, request)
    from app.ingest.agent.review import apply_review

    try:
        return apply_review(ingest_dir)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"应用审核结果失败: {type(exc).__name__}: {exc}"
        ) from exc


@router.post("/jobs/{job_id}/materialize")
def materialize_job(job_id: str, request: Request,
                    body: MaterializeRequest | None = None):
    manager = _jobs(request)
    record = manager.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    params = record.get("params") or {}
    upload_id = params.get("upload_id")
    out_dir = params.get("out_dir")
    if not upload_id or not out_dir:
        raise HTTPException(status_code=400, detail="任务缺少上传记录或输出目录")
    dataset_id = "ds_" + uuid.uuid4().hex[:12]
    root = Path(DATASETS_DIR) / dataset_id
    if root.exists():
        raise HTTPException(status_code=409, detail="数据集已存在")
    name = (body.name if body and body.name else upload_id)[:80]
    try:
        from app.ingest.agent.materialize import materialize

        result = materialize(
            _upload_dir(upload_id) / "original",
            Path(out_dir) / "ingest",
            root,
            dataset_id,
            name,
        )
    except Exception as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(
            status_code=400,
            detail=f"生成数据集失败: {type(exc).__name__}: {exc}",
        ) from exc

    service = getattr(request.app.state, "service", None)
    registry = getattr(service, "registry", None)
    if registry is not None:
        registry.refresh()
    return {**result, "name": name}


@router.post("/datasets/{dataset_id}/rebuild")
def rebuild_dataset(dataset_id: str, request: Request,
                    body: RebuildRequest | None = None):
    service = getattr(request.app.state, "service", None)
    registry = getattr(service, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="数据集注册表未初始化")
    try:
        ctx = registry.get(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    raw_dir = ctx.root / "raw"
    files = [
        p for p in sorted(raw_dir.iterdir())
        if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES
    ] if raw_dir.exists() else []
    if not files:
        raise HTTPException(status_code=400, detail="数据源没有可重建的原始文件")
    options = _ingest_options(body)
    backup = Path(RUNTIME_DIR) / "rebuild_backups" / (
        f"{dataset_id}-{datetime.now():%Y%m%d%H%M%S}"
    )
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ctx.root, backup)

    def on_success(_result) -> None:
        registry.invalidate(dataset_id)
        registry.refresh()
        try:
            registry.get(dataset_id)
        except DatasetError:
            pass
        if service is not None:
            service.invalidate_dataset(dataset_id)

    old_semantic: dict = {}
    try:
        old_semantic = yaml.safe_load(ctx.semantic_path.read_text(encoding="utf-8")) or {}
    except Exception:
        old_semantic = {}

    def run(*, progress):
        try:
            result = analyze_files(files, ctx.root, dataset_id, ctx.name,
                                   progress=progress, options=options)
            if old_semantic:
                try:
                    from app.datasets.merge import merge_semantics

                    semantic_path = ctx.root / "semantic.yaml"
                    new_semantic = yaml.safe_load(
                        semantic_path.read_text(encoding="utf-8")
                    ) or {}
                    catalog = json.loads(
                        (ctx.root / "catalog.json").read_text(encoding="utf-8")
                    )
                    merged, merge_warnings = merge_semantics(
                        new_semantic, old_semantic, catalog,
                    )
                    semantic_path.write_text(
                        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
                        encoding="utf-8",
                    )
                    result.warnings.extend(merge_warnings)
                except Exception as exc:
                    result.warnings.append(
                        f"口径保留失败: {type(exc).__name__}: {exc}"
                    )
            return result
        except Exception:
            # Restore the previous dataset so a failed rebuild is not visible.
            shutil.rmtree(ctx.root, ignore_errors=True)
            shutil.copytree(backup, ctx.root)
            raise

    job_id = _jobs(request).submit("rebuild", run, params={
        "dataset_id": dataset_id,
        "backup": str(backup),
        "options": options.as_dict(),
    }, on_success=on_success)
    return {"job_id": job_id, "dataset_id": dataset_id, "backup": str(backup)}


def _decode_doc(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


@router.post("/datasets/{dataset_id}/documentation")
async def upload_documentation(dataset_id: str, request: Request,
                               file: UploadFile = File(...)):
    service = getattr(request.app.state, "service", None)
    registry = getattr(service, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="数据集注册表未初始化")
    try:
        ctx = registry.get(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    filename = Path(file.filename or "README.md").name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_DOC_SUFFIXES:
        raise HTTPException(status_code=400,
                            detail="口径文档仅支持 md/txt/json/yaml")
    data = await file.read(MAX_DOC_BYTES + 1)
    if len(data) > MAX_DOC_BYTES:
        raise HTTPException(status_code=413, detail="口径文档超过 2MB")
    text = _decode_doc(data)
    docs_dir = ctx.root / "docs"
    docs_dir.mkdir(exist_ok=True)
    target = docs_dir / filename
    target.write_bytes(data)
    try:
        parsed = parse_document(text, filename, ctx)
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail=f"口径解析失败: {type(exc).__name__}: {exc}")
    parsed["size"] = len(data)
    parsed["path"] = str(target.relative_to(ctx.root))
    (ctx.root / "documentation.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    return parsed


@router.get("/datasets/{dataset_id}/documentation")
def get_documentation(dataset_id: str, request: Request):
    service = getattr(request.app.state, "service", None)
    registry = getattr(service, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="数据集注册表未初始化")
    try:
        ctx = registry.get(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    path = ctx.root / "documentation.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="尚未上传口径文档")
    return json.loads(path.read_text(encoding="utf-8"))


@router.post("/datasets/{dataset_id}/documentation/apply")
def apply_documentation_endpoint(dataset_id: str, request: Request):
    service = getattr(request.app.state, "service", None)
    registry = getattr(service, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="数据集注册表未初始化")
    try:
        ctx = registry.get(dataset_id)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    path = ctx.root / "documentation.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="尚未上传口径文档")
    documentation = json.loads(path.read_text(encoding="utf-8"))
    try:
        result = apply_documentation(ctx, documentation)
    except DocumentationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    registry.invalidate(dataset_id)
    registry.refresh()
    if service is not None:
        service.invalidate_dataset(dataset_id)
    return result


@router.get("/jobs")
def list_jobs(request: Request, limit: int = 50):
    return {"jobs": _jobs(request).list(limit)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    record = _jobs(request).get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return record


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request):
    ok = _jobs(request).cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"cancelled": job_id}
