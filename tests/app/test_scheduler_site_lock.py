"""本地 ``transfer_resource`` 的目标库位解析与执行互斥测试。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler

_SITE_UUID_A = "22222222-2222-4222-8222-222222222222"
_SITE_UUID_B = "33333333-3333-4333-8333-333333333333"


@pytest.fixture()
def inventory(
    tmp_path: Path,
) -> Iterator[tuple[InventoryStore, InventoryService, dict[str, str]]]:
    """建立一个父物料、两个空库位和两件待转移物料。

    参数：``tmp_path`` 提供隔离 SQLite 路径。返回：存储、库存服务和稳定身份
    字典。异常：夹具结束时始终关闭数据库。
    """

    store = InventoryStore(str(tmp_path / "site-lock.db"))
    backend = BackendResourceService(store)
    templates = backend.sync_resource_templates(
        [
            {
                "id": "test.site-owner",
                "display_name": "测试库位父物料",
                "registry_type": "resource",
                "class": {},
            },
            {
                "id": "test.transfer-item",
                "display_name": "测试转运物料",
                "registry_type": "material",
                "class": {},
            },
        ]
    )["templates"]
    template_by_name = {item["name"]: item["uuid"] for item in templates}
    owner = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.site-owner"],
            "barcode": "SITE-OWNER",
            "name": "库位父物料",
        }
    )
    source_owner = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.site-owner"],
            "barcode": "SOURCE-OWNER",
            "name": "来源库位父物料",
        }
    )
    first = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.transfer-item"],
            "barcode": "TRANSFER-A",
            "name": "待转物料 A",
        }
    )
    second = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.transfer-item"],
            "barcode": "TRANSFER-B",
            "name": "待转物料 B",
        }
    )
    with store.transaction() as connection:
        for site_uuid, name, order in (
            (_SITE_UUID_A, "A1", 0),
            (_SITE_UUID_B, "B1", 1),
        ):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,'{}',?,?,?,?,NULL,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    "2026-08-25T00:00:00Z",
                    "2026-08-25T00:00:00Z",
                    owner["uuid"],
                    name,
                    order,
                    json.dumps([template_by_name["test.transfer-item"]]),
                ),
            )
        for site_uuid, name, order, occupant in (
            (
                "55555555-5555-4555-8555-555555555551",
                "SOURCE-A",
                0,
                first["uuid"],
            ),
            (
                "55555555-5555-4555-8555-555555555552",
                "SOURCE-B",
                1,
                second["uuid"],
            ),
        ):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,'{}',?,?,?,?,?,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    "2026-08-25T00:00:00Z",
                    "2026-08-25T00:00:00Z",
                    source_owner["uuid"],
                    name,
                    order,
                    json.dumps([template_by_name["test.transfer-item"]]),
                    occupant,
                ),
            )
    try:
        yield (
            store,
            InventoryService(store),
            {
                "owner": owner["uuid"],
                "source_owner": source_owner["uuid"],
                "first": first["uuid"],
                "second": second["uuid"],
            },
        )
    finally:
        store.close()


def _material_reference_schema() -> dict[str, Any]:
    """返回只含稳定 UUID 的规范 ``ResourceSlot`` 锁合同。

    参数：无。返回：带动作物料锁标记的对象 Schema。异常：无。
    """

    return {
        "type": "object",
        "x-unilabos-material-lock": True,
        "properties": {"uuid": {"type": "string", "format": "uuid"}},
        "required": ["uuid"],
        "additionalProperties": False,
    }


def _action_schema(*, include_site_uuid: bool = True) -> dict[str, Any]:
    """返回转运动作冻结合同。

    参数：``include_site_uuid`` 为假时验证只按库位名称选择的显式合同。
    返回：严格 Goal 参数 Schema。异常：无。
    """

    properties: dict[str, Any] = {
        "resource": _material_reference_schema(),
        "mount_resource": _material_reference_schema(),
        "source_warehouse": _material_reference_schema(),
        "source_site": {"type": "string", "default": ""},
        "site": {"type": "string", "default": ""},
    }
    if include_site_uuid:
        properties["site_uuid"] = {
            "type": "string",
            "format": "uuid",
            "default": "",
        }
    return {
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": properties,
                "required": ["resource", "mount_resource"],
                "additionalProperties": False,
            }
        },
        "required": ["goal"],
    }


def _node(
    node_id: str,
    *,
    device_id: str,
    resource_uuid: str,
    owner_uuid: str,
    site_uuid: str = "",
    site: str = "",
    include_site_uuid: bool = True,
    source_contract: bool = False,
    expected_source_site: str = "",
) -> WorkflowNode:
    """构造真实参数形状的 ``transfer_resource`` 工作流节点。

    参数：节点、设备、待转物料、父物料和两个库位选择字段；
    ``include_site_uuid`` 控制合同是否提供稳定库位 UUID 参数。返回：冻结了动作合同的节点。
    异常：无。
    """

    param: dict[str, Any] = {
        "resource": {"uuid": resource_uuid},
        "mount_resource": {"uuid": owner_uuid},
    }
    if site:
        param["site"] = site
    if site_uuid:
        param["site_uuid"] = site_uuid
    if expected_source_site:
        param["source_site"] = expected_source_site
    return WorkflowNode(
        id=node_id,
        device_id=device_id,
        action_name="transfer_resource",
        action_type="goal",
        param=param,
        param_schema=_action_schema(include_site_uuid=include_site_uuid),
        executor_kind="material_transfer",
        action_resource_contract={
            "version": 1,
            "transfer": {
                "material_param": "resource",
                **(
                    {
                        "source_owner_param": "source_warehouse",
                        "source_site_uuid_param": "",
                        "source_site_name_param": "source_site",
                    }
                    if source_contract
                    else {}
                ),
                "target_owner_param": "mount_resource",
                "target_site_uuid_param": ("site_uuid" if include_site_uuid else ""),
                "target_site_name_param": "site",
                "gripper_site_role": "",
            },
        },
        always_free=True,
    )


def test_exact_site_selection_defers_occupancy_to_bound_gate7_authority(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """单候选精确覆盖也必须由 Gate 7 事务判断占用，不能在调度器前置过滤。"""

    store, service, identities = inventory
    with store.transaction() as connection:
        connection.execute(
            "UPDATE site SET occupied_material_uuid=NULL "
            "WHERE occupied_material_uuid=?",
            (identities["second"],),
        )
        connection.execute(
            "UPDATE site SET occupied_material_uuid=? WHERE uuid=?",
            (identities["second"], _SITE_UUID_A),
        )

    class _RecordingStationResources:
        def __init__(self, delegate: Any) -> None:
            self._delegate = delegate
            self.require_available: list[bool] = []

        def resolve_target_site(self, request: Any) -> Any:
            self.require_available.append(request.require_available)
            return self._delegate.resolve_target_site(request)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._delegate, name)

    resources = _RecordingStationResources(service.station_resources)
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=service,
        station_resources=resources,
    )
    admissions: list[dict[str, Any]] = []

    def wait_at_gate7(dispatching: dict[str, Any]) -> bool:
        admissions.append(dispatching)
        return False

    scheduler.bind_dispatch_admission_authority(wait_at_gate7)
    node = _node(
        "a",
        device_id="device-a",
        resource_uuid=identities["first"],
        owner_uuid=identities["owner"],
    )
    node.execution_policy = {
        "target_site_group": [_SITE_UUID_A],
        "target_site_selection": {
            "version": 1,
            "owner_material_uuid": identities["owner"],
            "group_key": "process_input",
            "requested_reference": "owner.A1",
            "strategy": "sort_order",
            "site_uuids": [_SITE_UUID_A],
            "fingerprint": "sha256:exact-selection",
        },
    }

    result = scheduler.submit_workflow(
        WorkflowSpec(workflow_id="wf-exact-gate7", nodes=[node])
    )

    assert result["dispatched"] == []
    # 派发期加锁也会按同一库存权威解析搬运端点（只查不占），所有解析都不能前置过滤占用。
    assert resources.require_available and set(resources.require_available) == {False}
    assert len(admissions) == 1


def test_deferred_site_selection_still_respects_busy_device(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """候选 Site 交给 Gate 7 选择时，设备互斥仍必须在本地生效。"""

    _store, service, identities = inventory
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        station_resources=service.station_resources,
    )
    admissions: list[dict[str, Any]] = []
    scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-device-owner",
            nodes=[
                WorkflowNode(
                    id="owner",
                    device_id="device-a",
                    action_name="run",
                    action_type="goal",
                    param={},
                )
            ],
        )
    )
    scheduler.bind_dispatch_admission_authority(
        lambda dispatching: admissions.append(dispatching) or False
    )
    waiter = _node(
        "waiter",
        device_id="device-a",
        resource_uuid=identities["first"],
        owner_uuid=identities["owner"],
    )
    waiter.always_free = False
    waiter.execution_policy = {
        "target_site_group": [_SITE_UUID_A, _SITE_UUID_B],
        "target_site_selection": {
            "version": 1,
            "owner_material_uuid": identities["owner"],
            "group_key": "process_input",
            "requested_reference": "",
            "strategy": "sort_order",
            "site_uuids": [_SITE_UUID_A, _SITE_UUID_B],
            "fingerprint": "sha256:busy-device-selection",
        },
    }

    result = scheduler.submit_workflow(
        WorkflowSpec(workflow_id="wf-device-waiter", nodes=[waiter])
    )

    assert [item["node_id"] for item in dispatcher.dispatched] == ["owner"]
    assert result["dispatched"] == []
    assert admissions == []


def test_transfer_source_arguments_are_injected_from_site_occupancy(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """机械臂动作来源参数必须使用物料当前 SiteOccupancy，而不是 DAG 猜测值。"""

    _, service, identities = inventory
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=service)

    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-source-injection",
            nodes=[
                _node(
                    "a",
                    device_id="device-a",
                    resource_uuid=identities["first"],
                    owner_uuid=identities["owner"],
                    site_uuid=_SITE_UUID_A,
                    source_contract=True,
                )
            ],
        )
    )

    assert len(result["dispatched"]) == 1
    action_args = dispatcher.dispatched[0]["action_args"]
    assert action_args["source_warehouse"] == {"uuid": identities["source_owner"]}
    assert action_args["source_site"] == "SOURCE-A"


def test_transfer_expected_source_mismatch_fails_before_dispatch(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """作者保留的来源约束与当前占用不一致时必须关闭失败。"""

    _, service, identities = inventory
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=service)

    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-source-mismatch",
            nodes=[
                _node(
                    "a",
                    device_id="device-a",
                    resource_uuid=identities["first"],
                    owner_uuid=identities["owner"],
                    site_uuid=_SITE_UUID_A,
                    source_contract=True,
                    expected_source_site="WRONG-SITE",
                )
            ],
        )
    )

    assert result["dispatched"] == []
    assert dispatcher.dispatched == []
    assert scheduler.workflow_snapshot("wf-source-mismatch")["state"] == "failed"


def test_site_uuid_has_priority_and_fills_canonical_site_name(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """验证只传 ``site_uuid`` 时解析并下发规范库位名。

    参数：``inventory`` 提供隔离库存。返回：无。异常：解析或派发不符合合同
    时由断言报告。
    """

    _, service, identities = inventory
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=service)

    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-site-uuid",
            nodes=[
                _node(
                    "a",
                    device_id="device-a",
                    resource_uuid=identities["first"],
                    owner_uuid=identities["owner"],
                    site_uuid=_SITE_UUID_A,
                )
            ],
        )
    )

    assert len(result["dispatched"]) == 1
    assert dispatcher.dispatched[0]["always_free"] is True
    assert dispatcher.dispatched[0]["action_args"]["site"] == "A1"
    assert dispatcher.dispatched[0]["action_args"]["site_uuid"] == _SITE_UUID_A
    job_uuid = result["dispatched"][0]["job_id"]
    assert set(scheduler.snapshot()["inflight_jobs"][job_uuid]["resource_locks"]) == {
        f"material/{identities['first']}/exclusive",
        (
            f"material/{identities['source_owner']}/site/"
            "55555555-5555-4555-8555-555555555551/exclusive"
        ),
        f"material/{identities['owner']}/site/{_SITE_UUID_A}/exclusive",
    }


def test_explicit_site_name_resolves_without_requiring_site_uuid(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """验证显式名称合同不传库位 UUID 时仍按权威库位解析。

    参数：``inventory`` 提供隔离库存。返回：无。异常：名称解析失效时由断言
    报告。
    """

    _, service, identities = inventory
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=service)
    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-site-name",
            nodes=[
                _node(
                    "a",
                    device_id="device-a",
                    resource_uuid=identities["first"],
                    owner_uuid=identities["owner"],
                    site="a1",
                    include_site_uuid=False,
                )
            ],
        )
    )

    assert len(result["dispatched"]) == 1
    assert dispatcher.dispatched[0]["action_args"] == {
        "resource": {"uuid": identities["first"]},
        "mount_resource": {"uuid": identities["owner"]},
        "site": "A1",
    }


def test_unknown_site_name_fails_closed_without_parent_lock_fallback(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """验证未知库位名失败关闭，不退化成整父物料锁后继续派发。

    参数：``inventory`` 提供隔离库存。返回：无。异常：未知名称越过派发边界
    时由断言报告。
    """

    _, service, identities = inventory
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=service)
    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-legacy-unknown-site",
            nodes=[
                _node(
                    "a",
                    device_id="device-a",
                    resource_uuid=identities["first"],
                    owner_uuid=identities["owner"],
                    site="LEGACY-SITE",
                    include_site_uuid=False,
                )
            ],
        )
    )

    assert result["dispatched"] == []
    assert dispatcher.dispatched == []
    assert scheduler.workflow_snapshot("wf-legacy-unknown-site")["state"] == "failed"


def test_site_uuid_from_another_owner_is_rejected(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """验证库位不属于 ``mount_resource`` 时失败关闭。

    参数：``inventory`` 提供隔离库存。返回：无。异常：错误库位越过派发边界
    时由断言报告。
    """

    _, service, identities = inventory
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=service)
    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-foreign-site",
            nodes=[
                _node(
                    "a",
                    device_id="device-a",
                    resource_uuid=identities["first"],
                    owner_uuid=identities["second"],
                    site_uuid=_SITE_UUID_A,
                )
            ],
        )
    )

    assert result["dispatched"] == []
    assert dispatcher.dispatched == []


def test_same_site_waits_but_distinct_sites_can_run_in_parallel(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """验证真实宿主动作同库位串行、不同库位并行。

    参数：``inventory`` 提供隔离库存。返回：无。异常：库位忙碌键或
    ``always_free`` 设备排队语义失效时由断言报告。
    """

    _, service, identities = inventory
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher(), inventory=service)
    first = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-first",
            nodes=[
                _node(
                    "first",
                    device_id="host_node",
                    resource_uuid=identities["first"],
                    owner_uuid=identities["owner"],
                    site_uuid=_SITE_UUID_A,
                )
            ],
        )
    )
    blocked = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-same-site",
            nodes=[
                _node(
                    "blocked",
                    device_id="host_node",
                    resource_uuid=identities["second"],
                    owner_uuid=identities["owner"],
                    site_uuid=_SITE_UUID_A,
                )
            ],
        )
    )
    parallel = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-other-site",
            nodes=[
                _node(
                    "parallel",
                    device_id="host_node",
                    resource_uuid=identities["second"],
                    owner_uuid=identities["owner"],
                    site_uuid=_SITE_UUID_B,
                )
            ],
        )
    )

    assert len(first["dispatched"]) == 1
    assert blocked["dispatched"] == []
    assert [item["node_id"] for item in parallel["dispatched"]] == ["parallel"]


@pytest.mark.parametrize(
    ("site_uuid", "site", "expected_code"),
    [
        ("not-a-uuid", "", "invalid_site_uuid"),
        (_SITE_UUID_A, "B1", "site_identity_mismatch"),
        ("44444444-4444-4444-8444-444444444444", "", "site_not_found"),
    ],
)
def test_invalid_site_selection_fails_closed(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
    site_uuid: str,
    site: str,
    expected_code: str,
) -> None:
    """验证非法库位选择全部失败关闭。

    参数：``inventory`` 是隔离库存；``site_uuid``、``site`` 和
    ``expected_code`` 描述错误样例并形成稳定工作流身份。返回：无。
    异常：非法选择被派发或工作流没有失败关闭时由断言报告。
    """

    _, service, identities = inventory
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=service)
    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id=f"wf-invalid-{expected_code}",
            nodes=[
                _node(
                    "a",
                    device_id="device-a",
                    resource_uuid=identities["first"],
                    owner_uuid=identities["owner"],
                    site_uuid=site_uuid,
                    site=site,
                )
            ],
        )
    )

    assert result["dispatched"] == []
    assert dispatcher.dispatched == []
    assert (
        scheduler.workflow_snapshot(f"wf-invalid-{expected_code}")["state"] == "failed"
    )


def test_occupied_site_is_rejected_before_dispatch(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """验证目标库位被其他物料占用时在派发前失败。

    参数：``inventory`` 提供可修改的隔离库存。返回：无。异常：占用校验失效
    时由断言报告。
    """

    store, service, identities = inventory
    with store.transaction() as connection:
        connection.execute(
            "UPDATE site SET occupied_material_uuid=NULL "
            "WHERE occupied_material_uuid=?",
            (identities["second"],),
        )
        connection.execute(
            "UPDATE site SET occupied_material_uuid=? WHERE uuid=?",
            (identities["second"], _SITE_UUID_A),
        )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=service)
    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-occupied-site",
            nodes=[
                _node(
                    "a",
                    device_id="device-a",
                    resource_uuid=identities["first"],
                    owner_uuid=identities["owner"],
                    site_uuid=_SITE_UUID_A,
                )
            ],
        )
    )

    assert result["dispatched"] == []
    assert dispatcher.dispatched == []


def test_site_rejects_material_template_not_allowed_by_site(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """验证目标库位拒绝其合同未允许的物料模板。

    参数：``inventory`` 提供可修改的隔离库存。返回：无。异常：不兼容物料
    越过派发边界时由断言报告。
    """

    store, service, identities = inventory
    owner_template = store.query_one(
        "SELECT resource_template_uuid FROM material WHERE uuid=?",
        (identities["owner"],),
    )["resource_template_uuid"]
    with store.transaction() as connection:
        connection.execute(
            "UPDATE site SET allowed_resource_template_uuids=? WHERE uuid=?",
            (json.dumps([owner_template]), _SITE_UUID_A),
        )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=service)
    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-template-not-allowed",
            nodes=[
                _node(
                    "a",
                    device_id="host_node",
                    resource_uuid=identities["first"],
                    owner_uuid=identities["owner"],
                    site_uuid=_SITE_UUID_A,
                )
            ],
        )
    )

    assert result["dispatched"] == []
    assert dispatcher.dispatched == []


def test_missing_site_keeps_legacy_whole_parent_material_lock(
    inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """验证未指定库位时仍独占整个 ``mount_resource``。

    参数：``inventory`` 提供隔离库存。返回：无。异常：遗留互斥语义漂移时
    由断言报告。
    """

    _, service, identities = inventory
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher(), inventory=service)
    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="wf-whole-parent",
            nodes=[
                _node(
                    "a",
                    device_id="device-a",
                    resource_uuid=identities["first"],
                    owner_uuid=identities["owner"],
                )
            ],
        )
    )

    job_uuid = result["dispatched"][0]["job_id"]
    assert set(scheduler.snapshot()["inflight_jobs"][job_uuid]["resource_locks"]) == {
        f"material/{identities['first']}/exclusive",
        f"material/{identities['owner']}/exclusive",
    }


def test_successor_can_use_site_reserved_by_its_continuous_interval(inventory):
    """真实调度回调续持 Site 后，后继转运不能把自己的预留判成其他 Job。"""
    from unilabos.app.scheduler.models import WorkflowEdge
    from unilabos.workflow.resource_lock_key import material_lock_key, site_lock_key
    from unilabos.workflow.resource_lock_plan import (
        bind_station_resource_plan,
        compile_template_resource_plan,
        serialize_resource_plan,
    )

    store, service, identities = inventory
    source_site = store.query_one("SELECT uuid FROM site WHERE occupied_material_uuid=?", (identities["first"],))["uuid"]
    keys = {
        "/devices/device-a": "device",
        material_lock_key(identities["first"]): "material",
        material_lock_key(identities["owner"]): "material",
        material_lock_key(identities["source_owner"]): "material",
        site_lock_key(identities["owner"], _SITE_UUID_A): "material_site",
        site_lock_key(identities["source_owner"], source_site): "material_site",
    }
    graph = {"resources": list(keys), "nodes": [{"uuid": "prepare"}, {"uuid": "place"}],
             "edges": [{"source_node_uuid": "prepare", "target_node_uuid": "place"}]}
    plan = serialize_resource_plan(bind_station_resource_plan(compile_template_resource_plan(graph),
        {key: {"canonical_key": key, "kind": kind} for key, kind in keys.items()}))
    nodes = [WorkflowNode(id="prepare", device_id="device-a", action_name="prepare", action_type="goal"),
             _node("place", device_id="device-a", resource_uuid=identities["first"], owner_uuid=identities["owner"], site_uuid=_SITE_UUID_A)]
    for node in nodes:
        node.resource_plan_id = plan["plan_id"]
        node.resource_interval_ids = [i["interval_id"] for i in plan["intervals"] if node.id in i["node_uuids"]]
        node.resource_acquire_set_id = next((a["acquire_set_id"] for a in plan["acquire_sets"] if a["node_uuid"] == node.id), "")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=service)
    scheduler.submit_workflow(WorkflowSpec(workflow_id="site-continuation", nodes=nodes, resource_plan=plan,
        edges=[WorkflowEdge(uuid="prepare-place", source_node_id="prepare", target_node_id="place")]))
    assert [j["node_id"] for j in dispatcher.dispatched] == ["prepare"]
    scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], True, {})
    assert [j["node_id"] for j in dispatcher.dispatched] == ["prepare", "place"]
