from __future__ import annotations

from dataclasses import dataclass

import pytest

from unilabos.server.backend.scheduler.materials import (
    extract_material_uuids,
    material_uuids_for_parameters,
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

