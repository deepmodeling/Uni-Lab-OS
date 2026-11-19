"""YAML 设备同样物料天然带锁：旧的 materials_need_lock / materials_free 键忽略并告警，推导值不写回。"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest
import yaml

from unilabos.registry import material_locks
from unilabos.registry.registry import Registry


@pytest.fixture(autouse=True)
def fresh_override_warnings(monkeypatch):
    monkeypatch.setattr(material_locks, "_warned_definitions", set())


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


def _override_warnings(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "unilabos.registry.material_locks"]


def test_runtime_load_ignores_removed_overrides_in_yaml(tmp_path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="unilabos.registry.material_locks")
    data, _complete, ok, _ids = Registry()._load_single_device_file(_write(tmp_path), complete_registry=False)
    assert ok
    mappings = _mappings(data)
    assert mappings["fill"]["materials_need_lock"] == ["plate"]
    # 显式 [] 与 materials_free 都不再豁免：placeholder 标为物料的字段一律加锁
    assert mappings["look"]["materials_need_lock"] == ["plate"]
    assert mappings["compare"]["materials_need_lock"] == ["sample", "references"]
    assert "materials_free" not in mappings["compare"]
    messages = _override_warnings(caplog)
    assert len(messages) == 2
    assert any("yaml_lock_test.look" in message and "materials_need_lock:" in message for message in messages)
    assert any("yaml_lock_test.compare" in message and "materials_free:" in message for message in messages)


def test_completion_drops_removed_overrides_and_never_writes_derived_locks(tmp_path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    path = _write(tmp_path)
    data, _complete, ok, _ids = Registry()._load_single_device_file(path, complete_registry=True)
    assert ok
    mappings = _mappings(data)
    assert mappings["fill"]["materials_need_lock"] == ["plate"]
    assert mappings["look"]["materials_need_lock"] == ["plate"]
    assert mappings["compare"]["materials_need_lock"] == ["sample", "references"]
    # 未声明的 auto- 动作按签名推导；没有物料入参的就是空
    assert mappings["auto-shake"]["materials_need_lock"] == []

    written = _mappings(yaml.safe_load(path.read_text(encoding="utf-8")))
    for name in ("fill", "look", "compare", "auto-shake"):
        assert "materials_need_lock" not in written[name]
        assert "materials_free" not in written[name]

    # 再补全一次结果不变（CI check_mode 依赖这一点）
    before = path.read_text(encoding="utf-8")
    Registry()._load_single_device_file(path, complete_registry=True)
    assert path.read_text(encoding="utf-8") == before
