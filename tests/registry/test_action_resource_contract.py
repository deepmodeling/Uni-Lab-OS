"""动作资源语义：``@action(material=Material.xxx(...))`` 从装饰器 / AST 到注册表 Schema 的行为合同。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from unilabos.registry.action_options import RemovedActionOptionError
from unilabos.registry.action_resource_contract import ActionResourceContractError
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import Material, action
from unilabos.registry.material_roles import MaterialRoleError, material_contract


def _source(material: str, *, signature: str | None = None) -> str:
    signature = signature or (
        "self, material: ResourceSlot, source_device: ResourceSlot, source_site: str, "
        "target_device: ResourceSlot, target_site_uuid: str"
    )
    return f'''
from typing import TypedDict
from unilabos.registry.decorators import Material, action, device
from unilabos.registry.placeholder_type import ResourceSlot

class Result(TypedDict):
    material: ResourceSlot

@device(id="robot", category=["test"])
class Robot:
    @action(material={material})
    def move({signature}) -> Result:
        """执行一次设备间物料转运。"""
        raise NotImplementedError
'''


def _scan_action(tmp_path: Path, material: str, **kwargs) -> dict:
    """真实 AST 扫描（不导入作者模块）一个声明了资源语义的动作。"""

    module_path = tmp_path / "robot.py"
    module_path.write_text(_source(material, **kwargs), encoding="utf-8")
    devices, _resources, _workflows = _parse_file(module_path, tmp_path)
    return devices[0]["actions"]["move"]


def _import(tmp_path: Path, source: str, name: str = "robot_runtime"):
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_material_roles_freeze_into_the_canonical_action_schema(tmp_path: Path) -> None:
    """设备间托管、完整搬运与资源参数合成一个字段，规范化成第 2 版合同冻结进 schema。"""

    action_entry = _scan_action(
        tmp_path,
        """[
            Material.load("material", device="target_device", from_device="source_device"),
            Material.transfer(
                "material", to="target_device", site="target_site_uuid",
                source="source_device", source_site_name="source_site", gripper="robot.gripper",
            ),
            Material.uses(device=["source_device", "target_device"]),
        ]""",
    )

    contract = action_entry["canonical_schema"]["x-unilabos-action-contract"]["resource_contract"]
    assert contract["version"] == 2
    assert contract["resource_params"] == [
        {"param": "source_device", "role": "device"},
        {"param": "target_device", "role": "device"},
    ]
    assert contract["device_tenancy"] == {
        "mode": "task_while_loaded",
        "material_param": "material",
        "acquire_device_param": "target_device",
        "release_device_param": "source_device",
    }
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


def test_decorator_and_ast_scan_produce_the_same_contract(tmp_path: Path) -> None:
    material = '[Material.in_place("material"), Material.order_sensitive()]'
    scanned = _scan_action(tmp_path, material)["canonical_schema"]["x-unilabos-action-contract"]["resource_contract"]
    module = _import(tmp_path, _source(material))
    assert module.Robot.move._action_registry_meta["resource_contract"] == scanned
    assert scanned == {"version": 2, "operate_in_place": {"material_param": "material"}, "order_sensitive": True}


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            Material.load("s", device="v"),
            {"device_tenancy": {"mode": "task_while_loaded", "material_param": "s",
                                "acquire_device_param": "v", "release_device_param": ""}},
        ),
        (
            Material.unload("s", device="v"),
            {"device_tenancy": {"mode": "task_while_loaded", "material_param": "s",
                                "acquire_device_param": "", "release_device_param": "v"}},
        ),
        (Material.in_place("s"), {"operate_in_place": {"material_param": "s"}}),
        (
            Material.pick("s", owner="o", site="t", carriers=["c"]),
            {"transfer_step": {"operation": "pick", "material_param": "s", "owner_param": "o",
                               "site_param": "t", "carrier_params": ["c"]}},
        ),
        (
            Material.place("s", owner="o", site="t", carriers="c"),
            {"transfer_step": {"operation": "place", "material_param": "s", "owner_param": "o",
                               "site_param": "t", "carrier_params": ["c"]}},
        ),
        (
            Material.aliquot(source="a", targets=["b", "c"]),
            {"aliquot": {"source_material_param": "a", "target_material_params": ["b", "c"]}},
        ),
        (Material.order_sensitive(), {"order_sensitive": True}),
        (
            Material.uses(device="d", tool=["t"], motion="m", site="s", material="x"),
            {"resource_params": [
                {"param": "d", "role": "device"}, {"param": "t", "role": "tool"}, {"param": "m", "role": "motion"},
                {"param": "s", "role": "site"}, {"param": "x", "role": "material"},
            ]},
        ),
        (
            Material.transfer("s", to="o", site_name="n", gripper="g", motion=["arm"], tools="hand"),
            {"transfer": {"material_param": "s", "source_owner_param": "", "source_site_uuid_param": "",
                          "source_site_name_param": "", "target_owner_param": "o", "target_site_uuid_param": "",
                          "target_site_name_param": "n", "gripper_site_role": "g",
                          "motion_resource_roles": ["arm"], "tool_resource_roles": ["hand"]}},
        ),
    ],
)
def test_every_role_maps_to_the_existing_contract_field(role, expected) -> None:
    assert material_contract(role) == {"version": 2, **expected}


def test_conflicting_or_invalid_roles_are_rejected() -> None:
    with pytest.raises(MaterialRoleError, match="不能写在同一个动作上"):
        material_contract([Material.load("s", device="a"), Material.unload("s", device="b")])
    with pytest.raises(MaterialRoleError):
        material_contract({"device_tenancy": {}})
    with pytest.raises(MaterialRoleError):
        Material.pick("s", owner="o", site="t", carriers=[])
    with pytest.raises(MaterialRoleError, match="写法无效"):
        material_contract(Material.load("s", device="v", from_device="v"))


def test_unknown_parameter_is_a_registration_error(tmp_path: Path) -> None:
    with pytest.raises(ActionResourceContractError) as caught:
        _scan_action(tmp_path, 'Material.in_place("missing")')
    assert caught.value.code == "unknown_action_resource_parameter"
    with pytest.raises(MaterialRoleError, match="missing"):
        _import(tmp_path, _source('Material.in_place("missing")'), name="robot_unknown")


def test_tenancy_device_parameter_accepts_readable_id_but_material_stays_resource_slot(tmp_path: Path) -> None:
    signature = "self, material: ResourceSlot, target_device: str"
    action_entry = _scan_action(tmp_path, 'Material.load("material", device="target_device")', signature=signature)
    schema = action_entry["canonical_schema"]
    assert schema["properties"]["goal"]["properties"]["target_device"]["type"] == "string"
    with pytest.raises(ActionResourceContractError):
        _scan_action(
            tmp_path, 'Material.load("material", device="target_device")', signature="self, material: str, target_device: str"
        )


@pytest.mark.parametrize(
    ("option", "hint"),
    [
        ('contract="typed"', "去掉 contract="),
        ('resource_contract={"version": 2, "operate_in_place": {"material_param": "x"}}', "Material.in_place"),
        ('goal={"y": "x"}', "字段名就是参数名"),
        ('result={"y": "x"}', "返回 TypedDict"),
    ],
)
def test_removed_options_fail_in_the_decorator_and_in_the_scan(tmp_path: Path, option: str, hint: str) -> None:
    source = f'''
from unilabos.registry.decorators import action, device

@device(id="removed_option", category=["test"])
class Driver:
    @action({option})
    def run(self, x: str) -> None:
        pass
'''
    path = tmp_path / "removed_option.py"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(RemovedActionOptionError, match=hint):
        _parse_file(path, tmp_path)
    with pytest.raises(RemovedActionOptionError, match=hint):
        _import(tmp_path, source, name="removed_option_runtime")


def test_ros_actions_keep_goal_and_result_mappings() -> None:
    @action(action_type="unilabos_msgs.action:SendCmd", goal={"command": "cmd"}, result={"success": "ok"})
    def send(self, cmd: str) -> None:
        pass

    assert send._action_registry_meta["goal"] == {"command": "cmd"}
    with pytest.raises(ValueError, match="material="):
        action(action_type="unilabos_msgs.action:SendCmd", material=Material.in_place("cmd"))(send)
