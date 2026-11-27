"""公开物料端口、真实完成后的证据补报与旧事件摘要兼容。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from tests.app.center_client.test_atomic_workflow_events import _events, _snapshot, _start, _submit, _workflow
from tests.app.center_client.test_catalog import contract
from tests.app.center_client.test_center_client import INSTANCE, INVOCATION, STATION
from unilabos.app.center_client.catalog import CatalogProjectionError, project_contract
from unilabos.app.center_client.client import TaskPackage
from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.events import EventEnvelopeError, validate_event
from unilabos.workflow.published_contract import PublishedWorkflowContractStore
from unilabos.workflow.store import StoreConflict


def port(*, direction="output", path="$.result", kind="material_uuid", cardinality="one"):
    return {"name": "sample", "direction": direction, "value_path": path,
            "local_value_kind": kind, "cardinality": cardinality, "resource_template_uuid": str(uuid4())}


def test_public_material_ports_bind_projection_and_preserve_local_digests():
    public = contract()
    public["output_contract"]["outputs"][0] = {"name": "result", "schema": {"type": "string"}}
    before = project_contract(public)
    public["material_ports"] = [port()]
    after = project_contract(public)
    assert after["material_ports"] == public["material_ports"]
    assert after["output_schema"]["properties"]["result"]["format"] == "uuid"
    assert after["contract_digest"] == before["contract_digest"]
    assert after["revision_fingerprint"] == before["revision_fingerprint"]
    assert after["projection_digest"] != before["projection_digest"]
    public["material_ports"][0]["name"] = "changed"
    assert after["material_ports"][0]["name"] == "sample"


def test_fixed_extended_outcome_fixture_is_valid():
    body = json.loads((Path(__file__).parent / "fixtures/station-outcome-evidence-v1.json").read_text())
    assert validate_event(body) == body


def test_public_port_export_cannot_override_frozen_graph(tmp_path):
    with _workflow(tmp_path) as (store, _, _, _, _):
        published = PublishedWorkflowContractStore(store)
        public = published.list_latest(page=1, page_size=10, keyword="")["items"][0]
        frozen = published.get(public["uuid"])
        frozen["material_ports"] = [port()]
        assert "material_ports" not in published.public(frozen)
        from unilabos.workflow.published_contract import PublishedContractInvalid, validate_historical_contract
        with pytest.raises(PublishedContractInvalid, match="冻结执行图"):
            validate_historical_contract(frozen)


@pytest.mark.parametrize("change", [
    {"value_path": "$.missing"}, {"value_path": "$.result[*]"}, {"cardinality": "many"},
    {"local_value_kind": "material_ref"}, {"unknown": True}, {"direction": "both"},
    {"resource_template_uuid": "invalid"},
])
def test_invalid_explicit_ports_fail_the_whole_projection(change):
    public = contract()
    public["output_contract"]["outputs"] = [{"name": "result", "schema": {"type": "string"}}]
    public["material_ports"] = [{**port(), **change}]
    with pytest.raises(CatalogProjectionError):
        project_contract(public)


def test_resource_slot_template_and_duplicate_path_are_rejected():
    public = contract()
    declared = port(direction="input", path="$.sample", kind="material_ref")
    public["input_contract"]["parameters"][1]["schema"]["allowed_resource_template_uuids"] = [str(uuid4())]
    public["material_ports"] = [declared]
    with pytest.raises(CatalogProjectionError):
        project_contract(public)
    public["input_contract"]["parameters"][1]["schema"]["allowed_resource_template_uuids"] = [declared["resource_template_uuid"]]
    assert project_contract(public)["material_ports"] == [declared]
    public["material_ports"].append({**declared, "name": "duplicate"})
    with pytest.raises(CatalogProjectionError):
        project_contract(public)


@contextmanager
def material_workflow(tmp_path, *, output_schema=None, value_kind="material_uuid", cardinality="one", extra_output=False):
    with _workflow(tmp_path, schedule=True) as (store, service, durable, _, scheduler):
        published = PublishedWorkflowContractStore(store)
        original = published.list_latest(page=1, page_size=10, keyword="")["items"][0]
        frozen = published.get(original["uuid"])
        graph = deepcopy(frozen["graph_snapshot"])
        # 新工作流发布一个真实字符串输出；保持原设备 Job 的数值输入。
        graph["workflow"]["uuid"] = str(uuid4())
        graph["workflow"]["revision"] = 1
        for node in graph["nodes"]:
            node["workflow_uuid"] = graph["workflow"]["uuid"]
        metadata = graph["workflow"]["meta_data"]["unilab"]
        schema = output_schema or {"type": "string"}
        metadata["output_contract"]["outputs"][0]["schema"] = deepcopy(schema)
        metadata["material_ports"] = [port(kind=value_kind, cardinality=cardinality)]
        graph["node_templates"][0]["meta_data"]["unilab"]["action_contract_schema"]["properties"]["result"]["properties"]["result"] = deepcopy(schema)
        for handle in graph["handle_templates"]:
            if handle["io_type"] == "source":
                handle["type"] = schema.get("type", "ResourceSlot")
                handle["meta_data"] = {"unilab": {"value_schema": deepcopy(schema)}}
        if extra_output:
            binding = deepcopy(metadata["output_bindings"]["result"])
            handle = deepcopy(next(handle for handle in graph["handle_templates"]
                                   if handle["uuid"] == binding["source_handle_uuid"]))
            handle.update(uuid=str(uuid4()), handle_key="flag", data_key="flag", type="boolean",
                          meta_data={"unilab": {"value_schema": {"type": "boolean"}}})
            graph["handle_templates"].append(handle)
            binding["source_handle_uuid"] = handle["uuid"]
            metadata["output_bindings"]["flag"] = binding
            metadata["output_contract"]["outputs"].append({"name": "flag", "schema": {"type": "boolean"}})
            action_result = graph["node_templates"][0]["meta_data"]["unilab"]["action_contract_schema"]["properties"]["result"]
            action_result["properties"]["flag"] = {"type": "boolean"}
            action_result["required"].append("flag")
        store.create_workflow(workflow_uuid=graph["workflow"]["uuid"], name="Material output", tags=[], description=None, meta_data={})
        frozen = published.publish(graph=graph, expected_revision=1)
        public = published.public(frozen)
        assert "graph_snapshot" not in public and public["material_ports"] == metadata["material_ports"]
        assert project_contract(public)["material_ports"] == metadata["material_ports"]
        body = json.loads(durable._db.execute("SELECT package_json FROM center_invocation_inbox").fetchone()[0])
        body["payload"].update(workflow_uuid=graph["workflow"]["uuid"], revision_fingerprint=frozen["source_hash"],
                               station_invocation_uuid=str(uuid4()), invocation_key=body["payload"]["invocation_key"] + "unused")
        # 使用合法新节点构造永久调用键，与原本未提交的夹具隔离。
        body["payload"]["invocation_key"] = body["payload"]["global_workflow_revision_uuid"] + ":" + str(uuid4())
        body["payload_digest"] = digest(body["payload"])
        package = TaskPackage.from_response(body)
        durable.remember_package(package)
        invocation = body["payload"]["station_invocation_uuid"]
        durable.confirm_claim(invocation)
        from unilabos.app.center_client.runtime import submission
        task = service.submit_station_workflow(**submission(package), center_invocation_uuid=invocation)
        yield store, service, durable, package, task, scheduler, invocation


def evidence(material):
    return {"material_refs": [{"station_uuid": STATION, "local_material_uuid": material,
                               "local_revision": 1, "material_lineage_uuid": str(uuid4()), "handoff_item_uuid": None}],
            "container_refs": [{"container_uuid": str(uuid4()), "state_version": 1}],
            "artifact_refs": [{"uri": "urn:artifact:result", "sha256": "a" * 64, "size": 12, "media_type": "text/plain"}]}


def record(store, package, task, invocation, refs, **overrides):
    args = {"invocation_uuid": invocation, "task_uuid": task["uuid"], "owner_instance_uuid": INSTANCE,
            "task_payload_digest": package.payload_digest, "evidence": refs}
    args.update(overrides)
    store.record_center_outcome_evidence(**args)


def test_material_outcome_waits_for_evidence_without_reexecuting_physical_job(tmp_path):
    with material_workflow(tmp_path) as (store, service, durable, package, task, scheduler, invocation):
        material = str(uuid4())
        projection, job = _start(store, task)
        projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info={"result": material})
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        assert store.get_station_workflow_result(task["uuid"]) == {"result": material}
        assert len(_events(store)) == 1
        assert store.get_center_outcome_evidence_status(invocation)["pending_reason"] == "material_evidence_required"
        before_jobs = store.list_jobs(task["uuid"])
        submitted = list(scheduler.submissions)
        service.recover_center_workflow_invocation(invocation)
        assert scheduler.submissions == submitted and store.list_jobs(task["uuid"]) == before_jobs
        refs = evidence(material)
        record(store, package, task, invocation, refs)
        outcome = _events(store)[-1]
        assert outcome["payload"]["material_refs"] == refs["material_refs"]
        assert outcome["payload"]["container_refs"] == refs["container_refs"]
        assert outcome["payload"]["artifact_refs"] == refs["artifact_refs"]
        assert outcome["payload"]["started_at"] is not None
        assert outcome["payload"]["finished_at"] == outcome["occurred_at"]
        record(store, package, task, invocation, refs)
        service.recover_center_workflow_invocation(invocation)
        assert len(_events(store)) == 2 and _events(store)[-1] == outcome
        assert scheduler.submissions == submitted and store.list_jobs(task["uuid"]) == before_jobs
        wrong = deepcopy(refs)
        wrong["material_refs"][0]["local_revision"] = 2
        with pytest.raises(StoreConflict, match="已冻结"):
            record(store, package, task, invocation, wrong)


@pytest.mark.parametrize("cardinality", ["one", "many"])
def test_resource_slot_outcome_projects_uuid_without_changing_local_fact_or_evidence(tmp_path, cardinality):
    schema = {"$slot": "ResourceSlot"}
    if cardinality == "many":
        schema = {"type": "array", "items": schema}
    with material_workflow(tmp_path, output_schema=schema, value_kind="material_ref",
                           cardinality=cardinality) as (store, service, _, package, task, scheduler, invocation):
        material = str(uuid4())
        raw = {"uuid": material, "custody_policy": "task_exclusive", "resource_template_uuid": str(uuid4())}
        public = {"uuid": material}
        if cardinality == "many":
            raw, public = [raw], [public]
        projection, job = _start(store, task)
        projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info={"result": raw})
        refs = evidence(material)
        record(store, package, task, invocation, refs)
        outcome = _events(store)[-1]
        assert outcome["payload"]["output"] == {"result": public}
        assert store.get_station_workflow_result(task["uuid"]) == {"result": raw}
        assert outcome["payload"]["material_refs"] == refs["material_refs"]
        assert outcome["payload"]["container_refs"] == refs["container_refs"]
        assert outcome["payload"]["artifact_refs"] == refs["artifact_refs"]
        jobs, submissions = store.list_jobs(task["uuid"]), list(scheduler.submissions)
        service.recover_center_workflow_invocation(invocation)
        record(store, package, task, invocation, refs)
        assert _events(store)[-1] == outcome
        assert store.list_jobs(task["uuid"]) == jobs and scheduler.submissions == submissions


@pytest.mark.parametrize("legacy_payload", [False, True])
def test_historical_resource_slot_outcome_is_replayed_without_rewriting_metadata(tmp_path, legacy_payload):
    with material_workflow(tmp_path, output_schema={"$slot": "ResourceSlot"},
                           value_kind="material_ref") as (store, service, _, package, task, scheduler, invocation):
        material = str(uuid4())
        raw = {"uuid": material, "custody_policy": "task_exclusive", "resource_template_uuid": str(uuid4())}
        projection, job = _start(store, task)
        projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info={"result": raw})
        record(store, package, task, invocation, evidence(material))
        historical = _events(store)[-1]
        historical["payload"]["output"] = {"result": raw}
        if legacy_payload:
            historical["payload"] = {key: historical["payload"][key]
                                     for key in ("task_payload_digest", "status", "output")}
        historical["event_digest"] = digest({key: value for key, value in historical.items() if key != "event_digest"})
        with store.transaction() as connection:
            connection.execute("UPDATE center_boundary_outbox SET event_json=? WHERE invocation_uuid=? AND phase=1",
                               (json.dumps(historical), invocation))
        jobs, submissions = store.list_jobs(task["uuid"]), list(scheduler.submissions)
        service.recover_center_workflow_invocation(invocation)
        assert _events(store)[-1] == historical
        assert store.get_station_workflow_result(task["uuid"]) == {"result": raw}
        assert store.list_jobs(task["uuid"]) == jobs and scheduler.submissions == submissions


def test_projected_outcome_replay_rejects_boolean_to_integer_result_drift(tmp_path):
    from unilabos.workflow.service import WorkflowConflict

    with material_workflow(tmp_path, output_schema={"$slot": "ResourceSlot"}, value_kind="material_ref",
                           extra_output=True) as (store, service, _, package, task, _, invocation):
        material = str(uuid4())
        raw = {"uuid": material, "custody_policy": "task_exclusive"}
        projection, job = _start(store, task)
        projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info={"result": raw, "flag": True})
        record(store, package, task, invocation, evidence(material))
        original = _events(store)[-1]
        assert original["payload"]["output"] == {"result": {"uuid": material}, "flag": True}
        # 模拟持久事实损坏：Python 的 True == 1 不能替代原有 JCS 精确校验。
        with store.transaction() as connection:
            connection.execute("UPDATE workflow_result SET result=? WHERE workflow_task_uuid=?",
                               (json.dumps({"result": raw, "flag": 1}), task["uuid"]))
        with pytest.raises(WorkflowConflict, match="真实工作流结果不一致"):
            service.recover_center_workflow_invocation(invocation)
        assert _events(store)[-1] == original


def test_evidence_binding_and_atomic_outbox_failure(tmp_path, monkeypatch):
    with material_workflow(tmp_path) as (store, _, _, package, task, _, invocation):
        material = str(uuid4())
        refs = evidence(material)
        with pytest.raises(StoreConflict, match="终态"):
            record(store, package, task, invocation, refs)
        projection, job = _start(store, task)
        projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info={"result": material})
        for wrong in ({"owner_instance_uuid": str(uuid4())}, {"task_uuid": str(uuid4())},
                      {"task_payload_digest": "sha256:" + "0" * 64}, {"invocation_uuid": INVOCATION}):
            with pytest.raises(StoreConflict):
                record(store, package, task, invocation, refs, **wrong)
        with pytest.raises(StoreConflict, match="不匹配"):
            record(store, package, task, invocation, evidence(str(uuid4())))
        before = _snapshot(store)
        from tests.app.center_client.test_atomic_workflow_events import _fail_after_write
        with monkeypatch.context() as fault:
            _fail_after_write(fault, terminal=True)
            with pytest.raises(RuntimeError, match="模拟故障"):
                record(store, package, task, invocation, refs)
        assert _snapshot(store) == before
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        record(store, package, task, invocation, refs)
        assert len(_events(store)) == 2


def test_invalid_material_output_preserves_physical_completion(tmp_path):
    with material_workflow(tmp_path) as (store, service, _, _, task, scheduler, invocation):
        projection, job = _start(store, task)
        projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info={"result": "not-a-uuid"})
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        assert store.get_station_workflow_result(task["uuid"]) == {"result": "not-a-uuid"}
        assert store.get_center_outcome_evidence_status(invocation)["pending_reason"] == "material_output_invalid"
        submitted = list(scheduler.submissions)
        service.recover_center_workflow_invocation(invocation)
        assert scheduler.submissions == submitted and len(_events(store)) == 1


def test_outcome_wire_requires_complete_extension_and_checks_refs_times(tmp_path):
    with _workflow(tmp_path, pure=True) as (store, service, _, package, _):
        _submit(service, package)
        outcome = _events(store)[-1]
        assert validate_event(outcome) == outcome
        for field in ("started_at", "finished_at", "material_refs", "container_refs", "artifact_refs"):
            bad = deepcopy(outcome)
            del bad["payload"][field]
            bad["event_digest"] = digest({k: v for k, v in bad.items() if k != "event_digest"})
            with pytest.raises(EventEnvelopeError):
                validate_event(bad)
        for refs in (evidence(str(uuid4())),):
            for mutation in ("station", "handoff", "duplicate", "time"):
                bad = deepcopy(outcome)
                bad["payload"].update(deepcopy(refs))
                if mutation == "station":
                    bad["payload"]["material_refs"][0]["station_uuid"] = str(uuid4())
                elif mutation == "handoff":
                    del bad["payload"]["material_refs"][0]["handoff_item_uuid"]
                elif mutation == "duplicate":
                    bad["payload"]["material_refs"] *= 2
                else:
                    bad["payload"]["started_at"] = "2099-01-01T00:00:00Z"
                bad["event_digest"] = digest({k: v for k, v in bad.items() if k != "event_digest"})
                with pytest.raises(EventEnvelopeError):
                    validate_event(bad)


def test_pending_cancel_has_no_invented_start_and_old_outcome_is_never_rewritten(tmp_path):
    with _workflow(tmp_path) as (store, service, _, package, _):
        task = _submit(service, package)
        from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection
        TaskRuntimeProjection(store).project_canceled(task["uuid"])
        outcome = _events(store)[-1]
        assert outcome["payload"]["started_at"] is None
        legacy = deepcopy(outcome)
        legacy["payload"] = {k: legacy["payload"][k] for k in ("task_payload_digest", "status", "output")}
        legacy["event_digest"] = digest({k: v for k, v in legacy.items() if k != "event_digest"})
        with store.transaction() as connection:
            connection.execute("UPDATE center_boundary_outbox SET event_json=? WHERE phase=1", (json.dumps(legacy),))
        service.recover_center_workflow_invocation(INVOCATION)
        assert _events(store)[-1] == legacy
