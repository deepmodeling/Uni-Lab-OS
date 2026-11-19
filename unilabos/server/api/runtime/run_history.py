"""V1 ``/api/v1/history/*``：工作流任务与作业运行历史（调度权威回答，不经 edge 代理）。

dev 原来占用该前缀的事件 / 载荷存储已移到 ``/api/v1/edge/history/*``（``api/history.py``）。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Query

from unilabos.server.api.envelope import WORKFLOW, WORKFLOW_NOT_FOUND, ApiError, envelope_router
from unilabos.server.services.runtime.workflow.run_history import RunHistory


def create_run_history_router(service: Any) -> APIRouter:
    """V1 运行历史路由；``service`` 是持有 runtime.db 的 WorkflowService。"""

    router = envelope_router(profile=WORKFLOW, prefix="/api/v1/history", tags=["history"])
    history = RunHistory(service)

    @router.get("/workflows", summary="工作流运行历史列表")
    def history_workflows(
        state: str = Query(default=""),
        since: float = Query(default=0.0),
        limit: int = Query(default=100),
        with_spec: bool = Query(default=False),
    ) -> Dict[str, Any]:
        """运行历史列表（新→旧）+ 总量统计。"""

        return {
            "runs": history.list_runs(state=state, since=since, limit=limit, with_spec=with_spec),
            "stats": history.stats(),
        }

    @router.get("/workflows/{workflow_id}", summary="单次运行详情")
    def history_workflow_detail(workflow_id: str, with_spec: bool = Query(default=True)) -> Dict[str, Any]:
        """单次运行详情：运行元信息 + 提交时整图快照 + 全部作业记录。"""

        run = history.get_run(workflow_id, with_spec=with_spec)
        if run is None:
            raise ApiError(WORKFLOW_NOT_FOUND, f"没有工作流运行 {workflow_id} 的历史")
        run["jobs"] = history.list_jobs(workflow_id=workflow_id, limit=2000)
        return run

    @router.get("/jobs", summary="作业执行历史")
    def history_jobs(
        workflow_id: str = Query(default=""),
        device_id: str = Query(default=""),
        limit: int = Query(default=200),
    ) -> Dict[str, Any]:
        """作业执行历史（新→旧；按任务 / 设备过滤）。"""

        return {"jobs": history.list_jobs(workflow_id=workflow_id, device_id=device_id, limit=limit)}

    return router


__all__ = ["create_run_history_router"]
