"""Small persistent background-job runner for ingestion/analysis.

The process is single-writer by design (see the deployment notes), so a
thread pool with an in-memory registry plus atomic JSON records is enough.
"""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

TERMINAL = {"succeeded", "failed", "cancelled"}


class JobCancelled(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JobManager:
    def __init__(self, root: Path, max_workers: int = 2):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=max_workers,
                                            thread_name_prefix="ingest")
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel: set[str] = set()
        self._load_existing()

    # ---------- persistence ----------
    def _load_existing(self) -> None:
        for path in sorted(self.root.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("job_id"):
                if record.get("status") in {"queued", "running"}:
                    record["status"] = "failed"
                    record["error"] = "服务重启，任务已中断"
                    record["updated_at"] = _now()
                self._jobs[record["job_id"]] = record

    def _persist(self, record: dict[str, Any]) -> None:
        path = self.root / f"{record['job_id']}.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    # ---------- public ----------
    def submit(self, kind: str, fn: Callable[..., Any],
               params: dict[str, Any] | None = None,
               on_success: Callable[[Any], None] | None = None) -> str:
        job_id = f"j-{uuid.uuid4().hex[:12]}"
        record = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "stage": "queued",
            "percent": 0,
            "message": "等待执行",
            "created_at": _now(),
            "updated_at": _now(),
            "params": params or {},
            "result": None,
            "error": None,
        }
        with self._condition:
            self._jobs[job_id] = record
            self._persist(record)
        self._executor.submit(self._execute, job_id, fn, on_success)
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            return json.loads(json.dumps(record)) if record else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            records = sorted(self._jobs.values(), key=lambda r: r["created_at"],
                             reverse=True)
            return [json.loads(json.dumps(r)) for r in records[:limit]]

    def cancel(self, job_id: str) -> bool:
        with self._condition:
            record = self._jobs.get(job_id)
            if record is None or record["status"] in TERMINAL:
                return False
            self._cancel.add(job_id)
            if record["status"] == "queued":
                record["status"] = "cancelled"
                record["message"] = "已取消"
                record["updated_at"] = _now()
                self._persist(record)
            self._condition.notify_all()
        return True

    def wait(self, job_id: str, timeout: float = 60.0) -> dict[str, Any] | None:
        with self._condition:
            self._condition.wait_for(
                lambda: (self._jobs.get(job_id) or {}).get("status") in TERMINAL,
                timeout=timeout,
            )
            record = self._jobs.get(job_id)
            return json.loads(json.dumps(record)) if record else None

    # ---------- execution ----------
    def _execute(self, job_id: str, fn: Callable[..., Any],
                 on_success: Callable[[Any], None] | None) -> None:
        with self._condition:
            record = self._jobs.get(job_id)
            if record is None or record["status"] == "cancelled":
                return
            record["status"] = "running"
            record["stage"] = "start"
            record["updated_at"] = _now()
            self._persist(record)

        def progress(stage: str, percent: int, message: str) -> None:
            with self._condition:
                if job_id in self._cancel:
                    raise JobCancelled("任务已取消")
                current = self._jobs.get(job_id)
                if current is None:
                    raise JobCancelled("任务不存在")
                current["stage"] = stage
                current["percent"] = int(percent)
                current["message"] = message
                current["updated_at"] = _now()
                self._persist(current)

        try:
            result = fn(progress=progress)
            if on_success is not None:
                on_success(result)
        except JobCancelled:
            self._finish(job_id, "cancelled", message="已取消")
        except Exception as exc:  # keep the message short and user-visible
            self._finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}",
                         message="解析失败")
        else:
            self._finish(job_id, "succeeded", result=_summarize(result),
                         message="解析完成")

    def _finish(self, job_id: str, status: str, *, result: Any = None,
                error: str | None = None, message: str = "") -> None:
        with self._condition:
            record = self._jobs.get(job_id)
            if record is None:
                return
            record["status"] = status
            record["result"] = result
            record["error"] = error
            if message:
                record["message"] = message
            if status == "succeeded":
                record["percent"] = 100
            record["updated_at"] = _now()
            self._persist(record)
            self._condition.notify_all()


def _summarize(result: Any) -> Any:
    if hasattr(result, "as_dict"):
        data = result.as_dict()
        return {
            "dataset_id": data.get("dataset_id"),
            "name": data.get("name"),
            "tables": [
                {"table_id": t.get("table_id"), "label": t.get("label"),
                 "rows": t.get("rows")}
                for t in data.get("tables", [])
            ],
            "warnings": data.get("warnings", []),
        }
    try:
        json.dumps(result)
        return result
    except TypeError:
        return str(result)
