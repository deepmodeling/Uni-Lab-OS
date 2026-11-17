from __future__ import annotations

from dataclasses import dataclass

import pytest

from types import SimpleNamespace

from unilabos.server.backend.scheduler.materials import (
    extract_material_uuids,
    material_uuids_for_parameters,
    resolve_material_name_references,
)
from unilabos.protocol.runtime.control import ExecuteJobContent
from unilabos.protocol.materials import InventoryRequirement


@dataclass
class _ResourceReference:
    unilabos_uuid: str


def test_material_uuid_extraction_accepts_only_authoritative_shapes() -> None:
    assert extract_material_uuids("material-a") == {"material-a"}
    assert extract_material_uuids({"material_uuid": "material-a"}) == {
        "material-a"
    }
    assert extract_material_uuids(
        [{"uuid": "material-a"}, {"data": {"unilabos_uuid": "material-b"}}]
    ) == {"material-a", "material-b"}
    assert extract_material_uuids(_ResourceReference("material-a")) == {
        "material-a"
    }
    assert extract_material_uuids({"id": "display-id", "name": "name"}) == set()


def test_declared_material_parameters_form_one_sorted_claim_set() -> None:
    assert material_uuids_for_parameters(
        ["target", "source", "target"],
        {
            "source": {"uuid": "material-b"},
            "target": [
                {"material_uuid": "material-c"},
                _ResourceReference("material-a"),
            ],
        },
    ) == ("material-a", "material-b", "material-c")


def test_declared_material_parameter_requires_authoritative_uuid() -> None:
    with pytest.raises(ValueError, match="无法解析权威物料 UUID"):
        material_uuids_for_parameters(
            ["material"],
            {"material": {"id": "local-name-only"}},
        )


class _NamedMaterials:
    """物料权威桩：按名字精确搜索，返回带 ``material.material_uuid`` 的聚合。"""

    def __init__(self, **names: list[str]) -> None:
        self.names = names
        self.searched: list[str] = []

    def search_materials(self, name: str) -> list[SimpleNamespace]:
        self.searched.append(name)
        return [SimpleNamespace(material=SimpleNamespace(material_uuid=item)) for item in self.names.get(name, [])]


def test_name_only_material_reference_is_resolved_before_locking() -> None:
    """挂载目标只给名字时，派发前按权威解析出 uuid：物料锁锁到真实物料，不再因缺 uuid 被拒绝。"""

    gateway = _NamedMaterials(bench_deck=["deck-uuid"], tip_box=["tips-uuid"])
    args = {
        "resource": {"uuid": "plate-uuid"},
        "mount_resource": {"name": "bench_deck"},
        "tips": [{"name": "tip_box"}, {"uuid": "other-tips"}],
        "slot_on_deck": "A1",
    }
    with pytest.raises(ValueError, match="无法解析权威物料 UUID"):
        material_uuids_for_parameters(["resource", "mount_resource", "tips"], args)

    resolved = resolve_material_name_references(["resource", "mount_resource", "tips"], args, gateway)

    assert resolved["mount_resource"] == {"name": "bench_deck", "uuid": "deck-uuid"}
    assert resolved["tips"] == [{"name": "tip_box", "uuid": "tips-uuid"}, {"uuid": "other-tips"}]
    assert resolved["resource"] == {"uuid": "plate-uuid"} and resolved["slot_on_deck"] == "A1"
    assert args["mount_resource"] == {"name": "bench_deck"}  # 不改调用方的字典
    assert material_uuids_for_parameters(["resource", "mount_resource", "tips"], resolved) == (
        "deck-uuid", "other-tips", "plate-uuid", "tips-uuid",
    )
    assert gateway.searched == ["bench_deck", "tip_box"]  # 已带 uuid 的引用不查权威


def test_name_reference_must_match_exactly_one_material() -> None:
    gateway = _NamedMaterials(deck=["deck-a", "deck-b"])
    with pytest.raises(ValueError, match="找不到"):
        resolve_material_name_references(["mount"], {"mount": {"name": "missing"}}, gateway)
    with pytest.raises(ValueError, match="匹配到多个物料"):
        resolve_material_name_references(["mount"], {"mount": {"name": "deck"}}, gateway)
    # 非物料锁参数、带 id 的引用都不按名字解析
    assert resolve_material_name_references(
        ["mount"], {"label": {"name": "deck"}, "mount": {"id": "x", "name": "deck"}}, gateway
    ) == {"label": {"name": "deck"}, "mount": {"id": "x", "name": "deck"}}


def test_absent_optional_material_takes_no_lock() -> None:
    """默认加锁会覆盖可选物料入参：未传或为空表示没有物料，不加锁也不拒绝。"""

    assert material_uuids_for_parameters(
        ["plate", "tips", "reference", "lid"],
        {"plate": {"uuid": "material-a"}, "tips": [], "reference": None},
    ) == ("material-a",)


def test_execute_job_protocol_preserves_scheduler_material_contract() -> None:
    content = ExecuteJobContent(
        job_uuid="job-1",
        task_uuid="task-1",
        node_uuid="node-1",
        attempt_group_uuid="attempt-1",
        device_uuid="device-a",
        action_name="use",
        action_args={"material": {"uuid": "material-1"}},
        materials_need_lock=["material"],
        inventory_requirements=[
            InventoryRequirement(
                key="solvent",
                kind="lot",
                template_uuid="solvent-template",
                quantity=10,
                unit="ul",
            )
        ],
        scheduler_revision=1,
    )

    assert content.materials_need_lock == ["material"]
    assert content.inventory_requirements[0].quantity == 10

