"""V1 任务生命周期、运行观测与异常处置路由的统一装配入口。

这些路由依赖 Workflow Authority（runtime.db），与 ``install_workflow_api`` 同时挂载在调度权威上；
按领域拆在 ``api/runtime/`` 下的各模块里，本文件只负责装配顺序。
"""

from __future__ import annotations

from fastapi import FastAPI


def install_lifecycle_v1_api(app: FastAPI, service) -> None:
    from unilabos.server.api.runtime.run_history import create_run_history_router

    app.include_router(create_run_history_router(service))


__all__ = ["install_lifecycle_v1_api"]
