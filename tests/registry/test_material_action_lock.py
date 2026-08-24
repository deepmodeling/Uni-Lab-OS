from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from unilabos.registry.ast_registry_scanner import scan_directory
from unilabos.registry.decorators import action, get_action_meta


def _registry():
    try:
        from unilabos.registry.registry import Registry
    except ImportError as exc:
        pytest.skip(f"当前环境未安装完整 unilabos_msgs: {exc}")
    return Registry()


def test_action_keeps_material_lock_parameter_names() -> None:
    @action(materials_need_lock=["target", "source", "target"])
    def transfer(self, source, target):
        return None

    assert get_action_meta(transfer)["materials_need_lock"] == [
        "target",
        "source",
    ]


def test_action_rejects_invalid_material_lock_declaration() -> None:
    with pytest.raises(TypeError, match="参数名列表"):

        @action(materials_need_lock="source")
        def transfer(self, source):
            return None


def test_ast_registry_publishes_material_lock_metadata(tmp_path) -> None:
    source = tmp_path / "material_lock_driver.py"
    source.write_text(
        '''
from unilabos.registry.decorators import action, device

@device(id="material_lock_test", category=["test"])
class Driver:
    @action(materials_need_lock=["source", "target"])
    def transfer(self, source: dict, target: dict) -> None:
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

    action_args = scanned["devices"]["material_lock_test"]["actions"][
        "transfer"
    ]["action_args"]
    assert action_args["materials_need_lock"] == ["source", "target"]

    entry = _registry()._build_device_entry_from_ast(
        "material_lock_test",
        scanned["devices"]["material_lock_test"],
    )
    assert entry["class"]["action_value_mappings"]["transfer"][
        "materials_need_lock"
    ] == ["source", "target"]
