"""设备单动作运行（DeviceActionRun）的 Backend-shaped HTTP 合同测试。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

DEVICE_MATERIAL_UUID = "10000000-0000-4000-8000-000000000001"
DEVICE_RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000001"
OTHER_RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000002"
IDEMPOTENCY_KEY = "device-run-contract-1"


class DeviceActionRegistry:
    """提供一个带第 2 版动作合同（Action Contract v2）的设备注册表替身。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回可投影为 Backend 动作节点模板的完整设备定义。

        返回值包含一个必填整数参数；资源模板业务身份由测试投影解析器映射为
        ``DEVICE_RESOURCE_TEMPLATE_UUID``。
        """

        return [
            {
                "id": "contract-device",
                "source_fqid": "test.devices:contract_device",
                "display_name": "合同测试设备",
                "class": {
                    "module": "test.devices.ContractDevice",
                    "action_value_mappings": {
                        "hold": {
                            "contract_kind": "typed",
                            "displayname": "保持",
                            "description": "保持指定秒数",
                            "type": "UniLabJsonCommand",
                            "goal": {"duration_seconds": "duration_seconds"},
                            "goal_default": {"duration_seconds": 1},
                            "feedback": {},
                            "result": {"completed": "completed"},
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "goal": {
                                        "type": "object",
                                        "properties": {
                                            "duration_seconds": {
                                                "type": "integer",
                                                "minimum": 1,
                                            }
                                        },
                                        "required": ["duration_seconds"],
                                        "additionalProperties": False,
                                    },
                                    "feedback": {
                                        "type": "object",
                                        "properties": {},
                                    },
                                    "result": {
                                        "type": "object",
                                        "properties": {
                                            "completed": {"type": "boolean"}
                                        },
                                        "required": ["completed"],
                                    },
                                },
                                "x-unilabos-action-contract": {
                                    "version": 2,
                                    "input_order": ["duration_seconds"],
                                    "output_order": ["completed"],
                                    "resource_template_symbols": {
                                        "goal": {},
                                        "result": {},
                                    },
                                },
                            },
                        }
                    },
                },
            }
        ]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回空器材模板集合，完成设备注册表快照公共接口。

        返回值为空是因为本测试动作没有引用额外物料资源模板；设备本身的资源
        模板身份由投影解析器提供。
        """

        return []


def _client(
    tmp_path: Any,
    *,
    with_scheduler: bool = False,
) -> tuple[
    TestClient,
    WorkflowStore,
    str,
    RecordingDispatcher | None,
    TaskSchedulerBridge | None,
]:
    """装配带模板投影和设备物料解析器的本地工作流权威。

    参数：``tmp_path`` 是隔离 SQLite 文件的 pytest 临时目录。
    ``with_scheduler`` 决定是否装配唯一公共任务调度桥。返回：HTTP 客户端、工作流
    存储、动作模板 UUID，以及可选派发记录器和需要关闭的公共桥。异常：模板投影
    或工作流组合错误原样传播。
    """

    # ``store`` 同时保存模板、工作流任务（WorkflowTask）和工作流节点作业
    # （WorkflowNodeJob），避免测试绕过公共事务边界读取第二套事实。
    store = WorkflowStore(tmp_path / "workflow_history.db")
    projection = RegistryTemplateProjection(
        store,
        authority_id="local",
        resource_template_identity_resolver=(
            lambda _resource_name: DEVICE_RESOURCE_TEMPLATE_UUID
        ),
    )
    # ``snapshot`` 是已持久发布的动作模板目录代际；模板 UUID 由稳定业务键解析。
    snapshot = projection.refresh(DeviceActionRegistry())
    template_uuid = str(snapshot.actions[0].template["uuid"])

    def resolve_material(material_uuid: str) -> dict[str, Any] | None:
        """按物料 UUID 返回设备物料及其资源模板身份。

        参数：``material_uuid`` 是设备单动作请求中的实际物料稳定身份。
        返回：匹配时返回活动设备物料摘要，否则返回 ``None``。
        """

        if material_uuid != DEVICE_MATERIAL_UUID:
            return None
        return {
            "uuid": DEVICE_MATERIAL_UUID,
            "resource_template_uuid": DEVICE_RESOURCE_TEMPLATE_UUID,
            "meta_data": {"edge_local_id": "contract-device"},
        }

    dispatcher: RecordingDispatcher | None = None
    bridge: TaskSchedulerBridge | None = None
    if with_scheduler:
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher)
        bridge = TaskSchedulerBridge(
            store,
            scheduler=scheduler,
        )
    service = WorkflowService(
        store,
        material_resolver=resolve_material,
        task_scheduler_bridge=bridge,
    )
    return (
        TestClient(create_workflow_app(service)),
        store,
        template_uuid,
        dispatcher,
        bridge,
    )


def _request(template_uuid: str) -> dict[str, Any]:
    """构造与 Backend 完全同名的设备单动作运行请求。

    参数：``template_uuid`` 是已发布动作节点模板身份。
    返回：不包含旧 authority、catalog、device_id 或 input 字段的请求对象。
    """

    return {
        "material_uuid": DEVICE_MATERIAL_UUID,
        "workflow_node_template_uuid": template_uuid,
        "param": {"duration_seconds": 3},
        "execution_policy": {"execution_timeout_seconds": 120},
        "idempotency_key": IDEMPOTENCY_KEY,
        "description": "设备页单动作运行",
        "meta_data": {"source": "contract-test"},
    }


def test_device_action_run_creates_backend_shaped_task_and_job(tmp_path: Any) -> None:
    """首次创建返回 201，并可经标准任务/作业接口恢复同一持久事实。

    参数：``tmp_path`` 是隔离工作流 SQLite 文件的 pytest 临时目录。返回：无；
    断言响应、工作流任务（WorkflowTask）和工作流节点作业（WorkflowNodeJob）的
    Backend 形状及持久恢复结果，HTTP 客户端或存储异常原样传播。
    """

    client, store, template_uuid, _dispatcher, bridge = _client(tmp_path)
    try:
        response = client.post(
            "/api/v1/device-action-runs",
            json=_request(template_uuid),
        )

        assert response.status_code == 201
        assert response.json()["code"] == 0
        # ``result`` 是 Backend 规定的创建结果，不是 OS 私有扁平 Task 视图。
        result = response.json()["data"]
        task = result["task"]
        job = result["job"]
        assert result["created"] is True
        assert task["workflow_uuid"] is None
        assert task["execution_kind"] == "ad_hoc_device_action"
        assert task["run_mode"] == "single_node"
        assert task["status"] == "pending"
        assert job["workflow_task_uuid"] == task["uuid"]
        assert job["material_uuid"] == DEVICE_MATERIAL_UUID
        assert job["executor_kind"] == "device_action"
        assert job["attempt"] == 1
        assert job["param"] == {"duration_seconds": 3}

        restored_task = client.get(f"/api/v1/workflow-tasks/{task['uuid']}").json()[
            "data"
        ]
        restored_job = client.get(f"/api/v1/workflow-node-jobs/{job['uuid']}").json()[
            "data"
        ]
        assert restored_task == task
        assert restored_job == job
    finally:
        client.close()
        if bridge is not None:
            bridge.close()
        store.close()


def test_device_action_run_reuses_idempotency_and_rejects_conflict(
    tmp_path: Any,
) -> None:
    """同请求幂等复用返回 200；同键改义不得创建第二次物理执行责任。"""

    client, store, template_uuid, _dispatcher, bridge = _client(tmp_path)
    try:
        request = _request(template_uuid)
        created = client.post("/api/v1/device-action-runs", json=request)
        repeated = client.post("/api/v1/device-action-runs", json=request)
        conflicting = client.post(
            "/api/v1/device-action-runs",
            json={**request, "param": {"duration_seconds": 4}},
        )

        assert created.status_code == 201
        assert repeated.status_code == 200
        assert repeated.json()["data"]["created"] is False
        assert (
            repeated.json()["data"]["task"]["uuid"]
            == created.json()["data"]["task"]["uuid"]
        )
        assert conflicting.status_code == 200
        assert conflicting.json()["code"] == 3003
        assert store.list_tasks(page=1, page_size=20)["total"] == 1
    finally:
        client.close()
        if bridge is not None:
            bridge.close()
        store.close()


def test_device_action_run_rejects_retired_task_contract_and_mismatch(
    tmp_path: Any,
) -> None:
    """旧 device-action-tasks 字段和不匹配设备物料均应关闭失败。"""

    client, store, template_uuid, _dispatcher, bridge = _client(tmp_path)
    try:
        retired = client.post(
            "/api/v1/device-action-runs",
            json={
                "authority_id": "local",
                "template_catalog_fingerprint": "sha256:" + "a" * 64,
                "workflow_node_template_uuid": template_uuid,
                "device_id": "contract-device",
                "input": {"duration_seconds": 3},
                "idempotency_key": IDEMPOTENCY_KEY,
            },
        )
        mismatch = client.post(
            "/api/v1/device-action-runs",
            json={
                **_request(template_uuid),
                "material_uuid": "10000000-0000-4000-8000-000000000099",
            },
        )

        assert retired.status_code == 200
        assert retired.json()["code"] == 1000
        assert mismatch.status_code == 200
        assert mismatch.json()["code"] == 1000
        assert store.list_tasks(page=1, page_size=20)["total"] == 0
    finally:
        client.close()
        if bridge is not None:
            bridge.close()
        store.close()


def test_device_action_run_api_submits_only_created_job_to_edge_scheduler(
    tmp_path: Any,
) -> None:
    """首次创建自动派发既有 Job；幂等复用不得产生第二次物理执行。"""

    client, store, template_uuid, dispatcher, bridge = _client(
        tmp_path,
        with_scheduler=True,
    )
    assert dispatcher is not None
    assert bridge is not None
    try:
        request = _request(template_uuid)
        created = client.post("/api/v1/device-action-runs", json=request)
        repeated = client.post("/api/v1/device-action-runs", json=request)

        assert created.status_code == 201
        created_data = created.json()["data"]
        assert created_data["task"]["status"] == "running"
        assert created_data["job"]["status"] == "dispatched"
        assert dispatcher.dispatched == [
            {
                "job_id": created_data["job"]["uuid"],
                "task_id": created_data["task"]["uuid"],
                "node_id": created_data["job"]["workflow_node_uuid"],
                "workflow_id": created_data["task"]["uuid"],
                "device_id": "contract-device",
                "action": "hold",
                "action_type": "UniLabJsonCommand",
                "action_args": {"duration_seconds": 3},
                "sample_material": {},
            }
        ]
        assert repeated.status_code == 200
        assert repeated.json()["data"]["created"] is False
        assert len(dispatcher.dispatched) == 1
    finally:
        client.close()
        bridge.close()
        store.close()
