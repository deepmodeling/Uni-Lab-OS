"""规范动作合同（ActionContract）进入注册表（Registry）的接线测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import action, legacy_action
from unilabos.registry.registry import Registry


def _scan_device(tmp_path: Path, source: str) -> dict:
    """静态扫描一个测试设备并构建注册表条目。

    Args:
        tmp_path: 隔离的 Python 包根目录。
        source: 含设备和动作声明的 Python 源码。

    Returns:
        经 AST 扫描器和注册表构建器生成的设备条目。
    """

    # ``driver.py`` 是本轮静态扫描输入，扫描过程不得导入或执行它。
    module_path = tmp_path / "driver.py"
    module_path.write_text(source, encoding="utf-8")
    devices, _resources = _parse_file(module_path, tmp_path)
    return Registry()._build_device_entry_from_ast("typed_device", devices[0])


def test_typed_action_publishes_canonical_schema_without_legacy_lock_fields(
    tmp_path: Path,
) -> None:
    """规范动作只发布编译 Schema，不再发布字符串物料锁声明。"""

    entry = _scan_device(
        tmp_path,
        '''
from typing import Annotated, TypedDict
from unilabos.registry.annotations import MaterialLock
from unilabos.registry.decorators import action, device
from unilabos.registry.placeholder_type import ResourceSlot

class Result(TypedDict):
    material: ResourceSlot

@device(id="typed_device")
class Driver:
    @action(description="处理物料")
    def process(
        self,
        plate: ResourceSlot,
        free_material: Annotated[ResourceSlot, MaterialLock(free=True)],
    ) -> Result:
        """处理需要独占的物料，并只读免锁物料。"""
        raise NotImplementedError
''',
    )

    # ``action_mapping`` 是设备动作名到规范动作元数据的注册表投影。
    action_mapping = entry["class"]["action_value_mappings"]["process"]
    goal_properties = action_mapping["schema"]["properties"]["goal"]["properties"]

    assert action_mapping["contract_kind"] == "typed"
    assert action_mapping["schema"]["x-unilabos-action-contract"]["version"] == 2
    assert goal_properties["plate"]["x-unilabos-material-lock"] is True
    assert goal_properties["free_material"]["x-unilabos-material-lock"] is False
    assert "lock_resource" not in action_mapping
    assert "materials_lock" not in action_mapping


def test_invalid_typed_action_keeps_diagnostic_but_not_typed_authority(
    tmp_path: Path,
) -> None:
    """不合规范的 ``@action`` 可被诊断，但不能伪装成规范动作权威。"""

    entry = _scan_device(
        tmp_path,
        '''
from unilabos.registry.decorators import action, device

@device(id="typed_device")
class Driver:
    @action()
    def invalid(self, value):
        """缺少参数和结果注解的遗留实现。"""
        return value
''',
    )

    action_mapping = entry["class"]["action_value_mappings"]["invalid"]

    assert action_mapping["contract_kind"] == "invalid_typed"
    assert action_mapping["contract_diagnostic"]["code"] == "invalid_action_contract"
    assert "x-unilabos-action-contract" not in action_mapping["schema"]


def test_legacy_action_is_explicit_and_cannot_accept_removed_lock_protocol() -> None:
    """遗留动作必须显式声明，且两个字符串物料锁参数均已从接口删除。"""

    @legacy_action()
    def reset() -> bool:
        """提供不进入规范工作流目录的遗留复位动作。"""

        return True

    assert reset._action_contract_kind == "legacy"

    with pytest.raises(TypeError):
        action(lock_resource=["plate"])
    with pytest.raises(TypeError):
        action(materials_lock="plate")


def test_ast_registry_rejects_removed_string_lock_protocol(tmp_path: Path) -> None:
    """静态注册表也必须拒绝旧字符串锁字段，不能把它们静默丢弃。

    Args:
        tmp_path: 保存静态扫描测试源码的隔离目录。
    """

    with pytest.raises(ValueError, match="字符串物料锁声明已移除"):
        _scan_device(
            tmp_path,
            '''
from unilabos.registry.decorators import device, legacy_action

@device(id="typed_device")
class Driver:
    @legacy_action(lock_resource=["plate"])
    def invalid_legacy(self, plate):
        """模拟仍携带旧字符串锁声明的外部设备动作。"""
        return plate
''',
        )
