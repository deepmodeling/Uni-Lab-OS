"""Local Edge 会话代际与 Kubernetes 就绪合同回归。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from unilabos.app.edge_control.local_authority import (
    LocalEdgeAuthorityStore,
    LocalEdgeControlAuthority,
    create_local_edge_control_router,
)
from unilabos.app.edge_control.local_edge_session import (
    project_local_edge_readiness,
)
from unilabos.app.scheduler.dispatch import DispatchPayload


class _StalledWebSocket:
    """用于证明半开旧连接不会持有换主锁的最小 WebSocket 替身。

    参数：构造时接收 hello 消息和是否阻塞发送。返回：可注入生产路由 handler
    的异步替身。异常：替身只传播任务取消，不模拟其他网络故障。
    """

    def __init__(self, hello: dict[str, Any], *, stall_send: bool) -> None:
        """初始化单条 hello 输入及可选的永久背压发送。

        参数：``hello`` 是首条协议消息；``stall_send`` 为真时首次发送一直等待
        任务取消。返回无。异常：无；测试通过事件观察发送入口与关闭结果。
        """

        self.headers = {"Authorization": "Bearer managed-local-secret"}
        self._incoming: asyncio.Queue[str] = asyncio.Queue()
        self._incoming.put_nowait(json.dumps(hello))
        self._stall_send = stall_send
        self._send_release = asyncio.Event()
        self.send_started = asyncio.Event()
        self.message_sent = asyncio.Event()
        self.sent_messages: list[dict[str, Any]] = []
        self.close_code: int | None = None

    async def accept(self) -> None:
        """接受测试连接。

        参数：无。返回无。异常：无；替身不执行 ASGI 握手。
        """

    async def receive_text(self) -> str:
        """读取预置 hello，随后保持连接半开。

        参数：无。返回下一条编码消息。异常：任务取消原样传播，用于模拟连接
        被新代际接管。
        """

        return await self._incoming.get()

    async def send_text(self, encoded: str) -> None:
        """记录正常发送，或模拟永不解除的 WebSocket 背压。

        参数：``encoded`` 是服务端协议消息。返回无。异常：背压等待被取消时
        ``CancelledError`` 原样传播，证明接管不依赖旧发送完成。
        """

        self.send_started.set()
        if self._stall_send:
            await self._send_release.wait()
        self.sent_messages.append(json.loads(encoded))
        self.message_sent.set()

    async def close(self, *, code: int) -> None:
        """记录服务端主动关闭码且立即返回。

        参数：``code`` 是 WebSocket 关闭码。返回无。异常：无。
        """

        self.close_code = code


def _websocket_endpoint(
    authority: LocalEdgeControlAuthority,
) -> Callable[[Any], Awaitable[None]]:
    """从生产路由中取得 WebSocket handler 以注入背压替身。

    参数：``authority`` 是测试事实权威。返回可等待的 WebSocket endpoint。
    异常：路由未注册目标 endpoint 时抛 ``AssertionError``。
    """

    router = create_local_edge_control_router(authority)
    for route in router.routes:
        if getattr(route, "path", "") == "/api/v1/edge/ws":
            return cast(Callable[[Any], Awaitable[None]], route.endpoint)
    raise AssertionError("Local Edge WebSocket route is missing")


def _authority(path: Path) -> LocalEdgeControlAuthority:
    """构造使用独立 SQLite 文件的 Local Edge Authority。

    参数：``path`` 是测试事实库路径。返回带固定测试密钥的 Authority。异常：
    数据库目录创建、连接或初始化错误原样传播。
    """

    return LocalEdgeControlAuthority(
        LocalEdgeAuthorityStore(path),
        api_key="managed-local-secret",
    )


def _durable_authority_facts(
    store: LocalEdgeAuthorityStore,
) -> dict[str, list[tuple[Any, ...]]]:
    """读取会被伪造 hello 污染的完整持久事实快照。

    参数：``store`` 是当前 Authority 的数据库 Store。返回 meta、session、command
    与 Job 全列的稳定有序元组。异常：数据库查询失败时原样传播；只读查询不写入。
    """

    query = store._connection.execute
    return {
        "meta": [
            tuple(row)
            for row in query("SELECT * FROM local_edge_meta ORDER BY id").fetchall()
        ],
        "sessions": [
            tuple(row)
            for row in query(
                "SELECT * FROM local_edge_session ORDER BY session_uuid"
            ).fetchall()
        ],
        "commands": [
            tuple(row)
            for row in query(
                "SELECT * FROM local_edge_command ORDER BY sequence"
            ).fetchall()
        ],
        "jobs": [
            tuple(row)
            for row in query("SELECT * FROM local_edge_job ORDER BY job_uuid").fetchall()
        ],
    }


def _registration_payload(
    *,
    edge_key: str = "workspace-edge",
    instance_uuid: str | None = None,
    devices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造一个完整且可被就绪合同接受的 Edge 注册。

    参数：``edge_key`` 指定 Authority 内的 Edge 身份；``instance_uuid`` 可覆盖
    实例身份；``devices`` 可覆盖设备能力数组。返回新的注册字典。异常：无；
    默认实例身份和物料身份均为新 UUID。
    """

    return {
        "edge_key": edge_key,
        "instance_uuid": instance_uuid or str(uuid.uuid4()),
        "devices": devices
        if devices is not None
        else [
            {
                "local_id": "robot-01",
                "material_uuid": str(uuid.uuid4()),
                "actions": [{"name": "transfer", "type": "UniLabJsonCommand"}],
            }
        ],
    }


def _dispatch_payload() -> DispatchPayload:
    """构造会跨入物理动作不确定边界的测试派发载荷。

    参数：无。返回包含完整 Claim 与 Fence 身份的新 ``DispatchPayload``。异常：
    无；每次调用生成彼此隔离的 UUID。
    """

    return DispatchPayload(
        job_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        node_id=str(uuid.uuid4()),
        workflow_id=str(uuid.uuid4()),
        device_id="robot-01",
        action="transfer",
        action_type="normal",
        action_args={"source": "A", "target": "B"},
        attempt=1,
        command_uuid=str(uuid.uuid4()),
        claim_uuid=str(uuid.uuid4()),
        fences=[{"lock_key": "/devices/robot-01", "fencing_token": 1}],
    )


def _hello_message(
    registration: dict[str, Any],
    *,
    process_uuid: str,
    last_ack_sequence: int = 0,
) -> dict[str, Any]:
    """构造绑定指定注册代际的 Edge hello 消息。

    参数：``registration`` 提供 Edge 与 Session 身份；``process_uuid`` 标识同一
    动作进程；``last_ack_sequence`` 是持久 ACK 游标。返回新的协议消息字典。
    异常：无；调用方负责传入规范 UUID 和非负游标。
    """

    return {
        "protocol_version": 1,
        "message_uuid": str(uuid.uuid4()),
        "sequence": 0,
        "type": "hello",
        "sent_at": "2026-09-01T00:00:00.000000Z",
        "payload": {
            "edge_uuid": registration["edge_uuid"],
            "session_uuid": registration["session_uuid"],
            "process_uuid": process_uuid,
            "last_ack_command_sequence": last_ack_sequence,
            "running_jobs": [],
        },
    }


def test_readiness_tracks_live_edge_connection_state(tmp_path: Path) -> None:
    """验证就绪探针随 Local Edge 注册和连接事实实时开闭。

    参数：``tmp_path`` 隔离本地 Edge 事实库。返回无。异常：未注册、只注册或
    断线状态未返回 503，连接且设备能力完整时未返回 200，或响应泄漏设备动作
    详情时由断言失败。
    """

    authority = _authority(tmp_path / "authority.db")
    application = FastAPI()
    application.include_router(create_local_edge_control_router(authority))
    client = TestClient(application)
    instance_uuid = str(uuid.uuid4())
    try:
        unregistered = client.get("/api/v1/edge/readiness")
        assert unregistered.status_code == 503
        assert unregistered.json() == {
            "status": "not_ready",
            "edge_key": "",
            "instance_uuid": "",
            "connected": False,
            "device_count": 0,
        }

        registration = authority.store.register_session(
            _registration_payload(instance_uuid=instance_uuid)
        )
        registered = client.get("/api/v1/edge/readiness")
        assert registered.status_code == 503
        assert registered.json() == {
            "status": "not_ready",
            "edge_key": "workspace-edge",
            "instance_uuid": instance_uuid,
            "connected": False,
            "device_count": 1,
        }

        authority.store.set_session_connected(registration["session_uuid"], True)
        connected = client.get("/api/v1/edge/readiness")
        assert connected.status_code == 200
        assert connected.json() == {
            "status": "ready",
            "edge_key": "workspace-edge",
            "instance_uuid": instance_uuid,
            "connected": True,
            "device_count": 1,
        }
        assert "devices" not in connected.json()
        assert "actions" not in connected.json()
        assert "token" not in connected.json()

        authority.store.disconnect_session(registration["session_uuid"])
        disconnected = client.get("/api/v1/edge/readiness")
        assert disconnected.status_code == 503
        assert disconnected.json()["status"] == "not_ready"
        assert disconnected.json()["connected"] is False
    finally:
        authority.stop()


def test_edge_auth_error_explains_workspace_token_requirement(tmp_path: Path) -> None:
    """Edge 鉴权失败时提示缺少或不匹配的工作区令牌及处理方式。"""

    authority = _authority(tmp_path / "authority.db")
    application = FastAPI()
    application.include_router(create_local_edge_control_router(authority))
    client = TestClient(application)
    try:
        response = client.post(
            "/api/v1/edge/sessions",
            json=_registration_payload(),
        )
        assert response.status_code == 401
        detail = response.json()["detail"]
        assert "Authorization" in detail
        assert "当前工作区" in detail
        assert "Workspace Host" in detail
    finally:
        authority.stop()


def test_new_connection_retires_stale_connected_session(tmp_path: Path) -> None:
    """新 Edge hello 必须清除 Backend 重启遗留的旧连接事实。

    参数：``tmp_path`` 隔离本地事实库。返回无。异常：第二个会话断开后仍能从
    第一个陈旧会话得到 connected=true 时由断言失败，防止实时探针误报 READY。
    """

    authority = _authority(tmp_path / "authority.db")
    try:
        first_payload = _registration_payload()
        first = authority.store.register_session(first_payload)
        authority.store.set_session_connected(first["session_uuid"], True)
        second = authority.store.register_session(_registration_payload())

        authority.store.set_session_connected(second["session_uuid"], True)
        authority.store.disconnect_session(second["session_uuid"])

        latest = authority.store.latest_registration()
        assert latest is not None
        assert latest["instance_uuid"] != first_payload["instance_uuid"]
        assert latest["connected"] is False
    finally:
        authority.stop()


def test_backend_restart_clears_non_durable_connection_fact(tmp_path: Path) -> None:
    """Backend 新进程不得沿用上一进程的 WebSocket 在线状态。

    参数：``tmp_path`` 隔离持久事实库。返回无。异常：重新打开 Authority Store
    后最新注册仍显示 connected=true 时由断言失败；注册能力本身必须继续保留。
    """

    database = tmp_path / "authority.db"
    first = _authority(database)
    instance_uuid = str(uuid.uuid4())
    try:
        registration = first.store.register_session(
            _registration_payload(instance_uuid=instance_uuid)
        )
        first.store.set_session_connected(registration["session_uuid"], True)
        assert first.store.latest_registration()["connected"] is True  # type: ignore[index]
    finally:
        first.stop()

    restarted = _authority(database)
    try:
        latest = restarted.store.latest_registration()
        assert latest is not None
        assert latest["instance_uuid"] == instance_uuid
        assert latest["connected"] is False
        assert len(latest["devices"]) == 1
    finally:
        restarted.stop()


@pytest.mark.parametrize(
    "devices",
    [
        [],
        [
            {
                "local_id": "",
                "material_uuid": "00000000-0000-4000-8000-000000000001",
                "actions": [{"name": "transfer", "type": "UniLabJsonCommand"}],
            }
        ],
        [
            {
                "local_id": "robot-01",
                "material_uuid": "not-a-uuid",
                "actions": [{"name": "transfer", "type": "UniLabJsonCommand"}],
            }
        ],
        [
            {
                "local_id": "robot-01",
                "material_uuid": "00000000-0000-4000-8000-000000000001",
                "actions": [],
            }
        ],
        [
            {
                "local_id": "robot-01",
                "material_uuid": "00000000-0000-4000-8000-000000000001",
                "actions": [{"name": "", "type": "UniLabJsonCommand"}],
            }
        ],
    ],
    ids=[
        "empty",
        "missing-local-id",
        "invalid-material",
        "empty-actions",
        "invalid-action",
    ],
)
def test_readiness_rejects_incomplete_device_capabilities(
    tmp_path: Path,
    devices: list[dict[str, Any]],
) -> None:
    """连接状态不能掩盖空设备集或损坏的设备动作声明。

    参数：``tmp_path`` 隔离事实库；``devices`` 是一种不完整能力声明。返回无。
    异常：任一非法能力声明被公开就绪合同接受时由断言失败。
    """

    authority = _authority(tmp_path / "authority.db")
    application = FastAPI()
    application.include_router(create_local_edge_control_router(authority))
    client = TestClient(application)
    try:
        registration = authority.store.register_session(
            _registration_payload(devices=devices)
        )
        authority.store.set_session_connected(registration["session_uuid"], True)

        response = client.get("/api/v1/edge/readiness")

        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert response.json()["connected"] is True
    finally:
        authority.stop()


def test_late_retired_session_close_preserves_inflight_job(tmp_path: Path) -> None:
    """旧会话迟到的 finally 不能破坏新会话已接管的在途作业。

    参数：``tmp_path`` 隔离会话与作业事实库。返回无。异常：A 被 B 接管后关闭
    仍把已派发 Job 转为 ``unknown``，或 B 真正关闭后未转为 ``unknown`` 时由
    断言失败；两次关闭均走事务化代际条件。
    """

    authority = _authority(tmp_path / "authority.db")
    payload = _dispatch_payload()
    try:
        first = authority.store.register_session(_registration_payload())
        authority.store.set_session_connected(first["session_uuid"], True)
        authority.dispatch(payload)
        command = authority.store.pending_commands()[0]
        authority.store.acknowledge_command(str(command["message_uuid"]))
        assert authority.store.job(payload["job_id"])["status"] == "dispatched"

        second = authority.store.register_session(_registration_payload())
        authority.store.set_session_connected(second["session_uuid"], True)

        assert authority.store.disconnect_session(first["session_uuid"]) == []
        assert authority.store.job(payload["job_id"])["status"] == "dispatched"
        latest = authority.store.latest_registration()
        assert latest is not None
        assert latest["connected"] is True

        assert authority.store.disconnect_session(second["session_uuid"]) == [
            payload["job_id"]
        ]
        assert authority.store.job(payload["job_id"])["status"] == "unknown"
    finally:
        authority.stop()


def test_unconnected_failed_session_cannot_disconnect_active_authority(
    tmp_path: Path,
) -> None:
    """不同 Edge 的失败 hello finally 不能触发全局离线后果。

    参数：``tmp_path`` 隔离已在线会话、失败注册与 Job 事实。返回无。异常：从未
    active 的会话关闭后使当前 Edge 离线，或把已派发 Job 转为 ``unknown`` 时由
    断言失败；该回归固定全局单 live-session 与 active-generation 条件。
    """

    authority = _authority(tmp_path / "authority.db")
    job = _dispatch_payload()
    try:
        active = authority.store.register_session(_registration_payload())
        authority.store.set_session_connected(active["session_uuid"], True)
        authority.dispatch(job)
        command = authority.store.pending_commands()[0]
        authority.store.acknowledge_command(str(command["message_uuid"]))
        assert authority.store.job(job["job_id"])["status"] == "dispatched"

        failed = authority.store.register_session(
            _registration_payload(edge_key="unrelated-edge")
        )

        assert authority.store.disconnect_session(failed["session_uuid"]) == []
        latest = authority.store.latest_registration()
        assert latest is not None
        assert latest["edge_key"] == "workspace-edge"
        assert latest["connected"] is True
        assert authority.store.job(job["job_id"])["status"] == "dispatched"
    finally:
        authority.stop()


def test_new_websocket_generation_rejects_old_commands_and_events(
    tmp_path: Path,
) -> None:
    """B 接管后 A 必须关闭且不能再收命令或提交事件。

    参数：``tmp_path`` 隔离双 WebSocket 共用的事实库。返回无。异常：A 未收到
    4409 换主关闭、B 未独占新命令、A 的迟到 ``job.started`` 生效、A 的 finally
    伤及 B/在途 Job，或 B 最终关闭未撤销 readiness 并转 unknown 时由断言失败。
    """

    authority = _authority(tmp_path / "authority.db")
    application = FastAPI()
    application.include_router(create_local_edge_control_router(authority))
    client = TestClient(application)
    headers = {"Authorization": "Bearer managed-local-secret"}
    process_uuid = str(uuid.uuid4())
    first_registration = authority.store.register_session(_registration_payload())
    second_registration = authority.store.register_session(_registration_payload())
    first_job = _dispatch_payload()
    authority.dispatch(first_job)
    try:
        with client.websocket_connect(
            "/api/v1/edge/ws",
            headers=headers,
        ) as first_socket:
            first_socket.send_json(
                _hello_message(first_registration, process_uuid=process_uuid)
            )
            first_command = first_socket.receive_json()
            first_socket.send_json(
                {
                    "protocol_version": 1,
                    "message_uuid": str(uuid.uuid4()),
                    "sequence": 0,
                    "type": "command.ack",
                    "sent_at": "2026-09-01T00:00:00.000000Z",
                    "payload": {
                        "command_uuid": first_command["message_uuid"],
                    },
                }
            )
            first_socket.receive_json()
            assert authority.store.job(first_job["job_id"])["status"] == "dispatched"

            with client.websocket_connect(
                "/api/v1/edge/ws",
                headers=headers,
            ) as second_socket:
                second_socket.send_json(
                    _hello_message(
                        second_registration,
                        process_uuid=process_uuid,
                        last_ack_sequence=int(first_command["sequence"]),
                    )
                )
                with pytest.raises(WebSocketDisconnect) as replaced:
                    first_socket.receive_json()
                assert replaced.value.code == 4409

                second_job = _dispatch_payload()
                authority.dispatch(second_job)
                second_command = second_socket.receive_json()
                assert second_command["payload"]["job_uuid"] == second_job["job_id"]

                stale_event = {
                    "protocol_version": 1,
                    "message_uuid": str(uuid.uuid4()),
                    "sequence": 0,
                    "type": "job.started",
                    "sent_at": "2026-09-01T00:00:00.000000Z",
                    "payload": {"job_uuid": second_job["job_id"]},
                }
                try:
                    first_socket.send_json(stale_event)
                except (RuntimeError, WebSocketDisconnect):
                    pass

                second_socket.send_json(
                    {
                        "protocol_version": 1,
                        "message_uuid": str(uuid.uuid4()),
                        "sequence": 0,
                        "type": "command.ack",
                        "sent_at": "2026-09-01T00:00:00.000000Z",
                        "payload": {
                            "command_uuid": second_command["message_uuid"],
                        },
                    }
                )
                second_socket.receive_json()
                assert (
                    authority.store.job(second_job["job_id"])["status"]
                    == "dispatched"
                )
                assert (
                    authority.store.job(first_job["job_id"])["status"]
                    == "dispatched"
                )
                assert client.get("/api/v1/edge/readiness").status_code == 200

            assert authority.store.job(second_job["job_id"])["status"] == "unknown"
            assert authority.store.job(first_job["job_id"])["status"] == "unknown"
            assert client.get("/api/v1/edge/readiness").status_code == 503
    finally:
        authority.stop()


def test_stalled_old_send_does_not_block_new_generation(tmp_path: Path) -> None:
    """旧连接发送背压时 B 仍须在有界时间内接管并获得命令。

    参数：``tmp_path`` 隔离半开连接共用的事实库。返回无。异常：A 的发送等待
    持有代际锁、B 未在 2 秒内收到命令、A 未被 4409 关闭，或 A 的 finally
    把既有在途 Job 转为 unknown 时由断言失败。
    """

    async def scenario() -> None:
        """驱动两个 handler 并验证取消旧发送后的代际状态。

        参数：无。返回无。异常：超时、协议或持久状态不满足接管合同时由断言
        失败；清理阶段取消最后一个 handler 并等待其事务化 finally。
        """

        authority = _authority(tmp_path / "authority.db")
        endpoint = _websocket_endpoint(authority)
        process_uuid = str(uuid.uuid4())
        first_job = _dispatch_payload()
        authority.dispatch(first_job)
        first_command = authority.store.pending_commands()[0]
        authority.store.acknowledge_command(str(first_command["message_uuid"]))
        blocked_job = _dispatch_payload()
        authority.dispatch(blocked_job)
        first_registration = authority.store.register_session(_registration_payload())
        second_registration = authority.store.register_session(
            _registration_payload()
        )
        first_socket = _StalledWebSocket(
            _hello_message(
                first_registration,
                process_uuid=process_uuid,
                last_ack_sequence=int(first_command["sequence"]),
            ),
            stall_send=True,
        )
        second_socket = _StalledWebSocket(
            _hello_message(
                second_registration,
                process_uuid=process_uuid,
                last_ack_sequence=int(first_command["sequence"]),
            ),
            stall_send=False,
        )
        first_task = asyncio.create_task(endpoint(first_socket))
        second_task: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(first_socket.send_started.wait(), timeout=2.0)
            second_task = asyncio.create_task(endpoint(second_socket))
            await asyncio.wait_for(second_socket.message_sent.wait(), timeout=2.0)
            await asyncio.wait_for(first_task, timeout=2.0)

            assert first_socket.close_code == 4409
            assert second_socket.sent_messages[0]["payload"]["job_uuid"] == blocked_job[
                "job_id"
            ]
            assert authority.store.job(first_job["job_id"])["status"] == "dispatched"
            latest = authority.store.latest_registration()
            assert latest is not None
            assert latest["connected"] is True
        finally:
            if not first_task.done():
                first_task.cancel()
            if second_task is not None and not second_task.done():
                second_task.cancel()
            await asyncio.gather(
                first_task,
                *(task for task in (second_task,) if task is not None),
                return_exceptions=True,
            )
            authority.stop()

    asyncio.run(scenario())


def test_failed_hello_cannot_mutate_or_disconnect_active_generation(tmp_path: Path) -> None:
    """伪造或复用 active Session 的失败 hello 必须完全无副作用。

    参数：``tmp_path`` 隔离 A 的在线代际及持久账本。返回无。异常：未知 Session、
    Edge 伪造、非法进程/运行 Job，或合法载荷复用 active UUID 后改变 READY、
    process meta、Command、Job、Session 任一事实，或结束 A handler 时由断言失败。
    """

    async def scenario() -> None:
        """保持 A 发送半开并依次注入六种失败的 B hello。

        参数：无。返回无。异常：任一失败 handler 未抛 ``ValueError``、污染完整
        SQLite 快照或取得内存 generation 时由断言失败；清理阶段只取消 A。
        """

        database = tmp_path / "authority.db"
        authority = _authority(database)
        endpoint = _websocket_endpoint(authority)
        process_uuid = str(uuid.uuid4())
        replacement_process_uuid = str(uuid.uuid4())
        active_registration = authority.store.register_session(
            _registration_payload()
        )
        dispatched_job = _dispatch_payload()
        authority.dispatch(dispatched_job)
        acknowledged_command = authority.store.pending_commands()[0]
        authority.store.acknowledge_command(
            str(acknowledged_command["message_uuid"])
        )
        pending_job = _dispatch_payload()
        authority.dispatch(pending_job)
        active_socket = _StalledWebSocket(
            _hello_message(
                active_registration,
                process_uuid=process_uuid,
                last_ack_sequence=int(acknowledged_command["sequence"]),
            ),
            stall_send=True,
        )
        active_task = asyncio.create_task(endpoint(active_socket))

        async def assert_rejected(
            message: dict[str, Any],
            expected_error: str,
            baseline: dict[str, list[tuple[Any, ...]]],
        ) -> None:
            """断言一个 B hello 失败且 A 的内存与完整 SQLite 事实不变。

            参数：``message`` 是待注入 hello；``expected_error`` 是稳定错误片段；
            ``baseline`` 是 A active 后快照。返回无。异常：handler 未按预期失败、
            A 被取消、READY 撤销或任何持久列变化时由断言失败。
            """

            failed_socket = _StalledWebSocket(message, stall_send=False)
            result = await asyncio.gather(endpoint(failed_socket), return_exceptions=True)
            assert len(result) == 1
            assert isinstance(result[0], ValueError)
            assert expected_error in str(result[0])
            assert not active_task.done()
            assert _durable_authority_facts(authority.store) == baseline
            ready, _ = project_local_edge_readiness(authority.store.latest_registration())
            assert ready is True
            assert authority.store.job(dispatched_job["job_id"])["status"] == "dispatched"
            assert authority.store.job(pending_job["job_id"])["status"] == "pending"

        try:
            await asyncio.wait_for(active_socket.send_started.wait(), timeout=2.0)
            rollback_registration = authority.store.register_session(_registration_payload())
            baseline = _durable_authority_facts(authority.store)
            ready, _ = project_local_edge_readiness(authority.store.latest_registration())
            assert ready is True

            partial_write = _hello_message(
                rollback_registration,
                process_uuid=process_uuid,
                last_ack_sequence=999,
            )
            partial_write["payload"]["running_jobs"] = [{
                "job_uuid": str(uuid.uuid4()),
                "command_uuid": str(uuid.uuid4()),
                "state": "running",
            }]
            await assert_rejected(partial_write, "running Edge job identity is unknown", baseline)

            unknown_session = _hello_message(
                {
                    "edge_uuid": str(uuid.uuid4()),
                    "session_uuid": str(uuid.uuid4()),
                },
                process_uuid=replacement_process_uuid,
                last_ack_sequence=999,
            )
            await assert_rejected(unknown_session, "unknown Edge session", baseline)

            forged_edge = _hello_message(
                {
                    **active_registration,
                    "edge_uuid": str(uuid.uuid4()),
                },
                process_uuid=replacement_process_uuid,
                last_ack_sequence=999,
            )
            await assert_rejected(
                forged_edge,
                "Edge session identity changed",
                baseline,
            )

            invalid_process = _hello_message(
                active_registration,
                process_uuid="invalid-process-uuid",
                last_ack_sequence=999,
            )
            await assert_rejected(invalid_process, "badly formed", baseline)

            invalid_running_jobs = _hello_message(
                active_registration,
                process_uuid=replacement_process_uuid,
                last_ack_sequence=999,
            )
            invalid_running_jobs["payload"]["running_jobs"] = {"invalid": True}
            await assert_rejected(
                invalid_running_jobs,
                "running_jobs must be a list of objects",
                baseline,
            )

            reused_active = _hello_message(
                active_registration,
                process_uuid=replacement_process_uuid,
                last_ack_sequence=999,
            )
            await assert_rejected(
                reused_active,
                "Edge session is already connected",
                baseline,
            )
        finally:
            if not active_task.done():
                active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)
            authority.stop()

    asyncio.run(scenario())
