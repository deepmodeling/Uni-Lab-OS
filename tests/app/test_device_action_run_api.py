"""设备单动作运行（DeviceActionRun）的 Backend-shaped HTTP 合同测试。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.registry.template_identity import device_template_uuid
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

DEVICE_MATERIAL_UUID = "10000000-0000-4000-8000-000000000001"
TRANSFER_MATERIAL_UUID = "10000000-0000-4000-8000-000000000011"
TARGET_OWNER_UUID = "10000000-0000-4000-8000-000000000012"
TARGET_SITE_UUID = "10000000-0000-4000-8000-000000000013"
DEVICE_RESOURCE_TEMPLATE_UUID = device_template_uuid("contract-device")
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


class MaterialTransferActionRegistry(DeviceActionRegistry):
    """提供带物料转移执行责任和目标库位合同的动作目录。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回一个与 SZLab 机械臂相同资源语义的最小动作合同。"""

        devices = super().obtain_registry_device_info()
        action = devices[0]["class"]["action_value_mappings"]["hold"]
        action.update(
            {
                "executor_kind": "material_transfer",
                "goal": {
                    "resource": "resource",
                    "target_warehouse": "target_warehouse",
                    "target_site": "target_site",
                },
                "goal_default": {},
            }
        )
        action["schema"] = {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": {
                        "resource": {
                            "type": "object",
                            "x-unilabos-material-lock": True,
                            "properties": {
                                "uuid": {"type": "string", "format": "uuid"}
                            },
                            "required": ["uuid"],
                        },
                        "target_warehouse": {
                            "type": "object",
                            "x-unilabos-material-lock": True,
                            "properties": {
                                "uuid": {"type": "string", "format": "uuid"}
                            },
                            "required": ["uuid"],
                        },
                        "target_site": {"type": "string"},
                    },
                    "required": ["resource", "target_warehouse", "target_site"],
                    "additionalProperties": False,
                },
                "feedback": {"type": "object", "properties": {}},
                "result": {"type": "object", "properties": {}},
            },
            "x-unilabos-action-contract": {
                "version": 2,
                "input_order": ["resource", "target_warehouse", "target_site"],
                "output_order": [],
                "resource_template_symbols": {"goal": {}, "result": {}},
                "resource_contract": {
                    "version": 1,
                    "transfer": {
                        "material_param": "resource",
                        "source_owner_param": "",
                        "source_site_uuid_param": "",
                        "source_site_name_param": "",
                        "target_owner_param": "target_warehouse",
                        "target_site_uuid_param": "",
                        "target_site_name_param": "target_site",
                        "gripper_site_role": "robot.gripper",
                    },
                },
            },
        }
        return devices


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


def test_error_manual_action_runs_on_busy_device_and_keeps_source_trace(tmp_path: Any) -> None:
    """已有动作在途时可手动执行同一设备，必须明确停止后才可完成处置。"""
    client, store, template_uuid, dispatcher, bridge = _client(tmp_path, with_scheduler=True)
    dispatcher.deferred_start = True  # 记录投递但由测试显式确认实际开始。
    try:
        created = client.post("/api/v1/device-action-runs", json=_request(template_uuid)).json()["data"]
        source_job = created["job"]["uuid"]
        from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection
        TaskRuntimeProjection(store).project_trace_context(created["task"]["uuid"], {
            "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"})
        error = bridge.error_control.report_error(source_job, phase="execution", message="测试设备异常")
        base = "/api/v1/stations/local"
        snapshot = client.get(base + "/error-handling").json()["data"]
        entered = client.post(base + f"/errors/{error['decision_id']}/decision", headers={"Idempotency-Key": "enter"}, json={
            "action": "enter_manual_handling", "expected_decision_version": error["decision_version"],
            "expected_station_version": snapshot["station_version"], "observed_error_epoch": snapshot["error_epoch"],
        })
        assert entered.status_code == 202, entered.text
        station = entered.json()["data"]["station"]
        session = station["active_session"]
        actions = client.get(base + f"/sessions/{session['session_id']}/devices/{DEVICE_MATERIAL_UUID}/actions").json()["data"]
        request = {"device_id": DEVICE_MATERIAL_UUID, "action_id": template_uuid,
            "action_version": actions[0]["action_version"], "parameters": {"duration_seconds": 2},
            "expected_station_version": station["station_version"], "expected_session_version": session["version"]}
        manual_url = base + f"/sessions/{session['session_id']}/manual-actions"
        manual = client.post(manual_url, json=request, headers={"Idempotency-Key": "manual"})
        assert manual.status_code == 202, manual.text
        action = manual.json()["data"]["manual_action"]
        fetched = client.get(base + f"/manual-actions/{action['manual_command_id']}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["data"]["manual_command_id"] == action["manual_command_id"]
        command_id = entered.json()["data"]["command_uuid"]
        command = client.get(base + f"/control-commands/{command_id}")
        assert command.status_code == 200, command.text
        assert command.json()["data"]["command_uuid"] == command_id
        assert action["execution_job"]["uuid"] != source_job
        assert action["execution_job"]["execution_source"] == "error_manual"
        assert action["execution_task"]["trace_context"]["trace_id"] == store.get_task(created["task"]["uuid"])["trace_context"]["trace_id"]
        assert len(dispatcher.dispatched) == 2
        assert dispatcher.dispatched[-1]["claim_uuid"] is None
        replay = client.post(manual_url, json=request, headers={"Idempotency-Key": "manual"})
        assert replay.json()["data"]["manual_action"]["manual_command_id"] == action["manual_command_id"]
        assert len(dispatcher.dispatched) == 2
        current = client.get(base + "/error-handling").json()["data"]
        completed = client.post(base + f"/sessions/{session['session_id']}/complete", headers={"Idempotency-Key": "complete"}, json={
            "expected_station_version": current["station_version"], "expected_session_version": session["version"], "summary": "已调整"})
        assert completed.status_code == 409, completed.text
        assert completed.json()["detail"] == "manual_actions_running"
        assert store.get_job(action["execution_job"]["uuid"])["status"] == "dispatched"
        stopped = client.post(base + f"/manual-actions/{action['manual_command_id']}/stop", json={},
                              headers={"Idempotency-Key": "explicit-stop"})
        assert stopped.status_code == 202, stopped.text
        assert bridge.error_control.begin_execution(action["execution_job"]["uuid"], action["manual_command_id"]) == "CANCELED"
        current = client.get(base + "/error-handling").json()["data"]
        completed = client.post(base + f"/sessions/{session['session_id']}/complete", headers={"Idempotency-Key": "complete"}, json={
            "expected_station_version": current["station_version"], "expected_session_version": session["version"], "summary": "已明确停止"})
        assert completed.status_code == 200, completed.text
    finally:
        bridge.close()
        store.close()


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


def _material_transfer_client(
    tmp_path: Any,
) -> tuple[TestClient, WorkflowStore, str, list[dict[str, Any]]]:
    """装配可审计精确库位选择的物料转移单动作入口。"""

    store = WorkflowStore(tmp_path / "material_transfer_history.db")
    projection = RegistryTemplateProjection(
        store,
        authority_id="local",
        resource_template_identity_resolver=(
            lambda _resource_name: DEVICE_RESOURCE_TEMPLATE_UUID
        ),
    )
    snapshot = projection.refresh(MaterialTransferActionRegistry())
    template_uuid = str(snapshot.actions[0].template["uuid"])
    requests: list[dict[str, Any]] = []

    def resolve_material(material_uuid: str) -> dict[str, Any] | None:
        if material_uuid not in {
            DEVICE_MATERIAL_UUID,
            TRANSFER_MATERIAL_UUID,
            TARGET_OWNER_UUID,
        }:
            return None
        return {
            "uuid": material_uuid,
            "resource_template_uuid": DEVICE_RESOURCE_TEMPLATE_UUID,
            "meta_data": (
                {"edge_local_id": "contract-device"}
                if material_uuid == DEVICE_MATERIAL_UUID
                else {}
            ),
        }

    def resolve_site_selection(request: dict[str, Any]) -> dict[str, Any]:
        requests.append(dict(request))
        return {
            "site_uuids": [TARGET_SITE_UUID],
            "fingerprint": "sha256:" + "a" * 64,
        }

    service = WorkflowService(
        store,
        material_resolver=resolve_material,
        site_selection_resolver=resolve_site_selection,
    )
    return (
        TestClient(create_workflow_app(service)),
        store,
        template_uuid,
        requests,
    )


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
        # 读取接口附加现场核对资格，创建结果的所有持久字段仍须完整一致。
        assert restored_task.pop("manual_review_attention_resolved") is False
        assert restored_task == task
        assert restored_job == job
    finally:
        client.close()
        if bridge is not None:
            bridge.close()
        store.close()


def test_material_transfer_device_action_run_freezes_typed_executor_and_site(
    tmp_path: Any,
) -> None:
    """临时转运动作必须保留库存结算类型并冻结目标 Site 身份。"""

    client, store, template_uuid, site_requests = _material_transfer_client(tmp_path)
    try:
        response = client.post(
            "/api/v1/device-action-runs",
            json={
                "material_uuid": DEVICE_MATERIAL_UUID,
                "workflow_node_template_uuid": template_uuid,
                "param": {
                    "resource": {"uuid": TRANSFER_MATERIAL_UUID},
                    "target_warehouse": {"uuid": TARGET_OWNER_UUID},
                    "target_site": TARGET_SITE_UUID,
                },
                "execution_policy": {},
                "idempotency_key": "typed-material-transfer-run",
            },
        )

        assert response.status_code == 201
        created = response.json()["data"]
        assert created["job"]["executor_kind"] == "material_transfer"
        assert created["job"]["param"] == {
            "resource": {"uuid": TRANSFER_MATERIAL_UUID},
            "target_warehouse": {"uuid": TARGET_OWNER_UUID},
        }
        planned_node = created["task"]["execution_plan"]["nodes"][0]
        assert planned_node["kind"] == "material_transfer"
        assert planned_node["action_resource_contract"]["transfer"][
            "target_site_name_param"
        ] == "target_site"
        assert planned_node["execution_policy"]["target_site_group"] == [
            TARGET_SITE_UUID
        ]
        assert planned_node["execution_policy"]["target_site_selection"] == {
            "version": 1,
            "owner_material_uuid": TARGET_OWNER_UUID,
            "group_key": "",
            "requested_reference": TARGET_SITE_UUID,
            "strategy": "sort_order",
            "site_uuids": [TARGET_SITE_UUID],
            "fingerprint": "sha256:" + "a" * 64,
        }
        assert site_requests == [
            {
                "version": 1,
                "owner_material_uuid": TARGET_OWNER_UUID,
                "occupant_material_uuid": TRANSFER_MATERIAL_UUID,
                "group_key": "",
                "exact_site_reference": TARGET_SITE_UUID,
                "strategy": "sort_order",
            }
        ]
    finally:
        client.close()
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
        assert created_data["job"]["status"] == "running"
        assert len(dispatcher.dispatched) == 1
        dispatched = dispatcher.dispatched[0]
        assert dispatched["job_id"] == created_data["job"]["uuid"]
        assert dispatched["task_id"] == created_data["task"]["uuid"]
        assert dispatched["node_id"] == created_data["job"]["workflow_node_uuid"]
        assert dispatched["workflow_id"] == created_data["task"]["uuid"]
        assert dispatched["device_id"] == "contract-device"
        assert dispatched["action"] == "hold"
        assert dispatched["action_type"] == "UniLabJsonCommand"
        assert dispatched["action_args"] == {"duration_seconds": 3}
        assert dispatched["sample_material"] == {}
        assert dispatched["attempt"] == 1
        assert dispatched["command_uuid"]
        assert dispatched["claim_uuid"]
        # RecordingDispatcher 是隔离干跑，不装配库存 Permit；生产物理派发测试另行
        # 断言三项均为非空。这里仅确认新协议字段没有被旧精确载荷断言裁掉。
        assert "effect_uuid" in dispatched
        assert "parameter_hash" in dispatched
        assert "expected_change_set" in dispatched
        assert repeated.status_code == 200
        assert repeated.json()["data"]["created"] is False
        assert len(dispatcher.dispatched) == 1
    finally:
        client.close()
        bridge.close()
        store.close()


def test_manual_command_recovery_and_pending_retry_stop_keep_execution_identity(tmp_path, monkeypatch):
    """提交失败复用原命令；手动 UNKNOWN 的排队重试可停止且不影响旧执行。"""
    from unilabos.app.scheduler.dispatch import CommittedJobOutcome
    client, store, template_uuid, dispatcher, bridge = _client(tmp_path, with_scheduler=True)
    base = "/api/v1/stations/local"
    snapshot = lambda: client.get(base + "/error-handling").json()["data"]
    def decide(error, action, key, **extra):
        current = snapshot()
        response = client.post(base + f"/errors/{error['decision_id']}/decision", headers={"Idempotency-Key": key}, json={
            "action": action, "expected_decision_version": error["decision_version"], "expected_station_version": current["station_version"],
            "observed_error_epoch": current["error_epoch"], **extra})
        assert response.status_code == 202, response.text
        return response.json()["data"]
    try:
        source = client.post("/api/v1/device-action-runs", json=_request(template_uuid)).json()["data"]
        root_error = bridge.error_control.report_error(source["job"]["uuid"], phase="execution", message="源节点错误")
        station = decide(root_error, "enter_manual_handling", "enter")["station"]
        session = station["active_session"]
        actions = client.get(base + f"/sessions/{session['session_id']}/devices/{DEVICE_MATERIAL_UUID}/actions").json()["data"]
        original_submit = bridge.submit
        failures = []
        def transient(task):
            if not failures:
                failures.append(task["uuid"])
                raise RuntimeError("temporary submission failure")
            return original_submit(task)
        monkeypatch.setattr(bridge, "submit", transient)
        accepted = client.post(base + f"/sessions/{session['session_id']}/manual-actions", headers={"Idempotency-Key": "manual"}, json={
            "device_id": DEVICE_MATERIAL_UUID, "action_id": template_uuid, "action_version": actions[0]["action_version"],
            "parameters": {"duration_seconds": 2}, "expected_station_version": station["station_version"], "expected_session_version": session["version"]}).json()["data"]
        assert accepted["dispatch_error"] == "temporary submission failure"
        command_id = accepted["command_uuid"]
        assert next(item for item in snapshot()["control_commands"] if item["command_uuid"] == command_id)["needs_apply"]
        replay = client.post(base + f"/control-commands/{command_id}/apply").json()["data"]
        assert replay["command_uuid"] == command_id
        old = replay["manual_action"]["execution_job"]
        # 旧版允许动作仍运行时关闭会话；保留该历史数据形态验证停止重试。
        with store.transaction() as conn:
            conn.execute("UPDATE workflow_error_session SET stage='RESOLVED' WHERE session_id=?", (session["session_id"],))
            conn.execute("UPDATE workflow_intervention SET stage='RESOLVED' WHERE decision_id=?", (root_error["decision_id"],))
            conn.execute("UPDATE workflow_station_control SET active_session_id=NULL,mode='RUNNING' WHERE station_id='local'")
        bridge._scheduler.on_job_outcome(old["uuid"], CommittedJobOutcome("failed", {}, [], [old["edge_command_uuid"]]))
        manual_error = next(item for item in snapshot()["errors"] if item["source_job_uuid"] == old["uuid"])
        retried = decide(manual_error, "retry_current_node", "retry-manual", resume_requested=False)
        retry_id = retried["result"]["retry_job_uuid"]
        retry = next(item for item in snapshot()["manual_actions"] if item["uuid"] == retry_id)
        assert retry["status"] == "pending"
        assert retry["edge_command_uuid"] and retry["edge_command_uuid"] != old["edge_command_uuid"]
        stopped = client.post(base + f"/manual-actions/{retry['edge_command_uuid']}/stop", json={}, headers={"Idempotency-Key": "stop-retry"})
        assert stopped.status_code == 202, stopped.text
        assert stopped.json()["data"]["status"] == "APPLIED"
        assert store.get_job(retry_id)["status"] == "canceled"
        assert store.get_job(old["uuid"])["status"] == "running"
    finally:
        bridge.close()
        store.close()
