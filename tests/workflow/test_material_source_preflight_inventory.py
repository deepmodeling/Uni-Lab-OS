"""来源库存预检与提交的真实 HTTP 纵向回归，覆盖交接保护而不预占库存。"""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.app.f05_material_source_http_fixture import graph_payload, handle_templates, node_templates
from tests.app.test_station_handoff import frozen, handoff_inventory_fixture, package, uid
from tests.app.test_station_handoff_pickup import picked_up
from tests.app.test_station_material_inputs import received  # noqa: F401
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.domain import (
    InsufficientStock,
    MaterialRequirement,
    MaterialSourceAdmissionRequest,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.site_selection import build_inventory_site_selection_resolver
from unilabos.app.scheduler.inventory.source_preflight import material_source_checks
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.published_contract import PublishedWorkflowContractStore
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


@contextmanager
def source_runtime(tmp_path, *, state="available", explicit=False, wide=False, existing_setup=None, existing_workflow_store=None,
                   policy="task_exclusive", center_output=True):
    """用正式交接 fixture 与来源图建立同一库存权威下的预检和提交接缝。"""
    fixture = handoff_inventory_fixture(tmp_path) if existing_setup is None else None
    s = next(fixture) if fixture is not None else existing_setup
    store = existing_workflow_store or WorkflowStore(tmp_path / "workflow.db")
    inventory = InventoryService(s["stores"]["source"])
    backend = BackendResourceService(inventory.store)
    carrier, mount, site = s["carrier"]["uuid"], s["carrier"]["parent_uuid"], s["sites"]["source"]
    template = backend.get_material(carrier)["resource_template_uuid"]
    if state == "empty":
        backend.update_material(carrier, {"site_placement": {"action": "remove"}})
    elif state in {"protected", "picked_up"}:
        s["payload"].update(global_task_uuid=uid(), transfer_definition_uuid=uid(), transfer_source_kind="node_output")
        frozen(s)
        if state == "picked_up":
            s["auth"]["source"].apply_center_snapshot(package(picked_up(s)))
    node = graph_payload(material_resource_template_uuid=template, material_uuid=carrier if explicit else mount,
        device_material_uuid=uid(), mode="existing", automatic=not explicit)["nodes"][0]
    node["param"]["custody_policy"] = policy
    if not explicit and not wide:
        node["meta_data"] = {"unilab": {"material_source_site_binding": {"parameter": "source_site"}}}
    if state == "wrong_template":
        node["param"]["resource_template_uuid"] = s["templates"]["source"]["test.rack"]
    elif state in {"missing_site", "missing_mount"}:
        node["meta_data"] = {}
        node["param"].update(site=uid() if state == "missing_site" else None,
            mount={"uuid": uid()} if state == "missing_mount" else {"uuid": mount})
    templates = node_templates(template, uid())[:1]
    handles = handle_templates(template, uid())[:1]
    handles[0].pop("node_business_key")
    handles[0]["workflow_node_template_uuid"] = templates[0]["uuid"]
    graph = store.create_workflow_with_graph(workflow_uuid=uid(), name="来源预检", tags=[], description=None,
        meta_data={"center_material_ports": [{"name": "sample", "direction": "output", "value_path": "$.sample",
            "local_value_kind": "material_ref", "cardinality": "one", "resource_template_uuid": template}] if center_output else [],
            "unilab": {"input_contract": {"version": 1, "parameters": [{"name": "source_site", "required": True, "schema": {"type": "string"}}]},
                "output_contract": {"version": 1, "outputs": [{"name": "sample", "implicit": False, "schema": {"$slot": "ResourceSlot"}}]},
                "output_bindings": {"sample": {"kind": "node_output", "workflow_node_uuid": node["uuid"], "source_handle_uuid": handles[0]["uuid"]}}}},
        nodes=[WorkflowNodeWrite.model_validate(node)], edges=[], node_templates=templates, handle_templates=handles,
        template_catalog_fingerprint="sha256:" + "1" * 64, trusted_authoring_graph=True)
    contract = PublishedWorkflowContractStore(store).publish(graph=graph, expected_revision=1)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    service = WorkflowService(store, task_scheduler_bridge=TaskSchedulerBridge(store, scheduler=scheduler),
        material_resolver=backend.get_material, resource_reference_resolver=backend.get_material,
        site_selection_resolver=build_inventory_site_selection_resolver(inventory.store))
    try:
        with TestClient(create_workflow_app(service)) as client:
            yield SimpleNamespace(client=client, service=service, store=store, inventory=inventory,
                dispatcher=dispatcher, graph=graph, contract=contract, carrier=carrier, site=site,
                backend=backend, mount=mount, template=template,
                candidate={"workflow_uuid": graph["workflow"]["uuid"], "input": {"source_site": site}})
    finally:
        service.close()
        scheduler.close()
        try:
            if fixture is not None:
                next(fixture)
        except StopIteration:
            pass


def facts(r):
    """比较真实业务表，预检不允许创建占用或更改已有保护。"""
    return [r.store.count_rows(table) for table in ("workflow_task", "workflow_node_job", "workflow_inventory_allocation")] + [
        r.inventory.store.query_all(f"SELECT * FROM {table}") for table in
        ("material", "site", "inventory_reservation", "inventory_material_source_binding", "station_handoff_resource", "inventory_ledger", "sync_outbox")]


def reports(r):
    candidate = r.candidate
    single = r.client.post(f"/api/v1/workflows/{candidate['workflow_uuid']}/run-preflight", json={"input": candidate["input"]})
    batch = r.client.post("/api/v1/workflows/run-preflight", json={"items": [candidate]})
    frozen_batch = r.client.post("/api/v1/workflows/run-preflight", json={"items": [{**candidate, "revision_fingerprint": r.contract["source_hash"]}]})
    assert all(response.json()["code"] == 0 for response in (single, batch, frozen_batch))
    return [single.json()["data"], batch.json()["data"]["items"][0], frozen_batch.json()["data"]["items"][0]]


@pytest.mark.parametrize("state,explicit,code", [("empty", False, "material_source_empty"),
    ("protected", False, "material_source_unavailable"), ("picked_up", False, "material_source_empty"),
    ("protected", True, "material_source_unavailable"), ("picked_up", True, "material_source_unavailable")])
def test_unavailable_source_blocks_all_preflight_and_submission(tmp_path, state, explicit, code):
    with source_runtime(tmp_path, state=state, explicit=explicit) as r:
        before = facts(r)
        for report in reports(r):
            assert report["can_run"] is False, report
            check = next(item for item in report["checks"] if item["code"] == code)
            assert check["status"] == "blocked" and check["blocking"]
            if code == "material_source_empty":
                assert "源库位无容器" in check["message"]
        assert facts(r) == before
        submitted = r.client.post("/api/v1/workflow-tasks", json={**r.candidate, "run_mode": "normal", "meta_data": {}})
        assert submitted.json()["error"]["code"] == "preflight_failed", submitted.text
        assert facts(r) == before
        assert r.dispatcher.dispatched == []


def test_source_preflight_available_and_pending_input_are_read_only(tmp_path):
    with source_runtime(tmp_path) as r:
        before = facts(r)
        assert all(report["can_run"] for report in reports(r))
        pending = r.client.post("/api/v1/workflows/run-preflight", json={"items": [{
            "workflow_uuid": r.candidate["workflow_uuid"], "input_ready": False, "revision_fingerprint": r.contract["source_hash"]}]})
        report = pending.json()["data"]
        assert report["can_run"] is True, report
        assert any(check["code"] == "upstream_input_pending" for check in report["items"][0]["checks"])
        assert facts(r) == before
        submitted = r.client.post("/api/v1/workflow-tasks", json={**r.candidate, "run_mode": "normal", "meta_data": {}})
        assert submitted.status_code == 201 and submitted.json()["data"]["status"] == "succeeded", submitted.text


def test_batch_cannot_assign_same_exclusive_container_twice(tmp_path):
    with source_runtime(tmp_path) as r:
        before = facts(r)
        candidate = {**r.candidate, "revision_fingerprint": r.contract["source_hash"]}
        report = r.client.post("/api/v1/workflows/run-preflight", json={"items": [candidate, deepcopy(candidate)]}).json()["data"]
        assert report["can_run"] is False, report
        assert any(check["code"] == "material_source_batch_conflict" for check in report["checks"])
        assert facts(r) == before


def test_regular_task_reservation_keeps_submission_queue_semantics(tmp_path):
    with source_runtime(tmp_path, explicit=True) as r:
        r.inventory.reserve_workflow(uid(), {uid(): [MaterialRequirement(instance_uuid=r.carrier)]})
        assert all(not report["can_run"] for report in reports(r))
        submitted = r.client.post("/api/v1/workflow-tasks", json={**r.candidate, "run_mode": "normal", "meta_data": {}})
        assert submitted.status_code == 201 and submitted.json()["data"]["status"] == "pending", submitted.text


@pytest.mark.parametrize("state,code", [("wrong_template", "material_source_unavailable"),
    ("missing_site", "invalid_material_source"), ("missing_mount", "invalid_material_source")])
def test_invalid_source_is_not_misreported_as_queueable_or_empty(tmp_path, state, code):
    with source_runtime(tmp_path, state=state) as r:
        before = facts(r)
        for report in reports(r):
            assert not report["can_run"], report
            assert any(check["code"] == code for check in report["checks"]), report
        response = r.client.post("/api/v1/workflow-tasks", json={**r.candidate, "run_mode": "normal", "meta_data": {}})
        assert response.json()["error"]["code"] == "preflight_failed", response.text
        assert facts(r) == before


def test_batch_wide_mount_can_select_distinct_containers_without_reserving(tmp_path):
    with source_runtime(tmp_path, wide=True) as r:
        extra_site = uid()
        with r.inventory.store.transaction() as connection:
            connection.execute("INSERT INTO site(uuid,create_time,update_time,material_uuid,name,sort_order) VALUES (?,'2026-01-01','2026-01-01',?,'second',2)", (extra_site, r.mount))
        r.backend.create_material({"resource_template_uuid": r.template, "name": "第二个容器",
            "site_placement": {"action": "place", "site_uuid": extra_site}})
        before = facts(r)
        candidate = {**r.candidate, "revision_fingerprint": r.contract["source_hash"]}
        report = r.client.post("/api/v1/workflows/run-preflight", json={"items": [candidate, deepcopy(candidate)]}).json()["data"]
        assert report["can_run"] is True, report
        assert facts(r) == before


@pytest.mark.parametrize("narrow_kind", ["site", "uuid", "slot_range"])
@pytest.mark.parametrize("narrow_first", [False, True])
@pytest.mark.parametrize("enough_stock", [False, True])
def test_batch_narrow_sources_have_priority_independent_of_candidate_order(tmp_path, narrow_kind, narrow_first, enough_stock):
    """宽范围不得抢走窄范围唯一容器；报告仍引用请求中的原候选位置。"""
    with source_runtime(tmp_path, wide=True) as r:
        if enough_stock:
            extra_site = uid()
            with r.inventory.store.transaction() as connection:
                connection.execute("INSERT INTO site(uuid,create_time,update_time,material_uuid,name,sort_order) VALUES (?,'2026-01-01','2026-01-01',?,'second',2)", (extra_site, r.mount))
            r.backend.create_material({"resource_template_uuid": r.template, "name": "第二个容器",
                "site_placement": {"action": "place", "site_uuid": extra_site}})
        node = deepcopy(r.graph["nodes"][0])
        selector = node["param"]
        if narrow_kind == "site":
            selector["site"] = r.site
        elif narrow_kind == "uuid":
            selector["material_uuid"] = r.carrier
        else:
            selector["slot_range"] = [r.site]
        narrow_graph = r.store.save_graph(r.candidate["workflow_uuid"], revision=1,
            nodes=[WorkflowNodeWrite.model_validate(node)], edges=[])
        contract = PublishedWorkflowContractStore(r.store).publish(graph=narrow_graph, expected_revision=2)
        wide = {**r.candidate, "revision_fingerprint": r.contract["source_hash"]}
        narrow = {**r.candidate, "revision_fingerprint": contract["source_hash"]}
        candidates = [narrow, wide] if narrow_first else [wide, narrow]
        before = facts(r)
        response = r.client.post("/api/v1/workflows/run-preflight", json={"items": candidates})
        assert response.json()["code"] == 0, response.text
        report = response.json()["data"]
        assert all(item["can_run"] for item in report["items"]), report
        assert report["can_run"] is enough_stock, report
        if not enough_stock:
            conflict = next(check for check in report["checks"] if check["code"] == "material_source_batch_conflict")
            assert conflict["details"]["item_index"] == (1 if narrow_first else 0)
        assert facts(r) == before


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("policy", ["task_exclusive", "shared_source"])
def test_one_task_duplicate_exclusive_sources_match_runtime_without_blocking_shared_reuse(tmp_path, explicit, policy):
    """同任务两次独占同一具体容器须拒绝；正式库存允许的共享复用保持可用。"""
    with source_runtime(tmp_path, explicit=explicit) as r:
        first = deepcopy(r.graph["nodes"][0])
        first["param"]["custody_policy"] = policy
        second = deepcopy(first)
        second["uuid"] = uid()
        metadata = deepcopy(r.graph["workflow"]["meta_data"])
        metadata["center_material_ports"] = []  # 本例检查来源复用，不声明共享物料出站。
        graph = r.store.save_graph(r.candidate["workflow_uuid"], revision=1,
            nodes=[WorkflowNodeWrite.model_validate(node) for node in [first, second]], edges=[], workflow_meta_data=metadata)
        r.contract = PublishedWorkflowContractStore(r.store).publish(graph=graph, expected_revision=2)
        prepared = r.service._prepare_task_input(graph, input_value=r.candidate["input"], run_mode="normal", target_node_uuid=None)
        sources = [node for node in prepared.execution_plan["nodes"] if node["kind"] == "material_source"]
        requests = [MaterialSourceAdmissionRequest(node_id=node["uuid"], resource_template_uuid=r.template,
            custody_policy=policy, requirement=MaterialRequirement.from_dict(node["material_requirements"][0])) for node in sources]
        before = facts(r)
        if policy == "task_exclusive":
            with pytest.raises(InsufficientStock):
                r.inventory.admit_material_sources(uid(), requests)
            assert facts(r) == before
        for report in reports(r):
            assert report["can_run"] is (policy == "shared_source"), report
            if policy == "task_exclusive":
                assert any(check["code"] == "material_source_batch_conflict" and "同一任务" in check["message"] for check in report["checks"])
        assert facts(r) == before
        if policy == "task_exclusive":
            submitted = r.client.post("/api/v1/workflow-tasks", json={**r.candidate, "run_mode": "normal", "meta_data": {}})
            assert submitted.json()["error"]["code"] == "preflight_failed", submitted.text
            assert facts(r) == before
        else:
            admitted = r.inventory.admit_material_sources(uid(), requests)
            assert list(admitted["allocations"].values()) == [[r.carrier], [r.carrier]]


def test_one_task_wide_exclusive_sources_keep_runtime_distinct_selection(tmp_path):
    """同任务宽范围仍可依次取得不同容器，不把独立首选相同误报为固定来源重复。"""
    with source_runtime(tmp_path, wide=True) as r:
        extra_site = uid()
        with r.inventory.store.transaction() as connection:
            connection.execute("INSERT INTO site(uuid,create_time,update_time,material_uuid,name,sort_order) VALUES (?,'2026-01-01','2026-01-01',?,'second',2)", (extra_site, r.mount))
        second_carrier = r.backend.create_material({"resource_template_uuid": r.template, "name": "第二个容器",
            "site_placement": {"action": "place", "site_uuid": extra_site}})
        graph = deepcopy(r.graph)
        second = deepcopy(graph["nodes"][0])
        second["uuid"] = uid()
        graph["nodes"].append(second)
        prepared = r.service._prepare_task_input(graph, input_value=r.candidate["input"], run_mode="normal", target_node_uuid=None)
        assert all(check["code"] == "material_source_ready" for check in material_source_checks(r.inventory, prepared.execution_plan))
        requests = [MaterialSourceAdmissionRequest(node_id=node["uuid"], resource_template_uuid=r.template,
            custody_policy="task_exclusive", requirement=MaterialRequirement.from_dict(node["material_requirements"][0]))
            for node in prepared.execution_plan["nodes"] if node["kind"] == "material_source"]
        admitted = r.inventory.admit_material_sources(uid(), requests)
        assert {identities[0] for identities in admitted["allocations"].values()} == {r.carrier, second_carrier["uuid"]}


def test_missing_inventory_authority_fails_closed_for_known_source(tmp_path):
    with source_runtime(tmp_path, explicit=True) as r:
        standalone = WorkflowService(r.store)
        report = standalone.get_workflow_run_preflight(r.candidate["workflow_uuid"],
            input_value=r.candidate["input"], evaluate_inventory=True)
        assert not report["can_run"] and any(check["code"] == "material_source_unavailable" for check in report["checks"])
        # 装配真实无库存调度器，不能用测试替身伪造预检结果。
        scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
        bridge = TaskSchedulerBridge(r.store, scheduler=scheduler)
        prepared = r.service._prepare_task_input(r.graph, input_value=r.candidate["input"], run_mode="normal", target_node_uuid=None)
        try:
            checks = bridge.preflight_material_sources(prepared)
            assert checks[0]["code"] == "material_source_unavailable" and checks[0]["blocking"]
        finally:
            bridge.close()
            scheduler.close()


def test_get_preflight_checks_static_source_without_entry_values(tmp_path):
    with source_runtime(tmp_path, state="protected", explicit=True) as r:
        metadata = deepcopy(r.graph["workflow"]["meta_data"])
        metadata["unilab"]["input_contract"]["parameters"] = []
        r.store.save_graph(r.candidate["workflow_uuid"], revision=1,
            nodes=[WorkflowNodeWrite.model_validate(node) for node in r.graph["nodes"]], edges=[], workflow_meta_data=metadata)
        before = facts(r)
        report = r.client.get(f"/api/v1/workflows/{r.candidate['workflow_uuid']}/run-preflight").json()["data"]
        assert not report["can_run"] and any(check["code"] == "material_source_unavailable" for check in report["checks"]), report
        assert facts(r) == before


def test_received_material_owned_by_fixed_center_task_remains_usable(tmp_path, received):
    """真实B收货与输入占用不应被误认为未签收交接；本任务可用、另一任务不可用。"""
    path = tmp_path / "definition"
    path.mkdir()
    with source_runtime(path, explicit=True) as r:
        state = received
        inventory = InventoryService(state["stores"]["target"])
        receipt = state["authority"].prepare(state["body"], input_materials=state["inputs"])
        graph = deepcopy(r.graph)
        selector = graph["nodes"][0]["param"]
        selector.update(material_uuid=state["local"], mount={"uuid": state["local"]},
            resource_template_uuid=state["inputs"][0]["resource_template_uuid"])
        prepared = r.service._prepare_task_input(graph, input_value=r.candidate["input"], run_mode="normal", target_node_uuid=None)
        before = [inventory.store.query_all(f"SELECT * FROM {table}") for table in
            ("material", "site", "station_handoff_resource", "station_material_input", "inventory_reservation")]
        foreign = material_source_checks(inventory, prepared.execution_plan)
        own = material_source_checks(inventory, prepared.execution_plan, task_uuid=receipt["local_task_uuid"])
        assert foreign[0]["code"] == "material_source_busy"
        assert own[0]["code"] == "material_source_ready", own
        assert [inventory.store.query_all(f"SELECT * FROM {table}") for table in
            ("material", "site", "station_handoff_resource", "station_material_input", "inventory_reservation")] == before
        node = next(node for node in prepared.execution_plan["nodes"] if node["kind"] == "material_source")
        admitted = inventory.admit_material_sources(receipt["local_task_uuid"], [MaterialSourceAdmissionRequest(
            node_id=node["uuid"], resource_template_uuid=selector["resource_template_uuid"], custody_policy="task_exclusive",
            requirement=MaterialRequirement.from_dict(node["material_requirements"][0]))])
        assert admitted["allocations"][node["uuid"]] == [state["local"]]


def test_dynamic_replenishment_source_keeps_runtime_deferred(tmp_path):
    """真实编译的补料分支尚未到达来源，不把未产生的候选误判为当前缺料。"""
    from tests.workflow.test_runtime_material_replenishment import compiled_refill
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder
    from unilabos.workflow.run_preflight import build_run_preflight_report

    with source_runtime(tmp_path) as r:
        _, compiled = compiled_refill()
        plan, _ = ExecutionPlanBuilder().build(compiled.graph, run_mode="normal", target_node_uuid=None)
        assert material_source_checks(r.inventory, plan) == []
        report = build_run_preflight_report(graph=compiled.graph, run_mode="normal", target_node_uuid=None, material_resolver=None)
        sources = [check for check in report["checks"] if check["type"] == "material_source"]
        assert sources and all(check["status"] == "deferred" and check["code"] == "material_source_checked_at_runtime" for check in sources)
