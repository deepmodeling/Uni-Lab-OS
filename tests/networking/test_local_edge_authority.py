"""AIW-03 Local Backend adapter for the durable production Edge protocol."""

from __future__ import annotations

import multiprocessing
import socket
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.edge_control.client import EdgeControlClient, EdgeControlSettings
from unilabos.app.edge_control.local_authority import (
    LocalEdgeAuthorityStore,
    LocalEdgeControlAuthority,
    create_local_edge_control_router,
)
from unilabos.app.scheduler.dispatch import CancelDispatchState, DispatchPayload


def _payload(*, device_id: str = "robot-01") -> DispatchPayload:
    """构造已经由工站调度门禁签发的双进程派发载荷。"""

    return DispatchPayload(
        job_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        node_id=str(uuid.uuid4()),
        workflow_id=str(uuid.uuid4()),
        device_id=device_id,
        action="transfer",
        action_type="normal",
        action_args={"source": "A", "target": "B"},
        attempt=1,
        command_uuid=str(uuid.uuid4()),
        claim_uuid=str(uuid.uuid4()),
        fences=[
            {
                "lock_key": f"/devices/{device_id}",
                "fencing_token": 1,
            }
        ],
    )


def _authority(path: Path) -> LocalEdgeControlAuthority:
    return LocalEdgeControlAuthority(
        LocalEdgeAuthorityStore(path), api_key="managed-local-secret"
    )


def _serve_local_edge_authority(
    database_path: str,
    port: int,
    material_uuid: str,
    database_type: str,
    database_url: str,
) -> None:
    """在独立 Scheduler 进程提供真实 HTTP/WebSocket Edge 协议。"""

    import uvicorn

    from unilabos.config.config import BasicConfig

    # spawn 子进程不继承父进程内存配置，按生产启动流程显式传入。
    BasicConfig.database_type = database_type
    BasicConfig.database_url = database_url
    authority = _authority(Path(database_path))
    application = FastAPI()
    application.include_router(create_local_edge_control_router(authority))

    @application.get("/api/v1/materials")
    def materials() -> dict[str, object]:
        return {
            "code": 0,
            "data": {
                "items": [
                    {
                        "uuid": material_uuid,
                        "barcode": "ROBOT-01",
                    }
                ],
                "total": 1,
            },
        }

    uvicorn.run(
        application,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )


class _RuntimeResources:
    def dump(self) -> list[list[dict[str, str]]]:
        return [[{"id": "robot-01", "name": "Robot 01", "barcode": "ROBOT-01"}]]


class _RuntimeHostNode:
    """模拟独立 Edge Runtime 中已启动的 HostNode 设备边界。"""

    def __init__(self) -> None:
        self.resources_config = _RuntimeResources()
        self.devices_names = {"robot-01": "/devices/robot-01"}
        self._action_value_mappings = {
            "robot-01": {"transfer": {"type": "UniLabJsonCommand"}}
        }
        self.started: list[object] = []
        self.retired: list[str] = []

    def send_goal(
        self,
        item: object,
        action_type: str,
        action_kwargs: dict[str, object],
        sample_material: dict[str, str],
        server_info: object,
    ) -> None:
        assert action_type == "normal"
        assert action_kwargs == {"source": "A", "target": "B"}
        assert sample_material == {}
        assert server_info is None
        self.started.append(item)

    def device_dispatch_block_reason(self, _device_id: str) -> str:
        return ""

    def device_unknown_command_ids(self, _device_id: str) -> list[str]:
        return []

    def retire_settled_device_command(self, command_id: str) -> int:
        self.retired.append(command_id)
        return 1


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("等待双进程 Edge 协议状态超时")


def _attempt_identity(
    payload: DispatchPayload,
    command: dict[str, object],
) -> dict[str, object]:
    """从调度派发和持久命令构造 HTTP 事实所需的完整尝试身份。"""

    command_payload = command["payload"]
    assert isinstance(command_payload, dict)
    return {
        "job_uuid": payload["job_id"],
        "task_uuid": payload["task_id"],
        "node_uuid": payload["node_id"],
        "command_uuid": command["message_uuid"],
        "claim_uuid": command_payload["claim_uuid"],
        "attempt": command_payload["attempt"],
        "fences": command_payload["fences"],
    }


def test_latest_registration_returns_detached_edge_capabilities(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path / "authority.db")
    instance_uuid = str(uuid.uuid4())
    material_uuid = str(uuid.uuid4())
    try:
        registered = authority.store.register_session(
            {
                "edge_key": "workspace-edge",
                "instance_uuid": instance_uuid,
                "devices": [
                    {
                        "local_id": "robot-01",
                        "name": "Robot",
                        "material_uuid": material_uuid,
                        "actions": [{"name": "transfer", "type": "command"}],
                    }
                ],
            }
        )
        authority.store.set_session_connected(registered["session_uuid"], True)

        snapshot = authority.store.latest_registration()

        assert snapshot is not None
        assert snapshot["edge_uuid"] == registered["edge_uuid"]
        assert snapshot["instance_uuid"] == instance_uuid
        assert snapshot["connected"] is True
        assert snapshot["devices"] == [
            {
                "local_id": "robot-01",
                "name": "Robot",
                "material_uuid": material_uuid,
                "actions": [{"name": "transfer", "type": "command"}],
            }
        ]
        snapshot["devices"][0]["name"] = "mutated"
        assert authority.store.latest_registration()["devices"][0]["name"] == "Robot"  # type: ignore[index]
    finally:
        authority.stop()


def test_connected_runtime_can_refresh_live_device_dispatch_state(
    tmp_path: Path,
) -> None:
    """设备状态增量必须更新注册快照且不能改写设备身份与动作能力。"""

    authority = _authority(tmp_path / "authority.db")
    material_uuid = str(uuid.uuid4())
    try:
        registered = authority.store.register_session(
            {
                "edge_key": "workspace-edge",
                "instance_uuid": str(uuid.uuid4()),
                "devices": [
                    {
                        "local_id": "robot-01",
                        "name": "Robot",
                        "material_uuid": material_uuid,
                        "actions": [{"name": "transfer", "type": "command"}],
                    }
                ],
            }
        )
        authority.store.set_session_connected(registered["session_uuid"], True)

        updated = authority.store.update_device_status(
            registered["session_uuid"],
            "robot-01",
            {
                "online": True,
                "dispatch_block_reason": "driver_fault",
                "unknown_command_ids": ["command-2", "command-1"],
                "status": {"temperature": 37.5},
            },
        )

        assert updated["dispatch_block_reason"] == "driver_fault"
        snapshot = authority.store.latest_registration()
        assert snapshot is not None
        device = snapshot["devices"][0]
        assert device["material_uuid"] == material_uuid
        assert device["actions"] == [{"name": "transfer", "type": "command"}]
        assert device["unknown_command_ids"] == ["command-2", "command-1"]
        assert device["status"] == {"temperature": 37.5}
    finally:
        authority.stop()


def test_dispatch_is_idempotent_and_rejects_changed_identity(tmp_path: Path) -> None:
    authority = _authority(tmp_path / "authority.db")
    payload = _payload()
    try:
        authority.dispatch(payload)
        authority.dispatch(payload)
        assert len(authority.store.pending_commands()) == 1

        changed = DispatchPayload(payload)
        changed["device_id"] = "robot-02"
        with pytest.raises(ValueError, match="identity changed"):
            authority.dispatch(changed)
    finally:
        authority.stop()


def test_cancel_persists_job_cancel_and_reports_async_acceptance(tmp_path: Path) -> None:
    """本地控制面应受理取消命令，但不伪造设备已停止。"""

    authority = _authority(tmp_path / "authority.db")
    payload = _payload()
    accepted: list[bool] = []
    try:
        authority.dispatch(payload)

        state = authority.cancel(payload["job_id"], accepted.append)

        assert state is CancelDispatchState.REQUESTED
        assert accepted == [True]
        command = next(
            item
            for item in authority.store.pending_commands()
            if item["type"] == "job.cancel"
        )
        assert command["payload"] == {"job_uuid": payload["job_id"]}
        # 取消请求的本地受理不能改变设备动作的在途事实。
        assert authority.store.job(payload["job_id"])["status"] == "pending"

        # 重复点击取消复用同一未投递命令，不产生第二个物理请求。
        assert authority.cancel(payload["job_id"], accepted.append) is CancelDispatchState.REQUESTED
        assert len(
            [item for item in authority.store.pending_commands() if item["type"] == "job.cancel"]
        ) == 1
    finally:
        authority.stop()


def test_error_decision_report_is_forwarded_back_to_edge(tmp_path: Path) -> None:
    """Edge 的错误报告必须进入工作流端口，并可作为下行命令回到同一设备。"""

    authority = _authority(tmp_path / "authority.db")
    payload = _payload()
    received: list[dict[str, object]] = []
    try:
        authority.dispatch(payload)
        authority.add_error_decision_required_listener(received.append)
        report = {
            "decision_id": str(uuid.uuid4()),
            "task_id": payload["task_id"],
            "job_id": payload["job_id"],
            "device_id": payload["device_id"],
            "action_name": payload["action"],
            "options": [{"id": "retry", "action": "retry", "label": "重试"}],
        }

        assert authority.publish_job_error_decision_required(report) is True
        assert received == [report]
        assert authority.resolve_error_decision(report["decision_id"], {"action": "retry"}) is True

        command = authority.store.pending_commands()[-1]
        assert command["type"] == "job.error_decision"
        assert command["payload"] == {
            "decision_id": report["decision_id"],
            "job_id": payload["job_id"],
            "device_id": payload["device_id"],
            "action": "retry",
        }
    finally:
        authority.stop()


def test_error_decision_can_use_persisted_delivery_identity(tmp_path: Path) -> None:
    """重启后由 intervention 元数据恢复 job/device 路由，不依赖内存报告。"""

    authority = _authority(tmp_path / "authority.db")
    payload = _payload()
    decision_id = str(uuid.uuid4())
    try:
        authority.dispatch(payload)
        assert authority.resolve_error_decision(
            decision_id,
            {
                "action": "retry",
                "job_id": payload["job_id"],
                "device_id": payload["device_id"],
            },
        ) is True
        assert authority.store.pending_commands()[-1]["payload"]["decision_id"] == decision_id
    finally:
        authority.stop()


def test_http_websocket_round_trip_projects_one_terminal_outcome(
    tmp_path: Path,
) -> None:
    """验证 Local Edge 公开协议可完成命令、ACK 与结果的单次闭环。

    参数：``tmp_path`` 隔离本地 Edge 事实库。返回无。异常：公开 HTTP/WebSocket
    合同、结果幂等或投影次数不符合预期时由断言失败；重复结果不得重复通知监听器。
    """

    authority = _authority(tmp_path / "authority.db")
    finished: list[tuple[str, bool, object, str]] = []
    authority.add_job_finished_listener(
        lambda job_id, success, result, suc_type: finished.append(
            (job_id, success, result, suc_type)
        )
    )
    application = FastAPI()
    application.include_router(create_local_edge_control_router(authority))
    client = TestClient(application)
    authorization = {"Authorization": "Bearer managed-local-secret"}
    registration = client.post(
        "/api/v1/edge/sessions",
        headers=authorization,
        json={
            "edge_key": "workspace-edge",
            "instance_uuid": str(uuid.uuid4()),
            "capability_revision": "unilabos-edge-v1",
            "devices": [],
        },
    ).json()["data"]
    payload = _payload()
    authority.dispatch(payload)

    try:
        with client.websocket_connect(
            "/api/v1/edge/ws", headers=authorization
        ) as websocket:
            websocket.send_json(
                {
                    "protocol_version": 1,
                    "message_uuid": str(uuid.uuid4()),
                    "sequence": 0,
                    "type": "hello",
                    "sent_at": "2026-08-13T00:00:00.000000Z",
                    "payload": {
                        "edge_uuid": registration["edge_uuid"],
                        "session_uuid": registration["session_uuid"],
                        "process_uuid": str(uuid.uuid4()),
                        "last_ack_command_sequence": 0,
                        "running_jobs": [],
                    },
                }
            )
            command = websocket.receive_json()
            assert command["type"] == "job.start"
            command_payload = command["payload"]
            assert command_payload["job_uuid"] == payload["job_id"]
            event_uuid = str(uuid.uuid4())
            websocket.send_json(
                {
                    "protocol_version": 1,
                    "message_uuid": event_uuid,
                    "sequence": 0,
                    "type": "command.ack",
                    "sent_at": "2026-08-13T00:00:00.000000Z",
                    "payload": {"command_uuid": command["message_uuid"]},
                }
            )
            assert websocket.receive_json()["payload"] == {
                "event_uuid": event_uuid
            }

        headers = {
            "Idempotency-Key": "round-trip-outcome",
            "X-Command-UUID": command["message_uuid"],
            "X-Job-Token": command_payload["job_access_token"],
        }
        job = client.get(
            f"/api/v1/edge/jobs/{payload['job_id']}",
            params={"task_uuid": payload["task_id"], "node_uuid": payload["node_id"]},
            headers=headers,
        )
        assert job.status_code == 200
        assert job.json()["data"]["param"] == payload["action_args"]

        outcome = {
            **_attempt_identity(payload, command),
            "outcome": "succeeded",
            "return_info": {"return_value": {"moved": True}},
            "error_info": [],
            "unknown_command_ids": [],
        }
        first = client.put(
            f"/api/v1/edge/jobs/{payload['job_id']}/outcome",
            headers=headers,
            json=outcome,
        )
        second = client.put(
            f"/api/v1/edge/jobs/{payload['job_id']}/outcome",
            headers=headers,
            json=outcome,
        )
        assert first.status_code == 201
        assert second.status_code == 200
        first_result = first.json()["data"]
        assert second.json()["data"] == first_result
        assert first_result["workflow_node_job_uuid"] == payload["job_id"]
        assert first_result["edge_command_uuid"] == command["message_uuid"]
        assert first_result["idempotency_key"] == "round-trip-outcome"
        assert first_result["outcome"] == "succeeded"
        assert first_result["return_info"] == outcome["return_info"]
        assert first_result["error_info"] == []
        assert first_result["uuid"]
        assert first_result["committed_at"].endswith("Z")
        assert finished == [
            (payload["job_id"], True, {"moved": True}, "normal")
        ]
    finally:
        authority.stop()


def test_scheduler_and_runtime_processes_complete_one_durable_job(
    tmp_path: Path,
) -> None:
    """真实双进程必须通过 HTTP/WS 保真传递 Claim、Fence、参数和终态。"""

    database_path = tmp_path / "dual-process-authority.db"
    runtime_path = tmp_path / "dual-process-runtime.db"
    payload = _payload()
    material_uuid = str(uuid.uuid4())
    seeded = _authority(database_path)
    seeded.dispatch(payload)
    seeded.stop()

    port = _free_loopback_port()
    from unilabos.config.config import BasicConfig

    context = multiprocessing.get_context("spawn")
    scheduler_process = context.Process(
        target=_serve_local_edge_authority,
        args=(str(database_path), port, material_uuid, BasicConfig.database_type, BasicConfig.database_url),
    )
    scheduler_process.start()
    client: EdgeControlClient | None = None
    try:
        def server_accepts_connections() -> bool:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return True
            except OSError:
                return False

        _wait_until(server_accepts_connections)
        host_node = _RuntimeHostNode()
        address = f"http://127.0.0.1:{port}"
        client = EdgeControlClient(
            EdgeControlSettings(
                scheduler_address=address,
                backend_address=address,
                api_key="managed-local-secret",
                edge_key="workspace-edge",
                capability_revision="dual-process-v1",
                instance_uuid="",
                state_db=str(runtime_path),
                reconnect_interval=0.05,
                request_timeout=2,
                event_retry_interval=0.05,
            ),
            host_node_provider=lambda: host_node,
        )
        client.start()
        client.publish_host_ready()
        _wait_until(client.is_connected)
        _wait_until(lambda: len(host_node.started) == 1)

        item = host_node.started[0]
        assert item.job_id == payload["job_id"]  # type: ignore[attr-defined]
        assert item.claim_uuid == payload["claim_uuid"]  # type: ignore[attr-defined]
        assert item.fences == ((f"/devices/{payload['device_id']}", 1),)  # type: ignore[attr-defined]
        client.publish_job_started(item)
        client.publish_job_status(
            {},
            item,
            "success",
            {"suc": True, "return_value": {"moved": True}},
        )

        from unilabos.persistence import open_database

        # 使用当前选择的数据库读回真实子进程提交结果，避免误读空 SQLite 文件。
        observer = open_database(str(database_path), "edge_authority")
        try:
            def outcome_is_committed() -> bool:
                row = observer.execute(
                    "SELECT outcome_json FROM local_edge_job WHERE job_uuid=?",
                    (payload["job_id"],),
                ).fetchone()
                return row is not None and row[0] is not None

            _wait_until(outcome_is_committed)
            encoded = observer.execute(
                "SELECT outcome_json FROM local_edge_job WHERE job_uuid=?",
                (payload["job_id"],),
            ).fetchone()[0]
        finally:
            observer.close()
        import json

        outcome = json.loads(encoded)
        assert outcome["outcome"] == "succeeded"
        assert outcome["return_info"] == {
            "suc": True,
            "return_value": {"moved": True},
        }
        assert outcome["unknown_command_ids"] == []
    finally:
        if client is not None:
            client.stop()
            client.store.close()
        scheduler_process.terminate()
        scheduler_process.join(timeout=10)
        if scheduler_process.is_alive():
            scheduler_process.kill()
            scheduler_process.join(timeout=5)

    assert scheduler_process.exitcode is not None


def test_unknown_outcome_locks_device_until_explicit_reconciliation(
    tmp_path: Path,
) -> None:
    """验证结果不明会锁住设备，且只能由明确取消证据完成物理结算。

    参数：``tmp_path`` 隔离本地 Edge 事实库。返回无。异常：UNKNOWN 未锁设备、
    被盲目重放或取消证据未幂等释放设备时由断言失败。
    """

    authority = _authority(tmp_path / "authority.db")
    finished: list[tuple[str, bool, object, str]] = []
    authority.add_job_finished_listener(
        lambda job_id, success, result, suc_type: finished.append(
            (job_id, success, result, suc_type)
        )
    )
    payload = _payload()
    authority.dispatch(payload)
    command = authority.store.pending_commands()[0]
    job_token = command["payload"]["job_access_token"]
    unknown_id = f"workflow-node-job:{payload['job_id']}"
    try:
        result = authority.commit_outcome(
            payload["job_id"],
            command_uuid=command["message_uuid"],
            job_token=job_token,
            idempotency_key="unknown-outcome",
            payload={
                **_attempt_identity(payload, command),
                "outcome": "failed",
                "return_info": {},
                "error_info": [{"message": "Edge disconnected"}],
                "unknown_command_ids": [unknown_id],
            },
        )
        assert result["status"] == "unknown"
        assert finished == []
        with pytest.raises(RuntimeError, match="locked by unresolved UNKNOWN"):
            authority.dispatch(_payload())

        resolution = authority.store.create_unknown_resolution(
            payload["job_id"], reason="operator confirmed safe state"
        )
        commands = authority.store.pending_commands()
        assert commands[-1]["message_uuid"] == resolution["command_uuid"]
        assert commands[-1]["type"] == "job.resolve_unknown"
        authority.resolve_unknown_committed(payload["job_id"])
        assert finished == [(payload["job_id"], False, None, "canceled")]
        assert authority.busy_device_action_keys() == set()
    finally:
        authority.stop()


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("return_info", []),
        ("error_info", {}),
        ("unknown_command_ids", {}),
        ("inventory_consumptions", {}),
    ],
)
def test_http_outcome_rejects_wrong_empty_json_types(
    tmp_path: Path,
    field: str,
    invalid_value: object,
) -> None:
    """错误的空 JSON 类型不能被默认值逻辑静默改写后提交。

    参数：``tmp_path`` 隔离事实库；``field`` 与 ``invalid_value`` 描述一个违反
    Backend DTO 的字段。返回无。异常：接口未拒绝输入或错误输入产生不可变结果
    时由断言失败；失败请求不得占用该 Job 的首次幂等提交位置。
    """

    authority = _authority(tmp_path / "authority.db")
    payload = _payload()
    authority.dispatch(payload)
    command = authority.store.pending_commands()[0]
    application = FastAPI()
    application.include_router(create_local_edge_control_router(authority))
    client = TestClient(application)
    outcome: dict[str, object] = {
        **_attempt_identity(payload, command),
        "outcome": "succeeded",
        "return_info": {},
        "error_info": [],
        "unknown_command_ids": [],
        "inventory_consumptions": [],
    }
    outcome[field] = invalid_value
    try:
        response = client.put(
            f"/api/v1/edge/jobs/{payload['job_id']}/outcome",
            headers={
                "Idempotency-Key": f"wrong-type-{field}",
                "X-Command-UUID": command["message_uuid"],
                "X-Job-Token": command["payload"]["job_access_token"],
            },
            json=outcome,
        )
        assert response.status_code == 400
        assert authority.store.job(payload["job_id"])["status"] == "pending"
    finally:
        authority.stop()


@pytest.mark.parametrize(
    "unknown_command_id",
    [
        "not-a-workflow-command",
        "workflow-node-job:11111111-1111-4111-8111-111111111111",
    ],
)
def test_http_outcome_rejects_unknown_command_from_another_job(
    tmp_path: Path,
    unknown_command_id: str,
) -> None:
    """UNKNOWN 证据必须采用规范身份并至少包含当前 Job 的设备命令。

    参数：``tmp_path`` 隔离事实库；``unknown_command_id`` 是格式错误或属于其他
    Job 的命令身份。返回无。异常：非法证据被接受并锁住设备时由断言失败；失败
    请求不得写入作业终态。
    """

    authority = _authority(tmp_path / "authority.db")
    payload = _payload()
    authority.dispatch(payload)
    command = authority.store.pending_commands()[0]
    application = FastAPI()
    application.include_router(create_local_edge_control_router(authority))
    client = TestClient(application)
    try:
        response = client.put(
            f"/api/v1/edge/jobs/{payload['job_id']}/outcome",
            headers={
                "Idempotency-Key": "invalid-unknown-evidence",
                "X-Command-UUID": command["message_uuid"],
                "X-Job-Token": command["payload"]["job_access_token"],
            },
            json={
                **_attempt_identity(payload, command),
                "outcome": "failed",
                "return_info": {},
                "error_info": [{"message": "连接中断"}],
                "unknown_command_ids": [unknown_command_id],
                "inventory_consumptions": [],
            },
        )
        assert response.status_code == 400
        assert authority.store.job(payload["job_id"])["status"] == "pending"
    finally:
        authority.stop()


def test_feedback_projection_failure_is_replayed_after_restart(tmp_path: Path) -> None:
    """Edge 已提交的反馈不得因工作流投影失败而丢失。"""

    database = tmp_path / "authority.db"
    authority = _authority(database)
    payload = _payload()
    authority.dispatch(payload)
    command = authority.store.pending_commands()[0]
    sample = {
        **_attempt_identity(payload, command),
        "sequence": 1,
        "feedback_type": "progress",
        "data": {"percent": 25},
        "observed_at": "2026-08-26T10:00:00Z",
        "idempotency_key": "feedback-1",
    }

    authority.add_job_feedback_listener(
        lambda _job_uuid, _sample: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    try:
        with pytest.raises(RuntimeError, match="db down"):
            authority.commit_feedback(
                payload["job_id"],
                command_uuid=command["message_uuid"],
                job_token=command["payload"]["job_access_token"],
                payload=sample,
            )
    finally:
        authority.stop()

    recovered = _authority(database)
    projected: list[tuple[str, dict[str, object]]] = []
    try:
        recovered.replay_pending_projections(
            feedback_listener=lambda job_uuid, item: projected.append(
                (job_uuid, item)
            )
        )
        assert projected == [(payload["job_id"], sample)]

        recovered.replay_pending_projections(
            feedback_listener=lambda job_uuid, item: projected.append(
                (job_uuid, item)
            )
        )
        assert projected == [(payload["job_id"], sample)]
    finally:
        recovered.stop()


def test_http_facts_reject_changed_job_attempt_identity(tmp_path: Path) -> None:
    """反馈和结果均必须拒绝与已持久 Claim/Fence 尝试不一致的身份。"""

    authority = _authority(tmp_path / "authority.db")
    payload = _payload()
    authority.dispatch(payload)
    command = authority.store.pending_commands()[0]
    identity = _attempt_identity(payload, command)
    changed_feedback = {
        **identity,
        "claim_uuid": str(uuid.uuid4()),
        "sequence": 1,
        "feedback_type": "progress",
        "data": {"percent": 50},
        "observed_at": "2026-08-30T00:00:00Z",
        "idempotency_key": "feedback-changed-claim",
    }
    changed_outcome = {
        **identity,
        "attempt": int(identity["attempt"]) + 1,
        "outcome": "succeeded",
        "return_info": {},
        "error_info": [],
        "unknown_command_ids": [],
    }
    try:
        with pytest.raises(ValueError, match="claim_uuid does not match"):
            authority.commit_feedback(
                payload["job_id"],
                command_uuid=command["message_uuid"],
                job_token=command["payload"]["job_access_token"],
                payload=changed_feedback,
            )
        with pytest.raises(ValueError, match="attempt does not match"):
            authority.commit_outcome(
                payload["job_id"],
                command_uuid=command["message_uuid"],
                job_token=command["payload"]["job_access_token"],
                idempotency_key="outcome-changed-attempt",
                payload=changed_outcome,
            )
        assert authority.store.job(payload["job_id"])["status"] == "pending"
    finally:
        authority.stop()


def test_outcome_projection_failure_is_replayed_after_restart(tmp_path: Path) -> None:
    """Edge 不可变结果在工作流库恢复后必须可重放。"""

    database = tmp_path / "authority.db"
    authority = _authority(database)
    payload = _payload()
    authority.dispatch(payload)
    command = authority.store.pending_commands()[0]
    authority.add_job_finished_listener(
        lambda *_args: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    try:
        with pytest.raises(RuntimeError, match="db down"):
            authority.commit_outcome(
                payload["job_id"],
                command_uuid=command["message_uuid"],
                job_token=command["payload"]["job_access_token"],
                idempotency_key="projection-failure",
                payload={
                    **_attempt_identity(payload, command),
                    "outcome": "succeeded",
                    "return_info": {"return_value": {"moved": True}},
                    "error_info": [],
                    "unknown_command_ids": [],
                },
            )
    finally:
        authority.stop()

    recovered = _authority(database)
    projected: list[tuple[str, bool, object, str]] = []
    try:
        recovered.replay_pending_projections(
            finished_listener=lambda job_uuid, success, result, suc_type: projected.append(
                (job_uuid, success, result, suc_type)
            )
        )
        assert projected == [
            (payload["job_id"], True, {"moved": True}, "normal")
        ]

        recovered.replay_pending_projections(
            finished_listener=lambda job_uuid, success, result, suc_type: projected.append(
                (job_uuid, success, result, suc_type)
            )
        )
        assert len(projected) == 1
    finally:
        recovered.stop()


@pytest.mark.parametrize("terminal_outcome", ["failed", "canceled", "timeout"])
def test_http_outcome_replay_preserves_exact_terminal_semantics(
    tmp_path: Path,
    terminal_outcome: str,
) -> None:
    """HTTP 提交的失败类结果必须按原终态和错误证据进行投递重放。

    参数：``tmp_path`` 隔离边缘控制存储；``terminal_outcome`` 是 Backend
    合同允许的失败、取消或超时终态。返回无；先在没有工作流监听器的启动窗口
    提交结果，再注册持久结果监听器并重放，证明结果不会被提前确认，也不会把
    不同终态压缩成普通失败。
    """

    database = tmp_path / "authority.db"
    authority = _authority(database)
    payload = _payload()
    authority.dispatch(payload)
    command = authority.store.pending_commands()[0]
    application = FastAPI()
    application.include_router(create_local_edge_control_router(authority))
    client = TestClient(application)
    headers = {
        "Authorization": "Bearer managed-local-secret",
        "Idempotency-Key": f"outcome-{terminal_outcome}",
        "X-Command-UUID": command["message_uuid"],
        "X-Job-Token": command["payload"]["job_access_token"],
    }
    error_info = [{"code": f"device_{terminal_outcome}", "message": "设备终态"}]
    try:
        response = client.put(
            f"/api/v1/edge/jobs/{payload['job_id']}/outcome",
            headers=headers,
            json={
                **_attempt_identity(payload, command),
                "outcome": terminal_outcome,
                "return_info": {"last_step": 3},
                "error_info": error_info,
                "unknown_command_ids": [],
            },
        )
        assert response.status_code == 201
    finally:
        authority.stop()

    recovered = _authority(database)
    projected: list[tuple[str, dict[str, object]]] = []
    try:
        recovered.replay_pending_projections(
            outcome_listener=lambda job_uuid, outcome: projected.append(
                (job_uuid, outcome.as_dict())
            )
        )
        assert projected == [
            (
                payload["job_id"],
                {
                    "outcome": terminal_outcome,
                    "return_info": {"last_step": 3},
                    "error_info": error_info,
                    "unknown_command_ids": [],
                    "inventory_consumptions": [],
                    "material_aliquot_receipts": [],
                },
            )
        ]

        recovered.replay_pending_projections(
            outcome_listener=lambda job_uuid, outcome: projected.append(
                (job_uuid, outcome.as_dict())
            )
        )
        assert len(projected) == 1
    finally:
        recovered.stop()


def test_transient_disconnect_marks_unknown_and_same_process_can_reconcile(
    tmp_path: Path,
) -> None:
    """短暂断线先持久化不确定事实，同一动作进程重连后恢复运行态。

    参数：``tmp_path`` 是隔离数据库目录。返回无。异常：回调遗漏、重复或早于
    本地账本提交时断言失败；网络抖动误报进程重启时测试失败。
    """

    authority = _authority(tmp_path / "authority.db")
    registration = authority.store.register_session(
        {
            "edge_key": "workspace-edge",
            "instance_uuid": str(uuid.uuid4()),
            "devices": [],
        }
    )
    session_identity = {
        "edge_uuid": registration["edge_uuid"],
        "session_uuid": registration["session_uuid"],
    }
    payload = _payload()
    authority.dispatch(payload)
    command = authority.store.pending_commands()[0]
    authority.store.acknowledge_command(command["message_uuid"])
    process_uuid = str(uuid.uuid4())
    restarted_notifications: list[tuple[str, ...]] = []
    authority.add_execution_process_restarted_listener(
        lambda job_uuids: restarted_notifications.append(job_uuids)
    )
    try:
        restarted, affected = authority.store.reconcile_hello(
            {
                **session_identity,
                "process_uuid": process_uuid,
                "last_ack_command_sequence": command["sequence"],
                "running_jobs": [],
            }
        )
        assert (restarted, affected) == (False, ())
        affected = authority.store.mark_disconnected_jobs_unknown()
        assert affected == [payload["job_id"]]
        assert restarted_notifications == []
        assert authority.store.job(payload["job_id"])["status"] == "unknown"
        assert authority.busy_device_action_keys() == {
            f"/devices/{payload['device_id']}/{payload['action']}"
        }

        restarted, affected = authority.store.reconcile_hello(
            {
                **session_identity,
                "process_uuid": process_uuid,
                "last_ack_command_sequence": command["sequence"],
                "running_jobs": [
                    {
                        "job_uuid": payload["job_id"],
                        "command_uuid": command["message_uuid"],
                        "state": "running",
                    }
                ],
            }
        )
        assert (restarted, affected) == (False, ())
        assert authority.store.job(payload["job_id"])["status"] == "running"
    finally:
        authority.stop()


def test_hello_preserves_cancel_requested_runtime_state(
    tmp_path: Path,
) -> None:
    """Edge hello 对账不得把已受理取消的作业降级成普通 running。"""

    authority = _authority(tmp_path / "authority-cancel.db")
    registration = authority.store.register_session(
        {
            "edge_key": "workspace-edge",
            "instance_uuid": str(uuid.uuid4()),
            "devices": [],
        }
    )
    session_identity = {
        "edge_uuid": registration["edge_uuid"],
        "session_uuid": registration["session_uuid"],
    }
    payload = _payload()
    authority.dispatch(payload)
    command = authority.store.pending_commands()[0]
    authority.store.acknowledge_command(command["message_uuid"])
    process_uuid = str(uuid.uuid4())
    try:
        assert authority.store.reconcile_hello(
            {
                **session_identity,
                "process_uuid": process_uuid,
                "last_ack_command_sequence": command["sequence"],
                "running_jobs": [],
            }
        ) == (False, ())

        restarted, affected = authority.store.reconcile_hello(
            {
                **session_identity,
                "process_uuid": process_uuid,
                "last_ack_command_sequence": command["sequence"],
                "running_jobs": [
                    {
                        "job_uuid": payload["job_id"],
                        "command_uuid": command["message_uuid"],
                        "state": "cancel_requested",
                    }
                ],
            }
        )
        assert (restarted, affected) == (False, ())
        assert authority.store.job(payload["job_id"])["status"] == (
            "cancel_requested"
        )
        assert authority.store.busy_device_action_keys() == {
            f"/devices/{payload['device_id']}/{payload['action']}"
        }

        assert authority.store.mark_disconnected_jobs_unknown() == [
            payload["job_id"]
        ]
        assert authority.store.job(payload["job_id"])["status"] == "unknown"
    finally:
        authority.stop()


def test_changed_process_identity_reports_restart_and_keeps_job_unknown(
    tmp_path: Path,
) -> None:
    """动作进程身份变化才通知重启，并保留在途作业不确定占用。

    参数：``tmp_path`` 是隔离数据库目录。返回无。异常：身份变化未返回受影响
    作业、错误恢复运行态或通知遗漏时断言失败。
    """

    authority = _authority(tmp_path / "authority.db")
    registration = authority.store.register_session(
        {
            "edge_key": "workspace-edge",
            "instance_uuid": str(uuid.uuid4()),
            "devices": [],
        }
    )
    session_identity = {
        "edge_uuid": registration["edge_uuid"],
        "session_uuid": registration["session_uuid"],
    }
    payload = _payload()
    authority.dispatch(payload)
    command = authority.store.pending_commands()[0]
    authority.store.acknowledge_command(command["message_uuid"])
    first_process_uuid = str(uuid.uuid4())
    authority.store.reconcile_hello(
        {
            **session_identity,
            "process_uuid": first_process_uuid,
            "last_ack_command_sequence": command["sequence"],
            "running_jobs": [],
        }
    )
    authority.store.mark_disconnected_jobs_unknown()
    notifications: list[tuple[str, ...]] = []
    authority.add_execution_process_restarted_listener(notifications.append)
    try:
        restarted, affected = authority.store.reconcile_hello(
            {
                **session_identity,
                "process_uuid": str(uuid.uuid4()),
                "last_ack_command_sequence": command["sequence"],
                "running_jobs": [],
            }
        )
        authority.notify_execution_process_restarted(affected)

        assert restarted is True
        assert affected == (payload["job_id"],)
        assert notifications == [(payload["job_id"],)]
        assert authority.store.job(payload["job_id"])["status"] == "unknown"
    finally:
        authority.stop()


def test_restart_notification_with_empty_edge_ledger_reaches_scheduler(
    tmp_path: Path,
) -> None:
    """进程身份变化本身就是崩溃证据；空 Edge 账本也必须唤醒任务恢复。"""

    authority = _authority(tmp_path / "authority.db")
    notifications: list[tuple[str, ...]] = []
    authority.add_execution_process_restarted_listener(notifications.append)
    try:
        authority.notify_execution_process_restarted(())
    finally:
        authority.stop()

    assert notifications == [()]


def test_fail_restarted_jobs_clears_unknown_busy_key(tmp_path: Path) -> None:
    """工作流已失败后，Edge 账本须把 unknown 作业收成 failed 并释放忙碌键。"""

    authority = _authority(tmp_path / "authority.db")
    payload = _payload()
    try:
        authority.dispatch(payload)
        command = authority.store.pending_commands()[0]
        authority.store.acknowledge_command(command["message_uuid"])
        authority.store.mark_disconnected_jobs_unknown()
        assert authority.store.job(payload["job_id"])["status"] == "unknown"
        assert authority.busy_device_action_keys() == {
            f"/devices/{payload['device_id']}/{payload['action']}"
        }

        failed = authority.fail_restarted_jobs((payload["job_id"],))

        assert failed == [payload["job_id"]]
        assert authority.store.job(payload["job_id"])["status"] == "failed"
        assert authority.busy_device_action_keys() == set()
        assert authority.fail_restarted_jobs((payload["job_id"],)) == []
    finally:
        authority.stop()


def test_fail_restarted_jobs_also_closes_cancel_requested_job(
    tmp_path: Path,
) -> None:
    """重启失败收口不能遗漏已受理取消但仍未结算的作业。"""

    authority = _authority(tmp_path / "authority-cancel-restart.db")
    payload = _payload()
    try:
        authority.dispatch(payload)
        with authority.store._lock, authority.store._connection:
            authority.store._connection.execute(
                "UPDATE local_edge_job SET status='cancel_requested' WHERE job_uuid=?",
                (payload["job_id"],),
            )
        assert authority.store.busy_device_action_keys() == {
            f"/devices/{payload['device_id']}/{payload['action']}"
        }

        assert authority.fail_restarted_jobs((payload["job_id"],)) == [
            payload["job_id"]
        ]
        assert authority.store.job(payload["job_id"])["status"] == "failed"
        assert authority.store.busy_device_action_keys() == set()
    finally:
        authority.stop()


def test_fail_restarted_jobs_retires_pending_error_decision(tmp_path: Path) -> None:
    """强制收口作业时，人工决定命令也必须退役。

    ``tmp_path`` 提供隔离 Authority SQLite 路径；返回为空。``job.error_decision``
    的载荷用 ``job_id`` 承载作业身份，而其他下行命令用 ``job_uuid``；只按后者
    清扫会让这条命令永远留在 pending，Edge 每次重连都要再收一遍一条已经没有
    物理意义的命令。
    """

    authority = _authority(tmp_path / "authority.db")
    payload = _payload()
    try:
        authority.dispatch(payload)
        start_command = authority.store.pending_commands()[0]
        authority.store.acknowledge_command(start_command["message_uuid"])

        decision_id = str(uuid.uuid4())
        assert authority.publish_job_error_decision_required(
            {
                "decision_id": decision_id,
                "task_id": payload["task_id"],
                "job_id": payload["job_id"],
                "device_id": payload["device_id"],
                "action_name": payload["action"],
                "options": [{"id": "retry", "action": "retry", "label": "重试"}],
            }
        )
        assert authority.resolve_error_decision(decision_id, {"action": "retry"}) is True
        assert [item["type"] for item in authority.store.pending_commands()] == [
            "job.error_decision"
        ]

        authority.fail_restarted_jobs((payload["job_id"],))

        assert authority.store.pending_commands() == []
    finally:
        authority.stop()


def test_individual_ack_does_not_ack_paused_earlier_command(tmp_path: Path) -> None:
    """手动命令越过普通等待命令后，重连不能用最大序号吞掉未送达命令。"""
    authority = _authority(tmp_path / "authority.db")
    try:
        registration = authority.store.register_session({"edge_key": "local", "instance_uuid": str(uuid.uuid4()), "devices": []})
        first = _payload(device_id="first")
        second = _payload(device_id="second")
        authority.dispatch(first)
        authority.dispatch(second)
        commands = authority.store.pending_commands()
        authority.store.acknowledge_command(commands[1]["message_uuid"])
        authority.store.reconcile_hello({
            "edge_uuid": registration["edge_uuid"], "session_uuid": registration["session_uuid"],
            "process_uuid": str(uuid.uuid4()), "last_ack_command_sequence": commands[1]["sequence"],
            "individual_command_ack": True, "running_jobs": [],
        })
        assert [item["message_uuid"] for item in authority.store.pending_commands()] == [commands[0]["message_uuid"]]
    finally:
        authority.store.close()


def test_restart_terminal_job_does_not_resend_unacked_start(tmp_path: Path) -> None:
    """失败收口作废未 ACK 的旧 job.start，不需要伪造执行端确认。"""
    authority = _authority(tmp_path / "authority.db")
    try:
        payload = _payload()
        authority.dispatch(payload)
        assert len(authority.store.pending_commands()) == 1
        authority.fail_restarted_jobs((payload["job_id"],))
        assert authority.store.pending_commands() == []
        assert authority.busy_device_action_keys() == set()
    finally:
        authority.stop()


def test_restart_notification_retries_after_projection_failure(tmp_path: Path) -> None:
    """新进程 HELLO 已落盘但任务终结失败时，同进程重连仍重试重启事件。"""
    authority = _authority(tmp_path / "restart-retry.db")
    registration = authority.store.register_session(
        {
            "edge_key": "workspace-edge",
            "instance_uuid": str(uuid.uuid4()),
            "devices": [],
        }
    )
    hello = {
        "edge_uuid": registration["edge_uuid"],
        "session_uuid": registration["session_uuid"],
        "process_uuid": str(uuid.uuid4()),
        "last_ack_command_sequence": 0,
        "running_jobs": [],
    }
    calls = []

    def fail_once(job_uuids):
        calls.append(job_uuids)
        if len(calls) == 1:
            raise RuntimeError("模拟失败投影中断")

    try:
        assert authority.store.reconcile_hello(hello) == (False, ())
        hello["process_uuid"] = str(uuid.uuid4())
        authority.add_execution_process_restarted_listener(fail_once)
        assert authority.store.reconcile_hello(hello) == (True, ())
        with pytest.raises(RuntimeError, match="模拟失败投影中断"):
            authority.notify_execution_process_restarted(())
        assert authority.store.reconcile_hello(hello) == (True, ())
        authority.notify_execution_process_restarted(())
        assert calls == [(), ()]
        assert authority.store.reconcile_hello(hello) == (False, ())
    finally:
        authority.stop()


@pytest.mark.parametrize('reported', ['current', 'other', 'offline'])
def test_failed_job_can_resolve_only_live_device_unknown(tmp_path, reported):
    """任务失败不等于物理命令已结算；只允许当前在线设备报告的本作业命令。"""
    authority = _authority(tmp_path / 'failed-resolution.db')
    payload = _payload()
    try:
        authority.dispatch(payload)
        authority.fail_restarted_jobs((payload['job_id'],))
        unknown_job = payload['job_id'] if reported == 'current' else str(uuid.uuid4())
        command_id = f'workflow-node-job:{unknown_job}:pick'
        registration = authority.store.register_session({
            'edge_key': 'local', 'instance_uuid': str(uuid.uuid4()),
            'devices': [{'local_id': payload['device_id'], 'unknown_command_ids': [command_id]}],
        })
        authority.store.set_session_connected(registration['session_uuid'], reported != 'offline')
        if reported != 'current':
            with pytest.raises(ValueError, match='no unresolved UNKNOWN'):
                authority.store.create_unknown_resolution(payload['job_id'], reason='已复位软件仿真')
            return
        result = authority.store.create_unknown_resolution(payload['job_id'], reason='已复位软件仿真')
        command = next(c for c in authority.store.pending_commands() if c['message_uuid'] == result['command_uuid'])
        assert command['payload']['device_command_id'] == command_id
        authority.resolve_unknown_committed(payload['job_id'])
        assert authority.store.job(payload['job_id'])['status'] == 'failed'
    finally:
        authority.stop()
