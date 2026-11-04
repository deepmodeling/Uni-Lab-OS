"""runtime.v1 控制面服务端 API：Edge 接入微后端的 WS 通知通道与 HTTP 命令文档。

Edge 侧对接方式与云端 Backend 完全一致：

- ``WS /api/v1/ws/schedule``：短通知通道（backend_session / backend_change /
  edge_change / edge_change_ack / runtime_logs_changed / ping / pong）
- ``GET /edge/commands/{command_uuid}``：命令权威正文
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from unilabos.protocol.runtime.control import EdgeHttpResponse
from unilabos.server.backend.edge_control import (
    EdgeControlService,
    get_edge_control_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["EdgeControl"])


def _require_service() -> EdgeControlService:
    service = get_edge_control_service()
    if service is None:
        raise HTTPException(status_code=503, detail="edge control service not ready")
    return service


@router.get("/edge/commands/{command_uuid}")
def get_edge_command(command_uuid: str) -> dict[str, Any]:
    """Edge 收到 backend_change 通知后拉取的权威命令文档。"""

    document = _require_service().get_command_document(command_uuid)
    if document is None:
        raise HTTPException(status_code=404, detail="command not found")
    return document


@router.post("/api/v1/edge/http-responses/{request_uuid}", include_in_schema=False)
def receive_edge_http_response(request_uuid: str, response: EdgeHttpResponse) -> dict[str, Any]:
    """Edge 把 ``backend_http`` 请求在进程内执行后的结果送回这里（Edge 自己不监听端口）。"""

    if response.request_uuid != request_uuid:
        raise HTTPException(status_code=422, detail="request_uuid mismatch")
    accepted = _require_service().complete_http_response(response)
    return {"accepted": accepted}


async def _pump_outgoing(
    websocket: WebSocket,
    service: EdgeControlService,
    epoch: str,
    send_lock: Optional[asyncio.Lock] = None,
) -> None:
    """把服务的线程安全下行队列泵到当前 WS 连接。"""

    while service.connection_epoch == epoch:
        try:
            message = await asyncio.to_thread(service.outgoing.get, True, 0.5)
        except queue.Empty:
            continue
        encoded = json.dumps(message, ensure_ascii=False)
        if send_lock is None:
            await websocket.send_text(encoded)
        else:
            async with send_lock:
                await websocket.send_text(encoded)


async def _pump_incoming(
    websocket: WebSocket,
    service: EdgeControlService,
    epoch: str,
    send_lock: Optional[asyncio.Lock] = None,
) -> None:
    # edge_change 的正文处理可能访问 Edge HTTP 数据面（秒级甚至更久）。
    # 它们保持 FIFO，但不能占住 receive_text；ping/pong 走下面的快速分支。
    # 不设有限容量：队列里只有短通知，且限制容量会让 receive_text 在
    # 高峰期反过来挡住后续 ping。正文仍由 durable HTTP/事件存储限流。
    business_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    async def _business_worker() -> None:
        while True:
            action, data = await business_queue.get()
            try:
                # handle_message 内部可能同步拉取 Edge HTTP payload，放线程池。
                reply: Optional[dict[str, Any]] = await asyncio.to_thread(
                    service.handle_message, action, data
                )
                if reply is not None and service.connection_epoch == epoch:
                    encoded = json.dumps(reply, ensure_ascii=False)
                    if send_lock is None:
                        await websocket.send_text(encoded)
                    else:
                        async with send_lock:
                            await websocket.send_text(encoded)
            except Exception:  # noqa: BLE001 - 单条上行事件失败不拖垮心跳
                logger.exception("[EdgeControl] 处理上行 %s 失败", action)
            finally:
                business_queue.task_done()

    worker = asyncio.create_task(
        _business_worker(), name="edge-control-business"
    )
    try:
        while service.connection_epoch == epoch:
            raw = await websocket.receive_text()
            try:
                envelope = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                logger.warning("[EdgeControl] 忽略无效 JSON 上行消息")
                continue
            if not isinstance(envelope, dict):
                continue
            action = str(envelope.get("action") or "")
            data = envelope.get("data", {})
            if not isinstance(data, dict):
                continue

            if action in {"ping", "pong", "runtime_logs_changed"}:
                # 心跳和有界日志轻通知不访问数据库，不等待业务 handler。
                try:
                    reply = service.handle_message(action, data)
                except Exception:  # noqa: BLE001 - 坏通知只丢弃当前消息
                    logger.exception("[EdgeControl] 处理快速通知 %s 失败", action)
                    continue
                if reply is not None and service.connection_epoch == epoch:
                    encoded = json.dumps(reply, ensure_ascii=False)
                    if send_lock is None:
                        await websocket.send_text(encoded)
                    else:
                        async with send_lock:
                            await websocket.send_text(encoded)
                continue

            # 业务消息只入队；接收循环马上继续读取后续 ping。
            await business_queue.put((action, data))
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


@router.websocket("/api/v1/ws/schedule")
async def edge_schedule_socket(websocket: WebSocket) -> None:
    service = get_edge_control_service()
    if service is None:
        await websocket.close(code=1013, reason="edge control service not ready")
        return
    await websocket.accept()
    # EdgeSession 由 Host 控制客户端在进程启动时生成、断线重连不变：用它识别 Host 是否重启过。
    epoch, session_message = service.attach_connection(edge_process=websocket.headers.get("EdgeSession", ""))
    try:
        await websocket.send_text(json.dumps(session_message, ensure_ascii=False))
        send_lock = asyncio.Lock()
        incoming = asyncio.create_task(
            _pump_incoming(
                websocket, service, epoch, send_lock
            ),
            name="edge-control-incoming",
        )
        outgoing = asyncio.create_task(
            _pump_outgoing(
                websocket, service, epoch, send_lock
            ),
            name="edge-control-outgoing",
        )
        done, pending = await asyncio.wait(
            {incoming, outgoing}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.exception("[EdgeControl] WS 通道异常", exc_info=exc)
    except WebSocketDisconnect:
        pass
    finally:
        service.detach_connection(epoch)


def install_edge_control_api(app: FastAPI) -> None:
    app.include_router(router)


__all__ = ["install_edge_control_api", "router"]
