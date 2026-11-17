"""工作流的容器内容物数量需求（I02 第一步）：多任务共享余量、随作业预留结算、只能更正一次、不吃别人的预留。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from unilabos.protocol.materials import (
    InventoryMutation,
    InventoryRequirement,
    InventoryReservationCreate,
    InventoryReservationTransition,
    MaterialDataWrite,
    MaterialSubstance,
)
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.contents import ContentContract
from unilabos.server.services.materials.core import InsufficientInventoryError, MaterialConflictError
from unilabos.server.services.materials.resource_contract import ResourceContract


def _mutation(operation: str, **extra: Any) -> InventoryMutation:
    return InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation, **extra)


@pytest.fixture
def lab(tmp_path):
    materials = MaterialsService(tmp_path / "materials.db")
    contents = ContentContract(materials, compound_source=None)
    contract = ResourceContract(materials)
    template = contract.sync_resource_templates([{"id": "bottle", "registry_type": "resource"}])["templates"][0]
    bottle = contract.create_material({"resource_template_uuid": template["uuid"], "name": "缓冲液瓶"})
    info = contents.create_reagent_info({"name": "PBS", "physical_state": "liquid"})
    reagent = contents.create_reagent({"material_uuid": bottle["uuid"], "reagent_info_uuid": info["uuid"],
                                       "quantity": 100, "quantity_unit": "ml"})
    yield materials, contents, bottle["uuid"], reagent["uuid"]
    materials.close()


def _reserve(materials, container: str, quantity: float, unit: str = "ml", **selector: Any):
    job = str(uuid4())
    requirement = InventoryRequirement(key="buffer", kind="content", material_uuid=container,
                                       quantity=quantity, unit=unit, **(selector or {"substance_name": "PBS"}))
    return materials.reserve_inventory(
        _mutation("reserve_inventory", job_uuid=job),
        InventoryReservationCreate(task_uuid=str(uuid4()), node_uuid=str(uuid4()), job_uuid=job,
                                   scheduler_revision=1, requirements=[requirement]),
    ).data


def _transition(materials, reservation, action: str):
    return getattr(materials, f"{action}_inventory_reservation")(
        _mutation(f"{action}_inventory_reservation", job_uuid=reservation.job_uuid),
        InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid, reason="test"),
    ).data


def _quantity(contents, reagent: str) -> float:
    return contents.get_reagent(reagent)["quantity"]


def test_tasks_share_the_unreserved_remainder_of_one_container(lab) -> None:
    materials, contents, container, reagent = lab
    first = _reserve(materials, container, 60)
    [item] = first.items
    assert (item.kind, item.substance_uuid, item.quantity, item.unit) == ("content", reagent, 60.0, "ml")
    assert item.content_reservation_uuid
    with pytest.raises(InsufficientInventoryError, match="缺少 10 ml"):
        _reserve(materials, container, 50)
    second = _reserve(materials, container, 0.04, "l", substance_uuid=reagent)  # 换单位、按 uuid 选
    assert second.items[0].content_reservation_uuid
    assert contents.get_reagent(reagent)["active_workflow_reserved_quantity"] == pytest.approx(100.0)
    assert _quantity(contents, reagent) == 100  # 预留不扣现量


def test_content_settles_with_the_job_reservation_like_lots(lab) -> None:
    materials, contents, container, reagent = lab
    started = _transition(materials, _reserve(materials, container, 30), "consume")  # 动作开始
    assert started.status == "consumed" and _quantity(contents, reagent) == 70
    _transition(materials, started, "quarantine")  # 开始后失败：不退回
    assert _quantity(contents, reagent) == 70

    released = _transition(materials, _reserve(materials, container, 20), "release")  # 未开始即失败
    assert released.status == "released" and _quantity(contents, reagent) == 70
    row = contents.get_content_reservation(released.items[0].content_reservation_uuid)
    assert row["status"] == "released"

    _transition(materials, _reserve(materials, container, 10), "quarantine")  # 结果不明：按预留量扣减
    assert _quantity(contents, reagent) == 60
    assert contents.get_reagent(reagent)["active_workflow_reserved_quantity"] == 0


def test_preview_reports_a_content_shortage_without_writing(lab) -> None:
    materials, contents, container, _reagent = lab
    report = materials.preview_inventory([
        InventoryRequirement(key="a", kind="content", material_uuid=container, substance_name="PBS", quantity=70, unit="ml"),
        InventoryRequirement(key="b", kind="content", material_uuid=container, substance_name="PBS", quantity=40, unit="ml"),
    ])
    assert report["available"] is False
    assert [(item["key"], item["shortage"]) for item in report["shortages"]] == [("b", pytest.approx(10.0))]
    assert contents.list_content_reservations() == []


def test_unknown_or_ambiguous_substances_are_reported_as_shortages(lab) -> None:
    materials, contents, container, reagent = lab
    with pytest.raises(InsufficientInventoryError, match="没有内容物 水"):
        _reserve(materials, container, 1, substance_name="水")
    aggregate = materials.get_material(container)
    twin = MaterialSubstance(substance_uuid=str(uuid4()), name="PBS", quantity=5, quantity_unit="ml",
                             physical_state="liquid")
    materials.put_data(_mutation("put_data"), container, MaterialDataWrite(
        data=dict(aggregate.data.data), substances=[*aggregate.data.substances, twin],
        sites_initialized=aggregate.data.sites_initialized, observed_at_ms=aggregate.data.observed_at_ms + 1))
    with pytest.raises(InsufficientInventoryError, match="请改用 substance_uuid"):
        _reserve(materials, container, 1)
    assert _reserve(materials, container, 1, substance_uuid=reagent).items[0].substance_uuid == reagent


def test_snapshots_cannot_drop_or_reunit_content_a_job_has_reserved(lab) -> None:
    materials, contents, container, reagent = lab
    _reserve(materials, container, 30)
    aggregate = materials.get_material(container)
    [substance] = aggregate.data.substances

    def write(substances):
        current = materials.get_material(container).data
        return materials.put_data(_mutation("put_data"), container, MaterialDataWrite(
            data=dict(current.data), substances=substances, sites_initialized=current.sites_initialized,
            observed_at_ms=current.observed_at_ms + 1))

    with pytest.raises(MaterialConflictError, match="不能从容器移除"):
        write([])
    with pytest.raises(MaterialConflictError, match="单位不能改成 g"):
        write([substance.model_copy(update={"quantity_unit": "g"})])
    write([substance.model_copy(update={"quantity": 80})])  # 设备上报的数量变化照常落地
    assert _quantity(contents, reagent) == 80


def test_usage_after_action_start_can_be_corrected_once(lab) -> None:
    materials, contents, container, reagent = lab
    started = _transition(materials, _reserve(materials, container, 60), "consume")
    content_uuid = started.items[0].content_reservation_uuid
    contents.adjust_consumed_content(content_uuid, 50, reason="称量复核")  # 少用 10 ml：退回
    assert _quantity(contents, reagent) == 50
    contents.adjust_consumed_content(content_uuid, 50)  # 同值重放不再记账
    assert _quantity(contents, reagent) == 50
    with pytest.raises(MaterialConflictError, match="already reconciled"):
        contents.adjust_consumed_content(content_uuid, 45)
    records = materials_consumptions(materials, started.job_uuid)
    assert [(item["planned_quantity"], item["actual_quantity"], item["balance_after"]) for item in records] == [(60.0, 50.0, 50.0)]


def test_extra_usage_cannot_take_other_tasks_reservations(lab) -> None:
    materials, contents, container, reagent = lab
    started = _transition(materials, _reserve(materials, container, 60), "consume")
    _reserve(materials, container, 30)  # 另一任务仍在等待
    content_uuid = started.items[0].content_reservation_uuid
    with pytest.raises(MaterialConflictError, match="other tasks' active reservations"):
        contents.adjust_consumed_content(content_uuid, 75)  # 多用 15，未预留余量只有 10
    assert _quantity(contents, reagent) == 40
    contents.adjust_consumed_content(content_uuid, 65)
    assert _quantity(contents, reagent) == 35


def test_content_requirement_shape_and_stable_hashes() -> None:
    with pytest.raises(ValueError, match="container material_uuid"):
        InventoryRequirement(key="a", kind="content", substance_name="PBS", quantity=1, unit="ml")
    with pytest.raises(ValueError, match="exactly one of substance_uuid/substance_name"):
        InventoryRequirement(key="a", kind="content", material_uuid="m", quantity=1, unit="ml")
    with pytest.raises(ValueError, match="only content requirements"):
        InventoryRequirement(key="a", kind="lot", lot_uuid="l", quantity=1, unit="ml", substance_name="PBS")
    # 内容物字段为空时不进入序列化：既有 lot / material 需求与预留请求哈希保持不变
    lot = InventoryRequirement(key="a", kind="lot", lot_uuid="l", quantity=1, unit="ml").model_dump(mode="json", exclude_none=False)
    assert "substance_uuid" not in lot and "substance_name" not in lot


def materials_consumptions(materials, job_uuid: str) -> list[dict[str, Any]]:
    from unilabos.server.services.materials.consumption import InventoryConsumptions

    return InventoryConsumptions(materials).for_job(job_uuid)
