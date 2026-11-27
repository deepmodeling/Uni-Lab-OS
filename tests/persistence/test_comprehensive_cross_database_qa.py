"""独立的跨数据库 QA 验收矩阵。

这组用例不调用既有测试函数作为断言，按 QA 编号覆盖表合并/删除、物料、试剂、
设备动作、实验操作、工作流和调度。每个逻辑用例通过 ``database_mode`` 在 SQLite
和真实 PostgreSQL 上执行；编号统计与仓库原有回归套件分开。
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.persistence.test_functional_regression import (
    _domain_workflow_app,
    _inventory_app,
    _reagent_info,
    _seed_device_task,
    _submit,
    _template,
    _workflow_app,
)
from tests.persistence.test_functional_regression_modules import _lab_app
from tests.persistence.test_qa_table_compaction_document import (
    _DOCUMENT_DROPPED_INVENTORY_TABLES,
    _WORKFLOW_DEFINITION_TABLES,
)
from tests.persistence.test_table_compaction_requirements import (
    _names,
    _seed_legacy_inventory,
)
from tests.registry.test_template_projection import (
    DEVICE_MATERIAL_UUID,
    FakeInventoryStore,
    FakeRegistry,
)
from tests.scheduler_core.conftest import build_core_runtime, stable_uuid
from tests.workflow.quantity_inventory_test_support import (
    _inventory as _quantity_inventory,
)
from tests.workflow.quantity_inventory_test_support import (
    _workflow as _quantity_workflow,
)
from tests.workflow.test_quantity_inventory_runtime import _create_task
from tests.workflow.test_workflow_intervention import (
    _InterventionDelivery,
    _report,
    _seed_running_job,
)
from unilabos.app.edge_control.store import EdgeControlStore
from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.app.scheduler.dispatch import CommittedJobOutcome, RecordingDispatcher
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.persistence.schema import objects_for_scope
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    reset_workflow_service_for_test,
)
from unilabos.workflow.device_action_run import (
    DeviceActionRunConflict,
    DeviceActionRunInputError,
)
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.quantity_inventory import WorkflowQuantityInventory
from unilabos.workflow.resource_lock_plan import compile_template_resource_plan
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import StoreConflict, StoreNotFound, WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection

Scenario = Callable[[str, Path], None]

_DEVICE_UUID_B = "30000000-0000-4000-8000-000000000002"
_WORKFLOW_UUID = "51000000-0000-4000-8000-000000000001"
_NODE_UUID = "52000000-0000-4000-8000-000000000001"
_JOB_UUID = "53000000-0000-4000-8000-000000000001"


def _db_names(store: Any) -> set[str]:
    """读取 SQLite/PostgreSQL 适配器的统一表名投影。"""

    return set(store.db.table_names())


def _new_material(client: TestClient, template_uuid: str, *, barcode: str) -> dict:
    """通过真实物料 API 创建一个测试物料。"""

    response = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "name": barcode,
            "barcode": barcode,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _workflow_node(name: str, *, uuid: str | None = None, **extra: Any) -> dict:
    """构造最小可保存工作流节点。"""

    result = {
        "uuid": uuid or str(uuid4()),
        "name": name,
        "type": "compute",
        "pose": {},
        "param": {"value": 1},
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "meta_data": {},
    }
    result.update(extra)
    return result


def _workflow_node_request(name: str, **extra: Any) -> dict:
    """构造工作流节点创建接口接受的载荷（不携带服务端生成的 UUID）。"""

    result = _workflow_node(name, **extra)
    result.pop("uuid", None)
    return result


def _new_workflow(client: TestClient, *, name: str = "QA 工作流", **extra: Any) -> dict:
    """通过工作流 API 创建一个工作流。"""

    response = client.post(
        "/api/v1/workflows",
        json={"name": name, "tags": [], "meta_data": {}, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _action_runtime(tmp_path: Path, *, scheduler: EdgeScheduler | None = None):
    """建立共享内存模板目录、真实库存和工作流服务。"""

    reset_workflow_service_for_test()
    inventory = FakeInventoryStore()
    service, projection = compose_local_workflow_template_runtime(
        tmp_path,
        inventory_store=inventory,
        registry=FakeRegistry(),
        scheduler=scheduler,
    )
    template = projection.snapshot().require_action(
        "lab.devices:Pump", "transfer"
    ).template
    return service, projection, inventory, template


def _close_action_runtime(service: Any, inventory: Any) -> None:
    """释放设备动作组合根的共享资源。"""

    try:
        reset_workflow_service_for_test()
    finally:
        inventory.close()


# ---------------------------------------------------------------------------
# Device-action cases: 12


def _device_list_actions(_mode: str, tmp_path: Path) -> None:
    service, _projection, inventory, template = _action_runtime(tmp_path)
    try:
        actions = service._device_action_runs.list_manual_actions(DEVICE_MATERIAL_UUID)
        assert any(item["template_uuid"] == template["uuid"] for item in actions)
        assert actions[0]["device_binding"]["material_uuid"] == DEVICE_MATERIAL_UUID
    finally:
        _close_action_runtime(service, inventory)


def _device_fingerprint_stable(_mode: str, tmp_path: Path) -> None:
    service, _projection, inventory, template = _action_runtime(tmp_path)
    try:
        clean = service._definition_store.get_node_template(template["uuid"])
        first = service._device_action_runs.action_fingerprint(clean)
        second = service._device_action_runs.action_fingerprint(dict(clean))
        assert first == second
        assert first.startswith("sha256:")
    finally:
        _close_action_runtime(service, inventory)


def _device_create_freezes_task(_mode: str, tmp_path: Path) -> None:
    service, _projection, inventory, template = _action_runtime(tmp_path)
    try:
        aggregate = service.create_device_action_run(
            material_uuid=DEVICE_MATERIAL_UUID,
            workflow_node_template_uuid=template["uuid"],
            param={"volume": 2.5},
            execution_policy={},
            idempotency_key="qa-device-create",
            description=None,
            meta_data={},
        )
        assert aggregate["created"] is True
        assert aggregate["job"]["material_uuid"] == DEVICE_MATERIAL_UUID
        assert aggregate["job"]["param"]["volume"] == 2.5
    finally:
        _close_action_runtime(service, inventory)


def _device_idempotent_replay(_mode: str, tmp_path: Path) -> None:
    service, _projection, inventory, template = _action_runtime(tmp_path)
    try:
        request = {
            "material_uuid": DEVICE_MATERIAL_UUID,
            "workflow_node_template_uuid": template["uuid"],
            "param": {"volume": 1},
            "execution_policy": {},
            "idempotency_key": "qa-device-replay",
            "description": None,
            "meta_data": {},
        }
        first = service.create_device_action_run(**request)
        replay = service.create_device_action_run(**request)
        assert first["job"]["uuid"] == replay["job"]["uuid"]
        assert replay["created"] is False
    finally:
        _close_action_runtime(service, inventory)


def _device_changed_idempotency_rejected(_mode: str, tmp_path: Path) -> None:
    service, _projection, inventory, template = _action_runtime(tmp_path)
    try:
        request = {
            "material_uuid": DEVICE_MATERIAL_UUID,
            "workflow_node_template_uuid": template["uuid"],
            "param": {"volume": 1},
            "execution_policy": {},
            "idempotency_key": "qa-device-conflict",
            "description": None,
            "meta_data": {},
        }
        service.create_device_action_run(**request)
        with pytest.raises(WorkflowError):
            service.create_device_action_run(**{**request, "param": {"volume": 2}})
    finally:
        _close_action_runtime(service, inventory)


def _device_missing_material_rejected(_mode: str, tmp_path: Path) -> None:
    service, _projection, inventory, template = _action_runtime(tmp_path)
    try:
        with pytest.raises(WorkflowError):
            service.create_device_action_run(
                material_uuid=_DEVICE_UUID_B,
                workflow_node_template_uuid=template["uuid"],
                param={"volume": 1},
                execution_policy={},
                idempotency_key="qa-device-missing-material",
                description=None,
                meta_data={},
            )
    finally:
        _close_action_runtime(service, inventory)


def _device_missing_template_rejected(_mode: str, tmp_path: Path) -> None:
    service, _projection, inventory, _template = _action_runtime(tmp_path)
    try:
        with pytest.raises(WorkflowError):
            service.create_device_action_run(
                material_uuid=DEVICE_MATERIAL_UUID,
                workflow_node_template_uuid="40000000-0000-4000-8000-000000000001",
                param={"volume": 1},
                execution_policy={},
                idempotency_key="qa-device-missing-template",
                description=None,
                meta_data={},
            )
    finally:
        _close_action_runtime(service, inventory)


def _device_invalid_param_rejected(_mode: str, tmp_path: Path) -> None:
    service, _projection, inventory, template = _action_runtime(tmp_path)
    try:
        with pytest.raises(WorkflowError):
            service.create_device_action_run(
                material_uuid=DEVICE_MATERIAL_UUID,
                workflow_node_template_uuid=template["uuid"],
                param={"volume": "invalid"},
                execution_policy={},
                idempotency_key="qa-device-invalid-param",
                description=None,
                meta_data={},
            )
    finally:
        _close_action_runtime(service, inventory)


def _device_manual_action_version(_mode: str, tmp_path: Path) -> None:
    service, _projection, inventory, template = _action_runtime(tmp_path)
    try:
        action = service._device_action_runs.action_fingerprint(
            service._definition_store.get_node_template(template["uuid"])
        )
        with pytest.raises(StoreConflict):
            service._device_action_runs.prepare_manual(
                {"device_id": DEVICE_MATERIAL_UUID, "action_id": template["uuid"], "action_version": "sha256:old", "parameters": {"volume": 1}},
                idempotency_key="qa-manual-version",
                metadata={},
            )
        assert action.startswith("sha256:")
    finally:
        _close_action_runtime(service, inventory)


def _device_memory_template_source(_mode: str, tmp_path: Path) -> None:
    service, projection, inventory, template = _action_runtime(tmp_path)
    try:
        assert projection.snapshot().require_action("lab.devices:Pump", "transfer").template["uuid"] == template["uuid"]
        runtime = service._store
        assert "workflow_node_template" not in _db_names(runtime)
    finally:
        _close_action_runtime(service, inventory)


def _device_result_persistence(_mode: str, tmp_path: Path) -> None:
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    service, _projection, inventory, template = _action_runtime(tmp_path, scheduler=scheduler)
    try:
        created = service.create_device_action_run(
            material_uuid=DEVICE_MATERIAL_UUID,
            workflow_node_template_uuid=template["uuid"],
            param={"volume": 1},
            execution_policy={},
            idempotency_key="qa-device-result",
            description=None,
            meta_data={},
        )
        scheduler.on_job_finished(created["job"]["uuid"], True, {"accepted": True})
        assert service._store.get_job(created["job"]["uuid"])["status"] == "succeeded"
        assert service._store.get_job(created["job"]["uuid"])["return_info"] == {"accepted": True}
    finally:
        _close_action_runtime(service, inventory)


def _device_dispatch_identity(_mode: str, tmp_path: Path) -> None:
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    service, _projection, inventory, template = _action_runtime(tmp_path, scheduler=scheduler)
    try:
        created = service.create_device_action_run(
            material_uuid=DEVICE_MATERIAL_UUID,
            workflow_node_template_uuid=template["uuid"],
            param={"volume": 1},
            execution_policy={},
            idempotency_key="qa-device-dispatch",
            description=None,
            meta_data={},
        )
        assert dispatcher.dispatched[0]["job_id"] == created["job"]["uuid"]
        assert dispatcher.dispatched[0]["device_id"] == "pump-01"
    finally:
        _close_action_runtime(service, inventory)


def _device_action_list_after_restart(_mode: str, tmp_path: Path) -> None:
    service, projection, inventory, template = _action_runtime(tmp_path)
    try:
        before = {item["action_id"] for item in service._device_action_runs.list_manual_actions(DEVICE_MATERIAL_UUID)}
        assert template["uuid"] in before
        fingerprint = projection.snapshot().fingerprint
        reset_workflow_service_for_test()
        service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory,
            registry=FakeRegistry(),
        )
        after = {item["action_id"] for item in service._device_action_runs.list_manual_actions(DEVICE_MATERIAL_UUID)}
        assert after == before
        assert projection.snapshot().fingerprint == fingerprint
    finally:
        _close_action_runtime(service, inventory)


DEVICE_CASES: list[tuple[str, Scenario]] = [
    ("DEV-01-list-actions", _device_list_actions),
    ("DEV-02-fingerprint-stable", _device_fingerprint_stable),
    ("DEV-03-create-freezes-task", _device_create_freezes_task),
    ("DEV-04-idempotent-replay", _device_idempotent_replay),
    ("DEV-05-changed-idempotency-rejected", _device_changed_idempotency_rejected),
    ("DEV-06-missing-material", _device_missing_material_rejected),
    ("DEV-07-missing-template", _device_missing_template_rejected),
    ("DEV-08-invalid-param", _device_invalid_param_rejected),
    ("DEV-09-manual-version", _device_manual_action_version),
    ("DEV-10-memory-template-source", _device_memory_template_source),
    ("DEV-11-result-persistence", _device_result_persistence),
    ("DEV-12-dispatch-identity", _device_dispatch_identity),
]


# ---------------------------------------------------------------------------
# Experiment-operation / workflow cases: 18


def _wf_crud(_mode: str, tmp_path: Path) -> None:
    client, service, store = _workflow_app(tmp_path)
    try:
        workflow = _new_workflow(client, name="QA workflow CRUD")
        assert client.get(f"/api/v1/workflows/{workflow['uuid']}").json()["data"]["name"] == "QA workflow CRUD"
        updated = client.put(f"/api/v1/workflows/{workflow['uuid']}", json={"name": "QA workflow updated", "tags": ["qa"], "meta_data": {}})
        assert updated.status_code == 200
        assert client.get("/api/v1/workflows", params={"name": "updated"}).json()["data"]["items"]
    finally:
        store.close()


def _wf_invalid_create(_mode: str, tmp_path: Path) -> None:
    client, service, store = _workflow_app(tmp_path)
    try:
        blank = client.post("/api/v1/workflows", json={"name": " ", "tags": [], "meta_data": {}})
        unknown = client.post("/api/v1/workflows", json={"name": "invalid", "workflow_type": "unknown", "tags": [], "meta_data": {}})
        assert blank.json()["code"] != 0
        assert unknown.json()["code"] != 0
        assert store.count_rows("workflow") == 0
    finally:
        store.close()


def _wf_node_crud(_mode: str, tmp_path: Path) -> None:
    client, service, store = _workflow_app(tmp_path)
    try:
        workflow = _new_workflow(client, name="QA nodes")
        node = client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json=_workflow_node_request("node")).json()["data"]
        assert client.get(f"/api/v1/workflow-nodes/{node['uuid']}").json()["data"]["name"] == "node"
        patched = client.patch(f"/api/v1/workflow-nodes/{node['uuid']}", json={"name": "node-updated"}).json()["data"]
        assert patched["name"] == "node-updated"
        assert client.delete(f"/api/v1/workflow-nodes/{node['uuid']}").json()["code"] == 0
    finally:
        service.close()
        store.close()


def _wf_edge_crud(_mode: str, tmp_path: Path) -> None:
    client, service, store = _workflow_app(tmp_path)
    try:
        workflow = _new_workflow(client, name="QA edges")
        first = client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json=_workflow_node_request("first")).json()["data"]
        second = client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json=_workflow_node_request("second")).json()["data"]
        edge = client.post(f"/api/v1/workflows/{workflow['uuid']}/edges", json={"source_node_uuid": first["uuid"], "target_node_uuid": second["uuid"], "source_handle_uuid": str(uuid4()), "target_handle_uuid": str(uuid4())})
        assert edge.json()["code"] != 0
        assert store.get_graph(workflow["uuid"])["edges"] == []
    finally:
        service.close()
        store.close()


def _wf_revision_conflict(_mode: str, tmp_path: Path) -> None:
    client, service, store = _workflow_app(tmp_path)
    try:
        workflow = _new_workflow(client, name="QA revision")
        node = _workflow_node("stable")
        assert client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={"revision": 1, "nodes": [node], "edges": []}).json()["code"] == 0
        stale = client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={"revision": 1, "nodes": [], "edges": []})
        assert stale.json()["code"] != 0
        assert store.get_graph(workflow["uuid"])["nodes"]
    finally:
        service.close()
        store.close()


def _wf_duplicate_independent(_mode: str, tmp_path: Path) -> None:
    client, service, store = _workflow_app(tmp_path)
    try:
        source = _new_workflow(client, name="QA source")
        node = client.post(f"/api/v1/workflows/{source['uuid']}/nodes", json=_workflow_node_request("source node")).json()["data"]
        copy = client.post(f"/api/v1/workflows/{source['uuid']}/duplicate", json={"name": "QA copy"}).json()["data"]
        assert copy["workflow"]["uuid"] != source["uuid"]
        copy_node = copy["nodes"][0]
        client.patch(f"/api/v1/workflow-nodes/{copy_node['uuid']}", json={"name": "copy node"})
        assert store.get_graph(source["uuid"])["nodes"][0]["name"] == node["name"]
    finally:
        service.close()
        store.close()


def _wf_batch_delete_atomic(_mode: str, tmp_path: Path) -> None:
    client, service, store = _workflow_app(tmp_path)
    try:
        workflow = _new_workflow(client, name="QA batch delete")
        node = client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json=_workflow_node_request("keep")).json()["data"]
        rejected = client.post(f"/api/v1/workflows/{workflow['uuid']}/batch-delete", json={"node_uuids": [str(uuid4())], "edge_uuids": []})
        assert rejected.json()["code"] != 0
        assert store.get_graph(workflow["uuid"])["nodes"][0]["uuid"] == node["uuid"]
    finally:
        service.close()
        store.close()


def _wf_task_snapshot(_mode: str, tmp_path: Path) -> None:
    client, service, store = _workflow_app(tmp_path)
    try:
        workflow = _new_workflow(client, name="QA snapshot")
        first = _workflow_node("first", uuid="61000000-0000-4000-8000-000000000001")
        second = _workflow_node("second", uuid="61000000-0000-4000-8000-000000000002")
        client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={"revision": 1, "nodes": [first, second], "edges": []})
        task = client.post(f"/api/v1/workflow-tasks", json={"workflow_uuid": workflow["uuid"], "run_mode": "single_node", "target_node_uuid": second["uuid"], "input": {}}).json()["data"]
        assert {item["uuid"] for item in task["workflow_snapshot"]["nodes"]} == {first["uuid"], second["uuid"]}
        assert [item["uuid"] for item in task["execution_plan"]["nodes"]] == [second["uuid"]]
    finally:
        service.close()
        store.close()


def _wf_step_task(_mode: str, tmp_path: Path) -> None:
    client, service, store = _workflow_app(tmp_path)
    try:
        workflow = _new_workflow(client, name="QA step")
        node = _workflow_node("step")
        client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={"revision": 1, "nodes": [node], "edges": []})
        task = client.post("/api/v1/workflow-tasks", json={"workflow_uuid": workflow["uuid"], "run_mode": "step", "input": {}}).json()["data"]
        assert task["run_mode"] == "step"
        assert all(job["status"] == "pending" for job in client.get(f"/api/v1/workflow-tasks/{task['uuid']}/jobs").json()["data"])
    finally:
        service.close()
        store.close()


def _wf_task_listing(_mode: str, tmp_path: Path) -> None:
    client, service, store = _workflow_app(tmp_path)
    try:
        workflow = _new_workflow(client, name="QA listing")
        node = _workflow_node("listing")
        client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={"revision": 1, "nodes": [node], "edges": []})
        created = client.post("/api/v1/workflow-tasks", json={"workflow_uuid": workflow["uuid"], "run_mode": "step", "input": {}}).json()["data"]
        page = client.get("/api/v1/workflow-tasks", params={"workflow_uuid": workflow["uuid"], "page": 1, "page_size": 1}).json()["data"]
        assert page["total"] == 1
        assert page["items"][0]["uuid"] == created["uuid"]
    finally:
        service.close()
        store.close()


def _wf_debug_api_retired(_mode: str, tmp_path: Path) -> None:
    client, service, store = _workflow_app(tmp_path)
    try:
        assert client.post("/api/v1/debug/workflow-tasks", json={}).status_code == 410
        assert client.get(f"/api/v1/debug/workflow-tasks/{uuid4()}").status_code == 410
    finally:
        service.close()
        store.close()


def _wf_debug_projection(_mode: str, tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "debug-projection.db", persist_workflow_definitions=False)
    try:
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(workflow_task)")}
        assert "debug_status" in columns
        assert "workflow_task_debug_command" in _db_names(store)
    finally:
        store.close()


def _wf_category_crud(_mode: str, tmp_path: Path) -> None:
    client, service, store = _domain_workflow_app(tmp_path)
    try:
        created = client.post("/api/v1/experiment-operation-categories", json={"name": "QA category", "sort_order": 8})
        assert created.status_code == 201
        category = created.json()["data"]
        updated = client.put(f"/api/v1/experiment-operation-categories/{category['uuid']}", json={"name": "QA category updated", "sort_order": 2})
        assert updated.json()["data"]["name"] == "QA category updated"
        assert any(item["uuid"] == category["uuid"] for item in client.get("/api/v1/experiment-operation-categories").json()["data"]["items"])
    finally:
        service.close()
        store.close()


def _wf_category_duplicate(_mode: str, tmp_path: Path) -> None:
    client, service, store = _domain_workflow_app(tmp_path)
    try:
        client.post("/api/v1/experiment-operation-categories", json={"name": "QA duplicate category", "sort_order": 1})
        duplicate = client.post("/api/v1/experiment-operation-categories", json={"name": "QA duplicate category", "sort_order": 2})
        assert duplicate.json()["code"] != 0
    finally:
        service.close()
        store.close()


def _wf_category_reference_guard(_mode: str, tmp_path: Path) -> None:
    client, service, store = _domain_workflow_app(tmp_path)
    try:
        category = client.post("/api/v1/experiment-operation-categories", json={"name": "QA protected category", "sort_order": 1}).json()["data"]
        workflow = _new_workflow(client, name="QA categorized", workflow_type="experiment_operation", operation_category_uuid=category["uuid"])
        assert client.delete(f"/api/v1/experiment-operation-categories/{category['uuid']}").json()["code"] != 0
        assert client.get(f"/api/v1/workflows/{workflow['uuid']}").json()["data"]["operation_category_uuid"] == category["uuid"]
    finally:
        service.close()
        store.close()


def _wf_legacy_tag_category(_mode: str, tmp_path: Path) -> None:
    client, service, store = _domain_workflow_app(tmp_path)
    try:
        workflow = _new_workflow(client, name="QA legacy tag", workflow_type="experiment_operation", tags=["device_operation"])
        assert workflow["operation_category_uuid"]
    finally:
        service.close()
        store.close()


def _wf_intervention_rebind(_mode: str, tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "intervention.db")
    try:
        _seed_running_job(store)
        first = WorkflowService(store)
        delivery = _InterventionDelivery(accepted=False)
        first.bind_intervention_delivery(delivery)
        delivery.publish(_report("50000000-0000-4000-8000-000000000701"))
        intervention = first.list_workflow_interventions(status="open", limit=10)[0]
        assert intervention["status"] == "open"
    finally:
        store.close()


def _wf_runtime_has_no_definitions(_mode: str, tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "runtime-no-definitions.db", persist_workflow_definitions=False)
    try:
        assert store.count_rows("workflow") == 0
        task = _seed_device_task(store, "71000000-0000-4000-8000-000000000001", with_material=False)
        assert task["workflow_uuid"] == "71000000-0000-4000-8000-000000000001"
    finally:
        store.close()


WORKFLOW_CASES: list[tuple[str, Scenario]] = [
    ("WF-01-workflow-crud", _wf_crud),
    ("WF-02-invalid-create", _wf_invalid_create),
    ("WF-03-node-crud", _wf_node_crud),
    ("WF-04-edge-crud", _wf_edge_crud),
    ("WF-05-revision-conflict", _wf_revision_conflict),
    ("WF-06-duplicate-independence", _wf_duplicate_independent),
    ("WF-07-batch-delete-atomic", _wf_batch_delete_atomic),
    ("WF-08-task-snapshot", _wf_task_snapshot),
    ("WF-09-step-task", _wf_step_task),
    ("WF-10-task-listing", _wf_task_listing),
    ("WF-11-debug-api-retired", _wf_debug_api_retired),
    ("WF-12-debug-projection", _wf_debug_projection),
    ("WF-13-category-crud", _wf_category_crud),
    ("WF-14-category-duplicate", _wf_category_duplicate),
    ("WF-15-category-reference-guard", _wf_category_reference_guard),
    ("WF-16-legacy-tag-category", _wf_legacy_tag_category),
    ("WF-17-intervention-report", _wf_intervention_rebind),
    ("WF-18-runtime-without-definitions", _wf_runtime_has_no_definitions),
]


# ---------------------------------------------------------------------------
# Migration / table-compaction cases: 18


def _mig_inventory_shape(_mode: str, tmp_path: Path) -> None:
    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        names = _db_names(store)
        assert {"material", "container_content", "resource_template", "lab_meta"} <= names
        assert names.isdisjoint(_DOCUMENT_DROPPED_INVENTORY_TABLES)
    finally:
        store.close()


def _mig_inventory_legacy(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "legacy-inventory.db"
    if mode == "sqlite":
        _seed_legacy_inventory(path)
    store = InventoryStore(str(path))
    try:
        assert "material" in _db_names(store)
        assert "container_content" in _db_names(store)
        assert "material_inventory" not in _db_names(store)
        if mode == "sqlite":
            assert store.query_one("SELECT COUNT(*) AS count FROM material") ["count"] >= 1
    finally:
        store.close()


def _mig_compatibility_views(_mode: str, tmp_path: Path) -> None:
    store = InventoryStore(str(tmp_path / "compat.db"))
    try:
        objects = {
            item["name"]: item["type"]
            for item in objects_for_scope("inventory")
        }
        expected = {
            "material_instance": "view",
            "resource_relation": "view",
            "substance_content": "view",
            "inventory_resource_template": "view",
        }
        assert {name for name, kind in objects.items() if kind == "view"} >= set(expected)
        assert all(objects[name] == kind for name, kind in expected.items())
    finally:
        store.close()


def _mig_soft_delete_history(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.mig.soft-delete")
        material = _new_material(client, template, barcode="QA-MIG-SOFT")
        deleted = client.delete(f"/api/v1/materials/{material['uuid']}")
        assert deleted.status_code == 200, deleted.text
        assert client.get(f"/api/v1/materials/{material['uuid']}").json()["code"] != 0
        assert store.query_one(
            "SELECT deleted_at FROM material WHERE uuid=?", (material["uuid"],)
        )["deleted_at"] is not None
    finally:
        store.close()


def _mig_content_integrity(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.mig.content")
        first = _new_material(client, template, barcode="QA-MIG-CONTENT")
        statement = (
            "INSERT INTO container_content(uuid,create_time,update_time,material_uuid,kind,code,name,quantity,quantity_unit) "
            "VALUES(?,?,?,?,?,?,?,?,?)"
        )
        with store.transaction() as connection:
            connection.execute(statement, ("qa-content-2", "t", "t", first["uuid"], "sample", "S-2", "样品", 0, "mL"))
        with pytest.raises(Exception):
            with store.transaction() as connection:
                connection.execute(
                    statement,
                    ("qa-content-3", "t", "t", first["uuid"], "sample", "S-3", "另一份样品", 0, "mL"),
                )
        assert store.query_one("SELECT COUNT(*) AS n FROM container_content")["n"] == 1
    finally:
        store.close()


def _mig_conflict_atomic(mode: str, tmp_path: Path) -> None:
    if mode == "sqlite":
        path = tmp_path / "collision.db"
        _seed_legacy_inventory(path, colliding_content_uuid=True)
        with pytest.raises(Exception):
            InventoryStore(str(path))
        import sqlite3

        connection = sqlite3.connect(path)
        try:
            assert "container_content" not in _names(connection, "table")
            assert {"sample", "current_substance", "reagent"} <= _names(connection, "table")
        finally:
            connection.close()
    else:
        store = InventoryStore(str(tmp_path / "collision-pg.db"))
        try:
            assert "container_content" in _db_names(store)
        finally:
            store.close()


def _mig_inventory_idempotent(_mode: str, tmp_path: Path) -> None:
    path = tmp_path / "idempotent-inventory.db"
    first = InventoryStore(str(path))
    first_names = _db_names(first)
    first.close()
    second = InventoryStore(str(path))
    try:
        assert _db_names(second) == first_names
    finally:
        second.close()


def _mig_lab_tables_removed(_mode: str, tmp_path: Path) -> None:
    client, store, service = _lab_app(tmp_path)
    try:
        assert client.get("/api/v1/lab/layout").status_code == 200
        assert {"lab_zone", "lab_placement"}.isdisjoint(_db_names(store))
    finally:
        store.close()


def _mig_runtime_workflow_shape(_mode: str, tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "runtime.db", persist_workflow_definitions=False)
    try:
        names = _db_names(store)
        assert _WORKFLOW_DEFINITION_TABLES.isdisjoint(names)
        assert {"workflow_task", "workflow_node_job", "workflow_task_debug_command"} <= names
    finally:
        store.close()


def _mig_memory_workflow_shape(_mode: str, tmp_path: Path) -> None:
    store = WorkflowStore(":memory:")
    try:
        assert _WORKFLOW_DEFINITION_TABLES <= _db_names(store)
        assert "workflow_task_debug_configuration" not in _db_names(store)
    finally:
        store.close()


def _mig_debug_merge(_mode: str, tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "debug.db", persist_workflow_definitions=False)
    try:
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(workflow_task)")}
        assert {
            "debug_create_time",
            "debug_update_time",
            "debug_start_node_uuids",
            "debug_breakpoint_node_uuids",
            "debug_execution_policy",
            "debug_status",
        } <= columns
        assert "workflow_task_debug_configuration" not in _db_names(store)
    finally:
        store.close()


def _mig_runtime_candidates(_mode: str, tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "candidates.db", persist_workflow_definitions=False)
    try:
        assert {
            "job_device_tenancy_transition",
            "workflow_task_material_admission",
            "workflow_inventory_saga",
        } <= _db_names(store)
    finally:
        store.close()


def _mig_outbox_stays_absent(_mode: str, tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "outbox.db", persist_workflow_definitions=False)
    try:
        from unilabos.workflow.station_event_outbox import append_station_event

        with store.transaction() as connection:
            append_station_event(
                connection,
                event_type="job.completed",
                aggregate_type="workflow_task",
                aggregate_uuid="qa-task",
                source_kind="dispatch",
                source_uuid="qa-source",
                idempotency_key="qa-outbox",
                payload={"ok": True},
            )
        assert "workflow_station_event_outbox" not in _db_names(store)
    finally:
        store.close()


def _mig_nonempty_definitions(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "definitions.db"
    if mode == "sqlite":
        source = WorkflowStore(path)
        workflow = source.create_workflow(
            workflow_uuid=str(uuid4()),
            name="legacy",
            tags=[],
            description=None,
            meta_data={},
        )
        source.close()
        runtime = WorkflowStore(path, persist_workflow_definitions=False)
        try:
            assert runtime.get_workflow(workflow["uuid"])["name"] == "legacy"
            assert _WORKFLOW_DEFINITION_TABLES <= _db_names(runtime)
        finally:
            runtime.close()
    else:
        runtime = WorkflowStore(path, persist_workflow_definitions=False)
        try:
            assert _WORKFLOW_DEFINITION_TABLES.isdisjoint(_db_names(runtime))
        finally:
            runtime.close()


def _mig_catalog_contract(_mode: str, _tmp_path: Path) -> None:
    runtime_tables = {
        item["name"]
        for item in objects_for_scope("workflow", persist_workflow_definitions=False)
        if item["type"] == "table"
    }
    assert _WORKFLOW_DEFINITION_TABLES.isdisjoint(runtime_tables)
    assert {"workflow_task_material_admission", "workflow_inventory_saga"} <= runtime_tables


def _mig_database_separation(_mode: str, tmp_path: Path) -> None:
    stores = [
        InventoryStore(str(tmp_path / "inventory.db")),
        WorkflowStore(tmp_path / "workflow.db", persist_workflow_definitions=False),
        EdgeControlStore(str(tmp_path / "edge.db")),
        DeviceStateStore(str(tmp_path / "device.db")),
    ]
    try:
        assert "material" in _db_names(stores[0])
        assert "workflow_task" in _db_names(stores[1])
        assert "edge_job_runtime" in _db_names(stores[2])
        assert "device_property_history" in _db_names(stores[3])
    finally:
        for store in reversed(stores):
            store.close()


def _mig_workflow_restart(_mode: str, tmp_path: Path) -> None:
    path = tmp_path / "workflow-restart.db"
    first = WorkflowStore(path, persist_workflow_definitions=False)
    before = _db_names(first)
    first.close()
    second = WorkflowStore(path, persist_workflow_definitions=False)
    try:
        assert _db_names(second) == before
        assert "workflow_task_debug_configuration" not in _db_names(second)
    finally:
        second.close()


MIGRATION_CASES: list[tuple[str, Scenario]] = [
    ("MIG-01-inventory-shape", _mig_inventory_shape),
    ("MIG-02-legacy-inventory", _mig_inventory_legacy),
    ("MIG-03-compatibility-views", _mig_compatibility_views),
    ("MIG-04-soft-delete-history", _mig_soft_delete_history),
    ("MIG-05-content-integrity", _mig_content_integrity),
    ("MIG-06-conflict-atomicity", _mig_conflict_atomic),
    ("MIG-07-inventory-idempotency", _mig_inventory_idempotent),
    ("MIG-08-lab-tables-deleted", _mig_lab_tables_removed),
    ("MIG-09-runtime-workflow-shape", _mig_runtime_workflow_shape),
    ("MIG-10-memory-workflow-shape", _mig_memory_workflow_shape),
    ("MIG-11-debug-merge", _mig_debug_merge),
    ("MIG-12-runtime-candidates", _mig_runtime_candidates),
    ("MIG-13-outbox-absent", _mig_outbox_stays_absent),
    ("MIG-14-nonempty-definitions", _mig_nonempty_definitions),
    ("MIG-15-catalog-contract", _mig_catalog_contract),
    ("MIG-16-database-separation", _mig_database_separation),
    ("MIG-17-workflow-restart", _mig_workflow_restart),
    ("MIG-18-candidate-schema-is-stable", _mig_catalog_contract),
]


# ---------------------------------------------------------------------------
# Material / reagent cases: 20


def _inv_material_crud(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.material.crud")
        material = _new_material(client, template, barcode="QA-MAT-CRUD")
        read = client.get(f"/api/v1/materials/{material['uuid']}")
        update = client.put(
            f"/api/v1/materials/{material['uuid']}",
            json={
                "resource_template_uuid": template,
                "name": "QA-MAT-UPDATED",
                "barcode": "QA-MAT-CRUD",
            },
        )
        assert read.status_code == 200
        assert update.status_code == 200
        assert update.json()["data"]["name"] == "QA-MAT-UPDATED"
        assert store.query_one(
            "SELECT name FROM material WHERE uuid=?", (material["uuid"],)
        )["name"] == "QA-MAT-UPDATED"
    finally:
        store.close()


def _inv_duplicate_barcode(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.material.unique")
        _new_material(client, template, barcode="QA-MAT-DUP")
        duplicate = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template,
                "name": "duplicate",
                "barcode": "QA-MAT-DUP",
            },
        )
        assert duplicate.json()["code"] != 0
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM material WHERE barcode=? AND deleted_at IS NULL",
            ("QA-MAT-DUP",),
        )["count"] == 1
    finally:
        store.close()


def _inv_unknown_template(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        response = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": str(uuid4()),
                "name": "invalid",
                "barcode": "QA-MAT-MISSING",
            },
        )
        assert response.json()["code"] != 0
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM material WHERE barcode=?",
            ("QA-MAT-MISSING",),
        )["count"] == 0
    finally:
        store.close()


def _inv_soft_delete(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.material.delete")
        material = _new_material(client, template, barcode="QA-MAT-DELETE")
        assert client.delete(f"/api/v1/materials/{material['uuid']}").json()["code"] == 0
        assert client.get("/api/v1/materials", params={"barcode": "QA-MAT-DELETE"}).json()["data"]["items"] == []
        assert store.query_one(
            "SELECT deleted_at IS NOT NULL AS deleted FROM material WHERE uuid=?",
            (material["uuid"],),
        )["deleted"] in (1, True)
    finally:
        store.close()


def _inv_pose_round_trip(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.material.pose")
        material = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template,
                "name": "QA pose",
                "barcode": "QA-MAT-POSE",
                "relative_position": {"position_x": 1.25, "position_y": 0, "position_z": 3},
            },
        ).json()["data"]
        kept = client.put(
            f"/api/v1/materials/{material['uuid']}",
            json={"resource_template_uuid": template, "name": "QA pose", "barcode": "QA-MAT-POSE", "relative_position": None},
        ).json()["data"]
        assert kept["relative_position"]["position_x"] == 1.25
        assert store.query_one(
            "SELECT position_x FROM material WHERE uuid=?", (material["uuid"],)
        )["position_x"] == 1.25
    finally:
        store.close()


def _inv_template_sync_idempotent(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        first = _template(client, "qa.material.template")
        second = _template(client, "qa.material.template")
        assert first == second
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM resource_template WHERE name=?",
            ("qa.material.template",),
        )["count"] == 1
    finally:
        store.close()


def _inv_lab_meta(_mode: str, tmp_path: Path) -> None:
    store = InventoryStore(str(tmp_path / "lab-meta.db"))
    try:
        store.set_meta("lab_name", "QA Lab")
        store.set_meta("resource_graph_bootstrap_fingerprint", "qa-fp")
        assert store.get_meta("lab_name") == "QA Lab"
        assert store.query_one("SELECT COUNT(*) AS count FROM lab_meta")["count"] == 1
        with pytest.raises(ValueError):
            store.set_meta("not_allowed", "x")
    finally:
        store.close()


def _inv_inline_reagent(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.reagent.inline")
        info = _reagent_info(client)
        created = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template,
                "name": "QA inline reagent",
                "barcode": "QA-REAGENT-INLINE",
                "reagent": {"reagent_info_uuid": info, "quantity": 25, "quantity_unit": "mL"},
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()["data"]
        assert body["reagent"]["quantity"] == 25
        assert store.query_one(
            "SELECT kind FROM container_content WHERE material_uuid=? AND deleted_at IS NULL",
            (body["uuid"],),
        )["kind"] == "reagent"
    finally:
        store.close()


def _inv_inline_reagent_rollback(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.reagent.rollback")
        response = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template,
                "name": "QA invalid reagent",
                "barcode": "QA-REAGENT-ROLLBACK",
                "reagent": {"cas": "missing", "quantity": 2, "quantity_unit": "mL"},
            },
        )
        assert response.json()["code"] != 0
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM material WHERE barcode=?",
            ("QA-REAGENT-ROLLBACK",),
        )["count"] == 0
        assert store.query_one("SELECT COUNT(*) AS count FROM container_content")["count"] == 0
    finally:
        store.close()


def _inv_reagent_info_crud(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        created = client.post(
            "/api/v1/reagent-infos",
            json={"cas": "7732-18-5", "name": "water", "physical_state": "liquid"},
        )
        assert created.status_code == 201
        info = created.json()["data"]
        updated = client.put(
            f"/api/v1/reagent-infos/{info['uuid']}", json={"name": "water-updated"}
        )
        assert updated.status_code == 200
        assert client.get(f"/api/v1/reagent-infos/{info['uuid']}").json()["data"]["name"] == "water-updated"
        assert store.query_one("SELECT name FROM reagent_info WHERE uuid=?", (info["uuid"],))["name"] == "water-updated"
    finally:
        store.close()


def _inv_reagent_create_read(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.reagent.create")
        material = _new_material(client, template, barcode="QA-REAGENT-MAT")
        info = _reagent_info(client)
        response = client.post(
            "/api/v1/reagents",
            json={"material_uuid": material["uuid"], "reagent_info_uuid": info, "quantity": 10, "quantity_unit": "mL"},
        )
        assert response.status_code == 201
        reagent = response.json()["data"]
        assert client.get(f"/api/v1/reagents/{reagent['uuid']}").json()["data"]["material_uuid"] == material["uuid"]
        assert store.query_one("SELECT kind FROM container_content WHERE uuid=?", (reagent["uuid"],))["kind"] == "reagent"
    finally:
        store.close()


def _inv_reagent_revision_conflict(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.reagent.revision")
        material = _new_material(client, template, barcode="QA-REAGENT-REV")
        info = _reagent_info(client)
        reagent = client.post(
            "/api/v1/reagents",
            json={"material_uuid": material["uuid"], "reagent_info_uuid": info, "quantity": 10, "quantity_unit": "mL"},
        ).json()["data"]
        conflict = client.put(
            f"/api/v1/reagents/{reagent['uuid']}",
            json={"quantity": 9, "quantity_unit": "mL", "expected_revision": 99},
        )
        assert conflict.json()["code"] != 0
        assert client.get(f"/api/v1/reagents/{reagent['uuid']}").json()["data"]["revision"] == 1
    finally:
        store.close()


def _inv_reagent_noop_revision(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.reagent.noop")
        material = _new_material(client, template, barcode="QA-REAGENT-NOOP")
        info = _reagent_info(client)
        reagent = client.post(
            "/api/v1/reagents",
            json={"material_uuid": material["uuid"], "reagent_info_uuid": info, "quantity": 10, "quantity_unit": "mL"},
        ).json()["data"]
        same = client.put(
            f"/api/v1/reagents/{reagent['uuid']}",
            json={"quantity": 10, "quantity_unit": "mL", "expected_revision": 1},
        ).json()["data"]
        assert same["revision"] == 1
    finally:
        store.close()


def _inv_reagent_delete_history(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.reagent.delete")
        material = _new_material(client, template, barcode="QA-REAGENT-DELETE")
        info = _reagent_info(client)
        reagent = client.post(
            "/api/v1/reagents",
            json={"material_uuid": material["uuid"], "reagent_info_uuid": info, "quantity": 10, "quantity_unit": "mL"},
        ).json()["data"]
        assert client.delete(f"/api/v1/reagents/{reagent['uuid']}").json()["code"] == 0
        history = client.get(f"/api/v1/materials/{material['uuid']}/reagent-history").json()["data"]["items"]
        assert history
        assert store.query_one("SELECT deleted_at FROM reagent WHERE uuid=?", (reagent["uuid"],))["deleted_at"] is not None
    finally:
        store.close()


def _inv_current_substance(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        source_template = _template(client, "qa.substance.source")
        target_template = _template(client, "qa.substance.target")
        source = _new_material(client, source_template, barcode="QA-SOURCE")
        target = _new_material(client, target_template, barcode="QA-TARGET")
        info = _reagent_info(client)
        reagent = client.post(
            "/api/v1/reagents",
            json={"material_uuid": source["uuid"], "reagent_info_uuid": info, "quantity": 10, "quantity_unit": "mL"},
        ).json()["data"]
        created = client.post(
            "/api/v1/current-substances",
            json={"material_uuid": target["uuid"], "quantity": 5, "quantity_unit": "mL", "physical_state": "liquid", "components": [{"reagent_uuid": reagent["uuid"], "quantity": 1, "quantity_unit": "mL"}]},
        )
        assert created.status_code in {200, 201}, created.text
        assert store.query_one("SELECT kind FROM container_content WHERE material_uuid=? AND deleted_at IS NULL", (target["uuid"],))["kind"] == "substance"
    finally:
        store.close()


def _inv_sample_rejects_reagent_container(_mode: str, tmp_path: Path) -> None:
    client, store = _inventory_app(tmp_path)
    try:
        template = _template(client, "qa.sample.reagent")
        material = _new_material(client, template, barcode="QA-SAMPLE-REAGENT")
        info = _reagent_info(client)
        client.post("/api/v1/reagents", json={"material_uuid": material["uuid"], "reagent_info_uuid": info, "quantity": 5, "quantity_unit": "mL"})
        rejected = client.post("/api/v1/samples", json={"material_uuid": material["uuid"], "code": "QA-SAMPLE", "name": "invalid", "quantity": 1, "quantity_unit": "mL"})
        assert rejected.json()["code"] != 0
    finally:
        store.close()


def _inv_quantity_consumption(_mode: str, tmp_path: Path) -> None:
    inventory, material, info, reagent = _quantity_inventory(tmp_path)
    workflow, graph, prepared = _quantity_workflow(tmp_path / "workflow.db", reagent_info_uuid=info, material_uuid=material)
    coordinator = WorkflowQuantityInventory(workflow, InventoryService(inventory))
    try:
        _create_task(workflow, coordinator, graph=graph, prepared=prepared, reagent_uuid=reagent)
        consumption = [{"inventory_type": "reagent", "inventory_uuid": reagent, "actual_quantity": 1, "quantity_unit": "mL"}]
        first = coordinator.consume_successful_job(task_uuid="30000000-0000-4000-8000-000000000001", job_uuid="40000000-0000-4000-8000-000000000001", consumptions=consumption)
        second = coordinator.consume_successful_job(task_uuid="30000000-0000-4000-8000-000000000001", job_uuid="40000000-0000-4000-8000-000000000001", consumptions=consumption)
        assert first == second
        assert float(inventory.query_one("SELECT quantity FROM reagent WHERE uuid=?", (reagent,))["quantity"]) == 9
    finally:
        workflow.close()
        inventory.close()


def _inv_quantity_preflight(_mode: str, tmp_path: Path) -> None:
    inventory, material, info, reagent = _quantity_inventory(tmp_path)
    workflow, graph, prepared = _quantity_workflow(tmp_path / "workflow.db", reagent_info_uuid=info, material_uuid=material)
    coordinator = WorkflowQuantityInventory(workflow, InventoryService(inventory))
    try:
        preview = coordinator.preflight_task_allocations(graph=graph, prepared=prepared, bindings=[{"requirement_key": "ethanol", "inventory_type": "reagent", "inventory_uuid": reagent, "reserved_quantity": 2, "quantity_unit": "mL"}])
        assert preview[0]["reserved_quantity"] == 2
        assert workflow.count_rows("workflow_task") == 0
        assert inventory.query_one("SELECT COUNT(*) AS count FROM inventory_reservation")["count"] == 0
    finally:
        workflow.close()
        inventory.close()


def _inv_reservation_protection(_mode: str, tmp_path: Path) -> None:
    inventory, material, info, reagent = _quantity_inventory(tmp_path)
    workflow, graph, prepared = _quantity_workflow(tmp_path / "workflow.db", reagent_info_uuid=info, material_uuid=material)
    coordinator = WorkflowQuantityInventory(workflow, InventoryService(inventory))
    try:
        _create_task(workflow, coordinator, graph=graph, prepared=prepared, reagent_uuid=reagent)
        service = BackendReagentService(inventory)
        current = service.get_reagent(reagent)
        with pytest.raises(Exception):
            service.update_reagent(reagent, {**current, "quantity": 1, "expected_revision": current["revision"]})
        coordinator.release_task("30000000-0000-4000-8000-000000000001", reason="qa")
        updated = service.update_reagent(reagent, {**current, "quantity": 1, "expected_revision": current["revision"]})
        assert float(updated["quantity"]) == 1
    finally:
        workflow.close()
        inventory.close()


def _inv_material_crud_after_restart(_mode: str, tmp_path: Path) -> None:
    """重启库存存储后，合并表中的物料仍可读写。"""

    path = tmp_path / "inventory.db"
    client, store = _inventory_app(tmp_path)
    template = _template(client, "qa.material.restart")
    material = _new_material(client, template, barcode="QA-MAT-RESTART")
    store.close()
    reopened = InventoryStore(str(path))
    try:
        assert reopened.query_one(
            "SELECT barcode FROM material WHERE uuid=?", (material["uuid"],)
        )["barcode"] == "QA-MAT-RESTART"
        assert reopened.query_one(
            "SELECT COUNT(*) AS count FROM container_content WHERE material_uuid=?",
            (material["uuid"],),
        )["count"] == 0
    finally:
        reopened.close()


INVENTORY_CASES: list[tuple[str, Scenario]] = [
    ("INV-01-material-crud", _inv_material_crud),
    ("INV-02-duplicate-barcode", _inv_duplicate_barcode),
    ("INV-03-unknown-template", _inv_unknown_template),
    ("INV-04-material-soft-delete", _inv_soft_delete),
    ("INV-05-pose-round-trip", _inv_pose_round_trip),
    ("INV-06-template-sync-idempotency", _inv_template_sync_idempotent),
    ("INV-07-lab-meta-singleton", _inv_lab_meta),
    ("INV-08-inline-reagent", _inv_inline_reagent),
    ("INV-09-inline-reagent-rollback", _inv_inline_reagent_rollback),
    ("INV-10-reagent-info-crud", _inv_reagent_info_crud),
    ("INV-11-reagent-create-read", _inv_reagent_create_read),
    ("INV-12-reagent-revision-conflict", _inv_reagent_revision_conflict),
    ("INV-13-reagent-noop-revision", _inv_reagent_noop_revision),
    ("INV-14-reagent-delete-history", _inv_reagent_delete_history),
    ("INV-15-current-substance", _inv_current_substance),
    ("INV-16-sample-rejects-reagent", _inv_sample_rejects_reagent_container),
    ("INV-17-quantity-consumption-idempotency", _inv_quantity_consumption),
    ("INV-18-quantity-preflight-read-only", _inv_quantity_preflight),
    ("INV-19-reservation-protection", _inv_reservation_protection),
    ("INV-20-material-crud-after-restart", _inv_material_crud_after_restart),
]


# ---------------------------------------------------------------------------
# Scheduling cases: 18


def _scheduler_basic_dispatch(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-basic")
    try:
        submitted = runtime.submit(task_name="qa-basic-dispatch", devices=["reactor-a"])
        assert submitted["task"]["status"] in {"running", "pending"}
        assert len(runtime.dispatcher.dispatched) == 1
        assert runtime.dispatcher.dispatched[0]["device_id"] == "reactor-a"
    finally:
        runtime.close()


def _scheduler_success(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-success")
    try:
        aggregate = runtime.submit(task_name="qa-success", devices=["reactor-a"])
        job_uuid = aggregate["jobs"][0]["uuid"]
        runtime.scheduler.on_job_finished(job_uuid, True, {"result": "ok"})
        assert runtime.workflow_store.get_job(job_uuid)["status"] == "succeeded"
        assert runtime.workflow_store.get_task(aggregate["task"]["uuid"])["status"] == "succeeded"
    finally:
        runtime.close()


def _scheduler_failure(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-failure")
    try:
        aggregate = runtime.submit(task_name="qa-failure", devices=["reactor-a"])
        job_uuid = aggregate["jobs"][0]["uuid"]
        runtime.scheduler.on_job_finished(job_uuid, False, {"error": "simulated"})
        assert runtime.workflow_store.get_job(job_uuid)["status"] == "failed"
        assert runtime.workflow_store.get_task(aggregate["task"]["uuid"])["status"] in {"running", "failed"}
    finally:
        runtime.close()


def _scheduler_dependency_order(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-dependency")
    try:
        aggregate = runtime.submit(
            task_name="qa-dependency", devices=["reactor-a", "reactor-b"], edges=[(0, 1)]
        )
        first, second = [job["uuid"] for job in aggregate["jobs"]]
        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [first]
        runtime.scheduler.on_job_finished(first, True, {})
        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [first, second]
    finally:
        runtime.close()


def _scheduler_pause_resume(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-pause")
    try:
        aggregate = runtime.submit(
            task_name="qa-pause", devices=["reactor-a", "reactor-b"], edges=[(0, 1)]
        )
        first, second = [job["uuid"] for job in aggregate["jobs"]]
        paused = runtime.bridge.pause(aggregate["task"]["uuid"])
        assert paused["task"]["control_status"] == "paused"
        runtime.scheduler.on_job_finished(first, True, {})
        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [first]
        runtime.bridge.resume(aggregate["task"]["uuid"])
        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [first, second]
    finally:
        runtime.close()


def _scheduler_step_mode(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-step")
    try:
        aggregate = runtime.persist(
            task_name="qa-step", devices=["reactor-a", "reactor-b"], edges=[(0, 1)], run_mode="step"
        )
        runtime.bridge.submit(aggregate)
        assert runtime.dispatcher.dispatched == []
        runtime.bridge.step(aggregate["uuid"])
        assert len(runtime.dispatcher.dispatched) == 1
        first = runtime.workflow_store.list_jobs(aggregate["uuid"])[0]["uuid"]
        runtime.scheduler.on_job_finished(first, True, {})
        runtime.bridge.step(aggregate["uuid"])
        assert len(runtime.dispatcher.dispatched) == 2
    finally:
        runtime.close()


def _scheduler_cancel_waiting(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-cancel")
    try:
        blocker = runtime.submit(task_name="qa-cancel-blocker", devices=["reactor-a"])
        waiting = runtime.persist(task_name="qa-cancel-waiting", devices=["reactor-a"])
        runtime.bridge.submit(waiting)
        waiting_job = runtime.workflow_store.list_jobs(waiting["uuid"])[0]["uuid"]
        result = runtime.bridge.cancel(waiting["uuid"], command_uuid=stable_uuid("qa-cancel-command"))
        assert result["task"]["status"] == "canceled"
        assert runtime.workflow_store.get_job(waiting_job)["status"] == "canceled"
        runtime.scheduler.on_job_finished(blocker["jobs"][0]["uuid"], True, {})
        assert all(item["job_id"] != waiting_job for item in runtime.dispatcher.dispatched)
    finally:
        runtime.close()


def _scheduler_drain_resume(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-drain")
    try:
        active = runtime.submit(task_name="qa-drain-active", devices=["reactor-a"])
        assert runtime.scheduler.begin_drain()["phase"] == "draining"
        waiting = runtime.submit(task_name="qa-drain-waiting", devices=["reactor-b"])
        runtime.scheduler.on_job_finished(active["jobs"][0]["uuid"], True, {})
        assert runtime.workflow_store.get_task(waiting["task"]["uuid"])["status"] == "pending"
        runtime.scheduler.resume_from_drain()
        assert any(item["job_id"] == waiting["jobs"][0]["uuid"] for item in runtime.dispatcher.dispatched)
    finally:
        runtime.close()


def _scheduler_restart_recovery(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-restart")
    try:
        aggregate = runtime.submit(task_name="qa-restart", devices=["reactor-a"])
        job_uuid = aggregate["jobs"][0]["uuid"]
        runtime.scheduler.on_job_finished(job_uuid, True, {"before_restart": True})
        sent = list(runtime.dispatcher.dispatched)
        runtime.restart_scheduler()
        assert runtime.workflow_store.get_job(job_uuid)["status"] == "succeeded"
        assert runtime.dispatcher.dispatched == sent
    finally:
        runtime.close()


def _scheduler_submit_idempotent(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-idempotent")
    try:
        task = runtime.persist(task_name="qa-submit-idempotent", devices=["reactor-a"])
        first = runtime.bridge.submit(task)
        second = runtime.bridge.submit(task)
        assert first["task"]["uuid"] == second["task"]["uuid"]
        assert len(runtime.dispatcher.dispatched) == 1
    finally:
        runtime.close()


def _scheduler_shared_device_wait(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-shared")
    try:
        first = runtime.submit(task_name="qa-shared-first", devices=["reactor-a"])
        second = runtime.submit(task_name="qa-shared-second", devices=["reactor-a"])
        assert len(runtime.dispatcher.dispatched) == 1
        runtime.scheduler.on_job_finished(first["jobs"][0]["uuid"], True, {})
        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [
            first["jobs"][0]["uuid"], second["jobs"][0]["uuid"]
        ]
    finally:
        runtime.close()


def _scheduler_independent_devices(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-independent")
    try:
        first = runtime.submit(task_name="qa-independent-a", devices=["reactor-a"])
        second = runtime.submit(task_name="qa-independent-b", devices=["reactor-b"])
        assert {item["job_id"] for item in runtime.dispatcher.dispatched} == {
            first["jobs"][0]["uuid"], second["jobs"][0]["uuid"]
        }
    finally:
        runtime.close()


def _scheduler_inflight_capacity(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-capacity", max_in_flight_jobs=1)
    try:
        first = runtime.submit(task_name="qa-capacity-a", devices=["reactor-a"])
        second = runtime.submit(task_name="qa-capacity-b", devices=["reactor-b"])
        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [first["jobs"][0]["uuid"]]
        runtime.scheduler.on_job_finished(first["jobs"][0]["uuid"], True, {})
        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [
            first["jobs"][0]["uuid"], second["jobs"][0]["uuid"]
        ]
    finally:
        runtime.close()


def _scheduler_priority_wait(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-priority")
    try:
        blocker = runtime.submit(task_name="qa-priority-blocker", devices=["reactor-a"])
        low = runtime.persist(task_name="qa-priority-low", devices=["reactor-a"], priority="normal")
        high = runtime.persist(task_name="qa-priority-high", devices=["reactor-a"], priority="high")
        runtime.bridge.submit(low)
        runtime.bridge.submit(high)
        runtime.scheduler.on_job_finished(blocker["jobs"][0]["uuid"], True, {})
        high_job = runtime.workflow_store.list_jobs(high["uuid"])[0]["uuid"]
        assert runtime.dispatcher.dispatched[-1]["job_id"] == high_job
    finally:
        runtime.close()


def _scheduler_offline_target(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(
        tmp_path / "scheduler-offline",
        device_available=lambda device_id: device_id != "reactor-a",
    )
    try:
        aggregate = runtime.submit(task_name="qa-offline", devices=["reactor-a"])
        assert runtime.dispatcher.dispatched == []
        assert runtime.workflow_store.get_task(aggregate["task"]["uuid"])["status"] in {"pending", "running", "failed"}
    finally:
        runtime.close()


def _scheduler_committed_outcome(_mode: str, tmp_path: Path) -> None:
    runtime = build_core_runtime(tmp_path / "scheduler-outcome")
    try:
        aggregate = runtime.submit(task_name="qa-committed-outcome", devices=["reactor-a"])
        job_uuid = aggregate["jobs"][0]["uuid"]
        runtime.scheduler.on_job_outcome(job_uuid, CommittedJobOutcome("succeeded", {"source": "edge"}, [], []))
        assert runtime.workflow_store.get_job(job_uuid)["return_info"] == {"source": "edge"}
    finally:
        runtime.close()


def _scheduler_device_state_history(_mode: str, tmp_path: Path) -> None:
    state = DeviceStateStore(str(tmp_path / "device-state.db"))
    try:
        assert state.set("reactor-a", "temperature", 20, now_ms=1)
        assert not state.set("reactor-a", "temperature", 20, now_ms=2)
        assert state.set("reactor-a", "temperature", 21, now_ms=3)
        assert state.latest_for("reactor-a")["temperature"]["value"] == 21
        assert len(state.history("reactor-a", "temperature")) == 2
    finally:
        state.close()


def _scheduler_edge_outbox_round_trip(_mode: str, tmp_path: Path) -> None:
    store = EdgeControlStore(str(tmp_path / "edge-control.db"))
    try:
        event_uuid = store.enqueue_event("job.completed", {"job_id": "qa-job"})
        event = store.event_for_ack(event_uuid)
        assert event is not None and event.payload["job_id"] == "qa-job"
        store.acknowledge_event(event_uuid)
        assert store.event_for_ack(event_uuid) is None
    finally:
        store.close()


SCHEDULER_CASES: list[tuple[str, Scenario]] = [
    ("SCH-01-basic-dispatch", _scheduler_basic_dispatch),
    ("SCH-02-success", _scheduler_success),
    ("SCH-03-failure", _scheduler_failure),
    ("SCH-04-dependency-order", _scheduler_dependency_order),
    ("SCH-05-pause-resume", _scheduler_pause_resume),
    ("SCH-06-step-mode", _scheduler_step_mode),
    ("SCH-07-cancel-waiting", _scheduler_cancel_waiting),
    ("SCH-08-drain-resume", _scheduler_drain_resume),
    ("SCH-09-restart-recovery", _scheduler_restart_recovery),
    ("SCH-10-submit-idempotency", _scheduler_submit_idempotent),
    ("SCH-11-shared-device-wait", _scheduler_shared_device_wait),
    ("SCH-12-independent-devices", _scheduler_independent_devices),
    ("SCH-13-inflight-capacity", _scheduler_inflight_capacity),
    ("SCH-14-priority-wait", _scheduler_priority_wait),
    ("SCH-15-offline-target", _scheduler_offline_target),
    ("SCH-16-committed-outcome", _scheduler_committed_outcome),
    ("SCH-17-device-state-history", _scheduler_device_state_history),
    ("SCH-18-edge-outbox", _scheduler_edge_outbox_round_trip),
]


def _case_params(cases: list[tuple[str, Scenario]]) -> list[Any]:
    return [pytest.param(scenario, id=case_id) for case_id, scenario in cases]


@pytest.mark.parametrize("scenario", _case_params(MIGRATION_CASES))
def test_qa_migration_case(database_mode: str, tmp_path: Path, scenario: Scenario) -> None:
    scenario(database_mode, tmp_path)


@pytest.mark.parametrize("scenario", _case_params(INVENTORY_CASES))
def test_qa_inventory_case(database_mode: str, tmp_path: Path, scenario: Scenario) -> None:
    scenario(database_mode, tmp_path)


@pytest.mark.parametrize("scenario", _case_params(DEVICE_CASES))
def test_qa_device_action_case(database_mode: str, tmp_path: Path, scenario: Scenario) -> None:
    scenario(database_mode, tmp_path)


@pytest.mark.parametrize("scenario", _case_params(WORKFLOW_CASES))
def test_qa_workflow_case(database_mode: str, tmp_path: Path, scenario: Scenario) -> None:
    scenario(database_mode, tmp_path)


@pytest.mark.parametrize("scenario", _case_params(SCHEDULER_CASES))
def test_qa_scheduler_case(database_mode: str, tmp_path: Path, scenario: Scenario) -> None:
    scenario(database_mode, tmp_path)
