"""YAML 设备同样物料天然带锁：不写 materials_need_lock = 默认，补全写回只保留显式声明。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import yaml

from unilabos.registry.material_locks import effective_material_lock_parameters
from unilabos.registry.registry import Registry

_DRIVER = '''
from typing import List

from unilabos.registry.placeholder_type import ResourceSlot


class YamlLockDriver:
    def __init__(self, **kwargs):
        pass

    def fill(self, plate: ResourceSlot, volume: float = 1.0) -> None:
        pass

    def look(self, plate: ResourceSlot) -> None:
        pass

    def compare(self, sample: ResourceSlot, references: List[ResourceSlot] = None) -> None:
        pass

    def shake(self, rpm: int = 300) -> None:
        pass
'''

_YAML = '''
yaml_lock_test:
  category:
  - test
  class:
    module: yaml_lock_driver:YamlLockDriver
    type: python
    status_types: {}
    action_value_mappings:
      look:
        type: UniLabJsonCommand
        goal:
          plate: plate
        feedback: {}
        result: {}
        goal_default: {}
        handles: {}
        placeholder_keys:
          plate: unilabos_resources
        materials_need_lock: []
      compare:
        type: UniLabJsonCommand
        goal:
          sample: sample
          references: references
        feedback: {}
        result: {}
        goal_default: {}
        handles: {}
        placeholder_keys:
          sample: unilabos_resources
          references: unilabos_resources
        materials_free:
        - references
      fill:
        type: UniLabJsonCommand
        goal:
          plate: plate
          volume: volume
        feedback: {}
        result: {}
        goal_default: {}
        handles: {}
        placeholder_keys:
          plate: unilabos_resources
  init_param_schema: {}
'''


def _write(tmp_path: Path) -> Path:
    (tmp_path / "yaml_lock_driver.py").write_text(_DRIVER, encoding="utf-8")
    path = tmp_path / "yaml_lock_test.yaml"
    path.write_text(textwrap.dedent(_YAML).lstrip(), encoding="utf-8")
    return path


def _mappings(data: dict) -> dict:
    return data["yaml_lock_test"]["class"]["action_value_mappings"]


def test_unknown_signature_derives_from_material_placeholders() -> None:
    keys = {"plate": "unilabos_resources", "device": "unilabos_devices", "tips": "unilabos_resources"}
    assert effective_material_lock_parameters(None, params=None, placeholder_keys=keys) == ["plate", "tips"]
    assert effective_material_lock_parameters(None, params=None, placeholder_keys=keys, free=["tips"]) == ["plate"]
    assert effective_material_lock_parameters([], params=None, placeholder_keys=keys) == []


def test_runtime_load_derives_default_locks_for_yaml_devices(tmp_path) -> None:
    data, _complete, ok, _ids = Registry()._load_single_device_file(_write(tmp_path), complete_registry=False)
    assert ok
    mappings = _mappings(data)
    assert mappings["fill"]["materials_need_lock"] == ["plate"]
    assert mappings["look"]["materials_need_lock"] == []  # 显式 [] 仍是不锁
    assert mappings["compare"]["materials_need_lock"] == ["sample"]  # materials_free 排除


def test_completion_writes_back_only_explicit_declarations(tmp_path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    path = _write(tmp_path)
    data, _complete, ok, _ids = Registry()._load_single_device_file(path, complete_registry=True)
    assert ok
    mappings = _mappings(data)
    assert mappings["fill"]["materials_need_lock"] == ["plate"]
    assert mappings["look"]["materials_need_lock"] == []
    assert mappings["compare"]["materials_need_lock"] == ["sample"]
    # 未声明的 auto- 动作按签名推导；没有物料入参的就是空
    assert mappings["auto-shake"]["materials_need_lock"] == []

    written = _mappings(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert "materials_need_lock" not in written["fill"]
    assert "materials_need_lock" not in written["compare"]
    assert "materials_need_lock" not in written["auto-shake"]
    assert written["look"]["materials_need_lock"] == []
    assert written["compare"]["materials_free"] == ["references"]

    # 再补全一次结果不变（CI check_mode 依赖这一点）
    before = path.read_text(encoding="utf-8")
    Registry()._load_single_device_file(path, complete_registry=True)
    assert path.read_text(encoding="utf-8") == before
