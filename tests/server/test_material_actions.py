"""``MaterialRole.move`` / ``pick`` / ``place`` 的调度解析与按类型加锁（真实物料权威，不起调度器）。

- 所属方由库位 uuid 决定：载架（仓库）或设备根节点；
- 搬运设备省略即执行设备，写了是 ``DeviceSlot``（设备 id 解析成设备物料）；
- 两步搬运：pick 后物料在夹爪库位、托管搬运设备；place 后在目标库位、夹爪空出、托管释放；
- ``DeviceSlot`` 参数加设备锁，``SiteSlot`` 参数加库位锁，``load`` 省略设备即执行设备。
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from tests.server.test_site_holder_placement import _bottle, _mutation, _site, _warehouse
from unilabos.client.materials import LocalMaterialsClient
from unilabos.protocol.material_execution import MaterialExecutionPrepare, MaterialExecutionReceipt
from unilabos.protocol.materials import (
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialTreeCreate,
    ResourceTemplateWrite,
    SiteCreate,
)
from unilabos.registry.decorators import MaterialRole
from unilabos.registry.material_roles import material_contract
from unilabos.resources import materials
from unilabos.server.scheduler.models import SchedulerResourceRequest
from unilabos.server.scheduler.physical_contract import resolve_physical_contract
from unilabos.server.scheduler.resource_keys import device_lock_key, site_lock_key
from unilabos.server.scheduler.resource_wait_policy import ResourceConditionWait
from unilabos.server.scheduler.tenancy_contract import resolve_tenancy_contract
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.runtime.workflow.resource_intervals import physical_keys

_DEVICE = {"type": "string", "description": "device reference", "x-unilabos-slot": "device"}
_SITE = {"type": "string", "description": "site reference (ResourceSite uuid)", "x-unilabos-slot": "site"}
_MATERIAL = {"type": "object", "properties": {"uuid": {"type": "string"}}, "required": ["uuid"],
             "x-unilabos-material-lock": True}


@pytest.fixture
def authority(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    service.set_resource_sync_dispatcher(lambda command: {"success": True})
    try:
        yield service, LocalMaterialsClient(service)
    finally:
        service.close()


def _device(service, resource_id: str, sites: list[tuple[str, dict]]) -> str:
    service.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(name=resource_id, display_name=resource_id, resource_type="device", class_name=resource_id),
    )
    service.create_tree(
        _mutation("create_material_tree"),
        MaterialTreeCreate(nodes=[MaterialNodeCreate(
            client_ref=resource_id,
            identity=MaterialIdentityWrite(resource_id=resource_id, name=resource_id, resource_type="device",
                                           class_name=resource_id, template_name=resource_id),
            sites=[SiteCreate(template_name=resource_id, site_index=index, label=label, meta_data=meta)
                   for index, (label, meta) in enumerate(sites)],
        )]),
    )
    return service.get_material_by_resource_id(resource_id).material.material_uuid


def _schema(material, fields: dict) -> dict:
    return {
        "properties": {"goal": {"properties": fields}},
        "x-unilabos-action-contract": {"version": 2, "resource_contract": material_contract(material)},
    }


def _settle(service, plan) -> None:
    ids = set(plan["material_ids"])
    for move in plan["moves"]:
        ids.update({move.material_uuid, move.source_owner_uuid, move.target_owner_uuid})
    record = service.executions.prepare(MaterialExecutionPrepare(
        job_uuid=str(uuid4()), task_uuid=str(uuid4()), claim_uuid=str(uuid4()), attempt_no=1,
        parameter_hash="sha256:test",
        fences={json.dumps(["material", item], separators=(",", ":")): 1 for item in ids},
        moves=plan["moves"], reserved_site_uuids=plan["reserved_site_uuids"],
    ))
    service.executions.settle(MaterialExecutionReceipt(
        job_uuid=record["job_uuid"], claim_uuid=record["claim_uuid"], plan_hash=record["plan_hash"],
        material_aliquot_receipts=[], material_transfer_receipts=plan["moves"],
    ))


def _occupant(service, owner_uuid: str, label: str):
    return _site(service, owner_uuid, label).occupied_material_uuid


def test_move_derives_the_owner_from_the_site_and_carries_with_the_executor(authority) -> None:
    service, gateway = authority
    source, target = _warehouse(gateway, "src"), _warehouse(gateway, "dst")
    arm = _device(service, "arm", [("grip", {"role": "gripper"})])
    sample = source["A01"].resource.unilabos_uuid
    schema = _schema(MaterialRole.move("plate", to_site="target_site"), {"plate": _MATERIAL, "target_site": _SITE})
    target_site = _site(service, target.unilabos_uuid, "A02").site_uuid

    plan = resolve_physical_contract(schema, {"plate": {"uuid": sample}, "target_site": target_site}, "arm", gateway)

    (move,) = plan["moves"]
    assert (move.source_owner_uuid, move.source_site_uuid) == (
        source.unilabos_uuid, _site(service, source.unilabos_uuid, "A01").site_uuid)
    assert (move.target_owner_uuid, move.target_site_uuid) == (target.unilabos_uuid, target_site)
    # 搬运途中物料在执行设备的 gripper 库位上：动作期间预留它
    assert plan["reserved_site_uuids"] == [_site(service, arm, "grip").site_uuid]
    assert "arm" in plan["device_ids"]
    _settle(service, plan)
    assert _occupant(service, target.unilabos_uuid, "A02") == sample
    assert _occupant(service, source.unilabos_uuid, "A01") is None


def test_move_checks_a_given_from_site_against_the_authority(authority) -> None:
    service, gateway = authority
    source, target = _warehouse(gateway, "src"), _warehouse(gateway, "dst")
    _device(service, "arm", [("grip", {"role": "gripper"})])
    sample = source["A01"].resource.unilabos_uuid
    schema = _schema(
        MaterialRole.move("plate", to_site="target_site", from_site="source_site"),
        {"plate": _MATERIAL, "target_site": _SITE, "source_site": _SITE},
    )
    values = {"plate": {"uuid": sample}, "target_site": _site(service, target.unilabos_uuid, "A02").site_uuid}

    plan = resolve_physical_contract(
        schema, {**values, "source_site": _site(service, source.unilabos_uuid, "A01").site_uuid}, "arm", gateway)
    assert plan["moves"][0].source_owner_uuid == source.unilabos_uuid
    with pytest.raises(ResourceConditionWait):
        resolve_physical_contract(
            schema, {**values, "source_site": _site(service, source.unilabos_uuid, "A02").site_uuid}, "arm", gateway)


def test_move_onto_a_device_root_site_with_a_device_slot_carrier(authority) -> None:
    """目标库位属于设备根节点；搬运设备是 DeviceSlot 参数（设备 id 解析成设备物料），不是执行设备。"""

    service, gateway = authority
    source = _warehouse(gateway, "src")
    _device(service, "executor", [])
    mover = _device(service, "mover", [("hand", {"role": "gripper"})])
    reader = _device(service, "reader", [("S1", {}), ("S2", {})])
    sample = source["A01"].resource.unilabos_uuid
    schema = _schema(
        MaterialRole.move("plate", to_site="target_site", carrier_device="carrier_device"),
        {"plate": _MATERIAL, "target_site": _SITE, "carrier_device": _DEVICE},
    )
    target_site = _site(service, reader, "S2").site_uuid

    plan = resolve_physical_contract(
        schema, {"plate": {"uuid": sample}, "target_site": target_site, "carrier_device": "mover"}, "executor", gateway)

    (move,) = plan["moves"]
    assert (move.target_owner_uuid, move.target_site_uuid) == (reader, target_site)
    assert plan["reserved_site_uuids"] == [_site(service, mover, "hand").site_uuid]
    assert {"mover", "reader"} <= set(plan["device_ids"])
    _settle(service, plan)
    assert _occupant(service, reader, "S2") == sample


def test_move_without_a_gripper_site_is_a_contract_error(authority) -> None:
    service, gateway = authority
    source, target = _warehouse(gateway, "src"), _warehouse(gateway, "dst")
    _device(service, "arm", [("grip", {"role": "robot.gripper"})])
    schema = _schema(MaterialRole.move("plate", to_site="target_site"), {"plate": _MATERIAL, "target_site": _SITE})
    with pytest.raises(ValueError, match="role=gripper"):
        resolve_physical_contract(schema, {
            "plate": {"uuid": source["A01"].resource.unilabos_uuid},
            "target_site": _site(service, target.unilabos_uuid, "A02").site_uuid,
        }, "arm", gateway)


def test_pick_then_place_hold_the_material_on_the_gripper(authority) -> None:
    """pick：物料到夹爪库位、托管搬运设备；place：物料到目标库位、夹爪空出、托管释放。"""

    service, gateway = authority
    source, target = _warehouse(gateway, "src"), _warehouse(gateway, "dst")
    arm = _device(service, "arm", [("grip", {"role": "gripper"})])
    sample = source["A01"].resource.unilabos_uuid
    grip = _site(service, arm, "grip").site_uuid

    pick = _schema(MaterialRole.pick("plate"), {"plate": _MATERIAL})
    plan = resolve_physical_contract(pick, {"plate": {"uuid": sample}}, "arm", gateway)
    (move,) = plan["moves"]
    assert (move.target_owner_uuid, move.target_site_uuid) == (arm, grip)
    assert plan["device_tenancy"].acquire_device_id == "arm"
    assert plan["device_tenancy"].release_device_id is None
    _settle(service, plan)
    assert _occupant(service, arm, "grip") == sample
    assert _occupant(service, source.unilabos_uuid, "A01") is None

    place = _schema(MaterialRole.place("plate", to_site="target_site"), {"plate": _MATERIAL, "target_site": _SITE})
    target_site = _site(service, target.unilabos_uuid, "A03").site_uuid
    plan = resolve_physical_contract(place, {"plate": {"uuid": sample}, "target_site": target_site}, "arm", gateway)
    (move,) = plan["moves"]
    assert (move.source_owner_uuid, move.source_site_uuid) == (arm, grip)
    assert (move.target_owner_uuid, move.target_site_uuid) == (target.unilabos_uuid, target_site)
    assert plan["device_tenancy"].release_device_id == "arm"
    _settle(service, plan)
    assert _occupant(service, target.unilabos_uuid, "A03") == sample
    assert _occupant(service, arm, "grip") is None


def test_pick_with_a_from_site_that_disagrees_waits(authority) -> None:
    service, gateway = authority
    source = _warehouse(gateway, "src")
    _device(service, "arm", [("grip", {"role": "gripper"})])
    schema = _schema(MaterialRole.pick("plate", from_site="from_site"), {"plate": _MATERIAL, "from_site": _SITE})
    values = {"plate": {"uuid": source["A01"].resource.unilabos_uuid}}
    resolve_physical_contract(schema, {**values, "from_site": _site(service, source.unilabos_uuid, "A01").site_uuid},
                              "arm", gateway)
    with pytest.raises(ResourceConditionWait):
        resolve_physical_contract(
            schema, {**values, "from_site": _site(service, source.unilabos_uuid, "A02").site_uuid}, "arm", gateway)


def test_place_with_a_device_slot_carrier_releases_that_device(authority) -> None:
    service, gateway = authority
    source, target = _warehouse(gateway, "src"), _warehouse(gateway, "dst")
    _device(service, "executor", [])
    arm = _device(service, "arm", [("grip", {"role": "gripper"})])
    sample = source["A01"].resource.unilabos_uuid
    pick = _schema(MaterialRole.pick("plate", carrier_device="arm_device"), {"plate": _MATERIAL, "arm_device": _DEVICE})
    plan = resolve_physical_contract(pick, {"plate": {"uuid": sample}, "arm_device": "arm"}, "executor", gateway)
    assert plan["device_tenancy"].acquire_device_id == "arm"
    _settle(service, plan)
    place = _schema(MaterialRole.place("plate", to_site="to_site", carrier_device="arm_device"),
                    {"plate": _MATERIAL, "to_site": _SITE, "arm_device": _DEVICE})
    plan = resolve_physical_contract(place, {
        "plate": {"uuid": sample}, "arm_device": "arm",
        "to_site": _site(service, target.unilabos_uuid, "A02").site_uuid,
    }, "executor", gateway)
    assert plan["device_tenancy"].release_device_id == "arm"
    assert plan["moves"][0].source_owner_uuid == arm


def test_parameters_are_locked_by_type(authority) -> None:
    """DeviceSlot（含列表）加设备锁、SiteSlot 加库位锁；空值与不是库位 uuid 的值不加锁。"""

    service, gateway = authority
    warehouse = _warehouse(gateway, "wh")
    _device(service, "executor", [])
    _device(service, "probe_a", [])
    _device(service, "probe_b", [])
    site = _site(service, warehouse.unilabos_uuid, "A02").site_uuid
    schema = {"properties": {"goal": {"properties": {
        "probe": _DEVICE, "probes": {"type": "array", "items": _DEVICE}, "spare": _DEVICE,
        "slot": _SITE, "label_slot": _SITE, "name": {"type": "string"},
    }}}, "x-unilabos-action-contract": {"version": 2}}
    values = {"probe": "probe_a", "probes": ["probe_b"], "spare": "", "slot": site, "label_slot": "A01", "name": "x"}

    tenancy = resolve_tenancy_contract(schema, values, "executor", gateway)

    assert tenancy["device_claims"] == ["probe_a", "probe_b"]
    (claim,) = tenancy["site_claims"]
    assert (claim.owner_material_uuid, claim.site_uuid) == (warehouse.unilabos_uuid, site)
    request = SchedulerResourceRequest(request_uuid="r", owner_uuid="job", task_uuid="task", **tenancy)
    assert physical_keys(request) == {
        device_lock_key("probe_a"), device_lock_key("probe_b"), site_lock_key(warehouse.unilabos_uuid, site),
    }
    with pytest.raises(Exception):
        resolve_tenancy_contract(schema, {**values, "probe": "no_such_device"}, "executor", gateway)
    assert resolve_tenancy_contract(schema, {"name": "x"}, "executor", gateway) == {}


def test_load_without_a_device_holds_the_executing_device(authority) -> None:
    service, gateway = authority
    vial = materials.create(_bottle("vial"), gateway=gateway)
    _device(service, "reactor", [])
    _device(service, "shelf", [])
    load = _schema(MaterialRole.load("sample"), {"sample": _MATERIAL})
    tenancy = resolve_tenancy_contract(load, {"sample": {"uuid": vial.unilabos_uuid}}, "reactor", gateway)
    assert tenancy["device_tenancy"].acquire_device_id == "reactor"
    assert tenancy["device_tenancy"].release_device_id is None
    unload = _schema(MaterialRole.unload("sample"), {"sample": _MATERIAL})
    tenancy = resolve_tenancy_contract(unload, {"sample": {"uuid": vial.unilabos_uuid}}, "reactor", gateway)
    assert tenancy["device_tenancy"].release_device_id == "reactor"
    moved = _schema(MaterialRole.load("sample", from_device="from_device"), {"sample": _MATERIAL, "from_device": _DEVICE})
    tenancy = resolve_tenancy_contract(
        moved, {"sample": {"uuid": vial.unilabos_uuid}, "from_device": "shelf"}, "reactor", gateway)
    assert (tenancy["device_tenancy"].acquire_device_id, tenancy["device_tenancy"].release_device_id) == (
        "reactor", "shelf")
    assert set(tenancy["device_claims"]) == {"reactor", "shelf"}


def test_site_picker_owned_by_a_device_is_checked_against_the_device_root(authority) -> None:
    """site_picker 省略 of 时库位属于执行设备根节点，of 是 DeviceSlot 时属于该设备；DeviceSlot 值必须是已登记设备。"""

    from unilabos.registry.material_roles import material_site_selectors
    from unilabos.server.services.runtime.workflow.run_preflight import (
        ResourceUnavailableError,
        validate_resource_values,
    )

    service, gateway = authority
    warehouse = _warehouse(gateway, "wh")
    reader = _device(service, "reader", [("S1", {}), ("S2", {})])
    other = _device(service, "other", [("T1", {})])
    vial = materials.create(_bottle("vial"), gateway=gateway)

    def schema(picker, fields):
        site_field = {**_SITE, "x-unilabos-site-selector": material_site_selectors(picker)["site"]}
        return {"properties": {"goal": {"properties": {**fields, "site": site_field}}},
                "x-unilabos-action-contract": {"version": 2}}

    own = schema(MaterialRole.site_picker("site", for_material="sample"), {"sample": _MATERIAL})
    values = {"sample": {"uuid": vial.unilabos_uuid}}
    validate_resource_values(own, {**values, "site": _site(service, reader, "S1").site_uuid}, gateway, device_id="reader")
    with pytest.raises(ValueError, match="不属于所选容器"):
        validate_resource_values(own, {**values, "site": _site(service, other, "T1").site_uuid}, gateway,
                                 device_id="reader")
    with pytest.raises(ValueError, match="不属于所选容器"):
        validate_resource_values(own, {**values, "site": _site(service, warehouse.unilabos_uuid, "A02").site_uuid},
                                 gateway, device_id="reader")

    of_device = schema(MaterialRole.site_picker("site", of="target_device", for_material="sample"),
                       {"sample": _MATERIAL, "target_device": _DEVICE})
    validate_resource_values(of_device, {**values, "target_device": "other",
                                         "site": _site(service, other, "T1").site_uuid}, gateway, device_id="reader")
    with pytest.raises(ValueError, match="不属于所选容器"):
        validate_resource_values(of_device, {**values, "target_device": "other",
                                             "site": _site(service, reader, "S2").site_uuid}, gateway, device_id="reader")
    with pytest.raises((ValueError, ResourceUnavailableError)):
        validate_resource_values(of_device, {**values, "target_device": "nowhere",
                                             "site": _site(service, other, "T1").site_uuid}, gateway, device_id="reader")


def test_a_declared_move_settles_from_the_frozen_plan_without_a_driver_receipt() -> None:
    """动作成功：没回报搬运回执时按冻结计划结算；回报了就必须一致。人工补记与分装照旧要求回执。"""

    from unilabos.server.scheduler.physical_recovery import PhysicalRecovery

    move = {"material_uuid": "m", "source_owner_uuid": "a", "source_site_uuid": "a1",
            "target_owner_uuid": "b", "target_site_uuid": "b1"}
    record = {"job_uuid": "job", "claim_uuid": "claim", "plan_hash": "sha256:x", "request": {"moves": [move]}}

    for result in (None, {}, {"success": True}):
        receipt = PhysicalRecovery._receipt(record, result, planned_moves=True)
        assert [item.model_dump() for item in receipt.material_transfer_receipts] == [move]
    other = {"material_uuid": "m", "target_owner_uuid": "c", "target_site_uuid": "c1"}
    receipt = PhysicalRecovery._receipt(record, {"material_transfer_receipts": [other]}, planned_moves=True)
    assert receipt.material_transfer_receipts[0].target_site_uuid == "c1"  # 不一致由物料权威结算时拒绝
    assert PhysicalRecovery._receipt(record, {}).material_transfer_receipts == []
    with pytest.raises(ValueError, match="具名回执"):
        PhysicalRecovery._receipt(record, "done")
    assert PhysicalRecovery._receipt(record, "done", planned_moves=True).material_transfer_receipts[0].target_site_uuid == "b1"
    aliquot = {**record, "request": {"moves": [], "aliquot": {"source_material_uuid": "s", "target_material_uuids": ["t"]}}}
    with pytest.raises(ValueError, match="具名回执"):
        PhysicalRecovery._receipt(aliquot, "done", planned_moves=True)


def test_a_site_label_resolves_under_the_site_picker_owner(authority) -> None:
    """库位 uuid 由物料权威发号，工作流只能写死库位名：带 site_picker(of=...) 关系时在所属方下解析、加锁、搬运。"""

    from unilabos.registry.material_roles import material_site_selectors
    from unilabos.server.services.runtime.workflow.run_preflight import validate_resource_values

    service, gateway = authority
    source, target = _warehouse(gateway, "src"), _warehouse(gateway, "dst")
    _device(service, "arm", [("grip", {"role": "gripper"})])
    sample = source["A01"].resource.unilabos_uuid
    declared = [MaterialRole.move("plate", to_site="target_site"),
                MaterialRole.site_picker("target_site", of="target", for_material="plate")]
    site_field = {**_SITE, "x-unilabos-site-selector": material_site_selectors(declared)["target_site"]}
    schema = _schema(declared, {"plate": _MATERIAL, "target": _MATERIAL, "target_site": site_field})
    values = {"plate": {"uuid": sample}, "target": {"uuid": target.unilabos_uuid}, "target_site": "A03"}
    target_site = _site(service, target.unilabos_uuid, "A03").site_uuid

    validate_resource_values(schema, values, gateway, device_id="arm")
    plan = resolve_physical_contract(schema, values, "arm", gateway)
    assert (plan["moves"][0].target_owner_uuid, plan["moves"][0].target_site_uuid) == (target.unilabos_uuid, target_site)
    (claim,) = resolve_tenancy_contract(schema, values, "arm", gateway)["site_claims"]
    assert (claim.owner_material_uuid, claim.site_uuid) == (target.unilabos_uuid, target_site)
    # 没有 site_picker 关系的库位名无从解析：派发前报错，也不加库位锁
    bare = _schema(MaterialRole.move("plate", to_site="target_site"), {"plate": _MATERIAL, "target_site": _SITE})
    with pytest.raises(ValueError, match="site_picker"):
        resolve_physical_contract(bare, {"plate": {"uuid": sample}, "target_site": "A03"}, "arm", gateway)
    assert "site_claims" not in resolve_tenancy_contract(bare, {"plate": {"uuid": sample}, "target_site": "A03"},
                                                        "arm", gateway)
