"""实际回执按最新材料模型结算，验证原子性、占用和重开幂等。"""

from __future__ import annotations

import json
from uuid import uuid4
import pytest

from tests.api_envelope import error_of, ok_data
from unilabos.protocol.material_execution import (
    MaterialExecutionPrepare,
    MaterialExecutionReceipt,
    PhysicalAliquot,
    AliquotReceipt,
    PhysicalMove,
)
from unilabos.protocol.materials import (
    MaterialDataWrite,
    MaterialSubstance,
    MaterialTreeCreate,
    MaterialNodeCreate,
    MaterialIdentityWrite,
    SiteCreate,
    MaterialMove,
    MaterialPatch,
)
from unilabos.server.services.materials import MaterialsService
from tests.server.test_scheduler_inventory_authority import _template, _mutation, _material


def prepare(service, *, moves=(), source=None, targets=()):
    ids = {
        item for move in moves for item in (move.material_uuid, move.source_owner_uuid, move.target_owner_uuid) if item
    }
    if source:
        ids.update((source, *targets))
    request = MaterialExecutionPrepare(
        job_uuid=str(uuid4()),
        task_uuid=str(uuid4()),
        claim_uuid=str(uuid4()),
        attempt_no=1,
        parameter_hash="sha256:test",
        fences={json.dumps(["material", item], separators=(",", ":")): 1 for item in ids},
        moves=list(moves),
        aliquot=PhysicalAliquot(source_material_uuid=source, target_material_uuids=list(targets)) if source else None,
    )
    return service.executions.prepare(request)


def receipt(record, *, amounts=(), moves=()):
    return MaterialExecutionReceipt(
        job_uuid=record["job_uuid"],
        claim_uuid=record["claim_uuid"],
        plan_hash=record["plan_hash"],
        material_aliquot_receipts=[
            AliquotReceipt(target_material_uuid=target, actual_quantity=quantity, quantity_unit=unit)
            for target, quantity, unit in amounts
        ],
        material_transfer_receipts=list(moves),
    )


def contents(service, identity):
    return {item.name: item.quantity for item in service.get_material(identity).data.substances}


def fill(service, identity, values):
    return service.put_data(
        _mutation("put_data"),
        identity,
        MaterialDataWrite(
            substances=[
                MaterialSubstance(name=name, quantity=quantity, quantity_unit="ul") for name, quantity in values
            ]
        ),
    )


def test_aliquot_actual_mixture_is_atomic_idempotent_and_blocks_other_writers(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    _template(service, "container-template", "container")
    source, left, right = [_material(service, "container") for _ in range(3)]
    fill(service, source, [("water", 60), ("ethanol", 40)])
    record = prepare(service, source=source, targets=[left, right])
    with pytest.raises(RuntimeError, match="未结算"):
        fill(service, source, [("water", 999)])
    assert contents(service, source) == {"water": 60, "ethanol": 40}
    outcome = receipt(record, amounts=[(left, 20, "ul"), (right, 0.03, "ml")])
    service.executions.settle(outcome)
    assert contents(service, source) == {"water": 30, "ethanol": 20}
    assert contents(service, left) == {"water": 12, "ethanol": 8}
    assert contents(service, right) == {"water": 18, "ethanol": 12}
    sequence = service.latest_ledger_sequence()
    service.close()
    reopened = MaterialsService(tmp_path / "materials.db")
    try:
        assert reopened.executions.settle(outcome)["state"] == "settled"
        assert reopened.latest_ledger_sequence() == sequence
        with pytest.raises(RuntimeError, match="不同"):
            reopened.executions.settle(receipt(record, amounts=[(left, 21, "ul"), (right, 30, "ul")]))
        assert contents(reopened, source) == {"water": 30, "ethanol": 20}
    finally:
        reopened.close()


def test_bad_target_capacity_rolls_back_all_aliquot_changes_and_retains_holds(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "container-template", "container")
        source, left, right = [_material(service, "container") for _ in range(3)]
        fill(service, source, [("water", 100)])
        service.patch_material(
            _mutation("patch_material"), right, MaterialPatch(config={"capacity": {"max_volume_ul": 10}})
        )
        record = prepare(service, source=source, targets=[left, right])
        sequence = service.latest_ledger_sequence()
        with pytest.raises((ValueError, RuntimeError), match="超过"):
            service.executions.settle(receipt(record, amounts=[(left, 20, "ul"), (right, 30, "ul")]))
        assert service.latest_ledger_sequence() == sequence
        assert contents(service, source) == {"water": 100}
        assert not contents(service, left) and not contents(service, right)
        assert service.executions.get(record["job_uuid"])["state"] == "prepared"
        with pytest.raises(RuntimeError, match="未结算"):
            fill(service, left, [("water", 1)])
    finally:
        service.close()


def test_transfer_settles_both_sites_and_rejects_incomplete_receipt(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "container-template", "container")

        def owner(label, child=False):
            nodes = [
                MaterialNodeCreate(
                    client_ref="owner",
                    identity=MaterialIdentityWrite(
                        resource_id=label,
                        name=label,
                        template_name="container",
                        resource_type="container",
                        class_name="Container",
                    ),
                    sites=[
                        SiteCreate(
                            template_name="slot",
                            site_index=0,
                            label="A",
                            occupied_client_ref="sample" if child else None,
                        )
                    ],
                )
            ]
            if child:
                nodes.append(
                    MaterialNodeCreate(
                        client_ref="sample",
                        parent_client_ref="owner",
                        identity=MaterialIdentityWrite(
                            resource_id="sample",
                            name="sample",
                            template_name="container",
                            resource_type="container",
                            class_name="Container",
                        ),
                    )
                )
            return service.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=nodes)).data

        src, dst = owner("source", True), owner("target")
        sample = src.client_ref_map["sample"]
        source_owner, target_owner = src.root_material_uuid, dst.root_material_uuid
        a, b = service.get_material(source_owner).sites[0], service.get_material(target_owner).sites[0]
        move = PhysicalMove(
            material_uuid=sample,
            source_owner_uuid=source_owner,
            source_site_uuid=a.site_uuid,
            target_owner_uuid=target_owner,
            target_site_uuid=b.site_uuid,
        )
        record = prepare(service, moves=[move])
        with pytest.raises(RuntimeError, match="完整匹配"):
            service.executions.settle(receipt(record))
        with pytest.raises(RuntimeError, match="未结算"):
            service.move_material(
                _mutation("move_material"), MaterialMove(material_uuid=sample, destination_site_uuid=b.site_uuid)
            )
        service.executions.settle(receipt(record, moves=[move]))
        assert service.get_material(sample).material.parent_material_uuid == target_owner
        assert service.get_site(a.site_uuid).occupied_material_uuid is None
        assert service.get_site(b.site_uuid).occupied_material_uuid == sample
    finally:
        service.close()


@pytest.mark.parametrize("actual", [0, 3, 7])
def test_actual_lot_usage_refunds_or_debits_difference_once(tmp_path, actual):
    from tests.server.test_workflow_inventory_recovery import Stack
    from unilabos.protocol.material_execution import InventoryConsumption

    stack = Stack(tmp_path)
    try:
        task, job, payload = stack.create()
        record = stack.materials.executions.prepare(
            MaterialExecutionPrepare(
                job_uuid=job,
                task_uuid=task["uuid"],
                claim_uuid=str(uuid4()),
                attempt_no=1,
                parameter_hash="test",
                fences={},
                inventory_reservation_uuid=payload["inventory_reservation_uuid"],
            )
        )
        value = receipt(record).model_copy(
            update={
                "inventory_consumptions": [
                    InventoryConsumption(key="solvent", lot_uuid="lot-a", actual_quantity=actual, quantity_unit="ul")
                ]
            }
        )
        stack.materials.executions.settle(value)
        assert stack.quantities() == (20 - actual, 20 - actual, 0)
        sequence = stack.materials.latest_ledger_sequence()
        stack.materials.executions.settle(value)
        assert stack.materials.latest_ledger_sequence() == sequence
        assert stack.quantities() == (20 - actual, 20 - actual, 0)
    finally:
        stack.close()


def test_actual_lot_overdraw_rolls_back_then_replenishment_allows_same_receipt(tmp_path):
    from tests.server.test_workflow_inventory_recovery import Stack
    from tests.server.test_scheduler_inventory_authority import _inbound
    from unilabos.protocol.material_execution import InventoryConsumption

    stack = Stack(tmp_path)
    try:
        task, job, payload = stack.create()
        record = stack.materials.executions.prepare(
            MaterialExecutionPrepare(
                job_uuid=job,
                task_uuid=task["uuid"],
                claim_uuid=str(uuid4()),
                attempt_no=1,
                parameter_hash="test",
                fences={},
                inventory_reservation_uuid=payload["inventory_reservation_uuid"],
            )
        )
        value = receipt(record).model_copy(
            update={
                "inventory_consumptions": [
                    InventoryConsumption(key="solvent", lot_uuid="lot-a", actual_quantity=30, quantity_unit="ul")
                ]
            }
        )
        before = stack.quantities()
        with pytest.raises(RuntimeError, match="invariant"):
            stack.materials.executions.settle(value)
        assert stack.quantities() == before
        _inbound(stack.materials, lot_uuid="lot-a", quantity=10)
        stack.materials.executions.settle(value)
        assert stack.quantities() == (0, 0, 0)
    finally:
        stack.close()


def test_v4_upgrade_preserves_materials_contents_and_ledger(tmp_path, monkeypatch):
    from dataclasses import replace
    from unilabos.server.database.tables.materials import MATERIALS_DATABASE
    from unilabos.server.services.materials import store

    from tests.server.materials_published_specs import materials_spec_v5

    v5 = materials_spec_v5()
    old = replace(
        v5,
        schema_version=4,
        tables=tuple(
            table
            for table in v5.tables
            if table.name not in {"material_execution", "material_execution_hold"}
        ),
        migrations=tuple(item for item in MATERIALS_DATABASE.migrations if item.version <= 4),
        on_unmigratable="refuse",
    )
    path = tmp_path / "materials.db"
    with monkeypatch.context() as patch:
        patch.setattr(store, "MATERIALS_DATABASE", old)
        previous = MaterialsService(path)
        # 用 v4 写入语义构造旧库；该版本没有物理占用守卫。
        patch.setattr(previous.executions, "guard_mutation", lambda *_: None)
        _template(previous, "container-template", "container")
        source = _material(previous, "container")
        fill(previous, source, [("water", 50)])
        sequence = previous.latest_ledger_sequence()
        previous.close()
    current = MaterialsService(path)
    try:
        assert contents(current, source) == {"water": 50}
        assert current.latest_ledger_sequence() == sequence
        assert current.connection.execute("PRAGMA user_version").fetchone()[0] == MATERIALS_DATABASE.schema_version
        assert current.executions.list() == []
        assert len(list(tmp_path.glob(f"materials.db.pre-v{MATERIALS_DATABASE.schema_version}-*.bak"))) == 1
    finally:
        current.close()


def test_material_execution_http_validation_is_read_only_and_stale_release_conflicts(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unilabos.server.api.materials import install_materials_api

    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "container-template", "container")
        source, target = [_material(service, "container") for _ in range(2)]
        fill(service, source, [("water", 20)])
        other = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[
                    MaterialNodeCreate(
                        client_ref="child",
                        identity=MaterialIdentityWrite(
                            resource_id="reactor/gripper#1",
                            name="hierarchical",
                            template_name="container",
                            resource_type="container",
                            class_name="Container",
                        ),
                    )
                ]
            ),
        ).data.root_material_uuid
        record = prepare(service, source=source, targets=[target])
        app = FastAPI()
        install_materials_api(app, service)
        with TestClient(app) as client:
            assert (
                ok_data(client.get("/api/v1/materials/instances/by-resource-id/reactor%2Fgripper%231"))["material"][
                    "material_uuid"
                ]
                == other
            )
            prefix = "/api/v1/materials/executions"
            body = receipt(record, amounts=[(target, 4, "ul")]).model_dump(mode="json")
            ok_data(client.post(prefix + "/validate", json=body))
            assert contents(service, source) == {"water": 20}
            assert service.executions.get(record["job_uuid"])["state"] == "prepared"
            # 版本过期的释放：物料冲突（资源口径 4002）
            error_of(
                client.post(
                    prefix + "/" + record["job_uuid"] + "/release",
                    json={"expected_version": 9, "reason": "现场确认", "outcome": "physically_verified"},
                ),
                4002,
            )
            ok_data(client.post(prefix + "/settle", json=body))
            assert ok_data(client.get(prefix + "/" + record["job_uuid"]))["state"] == "settled"
            error_of(client.get(prefix + "/absent"), 4001)
            assert contents(service, source) == {"water": 16}
    finally:
        service.close()
