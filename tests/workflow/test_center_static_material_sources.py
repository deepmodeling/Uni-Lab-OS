"""原库位来源的中心冻结身份与真实库存准入；本地任务保持独立。"""
import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.app.center_client.test_handoff_outbound_cancellation import cancel, outbound_handoff  # noqa: F401
from tests.app.center_client.test_material_exports import inventory  # noqa: F401
from tests.app.test_station_handoff import uid
from tests.workflow.test_material_source_preflight_inventory import facts, source_runtime
from unilabos.app.scheduler.inventory.handoff import StationHandoffAuthority
from unilabos.center_protocol.digest import digest


def install_source_context(r):
    station, instance, epoch, lab = json.loads(r.inventory.store.query_one(
        "SELECT identity_json FROM station_handoff_identity")["identity_json"])
    session = SimpleNamespace(station_uuid=station, instance_uuid=instance,
        process_uuid=uid(), session_uuid=uid(), session_epoch=1)
    authority = StationHandoffAuthority(r.inventory.store, station_uuid=session.station_uuid,
        instance_uuid=session.instance_uuid, event_epoch_uuid=epoch, lab_uuid=lab)
    r.service.install_center_material_source_authority(authority, lambda: session)
    return authority, session


def test_source_site_proof_freezes_actual_new_sample_without_reserving(tmp_path):
    with source_runtime(tmp_path) as r:
        authority, session = install_source_context(r)
        before = facts(r)
        response = r.client.post("/api/v1/workflows/material-source-bindings", json={
            **r.candidate, "revision_fingerprint": r.contract["source_hash"],
        })
        assert response.status_code == 200, response.text
        proof = response.json()["data"]
        assert proof["binding_version"] == 1
        assert proof["station_uuid"] == session.station_uuid
        assert proof["session_uuid"] == session.session_uuid
        assert len(proof["bindings"]) == 1
        binding = proof["bindings"][0]
        assert binding["material_uuid"] == r.carrier
        assert binding["lineage"] == {"kind": "unseen"}
        assert binding["inventory_snapshot"]["carrier_material_uuid"] == r.carrier
        assert binding["inventory_snapshot"]["materials"][0]["resource_template_uuid"] == r.template
        assert facts(r) == before


@contextmanager
def center_source(r, tmp_path, monkeypatch, bindings, *, late_install=False, existing_center=None):
    from tests.app.center_client import test_runtime as fixture
    from tests.app.center_client.test_center_client import config, package_response
    from unilabos.app.center_client.catalog import project_contract
    from unilabos.app.center_client.client import CenterClient
    from unilabos.app.center_client.durable import DurableCenterStore
    from unilabos.app.center_client.runtime import CenterRuntime
    from unilabos.workflow.published_contract import PublishedWorkflowContractStore

    authority, session = install_source_context(r)
    monkeypatch.setattr(fixture, "STATION", session.station_uuid)
    monkeypatch.setattr(fixture, "INSTANCE", session.instance_uuid)
    projection = project_contract(PublishedWorkflowContractStore(r.store).public(r.contract))
    package = package_response()
    package["payload"].update({key: projection[key] for key in ("contract_uuid", "workflow_uuid",
        "publication_version", "revision_fingerprint", "contract_digest", "projection_digest")})
    package["payload"].update(station_uuid=session.station_uuid, input=r.candidate["input"],
        material_refs=[], container_refs=[], handoff_uuids=[], station_invocation_uuid=uid(), global_task_uuid=uid())
    if bindings:
        package["payload"]["material_source_bindings"] = bindings
        for binding in bindings:
            if binding["lineage"]["kind"] == "retained":
                lineage = binding["lineage"]
                package["payload"]["material_refs"].extend({**ref, "handoff_item_uuid": None} for ref in lineage["material_refs"])
                package["payload"]["container_refs"].append({"container_uuid": lineage["container_uuid"], "state_version": lineage["container_state_version"]})
    package["payload_digest"] = digest(package["payload"])
    class SourceCenter(fixture.Center):
        def request(self, method, path, **kwargs):
            if path.endswith("/reconciliation-state"):
                payload = self.package["payload"]
                state = {"station_invocation_uuid": payload["station_invocation_uuid"],
                    "global_task_uuid": payload["global_task_uuid"], "station_uuid": session.station_uuid,
                    "workflow_uuid": payload["workflow_uuid"], "payload_digest": self.package["payload_digest"],
                    "state": {"dispatch_status": "claimed", "execution_status": "not_started", "attention_state": "clear",
                        "owner_instance_uuid": session.instance_uuid, "local_task_uuid": None, "result_digest": None, "result_revision": 0},
                    "state_revision": 1, "result_revision": 0, "recovery_blocked": False,
                    "result_events": [], "event_receipts": [], "conflicts": [], "history": []}
                state["evidence_digest"] = digest(state)
                return state
            if path.endswith("/material-input-issue"):
                return {"station_invocation_uuid": self.package["payload"]["station_invocation_uuid"],
                    "reporter_instance_uuid": session.instance_uuid, "request_digest": digest(kwargs["json_body"])}
            return super().request(method, path, **kwargs)
    center = existing_center or SourceCenter()
    center.package = package
    center.add()
    cfg = config(tmp_path, station_uuid=session.station_uuid, instance_uuid=session.instance_uuid)
    durable = DurableCenterStore(r.store.path, station_uuid=session.station_uuid, instance_uuid=session.instance_uuid)
    bridge = SimpleNamespace(authority=authority, pump=lambda: None)
    runtime = CenterRuntime(cfg, CenterClient(cfg, center), durable, r.service,
        handoffs=None if late_install else bridge)
    if late_install:
        runtime.install_handoff_bridge(lambda: bridge)
    try:
        yield runtime, center, durable, package
    finally:
        durable.close()


def test_unseen_source_uses_fixed_inventory_receipt_and_replays_one_task(tmp_path, monkeypatch):
    with source_runtime(tmp_path) as r:
        install_source_context(r)
        proof = r.service.material_source_bindings(workflow_uuid=r.candidate["workflow_uuid"],
            revision_fingerprint=r.contract["source_hash"], input_value=r.candidate["input"])
        with center_source(r, tmp_path, monkeypatch, proof["bindings"]) as (runtime, center, durable, package):
            runtime.poll_once()
            identity = package["payload"]["station_invocation_uuid"]
            held = durable.material_input(identity)
            assert held is not None
            task = r.store.get_task(held["receipt"]["local_task_uuid"])
            assert task["status"] == "succeeded"
            assert r.store.get_station_workflow_result(task["uuid"])["sample"]["uuid"] == r.carrier
            center.add()
            runtime.poll_once()
            assert r.store.count_rows("workflow_task") == 1
            assert center.claims == 1


@pytest.mark.parametrize("change", ["missing", "null", "empty", "bindings", "revision_type", "snapshot_null"])
def test_source_admission_rejects_changed_context_before_inventory(tmp_path, monkeypatch, change):
    """已持久化的中心身份不能因上下文缺失、变形或改写而退回本地选料。"""
    from unilabos.workflow.store import StoreConflict

    with source_runtime(tmp_path) as r:
        install_source_context(r)
        proof = r.service.material_source_bindings(workflow_uuid=r.candidate["workflow_uuid"],
            revision_fingerprint=r.contract["source_hash"], input_value=r.candidate["input"])
        with center_source(r, tmp_path, monkeypatch, proof["bindings"]) as (runtime, _, durable, package):
            runtime.poll_once()
            held = durable.material_input(package["payload"]["station_invocation_uuid"])
            task = r.store.get_task(held["receipt"]["local_task_uuid"])
            assert task["status"] == "succeeded"
            changed = deepcopy(task)
            plan = changed["execution_plan"]
            if change == "missing":
                plan.pop("center_material_source_context")
            elif change == "null":
                plan["center_material_source_context"] = None
            elif change == "empty":
                plan["center_material_source_context"] = {}
            elif change == "bindings":
                plan["center_material_source_context"]["bindings"] = []
            else:
                binding = plan["center_material_source_context"]["bindings"][0]
                if change == "revision_type":
                    # Python 的 True == 1，比较冻结 JSON 时仍须区分数值类型。
                    binding["local_revision"] = True
                else:
                    binding["inventory_snapshot"]["materials"][0].pop("parent_material_uuid")

            def unexpected_inventory(*args, **kwargs):
                raise AssertionError("损坏的中心上下文不得进入库存准入")

            monkeypatch.setattr(r.inventory, "admit_task_materials", unexpected_inventory)
            before = facts(r)
            coordinator = r.service._task_scheduler_bridge._material_sources
            with pytest.raises(StoreConflict, match="中心物料来源上下文"):
                coordinator.reconcile(changed, r.store.list_jobs(task["uuid"]))
            assert facts(r) == before


def test_source_context_read_does_not_revalidate_live_inventory(tmp_path, monkeypatch):
    """读取原任务投影不得拿执行后合法变化的库存修订重新核对冻结快照。"""
    with source_runtime(tmp_path) as r:
        install_source_context(r)
        proof = r.service.material_source_bindings(workflow_uuid=r.candidate["workflow_uuid"],
            revision_fingerprint=r.contract["source_hash"], input_value=r.candidate["input"])
        with center_source(r, tmp_path, monkeypatch, proof["bindings"]) as (runtime, _, durable, package):
            runtime.poll_once()
            held = durable.material_input(package["payload"]["station_invocation_uuid"])
            task = r.store.get_task(held["receipt"]["local_task_uuid"])
            r.backend.update_material(r.carrier, {"name": "执行后的合法库存更新"})
            r.service._task_scheduler_bridge._projection.validate_material_source_context(task["uuid"], task["execution_plan"])


def test_local_source_task_cannot_acquire_center_context(tmp_path):
    from unilabos.workflow.store import StoreConflict

    with source_runtime(tmp_path) as r:
        response = r.client.post("/api/v1/workflow-tasks", json={**r.candidate, "run_mode": "normal"})
        assert response.status_code == 201, response.text
        task = r.store.get_task(response.json()["data"]["uuid"])
        assert "center_material_source_context" not in task["execution_plan"]
        plan = {**task["execution_plan"], "center_material_source_context": {"bindings": [], "material_refs": []}}
        with pytest.raises(StoreConflict, match="本地任务不得携带"):
            r.service._task_scheduler_bridge._projection.validate_material_source_context(task["uuid"], plan)


def test_center_shared_source_accepts_empty_internal_context(tmp_path, monkeypatch):
    """只有共享来源时无需静态独占证明，也不能给中心原包补写空证明字段。"""
    with source_runtime(tmp_path, policy="shared_source", center_output=False) as r:
        with center_source(r, tmp_path, monkeypatch, []) as (runtime, _, durable, package):
            runtime.poll_once()
            invocation = package["payload"]["station_invocation_uuid"]
            with r.store.read() as connection:
                inbox = connection.execute("SELECT local_task_uuid FROM center_invocation_inbox WHERE invocation_uuid=?",
                    (invocation,)).fetchone()
            task = r.store.get_task(inbox["local_task_uuid"])
            assert task["status"] == "succeeded"
            assert task["execution_plan"]["center_material_source_context"] == {"bindings": [], "material_refs": []}
            assert "material_source_bindings" not in package["payload"]
            assert durable.material_input(invocation) is None


@pytest.mark.parametrize("lost_claim_ack", [False, True])
def test_claimed_source_canceled_before_local_creation_releases_original_hold(tmp_path, monkeypatch, lost_claim_ack):
    from unilabos.app.center_client.client import TaskPackage
    with source_runtime(tmp_path) as r:
        install_source_context(r)
        proof = r.service.material_source_bindings(workflow_uuid=r.candidate["workflow_uuid"],
            revision_fingerprint=r.contract["source_hash"], input_value=r.candidate["input"])
        with center_source(r, tmp_path, monkeypatch, proof["bindings"]) as (runtime, center, durable, body):
            package = TaskPackage.from_response(body)
            durable.remember_package(package)
            runtime._prepare_material_input(package, None)
            runtime.client.claim(package.station_invocation_uuid, package.payload_digest, durable.event_epoch_uuid)
            if not lost_claim_ack:
                durable.confirm_claim(package.station_invocation_uuid)
            original = durable.material_input(package.station_invocation_uuid)["receipt"]
            center.commands[0]["disposition"] = "canceled"
            center.commands[0]["disposition_revision"] = 1
            center.add_cancel()
            # 用同一持久库重建运行时，模拟领取后、创建前的进程退出。
            from unilabos.app.center_client.runtime import CenterRuntime
            restored = CenterRuntime(runtime.config, runtime.client, durable, r.service, handoffs=runtime.handoffs)
            restored.poll_once()
            restored.poll_once()
            assert r.store.count_rows("workflow_task") == 0
            held = durable.material_input(package.station_invocation_uuid)
            assert held["receipt"] == original and held["state"] == "released"
            assert r.inventory.store.query_one("SELECT state FROM station_material_input")["state"] == "released"
            assert len(center.reports) == 1 and center.reports[0]["event_type"] == "admission_failed"
            assert center.reports[0]["local_task_uuid"] is None
            assert durable.commands() == []
            center.add_cancel()
            restored.poll_once()
            assert len(center.reports) == 1
        # 释放后的同一样品可以被新的中心调用正式占用、运行。
        with center_source(r, tmp_path, monkeypatch, proof["bindings"], existing_center=center) as (runtime, _, durable, body):
            runtime.poll_once()
            assert r.store.count_rows("workflow_task") == 1
            next_hold = durable.material_input(body["payload"]["station_invocation_uuid"])
            assert next_hold["receipt"]["local_task_uuid"] != original["local_task_uuid"]


@pytest.mark.parametrize("fault", ["owner", "digest", "remote_task", "recovery", "command"])
def test_lost_claim_cancellation_requires_matching_remote_fact_and_real_cancel(tmp_path, monkeypatch, fault):
    from unilabos.app.center_client.client import CenterClientError, TaskPackage
    with source_runtime(tmp_path) as r:
        install_source_context(r)
        proof = r.service.material_source_bindings(workflow_uuid=r.candidate["workflow_uuid"],
            revision_fingerprint=r.contract["source_hash"], input_value=r.candidate["input"])
        with center_source(r, tmp_path, monkeypatch, proof["bindings"]) as (runtime, center, durable, body):
            package = TaskPackage.from_response(body)
            durable.remember_package(package)
            runtime._prepare_material_input(package, None)
            runtime.client.claim(package.station_invocation_uuid, package.payload_digest, durable.event_epoch_uuid)
            durable.receive(center.commands[0])
            command = center.add_cancel() if fault != "command" else center.commands[0]
            durable.receive(command)
            snapshot = runtime.client.reconciliation_state(package.station_invocation_uuid, durable.event_epoch_uuid)
            if fault == "owner":
                snapshot["state"]["owner_instance_uuid"] = uid()
            elif fault == "digest":
                snapshot["payload_digest"] = digest({"wrong": True})
            elif fault == "remote_task":
                snapshot["state"]["local_task_uuid"] = uid()
            elif fault == "recovery":
                snapshot["recovery_blocked"] = True
            snapshot["evidence_digest"] = digest({key: value for key, value in snapshot.items() if key != "evidence_digest"})
            with pytest.raises(CenterClientError):
                durable.reject_canceled_before_creation(package, command["command"]["command_uuid"], snapshot)
            assert not durable.claim_confirmed(package.station_invocation_uuid)
            assert not durable.uncreated_terminal(package.station_invocation_uuid)
            assert durable.material_input(package.station_invocation_uuid)["state"] == "prepared"


def test_cancel_then_original_source_site_graph_reuses_same_lineage(outbound_handoff, tmp_path, monkeypatch):
    previous = outbound_handoff
    original_result = previous.workflow.get_station_workflow_result(previous.task["uuid"])
    cancel(previous)
    # 中心双方取消确认后的新容器版本经正式快照应用，不能复用旧导出版本。
    from tests.app.test_station_handoff import package as handoff_package
    previous.s["payload"].update(status="canceled", revision=previous.s["payload"]["revision"] + 1)
    previous.s["payload"]["items"][0]["current_state_version"] = 3
    previous.s["auth"]["source"].apply_center_snapshot(handoff_package(previous.s["payload"]))
    path = tmp_path / "reuse"
    path.mkdir()
    with source_runtime(path, existing_setup=previous.s, existing_workflow_store=previous.workflow) as r:
        authority, _ = install_source_context(r)
        proof = r.service.material_source_bindings(workflow_uuid=r.candidate["workflow_uuid"],
            revision_fingerprint=r.contract["source_hash"], input_value=r.candidate["input"])
        binding = proof["bindings"][0]
        assert binding["lineage"]["kind"] == "retained"
        assert binding["lineage"]["container_state_version"] == 3
        original_lineage = previous.s["payload"]["items"][0]["source_material_refs"][0]["material_lineage_uuid"]
        assert binding["lineage"]["material_refs"][0]["material_lineage_uuid"] == original_lineage
        with center_source(r, path, monkeypatch, proof["bindings"]) as (runtime, _, durable, package):
            runtime.poll_once()
            receipt = durable.material_input(package["payload"]["station_invocation_uuid"])
            assert receipt is not None
            assert r.store.get_task(receipt["receipt"]["local_task_uuid"])["status"] == "succeeded"
            from unilabos.app.scheduler.inventory.material_exports import InventoryMaterialAuthority
            export = InventoryMaterialAuthority(r.inventory.store, station_uuid=authority.station_uuid,
                instance_uuid=authority.instance_uuid, event_epoch_uuid=durable.event_epoch_uuid)
            source = r.store.list_center_material_exports()[0]
            proposal = export.prepare(source)["request"]["items"][0]
            assert proposal["origin"] == {"kind": "retained", "material_lineage_uuid": original_lineage}
            assert proposal["local_material_uuid"] == r.carrier
        assert previous.workflow.get_task(previous.task["uuid"])["status"] == "succeeded"
        assert previous.workflow.get_station_workflow_result(previous.task["uuid"]) == original_result


def test_center_shared_known_source_keeps_original_shared_semantics(outbound_handoff, tmp_path, monkeypatch):
    previous = outbound_handoff
    cancel(previous)
    path = tmp_path / "shared"
    path.mkdir()
    with source_runtime(path, existing_setup=previous.s, existing_workflow_store=previous.workflow,
            policy="shared_source", center_output=False) as r:
        with center_source(r, path, monkeypatch, []) as (runtime, center, durable, package):
            runtime.poll_once()
            task = r.service.find_station_workflow_submission(backend_task_uuid=package["payload"]["global_task_uuid"],
                invocation_key=package["payload"]["invocation_key"], workflow_name=None,
                workflow_id=package["payload"]["workflow_uuid"], revision_fingerprint=package["payload"]["revision_fingerprint"],
                input_value=package["payload"]["input"])
            assert task["status"] == "succeeded"
            assert durable.material_input(package["payload"]["station_invocation_uuid"]) is None


@pytest.mark.parametrize("change", ["swap", "child_revision", "busy", "selector", "node"])
def test_changed_source_never_claims_or_creates_task(tmp_path, monkeypatch, change):
    with source_runtime(tmp_path) as r:
        authority, _ = install_source_context(r)
        child = r.backend.create_material({"resource_template_uuid": r.template, "parent_uuid": r.carrier, "name": "child"})
        proof = r.service.material_source_bindings(workflow_uuid=r.candidate["workflow_uuid"],
            revision_fingerprint=r.contract["source_hash"], input_value=r.candidate["input"])
        if change == "swap":
            r.backend.update_material(r.carrier, {"site_placement": {"action": "remove"}})
            r.backend.create_material({"resource_template_uuid": r.template, "name": "replacement",
                "site_placement": {"action": "place", "site_uuid": r.site}})
        elif change == "child_revision":
            before = r.backend.get_material(r.carrier)["revision"]
            r.backend.update_material(child["uuid"], {"name": "changed-child"})
            assert r.backend.get_material(r.carrier)["revision"] == before
        elif change == "busy":
            from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest
            r.inventory.admit_material_sources(uid(), [MaterialSourceAdmissionRequest(node_id=uid(),
                resource_template_uuid=r.template, custody_policy="task_exclusive",
                requirement=MaterialRequirement(template_id=r.template, instance_uuid=r.carrier))])
        elif change == "selector":
            proof["bindings"][0]["selector_digest"] = digest({"other": True})
        else:
            proof["bindings"][0]["node_uuid"] = uid()
        with center_source(r, tmp_path, monkeypatch, proof["bindings"]) as (runtime, center, durable, package):
            runtime.poll_once()
            assert center.claims == 0
            assert r.store.count_rows("workflow_task") == 0
            assert durable.material_input(package["payload"]["station_invocation_uuid"]) is None


def test_standalone_source_site_task_needs_no_center_configuration(tmp_path):
    with source_runtime(tmp_path) as r:
        response = r.client.post("/api/v1/workflow-tasks", json={**r.candidate, "run_mode": "normal", "meta_data": {}})
        assert response.status_code in {200, 201}, response.text
        assert response.json()["data"]["status"] == "succeeded"
        assert r.inventory.store.query_all("SELECT * FROM station_material_input") == []


def test_original_inventory_admission_replays_after_its_own_revision_change(tmp_path, monkeypatch):
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest
    from unilabos.app.scheduler.inventory.material_inputs import StationMaterialInputAuthority
    from unilabos.app.scheduler.inventory.source_bindings import source_nodes
    with source_runtime(tmp_path) as r:
        authority, _ = install_source_context(r)
        proof = r.service.material_source_bindings(workflow_uuid=r.candidate["workflow_uuid"],
            revision_fingerprint=r.contract["source_hash"], input_value=r.candidate["input"])
        with center_source(r, tmp_path, monkeypatch, proof["bindings"]) as (_, _, _, package):
            held = StationMaterialInputAuthority(authority).prepare(package, input_materials=[])
            prepared = r.service._prepare_task_input(r.graph, input_value=r.candidate["input"],
                run_mode="normal", target_node_uuid=None)
            node = source_nodes(prepared.execution_plan)[0]
            request = MaterialSourceAdmissionRequest(node_id=node["uuid"], resource_template_uuid=r.template,
                custody_policy="task_exclusive", requirement=MaterialRequirement.from_dict(node["material_requirements"][0]),
                center_source_context={"bindings": proof["bindings"], "material_refs": []})
            first = r.inventory.admit_material_sources(held["local_task_uuid"], [request])
            assert r.backend.get_material(r.carrier)["revision"] > proof["bindings"][0]["local_revision"]
            before = facts(r)
            second = r.inventory.admit_material_sources(held["local_task_uuid"], [request])
            assert second["allocations"] == first["allocations"]
            assert facts(r) == before


@pytest.mark.parametrize("change", ["missing_binding", "conflicting_lineage", "pending_export"])
def test_known_identity_cannot_bypass_frozen_source_contract(outbound_handoff, tmp_path, monkeypatch, change):
    previous = outbound_handoff
    cancel(previous)
    path = tmp_path / "invalid-known"
    path.mkdir()
    with source_runtime(path, existing_setup=previous.s, existing_workflow_store=previous.workflow) as r:
        install_source_context(r)
        proof = r.service.material_source_bindings(workflow_uuid=r.candidate["workflow_uuid"],
            revision_fingerprint=r.contract["source_hash"], input_value=r.candidate["input"])
        if change == "missing_binding":
            proof["bindings"] = []
        else:
            # 模拟迟到恢复带来的冲突谱系事实；不能借物理版本未变继续领取旧证明。
            with r.inventory.store.transaction() as c:
                row = dict(c.execute("SELECT * FROM station_material_export LIMIT 1").fetchone())
                row.update(invocation_uuid=uid(), local_task_uuid=uid(), completed=0 if change == "pending_export" else 1)
                c.execute(f"INSERT INTO station_material_export ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
                c.execute("INSERT INTO station_material_export_item VALUES (?,?,?)", (
                    row["invocation_uuid"], r.carrier, None if change == "pending_export" else uid()))
        before = r.store.count_rows("workflow_task")
        with center_source(r, path, monkeypatch, proof["bindings"]) as (runtime, center, durable, package):
            runtime.poll_once()
            assert r.store.count_rows("workflow_task") == before
            identity = package["payload"]["station_invocation_uuid"]
            if change == "missing_binding":
                assert center.claims == 1
                reports = [item for item in center.reports if item["station_invocation_uuid"] == identity]
                assert len(reports) == 1
                event = reports[0]
                assert event["event_type"] == "admission_failed"
                assert event["local_task_uuid"] is None
                assert event["payload"]["output"]["error_info"][0]["code"] == "preflight_failed"
                assert durable.material_input_issue(identity) is None
                assert durable.uncreated_terminal(identity)
                assert durable.events() == []
                center.add()
                runtime.poll_once()
                assert center.claims == 1
                assert [item for item in center.reports if item["station_invocation_uuid"] == identity] == reports
                assert durable.commands() == []
            else:
                assert center.claims == 0
                issue = durable.material_input_issue(identity)
                assert issue is not None and issue["receipt"] is not None
                center.add()
                runtime.poll_once()
                runtime.poll_once()
                assert center.claims == 0 and durable.commands() == []


def test_known_sample_after_cancel_runs_locally_without_center_configuration(outbound_handoff, tmp_path):
    previous = outbound_handoff
    cancel(previous)
    path = tmp_path / "offline-known"
    path.mkdir()
    with source_runtime(path, existing_setup=previous.s, existing_workflow_store=previous.workflow) as r:
        assert not hasattr(r.service, "_center_material_source_authority")
        response = r.client.post("/api/v1/workflow-tasks", json={**r.candidate, "run_mode": "normal", "meta_data": {}})
        assert response.status_code in {200, 201}, response.text
        task = response.json()["data"]
        assert task["status"] == "succeeded"
        assert r.store.get_station_workflow_result(task["uuid"])["sample"]["uuid"] == r.carrier
        assert r.inventory.store.query_all("SELECT * FROM station_material_input") == []


def test_unknown_child_of_registered_carrier_cannot_freeze_partial_tree(outbound_handoff, tmp_path):
    previous = outbound_handoff
    cancel(previous)
    child = previous.backend.create_material({"resource_template_uuid": previous.template,
        "parent_uuid": previous.s["carrier"]["uuid"], "name": "新子物料"})
    path = tmp_path / "partial-tree"
    path.mkdir()
    with source_runtime(path, explicit=True, existing_setup={**previous.s, "carrier": child},
            existing_workflow_store=previous.workflow) as r:
        install_source_context(r)
        response = r.client.post("/api/v1/workflows/material-source-bindings", json={
            **r.candidate, "revision_fingerprint": r.contract["source_hash"]})
        assert response.json()["error"]["code"] == "preflight_failed", response.text
        assert "载体子物料" in response.json()["error"]["msg"]


def test_center_source_busy_before_proof_is_rejected_without_reserving(tmp_path):
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement
    with source_runtime(tmp_path) as r:
        install_source_context(r)
        r.inventory.reserve_workflow(uid(), {uid(): [MaterialRequirement(instance_uuid=r.carrier)]})
        before = facts(r)
        response = r.client.post("/api/v1/workflows/material-source-bindings", json={
            **r.candidate, "revision_fingerprint": r.contract["source_hash"]})
        assert response.json()["error"]["code"] == "preflight_failed"
        assert facts(r) == before


def test_outbound_before_cancel_cannot_become_available_source_proof(outbound_handoff, tmp_path):
    previous = outbound_handoff
    path = tmp_path / "outbound-busy"
    path.mkdir()
    with source_runtime(path, existing_setup=previous.s, existing_workflow_store=previous.workflow) as r:
        install_source_context(r)
        before = facts(r)
        response = r.client.post("/api/v1/workflows/material-source-bindings", json={
            **r.candidate, "revision_fingerprint": r.contract["source_hash"]})
        assert response.json()["error"]["code"] == "preflight_failed", response.text
        assert facts(r) == before


def test_late_inventory_install_and_readonly_runtime_still_serve_source_proof(tmp_path, monkeypatch):
    from unilabos.app.runtime_capabilities import reset_runtime_capabilities, set_runtime_capabilities
    with source_runtime(tmp_path) as r:
        with center_source(r, tmp_path, monkeypatch, [], late_install=True) as (runtime, _, durable, _):
            runtime.client.open_session(durable.event_epoch_uuid)
            try:
                set_runtime_capabilities({"can_edit_definitions": False})
                response = r.client.post("/api/v1/workflows/material-source-bindings", json={
                    **r.candidate, "revision_fingerprint": r.contract["source_hash"]})
                assert response.status_code == 200, response.text
                assert response.json()["data"]["bindings"][0]["material_uuid"] == r.carrier
            finally:
                reset_runtime_capabilities()
