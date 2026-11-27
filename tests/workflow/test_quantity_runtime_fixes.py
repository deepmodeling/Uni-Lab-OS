"""数量型库存在运行时来源、补料分支、正式重试与单位换算下的回归。

覆盖：
- B1 来源缺料后经补料分支补液，汇合处消费动作按实际分支提供的容器补做预留并结算；
  无法唯一确定容器时关闭式失败，不能静默不扣量。
- B2 同一消费动作声明两个运行时来源的数量需求时，两个来源都能预留并结算。
- B3 消费动作已知失败后正式重试（attempt 2）成功，恰好结算一次，预留不泄漏。
- B5 需求单位与库存单位可换算（µL/L 对 mL）时先统一到库存单位再判断余量。
- R1 连续扣减到 0 时不残留浮点尾差。

全部经公开 HTTP、生产调度桥与库存权威执行，仅设备派发边界使用 RecordingDispatcher。
"""

from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from tests.app.f05_material_source_http_fixture import action_contract_schema
from tests.app.test_f05_material_source_http_e2e import (
    _create_material,
    _create_resource_template,
)
from tests.app.test_f05_material_source_http_e2e import (
    runtime as _shared_runtime,
)
from tests.workflow.test_authoring_engine import _handle
from tests.workflow.test_f05_material_source_authoring import PLATE_SOURCE_SYMBOL
from tests.workflow.test_f05_material_source_direct_graph import _projection_handle
from tests.workflow.test_material_io_contract import _material_io_catalog
from tests.workflow.test_runtime_material_replenishment import compiled_refill
from unilabos.app.scheduler.dispatch import CommittedJobOutcome
from unilabos.app.scheduler.inventory.workflow_quantity import (
    WorkflowQuantityInventoryAuthority,
    WorkflowQuantityReservationError,
    subtract_quantity,
)
from unilabos.registry.action_template_projection import goal_parameter_schema
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.models import (
    WorkflowEdgeWrite,
    WorkflowInventoryRequirementWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.quantity_inventory import WorkflowQuantityInventory
from unilabos.workflow.template_projection_store import RegistryTemplateProjectionStore

HEADER = '''from lab.devices import Reactor
from lab.resources import plate_96
from unilabos.workflow.authoring import MaterialCustodyPolicy, MaterialFlowRole, device, material_source, material_io, quantity_requirement, resource_ref, workflow, workflow_output
reactor: Reactor = device()
@workflow(workflow_uuid="WORKFLOW_UUID", displayname="数量回归")
def quantity_regression():
'''


def nid(label: str) -> str:
    """返回用例内稳定节点 UUID。"""

    return str(uuid5(NAMESPACE_URL, "quantity-runtime-fixes:" + label))


def src(var: str, label: str, mount: str, *, support: bool = False, level: int = 1,
        site: str | None = None) -> str:
    """生成一个 material_source 声明。"""

    ind = "    " * level
    site_text = f'"{site}"' if site else "None"
    return (
        f"{ind}# unilab:node_uuid={nid(label)}\n"
        f'{ind}{var} = material_source(resource_template=plate_96, mode="existing", '
        f'mount=resource_ref("{mount}"), material_uuid=None, site={site_text}, slot_range=None, '
        "flow_role=MaterialFlowRole.PRIMARY_SAMPLE, "
        f"custody_policy=MaterialCustodyPolicy.TASK_EXCLUSIVE, supports_replenishment={support})\n"
    )


def act(var: str, label: str, plate: str, level: int = 1) -> str:
    """生成一个设备动作。"""

    ind = "    " * level
    return f"{ind}# unilab:node_uuid={nid(label)}\n{ind}{var} = reactor.process_plate(plate={plate})\n"


def refill(var: str, label: str, mount: str, material: str, quantity: float, level: int = 1) -> str:
    """生成一个向原容器补液的 material_io(refill)。"""

    ind = "    " * level
    return (
        f"{ind}# unilab:node_uuid={nid(label)}\n"
        f'{ind}{var} = material_io(mode="refill", resource_template=None, mount=resource_ref("{mount}"), '
        f'site="T1", material={material}, disposition=None, scope=None, timeout_seconds=600, '
        f'reagent={{"quantity": {quantity}, "quantity_unit": "mL"}})\n'
    )


def cond(label: str, expr: str, level: int = 1) -> str:
    """生成一个条件节点。"""

    ind = "    " * level
    return f"{ind}# unilab:node_uuid={nid(label)}\n{ind}if {expr}:\n"


def requirement(key: str, source: str, consume: str, quantity: float, unit: str = "mL") -> dict[str, Any]:
    """构造与作者 quantity_requirement 编译产物同形的图级数量需求。"""

    return {
        "uuid": str(uuid5(NAMESPACE_URL, "quantity-runtime-fixes-req:" + key)),
        "consume_node_uuid": nid(consume),
        "requirement_key": key,
        "target_type": "current_substance",
        "reagent_info_uuid": None,
        "required_quantity": float(quantity),
        "quantity_unit": unit,
        "allow_split": False,
        "meta_data": {"unilab": {
            "material_source_node_uuid": nid(source),
            "quantity_target": "container_content",
            "quantity_binding": {"kind": "literal", "value": quantity},
            "quantity_scale": 1.0,
        }},
    }


class Harness:
    """在生产运行时上安装图、推进任务并读取持久事实。"""

    def __init__(self, rt: Any, root: Path) -> None:
        self.rt = rt
        self.root = root
        self.d: dict[str, str] = {}
        self.task = ""
        self.wf = ""
        self._engine: WorkflowAuthoringEngine | None = None

    def api(self, method: str, path: str, body: Any = None) -> Any:
        response = (
            self.rt.client.request(method, "/api/v1" + path, json=body)
            if body is not None else self.rt.client.request(method, "/api/v1" + path)
        )
        value = response.json()
        assert response.status_code < 300 and value.get("code", 0) == 0, (path, value)
        return value.get("data", value)

    def setup(self) -> None:
        client = self.rt.client
        bottle = self.api("POST", "/resource-templates", {"resources": [{
            "id": "qty.bottle", "display_name": "数量瓶", "registry_type": "material",
            "category": ["container"], "model": {}, "class": {"module": "lab.plate", "type": "python"},
            "handles": [], "metadata": {"capacity": {"max_volume_ul": 1_000_000}},
        }]})["templates"][0]["uuid"]
        mount = _create_resource_template(client, resource_id="qty.mount", display_name="位", registry_type="material")
        self.api("PUT", "/resource-templates/" + mount, {
            "display_name": "位", "registry_type": "material",
            "available_sites": [{"index": "T1", "label": "T1", "content_type": ["qty.bottle"],
                                 "allowed_resource_template_uuids": [bottle]}],
        })
        device = _create_resource_template(client, resource_id="qty.reactor", display_name="派发边界", registry_type="device")
        self.d.update(template=bottle, container_template=mount, device_template=device)
        self.d["device"] = _create_material(client, resource_template_uuid=device, barcode="QTY-DEV", name="QTY-DEV")
        for name in ("WORK", "SPARE", "TARGET", "OTHER"):
            self.d[name] = _create_material(client, resource_template_uuid=mount, barcode="MNT-" + name, name=name)

    def site(self, mount: str) -> str:
        return self.api("GET", f"/materials/{self.d[mount]}/sites")[0]["uuid"]

    def bottle(self, mount: str, name: str, quantity: float | None = None) -> dict[str, Any]:
        """在挂载位放一瓶液体试剂；``quantity`` 为 None 时只放空容器。"""

        material = _create_material(self.rt.client, resource_template_uuid=self.d["template"], barcode=name,
                                    name=name, parent_uuid=self.d[mount], site_uuid=self.site(mount))
        content = None
        if quantity is not None:
            info = self.api("POST", "/reagent-infos", {"name": "水-" + name, "physical_state": "liquid"})["uuid"]
            content = self.api("POST", "/reagents", {
                "material_uuid": material, "reagent_info_uuid": info,
                "quantity": max(quantity, 1.0), "quantity_unit": "mL",
            })
            if quantity != max(quantity, 1.0):
                current = self.api("GET", "/reagents/" + content["uuid"])
                self.api("PUT", "/reagents/" + content["uuid"], {
                    "quantity": quantity, "quantity_unit": "mL", "expected_revision": current.get("revision"),
                })
        return {"material": material, "content": content}

    def qty(self, bottle: dict[str, Any]) -> float:
        return self.api("GET", "/reagents/" + bottle["content"]["uuid"])["quantity"]

    def engine(self) -> WorkflowAuthoringEngine:
        if self._engine is not None:
            return self._engine
        base, _ = compiled_refill()
        io_catalog = _material_io_catalog()
        templates = [a.detached_template() for a in base._catalog.actions] + [a.detached_template() for a in io_catalog.actions]
        handles = [h for a in base._catalog.actions for h in a.detached_handles()] + [h for a in io_catalog.actions for h in a.detached_handles()]
        for template in templates:
            if template["name"] == "process_plate":
                template["resource_template_uuid"] = self.d["device_template"]
                template["type"] = "UniLabJsonCommand"
                template["schema"] = goal_parameter_schema(action_contract_schema())
                template["meta_data"]["unilab"] = {"contract_kind": "typed", "action_contract_schema": action_contract_schema()}
            if template["name"] in ("process_plate", "material_source"):
                for io in ("source", "target"):
                    if not any(h["workflow_node_template_uuid"] == template["uuid"] and h["io_type"] == io
                               and h.get("handle_key") == "ready" for h in handles):
                        handles.append(_handle(str(uuid4()), node_template_uuid=template["uuid"], key="ready",
                                               io_type=io, value_type="any", data_source="dependency"))
        source_template = next(t for t in templates if t["name"] == "material_source")
        handles.append(_handle(str(uuid4()), node_template_uuid=source_template["uuid"], key="mount",
                               io_type="target", value_type="ResourceSlot", required=False, data_source="goal"))
        self._engine = WorkflowAuthoringEngine(catalog=AuthoringCatalogSnapshot.from_entities(
            templates, handles, resource_template_symbols={
                PLATE_SOURCE_SYMBOL: self.d["template"], "lab.resources:box_resource": self.d["container_template"],
            }))
        return self._engine

    def install(self, body: str, requirements: tuple[dict[str, Any], ...] = ()) -> str:
        wf = self.api("POST", "/workflows", {"name": "数量回归", "tags": [], "meta_data": {}})["uuid"]
        text = HEADER.replace("WORKFLOW_UUID", wf) + body + "    return workflow_output()\n"
        applied = self.api("GET", f"/workflows/{wf}/graph")
        result = self.engine().compile(workflow_uuid=wf, workflow_revision=applied["workflow"]["revision"],
                                       python_source=text, source_uri="memory://quantity.py", applied_graph=applied)
        assert result.valid, result.diagnostics
        graph = deepcopy(result.graph)
        reqs = list(graph.get("inventory_requirements") or []) + [dict(item) for item in requirements]
        templates = {t["uuid"]: t for t in graph["node_templates"]}
        handles = [_projection_handle(h, resource_template_uuid=templates[h["workflow_node_template_uuid"]]["resource_template_uuid"],
                                      action_name=templates[h["workflow_node_template_uuid"]]["name"])
                   for h in graph["handle_templates"]]
        RegistryTemplateProjectionStore(self.rt.workflow_store).replace(
            authority_id="quantity-runtime-fixes", node_templates=graph["node_templates"], handle_templates=handles)
        for node in graph["nodes"]:
            if node.get("action_name") == "process_plate":
                node["material_uuid"] = self.d["device"]
                node.setdefault("meta_data", {}).setdefault("unilab", {})["executor_binding"] = {"mode": "fixed", "device_id": "reactor-a"}
        self.rt.workflow_store.save_graph(
            wf, revision=1,
            nodes=[WorkflowNodeWrite.model_validate(n) for n in graph["nodes"]],
            edges=[WorkflowEdgeWrite.model_validate(e) for e in graph["edges"]],
            inventory_requirements=[WorkflowInventoryRequirementWrite.model_validate(r) for r in reqs],
            workflow_meta_data=graph["workflow"].get("meta_data", {}),
        )
        self.wf = wf
        return self.create_task()

    def create_task(self) -> str:
        self.task = self.api("POST", "/workflow-tasks", {"workflow_uuid": self.wf, "run_mode": "normal"})["uuid"]
        return self.task

    def jobs(self) -> list[dict[str, Any]]:
        return self.api("GET", f"/workflow-tasks/{self.task}/jobs")

    def job(self, label: str) -> dict[str, Any]:
        rows = [j for j in self.jobs() if j["workflow_node_uuid"] == nid(label)]
        return max(rows, key=lambda row: row["attempt"])

    def outcome(self, job_uuid: str, state: str = "succeeded") -> Any:
        return self.rt.scheduler.on_job_outcome(job_uuid, CommittedJobOutcome(
            outcome=state, return_info={"success": state == "succeeded"},
            error_info=[] if state == "succeeded" else [{"code": "controlled", "message": "known stopped"}],
            unknown_command_ids=[], inventory_consumptions=[],
        ))

    def pump(self, rounds: int = 3) -> None:
        for _ in range(rounds):
            self.rt.scheduler.reschedule()
            time.sleep(0.02)

    def run(self, *, hold: frozenset[str] = frozenset(), max_steps: int = 40) -> dict[str, Any]:
        """批准全部 IO 并成功完成全部派发；``hold`` 中的节点保持在途。"""

        seen: set[str] = set()
        for _ in range(max_steps):
            progressed = False
            for job in self.jobs():
                if job["executor_kind"] in ("material_io", "manual_confirm") and job["status"] == "running" and job["uuid"] not in seen:
                    seen.add(job["uuid"])
                    self.api("POST", f"/workflow-node-jobs/{job['uuid']}/manual-confirmation", {"action": "approve"})
                    progressed = True
            by_uuid = {job["uuid"]: job for job in self.jobs()}
            for command in list(self.rt.dispatcher.dispatched):
                job_uuid = command["job_id"]
                if job_uuid in seen or job_uuid not in by_uuid:
                    continue
                seen.add(job_uuid)
                if by_uuid[job_uuid]["workflow_node_uuid"] in hold:
                    continue
                self.outcome(job_uuid)
                progressed = True
            if not progressed:
                self.pump(2)
                if not any(j["executor_kind"] in ("material_io", "manual_confirm") and j["status"] == "running"
                           and j["uuid"] not in seen for j in self.jobs()) and all(
                    c["job_id"] in seen for c in self.rt.dispatcher.dispatched if c["job_id"] in by_uuid
                ):
                    break
        return self.api("GET", f"/workflow-tasks/{self.task}")

    def decision_retry(self, job_uuid: str) -> None:
        state = self.api("GET", "/stations/local/error-handling")
        error = next(e for e in state["errors"] if e["source_job_uuid"] == job_uuid)
        response = self.rt.client.post(
            f"/api/v1/stations/local/errors/{error['decision_id']}/decision",
            json={"action": "retry_current_node", "expected_decision_version": error["decision_version"],
                  "expected_station_version": state["station_version"], "observed_error_epoch": state["error_epoch"],
                  "resume_requested": True, "operator_confirmed": True, "confirm_snapshot_id": state["snapshot_id"]},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code < 300 and response.json().get("code", 0) == 0, response.json()
        self.pump(3)

    def reservations(self) -> list[dict[str, Any]]:
        rows = self.rt.inventory.store.query_all(
            "SELECT * FROM inventory_reservation WHERE workflow_id=? ORDER BY node_id", (self.task,))
        result = []
        for row in rows:
            payload = json.loads(row["amounts_json"] or "{}")
            if isinstance(payload, dict) and payload.get("kind") == "workflow_quantity":
                result.append({"node_id": row["node_id"], "status": row["status"], "allocations": payload["allocations"]})
        return result

    def active_quantity_reservations(self) -> list[dict[str, Any]]:
        rows = self.rt.inventory.store.query_all("SELECT * FROM inventory_reservation WHERE status='active'")
        return [row for row in rows if '"workflow_quantity"' in (row["amounts_json"] or "")]

    def allocations(self) -> list[dict[str, Any]]:
        with self.rt.workflow_store.transaction() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM workflow_inventory_allocation WHERE workflow_task_uuid=? ORDER BY requirement_key",
                (self.task,)).fetchall()]

    def consumptions(self) -> list[dict[str, Any]]:
        return WorkflowQuantityInventory(self.rt.workflow_store, self.rt.inventory).list_task_consumptions(self.task)


@pytest.fixture()
def h(tmp_path: Path):
    """生产运行时（同 tests/app/test_f05_material_source_http_e2e.runtime）。"""

    from unilabos.app.runtime_capabilities import reset_runtime_capabilities

    reset_runtime_capabilities()
    generator = _shared_runtime.__wrapped__(tmp_path)
    harness = Harness(next(generator), tmp_path)
    harness.setup()
    try:
        yield harness
    finally:
        try:
            generator.close()
        finally:
            reset_runtime_capabilities()


def refill_graph(d: dict[str, str]) -> str:
    """支持补料的运行时来源：有料直接消费，缺料补液后进入汇合（不含消费）。"""

    return (src("pre_plate", "pre_src", d["TARGET"]) + act("pre", "pre", "pre_plate")
            + src("original", "original", d["WORK"], support=True)
            + cond("cond", "original.sufficient")
            + act("used", "used", "original", level=2)
            + "    else:\n"
            + src("recheck", "recheck", d["WORK"], level=2)
            + refill("refilled", "refilled", d["WORK"], "recheck", 1, level=2))


def sz48_graph(d: dict[str, str], *, ambiguous_else: bool = False) -> str:
    """SZ-48 同构：需求 source=运行时来源 original，consume=分支汇合后的加液动作。"""

    body = (src("original", "original", d["WORK"], support=True)
            + src("target", "target", d["TARGET"])
            + cond("cond", "original.sufficient")
            + refill("topped", "topped", d["WORK"], "original", 1, level=2)
            + "    else:\n"
            + src("recheck", "recheck", d["WORK"], level=2)
            + refill("refilled", "refilled", d["WORK"], "recheck", 1, level=2))
    if ambiguous_else:
        # 缺料分支出口是设备动作且分支内出现两个不同容器：无法唯一确定消费容器。
        body += src("other", "other", d["OTHER"], level=2) + act("still", "still", "other", level=2)
    return (body + act("dosed", "dosed", "target")
            + '    quantity_requirement(requirement_key="dose", source=original, consume=dosed, quantity=0.1, quantity_unit="mL")\n')


@pytest.mark.parametrize("stock,branch,expected_after", [(0.2, "if", 1.1), (0.05, "else", 0.95)])
def test_shortage_branch_refill_join_consumer_settles_declared_quantity_once(
    h: Harness, stock: float, branch: str, expected_after: float
) -> None:
    """B1：缺料分支补液后，汇合处消费动作按补料分支提供的容器预留并结算恰好一次。"""

    work = h.bottle("WORK", "WORK", stock)
    h.bottle("TARGET", "TARGET")
    h.install(sz48_graph(h.d))
    final = h.run()
    assert final["status"] == "succeeded", final
    assert h.job("cond")["return_info"]["selected_branch"] == branch
    dosed = h.job("dosed")["uuid"]
    consumptions = [c for c in h.consumptions() if c["workflow_node_job_uuid"] == dosed]
    assert len(consumptions) == 1
    assert math.isclose(consumptions[0]["actual_quantity"], 0.1)
    assert consumptions[0]["inventory_uuid"] == work["content"]["uuid"]
    assert math.isclose(h.qty(work), expected_after, abs_tol=1e-9)
    assert [a["requirement_key"] for a in h.allocations()] == ["dose"]
    assert [a["status"] for a in h.allocations()] == ["consumed"]
    assert [r["status"] for r in h.reservations()] == ["consumed"]
    assert h.active_quantity_reservations() == []
    # 同一成功回执重放不重复预留、不重复扣减。
    h.outcome(dosed)
    assert len(h.consumptions()) == 1 and math.isclose(h.qty(work), expected_after, abs_tol=1e-9)


def test_shortage_branch_without_unique_container_fails_closed(h: Harness) -> None:
    """B1：缺料分支给不出唯一容器时，消费动作在派发前失败，不能静默不扣量。"""

    work = h.bottle("WORK", "WORK", 0.05)
    other = h.bottle("OTHER", "OTHER", 5.0)
    h.bottle("TARGET", "TARGET")
    h.install(sz48_graph(h.d, ambiguous_else=True))
    h.run(hold=frozenset({nid("dosed")}))
    assert h.job("cond")["return_info"]["selected_branch"] == "else"
    dosed = h.job("dosed")
    # 派发前补做数量预留失败：该节点不越过物理边界，设备不会以不确定的容器动作。
    assert dosed["uuid"] not in [c["job_id"] for c in h.rt.dispatcher.dispatched]
    assert h.job("dosed")["status"] != "succeeded"
    assert h.consumptions() == []
    assert h.allocations() == [] and h.reservations() == []
    assert math.isclose(h.qty(work), 1.05) and math.isclose(h.qty(other), 5.0)


def test_two_runtime_sources_consumed_by_same_action_are_both_reserved_and_settled(h: Harness) -> None:
    """B2：同一动作消费两个运行时来源的数量需求，后到来源并入同一 Job 预留。"""

    a = h.bottle("WORK", "LIQ-A", 5.0)
    b = h.bottle("SPARE", "LIQ-B", 5.0)
    h.bottle("TARGET", "TARGET")
    d = h.d
    body = (src("liquid_a", "liquid_a", d["WORK"], support=True)
            + cond("cond_a", "liquid_a.sufficient") + act("seen_a", "seen_a", "liquid_a", level=2)
            + "    else:\n" + src("re_a", "re_a", d["WORK"], level=2) + refill("fill_a", "fill_a", d["WORK"], "re_a", 1, level=2)
            + src("liquid_b", "liquid_b", d["SPARE"], support=True)
            + cond("cond_b", "liquid_b.sufficient") + act("seen_b", "seen_b", "liquid_b", level=2)
            + "    else:\n" + src("re_b", "re_b", d["SPARE"], level=2) + refill("fill_b", "fill_b", d["SPARE"], "re_b", 1, level=2)
            + src("target", "target", d["TARGET"]) + act("mix", "mix", "target")
            + '    quantity_requirement(requirement_key="a_dose", source=liquid_a, consume=mix, quantity=0.1, quantity_unit="mL")\n'
            + '    quantity_requirement(requirement_key="b_dose", source=liquid_b, consume=mix, quantity=0.2, quantity_unit="mL")\n')
    h.install(body)
    h.run(hold=frozenset({nid("mix")}))
    assert h.job("liquid_a")["return_info"]["sufficient"] is True
    assert h.job("liquid_b")["return_info"]["sufficient"] is True
    mix = h.job("mix")["uuid"]
    held = h.reservations()
    assert [r["node_id"] for r in held] == [mix] and held[0]["status"] == "active"
    assert sorted(item["requirement_key"] for item in held[0]["allocations"]) == ["a_dose", "b_dose"]
    h.outcome(mix)
    h.pump()
    assert h.api("GET", f"/workflow-tasks/{h.task}")["status"] == "succeeded"
    assert sorted(x["requirement_key"] for x in h.allocations()) == ["a_dose", "b_dose"]
    assert math.isclose(h.qty(a), 4.9) and math.isclose(h.qty(b), 4.8)
    assert len(h.consumptions()) == 2
    assert h.active_quantity_reservations() == []


def test_runtime_incremental_reservation_rejects_changed_allocation_for_same_job(tmp_path: Path) -> None:
    """B2：同一 Job 的追加预留仍拒绝同一分配身份被改写。"""

    from tests.workflow.quantity_inventory_test_support import _inventory

    store, material_uuid, _, reagent_uuid = _inventory(tmp_path)
    authority = WorkflowQuantityInventoryAuthority(store, edge_id="edge", lab_id="lab")
    task, job = str(uuid4()), str(uuid4())

    def allocation(key: str, quantity: float) -> dict[str, Any]:
        return {"uuid": str(uuid5(NAMESPACE_URL, key)), "workflow_task_uuid": task, "workflow_node_job_uuid": job,
                "requirement_key": key, "inventory_type": "reagent", "inventory_uuid": reagent_uuid,
                "material_uuid": material_uuid, "reserved_quantity": quantity, "quantity_unit": "mL"}

    try:
        authority.reserve_task(task, [allocation("a", 1.0)], incremental=True)
        authority.reserve_task(task, [allocation("b", 2.0)], incremental=True)
        authority.reserve_task(task, [allocation("b", 2.0)], incremental=True)  # 重放幂等
        rows = store.query_all("SELECT * FROM inventory_reservation WHERE workflow_id=?", (task,))
        assert len(rows) == 1 and rows[0]["status"] == "active"
        assert sorted(i["requirement_key"] for i in json.loads(rows[0]["amounts_json"])["allocations"]) == ["a", "b"]
        with pytest.raises(WorkflowQuantityReservationError, match="首次提交冲突"):
            authority.reserve_task(task, [allocation("b", 3.0)], incremental=True)
        with pytest.raises(WorkflowQuantityReservationError, match="不足"):
            authority.reserve_task(task, [allocation("c", 7.5)], incremental=True)
        assert len(json.loads(store.query_all(
            "SELECT amounts_json FROM inventory_reservation WHERE workflow_id=?", (task,))[0]["amounts_json"])["allocations"]) == 2
    finally:
        store.close()


@pytest.mark.parametrize("runtime", [False, True], ids=["static", "runtime"])
def test_formal_retry_of_failed_consumer_settles_declared_quantity_exactly_once(h: Harness, runtime: bool) -> None:
    """B3：消费动作已知失败后正式重试成功，由重试作业结算恰好一次，预留不泄漏。"""

    work = h.bottle("WORK", "WORK", 1.0)
    if runtime:
        h.bottle("TARGET", "PRE")
        h.install(refill_graph(h.d), (requirement("dose", "original", "used", 0.1),))
        h.outcome(h.job("pre")["uuid"])
        h.pump()
        assert h.job("original")["return_info"]["sufficient"] is True
        label = "used"
    else:
        h.install(src("bottle", "bottle", h.d["WORK"], site=h.site("WORK")) + act("dosed", "dosed", "bottle")
                  + '    quantity_requirement(requirement_key="dose", source=bottle, consume=dosed, quantity=0.1, quantity_unit="mL")\n')
        h.pump()
        label = "dosed"
    first = h.job(label)["uuid"]
    h.outcome(first, "failed")
    assert h.qty(work) == 1.0 and h.consumptions() == []
    h.decision_retry(first)
    retry = h.job(label)
    assert retry["uuid"] != first and retry["attempt"] == 2
    h.outcome(retry["uuid"])
    h.pump()
    assert h.api("GET", f"/workflow-tasks/{h.task}")["status"] == "succeeded"
    consumptions = h.consumptions()
    assert len(consumptions) == 1 and consumptions[0]["workflow_node_job_uuid"] == retry["uuid"]
    assert math.isclose(h.qty(work), 0.9)
    assert [a["status"] for a in h.allocations()] == ["consumed"]
    assert h.allocations()[0]["workflow_node_job_uuid"] == retry["uuid"]
    assert [r["status"] for r in h.reservations()] == ["consumed"]
    assert h.active_quantity_reservations() == []
    h.outcome(retry["uuid"])  # 重放幂等
    assert len(h.consumptions()) == 1 and math.isclose(h.qty(work), 0.9)


@pytest.mark.parametrize("unit,quantity,sufficient,after", [
    ("uL", 100.0, True, 4.9),
    ("µL", 100.0, True, 4.9),
    ("L", 0.0001, True, 4.9),
    ("uL", 6000.0, False, 6.0),   # 6 mL > 5 mL：换算后是缺料，进入补料分支而不是报错
])
def test_convertible_requirement_unit_is_unified_to_inventory_unit(
    h: Harness, unit: str, quantity: float, sufficient: bool, after: float
) -> None:
    """B5：需求单位可换算时先统一到库存单位再判断余量；预留以库存单位记录。"""

    work = h.bottle("WORK", "WORK", 5.0)
    h.bottle("TARGET", "PRE")
    h.install(refill_graph(h.d), (requirement("dose", "original", "used", quantity, unit),))
    h.outcome(h.job("pre")["uuid"])
    h.pump()
    source = h.job("original")
    assert source["status"] == "succeeded", source.get("error_info")
    assert source["return_info"]["sufficient"] is sufficient
    if sufficient:
        allocation = h.reservations()[0]["allocations"][0]
        assert allocation["quantity_unit"] == "mL" and math.isclose(allocation["reserved_quantity"], 0.1)
    final = h.run()
    assert final["status"] == "succeeded"
    assert math.isclose(h.qty(work), after, abs_tol=1e-9)
    assert h.active_quantity_reservations() == []


def test_unconvertible_requirement_unit_still_fails_source_check(h: Harness) -> None:
    """B5：不可换算单位（体积库存对质量需求且无密度）保持报错，不当作缺料。"""

    work = h.bottle("WORK", "WORK", 5.0)
    h.bottle("TARGET", "PRE")
    h.install(refill_graph(h.d), (requirement("dose", "original", "used", 1.0, "g"),))
    h.outcome(h.job("pre")["uuid"])
    h.pump()
    source = h.job("original")
    assert source["status"] == "failed"
    assert (source.get("return_info") or {}).get("sufficient") is None
    assert h.reservations() == [] and h.qty(work) == 5.0


def test_sequential_consumptions_reach_exact_zero_without_float_residue(h: Harness) -> None:
    """R1：0.1+0.2 mL 连续三次扣 0.1 mL 后余量恰为 0，下一次检查判定缺料。"""

    work = h.bottle("WORK", "WORK", 0.1 + 0.2)
    h.bottle("TARGET", "PRE")
    h.install(refill_graph(h.d), (requirement("dose", "original", "used", 0.1),))
    seen = []
    for index in range(3):
        if index:
            h.create_task()
        assert h.run()["status"] == "succeeded"
        assert h.job("original")["return_info"]["sufficient"] is True
        seen.append(h.qty(work))
    assert math.isclose(seen[0], 0.2) and math.isclose(seen[1], 0.1)
    assert seen[-1] == 0.0, seen
    h.create_task()
    h.pump()
    h.outcome(h.job("pre")["uuid"])
    h.pump()
    assert h.job("original")["return_info"]["sufficient"] is False


@pytest.mark.parametrize("available,consumed,expected", [
    (0.1 + 0.2 - 0.1 - 0.1, 0.1, 0.0),       # 2.78e-17 正尾差归零
    (0.3 - 0.1 - 0.1, 0.1, 0.0),             # 负尾差归零
    (30.0, 0.1, 29.9),
    (1000.0, 999.0, 1.0),
])
def test_subtract_quantity_zeroes_rounding_residue_only(available: float, consumed: float, expected: float) -> None:
    """R1：极小余量按相对容差归零，真实余量保持不变。"""

    assert subtract_quantity(available, consumed) == pytest.approx(expected, abs=0)
    if expected == 0.0:
        assert subtract_quantity(available, consumed) == 0.0


def test_subtract_quantity_keeps_small_real_remainder_and_rejects_real_overdraw() -> None:
    """R1：归零容差不能吞掉真实小余量或真实超扣。"""

    assert subtract_quantity(1.0, 0.999999) == pytest.approx(1e-6)
    with pytest.raises(WorkflowQuantityReservationError, match="余量不足"):
        subtract_quantity(1000.0, 1000.00001)
