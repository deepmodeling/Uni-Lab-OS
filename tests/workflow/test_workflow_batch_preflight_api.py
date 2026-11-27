"""批量候选运行预检：共享库存总量、重复工作流与零占用。"""

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.workflow.quantity_inventory_test_support import _inventory
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import WorkflowInventoryRequirementWrite, WorkflowNodeWrite
from unilabos.workflow.published_contract import PublishedWorkflowContractStore
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


@contextmanager
def _runtime(tmp_path: Path, *, quantity: float = 8, unit: str = "mL", missing_device: bool = False) -> Iterator[tuple[Any, ...]]:
    """装配真实工作流服务、库存与调度桥，预检不得触发设备派发。"""

    inventory, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    store = WorkflowStore(tmp_path / "workflow.db")
    node_uuid, template_uuid = str(uuid4()), str(uuid4())
    goal = {"type": "object", "properties": {}}
    graph = store.create_workflow_with_graph(
        workflow_uuid=str(uuid4()), name="批量数量预检", tags=[], description=None,
        meta_data={"unilab": {"input_contract": {"version": 1, "parameters": [{"name": "sample", "schema": {"type": "string"}, "required": True}, {"name": "volume", "schema": {"type": "number", "minimum": 0}, "required": False, "default": quantity}]}, "output_contract": {"version": 1, "outputs": []}}},
        nodes=[WorkflowNodeWrite(
            uuid=node_uuid, workflow_node_template_uuid=template_uuid, name="分液", type="device_action",
            action_name="dispense", action_type="UniLabJsonCommand", material_uuid=material_uuid,
            param={"goal": {}}, meta_data={"unilab": {"executor_binding": {"mode": "fixed", "device_id": "batch-test-dispenser"}}},
        )], edges=[], inventory_requirements=[WorkflowInventoryRequirementWrite(
            uuid=str(uuid4()), consume_node_uuid=node_uuid, requirement_key="ethanol",
            target_type="reagent_info", reagent_info_uuid=info_uuid, required_quantity=quantity, quantity_unit=unit, allow_split=False,
            meta_data={"unilab": {"quantity_binding": {"kind": "workflow_input", "parameter": "volume"}}},
        )], node_templates=[{
            "uuid": template_uuid, "resource_template_uuid": str(uuid4()), "name": "dispense", "display_name": "分液",
            "type": "UniLabJsonCommand", "node_type": "device_action", "schema": goal,
            "meta_data": {"unilab": {"action_contract_schema": {"type": "object", "properties": {"goal": goal}}}},
        }], handle_templates=[], template_catalog_fingerprint="sha256:" + "1" * 64, trusted_authoring_graph=True,
    )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=InventoryService(inventory))
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    service = WorkflowService(
        store, task_scheduler_bridge=bridge,
        material_resolver=lambda identity: None if missing_device else {"uuid": identity},
        resource_reference_resolver=lambda identity: None if missing_device else {
            "uuid": material_uuid,
            "resource_template_uuid": graph["node_templates"][0]["resource_template_uuid"],
        },
    )
    try:
        yield TestClient(create_workflow_app(service)), service, store, inventory, graph, reagent_uuid, dispatcher
    finally:
        service.close()
        scheduler.close()
        inventory.close()


def _item(workflow_uuid: str, reagent_uuid: str, sample: str = "A") -> dict[str, Any]:
    """每项各需同一瓶试剂 8 mL，可重复使用工作流身份。"""

    return {"workflow_uuid": workflow_uuid, "input": {"sample": sample}, "inventory_bindings": [{
        "requirement_key": "ethanol", "inventory_type": "reagent",
        "inventory_uuid": reagent_uuid, "reserved_quantity": 8, "quantity_unit": "mL",
    }]}


def _facts(store: Any, inventory: Any) -> tuple[Any, ...]:
    """读取预检前后的全部库存、预留、台账、发件箱与任务事实。"""

    return tuple(store.count_rows(table) for table in (
        "workflow_task", "workflow_node_job", "workflow_inventory_allocation",
    )) + tuple(inventory.query_all(f"SELECT * FROM {table}") for table in (
        "reagent", "inventory_reservation", "inventory_ledger", "sync_outbox",
    ))


def test_batch_preflight_uses_frozen_contract_and_live_inventory(tmp_path: Path) -> None:
    """当前图和发布合同变化不能替换候选指纹，库存变化仍须即时生效。"""

    with _runtime(tmp_path) as (client, _, store, inventory, graph, reagent, dispatcher):
        workflow = graph["workflow"]["uuid"]
        contracts = PublishedWorkflowContractStore(store)
        frozen = contracts.publish(graph=graph, expected_revision=1)
        changed = deepcopy(graph["workflow"]["meta_data"])
        changed["unilab"]["input_contract"]["parameters"][0]["name"] = "new_sample"
        latest = store.save_graph(
            workflow, revision=1,
            nodes=[WorkflowNodeWrite.model_validate(node) for node in graph["nodes"]],
            edges=[], workflow_meta_data=changed,
            inventory_requirements=[WorkflowInventoryRequirementWrite.model_validate(row)
                                    for row in graph["inventory_requirements"]],
        )
        newest = contracts.publish(graph=latest, expected_revision=2)
        candidate = {**_item(workflow, reagent), "revision_fingerprint": frozen["source_hash"]}
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [candidate]})
        assert response.json()["code"] == 0, response.text
        report = response.json()["data"]
        assert report["can_run"] is True, report
        assert report["items"][0]["workflow_revision"] == 1
        assert _facts(store, inventory) == before
        current = client.post("/api/v1/workflows/run-preflight", json={"items": [_item(workflow, reagent)]})
        assert current.json()["data"]["items"][0]["workflow_revision"] == 2
        assert current.json()["data"]["can_run"] is False
        pending = client.post("/api/v1/workflows/run-preflight", json={"items": [{
            "workflow_uuid": workflow, "revision_fingerprint": frozen["source_hash"],
            "input_ready": False,
        }]})
        assert pending.json()["data"]["can_run"] is True, pending.text
        assert pending.json()["data"]["items"][0]["workflow_revision"] == 1
        assert any(check["code"] == "upstream_input_pending"
                   for check in pending.json()["data"]["items"][0]["checks"])
        mixed = client.post("/api/v1/workflows/run-preflight", json={"items": [candidate, {
            **_item(workflow, reagent), "input": {"new_sample": "B"},
            "revision_fingerprint": newest["source_hash"],
        }]})
        mixed_report = mixed.json()["data"]
        assert [row["workflow_revision"] for row in mixed_report["items"]] == [1, 2]
        assert all(row["can_run"] for row in mixed_report["items"])
        assert mixed_report["can_run"] is False
        assert mixed_report["checks"][0]["details"]["shortages"][0]["required_quantity"] == 16

        with inventory.transaction() as connection:
            connection.execute("UPDATE reagent SET quantity=5 WHERE uuid=?", (reagent,))
        before = _facts(store, inventory)
        shortage = client.post("/api/v1/workflows/run-preflight", json={"items": [candidate]})
        assert shortage.json()["code"] == 0, shortage.text
        report = shortage.json()["data"]
        assert report["can_run"] is False
        gap = report["checks"][0]["details"]["shortages"][0]
        assert gap["required_quantity"] == 8 and gap["available_quantity"] == 5
        assert _facts(store, inventory) == before
        assert dispatcher.dispatched == []


@pytest.mark.parametrize("missing", ["unknown", "other_workflow", "deleted"])
def test_batch_preflight_never_falls_back_when_contract_unavailable(tmp_path: Path, missing: str) -> None:
    """指纹不存在、属于其他工作流或已删除时必须失败，不能改查当前定义。"""

    with _runtime(tmp_path) as (client, _, store, inventory, graph, reagent, _):
        contract = PublishedWorkflowContractStore(store).publish(graph=graph, expected_revision=1)
        candidate = {**_item(graph["workflow"]["uuid"], reagent), "revision_fingerprint": contract["source_hash"]}
        if missing == "unknown":
            candidate["revision_fingerprint"] = "sha256:" + "f" * 64
        elif missing == "other_workflow":
            candidate["workflow_uuid"] = store.create_workflow(
                workflow_uuid=str(uuid4()), name="另一个工作流", tags=[], description=None, meta_data={},
            )["uuid"]
        else:
            with store.transaction() as connection:
                connection.execute("UPDATE published_workflow_contract SET deleted_at=? WHERE uuid=?",
                                   ("2026-10-30T00:00:00Z", contract["uuid"]))
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [candidate]})
        assert response.json()["code"] == 3003, response.text
        assert "指纹" in response.json()["error"]["msg"]
        assert _facts(store, inventory) == before


def test_batch_frozen_preflight_rejects_current_missing_executor(tmp_path: Path) -> None:
    """冻结合同不能绕过当前固定设备不存在的检查，失败时仍须零占用。"""

    with _runtime(tmp_path, missing_device=True) as (client, _, store, inventory, graph, reagent, dispatcher):
        contract = PublishedWorkflowContractStore(store).publish(graph=graph, expected_revision=1)
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [{
            **_item(graph["workflow"]["uuid"], reagent), "revision_fingerprint": contract["source_hash"],
        }]})
        assert response.json()["code"] == 3003, response.text
        assert _facts(store, inventory) == before
        assert dispatcher.dispatched == []


def test_batch_sums_repeated_workflow_inputs_without_reserving(tmp_path: Path) -> None:
    """只循环单份预检会错误放过 16 mL 总需求；整批检查必须拒绝。"""

    with _runtime(tmp_path) as (client, _, store, inventory, graph, reagent, dispatcher):
        workflow = graph["workflow"]["uuid"]
        items = [_item(workflow, reagent), _item(workflow, reagent, "B")]
        before = _facts(store, inventory)
        for item in items:
            response = client.post(f"/api/v1/workflows/{workflow}/run-preflight", json={
                key: value for key, value in item.items() if key != "workflow_uuid"
            })
            assert response.json()["data"]["can_run"] is True, response.text
        for _ in range(2):
            response = client.post("/api/v1/workflows/run-preflight", json={"items": items})
            assert response.status_code == 200 and response.json()["code"] == 0, response.text
            assert response.headers["cache-control"] == "no-store"
            report = response.json()["data"]
            assert report["can_run"] is False and report["status"] == "temporarily_unavailable"
            assert [item["workflow_uuid"] for item in report["items"]] == [workflow, workflow]
            assert all(item["can_run"] for item in report["items"])
            shortage = report["checks"][0]["details"]["shortages"][0]
            assert shortage["available_quantity"] == 10
            assert shortage["required_quantity"] == 16
            assert shortage["quantity_unit"] == "mL"
        assert _facts(store, inventory) == before
        assert dispatcher.dispatched == []


def test_batch_uses_existing_reservations_and_can_pass_after_release(tmp_path: Path) -> None:
    """已有预留必须扣除；释放后同一候选重新检查可通过且不创建任务。"""

    with _runtime(tmp_path) as (client, _, store, inventory, graph, reagent, _):
        from unilabos.app.scheduler.inventory.workflow_quantity import WorkflowQuantityInventoryAuthority

        item = _item(graph["workflow"]["uuid"], reagent)
        authority = WorkflowQuantityInventoryAuthority(inventory, edge_id="edge-default", lab_id="edge-lab")
        task = str(uuid4())
        authority.reserve_task(task, [{
            "uuid": str(uuid4()), "workflow_task_uuid": task, "workflow_node_job_uuid": str(uuid4()),
            "requirement_key": "other", "inventory_type": "reagent", "inventory_uuid": reagent,
            "material_uuid": graph["nodes"][0]["material_uuid"], "reserved_quantity": 3, "quantity_unit": "mL",
        }])
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [item]})
        assert response.json()["code"] == 0, response.text
        assert response.json()["data"]["can_run"] is False
        assert _facts(store, inventory) == before
        authority.release_task(task, reason="operator_canceled")
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [item]})
        assert response.json()["data"]["can_run"] is True, response.text
        assert _facts(store, inventory) == before


def test_batch_different_workflows_share_one_inventory_budget(tmp_path: Path) -> None:
    """不同工作流使用同一库存，也必须合计检查，并保留请求顺序。"""

    with _runtime(tmp_path) as (client, service, _, _, graph, reagent, _):
        first = graph["workflow"]["uuid"]
        second = service.duplicate_workflow(first, name="第二个候选")["workflow"]["uuid"]
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [
            _item(second, reagent), _item(first, reagent),
        ]})
        assert response.json()["code"] == 0, response.text
        report = response.json()["data"]
        assert report["can_run"] is False
        assert [item["workflow_uuid"] for item in report["items"]] == [second, first]
        assert report["checks"][0]["details"]["shortages"][0]["required_quantity"] == 16


def test_batch_bad_binding_cannot_produce_a_successful_batch(tmp_path: Path) -> None:
    """一项绑定缺失时必须整批不可运行，不能仅检查剩余合法候选。"""

    with _runtime(tmp_path) as (client, _, store, inventory, graph, reagent, _):
        item = _item(graph["workflow"]["uuid"], reagent)
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [
            item, {**item, "inventory_bindings": []},
        ]})
        assert response.json()["code"] == 0, response.text
        report = response.json()["data"]
        assert report["can_run"] is False
        assert report["items"][0]["can_run"] is True
        assert report["items"][1]["can_run"] is False
        assert report["checks"][0]["status"] == "blocked"
        assert _facts(store, inventory) == before


@pytest.mark.parametrize("payload", [
    {"items": []}, {"items": [{}]}, {"items": [{"workflow_uuid": "bad"}]},
    {"items": [{"workflow_uuid": str(uuid4()), "input": []}]},
    {"items": [{"workflow_uuid": str(uuid4())}] * 101},
    {"items": [{"workflow_uuid": str(uuid4())}], "unknown": 1},
    {"items": [{"workflow_uuid": str(uuid4()), "unknown": 1}]},
    {"items": [{"workflow_uuid": str(uuid4()), "revision_fingerprint": ""}]},
    {"items": [{"workflow_uuid": str(uuid4()), "revision_fingerprint": "bad"}]},
    {"items": [{"workflow_uuid": str(uuid4()), "revision_fingerprint": 123}]},
])
def test_batch_rejects_invalid_shape_without_tasks(tmp_path: Path, payload: dict[str, Any]) -> None:
    """批量载荷要求 1 至 100 项、合法身份和对象输入，并拒绝未知字段。"""

    with _runtime(tmp_path) as (client, _, store, inventory, _, _, _):
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json=payload)
        assert response.status_code == 200 and response.json()["code"] != 0, response.text
        assert _facts(store, inventory) == before


def test_batch_preflight_openapi_exposes_request_and_response(tmp_path: Path) -> None:
    """客户端从运行时 OpenAPI 能获取批量请求和完整成功响应模型。"""

    with _runtime(tmp_path) as (client, *_):
        schema = client.get("/openapi.json").json()
        operation = schema["paths"]["/api/v1/workflows/run-preflight"]["post"]
        assert "get" not in schema["paths"]["/api/v1/workflows/run-preflight"]
        assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/WorkflowBatchRunPreflightRequest")
        assert operation["responses"]["200"]["content"]["application/json"]["schema"]["anyOf"]
        candidate = schema["components"]["schemas"]["WorkflowBatchRunPreflightItem"]
        assert "revision_fingerprint" not in candidate["required"]
        assert candidate["properties"]["revision_fingerprint"]["anyOf"][0]["pattern"] == r"^sha256:[0-9a-f]{64}$"



def test_batch_reports_total_shortage_even_when_each_item_is_short(tmp_path: Path) -> None:
    """每项已经不够时，整批仍须返回全部需求的总量，不能遗漏受阻候选。"""

    with _runtime(tmp_path) as (client, _, store, inventory, graph, reagent, _):
        with inventory.transaction() as connection:
            connection.execute("UPDATE reagent SET quantity=5 WHERE uuid=?", (reagent,))
        item = _item(graph["workflow"]["uuid"], reagent)
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [item, item]})
        report = response.json()["data"]
        assert not report["can_run"] and all(not item["can_run"] for item in report["items"])
        shortage = report["checks"][0]["details"]["shortages"][0]
        assert shortage["available_quantity"] == 5 and shortage["required_quantity"] == 16
        assert _facts(store, inventory) == before


def test_batch_uses_each_candidates_input_quantity(tmp_path: Path) -> None:
    """每份入口参数独立解析，不能复用第一份的数量或把同一工作流去重。"""

    with _runtime(tmp_path) as (client, _, _, _, graph, reagent, _):
        first, second = _item(graph["workflow"]["uuid"], reagent), _item(graph["workflow"]["uuid"], reagent, "B")
        first["input"]["volume"] = 6
        first["inventory_bindings"][0]["reserved_quantity"] = 6
        second["input"]["volume"] = 5
        second["inventory_bindings"][0]["reserved_quantity"] = 5
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [first, second]})
        report = response.json()["data"]
        assert all(item["can_run"] for item in report["items"]), report
        assert report["can_run"] is False
        assert report["checks"][0]["details"]["shortages"][0]["required_quantity"] == 11


def test_batch_converts_all_candidates_to_inventory_unit(tmp_path: Path) -> None:
    """需求单位为微升时，合计不能拿未换算的数字与毫升库存比较。"""

    with _runtime(tmp_path, quantity=8000, unit="uL") as (client, _, _, _, graph, reagent, _):
        item = _item(graph["workflow"]["uuid"], reagent)
        item["inventory_bindings"][0].update(reserved_quantity=8000, quantity_unit="uL")
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [item, item]})
        report = response.json()["data"]
        assert all(candidate["can_run"] for candidate in report["items"]), report
        shortage = report["checks"][0]["details"]["shortages"][0]
        assert shortage["required_quantity"] == 16 and shortage["quantity_unit"] == "mL"


@pytest.mark.parametrize("capabilities,mode,expected_code", [
    ({"can_edit_definitions": False}, "normal", 0),
    ({"can_run_unpublished": False}, "normal", 3002),
    ({"can_debug": False}, "step", 1001),
])
def test_batch_respects_runtime_capabilities(tmp_path: Path, capabilities: dict[str, bool], mode: str, expected_code: int) -> None:
    """批量预检仍只读，不能绕过工站工作流可执行性与调试能力限制。"""

    from unilabos.app.runtime_capabilities import get_runtime_capabilities, set_runtime_capabilities

    with _runtime(tmp_path) as (client, _, store, inventory, graph, reagent, _):
        previous = get_runtime_capabilities()
        before = _facts(store, inventory)
        try:
            set_runtime_capabilities(capabilities)
            item = _item(graph["workflow"]["uuid"], reagent)
            item["run_mode"] = mode
            response = client.post("/api/v1/workflows/run-preflight", json={"items": [item]})
            assert response.json()["code"] == expected_code, response.text
            if expected_code == 0:
                assert response.json()["data"]["can_run"] is True
            assert _facts(store, inventory) == before
        finally:
            set_runtime_capabilities(previous)


def test_batch_keeps_known_shortages_when_another_candidate_is_invalid(tmp_path: Path) -> None:
    """一项缺绑定也不能隐藏其他两项已能确定的 16 mL 库存缺口。"""

    with _runtime(tmp_path) as (client, _, _, _, graph, reagent, _):
        item = _item(graph["workflow"]["uuid"], reagent)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [item, item, {**item, "inventory_bindings": []}]})
        report = response.json()["data"]
        assert report["can_run"] is False and report["items"][2]["can_run"] is False
        shortage = report["checks"][0]["details"]["shortages"][0]
        assert shortage["required_quantity"] == 16


def test_batch_null_input_matches_original_single_preflight(tmp_path: Path) -> None:
    """保留原单份将 null 归一为空对象的行为，批量 OpenAPI 也须如实声明。"""

    with _runtime(tmp_path) as (client, _, store, inventory, graph, reagent, _):
        workflow = graph["workflow"]["uuid"]
        item = _item(workflow, reagent)
        item["input"] = None
        before = _facts(store, inventory)
        single = client.post(f"/api/v1/workflows/{workflow}/run-preflight", json={key: value for key, value in item.items() if key != "workflow_uuid"})
        batch = client.post("/api/v1/workflows/run-preflight", json={"items": [item]})
        assert single.json()["code"] == batch.json()["code"] == 0
        original_report = single.json()["data"]
        batch_report = batch.json()["data"]["items"][0]
        original_report.pop("checked_at")
        batch_report.pop("checked_at")
        assert original_report == batch_report
        assert _facts(store, inventory) == before
        schema = client.get("/openapi.json").json()["components"]["schemas"]["WorkflowBatchRunPreflightItem"]
        assert {"type": "null"} in schema["properties"]["input"]["anyOf"]


def test_batch_pending_upstream_input_checks_structure_without_fake_input(tmp_path: Path) -> None:
    """下游缺少上游值不应阻断基础预检；延后项保持可见且不写入数据。"""

    with _runtime(tmp_path) as (client, _, store, inventory, graph, _, dispatcher):
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [{
            "workflow_uuid": graph["workflow"]["uuid"], "input_ready": False,
        }]})
        assert response.status_code == 200, response.text
        assert response.json()["code"] == 0, response.text
        report = response.json()["data"]
        item = report["items"][0]
        assert item["can_run"] is True and item["status"] == "runnable_now"
        assert report["can_run"] is True
        assert any(check["code"] == "execution_plan_ready" for check in item["checks"])
        pending = next(check for check in item["checks"] if check["code"] == "upstream_input_pending")
        assert pending["status"] == "deferred" and pending["blocking"] is False
        assert report["summary"]["blocking_check_count"] == 0
        assert not any(check["code"] == "quantity_inventory_ready" for check in item["checks"])
        assert report["checks"][0]["status"] == "deferred"
        assert _facts(store, inventory) == before
        assert dispatcher.dispatched == []


def test_batch_pending_input_keeps_known_inventory_shortages(tmp_path: Path) -> None:
    """下游输入延后不应隐藏其他完整候选已经能确定的共享库存不足。"""

    with _runtime(tmp_path) as (client, _, store, inventory, graph, reagent, _):
        workflow = graph["workflow"]["uuid"]
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [
            _item(workflow, reagent), _item(workflow, reagent, "B"),
            {"workflow_uuid": workflow, "input_ready": False},
        ]})
        assert response.status_code == 200, response.text
        report = response.json()["data"]
        assert report["can_run"] is False
        assert report["checks"][0]["status"] == "blocked"
        assert report["checks"][0]["details"]["shortages"][0]["required_quantity"] == 16
        assert _facts(store, inventory) == before


def test_batch_rejects_non_boolean_readiness_and_speculative_input(tmp_path: Path) -> None:
    """输入就绪标记必须明确，未就绪时不能夹带可被误用的候选值或库存绑定。"""

    with _runtime(tmp_path) as (client, _, _, _, graph, reagent, _):
        workflow = graph["workflow"]["uuid"]
        for fields in ({"input_ready": "false"}, {"input_ready": False, "input": {"sample": "fake"}}, {"input_ready": False, "inventory_bindings": _item(workflow, reagent)["inventory_bindings"]}):
            response = client.post("/api/v1/workflows/run-preflight", json={"items": [{"workflow_uuid": workflow, **fields}]})
            assert response.status_code in (200, 422), response.text
            assert response.json()["code"] != 0, response.text


def test_pending_input_preserves_real_device_error(tmp_path: Path) -> None:
    """仅未知输入可延后；基础检查已经确认设备物料不存在时仍阻断。"""

    with _runtime(tmp_path, missing_device=True) as (client, _, store, inventory, graph, _, dispatcher):
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [{
            "workflow_uuid": graph["workflow"]["uuid"], "input_ready": False,
        }]})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        report = response.json()["data"]
        item = report["items"][0]
        assert report["can_run"] is False and item["can_run"] is False
        assert item["status"] == "temporarily_unavailable"
        assert any(check["code"] == "device_material_unavailable" and check["blocking"] for check in item["checks"])
        assert any(check["code"] == "upstream_input_pending" and not check["blocking"] for check in item["checks"])
        assert _facts(store, inventory) == before
        assert dispatcher.dispatched == []


def test_pending_input_preserves_invalid_execution_plan(tmp_path: Path) -> None:
    """延后实际输入不能放过无效的工作流定义。"""

    with _runtime(tmp_path) as (client, _, store, inventory, _, _, _):
        created = client.post("/api/v1/workflows", json={"name": "无效人工闸门", "tags": [], "meta_data": {}})
        assert created.status_code == 201, created.text
        workflow = created.json()["data"]["uuid"]
        edited = client.put(f"/api/v1/workflows/{workflow}/graph", json={
            "revision": 1, "nodes": [{
                "uuid": str(uuid4()), "name": "缺少真实动作", "type": "manual_confirm",
                "pose": {}, "param": {}, "manual_confirmation": {"timeout_seconds": 7200},
                "execution_policy": {}, "disabled": False, "minimized": False, "meta_data": {},
            }], "edges": [],
        })
        assert edited.status_code == 200 and edited.json()["code"] == 0, edited.text
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [{"workflow_uuid": workflow, "input_ready": False}]})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        report = response.json()["data"]
        item = report["items"][0]
        assert item["status"] == "invalid" and report["status"] == "invalid"
        assert report["can_run"] is False
        assert any(check["code"] == "execution_plan_invalid" and check["blocking"] for check in item["checks"])
        assert _facts(store, inventory) == before


def test_pending_input_cannot_hide_other_known_invalid_input(tmp_path: Path) -> None:
    """已完整输入里的真实类型错误仍阻断，不能借未知下游放过。"""

    with _runtime(tmp_path) as (client, _, store, inventory, graph, reagent, _):
        workflow = graph["workflow"]["uuid"]
        invalid = _item(workflow, reagent)
        invalid["input"] = {"sample": 7}
        before = _facts(store, inventory)
        response = client.post("/api/v1/workflows/run-preflight", json={"items": [invalid, {"workflow_uuid": workflow, "input_ready": False}]})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        report = response.json()["data"]
        assert report["can_run"] is False
        assert report["items"][0]["can_run"] is False
        assert report["items"][1]["can_run"] is True
        assert report["items"][0]["summary"]["blocking_check_count"] > 0
        assert report["items"][1]["summary"]["blocking_check_count"] == 0
        assert _facts(store, inventory) == before
