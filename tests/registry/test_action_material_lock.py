"""``@action(materials_need_lock=...)`` 注册表合同测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unilabos.device_runtime.definition import DeviceDefinition
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import action, get_action_meta
from unilabos.registry.registry import Registry


def test_action_decorator_keeps_and_validates_material_parameter_names() -> None:
    declared = ["plate", "reagent"]

    @action(materials_need_lock=declared)
    def process(plate: object, reagent: object, temperature: float = 25.0) -> None:
        del plate, reagent, temperature

    declared.append("mutated_after_decoration")

    assert get_action_meta(process)["materials_need_lock"] == [
        "plate",
        "reagent",
    ]

    with pytest.raises(ValueError, match="非动作入参.*missing"):

        @action(materials_need_lock=["missing"])
        def invalid(plate: object) -> None:
            del plate

    with pytest.raises(TypeError, match="必须是参数名列表"):

        @action(materials_need_lock="plate")  # type: ignore[arg-type]
        def invalid_shape(plate: object) -> None:
            del plate


def _scan_entry(tmp_path: Path, source: str) -> tuple[dict, dict]:
    module_path = tmp_path / "material_lock_driver.py"
    module_path.write_text(source, encoding="utf-8")
    devices, _resources = _parse_file(module_path, tmp_path)
    ast_meta = devices[0]
    entry = Registry()._build_device_entry_from_ast(
        "material_lock_test",
        ast_meta,
    )
    return ast_meta, entry


def test_ast_registry_roundtrip_publishes_material_lock_contract(
    tmp_path: Path,
) -> None:
    ast_meta, entry = _scan_entry(
        tmp_path,
        '''
from unilabos.registry.decorators import action, device
from unilabos.registry.placeholder_type import ResourceSlot

@device(id="material_lock_test", category=["test"])
class Driver:
    @action(materials_need_lock=["plate", "reagent"])
    def process(
        self,
        plate: ResourceSlot,
        reagent: ResourceSlot,
        temperature: float = 25.0,
    ) -> None:
        pass

    @action()
    def inspect(self, plate: ResourceSlot) -> None:
        pass
''',
    )

    assert ast_meta["actions"]["process"]["action_args"][
        "materials_need_lock"
    ] == ["plate", "reagent"]
    mappings = entry["class"]["action_value_mappings"]
    assert mappings["process"]["materials_need_lock"] == ["plate", "reagent"]
    assert mappings["inspect"]["materials_need_lock"] == []

    restored = json.loads(json.dumps(entry, default=str))
    assert restored["class"]["action_value_mappings"]["process"][
        "materials_need_lock"
    ] == ["plate", "reagent"]


def test_ast_rejects_unknown_material_parameter(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="非动作入参.*missing"):
        _scan_entry(
            tmp_path,
            '''
from unilabos.registry.decorators import action, device

@device(id="material_lock_test", category=["test"])
class Driver:
    @action(materials_need_lock=["missing"])
    def process(self, plate: object) -> None:
        pass
''',
        )


def test_host_material_actions_declare_their_authoritative_locks() -> None:
    """框架自带的物料动作不能只支持锁协议，却忘记实际声明。"""

    source = (
        Path(__file__).parents[2]
        / "unilabos"
        / "ros"
        / "nodes"
        / "presets"
        / "host_node.py"
    )
    devices, _resources = _parse_file(source, source.parents[4])
    host = next(
        device for device in devices if device["device_id"] == "host_node"
    )
    actions = host["actions"]

    assert actions["set_substance"]["action_args"][
        "materials_need_lock"
    ] == ["resource"]
    assert actions["discard_resource"]["action_args"][
        "materials_need_lock"
    ] == ["resource"]
    assert actions["transfer_resource"]["action_args"][
        "materials_need_lock"
    ] == ["resource", "mount_resource"]
    assert actions["transfer_manual"]["action_args"][
        "materials_need_lock"
    ] == ["resource", "mount_resource"]


def test_runtime_device_definition_carries_defaulted_material_lock_metadata() -> None:
    class Driver:
        pass

    definition = DeviceDefinition(
        device_id="device",
        resource_uuid="resource",
        registry_name="driver",
        display_name="Driver",
        driver_class=Driver,
        driver_config={
            "action_value_mappings": {
                "process": {
                    "type": "UniLabJsonCommand",
                    "materials_need_lock": ["plate"],
                },
                "inspect": {"type": "UniLabJsonCommand"},
            }
        },
        runtime_config={},
        registry_entry={},
        categories=("test",),
    )

    mappings = definition.action_value_mappings
    assert mappings["process"]["materials_need_lock"] == ["plate"]
    assert mappings["inspect"]["materials_need_lock"] == []
