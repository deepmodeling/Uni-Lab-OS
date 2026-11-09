"""物料天然带锁：未声明 materials_need_lock 时，物料入参默认在本次使用期间互斥。"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import action, get_action_meta
from unilabos.registry.material_locks import (
    effective_material_lock_parameters,
    is_material_slot_type,
)
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.registry.registry import Registry


@pytest.mark.parametrize(
    "type_hint",
    [
        "ResourceSlot",
        "List[ResourceSlot]",
        "Optional[ResourceSlot]",
        "Optional[List[ResourceSlot]]",
        "ResourceSlot | None",
        "Tuple[ResourceSlot, ...]",
        "unilabos.registry.placeholder_type:ResourceSlot",
        ("list", "unilabos.registry.placeholder_type:ResourceSlot"),
        ResourceSlot,
        List[ResourceSlot],
        Optional[ResourceSlot],
    ],
)
def test_material_slot_shapes(type_hint) -> None:
    assert is_material_slot_type(type_hint)


@pytest.mark.parametrize(
    "type_hint",
    ["", None, "str", "dict", "DeviceSlot", "SiteSlot", "Dict[str, ResourceSlot]", "Union[ResourceSlot, str]"],
)
def test_non_material_shapes(type_hint) -> None:
    assert not is_material_slot_type(type_hint)


def test_decorator_locks_material_parameters_by_default() -> None:
    @action()
    def run(self, plate: ResourceSlot, tips: List[ResourceSlot] = None, volume: float = 1.0) -> None:
        del plate, tips, volume

    @action(placeholder_keys={"vessel": "unilabos_resources"})
    def marked(self, vessel: dict, speed: int = 1) -> None:
        del vessel, speed

    assert get_action_meta(run)["materials_need_lock"] == ["plate", "tips"]
    assert get_action_meta(marked)["materials_need_lock"] == ["vessel"]


def test_materials_free_and_explicit_list_opt_out() -> None:
    @action(materials_free=["tips"])
    def free_tips(self, plate: ResourceSlot, tips: List[ResourceSlot] = None) -> None:
        del plate, tips

    @action(materials_need_lock=[])
    def unlocked(self, plate: ResourceSlot) -> None:
        del plate

    @action(materials_need_lock=["plate"])
    def only_plate(self, plate: ResourceSlot, tips: List[ResourceSlot] = None) -> None:
        del plate, tips

    assert get_action_meta(free_tips)["materials_need_lock"] == ["plate"]
    assert get_action_meta(unlocked)["materials_need_lock"] == []
    assert get_action_meta(only_plate)["materials_need_lock"] == ["plate"]


def test_conflicting_or_misspelled_declarations_are_rejected() -> None:
    with pytest.raises(ValueError, match="不能同时声明"):

        @action(materials_need_lock=["plate"], materials_free=["plate"])
        def both(self, plate: ResourceSlot) -> None:
            del plate

    with pytest.raises(ValueError, match="materials_free 包含非动作入参.*missing"):

        @action(materials_free=["missing"])
        def misspelled(self, plate: ResourceSlot) -> None:
            del plate

    with pytest.raises(TypeError, match="materials_free 必须是参数名列表"):

        @action(materials_free="plate")  # type: ignore[arg-type]
        def wrong_shape(self, plate: ResourceSlot) -> None:
            del plate


def test_unknown_signature_does_not_invent_locks() -> None:
    assert effective_material_lock_parameters(None, params=None) == []
    assert effective_material_lock_parameters(["plate"], params=None) == ["plate"]


def test_ast_and_registry_publish_default_locks(tmp_path: Path) -> None:
    module_path = tmp_path / "default_lock_driver.py"
    module_path.write_text(
        '''
from typing import List, Optional
from unilabos.registry.decorators import action, device
from unilabos.registry.placeholder_type import ResourceSlot

@device(id="default_lock_test", category=["test"])
class Driver:
    @action()
    def transfer(self, source: ResourceSlot, targets: List[ResourceSlot], volume: float = 1.0) -> None:
        pass

    @action(materials_free=["reference"])
    def compare(self, sample: ResourceSlot, reference: Optional[ResourceSlot] = None) -> None:
        pass

    @action(materials_need_lock=[])
    def look(self, sample: ResourceSlot) -> None:
        pass

    def shake(self, plate: ResourceSlot, rpm: int = 300) -> None:
        pass
''',
        encoding="utf-8",
    )
    devices, _resources, _workflows = _parse_file(module_path, tmp_path)
    ast_actions = devices[0]["actions"]
    assert ast_actions["compare"]["action_args"]["materials_need_lock"] == ["sample"]
    assert "materials_free" not in ast_actions["compare"]["action_args"]

    entry = Registry()._build_device_entry_from_ast("default_lock_test", devices[0])
    mappings = entry["class"]["action_value_mappings"]
    assert mappings["transfer"]["materials_need_lock"] == ["source", "targets"]
    assert mappings["compare"]["materials_need_lock"] == ["sample"]
    assert mappings["look"]["materials_need_lock"] == []
    # 未加 @action 的 auto- 动作同样默认给物料入参加锁
    assert mappings["auto-shake"]["materials_need_lock"] == ["plate"]
