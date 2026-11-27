"""生产 Edge 协议客户端的持久化和任务闭环测试。"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import types
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.edge_control.client import (
    EdgeControlClient,
    EdgeControlSettings,
    _stored_event_envelope,
)
from unilabos.app.edge_control.http import (
    BACKEND_UNAUTHORIZED_BUSINESS_CODE,
    EdgeDataPlane,
    EdgeProtocolHTTPError,
)
from unilabos.app.edge_control.store import EdgeControlStore, StoredJob
from unilabos.config.config import BasicConfig, EdgeControlConfig, HTTPConfig
from unilabos.resources.resource_graph_node_kind import is_site_resource_node


class FakeDataPlane:
    def __init__(self) -> None:
        """创建记录 HTTP 作业载荷和终态的测试数据面；参数与返回均为空。"""

        self.fetched_jobs: list[StoredJob] = []
        self.outcomes: list[dict[str, Any]] = []
        self.device_statuses: list[dict[str, Any]] = []

    def update_device_status(
        self,
        session_uuid: str,
        local_device_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """记录 Runtime 提交给 Scheduler 的实时设备状态。"""

        item = {
            "session_uuid": session_uuid,
            "local_device_id": local_device_id,
            **payload,
        }
        self.device_statuses.append(item)
        return item

    def fetch_job(self, job: StoredJob) -> dict[str, Any]:
        """按持久 Edge 作业返回含 Claim/Fence 的动作载荷。

        参数：``job`` 是动作执行镜像。返回可执行 HTTP 载荷；测试实现不抛异常。
        """

        self.fetched_jobs.append(job)
        return {
            "job_uuid": job.job_uuid,
            "task_uuid": job.task_uuid,
            "node_uuid": job.node_uuid,
            "command_uuid": job.command_uuid,
            "claim_uuid": job.claim_uuid,
            "attempt": job.attempt,
            "fences": [
                {"lock_key": lock_key, "fencing_token": token}
                for lock_key, token in job.fences
            ],
            "local_device_id": "heater-01",
            "action_name": "heat",
            "action_type": "UniLabJsonCommand",
            "param": {
                "unilabos_device_id": "heater-01",
				"timeout_seconds": 7200,
				"assignee_user_ids": ["operator-1"],
                "temperature": 37,
            },
        }

    def commit_outcome(
        self,
        job: StoredJob,
        outcome: str,
        return_info: dict[str, Any],
        error_info: list[dict[str, Any]],
        unknown_command_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        self.outcomes.append(
            {
                "job": job,
                "outcome": outcome,
                "return_info": return_info,
                "error_info": error_info,
                "unknown_command_ids": unknown_command_ids or [],
            }
        )
        return {"uuid": str(uuid.uuid4())}


def _claim_fields(
    *,
    lock_key: str = "/devices/heater-01",
    fencing_token: int = 1,
) -> dict[str, Any]:
    """构造经过 Scheduler 签发的 Job Claim 与单资源 Fence 测试载荷。"""

    return {
        "claim_uuid": str(uuid.uuid4()),
        "attempt": 1,
        "fences": [
            {"lock_key": lock_key, "fencing_token": fencing_token},
        ],
    }


def test_edge_control_settings_defaults_to_local_control_plane(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(EdgeControlConfig, "scheduler_addr", "")
    monkeypatch.setattr(EdgeControlConfig, "backend_addr", "")
    monkeypatch.setattr(EdgeControlConfig, "state_db", str(tmp_path / "edge.db"))
    monkeypatch.setattr(HTTPConfig, "schedule_addr", "")
    monkeypatch.setattr(HTTPConfig, "remote_addr", "http://[::1]:8080")
    monkeypatch.setattr(BasicConfig, "machine_name", "edge-fixture")

    settings = EdgeControlSettings.from_config()

    assert settings.backend_address == "http://127.0.0.1:8002"
    assert settings.scheduler_address == "http://127.0.0.1:8002"


def test_runtime_device_status_refreshes_scheduler_registration(
    tmp_path: Path,
) -> None:
    """Runtime 属性回调必须同时刷新设备健康与未知命令快照。"""

    async def scenario() -> None:
        data_plane = FakeDataPlane()
        host_node = FakeHostNode()
        host_node.dispatch_block_reasons["heater-01"] = (
            "unresolved_unknown_command:command-1"
        )
        client = EdgeControlClient(
            _settings(tmp_path / "device-status.db"),
            data_plane=data_plane,  # type: ignore[arg-type]
            host_node_provider=lambda: host_node,
        )
        client._session_uuid = str(uuid.uuid4())
        client._connected.set()

        await client._commit_device_status(
            "heater-01",
            {"temperature": 37.5},
        )

        assert data_plane.device_statuses == [
            {
                "session_uuid": client._session_uuid,
                "local_device_id": "heater-01",
                "online": True,
                "dispatch_block_reason": (
                    "unresolved_unknown_command:command-1"
                ),
                "unknown_command_ids": ["command-1"],
                "status": {"temperature": 37.5},
            }
        ]
        client.store.close()

    asyncio.run(scenario())


def test_runtime_republishes_latest_properties_after_disconnect_and_transient_failure(tmp_path):
    from dataclasses import replace

    async def scenario():
        class FailingOnce(FakeDataPlane):
            failures = 1

            def update_device_status(self, *args, **kwargs):
                if self.failures:
                    self.failures -= 1
                    raise ConnectionError("temporary failure")
                return super().update_device_status(*args, **kwargs)

        class HostFailingOnce(FakeHostNode):
            failures = 1

            def device_dispatch_block_reason(self, device_id):
                if self.failures:
                    self.failures -= 1
                    raise OSError("temporary journal failure")
                return super().device_dispatch_block_reason(device_id)

        data_plane = FailingOnce()
        host = HostFailingOnce()
        host.device_status = {"heater-01": {"temperature": 25.0}}
        client = EdgeControlClient(
            replace(_settings(tmp_path / "status-retry.db"), device_status_interval=0.01),
            data_plane=data_plane, host_node_provider=lambda: host,
        )
        client._session_uuid = str(uuid.uuid4())
        await client._commit_device_status("heater-01", {"temperature": 25.0})
        assert not data_plane.device_statuses
        client._connected.set()
        sender = asyncio.create_task(client._device_status_sender())
        try:
            for _ in range(100):
                if data_plane.device_statuses:
                    break
                await asyncio.sleep(0.01)
            assert data_plane.device_statuses[-1]["status"] == {"temperature": 25.0}
            host.device_status["heater-01"]["temperature"] = 26.0
            await client._commit_device_status("heater-01", {"temperature": 25.0})
            assert data_plane.device_statuses[-1]["status"] == {"temperature": 26.0}
        finally:
            client._stopping.set()
            await sender
            client.store.close()

    asyncio.run(scenario())


class FakeHostNode:
    def __init__(self) -> None:
        """初始化协议测试所需的 HostNode 行为记录器。

        参数和返回值为空；各列表分别记录动作派发、执行未知处置和物理结算
        退役身份，``dispatch_block_reasons`` 保存设备当前阻断投影。
        """

        self.started: list[dict[str, Any]] = []
        self.cancel_requests: list[str] = []
        self.cancelable_jobs: set[str] = set()
        self.unknown_resolutions: list[dict[str, str]] = []
        self.retired_commands: list[str] = []
        self.dispatch_block_reasons: dict[str, str] = {}

    def send_goal(
        self,
        item: Any,
        action_type: str,
        action_kwargs: dict[str, Any],
        sample_material: dict[str, str],
        server_info: Any,
    ) -> None:
        self.started.append(
            {
                "item": item,
                "action_type": action_type,
                "action_kwargs": action_kwargs,
                "sample_material": sample_material,
                "server_info": server_info,
            }
        )

    def cancel_goal(self, job_uuid: str) -> bool:
        """记录对运行中工作流节点作业的取消请求。

        ``job_uuid`` 是作业身份；测试显式标记可取消时返回 ``True``，
        否则模拟重启后内存 goal 映射丢失。
        """

        self.cancel_requests.append(job_uuid)
        return job_uuid in self.cancelable_jobs

    def resolve_unknown_device_command(
        self,
        device_id: str,
        device_command_id: str,
        resolution_command_uuid: str,
        reason: str,
    ) -> dict[str, Any]:
        self.unknown_resolutions.append(
            {
                "device_id": device_id,
                "device_command_id": device_command_id,
                "resolution_command_uuid": resolution_command_uuid,
                "reason": reason,
            }
        )
        return {
            "command_id": device_command_id,
            "state": "CANCELED",
            "previous_state": "UNKNOWN",
            "resolution_committed": True,
            "resolution_command_uuid": resolution_command_uuid,
            "message": reason,
        }

    def device_dispatch_block_reason(self, device_id: str) -> str:
        return self.dispatch_block_reasons.get(device_id, "")

    def device_unknown_command_ids(self, device_id: str) -> list[str]:
        """从展示阻断投影还原测试设备的结构化 UNKNOWN 命令集合。

        ``device_id`` 是本地设备身份；返回尚未完成物理结算的命令列表。
        """

        reason = self.device_dispatch_block_reason(device_id)
        prefix = "unresolved_unknown_command:"
        return reason.removeprefix(prefix).split(",") if reason.startswith(prefix) else []

    def retire_settled_device_command(self, device_command_id: str) -> int:
        """记录 Backend ACK 触发的设备账本退役广播。

        ``device_command_id`` 是稳定物理命令身份；返回一个模拟退役计数。
        """

        self.retired_commands.append(device_command_id)
        return 1


class FakeRegistrationResources:
    def dump(self) -> list[list[dict[str, Any]]]:
        return [
            [
                {
                    "id": "robot-01",
                    "name": "Robot 01",
                    "barcode": "ROBOT-01",
                }
            ]
        ]


class FakeRegistrationResourcesWithoutBarcode:
    def dump(self) -> list[list[dict[str, Any]]]:
        return [[{"id": "robot-01", "name": "Robot 01"}]]


class FakeRegistrationResourcesWithClass:
    def dump(self) -> list[list[dict[str, Any]]]:
        return [[{
            "id": "robot-01",
            "name": "Robot 01",
            "barcode": "ROBOT-01",
            "class": "community.test.robot",
        }]]


class FakeRegistrationResourcesWithSite:
    def dump(self) -> list[list[dict[str, Any]]]:
        return [[
            {
                "id": "robot-01",
                "uuid": "50000000-0000-4000-8000-000000000201",
                "name": "Robot 01",
                "type": "device",
                "barcode": "ROBOT-01",
                "parent_uuid": None,
            },
            {
                "id": "robot-01_well_A1",
                "uuid": "50000000-0000-4000-8000-000000000202",
                "name": "robot-01_well_A1",
                "type": "well",
                "barcode": "ROBOT-01-WELL-A1",
                "parent_uuid": "50000000-0000-4000-8000-000000000201",
            },
        ]]


class FakeRegistrationResourcesWithSiteBarcodeCollision:
    def dump(self) -> list[list[dict[str, Any]]]:
        return [[
            {
                "id": "robot-01",
                "uuid": "50000000-0000-4000-8000-000000000201",
                "name": "Robot 01",
                "type": "device",
                "barcode": "SHARED-BARCODE",
                "parent_uuid": None,
            },
            {
                "id": "robot-01_well_A1",
                "uuid": "50000000-0000-4000-8000-000000000202",
                "name": "robot-01_well_A1",
                "type": "well",
                "barcode": "SHARED-BARCODE",
                "parent_uuid": "50000000-0000-4000-8000-000000000201",
            },
        ]]


class FakeRegistrationHostNode:
    def __init__(self) -> None:
        self.resources_config = FakeRegistrationResources()
        self.devices_names = {"robot-01": "/devices/robot-01"}
        self._action_value_mappings = {
            "robot-01": {
                "_execute_driver_command": {"type": "StrSingleInput"},
                "pick": {"type": "UniLabJsonCommand"},
                "place": {"type": "UniLabJsonCommand"},
            }
        }

    def device_dispatch_block_reason(self, device_id: str) -> str:
        if device_id == "robot-01":
            return "unresolved_unknown_command:workflow-node-job:old-job"
        return ""

    def device_unknown_command_ids(self, device_id: str) -> list[str]:
        """返回注册竞态测试中指定设备的结构化 UNKNOWN 命令集合。

        ``device_id`` 是注册资源身份；已知机器人返回一条稳定命令，其余返回空列表。
        """

        if device_id == "robot-01":
            return ["workflow-node-job:00000000-0000-4000-8000-000000000001"]
        return []


class FakeRegistrationHostNodeWithSystemDevice(FakeRegistrationHostNode):
    def __init__(self) -> None:
        super().__init__()
        self.resources_config = type(
            "Resources",
            (),
            {
                "dump": lambda _self: [[
                    {"id": "robot-01", "name": "Robot 01", "barcode": "ROBOT-01"},
                ]]
            },
        )()
        self.device_id = "host_node"
        self.devices_names["host_node"] = "/devices/host_node"
        self._action_value_mappings["host_node"] = {
            "transfer_resource": {"type": "UniLabJsonCommandAsync"},
        }


class FakeRegistrationDataPlane:
    def material_uuids_by_barcode(
        self, barcodes: Iterable[str], *, current_only_barcodes: Iterable[str] = ()
    ) -> dict[str, str]:
        return {barcode: str(uuid.uuid4()) for barcode in barcodes}

    def lookup_production_resource_nodes(
        self, query_uuids: Sequence[str]
    ) -> list[dict[str, Any]]:
        return []


class FakeRegistrationDataPlaneWithSite:
    def __init__(self) -> None:
        self.robot_uuid = str(uuid.uuid4())
        self.site_uuid = str(uuid.uuid4())

    def material_uuids_by_barcode(
        self, barcodes: Iterable[str], *, current_only_barcodes: Iterable[str] = ()
    ) -> dict[str, str]:
        return {"ROBOT-01": self.robot_uuid}

    def site_uuids_by_local_id(
        self, local_ids: Iterable[str]
    ) -> dict[str, str]:
        assert set(local_ids) == {"robot-01_well_A1"}
        return {"robot-01_well_A1": self.site_uuid}

    def lookup_production_resource_nodes(
        self, query_uuids: Sequence[str]
    ) -> list[dict[str, Any]]:
        return []


class FakeRegistrationDataPlaneWithSiteBarcodeCollision(
    FakeRegistrationDataPlaneWithSite
):
    def material_uuids_by_barcode(
        self, barcodes: Iterable[str], *, current_only_barcodes: Iterable[str] = ()
    ) -> dict[str, str]:
        assert set(barcodes) == {"SHARED-BARCODE"}
        return {"SHARED-BARCODE": self.robot_uuid}


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class RecordingWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, encoded: str) -> None:
        self.messages.append(__import__("json").loads(encoded))


class RecordingSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if method == "GET":
            job_uuid = url.rsplit("/", 1)[-1]
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "job_uuid": job_uuid,
                        "task_uuid": kwargs["params"]["task_uuid"],
                        "node_uuid": kwargs["params"]["node_uuid"],
                        "command_uuid": kwargs["headers"]["X-Command-UUID"],
                    },
                }
            )
        return FakeResponse({"code": 0, "data": {"uuid": str(uuid.uuid4())}})


def _settings(path: Path) -> EdgeControlSettings:
    return EdgeControlSettings(
        scheduler_address="http://scheduler:8081",
        backend_address="http://backend:8080",
        api_key="edge-secret",
        edge_key="edge-test",
        capability_revision="test-v1",
        instance_uuid="",
        state_db=str(path),
        reconnect_interval=0.01,
        request_timeout=1,
        event_retry_interval=1,
    )


def test_registration_reports_logical_actions_instead_of_transport_endpoint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    host_node = FakeRegistrationHostNode()
    client = EdgeControlClient(
        _settings(path),
        data_plane=FakeRegistrationDataPlane(),  # type: ignore[arg-type]
        host_node_provider=lambda: host_node,
    )

    devices = client._registration_devices()

    assert devices[0]["actions"] == [
        {"name": "pick", "type": "UniLabJsonCommand"},
        {"name": "place", "type": "UniLabJsonCommand"},
    ]
    assert devices[0]["unknown_command_ids"] == [
        "workflow-node-job:00000000-0000-4000-8000-000000000001"
    ]
    client.store.close()


def test_registration_uses_the_shared_graph_barcode_fallback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    host_node = FakeRegistrationHostNode()
    host_node.resources_config = FakeRegistrationResourcesWithoutBarcode()
    client = EdgeControlClient(
        _settings(path),
        data_plane=FakeRegistrationDataPlane(),  # type: ignore[arg-type]
        host_node_provider=lambda: host_node,
    )

    devices = client._registration_devices()

    assert devices[0]["barcode"] == "UNILAB-GRAPH-robot-01"
    client.store.close()


def test_registration_falls_back_to_registry_actions_during_discovery_race(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    host_node = FakeRegistrationHostNode()
    host_node.resources_config = FakeRegistrationResourcesWithClass()
    host_node._action_value_mappings["robot-01"] = {}
    fake_registry_module = types.ModuleType("unilabos.registry.registry")
    fake_registry_module.lab_registry = types.SimpleNamespace(
        device_type_registry={
            "community.test.robot": {
            "class": {
                "action_value_mappings": {
                    "_execute_driver_command": {"type": "StrSingleInput"},
                    "submit_pick_from_s06": {"type": "UniLabJsonCommand"},
                }
            }
            }
        }
    )
    monkeypatch.setitem(
        sys.modules,
        "unilabos.registry.registry",
        fake_registry_module,
    )
    client = EdgeControlClient(
        _settings(tmp_path / "runtime.db"),
        data_plane=FakeRegistrationDataPlane(),  # type: ignore[arg-type]
        host_node_provider=lambda: host_node,
    )

    devices = client._registration_devices()

    assert devices[0]["actions"] == [
        {"name": "submit_pick_from_s06", "type": "UniLabJsonCommand"}
    ]
    client.store.close()


def test_registration_includes_host_node_as_default_system_device(
    tmp_path: Path,
) -> None:
    client = EdgeControlClient(
        _settings(tmp_path / "runtime.db"),
        data_plane=FakeRegistrationDataPlane(),  # type: ignore[arg-type]
        host_node_provider=FakeRegistrationHostNodeWithSystemDevice,
    )

    devices = client._registration_devices()

    assert [device["local_id"] for device in devices] == ["host_node", "robot-01"]
    assert devices[0]["barcode"] == "UNILAB-GRAPH-host_node"
    assert devices[0]["actions"] == [
        {"name": "transfer_resource", "type": "UniLabJsonCommandAsync"},
    ]
    client.store.close()


def test_registration_resolves_resource_site_identity(tmp_path: Path) -> None:
    host_node = FakeRegistrationHostNode()
    host_node.resources_config = FakeRegistrationResourcesWithSite()
    data_plane = FakeRegistrationDataPlaneWithSite()
    client = EdgeControlClient(
        _settings(tmp_path / "runtime.db"),
        data_plane=data_plane,  # type: ignore[arg-type]
        host_node_provider=lambda: host_node,
    )

    devices = client._registration_devices()

    assert devices[0]["material_uuid"] == data_plane.robot_uuid
    from unilabos.ros.nodes.resource_slot_hydration import (
        query_production_resource_nodes_sync,
    )

    projected = query_production_resource_nodes_sync([data_plane.robot_uuid])
    assert [row["uuid"] for row in projected] == [
        data_plane.robot_uuid,
        data_plane.site_uuid,
    ]
    client.store.close()


def test_registration_keeps_site_identity_when_barcode_matches_material(
    tmp_path: Path,
) -> None:
    host_node = FakeRegistrationHostNode()
    host_node.resources_config = FakeRegistrationResourcesWithSiteBarcodeCollision()
    data_plane = FakeRegistrationDataPlaneWithSiteBarcodeCollision()
    client = EdgeControlClient(
        _settings(tmp_path / "runtime.db"),
        data_plane=data_plane,  # type: ignore[arg-type]
        host_node_provider=lambda: host_node,
    )

    devices = client._registration_devices()

    assert devices[0]["material_uuid"] == data_plane.robot_uuid
    from unilabos.ros.nodes.resource_slot_hydration import (
        query_production_resource_nodes_sync,
    )

    projected = query_production_resource_nodes_sync([data_plane.robot_uuid])
    assert [row["uuid"] for row in projected] == [
        data_plane.robot_uuid,
        data_plane.site_uuid,
    ]
    client.store.close()


@pytest.mark.parametrize(
    "node",
    [
        {"type": "well"},
        {"type": "TipSpot"},
        {"type": "tip_spot"},
        {"type": "tip-spot"},
        {"class": "TipSpot"},
        {"config": {"category": "well"}},
        {"config": {"type": "tip-spot"}},
    ],
)
def test_site_resource_classifier_matches_bootstrap_aliases(
    node: dict[str, Any],
) -> None:
    assert is_site_resource_node(node) is True


def test_store_persists_command_job_and_event_ack(tmp_path: Path) -> None:
    """验证命令、作业身份和事件 ACK 可跨 Edge 账本重启恢复。

    参数：``tmp_path`` 是隔离 SQLite 目录。返回无；身份、追踪或 ACK 丢失时失败。
    """

    path = tmp_path / "edge-control.db"
    command_uuid = str(uuid.uuid4())
    job_uuid = str(uuid.uuid4())
    task_uuid = str(uuid.uuid4())
    node_uuid = str(uuid.uuid4())
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    tracestate = "vendor=value"
    store = EdgeControlStore(str(path))

    assert store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 7,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
            "traceparent": traceparent,
            "tracestate": tracestate,
        }
    )
    assert not store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 7,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
        }
    )
    assert store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": task_uuid,
            "node_uuid": node_uuid,
            "job_access_token": "short-token",
            **_claim_fields(),
        },
        command_uuid,
    )
    store.mark_command_completed(command_uuid)
    event_uuid = store.enqueue_event(
        "command.ack",
        {"command_uuid": command_uuid},
        {"traceparent": traceparent, "tracestate": tracestate},
    )

    assert store.last_ack_command_sequence() == 7
    job = store.get_job(job_uuid)
    assert job is not None
    assert job.traceparent == traceparent
    assert job.tracestate == tracestate
    assert store.save_pending_outcome(
        job_uuid,
        "succeeded",
        {"suc": True},
        [],
    )
    assert store.get_pending_outcome(job_uuid) is not None
    pending_events = store.pending_events(0)
    assert [event.event_uuid for event in pending_events] == [event_uuid]
    assert pending_events[0].traceparent == traceparent
    assert pending_events[0].tracestate == tracestate
    envelope = _stored_event_envelope(pending_events[0])
    assert envelope["traceparent"] == traceparent
    assert envelope["tracestate"] == tracestate
    store.acknowledge_event(event_uuid)
    assert store.pending_events(float("inf")) == []
    instance_uuid = store.get_or_create_instance_uuid()
    store.close()

    reopened = EdgeControlStore(str(path))
    assert reopened.get_or_create_instance_uuid() == instance_uuid
    reopened_job = reopened.get_job(job_uuid)
    assert reopened_job is not None
    assert reopened_job.traceparent == traceparent
    assert reopened_job.tracestate == tracestate
    pending_outcome = reopened.get_pending_outcome(job_uuid)
    assert pending_outcome is not None
    assert pending_outcome.return_info == {"suc": True}
    reopened.close()


def test_edge_store_rejects_stale_resource_fence_before_job_mirror(tmp_path: Path) -> None:
    """执行进程必须在创建 Job 镜像前拒绝被新 Claim 淘汰的旧 Fence。

    参数：``tmp_path`` 提供隔离 Edge SQLite。返回无；断言相同命令重放幂等、
    低 Fence 关闭式拒绝、更高 Fence 可推进。异常：陈旧命令留下作业镜像会失败。
    """

    store = EdgeControlStore(str(tmp_path / "edge-fence.db"))
    lock_key = "/devices/heater-01"
    first_job_uuid = str(uuid.uuid4())
    first_payload = {
        "job_uuid": first_job_uuid,
        "task_uuid": str(uuid.uuid4()),
        "node_uuid": str(uuid.uuid4()),
        "job_access_token": "first-token",
        **_claim_fields(lock_key=lock_key, fencing_token=2),
    }
    first_command_uuid = str(uuid.uuid4())
    try:
        assert store.save_job_start(first_payload, first_command_uuid) is True
        assert store.save_job_start(first_payload, first_command_uuid) is False

        stale_job_uuid = str(uuid.uuid4())
        stale_payload = {
            "job_uuid": stale_job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "stale-token",
            **_claim_fields(lock_key=lock_key, fencing_token=1),
        }
        with pytest.raises(ValueError, match="stale resource Fence"):
            store.save_job_start(stale_payload, str(uuid.uuid4()))
        assert store.get_job(stale_job_uuid) is None

        next_job_uuid = str(uuid.uuid4())
        next_payload = {
            "job_uuid": next_job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "next-token",
            **_claim_fields(lock_key=lock_key, fencing_token=3),
        }
        assert store.save_job_start(next_payload, str(uuid.uuid4())) is True
        assert store.get_job(next_job_uuid) is not None
    finally:
        store.close()

def test_explicit_local_reset_preserves_identity_and_clears_protocol_work(
    tmp_path: Path,
) -> None:
    """调试重建只清理协议任务，不改变持久 Edge 身份。"""

    path = tmp_path / "edge-control.db"
    store = EdgeControlStore(str(path))
    instance_uuid = store.get_or_create_instance_uuid()
    command_uuid = str(uuid.uuid4())
    job_uuid = str(uuid.uuid4())
    store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 1,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
        }
    )
    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "reset-token",
            **_claim_fields(),
        },
        command_uuid,
    )
    store.enqueue_event("command.ack", {"command_uuid": command_uuid})

    store.reset_transient_state()

    assert store.get_or_create_instance_uuid() == instance_uuid
    assert store.get_job(job_uuid) is None
    assert store.command_status(command_uuid) == ""
    assert store.pending_events(float("inf")) == []
    store.close()


def test_store_resets_protocol_state_when_backend_edge_identity_changes(
    tmp_path: Path,
) -> None:
    """验证上游 Edge 身份变化时只重置协议运行态并保留本机实例身份。

    参数：``tmp_path`` 是隔离账本目录。返回无；旧命令或作业泄漏到新身份时失败。
    """

    store = EdgeControlStore(str(tmp_path / "runtime.db"))
    instance_uuid = store.get_or_create_instance_uuid()
    first_edge_uuid = str(uuid.uuid4())
    second_edge_uuid = str(uuid.uuid4())
    command_uuid = str(uuid.uuid4())
    job_uuid = str(uuid.uuid4())

    assert store.adopt_authority_edge_uuid(first_edge_uuid) is False
    assert store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 7,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
        }
    )
    store.mark_command_completed(command_uuid)
    assert store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "test-token",
            **_claim_fields(),
        },
        command_uuid,
    )
    store.set_job_status(job_uuid, "running")
    store.enqueue_event("job.started", {"job_uuid": job_uuid})

    assert store.adopt_authority_edge_uuid(first_edge_uuid) is False
    assert store.last_ack_command_sequence() == 7
    assert store.get_job(job_uuid) is not None

    assert store.adopt_authority_edge_uuid(second_edge_uuid) is True
    assert store.last_ack_command_sequence() == 0
    assert store.get_job(job_uuid) is None
    assert store.pending_events(float("inf")) == []
    assert store.get_or_create_instance_uuid() == instance_uuid
    assert store.get_meta("authority_edge_uuid") == second_edge_uuid
    store.close()


def test_store_migrates_existing_runtime_and_outbox_schema(tmp_path: Path) -> None:
    if BasicConfig.database_type == "postgresql":
        pytest.skip("此用例验证旧 SQLite 文件结构升级，不是跨数据库数据迁移")
    path = tmp_path / "old-edge-control.db"
    job_uuid = str(uuid.uuid4())
    event_uuid = str(uuid.uuid4())
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE edge_event_outbox (
            event_uuid TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_sent_at REAL,
            acked_at REAL
        );
        CREATE TABLE edge_job_runtime (
            job_uuid TEXT PRIMARY KEY,
            task_uuid TEXT NOT NULL,
            node_uuid TEXT NOT NULL,
            command_uuid TEXT NOT NULL,
            job_access_token TEXT NOT NULL,
            status TEXT NOT NULL,
            feedback_sequence INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO edge_event_outbox(
            event_uuid, type, payload_json, created_at
        ) VALUES (?, 'command.ack', '{}', '2026-08-02T00:00:00Z')
        """,
        (event_uuid,),
    )
    connection.execute(
        """
        INSERT INTO edge_job_runtime(
            job_uuid, task_uuid, node_uuid, command_uuid,
            job_access_token, status, updated_at
        ) VALUES (?, ?, ?, ?, 'token', 'received', 1)
        """,
        (job_uuid, str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())),
    )
    connection.commit()
    connection.close()

    store = EdgeControlStore(str(path))
    job = store.get_job(job_uuid)
    events = store.pending_events(float("inf"))
    assert job is not None
    assert job.traceparent == ""
    assert [event.event_uuid for event in events] == [event_uuid]
    assert events[0].traceparent == ""
    with sqlite3.connect(path) as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == EdgeControlStore.SCHEMA_VERSION
        assert {
            str(row[1])
            for row in migrated.execute("PRAGMA table_info(edge_event_outbox)")
        } >= {"trace_id", "traceparent", "tracestate"}
        assert {
            str(row[1])
            for row in migrated.execute("PRAGMA table_info(edge_command)")
        } >= {"trace_id", "traceparent", "tracestate"}
        assert {
            str(row[1])
            for row in migrated.execute("PRAGMA table_info(edge_job_runtime)")
        } >= {"trace_id", "traceparent", "tracestate"}
    store.close()


def test_store_discards_legacy_pong_events_when_reopened(tmp_path: Path) -> None:
    path = tmp_path / "edge-control.db"
    store = EdgeControlStore(str(path))
    store.enqueue_event("pong", {"ping_uuid": str(uuid.uuid4())})
    store.close()

    reopened = EdgeControlStore(str(path))

    assert reopened.pending_events(float("inf")) == []
    reopened.close()


def test_ping_pong_is_sent_on_current_connection_without_outbox(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "runtime.db"
        store = EdgeControlStore(str(path))
        client = EdgeControlClient(_settings(path), store=store)
        websocket = RecordingWebSocket()
        client._websocket = websocket
        ping_uuid = str(uuid.uuid4())

        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": str(uuid.uuid4()),
                "sequence": 0,
                "type": "ping",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "payload": {"ping_uuid": ping_uuid},
            }
        )

        assert len(websocket.messages) == 1
        assert websocket.messages[0]["type"] == "pong"
        assert websocket.messages[0]["payload"] == {"ping_uuid": ping_uuid}
        assert store.pending_events(float("inf")) == []
        store.close()

    asyncio.run(scenario())


def test_material_changed_is_acknowledged_without_dropping_connection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = EdgeControlStore(str(tmp_path / "runtime.db"))
        client = EdgeControlClient(_settings(tmp_path / "runtime.db"), store=store)
        command_uuid = str(uuid.uuid4())
        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": command_uuid,
                "sequence": 9,
                "type": "material.changed",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "payload": {
                    "device_material_uuid": str(uuid.uuid4()),
                    "material_uuid": str(uuid.uuid4()),
                    "action": "update",
                },
            }
        )

        assert store.command_status(command_uuid) == "completed"
        assert store.last_ack_command_sequence() == 9
        events = store.pending_events(float("inf"))
        assert [event.event_type for event in events] == ["command.ack"]
        assert events[0].payload == {"command_uuid": command_uuid}
        store.close()

    asyncio.run(scenario())


def test_unknown_resolution_is_committed_locally_before_edge_ack(
    tmp_path: Path,
) -> None:
    """验证执行未知处置先本地落账，并在 Backend ACK 后完成设备退役。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。重复对账恢复命令不得
    再次调用驱动或生成第二份处置事件，最终 ACK 同时结算子命令和根命令。
    """

    async def scenario() -> None:
        """运行一次执行未知处置、ACK 和重复命令场景；参数与返回值为空。"""

        path = tmp_path / "runtime.db"
        store = EdgeControlStore(str(path))
        host_node = FakeHostNode()
        client = EdgeControlClient(
            _settings(path), store=store, host_node_provider=lambda: host_node
        )
        # 当前作业、人工对账下行命令和设备子命令的稳定身份。
        command_uuid = str(uuid.uuid4())
        job_uuid = str(uuid.uuid4())
        device_command_id = f"workflow-node-job:{job_uuid}:atomic-transfer-place"
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": str(uuid.uuid4()),
                "node_uuid": str(uuid.uuid4()),
                "job_access_token": "temporary-secret",
                **_claim_fields(),
            },
            str(uuid.uuid4()),
        )
        assert store.save_pending_outcome(
            job_uuid,
            "failed",
            {},
            [{"message": "物理结果未知"}],
            [device_command_id],
        )
        outcome_event_uuid = store.complete_pending_outcome(
            job_uuid, {"job_uuid": job_uuid}
        )
        store.acknowledge_event(outcome_event_uuid)
        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": command_uuid,
                "sequence": 10,
                "type": "job.resolve_unknown",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "payload": {
                    "job_uuid": job_uuid,
                    "local_device_id": "robot-01",
                    "device_command_id": device_command_id,
                    "resolution": "canceled",
                    "reason": "操作员确认 PLC 已复位且设备空闲",
                },
            }
        )

        assert host_node.unknown_resolutions == [
            {
                "device_id": "robot-01",
                "device_command_id": device_command_id,
                "resolution_command_uuid": command_uuid,
                "reason": "操作员确认 PLC 已复位且设备空闲",
            }
        ]
        assert store.command_status(command_uuid) == "completed"
        events = store.pending_events(float("inf"))
        assert [event.event_type for event in events] == [
            "job.unknown_resolution_committed",
            "command.ack",
        ]
        assert events[0].payload == {
            "job_uuid": job_uuid,
            "command_uuid": command_uuid,
            "device_command_id": device_command_id,
            "resolution": "canceled",
            "previous_state": "UNKNOWN",
            "current_state": "CANCELED",
            "unknown_command_ids": [],
            "dispatch_block_reason": "",
        }
        await client._handle_envelope(
            {
                "type": "event.ack",
                "payload": {"event_uuid": events[0].event_uuid},
            }
        )
        assert host_node.retired_commands == [
            device_command_id,
            f"workflow-node-job:{job_uuid}",
        ]
        assert store.get_job(job_uuid) is None
        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": command_uuid,
                "sequence": 10,
                "type": "job.resolve_unknown",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "payload": {
                    "job_uuid": job_uuid,
                    "local_device_id": "robot-01",
                    "device_command_id": device_command_id,
                    "resolution": "canceled",
                    "reason": "操作员确认 PLC 已复位且设备空闲",
                },
            }
        )
        remaining_events = store.pending_events(float("inf"))
        assert [event.event_type for event in remaining_events] == ["command.ack"]
        store.close()

    asyncio.run(scenario())


def test_unknown_resolution_maps_restart_root_to_unique_device_child(
    tmp_path: Path,
) -> None:
    """重启产生的保守根命令可解析到设备账本唯一 UNKNOWN 子命令。"""

    async def scenario() -> None:
        path = tmp_path / "root-resolution.db"
        store = EdgeControlStore(str(path))
        host_node = FakeHostNode()
        client = EdgeControlClient(
            _settings(path), store=store, host_node_provider=lambda: host_node
        )
        job_uuid = str(uuid.uuid4())
        command_uuid = str(uuid.uuid4())
        child_command_id = (
            f"workflow-node-job:{job_uuid}:atomic-transfer-pick"
        )
        host_node.dispatch_block_reasons["robot-01"] = (
            "unresolved_unknown_command:" + child_command_id
        )
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": str(uuid.uuid4()),
                "node_uuid": str(uuid.uuid4()),
                "job_access_token": "temporary-secret",
                **_claim_fields(),
            },
            str(uuid.uuid4()),
        )

        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": command_uuid,
                "sequence": 11,
                "type": "job.resolve_unknown",
                "sent_at": "2026-09-02T00:00:00.000000Z",
                "payload": {
                    "job_uuid": job_uuid,
                    "local_device_id": "robot-01",
                    "device_command_id": f"workflow-node-job:{job_uuid}",
                    "resolution": "canceled",
                    "reason": "确认重启前的原子取料未执行",
                },
            }
        )

        assert host_node.unknown_resolutions[0]["device_command_id"] == (
            child_command_id
        )
        resolution_event = next(
            event
            for event in store.pending_events(float("inf"))
            if event.event_type == "job.unknown_resolution_committed"
        )
        assert resolution_event.payload["device_command_id"] == child_command_id
        store.close()

    asyncio.run(scenario())


def test_running_job_cancel_without_memory_goal_stays_unsettled(
    tmp_path: Path,
) -> None:
    """重启后找不到内存 goal 不得误报作业已物理取消。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。已进入设备边界的
    作业在 HostNode 无 goal 映射时仅记录取消请求，主状态继续保持
    ``running``，并由独立清理状态表达需要物理对账。
    """

    async def scenario() -> None:
        """运行内存 goal 丢失的取消场景；参数和返回值为空。"""

        database_path = tmp_path / "cancel-running.db"
        store = EdgeControlStore(str(database_path))
        data_plane = FakeDataPlane()
        host_node = FakeHostNode()
        client = EdgeControlClient(
            _settings(database_path),
            store=store,
            data_plane=data_plane,  # type: ignore[arg-type]
            host_node_provider=lambda: host_node,
        )
        # 作业 UUID 定位取消超时后必须人工对账的运行行。
        job_uuid = str(uuid.uuid4())
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": str(uuid.uuid4()),
                "node_uuid": str(uuid.uuid4()),
                "job_access_token": "temporary-secret",
                **_claim_fields(),
            },
            str(uuid.uuid4()),
        )
        store.set_job_status(job_uuid, "running")

        # 取消命令 UUID 是 Backend 下行取消的幂等身份。
        cancel_command_uuid = str(uuid.uuid4())
        await client._handle_envelope(
            {
                "message_uuid": cancel_command_uuid,
                "sequence": 31,
                "type": "job.cancel",
                "payload": {"job_uuid": job_uuid},
            }
        )

        assert host_node.cancel_requests == [job_uuid]
        assert data_plane.outcomes == []
        canceled_job = store.get_job(job_uuid)
        assert canceled_job is not None
        assert canceled_job.status == "cancel_requested"
        assert [event.event_type for event in store.pending_events(float("inf"))] == [
            "command.ack"
        ]

        # Backend 超时收敛后会为同一作业逐条下发人工对账命令；
        # 当前作业仍有 UNKNOWN 时不得提前结算，其他作业的 UNKNOWN
        # 也不得阻止当前作业在自己的最后一条回执后完成结算。
        other_job_uuid = str(uuid.uuid4())
        first_device_command_id = f"workflow-node-job:{job_uuid}:atomic-transfer-pick"
        second_device_command_id = f"workflow-node-job:{job_uuid}:atomic-transfer-place"
        host_node.dispatch_block_reasons["heater-01"] = (
            "unresolved_unknown_command:"
            f"{second_device_command_id},workflow-node-job:{other_job_uuid}"
        )
        first_resolution_command_uuid = str(uuid.uuid4())
        await client._handle_envelope(
            {
                "message_uuid": first_resolution_command_uuid,
                "sequence": 32,
                "type": "job.resolve_unknown",
                "payload": {
                    "job_uuid": job_uuid,
                    "local_device_id": "heater-01",
                    "device_command_id": first_device_command_id,
                    "resolution": "canceled",
                    "reason": "操作员确认取料动作已物理复位",
                },
            }
        )
        first_resolution_event = next(
            event
            for event in store.pending_events(float("inf"))
            if event.event_type == "job.unknown_resolution_committed"
        )
        assert first_resolution_event.payload["unknown_command_ids"] == [
            second_device_command_id,
            f"workflow-node-job:{other_job_uuid}",
        ]
        partially_settled_job = store.get_job(job_uuid)
        assert partially_settled_job is not None
        assert partially_settled_job.status == "cancel_requested"

        await client._handle_envelope(
            {
                "type": "event.ack",
                "payload": {"event_uuid": first_resolution_event.event_uuid},
            }
        )

        host_node.dispatch_block_reasons["heater-01"] = (
            f"unresolved_unknown_command:workflow-node-job:{other_job_uuid}"
        )
        second_resolution_command_uuid = str(uuid.uuid4())
        await client._handle_envelope(
            {
                "message_uuid": second_resolution_command_uuid,
                "sequence": 33,
                "type": "job.resolve_unknown",
                "payload": {
                    "job_uuid": job_uuid,
                    "local_device_id": "heater-01",
                    "device_command_id": second_device_command_id,
                    "resolution": "canceled",
                    "reason": "操作员确认放料动作已物理复位",
                },
            }
        )
        second_resolution_event = next(
            event
            for event in store.pending_events(float("inf"))
            if event.event_type == "job.unknown_resolution_committed"
        )
        assert second_resolution_event.payload["unknown_command_ids"] == [
            f"workflow-node-job:{other_job_uuid}"
        ]
        pending_final_job = store.get_job(job_uuid)
        assert pending_final_job is not None
        assert pending_final_job.job_access_token == ""
        assert pending_final_job.status == (
            f"outcome_resolution_pending_final:{second_resolution_event.event_uuid}"
        )

        await client._handle_envelope(
            {
                "type": "event.ack",
                "payload": {"event_uuid": second_resolution_event.event_uuid},
            }
        )
        assert host_node.retired_commands == [
            first_device_command_id,
            second_device_command_id,
            f"workflow-node-job:{job_uuid}",
        ]
        assert store.get_job(job_uuid) is None
        store.close()

    asyncio.run(scenario())


def test_not_dispatched_job_cancel_commits_canceled_outcome(tmp_path: Path) -> None:
    """确定未下发的工作流节点作业可直接提交取消结果。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。作业仍处于
    ``received`` 时尚未进入设备边界，因此不需要制造执行未知。
    """

    async def scenario() -> None:
        """运行未下发作业的取消场景；参数和返回值为空。"""

        database_path = tmp_path / "cancel-received.db"
        store = EdgeControlStore(str(database_path))
        data_plane = FakeDataPlane()
        host_node = FakeHostNode()
        client = EdgeControlClient(
            _settings(database_path),
            store=store,
            data_plane=data_plane,  # type: ignore[arg-type]
            host_node_provider=lambda: host_node,
        )
        # 作业 UUID 定位仍处于 ``received`` 的未下发运行行。
        job_uuid = str(uuid.uuid4())
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": str(uuid.uuid4()),
                "node_uuid": str(uuid.uuid4()),
                "job_access_token": "temporary-secret",
                **_claim_fields(),
            },
            str(uuid.uuid4()),
        )

        # 取消命令 UUID 是 Backend 下行取消的幂等身份。
        cancel_command_uuid = str(uuid.uuid4())
        await client._handle_envelope(
            {
                "message_uuid": cancel_command_uuid,
                "sequence": 32,
                "type": "job.cancel",
                "payload": {"job_uuid": job_uuid},
            }
        )

        assert host_node.cancel_requests == []
        assert len(data_plane.outcomes) == 1
        assert data_plane.outcomes[0]["outcome"] == "canceled"
        assert data_plane.outcomes[0]["unknown_command_ids"] == []
        canceled_job = store.get_job(job_uuid)
        assert canceled_job is not None
        assert canceled_job.status == "outcome_committed"
        store.close()

    asyncio.run(scenario())


def test_fetch_failure_cannot_revive_job_canceled_concurrently(tmp_path: Path) -> None:
    """取参失败与取消并发时不得用重试状态复活作业。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。取参线程在
    取消结果已持久化后才抛错，调度协程必须退出而不再设置
    ``fetch_retry`` 或下发设备。
    """

    class FetchFailsAfterCancelDataPlane(FakeDataPlane):
        """模拟被并发取消截断后才失败的 Backend 取参请求。"""

        def __init__(self) -> None:
            """初始化取参已开始/可释放同步信号；参数和返回值为空。"""

            super().__init__()
            self.fetch_started = threading.Event()
            self.release_fetch = threading.Event()

        def fetch_job(self, job: StoredJob) -> dict[str, Any]:
            """等待测试完成取消后抛出取参错误。

            ``job`` 是当前工作流节点作业；方法无正常返回，未在一秒内
            收到释放信号或收到信号后均抛出 ``RuntimeError``。
            """

            self.fetched_jobs.append(job)
            self.fetch_started.set()
            if not self.release_fetch.wait(timeout=1):
                raise RuntimeError("测试未释放取参请求")
            raise RuntimeError("取参失败")

    async def scenario() -> None:
        """运行取参失败与取消的竞态场景；参数和返回值为空。"""

        database_path = tmp_path / "cancel-fetch-race.db"
        store = EdgeControlStore(str(database_path))
        data_plane = FetchFailsAfterCancelDataPlane()
        host_node = FakeHostNode()
        client = EdgeControlClient(
            _settings(database_path),
            store=store,
            data_plane=data_plane,  # type: ignore[arg-type]
            host_node_provider=lambda: host_node,
        )
        client._connected.set()
        # 作业/任务/节点 UUID 绑定未下发运行行，命令 UUID 绑定 job.start。
        job_uuid = str(uuid.uuid4())
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": str(uuid.uuid4()),
                "node_uuid": str(uuid.uuid4()),
                "job_access_token": "temporary-secret",
                **_claim_fields(),
            },
            str(uuid.uuid4()),
        )
        execution_task = asyncio.create_task(client._execute_job(job_uuid))
        assert await asyncio.to_thread(data_plane.fetch_started.wait, 1)

        # 取消命令 UUID 是与取参线程竞态的 Backend 下行幂等身份。
        cancel_command_uuid = str(uuid.uuid4())
        await client._handle_envelope(
            {
                "message_uuid": cancel_command_uuid,
                "sequence": 41,
                "type": "job.cancel",
                "payload": {"job_uuid": job_uuid},
            }
        )
        data_plane.release_fetch.set()
        await asyncio.wait_for(execution_task, timeout=1)

        assert len(data_plane.fetched_jobs) == 1
        assert host_node.started == []
        settled_job = store.get_job(job_uuid)
        assert settled_job is not None
        assert settled_job.status == "outcome_committed"
        store.close()

    asyncio.run(scenario())


def test_http_data_plane_uses_complete_job_attempt_identity() -> None:
    """验证生产反馈和结果请求携带完整执行尝试身份及未知命令集合。

    参数：无。返回：无；断言边缘执行镜像（EdgeExecutionMirror）不会在
    HTTP 适配器边界丢失对账恢复（Reconciliation）所需的物理命令身份。
    """

    # 设备子命令身份及作业/任务/节点/下行命令四重协议身份。
    unknown_command_id = "workflow-node-job:00000000-0000-4000-8000-000000000001:place"
    job = StoredJob(
        job_uuid=str(uuid.uuid4()),
        task_uuid=str(uuid.uuid4()),
        node_uuid=str(uuid.uuid4()),
        command_uuid=str(uuid.uuid4()),
        claim_uuid=str(uuid.uuid4()),
        attempt=2,
        fences=(("/devices/robot-01", 7),),
        job_access_token="short-token",
        status="received",
        feedback_sequence=0,
    )
    plane = EdgeDataPlane(
        "http://backend:8080/api/v1",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = RecordingSession()
    plane._session = session  # type: ignore[assignment]

    plane.fetch_job(job)
    plane.commit_feedback(
        job,
        1,
        "progress",
        {"percent": 50},
        "2026-08-30T00:00:00.000000Z",
    )
    plane.commit_outcome(
        job,
        "failed",
        {"state": "UNKNOWN"},
        [{"message": "物理结果未知"}],
        [unknown_command_id],
    )

    fetch = session.calls[0]
    assert fetch["params"] == {
        "task_uuid": job.task_uuid,
        "node_uuid": job.node_uuid,
    }
    assert fetch["headers"]["X-Command-UUID"] == job.command_uuid
    assert fetch["headers"]["X-Job-Token"] == job.job_access_token
    assert fetch["headers"]["Authorization"] == "Bearer edge-secret"
    assert len(fetch["headers"]["trace_id"]) == 32
    assert fetch["url"].startswith("http://scheduler:8081/api/v1/edge/jobs/")
    expected_identity = {
        "job_uuid": job.job_uuid,
        "task_uuid": job.task_uuid,
        "node_uuid": job.node_uuid,
        "command_uuid": job.command_uuid,
        "claim_uuid": job.claim_uuid,
        "attempt": 2,
        "fences": [
            {"lock_key": "/devices/robot-01", "fencing_token": 7},
        ],
    }
    feedback = session.calls[1]
    assert feedback["url"].startswith(
        "http://scheduler:8081/api/v1/edge/jobs/"
    )
    assert {
        field: feedback["json"][field] for field in expected_identity
    } == expected_identity
    outcome = session.calls[2]
    assert outcome["url"].startswith(
        "http://scheduler:8081/api/v1/edge/jobs/"
    )
    assert {
        field: outcome["json"][field] for field in expected_identity
    } == expected_identity
    assert outcome["json"]["unknown_command_ids"] == [unknown_command_id]
    assert outcome["headers"]["Idempotency-Key"] == f"{job.job_uuid}:outcome:v1"


def test_material_identity_lookup_includes_child_resources() -> None:
    """生产资源身份解析必须显式包含有父级的物料。

    参数：无。返回：无；断言 Edge 查询 Backend 时携带 ``with_children=true``。
    """

    class MaterialSession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.params: dict[str, Any] = {}

        def request(self, _method: str, _url: str, **kwargs: Any) -> FakeResponse:
            self.params = kwargs["params"]
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [{"barcode": "CHILD-01", "uuid": "material-01"}],
                        "total": 1,
                    },
                }
            )

    plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = MaterialSession()
    plane._session = session  # type: ignore[assignment]

    resolved = plane.material_uuids_by_barcode(["CHILD-01"])

    assert resolved == {"CHILD-01": "material-01"}
    assert session.params["with_children"] == "true"


def test_material_identity_lookup_recovers_departed_graph_identities_without_replacing_current() -> None:
    """启动可查离站历史身份；当前身份优先，不把历史条目恢复成库存。"""
    class MaterialSession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.scopes: list[str] = []

        def request(self, _method: str, _url: str, **kwargs: Any) -> FakeResponse:
            scope = kwargs["params"].get("inventory_scope", "current")
            self.scopes.append(scope)
            rows = [{"barcode": "DEVICE", "uuid": "current-device"}] if scope == "current" else [
                {"barcode": "DEPARTED", "uuid": "departed-material", "inventory_presence": "departed"},
                {"barcode": "DEVICE", "uuid": "historical-device", "inventory_presence": "departed"},
            ]
            return FakeResponse({"code": 0, "data": {"items": rows, "total": len(rows)}})

    plane = EdgeDataPlane("http://backend:8080", "http://scheduler:8081", "edge-secret")
    session = MaterialSession()
    plane._session = session  # type: ignore[assignment]
    assert plane.material_uuids_by_barcode(["DEVICE", "DEPARTED"]) == {
        "DEVICE": "current-device", "DEPARTED": "departed-material",
    }
    assert session.scopes == ["current", "history"]


def test_departed_device_cannot_register_from_historical_identity(tmp_path) -> None:
    """执行设备必须属于当前库存，历史身份不能恢复其动作权限。"""
    class HistoryOnlySession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def request(self, _method: str, _url: str, **kwargs: Any) -> FakeResponse:
            rows = [{"barcode": "ROBOT-01", "uuid": str(uuid.uuid4()), "inventory_presence": "departed"}] if kwargs["params"].get("inventory_scope") == "history" else []
            return FakeResponse({"code": 0, "data": {"items": rows, "total": len(rows)}})

    plane = EdgeDataPlane("http://backend:8080", "http://scheduler:8081", "edge-secret")
    plane._session = HistoryOnlySession()  # type: ignore[assignment]
    client = EdgeControlClient(
        _settings(tmp_path / "departed-device.db"),
        data_plane=plane,
        host_node_provider=FakeRegistrationHostNode,
    )
    with pytest.raises(RuntimeError, match="not been initialized"):
        client._registration_devices()


def test_site_identity_lookup_uses_backend_batch_resolution() -> None:
    """Production registration resolves only requested Backend Site identities."""

    class SiteIdentitySession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.method = ""
            self.url = ""
            self.payload: dict[str, Any] = {}

        def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.method = method
            self.url = url
            self.payload = kwargs["json"]
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "site_uuids_by_source_node_id": {
                            "plate_well_A1": (
                                "50000000-0000-4000-8000-000000000211"
                            ),
                            "unrequested": (
                                "50000000-0000-4000-8000-000000000212"
                            ),
                        }
                    },
                }
            )

    plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = SiteIdentitySession()
    plane._session = session  # type: ignore[assignment]

    resolved = plane.site_uuids_by_local_id(
        ["unknown", "plate_well_A1", "plate_well_A1"]
    )

    assert resolved == {
        "plate_well_A1": "50000000-0000-4000-8000-000000000211"
    }
    assert session.method == "POST"
    assert session.url == (
        "http://backend:8080/api/v1/materials/sites/resolve-identities"
    )
    assert session.payload == {
        "source_node_ids": ["plate_well_A1", "unknown"],
    }


def test_site_identity_lookup_chunks_more_than_backend_batch_limit() -> None:
    class ChunkedSiteIdentitySession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.calls: list[dict[str, Any]] = []

        def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            payload = kwargs["json"]
            self.calls.append({"method": method, "url": url, "payload": payload})
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "site_uuids_by_source_node_id": {
                            source_node_id: f"uuid-{source_node_id}"
                            for source_node_id in payload["source_node_ids"]
                        }
                    },
                }
            )

    plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = ChunkedSiteIdentitySession()
    plane._session = session  # type: ignore[assignment]
    local_ids = [f"site-{index:03d}" for index in range(501)]

    resolved = plane.site_uuids_by_local_id(
        [*local_ids, local_ids[-1], "", "   "]
    )

    assert [len(call["payload"]["source_node_ids"]) for call in session.calls] == [
        500,
        1,
    ]
    assert [
        source_node_id
        for call in session.calls
        for source_node_id in call["payload"]["source_node_ids"]
    ] == local_ids
    assert resolved == {
        source_node_id: f"uuid-{source_node_id}"
        for source_node_id in local_ids
    }


def test_site_identity_lookup_rejects_conflict_across_batches() -> None:
    class ConflictingSiteIdentitySession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.call_count = 0

        def request(self, _method: str, _url: str, **kwargs: Any) -> FakeResponse:
            self.call_count += 1
            source_node_ids = kwargs["json"]["source_node_ids"]
            identities = {
                source_node_id: f"uuid-{source_node_id}"
                for source_node_id in source_node_ids
            }
            if self.call_count == 1:
                identities["site-500"] = "uuid-site-500-from-first-batch"
            return FakeResponse(
                {
                    "code": 0,
                    "data": {"site_uuids_by_source_node_id": identities},
                }
            )

    plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = ConflictingSiteIdentitySession()
    plane._session = session  # type: ignore[assignment]

    with pytest.raises(EdgeProtocolHTTPError, match="duplicate Site identity"):
        plane.site_uuids_by_local_id(
            [f"site-{index:03d}" for index in range(501)]
        )

    assert session.call_count == 2


def test_site_identity_lookup_rejects_invalid_later_batch() -> None:
    class InvalidSiteIdentitySession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.call_count = 0

        def request(self, _method: str, _url: str, **kwargs: Any) -> FakeResponse:
            self.call_count += 1
            identities: Any = {
                source_node_id: f"uuid-{source_node_id}"
                for source_node_id in kwargs["json"]["source_node_ids"]
            }
            if self.call_count == 2:
                identities = []
            return FakeResponse(
                {
                    "code": 0,
                    "data": {"site_uuids_by_source_node_id": identities},
                }
            )

    plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = InvalidSiteIdentitySession()
    plane._session = session  # type: ignore[assignment]

    with pytest.raises(EdgeProtocolHTTPError, match="invalid identities"):
        plane.site_uuids_by_local_id(
            [f"site-{index:03d}" for index in range(501)]
        )

    assert session.call_count == 2


def test_http_data_plane_injects_w3c_trace_headers(monkeypatch) -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    def inject(carrier: dict[str, Any]) -> dict[str, Any]:
        carrier["traceparent"] = traceparent
        carrier["tracestate"] = "vendor=value"
        return carrier

    monkeypatch.setattr(
        "unilabos.app.edge_control.http.inject_trace_context", inject
    )
    job = StoredJob(
        job_uuid=str(uuid.uuid4()),
        task_uuid=str(uuid.uuid4()),
        node_uuid=str(uuid.uuid4()),
        command_uuid=str(uuid.uuid4()),
        job_access_token="short-token",
        status="received",
        feedback_sequence=0,
    )
    plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = RecordingSession()
    plane._session = session  # type: ignore[assignment]

    plane.fetch_job(job)

    assert session.calls[0]["headers"]["traceparent"] == traceparent
    assert session.calls[0]["headers"]["tracestate"] == "vendor=value"


def test_job_start_fetches_http_payload_and_outcome_precedes_notification(
    tmp_path: Path,
) -> None:
    """验证作业取参、结果持久化和执行未知 ACK 的安全顺序。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。结果携带 UNKNOWN
    设备命令时，Backend 的结果 ACK 不得被解释为物理结算。
    """

    async def scenario() -> None:
        """运行一次含执行未知结果的完整 Edge 作业闭环；参数与返回值为空。"""

        data_plane = FakeDataPlane()
        host_node = FakeHostNode()
        store = EdgeControlStore(str(tmp_path / "runtime.db"))
        client = EdgeControlClient(
            _settings(tmp_path / "runtime.db"),
            store=store,
            data_plane=data_plane,  # type: ignore[arg-type]
            host_node_provider=lambda: host_node,
        )
        client._connected.set()
        # 工作流节点作业、任务、节点与 job.start 下行命令的稳定身份。
        job_uuid = str(uuid.uuid4())
        task_uuid = str(uuid.uuid4())
        node_uuid = str(uuid.uuid4())
        command_uuid = str(uuid.uuid4())
        traceparent = (
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        )
        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": command_uuid,
                "sequence": 11,
                "type": "job.start",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "traceparent": traceparent,
                "tracestate": "vendor=value",
                "payload": {
                    "job_uuid": job_uuid,
                    "task_uuid": task_uuid,
                    "node_uuid": node_uuid,
                    "executor_kind": "device_action",
                    "job_access_token": "short-token",
                    **_claim_fields(),
                },
            }
        )
        if client._tasks:
            await asyncio.gather(*list(client._tasks))

        assert len(data_plane.fetched_jobs) == 1
        assert len(host_node.started) == 1
        context = host_node.started[0]["item"]
        assert context.task_id == task_uuid
        assert context.node_id == node_uuid
        assert host_node.started[0]["action_kwargs"] == {"temperature": 37}
        assert context.trace_context["traceparent"] == traceparent

        client.publish_job_started(context)
        block_reason = f"unresolved_unknown_command:workflow-node-job:{job_uuid}"
        host_node.dispatch_block_reasons["heater-01"] = block_reason
        await client._commit_terminal_status(
            job_uuid,
            "failed",
            {"actual_temperature": 37},
            {"suc": False, "return_value": {"state": "UNKNOWN"}},
            "heater-01",
        )

        assert len(data_plane.outcomes) == 1
        assert data_plane.outcomes[0]["job"].task_uuid == task_uuid
        assert data_plane.outcomes[0]["outcome"] == "failed"
        assert data_plane.outcomes[0]["unknown_command_ids"] == [
            f"workflow-node-job:{job_uuid}"
        ]
        events = store.pending_events(float("inf"))
        assert [event.event_type for event in events] == [
            "command.ack",
            "job.started",
            "job.outcome_committed",
        ]
        assert all(event.traceparent == traceparent for event in events)
        assert all(event.tracestate == "vendor=value" for event in events)
        assert (
            store.get_job(job_uuid).status == "outcome_committed_unknown"  # type: ignore[union-attr]
        )
        assert store.get_pending_outcome(job_uuid) is None
        await client._handle_envelope(
            {
                "type": "event.ack",
                "payload": {"event_uuid": events[-1].event_uuid},
            }
        )
        assert host_node.retired_commands == []
        unresolved_job = store.get_job(job_uuid)
        assert unresolved_job is not None
        assert unresolved_job.status == "outcome_committed_unknown"
        await asyncio.wait_for(
            client._commit_feedback(job_uuid, {"late": True}), timeout=0.2
        )
        store.close()

    asyncio.run(scenario())


def test_acknowledged_settled_outcome_retires_device_journal_and_runtime(
    tmp_path: Path,
) -> None:
    """验证无执行未知事实的结果 ACK 会退役设备账本与作业镜像。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。Backend ACK 证明普通
    工作流节点作业（WorkflowNodeJob）完成物理结算后，根命令和终态运行行
    都应安全删除。
    """

    async def scenario() -> None:
        """运行一次无 UNKNOWN 的结果 ACK 退役场景；参数与返回值为空。"""

        database_path = tmp_path / "settled-runtime.db"
        store = EdgeControlStore(str(database_path))
        host_node = FakeHostNode()
        client = EdgeControlClient(
            _settings(database_path),
            store=store,
            host_node_provider=lambda: host_node,
        )
        # 作业 UUID 定位无 UNKNOWN 的结果事件，事件 UUID 定位本次 ACK。
        job_uuid = str(uuid.uuid4())
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": str(uuid.uuid4()),
                "node_uuid": str(uuid.uuid4()),
                "job_access_token": "temporary-secret",
                **_claim_fields(),
            },
            str(uuid.uuid4()),
        )
        assert store.save_pending_outcome(job_uuid, "succeeded", {}, [])
        outcome_event_uuid = store.complete_pending_outcome(
            job_uuid,
            {"job_uuid": job_uuid},
        )

        await client._handle_envelope(
            {
                "type": "event.ack",
                "payload": {"event_uuid": outcome_event_uuid},
            }
        )

        assert host_node.retired_commands == [f"workflow-node-job:{job_uuid}"]
        assert store.get_job(job_uuid) is None
        store.close()

    asyncio.run(scenario())


def test_outcome_ack_waits_when_device_retirement_adapter_is_unavailable(
    tmp_path: Path,
) -> None:
    """HostNode 未就绪时不得丢失待退役的物理结算事件。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。设备账本
    退役适配器缺失时抛出 ``RuntimeError``，发件箱和作业镜像保留
    以便重连后幂等重试。
    """

    async def scenario() -> None:
        """运行 HostNode 未就绪的结果 ACK 场景；参数和返回值为空。"""

        database_path = tmp_path / "host-unavailable.db"
        store = EdgeControlStore(str(database_path))
        client = EdgeControlClient(
            _settings(database_path),
            store=store,
            host_node_provider=lambda: None,
        )
        # 作业 UUID 定位待退役镜像，结果事件 UUID 必须在失败后保留。
        job_uuid = str(uuid.uuid4())
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": str(uuid.uuid4()),
                "node_uuid": str(uuid.uuid4()),
                "job_access_token": "temporary-secret",
                **_claim_fields(),
            },
            str(uuid.uuid4()),
        )
        assert store.save_pending_outcome(job_uuid, "succeeded", {}, [])
        outcome_event_uuid = store.complete_pending_outcome(
            job_uuid, {"job_uuid": job_uuid}
        )

        with pytest.raises(RuntimeError, match="HostNode is not ready"):
            await client._handle_envelope(
                {
                    "type": "event.ack",
                    "payload": {"event_uuid": outcome_event_uuid},
                }
            )

        assert store.event_for_ack(outcome_event_uuid) is not None
        assert store.get_job(job_uuid) is not None
        store.close()

    asyncio.run(scenario())


def test_terminal_job_token_rejection_retires_pending_outcome(
    tmp_path: Path,
) -> None:
    """Backend 拒绝失效作业 Token 后完整退役边缘执行镜像。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。权威端已明确
    终结的作业不再承担恢复职责，因此不应永久留下运行行。
    """

    class RevokedJobDataPlane(FakeDataPlane):
        """模拟 Backend 已权威撤销作业访问 Token 的数据面。"""

        def commit_outcome(
            self,
            job: StoredJob,
            outcome: str,
            return_info: dict[str, Any],
            error_info: list[dict[str, Any]],
            unknown_command_ids: list[str] | None = None,
        ) -> dict[str, Any]:
            """拒绝结果提交；参数与生产接口一致且总是抛出未授权异常。"""

            self.outcomes.append({"job": job, "outcome": outcome})
            raise EdgeProtocolHTTPError(
                "Job token was revoked after manual reconciliation",
                business_code=BACKEND_UNAUTHORIZED_BUSINESS_CODE,
            )

    async def scenario() -> None:
        """运行失效 Token 结果退役场景；参数和返回值为空。"""

        path = tmp_path / "runtime.db"
        store = EdgeControlStore(str(path))
        # 作业/任务/节点 UUID 定位被终结的执行，命令 UUID 定位原始下发。
        job_uuid = str(uuid.uuid4())
        task_uuid = str(uuid.uuid4())
        node_uuid = str(uuid.uuid4())
        command_uuid = str(uuid.uuid4())
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": task_uuid,
                "node_uuid": node_uuid,
                "job_access_token": "revoked-token",
                **_claim_fields(),
            },
            command_uuid,
        )
        store.save_pending_outcome(job_uuid, "succeeded", {"suc": True}, [])
        data_plane = RevokedJobDataPlane()
        client = EdgeControlClient(
            _settings(path),
            store=store,
            data_plane=data_plane,  # type: ignore[arg-type]
        )

        await asyncio.wait_for(client._commit_pending_outcome(job_uuid), timeout=1)

        assert len(data_plane.outcomes) == 1
        assert store.get_pending_outcome(job_uuid) is None
        assert store.get_job(job_uuid) is None
        store.close()

    asyncio.run(scenario())


def test_controlled_goal_acceptance_is_not_physical_start(tmp_path: Path) -> None:
    """ROS 受理可早于工站开始许可；只有实际 START 才发布 running。"""
    from types import SimpleNamespace

    class ControlledPlane(FakeDataPlane):
        next_start = "WAIT"

        def begin_execution(self, job: StoredJob) -> str:
            return self.next_start

    plane = ControlledPlane()
    store = EdgeControlStore(str(tmp_path / "runtime.db"))
    client = EdgeControlClient(_settings(tmp_path / "runtime.db"), store=store, data_plane=plane)
    job_uuid, command_uuid = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        store.save_job_start({"job_uuid": job_uuid, "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()), "job_access_token": "token", **_claim_fields()}, command_uuid)
        store.set_job_status(job_uuid, "dispatching")
        job = store.get_job(job_uuid)
        item = SimpleNamespace(job_id=job_uuid, start_guard=lambda: client.begin_execution(job))
        client.publish_job_started(item)
        assert store.get_job(job_uuid).status == "dispatching"
        assert item.start_guard() == "WAIT"
        assert store.pending_events(float("inf")) == []
        plane.next_start = "START"
        assert item.start_guard() == "START"
        assert store.get_job(job_uuid).status == "running"
        assert [event.event_type for event in store.pending_events(float("inf"))] == ["job.started"]
        plane.next_start = "ALREADY_STARTED"
        assert item.start_guard() == "ALREADY_STARTED"
        assert len(store.pending_events(float("inf"))) == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    "status",
    [
        "received",
        "fetch_retry",
        "dispatching",
        "running",
        "cancel_requested",
        "outcome_pending",
        "outcome_committed_unknown",
    ],
)
def test_restarted_station_runtime_retires_old_mirrors_without_reconciliation(
    tmp_path: Path, status: str
) -> None:
    """新动作进程不恢复工站旧命令，不产生 UNKNOWN 或待提交回执。"""

    class StationDataPlane(FakeDataPlane):
        def fetch_job(self, job: StoredJob) -> dict[str, Any]:
            return {**super().fetch_job(job), "station_controlled": True}

    path = tmp_path / "runtime.db"
    store = EdgeControlStore(str(path))
    job_uuid, task_uuid, node_uuid, command_uuid = (str(uuid.uuid4()) for _ in range(4))
    try:
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": task_uuid,
                "node_uuid": node_uuid,
                "job_access_token": "old-token",
                **_claim_fields(),
            },
            command_uuid,
        )
        if status == "outcome_pending":
            store.save_pending_outcome(job_uuid, "failed", {}, [], ["old-unknown"])
        else:
            store.set_job_status(job_uuid, status)
        data_plane = StationDataPlane()
        client = EdgeControlClient(_settings(path), store=store, data_plane=data_plane)
        asyncio.run(client._resume_received_jobs())
        asyncio.run(client._resume_pending_outcomes())
        assert store.get_job(job_uuid).status == "outcome_retired"
        assert store.list_pending_outcomes() == []
        assert client._tasks == set()
        assert data_plane.outcomes == []
        assert not store.save_pending_outcome(job_uuid, "succeeded", {}, [])
        event_uuid = store.enqueue_event(
            "job.outcome_committed", {"job_uuid": job_uuid}
        )
        # 默认 Host 未装配；旧 ACK 不得请求设备结算或 UNKNOWN 处置。
        client._retire_settled_device_command(store.event_for_ack(event_uuid))
    finally:
        store.close()


@pytest.mark.parametrize(
    ("status", "execution_start_state", "expected_status"),
    [
        ("running", "START_COMMITTED", "running"),
        ("running", "NOT_STARTED", "received"),
        ("cancel_requested", "START_COMMITTED", "cancel_requested"),
        ("cancel_requested", "NOT_STARTED", "cancel_requested"),
    ],
)
def test_reconcile_restart_preserves_cancel_state_and_start_boundary(
    tmp_path: Path,
    status: str,
    execution_start_state: str,
    expected_status: str,
) -> None:
    """核实模式只重开有持久未启动证据的作业，并保留取消事实。"""

    class ReconcileDataPlane(FakeDataPlane):
        def fetch_job(self, job: StoredJob) -> dict[str, Any]:
            return {
                **super().fetch_job(job),
                "station_controlled": True,
                "recovery_policy": "reconcile",
                "execution_start_state": execution_start_state,
            }

    path = tmp_path / "reconcile-runtime.db"
    store = EdgeControlStore(str(path))
    job_uuid = str(uuid.uuid4())
    try:
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": str(uuid.uuid4()),
                "node_uuid": str(uuid.uuid4()),
                "job_access_token": "reconcile-token",
                **_claim_fields(),
            },
            str(uuid.uuid4()),
        )
        store.set_job_status(job_uuid, status)
        client = EdgeControlClient(
            _settings(path),
            store=store,
            data_plane=ReconcileDataPlane(),  # type: ignore[arg-type]
            host_node_provider=FakeHostNode,
        )

        asyncio.run(client._resume_received_jobs())

        restarted = store.get_job(job_uuid)
        assert restarted is not None
        assert restarted.status == expected_status
        if status == "cancel_requested":
            hello_jobs = client._hello_envelope()["payload"]["running_jobs"]
            assert hello_jobs == [
                {
                    "job_uuid": job_uuid,
                    "command_uuid": restarted.command_uuid,
                    "state": "cancel_requested",
                }
            ]
            # 后续传输失败结果仍必须按已受理取消归一为 canceled。
            assert client._persist_terminal_status(
                job_uuid, "failed", {}, {"message": "late failure"}
            )
            pending = store.get_pending_outcome(job_uuid)
            assert pending is not None
            assert pending.outcome == "canceled"
    finally:
        store.close()


class _DecisionRecordingHostNode(FakeHostNode):
    """记录设备是否真的收到过人工异常决定的 HostNode 替身。"""

    def __init__(self, *, accepts: bool) -> None:
        """``accepts`` 决定设备侧是否仍持有可接收的挂起决定；返回为空。"""

        super().__init__()
        self._accepts = bool(accepts)
        self.decisions: list[dict[str, Any]] = []

        class _Node:
            def __init__(self, owner: "_DecisionRecordingHostNode") -> None:
                self._owner = owner

            def handle_action_error_decision(
                self,
                decision_id: str,
                job_id: str,
                decision: dict[str, Any],
            ) -> bool:
                self._owner.decisions.append(
                    {
                        "decision_id": decision_id,
                        "job_id": job_id,
                        "decision": dict(decision),
                    }
                )
                return self._owner._accepts

        class _Wrapper:
            def __init__(self, node: Any) -> None:
                self._ros_node = node

        self.devices_instances = {"robot-01": _Wrapper(_Node(self))}


def _error_decision_envelope(
    *, command_uuid: str, job_uuid: str, decision_id: str
) -> dict[str, Any]:
    """构造一条 ``job.error_decision`` 下行控制信封。"""

    return {
        "protocol_version": 1,
        "message_uuid": command_uuid,
        "sequence": 11,
        "type": "job.error_decision",
        "sent_at": "2026-09-21T00:00:00.000000Z",
        "payload": {
            "decision_id": decision_id,
            "job_id": job_uuid,
            "device_id": "robot-01",
            "action": "abort",
            "reason": "任务已取消",
        },
    }


def _start_job(store: EdgeControlStore, job_uuid: str) -> None:
    """在 Edge 镜像中登记一个已下发的工作流节点作业。"""

    store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "job_access_token": "temporary-secret",
            **_claim_fields(),
        },
        str(uuid.uuid4()),
    )


def test_error_decision_for_settled_job_retires_without_touching_device(
    tmp_path: Path,
) -> None:
    """已收口作业的人工决定必须就地 ACK，不得反复拖垮生产控制面。

    ``tmp_path`` 提供隔离 Edge SQLite 路径；返回为空。作业已经提交结果后，
    设备侧不可能再持有挂起决定。此时若继续抛错，命令永远拿不到 ACK，
    Backend 每次重连都重投同一条命令，Edge 因此永久无法就绪。
    """

    async def scenario() -> None:
        path = tmp_path / "settled-decision.db"
        store = EdgeControlStore(str(path))
        host_node = _DecisionRecordingHostNode(accepts=False)
        client = EdgeControlClient(
            _settings(path), store=store, host_node_provider=lambda: host_node
        )
        job_uuid = str(uuid.uuid4())
        command_uuid = str(uuid.uuid4())
        _start_job(store, job_uuid)
        assert store.save_pending_outcome(job_uuid, "canceled", {}, [])

        await client._handle_envelope(
            _error_decision_envelope(
                command_uuid=command_uuid,
                job_uuid=job_uuid,
                decision_id=str(uuid.uuid4()),
            )
        )

        # 已收口作业不得再被人工决定触碰，命令必须终结而不是无限重投。
        assert host_node.decisions == []
        assert store.command_status(command_uuid) == "completed"
        assert [event.event_type for event in store.pending_events(float("inf"))] == [
            "command.ack"
        ]
        store.close()

    asyncio.run(scenario())


def test_error_decision_for_absent_job_retires_without_touching_device(
    tmp_path: Path,
) -> None:
    """本 Authority 账本里没有的作业，其人工决定同样必须就地退役。"""

    async def scenario() -> None:
        path = tmp_path / "absent-decision.db"
        store = EdgeControlStore(str(path))
        host_node = _DecisionRecordingHostNode(accepts=False)
        client = EdgeControlClient(
            _settings(path), store=store, host_node_provider=lambda: host_node
        )
        command_uuid = str(uuid.uuid4())

        await client._handle_envelope(
            _error_decision_envelope(
                command_uuid=command_uuid,
                job_uuid=str(uuid.uuid4()),
                decision_id=str(uuid.uuid4()),
            )
        )

        assert host_node.decisions == []
        assert store.command_status(command_uuid) == "completed"
        store.close()

    asyncio.run(scenario())


def test_error_decision_for_live_job_still_reaches_the_device(
    tmp_path: Path,
) -> None:
    """仍在设备边界内等待的作业必须真实收到人工决定。"""

    async def scenario() -> None:
        path = tmp_path / "live-decision.db"
        store = EdgeControlStore(str(path))
        host_node = _DecisionRecordingHostNode(accepts=True)
        client = EdgeControlClient(
            _settings(path), store=store, host_node_provider=lambda: host_node
        )
        job_uuid = str(uuid.uuid4())
        command_uuid = str(uuid.uuid4())
        decision_id = str(uuid.uuid4())
        _start_job(store, job_uuid)
        store.set_job_status(job_uuid, "running")

        await client._handle_envelope(
            _error_decision_envelope(
                command_uuid=command_uuid,
                job_uuid=job_uuid,
                decision_id=decision_id,
            )
        )

        assert [item["job_id"] for item in host_node.decisions] == [job_uuid]
        assert host_node.decisions[0]["decision"]["action"] == "abort"
        assert store.command_status(command_uuid) == "completed"
        store.close()

    asyncio.run(scenario())


def test_error_decision_for_live_job_without_pending_device_state_retries(
    tmp_path: Path,
) -> None:
    """作业仍然活跃但设备暂时无法接收时保持失败关闭，由重连重投。"""

    async def scenario() -> None:
        path = tmp_path / "retry-decision.db"
        store = EdgeControlStore(str(path))
        host_node = _DecisionRecordingHostNode(accepts=False)
        client = EdgeControlClient(
            _settings(path), store=store, host_node_provider=lambda: host_node
        )
        job_uuid = str(uuid.uuid4())
        command_uuid = str(uuid.uuid4())
        _start_job(store, job_uuid)
        store.set_job_status(job_uuid, "running")

        with pytest.raises(RuntimeError):
            await client._handle_envelope(
                _error_decision_envelope(
                    command_uuid=command_uuid,
                    job_uuid=job_uuid,
                    decision_id=str(uuid.uuid4()),
                )
            )

        assert store.command_status(command_uuid) != "completed"
        store.close()

    asyncio.run(scenario())


def test_pending_ros_aliquot_receipts_survive_restart_and_publish(tmp_path):
    """已持久化的 ROS 返回封装重启后仍向标准 outcome 发送分装回执。"""
    import json

    async def scenario():
        path=tmp_path/'aliquot-recovery.db'
        store=EdgeControlStore(str(path)); job_uuid=str(uuid.uuid4())
        store.save_job_start({'job_uuid':job_uuid,'task_uuid':str(uuid.uuid4()),'node_uuid':str(uuid.uuid4()),
            'job_access_token':'fixture-token',**_claim_fields()},str(uuid.uuid4()))
        receipts=[{'target_material_uuid':str(uuid.uuid4()),'actual_quantity':0.5,'quantity_unit':'mL'}]
        returned={'success':True,'material_aliquot_receipts':receipts}
        raw={'result':{'return_info':json.dumps({'suc':True,'error':'','return_value':returned})},'error':''}
        store.save_pending_outcome(job_uuid,'succeeded',raw,[]); store.close()
        store=EdgeControlStore(str(path))
        class Plane(FakeDataPlane):
            def commit_outcome(self,*args,**kwargs):
                client._stopping.set()
                assert kwargs.get('material_aliquot_receipts')==receipts
                return super().commit_outcome(*args)
        plane=Plane();client=EdgeControlClient(_settings(path),store=store,data_plane=plane)
        await client._commit_pending_outcome(job_uuid)
        assert len(plane.outcomes)==1
        assert plane.outcomes[0]['return_info']==raw
        store.close()
    asyncio.run(scenario())


def test_aliquot_receipt_envelopes_reject_conflicts_and_do_not_search_arbitrary_data():
    from unilabos.app.edge_control.client import _aliquot_receipts
    first=[{'target_material_uuid':'target','actual_quantity':0.5,'quantity_unit':'mL'}]
    assert _aliquot_receipts({'return_value':{'material_aliquot_receipts':first}})==first
    assert _aliquot_receipts({'unrelated':{'material_aliquot_receipts':first}})==[]
    with pytest.raises(ValueError,match='冲突'):
        _aliquot_receipts({'material_aliquot_receipts':[], 'return_value':{'material_aliquot_receipts':first}})
    with pytest.raises(ValueError,match='对象数组'):
        _aliquot_receipts({'material_aliquot_receipts':[1]})


def test_restart_keeps_committed_unknown_without_fetching_cleared_token(tmp_path):
    """结果已提交后令牌退役，重连只等待物理处置，不能再次取参。"""
    store = EdgeControlStore(str(tmp_path / 'committed-unknown.db'))
    job_uuid, task_uuid, node_uuid, command_uuid = (str(uuid.uuid4()) for _ in range(4))
    try:
        store.save_job_start({'job_uuid': job_uuid, 'task_uuid': task_uuid,
            'node_uuid': node_uuid, 'job_access_token': 'initial-token', **_claim_fields()}, command_uuid)
        store.save_pending_outcome(job_uuid, 'failed', {}, [], [f'workflow-node-job:{job_uuid}'])
        store.complete_pending_outcome(job_uuid, {'job_uuid': job_uuid})
        assert store.get_job(job_uuid).job_access_token == ''
        plane = FakeDataPlane()
        client = EdgeControlClient(_settings(tmp_path / 'committed-unknown.db'), store=store, data_plane=plane)
        asyncio.run(client._resume_received_jobs())
        assert plane.fetched_jobs == []
        assert store.get_job(job_uuid).status == 'outcome_committed_unknown'
        assert client._tasks == set()
    finally:
        store.close()
