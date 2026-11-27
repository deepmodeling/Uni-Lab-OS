"""Material I/O 准备期预检、公开删除与软删除实例列表的回归测试。

- 当前库存即可判定结算必然失败的 IO，必须在准备阶段失败，不能先打开人工确认
  让操作员做实物动作、批准后再被账本拒绝；
- 目标资源正被其他活动 Claim 持有时预检放行（状态可能在对方结算后改变），
  仍按原语义等待后执行；
- 公开删除 ``material_only`` 与整树删除一致：共享来源绑定阻塞、库存状态退出；
- 修复前软删除仍停留在 warehouse 的旧行，公开实例列表按 discarded 投影。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.test_material_io import _install_permit, _release_permit, _source
from tests.scheduler_core.conftest import CoreRuntime, build_core_runtime
from tests.scheduler_core.test_material_io_runtime import _submit_material_io
from unilabos.app.scheduler.inventory.backend_contract import (
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.material_io import MaterialIOError
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.resource_lock import material_lock_key, site_lock_key
from unilabos.workflow.service import WorkflowService


def _world(store: InventoryStore) -> dict[str, Any]:
    """工作台 A/B/C/D 四个库位：A 放带子 TIP 的盒，B 放 10 mL/100 mL 的试剂瓶，C 放空容器，D 空。"""

    backend = BackendResourceService(store)

    def template(name: str, sites: list[dict] | None = None, category: list[str] | None = None) -> str:
        return backend.sync_resource_templates(
            [
                {
                    "id": f"precheck.{name}",
                    "display_name": name,
                    "registry_type": "resource",
                    "available_sites": sites or [],
                    "category": category or [],
                }
            ]
        )["templates"][0]["uuid"]

    tip = template("tip")
    box = template(
        "box",
        [{"index": 0, "label": "T1", "allowed_resource_template_uuids": [tip], "initial_material_template_uuid": tip}],
    )
    bottle = template("bottle", category=["container"])
    cup = template("cup")
    mount_template = template(
        "mount",
        [{"index": index, "label": label, "allowed_resource_template_uuids": []} for index, label in enumerate("ABCD")],
    )
    mount = backend.create_material({"resource_template_uuid": mount_template, "name": "工作台"})
    sites = {site["name"]: site["uuid"] for site in backend.list_sites(mount["uuid"])}
    placed_box = backend.create_material(
        {"resource_template_uuid": box, "name": "吸头盒", "site_placement": {"action": "place", "site_uuid": sites["A"]}}
    )
    loaded = backend.material_io_load(
        {
            "template_uuid": bottle,
            "mount_material_uuid": mount["uuid"],
            "site_uuid": sites["B"],
            "name": "试剂瓶",
            "workflow_id": "precheck-setup",
            "node_id": "bottle",
            "attempt": 1,
            "reagent": {
                "name": "乙醇",
                "cas": "64-17-5",
                "quantity": 10,
                "quantity_unit": "mL",
                "container_capacity": {"max_volume_ul": 100000},
            },
        }
    )
    placed_cup = backend.create_material(
        {"resource_template_uuid": cup, "name": "空杯", "site_placement": {"action": "place", "site_uuid": sites["C"]}}
    )
    return {
        "backend": backend,
        "mount": mount["uuid"],
        "sites": sites,
        "cup_template": cup,
        "box": placed_box["uuid"],
        "tip": placed_box["initial_material_uuids"][0],
        "bottle": loaded["material_uuid"],
        "reagent": loaded["reagent"]["uuid"],
        "cup": placed_cup["uuid"],
    }


def _facts(store: InventoryStore) -> dict[str, Any]:
    return {
        "material": store.query_all("SELECT uuid,parent_uuid,deleted_at FROM material ORDER BY uuid"),
        "site": store.query_all("SELECT uuid,occupied_material_uuid,deleted_at FROM site ORDER BY uuid"),
        "reagent": store.query_all("SELECT uuid,quantity,quantity_unit,deleted_at FROM reagent ORDER BY uuid"),
        "effects": store.query_all(
            "SELECT command_id FROM processed_command WHERE command_id LIKE 'material_io:%' ORDER BY command_id"
        ),
    }


def _active_claims(store: InventoryStore) -> list[dict[str, Any]]:
    return store.query_all("SELECT claim_uuid FROM station_execution_claim WHERE state<>'released'")


# ---------------------------------------------------------------- resolve_resources 预检（单元）


def _resolve(store: InventoryStore, **values: Any):
    return BackendResourceService(store)._material_io.resolve_resources(**values)


@pytest.mark.parametrize(
    "case,code",
    [
        ("load_occupied", "site_occupied"),
        ("unload_bad_disposition", "invalid_parameter"),
        ("unload_detach_with_contents", "invalid_parameter"),
        ("unload_material_only_box", "material_not_empty"),
        ("unload_material_only_bottle", "material_not_empty"),
        ("refill_unit_mismatch", "invalid_parameter"),
        ("refill_over_capacity", "invalid_parameter"),
        ("refill_without_reagent", "reagent_not_found"),
        ("refill_identity_field", "invalid_parameter"),
    ],
)
def test_resolve_resources_rejects_what_settlement_would_reject(tmp_path: Path, case: str, code: str) -> None:
    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        w = _world(store)
        mount, sites = w["mount"], w["sites"]
        values = {
            "load_occupied": dict(operation="load", mount_material_uuid=mount, site="A"),
            "unload_bad_disposition": dict(
                operation="unload", material_uuid=w["cup"], scope="material_only", disposition="destroy"
            ),
            "unload_detach_with_contents": dict(
                operation="unload", material_uuid=w["cup"], scope="material_with_contents", disposition="detach"
            ),
            "unload_material_only_box": dict(
                operation="unload", material_uuid=w["box"], scope="material_only", disposition="detach"
            ),
            "unload_material_only_bottle": dict(
                operation="unload", material_uuid=w["bottle"], scope="material_only", disposition="soft_delete"
            ),
            "refill_unit_mismatch": dict(
                operation="refill", material_uuid=w["bottle"], mount_material_uuid=mount,
                site_uuid=sites["B"], reagent={"quantity": 5, "quantity_unit": "g"},
            ),
            "refill_over_capacity": dict(
                operation="refill", material_uuid=w["bottle"], mount_material_uuid=mount,
                site_uuid=sites["B"], reagent={"quantity": 95, "quantity_unit": "mL"},
            ),
            "refill_without_reagent": dict(
                operation="refill", material_uuid=w["cup"], mount_material_uuid=mount,
                site_uuid=sites["C"], reagent={"quantity": 5, "quantity_unit": "mL"},
            ),
            "refill_identity_field": dict(
                operation="refill", material_uuid=w["bottle"], mount_material_uuid=mount,
                site_uuid=sites["B"], reagent={"name": "乙醇", "quantity": 5, "quantity_unit": "mL"},
            ),
        }[case]
        before = _facts(store)
        with pytest.raises(MaterialIOError) as caught:
            _resolve(store, **values)
        assert caught.value.code == code
        assert _facts(store) == before
    finally:
        store.close()


def test_resolve_resources_accepts_valid_requests_and_omitted_prepare_fields(tmp_path: Path) -> None:
    """合法请求照常返回锁；调用方未提供 disposition/reagent 时只做与旧行为一致的解析。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        w = _world(store)
        mount, sites = w["mount"], w["sites"]
        load = _resolve(store, operation="load", mount_material_uuid=mount, site="D")
        assert site_lock_key(mount, sites["D"]) in {item["lock_key"] for item in load}
        _resolve(store, operation="unload", material_uuid=w["cup"], scope="material_only", disposition="detach")
        _resolve(store, operation="unload", material_uuid=w["box"], scope="material_with_contents",
                 disposition="soft_delete")
        _resolve(store, operation="refill", material_uuid=w["bottle"], mount_material_uuid=mount,
                 site_uuid=sites["B"], reagent={"quantity": 90, "quantity_unit": "mL"})
        # 省略 disposition / reagent：不做参数预检（旧调用方），状态预检仍与结算同口径
        _resolve(store, operation="unload", material_uuid=w["cup"], scope="material_only")
        _resolve(store, operation="refill", material_uuid=w["bottle"], mount_material_uuid=mount, site_uuid=sites["B"])
        with pytest.raises(MaterialIOError) as caught:
            _resolve(store, operation="refill", material_uuid=w["cup"], mount_material_uuid=mount, site_uuid=sites["C"])
        assert caught.value.code == "reagent_not_found"
    finally:
        store.close()


def test_resolve_resources_treats_soft_deleted_occupant_as_empty_like_settlement(tmp_path: Path) -> None:
    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        w = _world(store)
        # 模拟旧数据：占用者已软删除但库位仍写着它；结算会视为空位并清理
        with store.transaction() as connection:
            connection.execute("UPDATE material SET deleted_at='2026-09-01T00:00:00Z' WHERE uuid=?", (w["cup"],))
        _resolve(store, operation="load", mount_material_uuid=w["mount"], site="C")
    finally:
        store.close()


@pytest.mark.parametrize("case", ["load_occupied", "unload_material_only_box", "refill_over_capacity"])
def test_resolve_resources_defers_state_checks_while_resource_is_claimed(tmp_path: Path, case: str) -> None:
    """资源正被活动 Claim 持有（例如下料待确认、消耗进行中）时，调度会先等待；预检不得抢先判死。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        w = _world(store)
        mount, sites = w["mount"], w["sites"]
        held, values = {
            "load_occupied": (
                {material_lock_key(w["box"]), site_lock_key(mount, sites["A"])},
                dict(operation="load", mount_material_uuid=mount, site="A"),
            ),
            "unload_material_only_box": (
                {material_lock_key(w["tip"]), site_lock_key(w["box"], w["backend"].list_sites(w["box"])[0]["uuid"])},
                dict(operation="unload", material_uuid=w["box"], scope="material_only", disposition="detach"),
            ),
            "refill_over_capacity": (
                {material_lock_key(w["bottle"])},
                dict(operation="refill", material_uuid=w["bottle"], mount_material_uuid=mount,
                     site_uuid=sites["B"], reagent={"quantity": 95, "quantity_unit": "mL"}),
            ),
        }[case]
        canonical = {"operation": "unload", "material_uuid": w["box"], "disposition": "soft_delete",
                     "scope": "material_with_contents", "workflow_id": "holder", "node_id": "n", "attempt": 1}
        permit = _install_permit(store, canonical_request=canonical, lock_keys=held, operation="unload")
        _resolve(store, **values)
        # 纯参数错误不受 Claim 影响，仍立即拒绝
        if values["operation"] == "unload":
            with pytest.raises(MaterialIOError):
                _resolve(store, **{**values, "disposition": "destroy"})
        # Claim 释放后状态未变，预检恢复拒绝
        _release_permit(store, permit)
        with pytest.raises(MaterialIOError):
            _resolve(store, **values)
    finally:
        store.close()


# ---------------------------------------------------------------- 调度准备阶段（端到端）


def _io_param(w: dict[str, Any], mode: str, **overrides: Any) -> dict[str, Any]:
    param = {"mode": mode, "resource_template_uuid": None, "mount": {"uuid": w["mount"]}, "site": None,
             "site_uuid": None, "material": None, "disposition": None, "scope": None, "timeout_seconds": 3600}
    param.update(overrides)
    return param


STATIC_FAILURES = {
    "load_occupied_site": ("load", lambda w: dict(resource_template_uuid=w["cup_template"], site="A"), "site_occupied"),
    "unload_detach_structured_box": (
        "unload", lambda w: dict(material={"uuid": w["box"]}, disposition="detach", scope="material_only"),
        "material_not_empty",
    ),
    "unload_bad_disposition": (
        "unload", lambda w: dict(material={"uuid": w["cup"]}, disposition="destroy", scope="material_only"),
        "invalid_parameter",
    ),
    "unload_detach_with_contents": (
        "unload", lambda w: dict(material={"uuid": w["cup"]}, disposition="detach", scope="material_with_contents"),
        "invalid_parameter",
    ),
    "refill_missing_reagent": (
        "refill", lambda w: dict(material={"uuid": w["cup"]}, site="C", reagent={"quantity": 5, "quantity_unit": "mL"}),
        "reagent_not_found",
    ),
    "refill_unit_mismatch": (
        "refill", lambda w: dict(material={"uuid": w["bottle"]}, site="B", reagent={"quantity": 5, "quantity_unit": "g"}),
        "invalid_parameter",
    ),
    "refill_over_capacity": (
        "refill",
        lambda w: dict(material={"uuid": w["bottle"]}, site="B", reagent={"quantity": 95, "quantity_unit": "mL"}),
        "invalid_parameter",
    ),
}


@pytest.fixture
def runtime(tmp_path: Path):
    value = build_core_runtime(tmp_path / "runtime")
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize("variant", sorted(STATIC_FAILURES))
def test_statically_invalid_io_fails_in_prepare_without_manual_confirmation(
    runtime: CoreRuntime, variant: str
) -> None:
    mode, overrides, code = STATIC_FAILURES[variant]
    w = _world(runtime.inventory_store)
    before = _facts(runtime.inventory_store)
    _pending, job_uuid = _submit_material_io(
        runtime, task_name=f"precheck-{variant}", param=_io_param(w, mode, **overrides(w))
    )
    job = WorkflowService(runtime.workflow_store, task_scheduler_bridge=runtime.bridge).get_workflow_node_job(job_uuid)
    assert (job.get("manual_confirmation") or {}).get("status") != "pending", job
    assert job["status"] == "failed", job
    assert code in {str(item.get("code")) for item in job.get("error_info") or []}, job
    assert _facts(runtime.inventory_store) == before
    assert _active_claims(runtime.inventory_store) == []
    assert runtime.dispatcher.dispatched == []


def test_load_waits_for_pending_unload_of_the_same_site_then_succeeds(runtime: CoreRuntime) -> None:
    """将释放的库位：另一任务的下料正持有该位待确认，上料应等待而不是在准备期判为占用失败。"""

    w = _world(runtime.inventory_store)
    service = WorkflowService(runtime.workflow_store, task_scheduler_bridge=runtime.bridge)
    unload, unload_job = _submit_material_io(
        runtime, task_name="precheck-release-unload",
        param=_io_param(w, "unload", material={"uuid": w["box"]}, site="A",
                        disposition="soft_delete", scope="material_with_contents"),
    )
    assert unload["jobs"][0]["manual_confirmation"]["status"] == "pending"
    _load, load_job = _submit_material_io(
        runtime, task_name="precheck-release-load",
        param=_io_param(w, "load", resource_template_uuid=w["cup_template"], site="A"),
    )
    waiting = service.get_workflow_node_job(load_job)
    assert waiting["status"] == "pending", waiting
    assert "manual_confirmation" not in waiting or waiting["manual_confirmation"] is None

    service.decide_manual_confirmation(unload_job, action="approve")
    runtime.bridge.reschedule()
    opened = service.get_workflow_node_job(load_job)
    assert opened["manual_confirmation"]["status"] == "pending", opened
    approved = service.decide_manual_confirmation(load_job, action="approve")
    assert approved["task"]["status"] == "succeeded"
    material = next(item for item in approved["jobs"] if item["uuid"] == load_job)["return_info"]["material"]["uuid"]
    assert runtime.inventory_store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (w["sites"]["A"],)
    )["occupied_material_uuid"] == material


# ---------------------------------------------------------------- 公开删除 material_only 与旧软删除行


def _free_material_bound_by(tmp_path: Path) -> tuple[InventoryStore, BackendResourceService, InventoryService, str, str]:
    """持有方任务以共享来源绑定一只空杯后，自己把它 detach（同任务绑定不阻塞），留下仍被绑定的游离空杯。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    w = _world(store)
    backend = w["backend"]
    inventory = InventoryService(store)
    holder = str(uuid4())
    inventory.admit_task_materials(holder, [_source("shared-cup", w["cup_template"], w["mount"], "shared_source")], [])
    backend.material_io_unload(
        {"material_uuid": w["cup"], "disposition": "detach", "scope": "material_only",
         "workflow_id": holder, "node_id": "detach", "attempt": 1}
    )
    return store, backend, inventory, holder, w["cup"]


def test_public_material_only_delete_waits_for_shared_binding_then_retires_inventory(tmp_path: Path) -> None:
    store, backend, inventory, holder, cup = _free_material_bound_by(tmp_path)
    try:
        before = _facts(store)
        with pytest.raises(BackendContractError) as blocked:
            backend.delete_material(cup, scope="material_only")
        assert blocked.value.code == 6011
        assert "共享来源" in blocked.value.message
        assert _facts(store) == before

        inventory.release_workflow(holder)
        backend.delete_material(cup, scope="material_only")
        assert store.query_one("SELECT deleted_at FROM material WHERE uuid=?", (cup,))["deleted_at"] is not None
        assert store.query_one("SELECT status FROM material_instance WHERE edge_uuid=?", (cup,))["status"] == "discarded"
        with pytest.raises(Exception):
            inventory.mark_instance_used(cup)
        assert store.query_one("SELECT deleted_at FROM material WHERE uuid=?", (cup,))["deleted_at"] is not None
    finally:
        store.close()


def test_instance_list_projects_legacy_soft_deleted_rows_as_discarded(tmp_path: Path) -> None:
    from unilabos.app.scheduler.inventory.api import create_router

    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        w = _world(store)
        # 修复前的软删除只写 deleted_at，库存状态仍是 warehouse
        with store.transaction() as connection:
            connection.execute(
                "UPDATE material SET deleted_at='2026-09-01T00:00:00Z' WHERE uuid IN (?,?)", (w["cup"], w["tip"])
            )
            connection.execute(
                "UPDATE material SET inventory_status='consumed' WHERE uuid=?", (w["tip"],)
            )
        assert store.query_one("SELECT status FROM material_instance WHERE edge_uuid=?", (w["cup"],))["status"] == "warehouse"
        app = FastAPI()
        app.include_router(create_router(InventoryService(store)))
        client = TestClient(app)

        def listed(**params: Any) -> dict[str, str]:
            response = client.get("/api/v1/inventory/instances", params=params)
            assert response.status_code == 200, response.text
            return {row["edge_uuid"]: row["status"] for row in response.json()["instances"]}

        warehouse = listed(status="warehouse")
        assert w["cup"] not in warehouse and w["tip"] not in warehouse
        assert w["box"] in warehouse and w["bottle"] in warehouse
        assert listed(status="discarded").get(w["cup"]) == "discarded"
        assert listed(status="consumed").get(w["tip"]) == "consumed"
        everything = listed()
        assert everything[w["cup"]] == "discarded" and everything[w["box"]] == "warehouse"
    finally:
        store.close()
