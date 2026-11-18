from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from unilabos.registry import material_locks
from unilabos.registry.ast_registry_scanner import scan_directory


@pytest.fixture(autouse=True)
def fresh_override_warnings(monkeypatch):
    monkeypatch.setattr(material_locks, "_warned_definitions", set())


def _registry():
    try:
        from unilabos.registry.registry import Registry
    except ImportError as exc:
        pytest.skip(f"当前环境未安装完整 unilabos_msgs: {exc}")
    return Registry()


def test_removed_lock_list_no_longer_locks_plain_parameters(tmp_path) -> None:
    """旧的 materials_need_lock 能把非物料类型的入参（如 dict）列进锁集合；移除后物料入参要写成
    ResourceSlot 注解，锁才会加上（placeholder_keys 也已删除）。"""

    source = tmp_path / "material_lock_driver.py"
    source.write_text(
        '''
from unilabos.registry.decorators import action, device
from unilabos.registry.placeholder_type import ResourceSlot

@device(id="material_lock_test", category=["test"])
class Driver:
    @action(materials_need_lock=["source", "target"])
    def transfer(self, source: dict, target: dict) -> None:
        pass

    @action()
    def process(self, plate: ResourceSlot, tag: str = "") -> None:
        pass
''',
        encoding="utf-8",
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        scanned = scan_directory(
            tmp_path,
            python_path=tmp_path,
            executor=executor,
        )

    actions = scanned["devices"]["material_lock_test"]["actions"]
    assert "materials_need_lock" not in actions["transfer"]["action_args"]

    mappings = _registry()._build_device_entry_from_ast(
        "material_lock_test",
        scanned["devices"]["material_lock_test"],
    )["class"]["action_value_mappings"]
    assert mappings["transfer"]["materials_need_lock"] == []
    assert mappings["process"]["materials_need_lock"] == ["plate"]
