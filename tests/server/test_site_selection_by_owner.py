"""库位参数按所属物料选择：``{"owner_uuid": ...}`` 在任务准入时选定、经入口预留占住并冻结进任务。

候选是所属物料下接受放入物料（``MaterialRole.site_picker`` 的 for_material）的库位（模板白名单 / 允许的资源类别），按库位序号选第一个
空着且没被预留的；没有接受的库位是预检错误（``reason=no_acceptable_site``），接受的都被占着时任务排队
（``wait_reason.code=site_group_unavailable``）。精确库位 uuid 照旧。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.server.test_scheduler_inventory_authority import _mutation
from tests.server.test_workflow_inventory_recovery import Stack
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from unilabos.protocol.materials import (
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialTreeCreate,
    ResourceTemplateWrite,
    SiteCreate,
)
from unilabos.protocol.runtime.site_references import site_under_owner
from unilabos.registry.workflows import upsert_workflow
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowError

_LOCK = {"type": "object", "properties": {"uuid": {"type": "string", "format": "uuid"}}, "required": ["uuid"],
         "x-unilabos-material-lock": True}
_SELECTOR = {"version": 1, "owner": "heater", "occupant": "sample", "show_occupied": True, "allow_occupied": False}


def _schema(selector: dict | None = _SELECTOR) -> dict:
    site = {"type": "string", "format": "uuid"}
    if selector is not None:
        site["x-unilabos-site-selector"] = dict(selector)
    return {
        "x-unilabos-action-contract": {"version": 2},
        "properties": {
            "goal": {"type": "object", "properties": {"heater": _LOCK, "sample": _LOCK, "site": site},
                     "required": ["heater", "sample", "site"]},
            "result": {},
        },
    }


def _identity(name: str, template: str) -> MaterialIdentityWrite:
    return MaterialIdentityWrite(resource_id=f"{name}-{uuid4()}", name=f"{name}-{uuid4()}", template_name=template,
                                 resource_type="container", class_name="Container")


def _seed(stack: Stack, *, categories: tuple[str, ...] = ("other", "heating", "heating", "heating"),
          occupied: tuple[int, ...] = (1,), samples: int = 2) -> dict:
    """一个加热台：按序号排的工位（每个工位只接受一种类别），``occupied`` 序号的工位上已有一个样品；外加几个待放的样品。"""

    from unilabos.server.services.materials.core import MaterialNoChangeError

    for name, category in (("heater", []), ("sample", ["heating"])):
        try:
            stack.materials.put_template(_mutation("put_template"), ResourceTemplateWrite(
                template_uuid=f"{name}-template", name=name, display_name=name, resource_type="container",
                class_name="Container", category=category))
        except MaterialNoChangeError:
            pass
    nodes = [MaterialNodeCreate(client_ref="heater", identity=_identity("heater", "heater"), sites=[
        SiteCreate(template_name="slot", site_index=index, label=f"S{index}", allowed_resource_categories=[category],
                   occupied_client_ref=f"blocker-{index}" if index in occupied else None)
        for index, category in enumerate(categories)])]
    nodes += [MaterialNodeCreate(client_ref=f"blocker-{index}", parent_client_ref="heater",
                                 identity=_identity("blocker", "sample")) for index in occupied]
    tree = stack.materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=nodes)).data
    heater = tree.client_ref_map["heater"]
    sites = {site.label: site.site_uuid for site in stack.materials.get_material(heater).sites}
    return {"heater": heater, "sites": sites, "samples": [_sample(stack) for _ in range(samples)]}


def _sample(stack: Stack) -> str:
    tree = stack.materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[
        MaterialNodeCreate(client_ref="sample", identity=_identity("sample", "sample"))])).data
    return tree.client_ref_map["sample"]


def _node(param: dict, **extra) -> dict:
    return {"uuid": str(uuid4()), "name": "放到加热台", "type": "device_action", "material_uuid": str(uuid4()),
            "action_name": "place", "meta_data": {"target_device_id": "robot"}, "param": param, **extra}


def _workflow(stack: Stack, nodes: list[dict], *, schema: dict | None = None, io: dict | None = None) -> dict:
    stack.workflow.configure_preflight(materials=stack.client, endpoints=lambda: [],
                                       action_schema_resolver=lambda *_: schema or _schema())
    payload = {"workflow_uuid": str(uuid4()), "name": "按所属物料选库位", "description": "", "tags": [], "edges": [],
               "nodes": nodes}
    if io is not None:
        payload["io"] = io
    return upsert_workflow(stack.workflow, payload)


def _create(stack: Stack, record: dict, input_value: dict | None = None) -> dict:
    return stack.workflow.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
                                               input_value=input_value or {}, description=None, meta_data={})


def _scheduler(stack: Stack, executor: Executor) -> BackendScheduler:
    scheduler = BackendScheduler(stack.workflow, executor, materials_gateway=stack.client)
    stack.workflow.set_task_submitter(scheduler.submit)
    return scheduler


def _reservations(stack: Stack, task_uuid: str) -> list[dict]:
    from unilabos.server.services.materials.ingress import IngressReservations

    return [item for item in IngressReservations(stack.materials).list() if item["backend_task_uuid"] == task_uuid]


def _status(stack: Stack, task_uuid: str) -> str:
    return stack.workflow.get_workflow_task(task_uuid)["status"]


def test_admission_picks_the_first_acceptable_free_site_reserves_and_freezes_it(tmp_path) -> None:
    stack, executor = Stack(tmp_path), Executor()
    scheduler = _scheduler(stack, executor)
    try:
        seed = _seed(stack)
        sample = seed["samples"][0]
        record = _workflow(stack, [_node({"heater": {"uuid": seed["heater"]}, "sample": {"uuid": sample},
                                          "site": site_under_owner(seed["heater"])})])
        task = _create(stack, record)
        assert task["execution_plan"]["nodes"][0]["param"]["site"] == {"owner_uuid": seed["heater"]}
        wait_for(lambda: len(executor.dispatched) == 1)
        # S0 只接受别的类别、S1 已被占：按库位序号选 S2
        chosen = seed["sites"]["S2"]
        assert executor.dispatched[0]["action_args"]["site"] == chosen
        frozen = stack.workflow.get_workflow_task(task["uuid"])
        [selection] = frozen["execution_plan"]["site_selections"]
        assert selection["site_uuid"] == chosen and selection["owner_uuid"] == seed["heater"]
        assert selection["occupant_uuid"] == sample
        assert frozen["execution_plan"]["nodes"][0]["param"]["site"] == chosen
        [run] = stack.workflow.list_workflow_node_runs(task["uuid"])
        assert run["param"]["site"] == chosen
        [reservation] = _reservations(stack, task["uuid"])
        assert reservation["site_uuid"] == chosen and reservation["carrier_material_uuid"] == sample
        # 派发用到它的动作时转 in_transit：放入物料直接放进预留的库位等同接收，别的物料照旧被挡
        assert reservation["state"] == "in_transit"
        stack.materials.move_material(_mutation("move_material"),
                                      MaterialMove(material_uuid=sample, destination_site_uuid=chosen))
        assert _reservations(stack, task["uuid"])[0]["state"] == "received"
        executor.finish(0, {})
        wait_for(lambda: _status(stack, task["uuid"]) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_two_tasks_get_different_sites_under_the_same_owner(tmp_path) -> None:
    stack, executor = Stack(tmp_path), Executor()
    scheduler = _scheduler(stack, executor)
    try:
        seed = _seed(stack, occupied=())
        record = _workflow(stack, [_node({"heater": {"uuid": seed["heater"]}, "sample": {"kind": "workflow_input",
                                                                                         "parameter": "sample"},
                                          "site": site_under_owner(seed["heater"])})],
                           io={"input_contract": {"version": 1, "parameters": [
                               {"name": "sample", "schema": {"$slot": "ResourceSlot"}, "required": True}]},
                               "output_contract": {"version": 1, "outputs": [
                                   {"name": "sample", "schema": {"$slot": "ResourceSlot"}, "implicit": True}]},
                               "output_bindings": {"sample": {"kind": "workflow_input", "parameter": "sample"}}})
        tasks = [_create(stack, record, {"sample": {"uuid": sample}}) for sample in seed["samples"]]
        wait_for(lambda: all(stack.workflow.get_workflow_task(task["uuid"])["execution_plan"].get("site_selections")
                             for task in tasks))
        chosen = [stack.workflow.get_workflow_task(task["uuid"])["execution_plan"]["site_selections"][0]["site_uuid"]
                  for task in tasks]
        # 同一所属物料下各自预留：不会选到同一个库位，按准入先后取序号靠前的
        assert sorted(chosen) == sorted([seed["sites"]["S1"], seed["sites"]["S2"]])
        assert {item["site_uuid"] for task in tasks for item in _reservations(stack, task["uuid"])} == set(chosen)
        # 同一台设备的动作锁让两个任务先后派发，各用各的库位
        wait_for(lambda: len(executor.dispatched) == 1)
        executor.finish(0, {})
        wait_for(lambda: len(executor.dispatched) == 2)
        assert {payload["action_args"]["site"] for payload in executor.dispatched} == set(chosen)
    finally:
        scheduler.stop()
        stack.close()


def test_all_acceptable_sites_taken_waits_until_one_frees(tmp_path) -> None:
    stack, executor = Stack(tmp_path), Executor()
    scheduler = _scheduler(stack, executor)
    try:
        # 只有 S1 接受样品
        seed = _seed(stack, categories=("other", "heating"), occupied=())
        first, second = seed["samples"]
        record_for = {sample: _workflow(stack, [_node({"heater": {"uuid": seed["heater"]}, "sample": {"uuid": sample},
                                                       "site": site_under_owner(seed["heater"])})])
                      for sample in (first, second)}
        task_a = _create(stack, record_for[first])
        wait_for(lambda: len(executor.dispatched) == 1)
        task_b = _create(stack, record_for[second])
        [job_b] = stack.workflow.list_workflow_node_jobs(task_b["uuid"])
        wait_for(lambda: (stack.workflow.get_workflow_node_job(job_b["uuid"])["control_data"].get("wait_reason") or {})
                 .get("code") == "site_group_unavailable")
        assert _status(stack, task_b["uuid"]) == "pending"
        assert _reservations(stack, task_b["uuid"]) == []
        assert len(executor.dispatched) == 1
        # 任务 A 结束：它的预留收尾（样品没放进去 → 取消），B 按排队先后重试并取得 S1
        executor.finish(0, {})
        wait_for(lambda: _status(stack, task_a["uuid"]) == "succeeded")
        assert _reservations(stack, task_a["uuid"])[0]["state"] == "canceled"
        wait_for(lambda: len(executor.dispatched) == 2)
        assert executor.dispatched[1]["action_args"]["site"] == seed["sites"]["S1"]
        assert "wait_reason" not in stack.workflow.get_workflow_node_job(job_b["uuid"])["control_data"]
        executor.finish(1, {})
        wait_for(lambda: _status(stack, task_b["uuid"]) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_no_acceptable_site_under_the_owner_is_a_preflight_error(tmp_path) -> None:
    stack, executor = Stack(tmp_path), Executor()
    scheduler = _scheduler(stack, executor)
    try:
        seed = _seed(stack, categories=("other", "other"), occupied=())
        record = _workflow(stack, [_node({"heater": {"uuid": seed["heater"]}, "sample": {"uuid": seed["samples"][0]},
                                          "site": site_under_owner(seed["heater"])})])
        report = TestClient(create_workflow_app(stack.workflow)).post(
            f"/api/v1/workflows/{record['uuid']}/run-preflight", json={}).json()["data"]
        assert report["status"] == "invalid"
        [blocked] = [item for item in report["checks"] if item["blocking"]]
        assert blocked["code"] == "invalid_site_selection" and blocked["details"]["reason"] == "no_acceptable_site"

        task = _create(stack, record)
        wait_for(lambda: _status(stack, task["uuid"]) == "failed")
        [error] = stack.workflow.get_workflow_task(task["uuid"])["error_info"]
        assert error["code"] == "plan_not_executable" and error["stage"] == "preflight"
        assert error["reason"] == "no_acceptable_site" and error["details"]["parameter"] == "site"
        assert executor.dispatched == [] and _reservations(stack, task["uuid"]) == []
    finally:
        scheduler.stop()
        stack.close()


def test_preflight_reports_free_and_taken_candidates_without_reserving(tmp_path) -> None:
    stack = Stack(tmp_path)
    try:
        seed = _seed(stack, categories=("heating", "heating"), occupied=(0,))
        record = _workflow(stack, [_node({"heater": {"uuid": seed["heater"]}, "sample": {"uuid": seed["samples"][0]},
                                          "site": site_under_owner(seed["heater"])})])
        report = stack.workflow.preflight_workflow(workflow_uuid=record["uuid"], run_mode="normal",
                                                   target_node_uuid=None, input_value={})
        [ready] = [item for item in report["checks"] if item["code"] == "site_selection_ready"]
        assert ready["details"]["available"] == [seed["sites"]["S1"]]
        assert not any(item["code"] == "invalid_action_parameters" for item in report["checks"])
        stack.materials.move_material(_mutation("move_material"), MaterialMove(
            material_uuid=seed["samples"][1], destination_site_uuid=seed["sites"]["S1"]))
        report = stack.workflow.preflight_workflow(workflow_uuid=record["uuid"], run_mode="normal",
                                                   target_node_uuid=None, input_value={})
        assert report["status"] == "temporarily_unavailable"
        # 设备在线快照这里是空的（device_action_unavailable）：只看库位这一项
        assert "site_selection_unavailable" in [item["code"] for item in report["checks"] if item["blocking"]]
        from unilabos.server.services.materials.ingress import IngressReservations

        assert IngressReservations(stack.materials).list() == []
    finally:
        stack.close()


def test_exact_site_uuid_still_works_without_a_reservation(tmp_path) -> None:
    stack, executor = Stack(tmp_path), Executor()
    scheduler = _scheduler(stack, executor)
    try:
        seed = _seed(stack)
        record = _workflow(stack, [_node({"heater": {"uuid": seed["heater"]}, "sample": {"uuid": seed["samples"][0]},
                                          "site": seed["sites"]["S3"]})])
        task = _create(stack, record)
        wait_for(lambda: len(executor.dispatched) == 1)
        assert executor.dispatched[0]["action_args"]["site"] == seed["sites"]["S3"]
        assert "site_selections" not in stack.workflow.get_workflow_task(task["uuid"])["execution_plan"]
        assert _reservations(stack, task["uuid"]) == []
        executor.finish(0, {})
        wait_for(lambda: _status(stack, task["uuid"]) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_site_slot_workflow_input_by_owner_is_selected_once_and_frozen(tmp_path) -> None:
    stack, executor = Stack(tmp_path), Executor()
    scheduler = _scheduler(stack, executor)
    try:
        seed = _seed(stack)
        sample = seed["samples"][0]
        site_ref = {"kind": "workflow_input", "parameter": "site"}
        place = _node({"heater": {"uuid": seed["heater"]}, "sample": {"uuid": sample}, "site": site_ref})
        heat = _node({"heater": {"uuid": seed["heater"]}, "sample": {"uuid": sample}, "site": site_ref},
                     execution_policy={"depends_on": [place["uuid"]]})
        io = {"input_contract": {"version": 1, "parameters": [
                  {"name": "site", "schema": {"$slot": "SiteSlot"}, "required": True}]},
              "output_contract": {"version": 1, "outputs": []}, "output_bindings": {}}
        record = _workflow(stack, [place, heat], io=io)
        task = _create(stack, record, {"site": {"owner_uuid": seed["heater"].upper()}})
        assert task["input"] == {"site": {"owner_uuid": seed["heater"]}}
        wait_for(lambda: len(executor.dispatched) == 1)
        frozen = stack.workflow.get_workflow_task(task["uuid"])
        chosen = seed["sites"]["S2"]
        assert frozen["input"] == {"site": chosen}
        [selection] = frozen["execution_plan"]["site_selections"]
        assert selection["input"] == "site" and len(selection["targets"]) == 2
        assert {node["param"]["site"] for node in frozen["execution_plan"]["nodes"]} == {chosen}
        assert len(_reservations(stack, task["uuid"])) == 1
        executor.finish(0, {})
        wait_for(lambda: len(executor.dispatched) == 2)
        assert executor.dispatched[1]["action_args"]["site"] == chosen
        executor.finish(1, {})
        wait_for(lambda: _status(stack, task["uuid"]) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_occupant_bound_by_site_feeds_the_owner_selection(tmp_path) -> None:
    """物料参数按库位绑定（B）先解析，按所属物料选库位（C）用解析出的放入物料核对接收规则。"""

    stack, executor = Stack(tmp_path), Executor()
    scheduler = _scheduler(stack, executor)
    try:
        seed = _seed(stack, categories=("heating", "heating", "heating"), occupied=(0,))
        source_site = seed["sites"]["S0"]
        blocker = stack.materials.get_site(source_site).occupied_material_uuid
        record = _workflow(stack, [_node({"heater": {"uuid": seed["heater"]}, "sample": {"site_uuid": source_site},
                                          "site": site_under_owner(seed["heater"])})])
        task = _create(stack, record)
        wait_for(lambda: len(executor.dispatched) == 1)
        assert executor.dispatched[0]["action_args"]["sample"] == {"uuid": blocker}
        assert executor.dispatched[0]["action_args"]["site"] == seed["sites"]["S1"]
        assert _reservations(stack, task["uuid"])[0]["carrier_material_uuid"] == blocker
    finally:
        scheduler.stop()
        stack.close()


@pytest.mark.parametrize("case", ["no_site_relation", "occupant_undeclared", "owner_mismatch"])
def test_selection_that_can_never_work_is_rejected_when_the_task_is_created(tmp_path, case: str) -> None:
    stack = Stack(tmp_path)
    try:
        seed = _seed(stack)
        selector = None if case == "no_site_relation" else {
            **_SELECTOR, **({"occupant": None} if case == "occupant_undeclared" else {})}
        heater = {"uuid": seed["samples"][1]} if case == "owner_mismatch" else {"uuid": seed["heater"]}
        record = _workflow(stack, [_node({"heater": heater, "sample": {"uuid": seed["samples"][0]},
                                          "site": site_under_owner(seed["heater"])})], schema=_schema(selector))
        with pytest.raises(WorkflowError) as caught:
            _create(stack, record)
        assert caught.value.code == "site_selection_invalid" and caught.value.details["reason"] == case
        assert caught.value.details["parameter"] == "site"
    finally:
        stack.close()


def test_ingress_reservation_by_owner_through_the_materials_api(tmp_path, monkeypatch) -> None:
    """物料权威：按所属物料预留（6008 没有接受的库位 / 6007 都被占），in_transit 的载体放进来等同接收，任务结束收尾。"""

    from fastapi import FastAPI

    from tests.server.test_v1_inventory_api import _TestClientOpener
    from unilabos.client.materials import HTTPMaterialsClient
    from unilabos.client.materials.core import MaterialsHTTPError
    from unilabos.server.api.materials import install_materials_api

    stack = Stack(tmp_path)
    try:
        seed = _seed(stack, categories=("other", "heating"), occupied=(), samples=3)
        app = FastAPI()
        install_materials_api(app, stack.materials)
        with TestClient(app) as client:
            monkeypatch.setattr("unilabos.client.materials.core.urlopen", _TestClientOpener(client))
            gateway = HTTPMaterialsClient("http://testserver")
            first, second, third = seed["samples"]
            body = {"carrier_material_uuid": first, "owner_material_uuid": seed["heater"], "ttl_seconds": 600,
                    "backend_task_uuid": "task-a", "idempotency_key": "k-1"}
            reservation = gateway.reserve_ingress(body)
            assert reservation["site_uuid"] == seed["sites"]["S1"] and reservation["state"] == "reserved"
            assert gateway.reserve_ingress(body)["uuid"] == reservation["uuid"]
            with pytest.raises(MaterialsHTTPError) as taken:
                gateway.reserve_ingress({**body, "carrier_material_uuid": second, "idempotency_key": "k-2"})
            assert taken.value.business_code == 6007
            with pytest.raises(MaterialsHTTPError) as none:
                gateway.reserve_ingress({**body, "carrier_material_uuid": third, "idempotency_key": "k-3",
                                         "owner_material_uuid": first})
            assert none.value.business_code in {4001, 6000, 6008}
            facts = gateway.site_admission_facts({"owners": [
                {"owner_material_uuid": seed["heater"], "occupant_material_uuid": second}]})
            assert [item["site_uuid"] for item in facts["owners"][0]["acceptable"]] == [seed["sites"]["S1"]]
            assert facts["owners"][0]["available"] == []

            # reserved 期间载体自己也放不进来（入口预留的原有语义）；转 in_transit 后放进来等同接收
            with pytest.raises(Exception):
                stack.client.move_material(_mutation("move_material"), MaterialMove(
                    material_uuid=first, destination_site_uuid=seed["sites"]["S1"]))
            assert gateway.mark_ingress_in_transit(reservation["uuid"])["state"] == "in_transit"
            with pytest.raises(Exception):
                stack.client.move_material(_mutation("move_material"), MaterialMove(
                    material_uuid=second, destination_site_uuid=seed["sites"]["S1"]))
            stack.client.move_material(_mutation("move_material"), MaterialMove(
                material_uuid=first, destination_site_uuid=seed["sites"]["S1"]))
            assert gateway.finish_task_ingress("task-a", reason="任务结束") == []
            from unilabos.server.services.materials.ingress import IngressReservations

            assert IngressReservations(stack.materials).get(reservation["uuid"])["state"] == "received"

            # 没有接受的库位：6008（合同错误）
            other = _seed(stack, categories=("other",), occupied=(), samples=1)
            with pytest.raises(MaterialsHTTPError) as rejected:
                gateway.reserve_ingress({**body, "carrier_material_uuid": other["samples"][0],
                                         "owner_material_uuid": other["heater"], "idempotency_key": "k-4"})
            assert rejected.value.business_code == 6008
            # 收尾：没放进来的预留取消
            fresh = _seed(stack, categories=("heating",), occupied=(), samples=1)
            later = gateway.reserve_ingress({**body, "carrier_material_uuid": fresh["samples"][0], "idempotency_key": "k-5",
                                             "owner_material_uuid": fresh["heater"], "backend_task_uuid": "task-b"})
            [finished] = gateway.finish_task_ingress("task-b", reason="任务结束")
            assert finished["uuid"] == later["uuid"] and finished["state"] == "canceled"
    finally:
        stack.close()
