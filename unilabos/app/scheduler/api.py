"""Edge scheduler REST 面（FastAPI thin layer）。

端点：

    GET  /api/v1/health
    POST /api/v1/workflows                  提交工作流（触发点 1：立即重排）
    GET  /api/v1/workflows                  全量快照
    GET  /api/v1/workflows/{workflow_id}    单工作流状态
    POST /api/v1/workflows/{workflow_id}/cancel
    POST /api/v1/jobs/{job_id}/finish       子 action 完成回调（触发点 2：立即重排）
    POST /api/v1/reschedule                 手动重排（调试用）
    GET  /api/v1/error-decisions            等待人工决策的 action 异常
    POST /api/v1/error-decisions/{decision_id}  提交决策（retry/skip/abort/干预）
    GET  /api/v1/status-incidents           设备状态 incident 与 dispatch 暂停
    POST /api/v1/status-incidents/{id}      提交 Host 公布的联锁选择
    GET  /api/v1/monitor/events             SSE 实时事件流（五通道监控面板）
    GET  /api/v1/monitor/snapshot           监控一次性快照（面板初始填充）
    GET  /api/v1/device-state               全量设备属性当前值（分设备分组）
    GET  /api/v1/device-state/{device_id}   单设备当前值
    GET  /api/v1/device-state/{device_id}/history  单属性变化轨迹
    POST /api/v1/device-state/report        上报入口（非 ROS 设备 / 调试）
    GET  /api/v1/history/workflows          工作流运行历史列表（持久化，跨重启）
    GET  /api/v1/history/workflows/{id}     单次运行详情（含提交时整图 spec）
    GET  /api/v1/history/jobs               job 执行历史（按 workflow/device 过滤）
"""

from __future__ import annotations

import asyncio
import json
import queue as queue_mod
import time
from typing import Any, Dict, List, Optional

from typing import Callable

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from unilabos.app.scheduler.dag_state import WorkflowCycleError
from unilabos.app.scheduler.models import spec_from_dict
from unilabos.app.scheduler.monitor import CHANNELS, monitor_bus
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.utils.tracing import install_http_tracing


class HandleIn(BaseModel):
    """workflow_handle_template 子集；uuid 是规范引用，(node_id, handle_key) 为无 uuid 时的兼容寻址。"""

    uuid: str = ""
    data_source: str = ""
    handle_key: str = ""
    data_key: str = ""
    node_id: str = ""
    io_type: str = ""  # source / target


class MaterialRequirementIn(BaseModel):
    template_id: str = ""
    lot_id: str = ""
    quantity: float = 0.0
    unit: str = ""
    instance_uuid: str = ""
    barcode: str = ""


class NodeIn(BaseModel):
    id: str
    device_id: str = ""
    action_name: str = ""
    action_type: str = ""
    param: Dict[str, Any] = Field(default_factory=dict)
    # 云端 workflow_node 类型枚举：Group / ILab / py_script / tool_call / manual_confirm
    # （spec_from_dict 会做大小写归一，旧值 "ilab" 兼容）
    node_type: str = "ILab"
    disabled: bool = False
    material_requirements: List[MaterialRequirementIn] = Field(default_factory=list)


class EdgeIn(BaseModel):
    """workflow_edge 子集；handle uuid 是规范引用（云端定稿），key 字段为无 uuid 时的兼容寻址。"""

    uuid: str = ""
    source_node_id: str
    target_node_id: str
    source_handle_uuid: str = ""
    target_handle_uuid: str = ""
    source_handle_key: str = ""
    target_handle_key: str = ""


class WorkflowSubmitIn(BaseModel):
    workflow_id: str
    nodes: List[NodeIn]
    edges: List[EdgeIn] = Field(default_factory=list)
    handles: List[HandleIn] = Field(default_factory=list)
    priority: Any = 1.0
    lab_id: str = ""
    task_id: str = ""


class JobFinishIn(BaseModel):
    success: bool = True
    ret_value: Any = None
    suc_type: str = "normal"  # normal / skip / operator_intervention


class ErrorDecisionIn(BaseModel):
    """人工审批结果（与云端 ws job_error_decision 消息同语义）。"""

    action: str = ""  # retry / skip / abort / 其它已配置 option
    option: Optional[Dict[str, Any]] = None  # 或直接回传选中的 option 对象
    result: Any = None  # operator_intervention 的服务端结果
    reason: str = ""


class DeviceStateReportIn(BaseModel):
    """设备属性上报（值仅允许标量 str/int/float/bool）。"""

    device_id: str
    properties: Dict[str, Any] = Field(default_factory=dict)


class StatusIncidentActionIn(BaseModel):
    """只能回传 Host 在 incident.options 中公布的选择。"""

    action: str = ""
    option: Optional[Dict[str, Any]] = None
    reason: str = ""


def create_scheduler_router(
    get_scheduler: Callable[[], Optional[EdgeScheduler]],
    get_backend: Optional[Callable[[], Any]] = None,
    get_device_state: Optional[Callable[[], Any]] = None,
    get_history: Optional[Callable[[], Any]] = None,
    *,
    include_execution_shaped_workflow_routes: bool = True,
) -> APIRouter:
    """调度器 REST 路由（可挂独立 app，也可挂主进程 web server）。

    ``get_scheduler`` 动态取实例：主进程里 ``setup_edge_scheduler`` 的装配时机
    可能晚于 web server 启动，绑定 getter 而非实例。
    ``get_backend`` 提供 JobExecutionBackend（本地异常决策审批入口）；
    不传时 error-decisions 端点返回 503。
    ``get_device_state`` 提供 DeviceStateStore；不传时退回
    ``get_backend().device_state``，都没有则 device-state 端点 503。
    ``get_history`` 提供 WorkflowHistoryStore；不传时退回调度器内部
    ``_history``，都没有则 history 端点 503。
    """
    router = APIRouter(prefix="/api/v1", tags=["edge-scheduler"])

    def _sched() -> EdgeScheduler:
        scheduler = get_scheduler()
        if scheduler is None:
            raise HTTPException(status_code=503, detail="edge scheduler not enabled")
        return scheduler

    @router.get("/health")
    def health() -> Dict[str, str]:
        return {
            "status": "ok",
            "scheduler": "ready" if get_scheduler() is not None else "disabled",
        }

    @router.get("/hostlink/peers")
    def hostlink_peers() -> Dict[str, Any]:
        """微后端组网状态：Host 列 Slave，Slave 报连接及已接收 ROS 配置。"""
        from unilabos.hostlink.client import get_hostlink_client
        from unilabos.hostlink.server import get_hostlink_server

        link_server = get_hostlink_server()
        link_client = get_hostlink_client()
        role = "host" if link_server else ("slave" if link_client else "disabled")
        result: Dict[str, Any] = {
            "role": role,
            "peers": link_server.peers() if link_server else [],
            "client": (
                {
                    "online": link_client.online,
                    "host": link_client.host,
                    "port": link_client.port,
                    "node_id": link_client.node_id,
                    "device_ids": link_client.device_ids,
                    "capabilities": link_client.capabilities,
                }
                if link_client
                else None
            ),
        }
        # 保持原 role/peers/client 形状；仅在通路存在时追加微后端职责和
        # ROS 下发快照，旧前端可无感忽略，新前端可据此核对配置来源。
        if role != "disabled":
            hello = link_server.hello_payload if link_server else link_client.hello_info
            result["owner"] = hello.get("owner")
            result["host_id"] = hello.get("host_id") or hello.get("host_name")
            result["host_node_id"] = hello.get("host_node_id")
            result["protocol_version"] = hello.get("protocol_version")
            result["ros"] = hello.get("ros")
        return result

    # 旧接口把一次执行错误命名成 Workflow。共享前端 Interface 中 /workflows
    # 只表示定义，执行统一由 /workflow-tasks 表示；旧形状仅供显式兼容测试。
    if include_execution_shaped_workflow_routes:

        @router.post("/workflows")
        def submit_workflow(body: WorkflowSubmitIn) -> Dict[str, Any]:
            spec = spec_from_dict(body.model_dump())
            try:
                return _sched().submit_workflow(spec)
            except WorkflowCycleError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @router.get("/workflows")
        def all_workflows() -> Dict[str, Any]:
            return _sched().snapshot()

        @router.get("/workflows/{workflow_id}")
        def workflow_detail(workflow_id: str) -> Dict[str, Any]:
            snap = _sched().workflow_snapshot(workflow_id)
            if snap is None:
                raise HTTPException(
                    status_code=404, detail=f"workflow {workflow_id} not found"
                )
            return snap

        @router.post("/workflows/{workflow_id}/cancel")
        def cancel_workflow(workflow_id: str) -> Dict[str, Any]:
            if not _sched().cancel_workflow(workflow_id):
                raise HTTPException(
                    status_code=404, detail=f"workflow {workflow_id} not found"
                )
            return {"workflow_id": workflow_id, "state": "canceled"}

    @router.post("/jobs/{job_id}/finish")
    def finish_job(job_id: str, body: JobFinishIn) -> Dict[str, Any]:
        return _sched().on_job_finished(
            job_id, body.success, body.ret_value, body.suc_type
        )

    @router.post("/reschedule")
    def manual_reschedule() -> Dict[str, Any]:
        return {"dispatched": _sched().reschedule()}

    def _status_incidents() -> Any:
        backend = get_backend() if get_backend is not None else None
        manager = getattr(backend, "status_incidents", None) if backend is not None else None
        if manager is None:
            raise HTTPException(
                status_code=503, detail="edge execution backend not enabled"
            )
        return backend, manager

    @router.get("/status-incidents")
    def status_incidents(
        device_id: str = "", include_terminal: bool = False
    ) -> Dict[str, Any]:
        backend, manager = _status_incidents()
        host_ready = bool(backend.host_ready())
        return {
            "host_ready": host_ready,
            "incidents": (
                manager.list(
                    device_id=device_id, include_terminal=include_terminal
                )
                if host_ready
                else []
            ),
            "holds": manager.holds() if host_ready else [],
        }

    @router.post("/status-incidents/{incident_id}")
    def decide_status_incident(
        incident_id: str, body: StatusIncidentActionIn
    ) -> Dict[str, Any]:
        backend, manager = _status_incidents()
        if not backend.host_ready():
            raise HTTPException(status_code=503, detail="host node not ready")
        try:
            result = manager.decide(
                incident_id,
                action=body.action,
                option=body.option,
                reason=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="status incident not found")
        return result["ack"]

    @router.post("/status-incidents/{incident_id}/acknowledge")
    def acknowledge_status_incident(
        incident_id: str, body: StatusIncidentActionIn
    ) -> Dict[str, Any]:
        _, manager = _status_incidents()
        incident = manager.acknowledge(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="status incident not found")
        return incident

    @router.post("/status-incidents/{incident_id}/resolve")
    def resolve_status_incident(
        incident_id: str, body: StatusIncidentActionIn
    ) -> Dict[str, Any]:
        _, manager = _status_incidents()
        incident = manager.resolve(
            incident_id,
            reason=body.reason,
        )
        if incident is None:
            raise HTTPException(status_code=404, detail="status incident not found")
        return incident

    @router.get("/timeline")
    def timeline(window_s: float = 3600.0) -> Dict[str, Any]:
        """泳道图数据：执行中 + 窗口内已完结 job 的起止/预估 + 预估器统计。"""
        return _sched().timeline(window_s=window_s)

    # ── 实时监控（SSE 五通道：material/device/action/scheduler/status） ──

    @router.get("/monitor/events")
    async def monitor_events(
        request: Request, channels: str = "", backlog: int = 40
    ) -> StreamingResponse:
        """SSE 事件流；``channels`` 逗号分隔过滤（缺省全部），断线由前端 EventSource 自动重连。"""
        requested = {c.strip() for c in channels.split(",") if c.strip()}
        channel_filter = (requested & set(CHANNELS)) or None
        sub_id, sub_queue, replay = monitor_bus.subscribe(
            channels=channel_filter, backlog=max(0, min(backlog, 200))
        )

        def _sse(event: Dict[str, Any]) -> str:
            payload = json.dumps(event, ensure_ascii=False, default=str)
            return f"id: {event['seq']}\nevent: {event['channel']}\ndata: {payload}\n\n"

        async def stream():
            try:
                yield f"retry: 3000\n: connected channels={','.join(sorted(channel_filter or CHANNELS))}\n\n"
                for event in replay:
                    yield _sse(event)
                last_beat = time.time()
                while True:
                    if await request.is_disconnected():
                        break
                    sent = False
                    while True:
                        try:
                            event = sub_queue.get_nowait()
                        except queue_mod.Empty:
                            break
                        yield _sse(event)
                        sent = True
                    if sent:
                        last_beat = time.time()
                        continue
                    if time.time() - last_beat > 15:
                        yield ": keepalive\n\n"
                        last_beat = time.time()
                    await asyncio.sleep(0.4)
            finally:
                monitor_bus.unsubscribe(sub_id)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/monitor/snapshot")
    def monitor_snapshot() -> Dict[str, Any]:
        """监控面板初始填充：设备占用视图 + 调度概览 + 各通道最近事件。"""
        scheduler = _sched()
        snap = scheduler.snapshot()
        workflow_states: Dict[str, int] = {}
        for run in snap["workflows"].values():
            state = run.get("state", "unknown")
            workflow_states[state] = workflow_states.get(state, 0) + 1
        backend = get_backend() if get_backend is not None else None
        status_incidents = (
            getattr(backend, "status_incidents", None)
            if backend is not None
            else None
        )
        return {
            "now": time.time(),
            "host_ready": bool(backend is not None and backend.host_ready()),
            "pending_error_decisions": (
                backend.list_error_decisions() if backend is not None else []
            ),
            "active_status_incidents": (
                status_incidents.list() if status_incidents is not None else []
            ),
            "scheduler_holds": (
                status_incidents.holds() if status_incidents is not None else []
            ),
            "devices": scheduler.device_status(),
            "scheduler": {
                "workflow_states": workflow_states,
                "inflight": len(snap["inflight_jobs"]),
                "reschedule_count": snap["reschedule_count"],
            },
            "recent": {
                channel: monitor_bus.recent(channel, 40) for channel in CHANNELS
            },
        }

    def _backend() -> Any:
        backend = get_backend() if get_backend is not None else None
        if backend is None:
            raise HTTPException(
                status_code=503, detail="edge execution backend not enabled"
            )
        return backend

    # ── 工作流执行历史（第三个独立 SQLite，跨进程重启保留） ──

    def _history() -> Any:
        store = get_history() if get_history is not None else None
        if store is None:
            scheduler = get_scheduler()
            store = (
                getattr(scheduler, "_history", None) if scheduler is not None else None
            )
        if store is None:
            raise HTTPException(
                status_code=503, detail="workflow history store not enabled"
            )
        return store

    @router.get("/history/workflows")
    def history_workflows(
        state: str = "",
        since: float = 0.0,
        limit: int = 100,
        with_spec: bool = False,
    ) -> Dict[str, Any]:
        """运行历史列表（新→旧）+ 总量统计。"""
        store = _history()
        return {
            "runs": store.list_runs(
                state=state,
                since=since,
                limit=limit,
                with_spec=with_spec,
            ),
            "stats": store.stats(),
        }

    @router.get("/history/workflows/{workflow_id}")
    def history_workflow_detail(
        workflow_id: str, with_spec: bool = True
    ) -> Dict[str, Any]:
        """单次运行详情：run 元信息 + 提交时整图 spec + 全部 job 记录。"""
        store = _history()
        run = store.get_run(workflow_id, with_spec=with_spec)
        if run is None:
            raise HTTPException(
                status_code=404, detail=f"no history for workflow {workflow_id}"
            )
        run["jobs"] = store.list_jobs(workflow_id=workflow_id, limit=2000)
        return run

    @router.get("/history/jobs")
    def history_jobs(
        workflow_id: str = "", device_id: str = "", limit: int = 200
    ) -> Dict[str, Any]:
        """job 执行历史（新→旧；workflow_id / device_id 过滤）。"""
        return {
            "jobs": _history().list_jobs(
                workflow_id=workflow_id, device_id=device_id, limit=limit
            )
        }

    # ── 设备状态（归微后端管；独立 SQLite，与物料/工作流库分开） ──

    def _device_state() -> Any:
        store = get_device_state() if get_device_state is not None else None
        if store is None and get_backend is not None:
            backend = get_backend()
            store = (
                getattr(backend, "device_state", None) if backend is not None else None
            )
        if store is None:
            raise HTTPException(
                status_code=503, detail="device state store not enabled"
            )
        return store

    @router.get("/device-state")
    def device_state_all() -> Dict[str, Any]:
        """全量设备属性当前值 + 存储统计。"""
        store = _device_state()
        return {"devices": store.latest_all(), "stats": store.stats()}

    @router.get("/device-state/history")
    def device_state_history_all(since_ms: int = 0, limit: int = 500) -> Dict[str, Any]:
        """跨设备/属性的最近变化点（新→旧）。"""
        return {"entries": _device_state().history_all(since_ms, limit)}

    @router.get("/device-state/{device_id:path}/history")
    def device_state_history(
        device_id: str, property: str, since_ms: int = 0, limit: int = 200
    ) -> Dict[str, Any]:
        """单属性变化轨迹（新→旧；只记变化点，非采样流水）。"""
        return {
            "device_id": device_id,
            "property": property,
            "entries": _device_state().history(device_id, property, since_ms, limit),
        }

    @router.get("/device-state/{device_id:path}")
    def device_state_one(device_id: str) -> Dict[str, Any]:
        properties = _device_state().latest_for(device_id)
        if not properties:
            raise HTTPException(
                status_code=404, detail=f"no state for device {device_id}"
            )
        return {"device_id": device_id, "properties": properties}

    @router.post("/device-state/report")
    def device_state_report(body: DeviceStateReportIn) -> Dict[str, Any]:
        """上报入口：ROS 之外的设备（或调试）直接写；值必须是标量。"""
        try:
            backend = get_backend() if get_backend is not None else None
            report = getattr(backend, "report_device_properties", None)
            if callable(report):
                changed = report(body.device_id, body.properties)
            else:
                store = _device_state()
                changed = {}
                for prop, value in body.properties.items():
                    was_changed = store.set(body.device_id, prop, value)
                    changed[prop] = was_changed
                    if was_changed:
                        monitor_bus.emit(
                            "device",
                            "device_property",
                            {
                                "device_id": body.device_id,
                                "property": prop,
                                "value": value,
                            },
                        )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"device_id": body.device_id, "changed": changed}

    @router.get("/error-decisions")
    def list_error_decisions() -> Dict[str, Any]:
        """等待人工决策的 action 异常（本地审批通道）。"""
        return {"decisions": _backend().list_error_decisions()}

    @router.post("/error-decisions/{decision_id}")
    def resolve_error_decision(
        decision_id: str, body: ErrorDecisionIn
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"reason": body.reason}
        if body.option is not None:
            payload["option"] = body.option
        if body.action:
            payload["action"] = body.action
        if body.result is not None:
            payload["result"] = body.result
        if not _backend().resolve_error_decision(decision_id, payload):
            raise HTTPException(
                status_code=404,
                detail=f"decision {decision_id} not pending (resolved / timed out / device gone)",
            )
        return {"decision_id": decision_id, "status": "delivered"}

    return router


def create_app(
    scheduler: Optional[EdgeScheduler] = None,
    device_state: Any = None,
    history: Any = None,
    *,
    include_execution_shaped_workflow_routes: bool = True,
) -> FastAPI:
    app = FastAPI(title="Uni-Lab Edge Scheduler", version="0.1.0")
    install_http_tracing(app)
    # 静态站点（如 GitHub Pages 上的 unilab-edge-ui）直连本地端口需要跨域放行
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
    )
    app.state.scheduler = scheduler or EdgeScheduler(monitor=monitor_bus)
    app.state.device_state = device_state
    app.state.history = history
    app.include_router(
        create_scheduler_router(
            lambda: app.state.scheduler,
            get_device_state=lambda: app.state.device_state,
            get_history=lambda: app.state.history,
            include_execution_shaped_workflow_routes=(
                include_execution_shaped_workflow_routes
            ),
        )
    )
    return app


__all__ = ["create_app", "create_scheduler_router"]
