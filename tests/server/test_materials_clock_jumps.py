"""墙钟回拨（NTP 步进校时、Windows/WSL 对时、休眠唤醒）下物料时间戳不倒退的回归。

记录模型要求 ``updated_at_ms >= created_at_ms``（表上还有同样的 CHECK）。墙钟回拨后，
更新几秒前刚建的记录不能因此失败，``updated_at_ms`` 也不能倒退。只改写墙钟（``time.time``）。
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest

import unilabos.server.services.materials.core as materials_core
from unilabos.protocol.materials import (
    InventoryLotInbound,
    InventoryMutation,
    InventoryRequirement,
    InventoryReservationCreate,
    InventoryReservationTransition,
    MaterialDataDelta,
    MaterialDataWrite,
    MaterialDelete,
    MaterialDelta,
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialNodeDelta,
    MaterialPatch,
    MaterialPosition,
    MaterialSnapshot,
    MaterialSubstance,
    MaterialTreeCreate,
    ResourceTemplateWrite,
    SiteDelta,
)
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.ingress import IngressReservations

#: 实测：墙钟每 31 s 先回拨约 12 s、再前跳约 13 s
_BACKWARD_STEP_S = -12.0

#: 带 updated_at_ms 的物料表及其主键
_TABLES = {
    "resource_template": "template_uuid",
    "material": "material_uuid",
    "material_position": "material_uuid",
    "material_data": "material_uuid",
    "material_substance": "substance_uuid",
    "site": "site_uuid",
    "inventory_lot": "lot_uuid",
    "inventory_reservation": "reservation_uuid",
}


def _mutation(operation: str, **values: Any) -> InventoryMutation:
    return InventoryMutation(
        command_uuid=str(uuid4()), effect_key=operation, operation=operation, **values
    )


def _jump_wall_clock(monkeypatch, offset_s: float) -> None:
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + offset_s)


def _restart_process_clock(monkeypatch) -> None:
    """模拟进程重启：新进程的物料时钟不记得上一个进程给出过的时间戳。"""

    monkeypatch.setattr(materials_core, "_clock_last_ms", 0, raising=False)


def _updated_at(service: MaterialsService) -> dict[tuple[str, str], int]:
    result: dict[tuple[str, str], int] = {}
    for table, key in _TABLES.items():
        for row in service.connection.execute(f"SELECT {key}, updated_at_ms FROM {table}"):
            result[(table, row[0])] = int(row[1])
    return result


def _assert_not_earlier(before: dict[tuple[str, str], int], service: MaterialsService) -> None:
    after = _updated_at(service)
    went_back = {
        identity: (previous, after[identity])
        for identity, previous in before.items()
        if identity in after and after[identity] < previous
    }
    assert went_back == {}


@pytest.fixture
def service(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    yield service
    service.close()


def _template(service: MaterialsService, template_uuid: str, name: str, **values: Any) -> None:
    service.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(
            template_uuid=template_uuid,
            name=name,
            resource_type="container",
            class_name="Container",
            **values,
        ),
    )


def _node(ref: str, template_name: str, *, parent: str | None = None) -> MaterialNodeCreate:
    return MaterialNodeCreate(
        client_ref=ref,
        parent_client_ref=parent,
        identity=MaterialIdentityWrite(
            resource_id=f"resource-{ref}",
            name=ref,
            resource_type="container",
            class_name="Container",
            template_name=template_name,
        ),
        data=MaterialDataWrite(
            substances=[MaterialSubstance(name="NaCl", quantity=2, quantity_unit="ug")]
        ),
    )


def _decks_and_tube(service: MaterialsService) -> tuple[str, str, str, str]:
    """两个各带一个位点的台面，管子放在第一个台面的位点上。"""

    site = [{"index": 0, "label": "A1", "content_type": ["container"]}]
    _template(service, "deck-template", "deck", available_sites=site)
    _template(service, "tube-template", "tube")
    first = service.create_tree(
        _mutation("create_material_tree"),
        MaterialTreeCreate(nodes=[_node("deck-1", "deck"), _node("tube", "tube", parent="deck-1")]),
    )
    second = service.create_tree(
        _mutation("create_material_tree"), MaterialTreeCreate(nodes=[_node("deck-2", "deck")])
    )
    tube = first.data.client_ref_map["tube"]
    source_site = first.data.nodes[0].sites[0].site_uuid
    service.move_material(
        _mutation("move_material"),
        MaterialMove(material_uuid=tube, destination_site_uuid=source_site),
    )
    return first.data.root_material_uuid, tube, source_site, second.data.nodes[0].sites[0].site_uuid


def test_update_after_backward_wall_clock_step_is_accepted(service, monkeypatch) -> None:
    _template(service, "tube-template", "tube")
    created = service.create_tree(
        _mutation("create_material_tree"), MaterialTreeCreate(nodes=[_node("tube", "tube")])
    )
    material_uuid = created.data.root_material_uuid
    before = service.get_material(material_uuid).material

    _jump_wall_clock(monkeypatch, _BACKWARD_STEP_S)
    patched = service.patch_material(
        _mutation("patch_material"), material_uuid, MaterialPatch(description="回拨之后")
    ).data.material

    assert patched.description == "回拨之后"
    assert patched.version == before.version + 1
    assert patched.created_at_ms == before.created_at_ms
    assert patched.updated_at_ms >= patched.created_at_ms
    assert patched.updated_at_ms >= before.updated_at_ms


def test_process_clock_never_goes_back(monkeypatch) -> None:
    first = MaterialsService._now_ms()
    _jump_wall_clock(monkeypatch, _BACKWARD_STEP_S)
    assert MaterialsService._now_ms() >= first

    # 设备端报来的观测时间只抬高这一次写入，不把进程时钟带到将来
    future = first + 3_600_000
    assert MaterialsService._now_ms(_mutation("put_data", observed_at_ms=future)) == future
    assert MaterialsService._now_ms() < future


def test_material_writes_after_restart_with_backward_wall_clock(service, monkeypatch) -> None:
    deck, tube, _source_site, destination_site = _decks_and_tube(service)
    before = _updated_at(service)

    # 重启后的进程不记得之前的时间戳，墙钟又落在已有记录之前：只能靠逐行下限兜住
    _restart_process_clock(monkeypatch)
    _jump_wall_clock(monkeypatch, _BACKWARD_STEP_S)

    service.patch_material(_mutation("patch_material"), tube, MaterialPatch(description="回拨之后"))
    service.put_position(
        _mutation("put_position"), tube, MaterialPosition(position_x=1, position_y=2, position_z=3)
    )
    service.put_data(
        _mutation("put_data"),
        tube,
        MaterialDataWrite(substances=[MaterialSubstance(name="NaCl", quantity=3, quantity_unit="ug")]),
    )
    service.move_material(
        _mutation("move_material"),
        MaterialMove(material_uuid=tube, destination_site_uuid=destination_site),
    )
    deck_node = service.get_material(deck)
    service.apply_delta(
        _mutation("apply_material_delta"),
        MaterialDelta(
            root_material_uuid=deck,
            nodes=[
                MaterialNodeDelta(
                    material_uuid=deck,
                    position=MaterialPosition(position_x=5, position_y=5, position_z=0),
                    data=MaterialDataDelta(
                        substances=[MaterialSubstance(name="NaCl", quantity=1, quantity_unit="ug")]
                    ),
                    sites=[SiteDelta(site_uuid=deck_node.sites[0].site_uuid, visible=False)],
                )
            ],
        ),
    )
    current = service.get_material(tube)
    service.apply_snapshot(
        _mutation("apply_material_snapshot"),
        MaterialSnapshot(
            root_material_uuid=current.material.parent_material_uuid,
            nodes=[
                service.get_material(current.material.parent_material_uuid),
                current.model_copy(
                    update={"position": MaterialPosition(position_x=7, position_y=8, position_z=9)}
                ),
            ],
        ),
    )
    _template(service, "tube-template", "tube", display_name="Tube v2")
    service.delete_material(_mutation("delete_material"), MaterialDelete(material_uuid=tube))
    service.delete_template(_mutation("delete_template"), "tube-template")

    _assert_not_earlier(before, service)
    retired = service.connection.execute(
        "SELECT created_at_ms, updated_at_ms, deleted_at_ms FROM material WHERE material_uuid=?", (tube,)
    ).fetchone()
    assert retired[0] <= retired[1] == retired[2]


def test_inventory_writes_after_restart_with_backward_wall_clock(service, monkeypatch) -> None:
    _template(service, "naoh-template", "naoh")
    _template(service, "tube-template", "tube")
    service.inbound_inventory_lot(
        _mutation("inbound_inventory_lot"),
        InventoryLotInbound(lot_uuid="lot-naoh", template_uuid="naoh-template", unit="ml", quantity=100),
    )
    tube = service.create_tree(
        _mutation("create_material_tree"), MaterialTreeCreate(nodes=[_node("tube", "tube")])
    ).data.root_material_uuid
    reservation = service.reserve_inventory(
        _mutation("reserve_inventory", job_uuid="job-1"),
        InventoryReservationCreate(
            task_uuid="task-1",
            node_uuid="node-1",
            job_uuid="job-1",
            scheduler_revision=1,
            requirements=[
                InventoryRequirement(key="naoh", kind="lot", lot_uuid="lot-naoh", quantity=10, unit="ml"),
                InventoryRequirement(key="tube", kind="material", material_uuid=tube),
            ],
        ),
    ).data
    before = _updated_at(service)

    _restart_process_clock(monkeypatch)
    _jump_wall_clock(monkeypatch, _BACKWARD_STEP_S)

    service.inbound_inventory_lot(
        _mutation("inbound_inventory_lot"),
        InventoryLotInbound(lot_uuid="lot-naoh", template_uuid="naoh-template", unit="ml", quantity=5),
    )
    released = service.release_inventory_reservation(
        _mutation("release_inventory_reservation"),
        InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid),
    ).data

    assert released.status == "released"
    assert service.get_inventory_lot("lot-naoh").quantity_available == 105
    _assert_not_earlier(before, service)


def test_ingress_reservation_update_after_restart_with_backward_wall_clock(service, monkeypatch) -> None:
    _deck, tube, _source_site, destination_site = _decks_and_tube(service)
    ingress = IngressReservations(service)
    reservation, _ = ingress.reserve(
        {
            "idempotency_key": "agv-1",
            "carrier_material_uuid": tube,
            "candidate_site_uuids": [destination_site],
            "ttl_seconds": 600,
        }
    )
    # 让运输中这次更新严格晚于创建时间：之后的写入不能退回 created_at_ms
    time.sleep(0.05)
    ingress.mark_in_transit(reservation["uuid"])

    def stamps() -> tuple[int, int]:
        row = service.connection.execute(
            "SELECT created_at_ms, updated_at_ms FROM station_ingress_reservation WHERE reservation_uuid=?",
            (reservation["uuid"],),
        ).fetchone()
        return int(row[0]), int(row[1])

    created_at, in_transit_at = stamps()
    assert in_transit_at > created_at

    _restart_process_clock(monkeypatch)
    _jump_wall_clock(monkeypatch, _BACKWARD_STEP_S)
    assert ingress.cancel(reservation["uuid"], reason="现场取消")["state"] == "canceled"

    assert stamps()[1] >= in_transit_at
