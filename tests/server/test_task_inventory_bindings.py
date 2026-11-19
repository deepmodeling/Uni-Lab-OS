"""任务的 ``inventory_bindings`` 决定库存需求实际预留哪份试剂 / 当前内容物（V1 ``prepare_task_allocations``）。

绑定在建任务时落到冻结计划里对应的需求：调度器、整任务预留与运行预检都按绑定的来源工作；
对不上声明需求的绑定一律拒绝，不静默丢弃（F03）。
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from tests.server.test_scheduler_inventory_authority import _inbound, _template
from tests.server.test_workflow_inventory_recovery import Stack
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from unilabos.registry.workflows import upsert_workflow
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.materials.contents import ContentContract
from unilabos.server.services.materials.resource_contract import ResourceContract
from unilabos.server.services.runtime.workflow.service import WorkflowError


class Lab:
    """两只装 PBS 的瓶子（A 为工作流声明的默认来源，B 为前端另选的瓶子）和一个当前内容物。"""

    def __init__(self, tmp_path, *, quantity_a: float = 100, quantity_b: float = 100) -> None:
        self.stack = Stack(tmp_path)
        self.contents = ContentContract(self.stack.materials, compound_source=None)
        contract = ResourceContract(self.stack.materials)
        template = contract.sync_resource_templates([{"id": "bottle", "registry_type": "resource"}])["templates"][0]
        info = self.contents.create_reagent_info({"name": "PBS", "physical_state": "liquid"})
        self.bottles, self.reagents = [], []
        for name, quantity in (("瓶 A", quantity_a), ("瓶 B", quantity_b)):
            bottle = contract.create_material({"resource_template_uuid": template["uuid"], "name": name})["uuid"]
            reagent = self.contents.create_reagent({"material_uuid": bottle, "reagent_info_uuid": info["uuid"],
                                                    "quantity": quantity, "quantity_unit": "ml"})
            self.bottles.append(bottle)
            self.reagents.append(reagent["uuid"])
        mixture = contract.create_material({"resource_template_uuid": template["uuid"], "name": "混合瓶"})["uuid"]
        self.current = self.contents.create_current_substance(
            {"material_uuid": mixture, "name": "混合液", "quantity": 50, "quantity_unit": "ml"})["uuid"]
        self.stack.workflow.configure_preflight(materials=self.stack.client)
        self.executor = Executor()
        self.scheduler = BackendScheduler(self.stack.workflow, self.executor, materials_gateway=self.stack.client)
        self.stack.workflow.set_task_submitter(self.scheduler.submit)

    def close(self) -> None:
        self.scheduler.stop()
        self.stack.close()

    def workflow(self, requirements: list[dict[str, Any]]) -> dict[str, Any]:
        node = {"uuid": str(uuid4()), "name": "加缓冲液", "type": "device_action", "material_uuid": str(uuid4()),
                "action_name": "dose", "meta_data": {"target_device_id": "pipette", "inventory_requirements": requirements},
                "param": {}}
        return upsert_workflow(self.stack.workflow, {"workflow_uuid": str(uuid4()), "name": "加液", "description": "",
                                                     "tags": [], "nodes": [node], "edges": []})

    def buffer(self, quantity: float = 30) -> dict[str, Any]:
        return {"key": "buffer", "kind": "content", "material_uuid": self.bottles[0], "substance_name": "PBS",
                "quantity": quantity, "unit": "ml"}

    def create(self, workflow: dict[str, Any], bindings: list[dict[str, Any]]) -> dict[str, Any]:
        return self.stack.workflow.create_workflow_task(
            workflow_uuid=workflow["uuid"], run_mode="normal", target_node_uuid=None, input_value={},
            description=None, meta_data={}, inventory_bindings=bindings)

    def quantity(self, index: int) -> float:
        return self.contents.get_reagent(self.reagents[index])["quantity"]


def _binding(inventory_uuid: str = "", **overrides: Any) -> dict[str, Any]:
    return {"requirement_key": "buffer", "inventory_type": "reagent", "inventory_uuid": inventory_uuid,
            "reserved_quantity": 30.0, "quantity_unit": "ml", **overrides}


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    yield value
    value.close()


def test_binding_selects_the_reagent_the_task_reserves_and_consumes(lab) -> None:
    task = lab.create(lab.workflow([lab.buffer()]), [_binding(lab.reagents[1])])
    assert task["inventory_bindings"] == [_binding(lab.reagents[1])]
    [planned] = task["execution_plan"]["nodes"][0]["inventory_requirements"]
    assert (planned["material_uuid"], planned["substance_uuid"]) == (lab.bottles[1], lab.reagents[1])
    assert "substance_name" not in planned
    wait_for(lambda: len(lab.executor.dispatched) == 1)
    [reservation] = lab.stack.client.list_inventory_reservations(task_uuid=task["uuid"])
    [item] = reservation.items
    assert (item.material_uuid, item.substance_uuid, item.quantity) == (lab.bottles[1], lab.reagents[1], 30.0)
    assert lab.executor.dispatched[0]["inventory_requirements"][0]["substance_uuid"] == lab.reagents[1]
    assert lab.contents.get_reagent(lab.reagents[1])["active_workflow_reserved_quantity"] == 30
    assert lab.contents.get_reagent(lab.reagents[0])["active_workflow_reserved_quantity"] == 0
    lab.executor.finish(0, {})
    wait_for(lambda: lab.stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
    lab.scheduler._reconcile_inventory()  # noqa: SLF001 - 终态结算
    assert (lab.quantity(0), lab.quantity(1)) == (100, 70)  # 扣的是绑定的瓶 B，不是工作流写死的瓶 A


def test_binding_can_name_the_step_and_select_a_current_substance(lab) -> None:
    workflow = lab.workflow([lab.buffer()])
    node_uuid = lab.stack.workflow.get_graph(workflow["uuid"])["nodes"][0]["uuid"]
    task = lab.create(workflow, [_binding(lab.current, requirement_key=f"{node_uuid}/buffer",
                                          inventory_type="current_substance")])
    [planned] = task["execution_plan"]["nodes"][0]["inventory_requirements"]
    assert planned["substance_uuid"] == lab.current
    wait_for(lambda: len(lab.executor.dispatched) == 1)


@pytest.mark.parametrize(("requirements", "binding", "message"), [
    ("buffer", {"requirement_key": "solvent"}, "未对应本次执行的库存需求"),
    ("lot", {}, "只有容器内容物（content）需求能绑定"),
    ("buffer", {"quantity_unit": "ul"}, "必须使用需求声明的单位 ml"),
    ("buffer", {"reserved_quantity": 20.0}, "预留 20，应为 30"),
    ("buffer", {"inventory_type": "current_substance"}, "不存在"),  # 试剂 uuid 不是当前内容物
    ("buffer", {"inventory_uuid": str(uuid4())}, "不存在"),
    ("dynamic", {}, "数量由入口参数或上游结果决定"),
    ("twice", {}, "出现在多个步骤中"),
])
def test_bindings_that_do_not_match_a_declared_requirement_are_rejected(lab, requirements, binding, message) -> None:
    _template(lab.stack.materials, "reagent-template", "reagent")
    _inbound(lab.stack.materials, lot_uuid="lot-a", quantity=100)
    if requirements == "lot":
        workflow = lab.workflow([{"key": "buffer", "kind": "lot", "template_uuid": "reagent-template",
                                  "quantity": 30, "unit": "ml"}])
    elif requirements == "dynamic":
        measure = {"uuid": str(uuid4()), "name": "称量", "type": "device_action", "material_uuid": str(uuid4()),
                   "action_name": "measure", "param": {}, "meta_data": {"target_device_id": "balance"}}
        amount = {"kind": "node_output", "workflow_node_uuid": measure["uuid"], "data_key": "amount"}
        dose = {"uuid": str(uuid4()), "name": "加缓冲液", "type": "device_action", "material_uuid": str(uuid4()),
                "action_name": "dose", "param": {}, "execution_policy": {"depends_on": [measure["uuid"]]},
                "meta_data": {"target_device_id": "pipette", "inventory_requirements": [{**lab.buffer(), "quantity": amount}]}}
        workflow = upsert_workflow(lab.stack.workflow, {"workflow_uuid": str(uuid4()), "name": "按称量加液",
                                                        "description": "", "tags": [], "nodes": [measure, dose], "edges": []})
    elif requirements == "twice":
        first, second = lab.buffer(), lab.buffer()
        nodes = [{"uuid": str(uuid4()), "name": f"加缓冲液{index}", "type": "device_action",
                  "material_uuid": str(uuid4()), "action_name": "dose", "param": {},
                  "meta_data": {"target_device_id": "pipette", "inventory_requirements": [item]}}
                 for index, item in enumerate((first, second))]
        workflow = upsert_workflow(lab.stack.workflow, {"workflow_uuid": str(uuid4()), "name": "两步", "description": "",
                                                        "tags": [], "nodes": nodes, "edges": []})
    else:
        workflow = lab.workflow([lab.buffer()])
    before = lab.stack.workflow.list_workflow_tasks()["total"]
    with pytest.raises(WorkflowError) as caught:
        lab.create(workflow, [_binding(**{"inventory_uuid": lab.reagents[1], **binding})])
    assert caught.value.code == "invalid_input" and message in caught.value.message
    assert lab.stack.workflow.list_workflow_tasks()["total"] == before
    assert lab.executor.dispatched == []


def test_binding_without_a_materials_authority_is_not_silently_dropped(lab) -> None:
    workflow = lab.workflow([lab.buffer()])
    lab.stack.workflow.configure_preflight(materials=None)
    with pytest.raises(WorkflowError) as caught:
        lab.create(workflow, [_binding(lab.reagents[1])])
    assert caught.value.code == "invalid_input" and "库存权威尚未接入" in caught.value.message


def test_run_preflight_reports_inventory_against_the_bound_source(tmp_path) -> None:
    lab = Lab(tmp_path, quantity_a=100, quantity_b=10)
    try:
        workflow = lab.workflow([lab.buffer()])

        def preflight(bindings):
            return lab.stack.workflow.preflight_workflow(workflow_uuid=workflow["uuid"], run_mode="normal",
                                                         target_node_uuid=None, input_value={},
                                                         inventory_bindings=bindings)

        def inventory(report):
            return next(item for item in report["checks"] if item["code"].startswith("inventory_")
                        and item["code"] != "inventory_bindings_applied")

        assert inventory(preflight([]))["status"] == "passed"  # 默认的瓶 A 足够
        bound = preflight([_binding(lab.reagents[1])])
        check = inventory(bound)
        assert (check["status"], check["code"]) == ("blocked", "inventory_shortage")
        [shortage] = check["details"]["shortages"]
        assert (shortage["substance_uuid"], shortage["shortage"]) == (lab.reagents[1], pytest.approx(20.0))
        assert bound["status"] == "temporarily_unavailable"
        invalid = preflight([_binding(lab.reagents[1], requirement_key="solvent")])
        assert invalid["status"] == "invalid"
        assert [item["code"] for item in invalid["checks"] if item["blocking"]] == ["invalid_inventory_binding"]
    finally:
        lab.close()


def test_http_materials_client_reads_bound_inventory_from_the_v1_routes(monkeypatch) -> None:
    from unilabos.client.materials.core import HTTPMaterialsClient

    client = HTTPMaterialsClient("http://authority:8002")
    calls = []
    monkeypatch.setattr(client, "_get", lambda path: calls.append(path) or {"uuid": "s", "material_uuid": "m"})
    client.get_inventory_content("reagent", "r-1")
    client.get_inventory_content("current_substance", "c-1")
    assert calls == ["/reagents/r-1", "/current-substances/c-1"]
