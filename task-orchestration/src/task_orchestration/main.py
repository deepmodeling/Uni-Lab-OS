"""Task 编排 FastAPI 应用入口。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.router import create_router
from .service import WorkspaceService
from .store import WorkspaceStore


def create_app(workspace_root: Path | str | None = None) -> FastAPI:
    """创建服务应用，workspace_root 限定可读写的 workflow 目录。"""
    app = FastAPI(
        title="task-orchestration",
        version="0.1.0",
        description="Task orchestration workspace service",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5174",
            "http://localhost:5174",
            "http://127.0.0.1:8014",
            "http://localhost:8014",
        ],
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Content-Type"],
    )
    store = WorkspaceStore(workspace_root or Path.cwd())
    router = create_router(store, WorkspaceService(store))
    app.include_router(router)
    app.include_router(router, prefix="/api/v1")
    return app


app = create_app()
