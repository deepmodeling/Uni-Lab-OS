"""公开目录的无损投影、精确提交和重启重传，不以分页缺失推断删除。"""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

from tests.app.center_client.test_center_client import INSTANCE, STATION, config
from unilabos.app.center_client.catalog import (
    CatalogProjectionError,
    make_upload,
    project_contract,
    projected_snapshot,
)
from unilabos.app.center_client.client import CenterClient, CenterClientError
from unilabos.app.center_client.digest import digest
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.transport import CenterTransportError


def contract() -> dict:
    return {
        "uuid": str(uuid4()), "workflow_uuid": str(uuid4()), "version": 2, "workflow_revision": 5,
        "source_hash": "sha256:" + "a" * 64, "contract_digest": "sha256:" + "b" * 64,
        "input_contract": {"version": 1, "parameters": [
            {"name": "amount", "schema": {"type": "number", "minimum": 0},
             "required": False, "default": 1.5, "unit": "mL"},
            {"name": "sample", "schema": {"$slot": "ResourceSlot"},
             "required": True},
        ]},
        "output_contract": {"version": 1, "outputs": [{"name": "value", "schema": {"type": "integer"}}]},
        "executor_requirements": [{"key": "executor_1", "resource_template_uuid": str(uuid4()), "required": True}],
    }


def snapshot() -> dict:
    return projected_snapshot({"contracts": [contract()], "tombstones": []})


def test_site_input_projection_keeps_frozen_scope_and_empty_site_policy():
    public = contract()
    public["input_contract"]["parameters"] = [{"name": "dispatch_position", "schema": {"type": "string"}, "required": True}]
    selector = {"version": 1, "owner": {"kind": "fixed", "material_uuid": str(uuid4())},
                "value_kind": "uuid", "allowed_resource_template_uuids": [str(uuid4())],
                "show_occupied": True, "allow_occupied": False}
    public["site_bindings"] = {"dispatch_position": selector}
    projected = project_contract(public)
    field = projected["input_schema"]["properties"]["dispatch_position"]
    assert field == {"type": "string", "site_selector": selector}
    assert projected["input_schema"]["required"] == ["dispatch_position"]
    assert projected["material_ports"] == []
    for key, invalid in (("version", 2), ("allow_occupied", "false"), ("owner", {"kind": "fixed", "material_uuid": "wrong"})):
        bad = deepcopy(public)
        bad["site_bindings"]["dispatch_position"][key] = invalid
        with pytest.raises(CatalogProjectionError):
            project_contract(bad)


def store(path):
    return DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)


def test_projection_keeps_constraints_opaque_identity_and_private_boundary():
    public = contract()
    projected = project_contract(public)
    assert projected["input_schema"]["required"] == ["sample"]
    assert projected["input_schema"]["properties"]["amount"] == {
        "type": "number", "minimum": 0, "default": 1.5, "unit": "mL",
    }
    slot = projected["input_schema"]["properties"]["sample"]
    assert slot["resource_slot"] == {"allowed_resource_template_uuids": []}
    assert slot["properties"] == {"uuid": {"type": "string", "format": "uuid"}}
    assert projected["material_ports"] == []
    assert projected["capabilities"] == []
    assert "executor_requirements" not in projected
    assert projected["revision_fingerprint"] == public["source_hash"]
    assert projected["contract_digest"] == public["contract_digest"]
    assert projected["projection_digest"] == digest({key: value for key, value in projected.items()
        if key not in {"projection_digest", "revision_fingerprint", "contract_digest"}})
    assert projected["output_schema"]["required"] == ["value"]



@pytest.mark.parametrize("description", ["检测细胞密度\n并返回结果", "", None])
def test_projection_reports_existing_display_metadata_without_changing_execution_identity(description):
    public = contract()
    legacy = project_contract(public)
    public["name"] = "细胞检测"
    public["description"] = description
    projected = project_contract(public)
    assert projected["name"] == "细胞检测"
    if description is None:
        assert "description" not in projected
    else:
        assert projected["description"] == description
    assert projected["projection_digest"] == legacy["projection_digest"]
    assert projected["contract_digest"] == legacy["contract_digest"]
    assert projected["revision_fingerprint"] == legacy["revision_fingerprint"]


@pytest.mark.parametrize("field,value", [("name", ""), ("name", "   "),
                                         ("name", 123), ("description", {"text": "错误类型"})])
def test_projection_rejects_invalid_display_metadata(field, value):
    public = contract()
    public[field] = value
    with pytest.raises(CatalogProjectionError) as failure:
        project_contract(public)
    assert failure.value.path == f"/{field}"


def test_nullable_and_array_resource_slot_are_lossless():
    public = contract()
    public["input_contract"]["parameters"] = [
        {"name": "note", "schema": {"anyOf": [{"type": "string", "enum": ["a"]}, {"type": "null"}]},
         "required": False, "default": None},
        {"name": "samples", "schema": {"type": "array", "items": {
            "$slot": "ResourceSlot", "allowed_resource_template_uuids": [str(uuid4()), str(uuid4())]},
            "minItems": 1, "maxItems": 4}, "required": True},
    ]
    props = project_contract(public)["input_schema"]["properties"]
    assert props["note"] == {"type": ["string", "null"], "enum": ["a", None], "default": None}
    assert props["samples"]["minItems"] == 1
    assert len(props["samples"]["items"]["resource_slot"]["allowed_resource_template_uuids"]) == 2


@pytest.mark.parametrize("schema", [{"type": "object"}, {"$ref": "https://unsafe.example/schema"},
                                    {"anyOf": [{"type": "string"}, {"type": "number"}]}])
def test_unsupported_schema_fails_complete_snapshot_with_path(schema):
    public = contract()
    public["input_contract"]["parameters"][0] = {"name": "bad", "schema": schema, "required": True}
    with pytest.raises(CatalogProjectionError) as failure:
        projected_snapshot({"contracts": [contract(), public], "tombstones": []})
    assert "/parameters/0/schema" in failure.value.path


def test_active_and_tombstone_must_be_disjoint():
    public = contract()
    with pytest.raises(CatalogProjectionError):
        projected_snapshot({"contracts": [public], "tombstones": [{"workflow_uuid": public["workflow_uuid"], "reason": "撤销"}]})


def test_projection_limits_resource_allowlist_and_schema_size():
    public = contract()
    public["input_contract"]["parameters"][1]["schema"]["allowed_resource_template_uuids"] = [str(uuid4()) for _ in range(257)]
    with pytest.raises(CatalogProjectionError, match="256"):
        project_contract(public)
    public = contract()
    public["input_contract"]["parameters"][0]["description"] = "a" * (256 * 1024)
    with pytest.raises(CatalogProjectionError, match="256 KiB"):
        project_contract(public)


def test_projection_rejects_deep_input_before_recursive_parser():
    public = contract()
    nested = {}
    cursor = nested
    for _ in range(100):
        cursor["next"] = {}
        cursor = cursor["next"]
    public["meta_data"] = nested
    with pytest.raises(CatalogProjectionError, match="深度"):
        project_contract(public)


def test_empty_catalog_has_one_chunk_and_identity_covered_digest():
    data = {"contracts": [], "tombstones": []}
    upload = make_upload(data, station_uuid=STATION, epoch=str(uuid4()), sequence=1, version=str(uuid4()))
    assert len(upload["chunks"]) == 1
    chunk = upload["chunks"][0]
    assert chunk["chunk_digest"] == digest({key: value for key, value in chunk.items() if key != "chunk_digest"})
    assert upload["manifest"]["catalog_digest"] == digest({key: value for key, value in chunk.items()
                                                          if key not in {"chunk_digest", "chunk_index"}})


def test_restart_resends_exact_version_before_observing_changed_source(tmp_path):
    path = tmp_path / "center.db"
    data = snapshot()
    first = store(path)
    frozen = first.catalog_upload(data, None)
    first.close()
    restarted = store(path)
    try:
        assert restarted.catalog_upload(snapshot(), None) == frozen
        assert restarted.catalog_upload(data, frozen["receipt"]) is None
        changed = deepcopy(data)
        changed["contracts"][0]["workflow_revision"] += 1
        next_upload = restarted.catalog_upload(changed, frozen["receipt"])
        assert next_upload["receipt"]["catalog_epoch_uuid"] == frozen["receipt"]["catalog_epoch_uuid"]
        assert next_upload["receipt"]["catalog_sequence"] == 2
        assert next_upload["receipt"]["catalog_version"] != frozen["receipt"]["catalog_version"]
    finally:
        restarted.close()


def test_ack_loss_server_receipt_confirms_exact_manifest(tmp_path):
    durable = store(tmp_path / "center.db")
    try:
        data = snapshot()
        upload = durable.catalog_upload(data, None)
        assert durable.catalog_upload(data, upload["receipt"]) is None
        with pytest.raises(CenterClientError, match="丢失已确认目录"):
            durable.catalog_upload(data, None)
        forged = deepcopy(upload["receipt"])
        forged["catalog_digest"] = "sha256:" + "c" * 64
        with pytest.raises(CenterClientError):
            durable.catalog_upload(data, forged)
    finally:
        durable.close()


def test_missing_local_catalog_adopts_server_epoch_and_advances(tmp_path):
    old = make_upload(snapshot(), station_uuid=STATION, epoch=str(uuid4()), sequence=10, version=str(uuid4()))
    durable = store(tmp_path / "center.db")
    try:
        upload = durable.catalog_upload(snapshot(), old["receipt"])
        assert upload["receipt"]["catalog_epoch_uuid"] == old["receipt"]["catalog_epoch_uuid"]
        assert upload["receipt"]["catalog_sequence"] == 11
    finally:
        durable.close()


class CatalogCenter:
    def __init__(self):
        self.current = None
        self.chunks = []
        self.lost_commit = True

    def request(self, method, path, *, json_body=None, session_headers=None, **kwargs):
        if path.endswith("/session"):
            return {"session_uuid": str(uuid4()), "session_epoch": 1,
                    "station_uuid": STATION, "instance_uuid": INSTANCE}
        if path.endswith("/current"):
            assert kwargs["allow_null_response"]
            return deepcopy(self.current)
        if "/chunks/" in path:
            self.chunks.append(deepcopy(json_body))
            return {key: json_body[key] for key in ("station_uuid", "catalog_version", "chunk_index", "chunk_digest")}
        assert path.endswith("/commit")
        assert session_headers["Idempotency-Key"] == json_body["catalog_version"]
        self.current = {key: value for key, value in json_body.items() if key not in {"protocol_version", "chunk_digests"}}
        if self.lost_commit:
            self.lost_commit = False
            raise CenterTransportError("响应丢失", retryable=True)
        return deepcopy(self.current)


def test_client_validates_chunk_and_manifest_and_retries_exactly(tmp_path):
    remote = CatalogCenter()
    client = CenterClient(config(tmp_path), remote)
    durable = store(tmp_path / "center.db")
    try:
        data = snapshot()
        upload = durable.catalog_upload(data, client.current_catalog(durable.event_epoch_uuid))
        with pytest.raises(CenterTransportError):
            client.upload_catalog(upload, durable.event_epoch_uuid)
        assert durable.catalog_upload(data, client.current_catalog(durable.event_epoch_uuid)) is None
        assert remote.chunks == upload["chunks"]
    finally:
        durable.close()


def test_pending_catalog_is_recovered_before_invalid_new_source(tmp_path):
    from tests.app.center_client.test_runtime import Center, Service
    from unilabos.app.center_client.runtime import CenterRuntime

    class Remote(CatalogCenter, Center):
        def __init__(self):
            CatalogCenter.__init__(self)
            Center.__init__(self)
            self.lost_commit = False

        def request(self, method, path, **kwargs):
            if "/workflow-catalog/" in path:
                return CatalogCenter.request(self, method, path, **kwargs)
            return Center.request(self, method, path, **kwargs)

    class InvalidNewSource(Service):
        def export_published_workflow_catalog(self):
            raise AssertionError("旧待传版本必须先恢复，不能读取新源码")

    durable = store(tmp_path / "center.db")
    frozen = durable.catalog_upload(snapshot(), None)
    remote = Remote()
    run = CenterRuntime(config(tmp_path), CenterClient(config(tmp_path), remote), durable, InvalidNewSource())
    try:
        run.poll_once()
        assert remote.current == frozen["receipt"]
        assert durable.pending_catalog_upload() is None
    finally:
        run.close()


def _sync_request(center, payload=None):
    envelope = center.add()
    envelope["command"]["type"] = "catalog.sync_requested"
    envelope["command"]["payload"] = {"reason": "中心请求核实目录"} if payload is None else payload
    envelope["command_digest"] = digest(envelope["command"])
    return envelope


def test_sync_command_replays_unchanged_catalog_after_tasks_and_survives_restart(tmp_path):
    from tests.app.center_client.test_runtime import Center, Service, runtime

    class DirectoryService(Service):
        def export_published_workflow_catalog(self):
            return {"contracts": [], "tombstones": []}

    class Remote(Center):
        def __init__(self):
            super().__init__()
            self.commits = []
            self.fail_catalog = False

        def request(self, method, path, **kwargs):
            if "/chunks/" in path and self.fail_catalog:
                raise CenterTransportError("目录网络故障", retryable=True)
            if path.endswith("/commit"):
                self.commits.append(deepcopy(kwargs["json_body"]))
            return super().request(method, path, **kwargs)

    remote, service = Remote(), DirectoryService()
    first = runtime(tmp_path, remote, service)
    first.poll_once()
    original = deepcopy(remote.catalog_receipt)
    command = _sync_request(remote)
    remote.add()
    remote.fail_catalog = True
    with pytest.raises(CenterTransportError):
        first.poll_once()
    assert remote.claims == 1
    assert len(service.calls) == 1
    assert remote.reports  # 目录网络故障发生前，已处理后续任务并补报受理事件。
    assert [item["command"]["command_uuid"] for item in first.store.commands()] == [command["command"]["command_uuid"]]
    first.close()

    remote.fail_catalog = False
    restarted = runtime(tmp_path, remote, service)
    try:
        restarted.poll_once()
        assert restarted.store.commands() == []
        assert remote.catalog_receipt == original
        assert remote.commits[0] == remote.commits[1]
        assert len(service.calls) == 1
        restarted.store.receive(command)  # 重复送达不能重新打开已完成命令。
        restarted.poll_once()
        assert len(remote.commits) == 2
        _sync_request(remote)
        restarted.poll_once()
        assert remote.commits[2] == remote.commits[0]
    finally:
        restarted.close()


@pytest.mark.parametrize("payload", [{}, {"reason": ""}, {"reason": 1},
                                     {"reason": "x", "extra": True}, {"reason": "x" * 501}])
def test_invalid_sync_command_is_quarantined_without_forced_upload(tmp_path, payload):
    from tests.app.center_client.test_runtime import Center, Service, runtime

    class DirectoryService(Service):
        def export_published_workflow_catalog(self):
            return {"contracts": [], "tombstones": []}

    remote = Center()
    run = runtime(tmp_path, remote, DirectoryService())
    try:
        run.poll_once()
        old_receipt = deepcopy(remote.catalog_receipt)
        _sync_request(remote, payload)
        run.poll_once()
        assert run.store.commands() == []
        assert remote.catalog_receipt == old_receipt
    finally:
        run.close()


def test_explicit_withdrawal_survives_restart_and_frozen_claim_still_executes(tmp_path):
    from tests.app.center_client.test_atomic_workflow_events import _publish
    from unilabos.app.center_client.client import TaskPackage
    from unilabos.app.center_client.runtime import submission
    from unilabos.workflow.publication_catalog import WorkflowPublicationCatalog
    from unilabos.workflow.published_contract import PublishedWorkflowContractStore
    from unilabos.workflow.service import WorkflowConflict, WorkflowService
    from unilabos.workflow.store import WorkflowStore

    path = tmp_path / "history.db"
    definitions = WorkflowStore(":memory:")
    response = _publish(definitions, pure=True)
    full = PublishedWorkflowContractStore(definitions).get_by_revision_fingerprint(
        workflow_uuid=response["payload"]["workflow_uuid"],
        revision_fingerprint=response["payload"]["revision_fingerprint"],
    )
    publication_root = tmp_path / "package"
    publication_root.mkdir()
    authority = WorkflowPublicationCatalog(package_root=publication_root, package_root_identity=(
        publication_root.stat().st_dev, publication_root.stat().st_ino,
    ))
    authority.save(source_draft_hash="sha256:" + "c" * 64, contract=full)
    service = WorkflowService(WorkflowStore(path), definition_store=definitions)
    service._publication_catalog = authority
    durable = store(path)
    response["payload"]["contract_uuid"] = full["uuid"]
    response["payload_digest"] = digest(response["payload"])
    package = durable.remember_package(TaskPackage.from_response(response))
    durable.confirm_claim(package.station_invocation_uuid)
    identity = full["workflow_uuid"]
    assert len(service.export_published_workflow_catalog()["contracts"]) == 1
    service.delete_workflow(identity)
    assert authority.list_entries()[0]["contract"]["uuid"] == full["uuid"]
    assert service.export_published_workflow_catalog()["contracts"] == []
    assert service.export_published_workflow_catalog()["tombstones"][0]["workflow_uuid"] == identity
    with pytest.raises(WorkflowConflict):
        service.submit_station_workflow(**submission(package))
    durable.close()
    service.close()

    restarted = WorkflowService(WorkflowStore(path))
    restarted._publication_catalog = authority
    try:
        assert restarted.export_published_workflow_catalog()["contracts"] == []
        assert len(restarted.export_published_workflow_catalog()["tombstones"]) == 1
        task = restarted.submit_station_workflow(**submission(package), center_invocation_uuid=package.station_invocation_uuid)
        replay = restarted.submit_station_workflow(**submission(package), center_invocation_uuid=package.station_invocation_uuid)
        assert replay["uuid"] == task["uuid"]
        assert task["status"] == "succeeded"
        assert restarted.runtime_store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 1
    finally:
        restarted.close()


def test_delete_failure_retains_explicit_tombstone_until_republish(tmp_path, monkeypatch):
    from tests.app.center_client.test_atomic_workflow_events import _publish
    from unilabos.workflow.published_contract import PublishedWorkflowContractStore
    from unilabos.workflow.service import WorkflowService
    from unilabos.workflow.store import WorkflowStore

    definitions = WorkflowStore(tmp_path / "history.db")
    response = _publish(definitions, pure=True)
    service = WorkflowService(definitions)
    identity = response["payload"]["workflow_uuid"]
    full = PublishedWorkflowContractStore(definitions).get_by_revision_fingerprint(
        workflow_uuid=identity, revision_fingerprint=response["payload"]["revision_fingerprint"],
    )

    def fail(*args, **kwargs):
        raise OSError("源码删除失败")

    try:
        monkeypatch.setattr(definitions, "delete_workflow", fail)
        with pytest.raises(OSError):
            service.delete_workflow(identity)
        snapshot_value = service.export_published_workflow_catalog()
        assert snapshot_value["contracts"] == []
        assert snapshot_value["tombstones"][0]["workflow_uuid"] == identity
        assert service.get_workflow(identity)["uuid"] == identity
        monkeypatch.setattr(service, "get_graph", lambda _identity: deepcopy(full["graph_snapshot"]))
        service.publish_workflow_contract(identity, revision=1)
        assert service.export_published_workflow_catalog()["tombstones"] == []
        assert len(service.export_published_workflow_catalog()["contracts"]) == 1
    finally:
        service.close()


@pytest.mark.parametrize("field", ["graph", "node_count", "edge_count", "boundary_mapping"])
def test_historical_contract_recomputes_frozen_graph_digest(tmp_path, field):
    from tests.app.center_client.test_atomic_workflow_events import _publish
    from unilabos.workflow.published_contract import (
        PublishedContractInvalid,
        PublishedWorkflowContractStore,
        validate_historical_contract,
    )
    from unilabos.workflow.store import WorkflowStore

    definitions = WorkflowStore(tmp_path / "history.db")
    try:
        response = _publish(definitions, pure=True)
        full = PublishedWorkflowContractStore(definitions).get_by_revision_fingerprint(
            workflow_uuid=response["payload"]["workflow_uuid"],
            revision_fingerprint=response["payload"]["revision_fingerprint"],
        )
        validate_historical_contract(full)
        if field == "graph":
            full["graph_snapshot"]["nodes"][0]["description"] = "被改写的执行图"
        elif field == "boundary_mapping":
            full[field] = {"tampered": True}
        else:
            full[field] += 1
        with pytest.raises(PublishedContractInvalid):
            validate_historical_contract(full)
    finally:
        definitions.close()

def test_v2_capability_is_explicit_frozen_metadata_not_parameter_inference():
    from unilabos.workflow.published_contract import PublishedWorkflowContractStore
    public = contract()
    public['input_contract'] = {'version': 1, 'parameters': [{'name': 'center_transport', 'schema': {'type': 'string'}, 'required': True}]}
    assert project_contract(public)['capabilities'] == []
    raw = {**public, 'graph_snapshot': {'workflow': {'meta_data': {'center_capabilities': ['center_transport.placement.v2']}}}}
    frozen_public = PublishedWorkflowContractStore._public(raw)
    assert project_contract(frozen_public)['capabilities'] == ['center_transport.placement.v2']
    raw['graph_snapshot']['workflow']['meta_data'] = {}
    assert 'capabilities' not in PublishedWorkflowContractStore._public(raw)


def test_placement_capability_coexists_with_fleet_and_handoff_capabilities():
    public = contract()
    public["capabilities"] = ["center_transport.placement.v2"]
    public["transport_allocation"] = {
        "schema_version": 1, "role": "transport_vehicle", "executor_key": "executor_1",
        "resource_alias": "agv-1", "equivalent_devices": ["agv-1", "agv-2"],
    }
    public["transport_pickup"] = True
    public["handoff_operations"] = {"schema_version": 1}
    assert project_contract(public)["capabilities"] == [
        "center_transport.placement.v2", "transport_preparation.v1",
        "allocatable_executor_binding.v1", "transport_settlement_hold.v1",
        "transport_pickup.v1", "handoff_endpoint_snapshot.v1",
        "manual_handoff_operations.v1", "ingress_renewal.v1",
    ]
