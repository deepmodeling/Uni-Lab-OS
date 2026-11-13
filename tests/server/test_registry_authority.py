"""Registry Authority：条目级版本、workflow 引用冲突、还原与上报 API。"""

from __future__ import annotations

import gzip
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.protocol.base import canonical_hash
from unilabos.server.api.runtime.registry import install_registry_api
from unilabos.server.services.runtime.registry import (
    RegistryAuthorityError,
    RegistryService,
    set_registry_service,
    template_uuid,
)


def _device(name: str, *, module: str = "pkg.mod:Cls", lock=None, goal=None) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": "UniLabJsonCommand",
        "goal": goal or {},
        "handles": {"input": [], "output": []},
    }
    if lock is not None:
        action["materials_need_lock"] = lock
    return {
        "id": name,
        "registry_type": "device",
        "class": {
            "module": module,
            "type": "python",
            "action_value_mappings": {"run": action},
        },
        "handles": [],
    }


def _workflow_template(name: str, display_name: str) -> dict[str, Any]:
    """一个 @workflow 模板条目（``build_workflow_template_payload`` 的输出形状）。"""

    return {
        "id": name,
        "registry_type": "workflow",
        "uuid": "6c4a2f9e-3b1d-5f0a-9c2e-7d8b1a4e5f60",
        "display_name": display_name,
        "description": "",
        "tags": ["demo"],
        "roles": [
            {"role": "class:rack_demo", "label": "rack_demo", "kind": "class",
             "device_class": "rack_demo", "matches": ["rack_demo"]},
        ],
        "nodes": [
            {"key": "step-0", "kind": "action", "role": "class:rack_demo",
             "action_name": "load", "name": "装载", "param": {"site": "A1"}},
        ],
        "edges": [],
    }


def _ref_row(
    name: str,
    action: str,
    *,
    workflow: str = "wf-1",
    workflow_name: str = "工作流A",
    node: str = "node-1",
    node_name: str = "节点1",
) -> dict[str, str]:
    """一行 workflow 节点对模板 action 的引用（store 明细行形状）。"""

    return {
        "template_uuid": template_uuid(name),
        "action": action,
        "node_uuid": node,
        "node_name": node_name,
        "workflow_uuid": workflow,
        "workflow_name": workflow_name,
    }


@pytest.fixture()
def refs():
    return []


@pytest.fixture()
def service(tmp_path, refs):
    # registry 三表落 runtime.db（同一 RUNTIME_DATABASE 规格，无第五个库）
    instance = RegistryService(
        tmp_path / "runtime.db", reference_rows_resolver=lambda: refs
    )
    try:
        yield instance
    finally:
        instance.close()


class TestEntryReport:
    def test_first_report_adds_all_entries_at_v1(self, service) -> None:
        report = service.report([_device("pump"), _device("stirrer")], edge_uuid="e1")

        counts = report["summary"]["counts"]
        assert counts["added"] == 2 and counts["updated"] == 0
        assert report["report_id"] == 1
        assert {t["name"] for t in report["templates"]} == {"pump", "stirrer"}
        entries = {e["name"]: e for e in service.list_entries()}
        assert entries["pump"]["active_version"] == 1
        assert entries["pump"]["status"] == ["active"]

    def test_identical_report_keeps_versions(self, service) -> None:
        service.report([_device("pump")])
        again = service.report([_device("pump")])

        assert again["summary"]["counts"]["unchanged"] == 1
        assert service.list_entries()[0]["active_version"] == 1
        assert service.entry_versions("pump") == [
            {
                "version": 1,
                "created_at_ms": service.entry_versions("pump")[0]["created_at_ms"],
                "source": "edge-report",
                "edge_uuid": "",
                "restored_from": None,
                "content_sha256": service.entry_versions("pump")[0]["content_sha256"],
            }
        ]

    def test_any_field_change_bumps_entry_version(self, service) -> None:
        service.report([_device("pump")])
        changed = _device("pump")
        changed["display_name"] = "只是改名"

        report = service.report([changed])

        assert report["summary"]["updated"] == ["pump"]
        state = service.list_entries()[0]
        assert state["active_version"] == 2 and state["pending_version"] is None

    def test_unreferenced_action_change_updates_automatically(self, service, refs) -> None:
        service.report([_device("pump", goal={"speed": 1})])
        refs.clear()  # 无 workflow 引用

        report = service.report([_device("pump", goal={"speed": 2})])

        assert report["summary"]["updated"] == ["pump"]
        assert service.list_entries()[0]["active_version"] == 2


class TestReferenceConflicts:
    def test_referenced_action_change_goes_pending(self, service, refs) -> None:
        service.report([_device("pump", goal={"speed": 1})])
        refs.append(_ref_row("pump", "run"))

        report = service.report([_device("pump", goal={"speed": 2})])

        pending = report["summary"]["pending"]
        assert pending == [
            {"name": "pump", "conflicts": [{"action": "run", "reason": "action-changed"}]}
        ]
        state = service.list_entries()[0]
        assert state["active_version"] == 1 and state["pending_version"] == 2
        assert state["status"] == ["active", "pending"]

    def test_referenced_action_removed_goes_pending(self, service, refs) -> None:
        service.report([_device("pump")])
        refs.append(_ref_row("pump", "run"))
        without_action = _device("pump")
        without_action["class"]["action_value_mappings"] = {}

        report = service.report([without_action])

        assert report["summary"]["pending"][0]["conflicts"] == [
            {"action": "run", "reason": "action-removed"}
        ]

    def test_broken_reference_does_not_block_update(self, service, refs) -> None:
        service.report([_device("pump")])
        refs.append(_ref_row("pump", "ghost-action"))  # 基线版本也不包含该动作。

        report = service.report([_device("pump", module="pkg.mod:V2")])

        assert report["summary"]["updated"] == ["pump"]

    def test_unreferenced_field_change_with_reference_present(self, service, refs) -> None:
        """被引用的 action 没变时，其他字段变化仍自动生效。"""

        service.report([_device("pump")])
        refs.append(_ref_row("pump", "run"))
        changed = _device("pump")
        changed["description"] = "新描述"

        report = service.report([changed])

        assert report["summary"]["updated"] == ["pump"]
        assert service.list_entries()[0]["active_version"] == 2

    def test_apply_pending_activates_new_version(self, service, refs) -> None:
        service.report([_device("pump", goal={"speed": 1}, lock=["v1"])])
        refs.append(_ref_row("pump", "run"))
        service.report([_device("pump", goal={"speed": 2}, lock=["v2"])])
        assert service.material_lock_parameters("pump", "run") == ["v1"]

        state = service.apply_pending("pump")

        assert state["active_version"] == 2 and state["pending_version"] is None
        assert service.material_lock_parameters("pump", "run") == ["v2"]

    def test_dismiss_pending_keeps_active(self, service, refs) -> None:
        service.report([_device("pump", goal={"speed": 1})])
        refs.append(_ref_row("pump", "run"))
        service.report([_device("pump", goal={"speed": 2})])

        state = service.dismiss_pending("pump")

        assert state["active_version"] == 1 and state["pending_version"] is None
        assert state["pending_conflicts"] == []

    def test_new_report_overrides_stale_pending(self, service, refs) -> None:
        service.report([_device("pump", goal={"speed": 1})])
        refs.append(_ref_row("pump", "run"))
        service.report([_device("pump", goal={"speed": 2})])
        service.report([_device("pump", goal={"speed": 3})])

        state = service.list_entries()[0]
        assert state["active_version"] == 1 and state["pending_version"] == 3

    def test_apply_without_pending_raises(self, service) -> None:
        service.report([_device("pump")])
        with pytest.raises(RegistryAuthorityError, match="no pending"):
            service.apply_pending("pump")

    def test_pending_impacts_list_affected_nodes(self, service, refs) -> None:
        """挂起条目按冲突 action 反查受影响画布节点；无关节点不出现。"""

        service.report([_device("pump", goal={"speed": 1}), _device("stirrer")])
        refs.append(_ref_row("pump", "run", node="n-run", node_name="进料"))
        refs.append(_ref_row("pump", "idle", node="n-idle"))  # 引用了未冲突 action
        service.report([_device("pump", goal={"speed": 2}), _device("stirrer")])

        impacts = service.pending_impacts()

        assert len(impacts) == 1
        impact = impacts[0]
        assert impact["name"] == "pump"
        assert impact["template_uuid"] == template_uuid("pump")
        assert impact["active_version"] == 1 and impact["pending_version"] == 2
        assert impact["conflicts"] == [{"action": "run", "reason": "action-changed"}]
        assert impact["affected_nodes"] == [
            {
                "workflow_uuid": "wf-1",
                "workflow_name": "工作流A",
                "node_uuid": "n-run",
                "node_name": "进料",
                "action": "run",
            }
        ]

    def test_pending_impacts_empty_without_pending(self, service) -> None:
        service.report([_device("pump")])
        assert service.pending_impacts() == []


class TestRemoveRestore:
    def test_missing_entry_is_soft_removed_and_revivable(self, service) -> None:
        service.report([_device("pump"), _device("stirrer")])

        removed_report = service.report([_device("pump")])
        assert removed_report["summary"]["removed"] == ["stirrer"]
        stirrer = {e["name"]: e for e in service.list_entries()}["stirrer"]
        assert stirrer["status"] == ["removed"]
        assert service.material_lock_parameters("stirrer", "run") == []

        revived_report = service.report([_device("pump"), _device("stirrer")])
        assert revived_report["summary"]["revived"] == ["stirrer"]
        stirrer = {e["name"]: e for e in service.list_entries()}["stirrer"]
        assert stirrer["status"] == ["active"] and stirrer["active_version"] == 1

    def test_restore_creates_new_active_version(self, service) -> None:
        service.report([_device("pump", module="pkg.mod:V1")])
        service.report([_device("pump", module="pkg.mod:V2")])

        state = service.restore("pump", 1)

        assert state["active_version"] == 3
        versions = service.entry_versions("pump")
        assert versions[0]["source"] == "restore" and versions[0]["restored_from"] == 1
        detail = service.entry_detail("pump")
        assert detail["active_payload"]["class"]["module"] == "pkg.mod:V1"

    def test_restore_identical_content_is_idempotent(self, service) -> None:
        service.report([_device("pump")])
        state = service.restore("pump", 1)
        assert state["active_version"] == 1
        assert len(service.entry_versions("pump")) == 1

    def test_restore_unknown_version_raises(self, service) -> None:
        service.report([_device("pump")])
        with pytest.raises(RegistryAuthorityError, match="not found"):
            service.restore("pump", 99)


class TestUnusable:
    def test_unusable_entry_records_reason_without_version(self, service) -> None:
        report = service.report(
            [
                {"id": "no-class", "registry_type": "device"},
                {"id": "bad-type", "registry_type": "widget", "class": {}},
                {"registry_type": "device", "class": {"module": "m"}},
            ]
        )

        unusable = {item["id"]: item["reason"] for item in report["summary"]["unusable"]}
        assert unusable == {
            "no-class": "missing-class",
            "bad-type": "invalid-registry-type",
            "": "missing-id",
        }
        states = {e["name"]: e for e in service.list_entries()}
        assert states["no-class"]["active_version"] is None
        assert states["no-class"]["status"] == ["unusable"]
        with pytest.raises(RegistryAuthorityError):
            service.entry_versions("no-class")

    def test_unusable_update_keeps_active_version_serving(self, service) -> None:
        service.report([_device("pump", lock=["vessel"])])

        broken = {"id": "pump", "registry_type": "device"}
        service.report([broken])

        state = service.list_entries()[0]
        assert state["active_version"] == 1
        assert state["unusable_reason"] == "missing-class"
        assert service.material_lock_parameters("pump", "run") == ["vessel"]

        service.report([_device("pump", lock=["vessel"])])
        assert service.list_entries()[0]["unusable_reason"] == ""


class TestLockMirror:
    def test_lock_parameters_from_active_entry(self, service) -> None:
        service.report([_device("pump", lock=["from_vessel", "to_vessel"])])
        assert service.material_lock_parameters("pump", "run") == [
            "from_vessel",
            "to_vessel",
        ]

    def test_lock_parameters_auto_prefix_fallback(self, service) -> None:
        entry = _device("pump", lock=["vessel"])
        mappings = entry["class"]["action_value_mappings"]
        mappings["auto-transfer"] = mappings.pop("run")
        service.report([entry])
        assert service.material_lock_parameters("pump", "transfer") == ["vessel"]

    def test_lock_parameters_empty_before_first_report(self, service) -> None:
        assert service.material_lock_parameters("pump", "run") == []


class TestWorkflowReferenceRows:
    def test_store_rows_join_workflow_and_skip_deleted(self) -> None:
        """store 明细行含 workflow/node 名，软删的节点/工作流不计入。"""

        from unilabos.server.services.runtime.workflow.store import WorkflowStore

        store = WorkflowStore(":memory:")
        pump_uuid = template_uuid("pump")
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO workflow (uuid, create_time, update_time, name, meta_data, tags)"
                " VALUES ('wf-1', 't0', 't0', '合成A', '{}', '[]')"
            )
            conn.execute(
                "INSERT INTO workflow_node_template (uuid, create_time, update_time,"
                " authority_id, resource_template_uuid, name, display_name, type, node_type,"
                " meta_data, goal, goal_default, feedback, result)"
                " VALUES ('tpl-1', 't0', 't0', 'auth', ?, 'run', '运行', 'device', 'action',"
                " '{}', '{}', '{}', '{}', '{}')",
                (pump_uuid,),
            )
            conn.execute(
                "INSERT INTO workflow_node (uuid, create_time, update_time, workflow_uuid,"
                " workflow_node_template_uuid, name, status, type, disabled, minimized,"
                " meta_data, pose, param, execution_policy)"
                " VALUES ('n-1', 't0', 't0', 'wf-1', 'tpl-1', '进料', 'idle', 'device', 0, 0,"
                " '{}', '{}', '{}', '{}')"
            )
            conn.execute(
                "INSERT INTO workflow_node (uuid, create_time, update_time, deleted_at,"
                " workflow_uuid, workflow_node_template_uuid, name, status, type,"
                " disabled, minimized, meta_data, pose, param, execution_policy)"
                " VALUES ('n-gone', 't0', 't0', 't1', 'wf-1', 'tpl-1', '已删', 'idle',"
                " 'device', 0, 0, '{}', '{}', '{}', '{}')"
            )

        rows = store.list_template_action_references()

        assert rows == [
            {
                "template_uuid": pump_uuid,
                "action": "run",
                "node_uuid": "n-1",
                "node_name": "进料",
                "workflow_uuid": "wf-1",
                "workflow_name": "合成A",
            }
        ]


class TestHashedReport:
    """按哈希增量上报：每个条目带 content_sha256，权威没有该哈希的才带 payload。"""

    @staticmethod
    def _entry(definition: dict[str, Any], *, with_payload: bool) -> dict[str, Any]:
        return {
            "id": definition["id"],
            "content_sha256": canonical_hash(definition),
            "payload": definition if with_payload else None,
        }

    def test_first_report_needs_payloads_and_digest_then_holds_them(self, service) -> None:
        pump, stirrer = _device("pump"), _device("stirrer")
        assert service.digest() == {"active": {}, "pending": {}}

        report = service.report_entries(
            [self._entry(pump, with_payload=True), self._entry(stirrer, with_payload=True)],
            edge_uuid="host",
        )
        assert report["summary"]["counts"]["added"] == 2 and report["missing"] == []
        digest = service.digest()
        assert digest["active"] == {"pump": canonical_hash(pump), "stirrer": canonical_hash(stirrer)}
        assert digest["pending"] == {}

        # 第二次只发哈希：全部"未变"，不生成版本，不需要 payload
        again = service.report_entries(
            [self._entry(pump, with_payload=False), self._entry(stirrer, with_payload=False)]
        )
        assert again["summary"]["counts"]["unchanged"] == 2 and again["missing"] == []
        assert service.list_entries()[0]["active_version"] == 1

    def test_unknown_hash_without_payload_is_reported_missing_not_removed(self, service) -> None:
        pump = _device("pump")
        service.report([pump])
        changed = _device("pump", goal={"speed": 9})

        report = service.report_entries([self._entry(changed, with_payload=False)])
        assert report["missing"] == ["pump"]
        assert report["templates"] == []  # 身份只发给已落库的条目
        # 条目仍在上报集合里：不能被当成"消失"而软移除，生效版本也不动
        entry = service.entry_detail("pump")
        assert "removed" not in entry["status"] and entry["active_version"] == 1

        # Host 补上 payload 再报：正常升版本
        report = service.report_entries([self._entry(changed, with_payload=True)])
        assert report["missing"] == [] and report["summary"]["counts"]["updated"] == 1
        assert service.entry_detail("pump")["active_version"] == 2

    def test_claimed_hash_is_not_trusted_when_payload_is_present(self, service) -> None:
        pump = _device("pump")
        report = service.report_entries(
            [{"id": "pump", "content_sha256": "deadbeef", "payload": pump}]
        )
        assert report["summary"]["counts"]["added"] == 1
        assert service.digest()["active"]["pump"] == canonical_hash(pump)

    def test_pending_hash_is_held_and_not_reversioned(self, service, refs) -> None:
        service.report([_device("pump", goal={"speed": 1})])
        refs.append(_ref_row("pump", "run"))
        changed = _device("pump", goal={"speed": 2})
        service.report([changed])  # 被引用的 action 变了 → 挂起 v2
        digest = service.digest()
        assert digest["pending"] == {"pump": canonical_hash(changed)}
        assert digest["active"]["pump"] == canonical_hash(_device("pump", goal={"speed": 1}))

        # Host 重启后再报同一份挂起内容：只发哈希即可，不再多生成版本
        report = service.report_entries([self._entry(changed, with_payload=False)])
        assert report["missing"] == []
        assert [item["name"] for item in report["summary"]["pending"]] == ["pump"]
        assert [v["version"] for v in service.entry_versions("pump")] == [2, 1]

    def test_removed_entry_revives_by_hash_alone(self, service) -> None:
        pump, stirrer = _device("pump"), _device("stirrer")
        service.report([pump, stirrer])
        service.report([pump])  # stirrer 软移除
        assert "removed" in service.entry_detail("stirrer")["status"]
        assert service.digest()["active"]["stirrer"] == canonical_hash(stirrer)

        report = service.report_entries(
            [self._entry(pump, with_payload=False), self._entry(stirrer, with_payload=False)]
        )
        assert report["summary"]["revived"] == ["stirrer"] and report["missing"] == []
        assert service.action_definition("stirrer", "run") is not None

    def test_entries_without_hash_or_payload_are_unusable(self, service) -> None:
        report = service.report_entries([{"id": "pump"}, {"id": "", "content_sha256": "x"}])
        assert [item["reason"] for item in report["summary"]["unusable"]] == [
            "missing-payload-and-hash",
            "missing-id",
        ]


class TestRegistryApi:
    @pytest.fixture()
    def client(self, service):
        app = FastAPI()
        install_registry_api(app)
        set_registry_service(service)
        try:
            yield TestClient(app)
        finally:
            set_registry_service(None)

    def test_gzip_report_roundtrip(self, client) -> None:
        body = gzip.compress(
            json.dumps({"resources": [_device("pump")]}).encode("utf-8")
        )
        response = client.post(
            "/api/v1/registry/reports",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "Authorization": "Bearer edge-report",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["code"] == 0
        data = payload["data"]
        assert data["templates"] == [{"name": "pump", "uuid": template_uuid("pump")}]
        assert data["report_id"] == 1
        assert data["summary"]["counts"]["added"] == 1

    def test_entry_endpoints(self, client, refs) -> None:
        client.post("/api/v1/registry/reports", json={"resources": [_device("a", goal={"v": 1})]})
        refs.append(_ref_row("a", "run"))
        client.post("/api/v1/registry/reports", json={"resources": [_device("a", goal={"v": 2})]})

        pending = client.get("/api/v1/registry/entries", params={"status": "pending"}).json()["data"]
        assert [e["name"] for e in pending["entries"]] == ["a"]

        detail = client.get("/api/v1/registry/entries/a").json()["data"]
        assert detail["active_payload"]["class"]["action_value_mappings"]["run"]["goal"] == {"v": 1}
        assert detail["pending_payload"]["class"]["action_value_mappings"]["run"]["goal"] == {"v": 2}

        impacts = client.get("/api/v1/registry/pending-impacts").json()["data"]["impacts"]
        assert impacts[0]["name"] == "a"
        assert impacts[0]["affected_nodes"][0]["node_uuid"] == "node-1"

        applied = client.post("/api/v1/registry/entries/a/apply").json()["data"]
        assert applied["active_version"] == 2

        restored = client.post("/api/v1/registry/entries/a/restore/1").json()["data"]
        assert restored["active_version"] == 3

        versions = client.get("/api/v1/registry/entries/a/versions").json()["data"]
        assert [v["version"] for v in versions["versions"]] == [3, 2, 1]

        reports = client.get("/api/v1/registry/reports").json()["data"]
        assert reports["total"] == 2

    def test_apply_without_pending_returns_409(self, client) -> None:
        client.post("/api/v1/registry/reports", json={"resources": [_device("a")]})
        response = client.post("/api/v1/registry/entries/a/apply")
        # 没有挂起版本：状态冲突 3003（HTTP 200 信封）
        error_of(response, 3003)

    def test_invalid_body_rejected(self, client) -> None:
        response = client.post(
            "/api/v1/registry/reports",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        error_of(response, 1000)

    def test_missing_service_returns_503(self, service) -> None:
        app = FastAPI()
        install_registry_api(app)
        set_registry_service(None)
        response = TestClient(app).get("/api/v1/registry/entries")
        error_of(response, 5001)

    @pytest.mark.parametrize("malformed", [False, True])
    def test_condition_template_report_validates_branch_references(self, client, service, malformed) -> None:
        template = _workflow_template("demo.workflows:conditional", "条件演示")
        action = template["nodes"][0]
        template["nodes"] = [
            {"key": "condition", "kind": "condition", "name": "判定",
             "param": {"version": 1, "expression": {"lit": True}}},
            {**action, "condition_guards": [{"node_key": "missing" if malformed else "condition", "equals": True}]},
        ]
        template["edges"] = [{"source": "condition", "target": "step-0"}]
        response = client.post("/api/v1/registry/reports", json={"resources": [], "workflow_templates": [template]})
        assert response.status_code == 200
        if malformed:
            assert service.list_workflow_templates() == []
            assert service.entry_detail(template["id"])["unusable_reason"] == "invalid-condition-node"
        else:
            assert service.get_workflow_template(template["uuid"])["nodes"] == template["nodes"]

    def test_workflow_templates_ride_the_snapshot_report(self, client, service) -> None:
        """包里的 @workflow 模板随同一份快照上报（独立键，旧权威忽略），与设备条目同一套
        版本化：内容变了升版本、从快照里消失即软移除、再出现即复活。"""

        template = _workflow_template("demo.workflows:tour", "位点操作演示")
        response = client.post(
            "/api/v1/registry/reports",
            json={"resources": [_device("pump")], "workflow_templates": [template]},
        ).json()
        assert response["code"] == 0
        assert [item["name"] for item in response["data"]["templates"]] == ["demo.workflows:tour", "pump"]
        assert response["data"]["summary"]["counts"]["added"] == 2

        listed = client.get("/api/v1/registry/workflow-templates").json()["data"]["templates"]
        assert [item["uuid"] for item in listed] == [template["uuid"]]
        assert listed[0]["nodes"][0]["action_name"] == "load"
        assert service.get_workflow_template(template["uuid"])["display_name"] == "位点操作演示"

        # 不认识的键类型 / 形状错误的模板进不可用明细，不进模板列表
        broken = {**template, "id": "demo.workflows:broken", "uuid": "not-a-uuid"}
        bad_guide = {**template, "id": "demo.workflows:bad_guide", "guide": {"steps": ["x"]}}
        guided = {
            **template,
            "id": "demo.workflows:guided",
            "guide": {"preparation": ["出库一块板"], "expected": ["T1 有板"], "notes": []},
            "nodes": [{**template["nodes"][0], "description": "把样品放到 A1"}],
        }
        client.post(
            "/api/v1/registry/reports",
            json={
                "resources": [_device("pump")],
                "workflow_templates": [template, broken, bad_guide, guided],
            },
        )
        assert [item["id"] for item in service.list_workflow_templates()] == [
            "demo.workflows:guided",
            "demo.workflows:tour",
        ]
        assert service.entry_detail("demo.workflows:broken")["unusable_reason"] == "missing-template-uuid"
        assert service.entry_detail("demo.workflows:bad_guide")["unusable_reason"] == "invalid-guide"
        stored_guided = next(
            item for item in service.list_workflow_templates() if item["id"] == "demo.workflows:guided"
        )
        assert stored_guided["guide"]["preparation"] == ["出库一块板"]
        assert stored_guided["nodes"][0]["description"] == "把样品放到 A1"

        # 包卸载：快照里没有模板 → 软移除，列表为空；再上报复活
        client.post("/api/v1/registry/reports", json={"resources": [_device("pump")]})
        assert service.list_workflow_templates() == []
        assert "removed" in service.entry_detail("demo.workflows:tour")["status"]
        client.post(
            "/api/v1/registry/reports",
            json={"resources": [_device("pump")], "workflow_templates": [template]},
        )
        assert [item["uuid"] for item in service.list_workflow_templates()] == [template["uuid"]]

        # 只认 resources 的旧客户端形状照常工作；workflow_templates 不是列表则 1000
        error_of(client.post(
            "/api/v1/registry/reports",
            json={"resources": [_device("pump")], "workflow_templates": {"x": 1}},
        ), 1000)

    def test_digest_and_hashed_report_shapes(self, client) -> None:
        pump = _device("pump")
        digest = client.get("/api/v1/registry/digest").json()["data"]
        assert digest == {"protocol_version": "runtime.v1", "active": {}, "pending": {}}

        response = client.post(
            "/api/v1/registry/reports",
            json={
                "protocol_version": "runtime.v1",
                "edge_uuid": "host-a",
                "entries": [{"id": "pump", "content_sha256": canonical_hash(pump), "payload": pump}],
            },
        ).json()
        assert response["code"] == 0
        assert response["data"]["summary"]["counts"]["added"] == 1 and response["data"]["missing"] == []
        assert client.get("/api/v1/registry/digest").json()["data"]["active"] == {
            "pump": canonical_hash(pump)
        }

        # 只发哈希：对得上就"未变"，对不上进 missing
        response = client.post(
            "/api/v1/registry/reports",
            json={"entries": [
                {"id": "pump", "content_sha256": canonical_hash(pump)},
                {"id": "stirrer", "content_sha256": canonical_hash(_device("stirrer"))},
            ]},
        ).json()["data"]
        assert response["summary"]["counts"]["unchanged"] == 1 and response["missing"] == ["stirrer"]

        # 形状错误（entries 不是列表 / 条目缺字段）→ 1000
        error_of(client.post("/api/v1/registry/reports", json={"entries": {"x": 1}}), 1000)
        error_of(client.post(
            "/api/v1/registry/reports", json={"entries": [{"content_sha256": "x"}]}
        ), 1000)


class _SessionOverTestClient:
    """让 TemplateSynchronizer 的 requests.Session 调用落到 FastAPI TestClient 上。"""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.posted: list[dict[str, Any]] = []

    @staticmethod
    def _path(url: str) -> str:
        from urllib.parse import urlsplit

        return urlsplit(url).path

    def get(self, url: str, headers=None, timeout=None):
        return self.client.get(self._path(url), headers=headers or {})

    def post(self, url: str, data=None, headers=None, timeout=None):
        self.posted.append(json.loads(gzip.decompress(data)))
        return self.client.post(self._path(url), content=data, headers=headers or {})


class _FakeRegistry:
    def __init__(self, devices: dict[str, Any]) -> None:
        self.device_type_registry = devices
        self.resource_type_registry: dict[str, Any] = {}
        self.workflow_registry: dict[str, Any] = {}

    def obtain_registry_device_info(self):
        return [dict(item) for item in self.device_type_registry.values()]

    def obtain_registry_resource_info(self):
        return []


class TestTemplateSynchronizerOverHttp:
    """Host 侧上报客户端：首次全量、之后只发哈希、变更只发变更、缺定义补发、旧权威回退。"""

    @pytest.fixture()
    def stack(self, service):
        app = FastAPI()
        install_registry_api(app)
        set_registry_service(service)
        try:
            yield TestClient(app)
        finally:
            set_registry_service(None)

    @staticmethod
    def _payload_names(posted: dict[str, Any]) -> list[str]:
        return sorted(entry["id"] for entry in posted["entries"] if "payload" in entry)

    def test_incremental_report_over_http(self, stack, service) -> None:
        from unilabos.server.backend.legacy_adaptor.sync.templates import TemplateSynchronizer

        registry = _FakeRegistry({"pump": _device("pump"), "stirrer": _device("stirrer")})
        session = _SessionOverTestClient(stack)
        synchronizer = TemplateSynchronizer("http://authority", session=session)

        first = synchronizer.sync(registry)
        assert first.summary["counts"]["added"] == 2
        assert self._payload_names(session.posted[-1]) == ["pump", "stirrer"]  # 首次：全部定义

        second = synchronizer.sync(registry)
        assert second.summary["counts"]["unchanged"] == 2
        assert self._payload_names(session.posted[-1]) == []  # 之后：只剩哈希清单
        assert all("content_sha256" in entry for entry in session.posted[-1]["entries"])
        assert second.template_uuids == {"pump": template_uuid("pump"), "stirrer": template_uuid("stirrer")}

        registry.device_type_registry["pump"] = _device("pump", goal={"speed": 3})
        third = synchronizer.sync(registry)
        assert third.summary["counts"]["updated"] == 1 and third.summary["counts"]["unchanged"] == 1
        assert self._payload_names(session.posted[-1]) == ["pump"]  # 只带变了的那个
        assert service.entry_detail("pump")["active_version"] == 2

    def test_missing_entries_are_resent_with_payload(self, stack, service, monkeypatch) -> None:
        from unilabos.protocol.runtime.registry import RegistryDigest
        from unilabos.server.backend.legacy_adaptor.sync.templates import (
            TemplateSynchronizer,
            collect_registry_templates,
        )

        registry = _FakeRegistry({"pump": _device("pump")})
        session = _SessionOverTestClient(stack)
        synchronizer = TemplateSynchronizer("http://authority", session=session)
        # 索引声称权威已持有该哈希（例如取索引后权威被重置）：第一次只发哈希会被判 missing
        projected_pump = collect_registry_templates(registry)[0][0]
        stale = RegistryDigest(active={"pump": canonical_hash(projected_pump)})
        monkeypatch.setattr(synchronizer, "_fetch_digest", lambda: stale)

        report = synchronizer.sync(registry)
        assert report.summary["counts"]["added"] == 1
        assert len(session.posted) == 2
        assert self._payload_names(session.posted[0]) == []
        assert self._payload_names(session.posted[1]) == ["pump"]

    def test_old_authority_without_digest_gets_full_snapshot(self, stack, monkeypatch) -> None:
        from unilabos.server.backend.legacy_adaptor.sync.templates import TemplateSynchronizer

        registry = _FakeRegistry({"pump": _device("pump")})
        session = _SessionOverTestClient(stack)
        original_get = session.get

        def get_without_digest(url, headers=None, timeout=None):
            if url.endswith("/registry/digest"):
                return stack.get("/api/v1/registry/does-not-exist")  # 旧权威：404
            return original_get(url, headers=headers, timeout=timeout)

        monkeypatch.setattr(session, "get", get_without_digest)
        report = TemplateSynchronizer("http://authority", session=session).sync(registry)
        assert report.summary["counts"]["added"] == 1
        assert "resources" in session.posted[-1] and "entries" not in session.posted[-1]
