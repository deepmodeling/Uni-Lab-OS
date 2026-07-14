"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from scheduler.api.router import router
from scheduler.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化 (如有需要)
    yield
    # 关闭时清理


app = FastAPI(
    title="uni-lab-scheduler",
    version="0.1.0",
    description="Laboratory experiment scheduling microservice",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
