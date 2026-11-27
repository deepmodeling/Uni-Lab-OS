"""动作资源语义：``@action(material_contract=Material.xxx(...))`` 从装饰器 / AST 到注册表 Schema 的行为合同。"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

from unilabos.registry.action_options import RemovedActionOptionError
from unilabos.registry.action_resource_contract import EXECUTING_DEVICE
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import Material, action
from unilabos.registry.material_roles import MaterialRoleError, material_contract, material_site_selectors

_SIGNATURE = (
    "self, material: ResourceSlot, target_site: SiteSlot, source_site: SiteSlot, "
    "arm_device: DeviceSlot, vessel_device: DeviceSlot, other: ResourceSlot, label: str"
)


def _source(material: str, *, signature: str | None = None) -> str:
    signature = signature or _SIGNATURE
    return f'''
from typing import TypedDict
from unilabos.registry.decorators import Material, action, device
from unilabos.registry.placeholder_type import DeviceSlot, ResourceSlot, SiteSlot

class Result(TypedDict):
    material: ResourceSlot

@device(id="robot", category=["test"])
class Robot:
    @action(material_contract={material})
    def move({signature}) -> Result:
        """执行一次物料动作。"""
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


def _contract(entry: dict) -> dict:
    return entry["canonical_schema"]["x-unilabos-action-contract"]["resource_contract"]


def test_move_freezes_into_the_transfer_contract_with_site_derived_owner(tmp_path: Path) -> None:
    """Material.move：所属方由库位决定（不写所属物料参数），搬运设备省略即执行设备。"""

    contract = _contract(_scan_action(tmp_path, 'Material.move("material", to_site="target_site")'))
    assert contract == {
        "version": 2,
        "transfer": {
            "material_param": "material",
            "source_owner_param": "",
            "source_site_uuid_param": "",
            "source_site_name_param": "",
            "target_owner_param": "",
            "target_site_uuid_param": "target_site",
            "target_site_name_param": "",
            "gripper_site_role": "gripper",
        },
    }
    explicit = _contract(_scan_action(
        tmp_path, 'Material.move("material", to_site="target_site", from_site="source_site", carrier_device="arm_device")'
    ))
    assert explicit["transfer"]["source_site_uuid_param"] == "source_site"
    assert explicit["transfer"]["carrier_device_param"] == "arm_device"


def test_decorator_and_ast_scan_produce_the_same_contract(tmp_path: Path) -> None:
    material = '[Material.pick("material", from_site="source_site", carrier_device="arm_device")]'
    scanned = _contract(_scan_action(tmp_path, material))
    module = _import(tmp_path, _source(material))
    assert module.Robot.move._action_registry_meta["resource_contract"] == scanned
    assert scanned == {"version": 2, "transfer_step": {
        "operation": "pick", "material_param": "material", "owner_param": "",
        "site_param": "source_site", "carrier_params": ["arm_device"],
    }}


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            Material.load("s"),
            {"device_tenancy": {"mode": "task_while_loaded", "material_param": "s",
                                "acquire_device_param": EXECUTING_DEVICE, "release_device_param": ""}},
        ),
        (
            Material.load("s", device="b", from_device="a"),
            {"device_tenancy": {"mode": "task_while_loaded", "material_param": "s",
                                "acquire_device_param": "b", "release_device_param": "a"}},
        ),
        (
            Material.unload("s", device="v"),
            {"device_tenancy": {"mode": "task_while_loaded", "material_param": "s",
                                "acquire_device_param": "", "release_device_param": "v"}},
        ),
        (
            Material.unload("s"),
            {"device_tenancy": {"mode": "task_while_loaded", "material_param": "s",
                                "acquire_device_param": "", "release_device_param": EXECUTING_DEVICE}},
        ),
        (Material.in_place("s"), {"operate_in_place": {"material_param": "s"}}),
        (
            Material.pick("s"),
            {"transfer_step": {"operation": "pick", "material_param": "s", "owner_param": "",
                               "site_param": "", "carrier_params": []}},
        ),
        (
            Material.place("s", to_site="t", carrier_device="c"),
            {"transfer_step": {"operation": "place", "material_param": "s", "owner_param": "",
                               "site_param": "t", "carrier_params": ["c"]}},
        ),
        (
            Material.dispense(from_material="a", to_materials=["b", "c"]),
            {"aliquot": {"source_material_param": "a", "target_material_params": ["b", "c"]}},
        ),
        (
            Material.dispense(from_material="a", to_materials="b"),
            {"aliquot": {"source_material_param": "a", "target_material_params": ["b"]}},
        ),
        (Material.order_sensitive(), {"order_sensitive": True}),
    ],
)
def test_every_role_maps_to_the_existing_contract_field(role, expected) -> None:
    assert material_contract(role) == {"version": 2, **expected}


def test_site_picker_keeps_the_site_selector_shape() -> None:
    assert material_site_selectors(Material.site_picker("site", of="carrier", for_material="s")) == {
        "site": {"version": 1, "owner": "carrier", "occupant": "s", "show_occupied": True, "allow_occupied": False}
    }
    assert material_site_selectors(Material.site_picker("site", occupied="hide"))["site"] == {
        "version": 1, "owner": None, "occupant": None, "show_occupied": False, "allow_occupied": False,
    }
    assert material_site_selectors(Material.site_picker("site", of="o", occupied="allow"))["site"]["allow_occupied"]
    with pytest.raises(MaterialRoleError, match="hide"):
        Material.site_picker("site", occupied="never")


def test_conflicting_or_invalid_roles_are_rejected() -> None:
    with pytest.raises(MaterialRoleError, match="不能写在同一个动作上"):
        material_contract([Material.load("s", device="a"), Material.unload("s", device="b")])
    with pytest.raises(MaterialRoleError, match="不能写在同一个动作上"):
        material_contract([Material.move("s", to_site="t"), Material.pick("s")])
    with pytest.raises(MaterialRoleError):
        material_contract({"device_tenancy": {}})
    with pytest.raises(MaterialRoleError, match="不能是同一个参数"):
        Material.load("s", device="v", from_device="v")
    with pytest.raises(MaterialRoleError, match="不同的参数"):
        Material.dispense(from_material="a", to_materials=["a"])


@pytest.mark.parametrize(
    ("call", "hint"),
    [
        ('Material.transfer("material", to="other", site="target_site", gripper="robot.gripper")', "Material.move"),
        ('Material.aliquot(source="material", targets=["other"])', "Material.dispense"),
        ('Material.site("target_site", owner="other", occupant="material")', "Material.site_picker"),
        ('Material.uses(device=["arm_device"])', "按类型加锁"),
        ('Material.pick("material", owner="other", site="source_site", carriers=["other"])', "carrier_device"),
        ('Material.place("material", owner="other", site="target_site", carriers=["other"])', "to_site"),
    ],
)
def test_legacy_spellings_fail_in_the_decorator_and_in_the_scan(tmp_path: Path, call: str, hint: str) -> None:
    """旧写法：装饰器（导入时）与 AST 扫描都直接报错，错误信息给出新写法。"""

    with pytest.raises(MaterialRoleError, match=hint):
        _scan_action(tmp_path, call)
    with pytest.raises(MaterialRoleError, match=hint):
        _import(tmp_path, _source(call), name="robot_legacy")


@pytest.mark.parametrize(
    ("call", "hint"),
    [
        ('Material.move("material", to_site="label")', "必须注解为 SiteSlot"),
        ('Material.move("material", to_site="target_site", carrier_device="other")', "必须注解为 DeviceSlot"),
        ('Material.pick("label")', "必须注解为单个 ResourceSlot"),
        ('Material.load("material", device="other")', "必须注解为 DeviceSlot"),
        ('Material.site_picker("label")', "SiteSlot"),
        ('Material.site_picker("target_site", of="label")', "ResourceSlot 或 DeviceSlot"),
        ('Material.dispense(from_material="material", to_materials=["arm_device"])', "必须注解为单个 ResourceSlot"),
        ('Material.in_place("missing")', "签名里没有的参数.*missing"),
    ],
)
def test_parameter_types_are_checked_against_the_signature(tmp_path: Path, call: str, hint: str) -> None:
    """字符串参数对照签名：物料 ResourceSlot、库位 SiteSlot、设备 DeviceSlot；装饰器与 AST 扫描同一口径。"""

    with pytest.raises(ValueError, match=hint):
        _scan_action(tmp_path, call)
    with pytest.raises(ValueError, match=hint):
        _import(tmp_path, _source(call), name="robot_types")


def test_device_owner_and_omitted_owner_are_accepted_by_the_site_picker(tmp_path: Path) -> None:
    entry = _scan_action(tmp_path, '[Material.site_picker("target_site", of="arm_device", for_material="material")]')
    field = entry["canonical_schema"]["properties"]["goal"]["properties"]["target_site"]
    assert field["x-unilabos-site-selector"]["owner"] == "arm_device"
    assert field["x-unilabos-editor-control"] == "site_selector"
    entry = _scan_action(tmp_path, '[Material.site_picker("target_site", occupied="allow")]')
    selector = entry["canonical_schema"]["properties"]["goal"]["properties"]["target_site"]["x-unilabos-site-selector"]
    assert selector == {"version": 1, "owner": None, "occupant": None, "show_occupied": True, "allow_occupied": True}


def test_slot_parameters_carry_their_kind_in_the_schema(tmp_path: Path) -> None:
    """DeviceSlot / SiteSlot 字段带 x-unilabos-slot：调度器按它给设备 / 库位加锁。"""

    goal = _scan_action(tmp_path, 'Material.pick("material")')["canonical_schema"]["properties"]["goal"]["properties"]
    assert goal["arm_device"]["x-unilabos-slot"] == "device"
    assert goal["target_site"]["x-unilabos-slot"] == "site"
    assert "x-unilabos-slot" not in goal["label"]


def test_material_contract_field_warns_experimental_once_per_definition(tmp_path: Path, caplog) -> None:
    """整个 material_contract= 字段是实验性功能：写了就按定义告警一次（列出用到的声明），重复扫描不再告警。"""

    caplog.set_level(logging.WARNING)
    material = '[Material.load("material", device="vessel_device"), Material.order_sensitive()]'
    _scan_action(tmp_path, material)
    _scan_action(tmp_path, material)
    warnings = [record.getMessage() for record in caplog.records if "实验性功能" in record.getMessage()]
    assert len(warnings) == 1
    assert "material_contract= 是实验性功能" in warnings[0]
    assert "Material.load" in warnings[0] and "Material.order_sensitive" in warnings[0]


def test_material_actions_alone_also_warn_experimental(tmp_path: Path, caplog) -> None:
    """move / pick / place / dispense 不再单独算稳定：只写一条 Material.move 也告警一次。"""

    caplog.set_level(logging.WARNING)
    # 独立的模块名：告警按「定义 + 原因」进程内去重，不能与其他用例扫描过的 robot.Robot.move 同一身份
    path = tmp_path / "move_only_experimental.py"
    path.write_text(_source('Material.move("material", to_site="target_site")'), encoding="utf-8")
    _parse_file(path, tmp_path)
    _parse_file(path, tmp_path)
    warnings = [record.getMessage() for record in caplog.records if "实验性功能" in record.getMessage()]
    assert len(warnings) == 1
    assert "move_only_experimental.Robot.move" in warnings[0]
    assert "material_contract= 是实验性功能（Material.move）" in warnings[0]


def test_experimental_warning_from_the_decorator(caplog) -> None:
    from unilabos.registry.placeholder_type import ResourceSlot, SiteSlot

    caplog.set_level(logging.WARNING)

    class Driver:
        @action(material_contract=Material.in_place("sample"))
        def stir_experimental(self, sample: ResourceSlot) -> None:
            pass

        @action(material_contract=Material.move("plate", to_site="target_site"))
        def move_experimental(self, plate: ResourceSlot, target_site: SiteSlot) -> None:
            pass

    messages = [record.getMessage() for record in caplog.records]
    assert any("material_contract= 是实验性功能（Material.in_place）" in message for message in messages)
    assert any("move_experimental" in message and "material_contract= 是实验性功能（Material.move）" in message for message in messages)


@pytest.mark.parametrize("old_name", ["material", "materials"])
def test_old_field_names_fail_with_the_new_name(tmp_path: Path, old_name: str) -> None:
    """dev 上的旧名 material= 与短暂出现过的 materials= 在装饰器与 AST 扫描里都直接报错（不会被静默忽略），
    提示改成 material_contract=（值不变）。"""

    source = f'''
from unilabos.registry.decorators import Material, action, device
from unilabos.registry.placeholder_type import ResourceSlot, SiteSlot

@device(id="old_{old_name}_field", category=["test"])
class Driver:
    @action({old_name}=Material.move("plate", to_site="target_site"))
    def run(self, plate: ResourceSlot, target_site: SiteSlot) -> None:
        pass
'''
    path = tmp_path / f"old_{old_name}_field.py"
    path.write_text(source, encoding="utf-8")
    hint = f"不再接受 {old_name}=.*字段改名为 material_contract="
    with pytest.raises(RemovedActionOptionError, match=hint):
        _parse_file(path, tmp_path)
    with pytest.raises(RemovedActionOptionError, match=hint):
        _import(tmp_path, source, name=f"old_{old_name}_field_runtime")


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
    with pytest.raises(ValueError, match="material_contract= 只用于 JSON 动作"):
        action(action_type="unilabos_msgs.action:SendCmd", material_contract=Material.in_place("cmd"))(send)
