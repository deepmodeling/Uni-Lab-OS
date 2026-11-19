"""注册表发布的物料锁合同：``materials_need_lock`` 是按签名推导的只读结果。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from unilabos.client.host.runtime.definition import DeviceDefinition
from unilabos.registry import material_locks
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.registry import Registry


@pytest.fixture(autouse=True)
def fresh_override_warnings(monkeypatch):
    monkeypatch.setattr(material_locks, "_warned_definitions", set())


def _scan_entry(tmp_path: Path, source: str) -> tuple[dict, dict]:
    module_path = tmp_path / "material_lock_driver.py"
    module_path.write_text(source, encoding="utf-8")
    devices, _resources, _workflows = _parse_file(module_path, tmp_path)
    ast_meta = devices[0]
    entry = Registry()._build_device_entry_from_ast(
        "material_lock_test",
        ast_meta,
    )
    return ast_meta, entry


def test_ast_registry_roundtrip_ignores_removed_lock_overrides(
    tmp_path: Path,
) -> None:
    ast_meta, entry = _scan_entry(
        tmp_path,
        '''
from unilabos.registry.decorators import action, device
from unilabos.registry.placeholder_type import ResourceSlot

@device(id="material_lock_test", category=["test"])
class Driver:
    @action(materials_need_lock=["plate"])
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

    @action(materials_need_lock=["missing"])
    def observe(self, plate: ResourceSlot) -> None:
        pass
''',
    )

    assert "materials_need_lock" not in ast_meta["actions"]["process"]["action_args"]
    mappings = entry["class"]["action_value_mappings"]
    # 显式列表不再收窄锁集合，拼错的名字也不再让扫描失败：每个物料入参都加锁
    assert mappings["process"]["materials_need_lock"] == ["plate", "reagent"]
    assert mappings["inspect"]["materials_need_lock"] == ["plate"]
    assert mappings["observe"]["materials_need_lock"] == ["plate"]

    restored = json.loads(json.dumps(entry, default=str))
    assert restored["class"]["action_value_mappings"]["process"][
        "materials_need_lock"
    ] == ["plate", "reagent"]


def test_host_material_actions_lock_every_material_parameter(caplog) -> None:
    """框架自带的物料动作同样不豁免：每个物料入参都加锁，内置定义也不再写已废弃的改写。"""

    caplog.set_level(logging.WARNING, logger="unilabos.registry.material_locks")
    repo_root = Path(__file__).parents[2]
    # host_node 动作的唯一 @device/@action 扫描源（backend 无关的 HostServices）
    source = repo_root / "unilabos" / "client" / "host" / "host_services.py"
    devices, _resources, _workflows = _parse_file(source, repo_root)
    host = next(
        device for device in devices if device["device_id"] == "host_node"
    )
    mappings = Registry()._build_device_entry_from_ast("host_node", host)["class"][
        "action_value_mappings"
    ]

    assert mappings["set_substance"]["materials_need_lock"] == ["resource"]
    assert mappings["discard_resource"]["materials_need_lock"] == ["resource"]
    assert mappings["transfer_resource"]["materials_need_lock"] == ["resource", "mount_resource"]
    # 以前豁免的挂载目标与诊断回显的物料入参现在同样加锁
    assert mappings["apply_deduct_resource"]["materials_need_lock"] == ["resource", "mount_resource"]
    assert mappings["test_resource"]["materials_need_lock"] == ["resource", "resources"]
    assert mappings["manual_confirm"]["materials_need_lock"] == []
    assert not [record for record in caplog.records if record.name == "unilabos.registry.material_locks"]


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
