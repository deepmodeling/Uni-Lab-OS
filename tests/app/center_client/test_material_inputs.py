"""真实库存、工作流和中心收件箱的跨库准入与崩溃恢复。"""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.app.center_client import test_runtime as runtime_fixture
from tests.app.center_client.test_atomic_workflow_events import RecordingScheduler, _start
from tests.app.center_client.test_center_client import config
from tests.app.test_station_material_inputs import received  # noqa: F401
from tests.workflow.test_c1_published_workflow_contract import _applied_snapshot
from unilabos.app.center_client.catalog import project_contract
from unilabos.app.center_client.client import CenterClient, CenterClientError, TaskPackage
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.runtime import CenterRuntime, submission
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.app.scheduler.inventory.material_inputs import MaterialInputError
from unilabos.center_protocol.digest import digest
from unilabos.workflow.published_contract import PublishedWorkflowContractStore
from unilabos.workflow.service import WorkflowConflict, WorkflowService
from unilabos.workflow.store import StoreConflict, WorkflowStore


@contextmanager
def material_runtime(tmp_path, s, monkeypatch, *, action=False, material_output=False):
    path = tmp_path / "workflow.db"
    store = WorkflowStore(path)
    graph = _applied_snapshot()
    graph["workflow"]["revision"] = 1
    metadata = graph["workflow"]["meta_data"]["unilab"]
    metadata["input_contract"]["parameters"][0]["schema"] = {"type": "string"}
    metadata["output_contract"]["outputs"][0]["schema"] = {"type": "string"}
    metadata["material_ports"] = [{"name": "sample", "direction": "input", "value_path": "$.value",
        "local_value_kind": "material_uuid", "cardinality": "one", "resource_template_uuid": s["inputs"][0]["resource_template_uuid"]}]
    if material_output:
        metadata["material_ports"].append({"name":"sample_output","direction":"output","value_path":"$.result",
            "local_value_kind":"material_uuid","cardinality":"one",
            "resource_template_uuid":s["inputs"][0]["resource_template_uuid"]})
    if action:
        schema = {"type": "object", "required": ["goal"], "properties": {
            "goal": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            "result": {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]},
        }}
        graph["node_templates"][0].update(schema=schema["properties"]["goal"],
            meta_data={"unilab": {"contract_kind": "typed", "action_contract_schema": schema}})
        graph["nodes"][0].update(action_name="inspect_sample", action_type="UniLabJsonCommand")
        graph["nodes"][0]["meta_data"]["unilab"]["executor_binding"] = {"mode": "fixed", "device_id": "material-reader-1"}
        for handle in graph["handle_templates"]:
            handle.update(type="string", meta_data={"unilab": {"value_schema": {"type": "string"}}})
    else:
        metadata["output_bindings"] = {"result": {"kind": "workflow_input", "parameter": "value"}}
        graph["nodes"][0].update(type="group", meta_data={})
        graph["node_templates"][0].update(type="group", node_type="group", name="group")
        graph["handle_templates"] = []
    store.create_workflow(workflow_uuid=graph["workflow"]["uuid"], name="收到物料后运行", tags=[], description=None, meta_data={})
    published = PublishedWorkflowContractStore(store)
    contract = published.publish(graph=graph, expected_revision=1)
    projection = project_contract(published.public(contract))
    body = s["body"]
    body["payload"].update({key: projection[key] for key in (
        "contract_uuid", "workflow_uuid", "publication_version", "revision_fingerprint", "contract_digest", "projection_digest",
    )})
    body["payload"]["input"] = {"value": s["local"]}
    body["payload_digest"] = digest(body["payload"])
    monkeypatch.setattr(runtime_fixture, "STATION", s["ids"]["target"])
    monkeypatch.setattr(runtime_fixture, "INSTANCE", s["ids"]["target_instance"])
    center = runtime_fixture.Center()
    center.package = body
    center.add()
    cfg = config(tmp_path, station_uuid=s["ids"]["target"], instance_uuid=s["ids"]["target_instance"])
    durable = DurableCenterStore(path, station_uuid=cfg.station_uuid, instance_uuid=cfg.instance_uuid)
    service = WorkflowService(store, task_scheduler_bridge=RecordingScheduler(store) if action else None)
    runtime = CenterRuntime(cfg, CenterClient(cfg, center), durable, service,
                            handoffs=SimpleNamespace(authority=s["auth"]["target"], pump=lambda: None))
    try:
        yield runtime, center, service, store, durable
    finally:
        durable.close()
        service.close()


def test_real_submission_requires_input_proof_before_claim_and_uses_fixed_task(tmp_path, received, monkeypatch):
    with material_runtime(tmp_path, received, monkeypatch) as (runtime, center, service, store, durable):
        package = TaskPackage.from_response(received["body"])
        durable.remember_package(package)
        with pytest.raises(CenterClientError, match="库存占用"):
            durable.confirm_claim(package.station_invocation_uuid)
        runtime.poll_once()
        receipt = durable.material_input(package.station_invocation_uuid)["receipt"]
        task = service.find_station_workflow_submission(**submission(package))
        assert task["uuid"] == receipt["local_task_uuid"]
        assert task["status"] == "succeeded"
        assert store.get_station_workflow_result(task["uuid"]) == {"result": received["local"]}
        assert durable.material_input(package.station_invocation_uuid)["state"] == "released"
        assert received["stores"]["target"].query_one("SELECT state FROM station_material_input")["state"] == "released"
        # 原通知重投只有一次领取、一个真实 Task；已有终态可以恢复原证据。
        center.add()
        runtime.poll_once()
        assert center.claims == 1
        assert store.db.execute("SELECT count(*) FROM workflow_task").fetchone()[0] == 1


@pytest.mark.parametrize("window", ["before_claim", "claim_response_lost", "after_claim", "after_task"])
def test_crash_windows_resume_one_fixed_task(tmp_path, received, monkeypatch, window):
    with material_runtime(tmp_path, received, monkeypatch) as (runtime, center, service, store, durable):
        original_claim, original_submit = runtime.client.claim, service.submit_station_workflow
        crash = [True]

        def claim(*args):
            if crash[0] and window == "claim_response_lost":
                original_claim(*args)
                crash[0] = False
                raise CenterTransportError("中心已领取但响应丢失", retryable=True)
            if crash[0] and window == "before_claim":
                crash[0] = False
                raise CenterTransportError("领取响应丢失", retryable=True)
            return original_claim(*args)

        def submit(**kwargs):
            if crash[0] and window == "after_claim":
                crash[0] = False
                raise RuntimeError("领取后进程退出")
            task = original_submit(**kwargs)
            if crash[0] and window == "after_task":
                crash[0] = False
                raise RuntimeError("Task 提交后进程退出")
            return task

        monkeypatch.setattr(runtime.client, "claim", claim)
        monkeypatch.setattr(service, "submit_station_workflow", submit)
        with pytest.raises((CenterTransportError, RuntimeError)):
            runtime.poll_once()
        identity = received["body"]["payload"]["station_invocation_uuid"]
        proof = durable.material_input(identity)
        restarted = CenterRuntime(runtime.config, runtime.client, durable, service, handoffs=runtime.handoffs)
        restarted.poll_once()
        assert durable.material_input(identity)["receipt"] == proof["receipt"]
        assert store.db.execute("SELECT uuid FROM workflow_task").fetchone()[0] == proof["receipt"]["local_task_uuid"]
        assert store.db.execute("SELECT count(*) FROM workflow_task").fetchone()[0] == 1


def test_missing_inventory_hold_after_claim_never_creates_task(tmp_path, received, monkeypatch):
    with material_runtime(tmp_path, received, monkeypatch) as (runtime, _, service, store, durable):
        package = TaskPackage.from_response(received["body"])
        durable.remember_package(package)
        runtime._prepare_material_input(package, None)
        durable.confirm_claim(package.station_invocation_uuid)
        with received["stores"]["target"].transaction() as c:
            c.execute("DELETE FROM station_material_input_resource")
            c.execute("DELETE FROM station_material_input")
        with pytest.raises(MaterialInputError):
            runtime._prepare_material_input(package, None)
        with pytest.raises(MaterialInputError):
            service.submit_station_workflow(**submission(package), center_invocation_uuid=package.station_invocation_uuid)
        assert service.find_station_workflow_submission(**submission(package)) is None
        assert store.db.execute("SELECT count(*) FROM workflow_task").fetchone()[0] == 0


def test_real_device_job_keeps_hold_until_actual_outcome_and_replays_without_resubmission(tmp_path, received, monkeypatch):
    with material_runtime(tmp_path, received, monkeypatch, action=True) as (runtime, center, service, store, durable):
        runtime.poll_once()
        package = TaskPackage.from_response(received["body"])
        task = service.find_station_workflow_submission(**submission(package))
        proof = durable.material_input(package.station_invocation_uuid)
        assert proof["state"] == "claimed"
        assert store.center_material_input_receipt(task["uuid"]) == proof["receipt"]
        jobs = [j for j in store.list_jobs(task["uuid"]) if j["executor_kind"] == "device_action"]
        assert len(jobs) == 1
        assert received["local"] in str(jobs[0]["param"])
        assert received["stores"]["target"].query_one("SELECT state FROM station_material_input")["state"] == "active"
        center.add()
        runtime.poll_once()
        assert service._task_scheduler_bridge.submissions == [task["uuid"]]
        projection, job_uuid = _start(store, task)
        projection.project_job_finished(job_uuid=job_uuid, scheduler_state="success", return_info={"result": received["local"]})
        runtime.poll_once()
        assert durable.material_input(package.station_invocation_uuid)["state"] == "released"
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        assert [j["uuid"] for j in store.list_jobs(task["uuid"]) if j["executor_kind"] == "device_action"] == [job_uuid]


def test_preclaim_cancel_releases_hold_and_permanently_prevents_creation(tmp_path, received, monkeypatch):
    with material_runtime(tmp_path, received, monkeypatch) as (runtime, _, service, store, durable):
        package = TaskPackage.from_response(received["body"])
        durable.remember_package(package)
        runtime._prepare_material_input(package, None)
        durable.reject_unclaimed_material_input(package.station_invocation_uuid)
        runtime._release_material_inputs()
        assert durable.material_input(package.station_invocation_uuid)["state"] == "released"
        with pytest.raises(CenterClientError):
            durable.confirm_claim(package.station_invocation_uuid)
        with pytest.raises((StoreConflict, WorkflowConflict, ValueError)):
            service.submit_station_workflow(**submission(package), center_invocation_uuid=package.station_invocation_uuid)
        assert store.db.execute("SELECT count(*) FROM workflow_task").fetchone()[0] == 0


def test_local_input_issue_survives_restart_and_lost_receipt_without_task(tmp_path, received, monkeypatch):
    with material_runtime(tmp_path, received, monkeypatch) as (runtime, center, service, store, durable):
        identity = received["body"]["payload"]["station_invocation_uuid"]
        request_original = center.request
        attempts = []

        def request(method, path, **kwargs):
            if path.endswith("/material-input-issue"):
                attempts.append(deepcopy(kwargs["json_body"]))
                if len(attempts) == 1:
                    raise CenterTransportError("中心已保存异常，但响应丢失", retryable=True)
                return {"station_invocation_uuid": identity, "reporter_instance_uuid": runtime.config.instance_uuid,
                        "request_digest": digest(attempts[0])}
            return request_original(method, path, **kwargs)

        monkeypatch.setattr(center, "request", request)
        with received["stores"]["target"].transaction() as c:
            c.execute("UPDATE material SET aggregate_version=aggregate_version+1 WHERE uuid=?", (received["local"],))
        runtime.poll_once()
        original = durable.material_input_issue(identity)
        assert original is not None and original["receipt"] is None and len(attempts) == 1
        assert center.claims == 0 and center.reports == []
        # 即使库存又被改回，已发现的异常也不能默默恢复执行。
        with received["stores"]["target"].transaction() as c:
            c.execute("UPDATE material SET aggregate_version=aggregate_version-1 WHERE uuid=?", (received["local"],))
        durable.close()
        reopened = DurableCenterStore(tmp_path / "workflow.db", station_uuid=runtime.config.station_uuid,
                                      instance_uuid=runtime.config.instance_uuid)
        try:
            restarted = CenterRuntime(runtime.config, CenterClient(runtime.config, center), reopened, service, handoffs=runtime.handoffs)
            restarted.poll_once()
            center.add()
            restarted.poll_once()
            assert attempts == [original["request"], original["request"]]
            assert reopened.material_input_issue(identity)["receipt"]["request_digest"] == digest(original["request"])
            assert center.claims == 0 and center.reports == []
            assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
            assert store.db.execute("SELECT COUNT(*) FROM workflow_node_job").fetchone()[0] == 0
            assert received["stores"]["target"].query_one("SELECT COUNT(*) n FROM station_material_input")["n"] == 0
            with pytest.raises(CenterClientError, match="输入异常"):
                reopened.confirm_claim(identity)
        finally:
            reopened.close()


def test_material_projection_delay_retries_without_permanent_issue(tmp_path, received, monkeypatch):
    with material_runtime(tmp_path, received, monkeypatch) as (runtime, center, _, store, durable):
        with received["stores"]["target"].transaction() as c:
            c.execute("UPDATE station_handoff_item SET state='received'")
        runtime.poll_once()
        identity = received["body"]["payload"]["station_invocation_uuid"]
        assert durable.material_input_issue(identity) is None
        assert center.claims == 0
        with received["stores"]["target"].transaction() as c:
            c.execute("UPDATE station_handoff_item SET state='settled'")
        runtime.poll_once()
        assert center.claims == 1
        assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 1


def test_reported_input_issue_blocks_public_creation_after_claim(tmp_path, received, monkeypatch):
    with material_runtime(tmp_path, received, monkeypatch) as (runtime, _, service, store, durable):
        package = TaskPackage.from_response(received["body"])
        durable.remember_package(package)
        runtime._prepare_material_input(package, None)
        durable.confirm_claim(package.station_invocation_uuid)
        durable.remember_material_input_issue(package, "已经领取，但库存证明需要核实")
        with pytest.raises((StoreConflict, WorkflowConflict), match="输入已报告异常"):
            service.submit_station_workflow(**submission(package), center_invocation_uuid=package.station_invocation_uuid)
        assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0


@pytest.mark.parametrize("window", ["confirmed", "response_lost", "prepared", "unclaimed"])
def test_withdrawn_material_contract_resumes_only_previously_claimed_input(tmp_path, received, monkeypatch, window):
    with material_runtime(tmp_path, received, monkeypatch) as (runtime, center, service, store, durable):
        package = TaskPackage.from_response(received["body"])
        request_original = center.request

        def request(method, path, **kwargs):
            if path.endswith("/material-input-issue"):
                return {"station_invocation_uuid": package.station_invocation_uuid,
                        "reporter_instance_uuid": runtime.config.instance_uuid,
                        "request_digest": digest(kwargs["json_body"])}
            return request_original(method, path, **kwargs)

        monkeypatch.setattr(center, "request", request)
        def reconciliation_state(identity, epoch):
            assert identity == package.station_invocation_uuid and epoch == durable.event_epoch_uuid
            if not center.claims:
                raise CenterTransportError("原调用尚未领取", code="INVOCATION_OWNER_CONFLICT", retryable=False)
            return {"payload_digest": package.payload_digest,
                    "state": {"owner_instance_uuid": runtime.config.instance_uuid}}

        monkeypatch.setattr(runtime.client, "reconciliation_state", reconciliation_state)
        durable.remember_package(package)
        if window != "unclaimed":
            runtime._prepare_material_input(package, None)
        if window in {"confirmed", "response_lost"}:
            runtime.client.claim(package.station_invocation_uuid, package.payload_digest, durable.event_epoch_uuid)
        if window == "confirmed":
            durable.confirm_claim(package.station_invocation_uuid)
        service.delete_workflow(package.raw["workflow_uuid"])
        restarted = CenterRuntime(runtime.config, runtime.client, durable, service, handoffs=runtime.handoffs)
        restarted.poll_once()
        if window in {"confirmed", "response_lost"}:
            assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 1
            assert durable.material_input_issue(package.station_invocation_uuid) is None
            assert center.claims == 2
        else:
            assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
            assert center.claims == 0
            if window == "unclaimed":
                assert durable.material_input_issue(package.station_invocation_uuid)["receipt"] is not None
            else:
                assert durable.material_input(package.station_invocation_uuid)["state"] == "released"
