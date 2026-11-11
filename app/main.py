"""FastAPI entrypoint.

Run:  ./.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.api.uploads import router as uploads_router
from app.config.settings import (
    ALLOW_REMOTE,
    API_TOKEN,
    BASE_DIR,
    HOST,
    JOBS_DIR,
    LOOPBACK_HOSTS,
    PORT,
)
from app.core.jobs import JobManager
from app.factory import build_service

DIST_DIR = BASE_DIR / "web" / "dist"

# Local-only by default. Binding a non-loopback interface exposes the whole
# warehouse, every session transcript and the upload API, so require an
# explicit token (or an explicit opt-in) before that can happen.
if not API_TOKEN and not ALLOW_REMOTE and HOST not in LOOPBACK_HOSTS:
    raise RuntimeError(
        f"HOST={HOST} 不是本机地址：请设置 API_TOKEN，或显式设置 ALLOW_REMOTE=1"
    )

PROTECTED_PREFIXES = ("/api", "/docs", "/redoc", "/openapi.json")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.jobs = JobManager(JOBS_DIR)
    app.state.service = build_service()
    yield


app = FastAPI(title="明账 · 对话式经营分析", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    path = request.url.path
    if API_TOKEN and path != "/api/health" and any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in PROTECTED_PREFIXES
    ):
        supplied = request.headers.get("x-api-token") or ""
        authorization = request.headers.get("authorization") or ""
        if not supplied and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if supplied != API_TOKEN:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:8000", "http://127.0.0.1:8000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(uploads_router)

if DIST_DIR.exists():
    if (DIST_DIR / "assets").exists():
        app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(DIST_DIR / "index.html")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        root = DIST_DIR.resolve()
        candidate = (DIST_DIR / path).resolve()
        # The catch-all must never serve files outside the built frontend.
        if candidate != root and root not in candidate.parents:
            raise HTTPException(status_code=404, detail="Not found")
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
