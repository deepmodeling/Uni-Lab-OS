"""前端选择器 ``placeholder_keys``：按类型推出的 ∪ 手写的，同一参数以手写的为准。"""

from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import textwrap
from pathlib import Path

import pytest

from unilabos.registry import placeholder_keys as placeholder_keys_module
from unilabos.registry.action_schema import ActionSchemaError
from unilabos.registry.annotation_schema import NO_DEFAULT, AnnotationSchemaError, parse_parameter_annotation
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.placeholder_keys import PlaceholderKeysError, check_placeholder_keys
from unilabos.registry.registry import Registry

SOURCE = '''
from unilabos.registry.decorators import action, device
from unilabos.registry.placeholder_type import (
    PLACEHOLDER_MANUAL_CONFIRM,
    PLACEHOLDER_NODES,
    DeviceSlot,
    ResourceSlot,
    SiteSlot,
)


@device(id="selector_driver", category=["test"])
class Driver:
    @action(
        placeholder_keys={
            "mount": PLACEHOLDER_NODES,
            "assignees": PLACEHOLDER_MANUAL_CONFIRM,
            "note": "unilabos_future_selector",
        }
    )
    def run(
        self,
        sample: ResourceSlot,
        mount: ResourceSlot,
        device: DeviceSlot,
        site: SiteSlot,
        assignees: list[str],
        note: str = "",
    ) -> None:
        pass

    @action(placeholder_keys={"plate": "unilabos_resources"})
    def legacy(self, plate: dict) -> None:
        pass

    @action(action_type="unilabos_msgs.action:SendCmd", goal={"command": "cmd"}, placeholder_keys={"command": PLACEHOLDER_NODES})
    def send(self, cmd: str) -> None:
        pass

    @action(parent=True, placeholder_keys={"inherited": PLACEHOLDER_NODES})
    def inherited(self, *args, **kwargs) -> None:
        pass
'''


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(placeholder_keys_module, "_warned", set())


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def _import(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(path.stem, None)
    return module


@pytest.fixture()
def actions(tmp_path: Path):
    path = _write(tmp_path, "selector_driver", SOURCE)
    devices, _, _ = _parse_file(path, tmp_path)
    return Registry()._build_device_entry_from_ast("selector_driver", devices[0])["class"]["action_value_mappings"]


def test_type_derived_and_handwritten_selectors_are_merged_handwritten_first(actions) -> None:
    run = actions["run"]
    assert run["placeholder_keys"] == {
        "sample": "unilabos_resources",  # 类型推出
        "mount": "unilabos_nodes",  # 类型推出 unilabos_resources，手写优先
        "device": "unilabos_devices",  # 类型推出
        "site": "unilabos_sites",  # 类型推出
        "assignees": "unilabos_manual_confirm",  # 只有手写
        "note": "unilabos_future_selector",  # 未知种类：告警后照常写入
    }
    # 参数是什么仍由类型决定：mount 仍是物料参数（物料引用 schema、加锁），assignees / note 仍是普通值
    goal = run["schema"]["properties"]["goal"]["properties"]
    assert goal["mount"]["x-unilabos-material-lock"] is True
    assert run["materials_need_lock"] == ["sample", "mount"]
    assert goal["assignees"] == {"type": "array", "items": {"type": "string"}}


def test_handwritten_resource_selector_still_locks_the_parameter(actions) -> None:
    """兼容此前的规则：手写成 unilabos_resources 的参数也加共享锁，但类型与校验不变。"""

    legacy = actions["legacy"]
    assert legacy["placeholder_keys"] == {"plate": "unilabos_resources"}
    assert legacy["materials_need_lock"] == ["plate"]
    assert legacy["schema"]["properties"]["goal"]["properties"]["plate"] == {"type": "object"}


def test_ros_actions_may_name_goal_fields_and_parent_signatures_are_not_checked(actions) -> None:
    assert actions["send"]["placeholder_keys"] == {"command": "unilabos_nodes"}
    assert actions["inherited"]["placeholder_keys"] == {"inherited": "unilabos_nodes"}


def test_unknown_kind_warns_once_per_definition(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="unilabos.registry.placeholder_keys")
    path = _write(tmp_path, "selector_driver", SOURCE)
    _parse_file(path, tmp_path)
    _parse_file(path, tmp_path)
    _import(path)
    warnings = [record.getMessage() for record in caplog.records if "unilabos_future_selector" in record.getMessage()]
    assert len(warnings) == 1 and "selector_driver.Driver.run" in warnings[0] and "note" in warnings[0]


def test_handwritten_key_must_be_a_parameter_in_the_scan_and_the_decorator(tmp_path: Path) -> None:
    source = '''
    from unilabos.registry.decorators import action, device
    from unilabos.registry.placeholder_type import PLACEHOLDER_NODES

    @device(id="bad_selector", category=["test"])
    class Driver:
        @action(placeholder_keys={"target": PLACEHOLDER_NODES, "sample_uuids": PLACEHOLDER_NODES})
        def run(self, target: str, sample_uuids: dict = None) -> None:
            pass
    '''
    path = _write(tmp_path, "bad_selector", source)
    with pytest.raises(PlaceholderKeysError, match="sample_uuids"):
        _parse_file(path, tmp_path)
    with pytest.raises(PlaceholderKeysError, match="sample_uuids"):
        _import(path)
    with pytest.raises(PlaceholderKeysError, match="字典"):
        check_placeholder_keys(["target"], parameter_names=["target"], definition="d")
    with pytest.raises(PlaceholderKeysError):
        check_placeholder_keys({"target": 1}, parameter_names=["target"], definition="d")


def test_removed_placeholder_annotation_points_to_placeholder_keys(tmp_path: Path) -> None:
    with pytest.raises(ImportError, match=r"placeholder_keys"):
        from unilabos.registry.annotations import Placeholder  # noqa: F401
    source = '''
    from typing import Annotated
    from unilabos.registry.annotations import Placeholder
    from unilabos.registry.decorators import action, device
    from unilabos.registry.placeholder_type import PLACEHOLDER_NODES, ResourceSlot

    @device(id="old_selector", category=["test"])
    class Driver:
        @action()
        def run(self, target: Annotated[ResourceSlot, Placeholder(PLACEHOLDER_NODES)]) -> None:
            pass
    '''
    path = _write(tmp_path, "old_selector", source)
    with pytest.raises(ActionSchemaError, match=r"参数 target：Placeholder 已删除.*placeholder_keys"):
        _parse_file(path, tmp_path)
    with pytest.raises(ImportError, match=r"placeholder_keys"):
        _import(path)
    # 工作流 / 合同共用的注解解析器同样给出新写法
    annotation = ast.parse("Annotated[str, Placeholder(PLACEHOLDER_NODES)]", mode="eval").body
    with pytest.raises(AnnotationSchemaError, match="placeholder_keys"):
        parse_parameter_annotation(
            "target",
            annotation,
            default=NO_DEFAULT,
            imports={"Annotated": "typing:Annotated", "Placeholder": "unilabos.registry.annotations:Placeholder"},
        )


def test_yaml_selectors_count_as_handwritten_when_completing_the_registry() -> None:
    """补全手写 YAML 的 JSON 动作：YAML 里已有的选择器算手写，与类型推出的取并集、手写优先；不是参数的键丢弃。"""

    method_info = {
        "params": [
            {"name": "plate", "type": "ResourceSlot", "required": True},
            {"name": "target", "type": "DeviceSlot", "required": True},
        ]
    }
    entry = Registry()._json_action_entry(
        method_info,
        {},
        action_name="run",
        previous={"placeholder_keys": {"plate": "unilabos_nodes", "gone": "unilabos_devices"}},
    )
    assert entry["placeholder_keys"] == {"plate": "unilabos_nodes", "target": "unilabos_devices"}
    assert entry["materials_need_lock"] == ["plate"]
