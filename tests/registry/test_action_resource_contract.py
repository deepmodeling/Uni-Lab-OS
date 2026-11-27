"""动作资源合同从装饰器 AST 到注册表 Schema 的行为合同。"""

from __future__ import annotations

from pathlib import Path

from unilabos.registry.ast_registry_scanner import _parse_file


def _scan_action(tmp_path: Path, resource_contract: str) -> dict:
    """扫描一个带声明式资源合同的强类型设备动作。

    参数：``tmp_path`` 是隔离源码目录；``resource_contract`` 是只插入装饰器的
    Python 字面量。返回：真实 AST 扫描后的动作条目。异常：源码或合同错误原样
    传播，测试不得导入并执行作者模块。
    """

    source = f'''
from typing import TypedDict
from unilabos.registry.decorators import action, device
from unilabos.registry.placeholder_type import ResourceSlot

class Result(TypedDict):
    material: ResourceSlot

@device(id="robot")
class Robot:
    @action(resource_contract={resource_contract})
    def move(
        self,
        material: ResourceSlot,
        source_device: ResourceSlot,
        source_site: str,
        target_device: ResourceSlot,
        target_site_uuid: str,
    ) -> Result:
        """执行一次设备间物料转运。"""
        raise NotImplementedError
'''
    module_path = tmp_path / "robot.py"
    module_path.write_text(source, encoding="utf-8")
    devices, _resources = _parse_file(module_path, tmp_path)
    return devices[0]["actions"]["move"]


def test_ast_freezes_resource_contract_into_canonical_action_schema(
    tmp_path: Path,
) -> None:
    """AST 必须把设备托管和机械臂完整资源参数映射冻结为纯 JSON。

    参数：``tmp_path`` 隔离测试设备源码。返回：无；断言运行时只需读取动作 Schema，
    不扫描函数体里的锁调用。异常：扫描与投影异常原样传播。
    """

    action = _scan_action(
        tmp_path,
        """{
            "version": 1,
            "required_device_params": ["source_device", "target_device"],
            "device_tenancy": {
                "mode": "task_while_loaded",
                "material_param": "material",
                "acquire_device_param": "target_device",
                "release_device_param": "source_device",
            },
            "transfer": {
                "material_param": "material",
                "source_owner_param": "source_device",
                "source_site_uuid_param": "",
                "source_site_name_param": "source_site",
                "target_owner_param": "target_device",
                "target_site_uuid_param": "target_site_uuid",
                "gripper_site_role": "robot.gripper",
            },
        }""",
    )

    extension = action["schema"]["x-unilabos-action-contract"]
    contract = extension["resource_contract"]
    assert contract["version"] == 1
    assert contract["required_device_params"] == [
        "source_device",
        "target_device",
    ]
    assert contract["device_tenancy"]["mode"] == "task_while_loaded"
    assert contract["transfer"] == {
        "material_param": "material",
        "source_owner_param": "source_device",
        "source_site_uuid_param": "",
        "source_site_name_param": "source_site",
        "target_owner_param": "target_device",
        "target_site_uuid_param": "target_site_uuid",
        "target_site_name_param": "",
        "gripper_site_role": "robot.gripper",
    }


def test_invalid_ast_resource_contract_loses_typed_authority(
    tmp_path: Path,
) -> None:
    """无法静态解释的资源合同不得进入强类型工作流目录。

    参数：``tmp_path`` 隔离测试设备源码。返回：无；断言错误合同保留稳定诊断，
    但不再拥有 typed 动作权威。异常：扫描基础设施错误原样传播。
    """

    action = _scan_action(
        tmp_path,
        '{"version": 1, "transfer": {"material_param": "material"}}',
    )

    assert action["contract_kind"] == "typed"
    assert action["contract_diagnostic"]["code"] == "invalid_parameter_name"
    assert "schema" not in action


def test_ast_freezes_aliquot_source_and_complete_target_parameter_set(
    tmp_path: Path,
) -> None:
    """分装合同必须由 AST 冻结来源与全部目标 ResourceSlot 参数。"""

    action = _scan_action(
        tmp_path,
        """{
            "version": 1,
            "aliquot": {
                "source_material_param": "material",
                "target_material_params": ["source_device", "target_device"],
            },
        }""",
    )

    contract = action["schema"]["x-unilabos-action-contract"]["resource_contract"]
    assert contract["aliquot"] == {
        "source_material_param": "material",
        "target_material_params": ["source_device", "target_device"],
    }


def test_ast_freezes_each_in_place_material_parameter(tmp_path: Path) -> None:
    action = _scan_action(
        tmp_path, '{"version": 2, "operate_in_place": {"material_params": ["material", "source_device"]}}'
    )
    assert action["schema"]["x-unilabos-action-contract"]["resource_contract"]["operate_in_place"] == {
        "material_params": ["material", "source_device"],
    }


def test_in_place_material_parameter_sets_reject_empty_duplicates_and_mixed_shapes():
    import pytest

    from unilabos.registry.action_resource_contract import (
        ActionResourceContractError,
        normalize_action_resource_contract,
    )

    for operation in (
        {"material_params": []},
        {"material_params": ["material", "material"]},
        {"material_param": "material", "material_params": ["source_device"]},
    ):
        with pytest.raises(ActionResourceContractError):
            normalize_action_resource_contract({"version": 2, "operate_in_place": operation})

    with pytest.raises(ActionResourceContractError, match="版本 2"):
        normalize_action_resource_contract(
            {"version": 1, "operate_in_place": {"material_params": ["material", "source_device"]}}
        )

    with pytest.raises(ActionResourceContractError, match="单物料设备托管"):
        normalize_action_resource_contract(
            {
                "version": 2,
                "operate_in_place": {"material_params": ["material", "source_device"]},
                "device_tenancy": {
                    "mode": "task_while_loaded",
                    "material_param": "material",
                    "acquire_device_param": "target_device",
                    "release_device_param": "",
                },
            }
        )
