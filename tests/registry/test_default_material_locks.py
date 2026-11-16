"""物料天然带锁：动作的每个物料入参都默认加共享资源锁，驱动不能再按动作改写。"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import List, Optional

import pytest

from unilabos.registry import material_locks
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import action, get_action_meta
from unilabos.registry.material_locks import (
    default_material_lock_parameters,
    is_material_slot_type,
)
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.registry.registry import Registry

_LOGGER = "unilabos.registry.material_locks"


@pytest.fixture(autouse=True)
def fresh_override_warnings(monkeypatch):
    # 告警按定义在进程内只发一次；每个测试从空集合开始，避免依赖执行顺序。
    monkeypatch.setattr(material_locks, "_warned_definitions", set())


def _override_warnings(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == _LOGGER]


@pytest.mark.parametrize(
    "type_hint",
    [
        "ResourceSlot",
        "List[ResourceSlot]",
        "Optional[ResourceSlot]",
        "Optional[List[ResourceSlot]]",
        "ResourceSlot | None",
        "Tuple[ResourceSlot, ...]",
        "unilabos.registry.placeholder_type:ResourceSlot",
        ("list", "unilabos.registry.placeholder_type:ResourceSlot"),
        ResourceSlot,
        List[ResourceSlot],
        Optional[ResourceSlot],
    ],
)
def test_material_slot_shapes(type_hint) -> None:
    assert is_material_slot_type(type_hint)


@pytest.mark.parametrize(
    "type_hint",
    ["", None, "str", "dict", "DeviceSlot", "SiteSlot", "Dict[str, ResourceSlot]", "Union[ResourceSlot, str]"],
)
def test_non_material_shapes(type_hint) -> None:
    assert not is_material_slot_type(type_hint)


def test_runtime_signature_locks_every_material_parameter() -> None:
    def run(self, plate: ResourceSlot, tips: List[ResourceSlot] = None, volume: float = 1.0) -> None:
        del plate, tips, volume

    def marked(self, vessel: dict, speed: int = 1) -> None:
        del vessel, speed

    assert default_material_lock_parameters(params=inspect.signature(run).parameters.values()) == ["plate", "tips"]
    assert default_material_lock_parameters(
        params=inspect.signature(marked).parameters.values(),
        placeholder_keys={"vessel": "unilabos_resources"},
    ) == ["vessel"]


def test_unknown_signature_derives_only_from_material_placeholders() -> None:
    assert default_material_lock_parameters(params=None) == []
    keys = {"plate": "unilabos_resources", "device": "unilabos_devices"}
    assert default_material_lock_parameters(params=None, placeholder_keys=keys) == ["plate"]


def test_removed_decorator_overrides_still_load_and_warn_once_per_definition(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    # 旧写法（含以前会被拒绝的冲突、拼错与错误形状）都照常加载：值被忽略，不再抛错。
    @action(materials_free=["tips"])
    def free_tips(self, plate: ResourceSlot, tips: List[ResourceSlot] = None) -> None:
        del plate, tips

    @action(materials_need_lock=[])
    def unlocked(self, plate: ResourceSlot) -> None:
        del plate

    @action(materials_need_lock=["missing"], materials_free="plate")  # type: ignore[arg-type]
    def malformed(self, plate: ResourceSlot) -> None:
        del plate

    for func in (free_tips, unlocked, malformed):
        meta = get_action_meta(func)
        assert "materials_need_lock" not in meta and "materials_free" not in meta

    messages = _override_warnings(caplog)
    assert len(messages) == 3
    assert all("已废弃并被忽略" in message for message in messages)
    assert any("free_tips" in message and "materials_free=" in message for message in messages)
    assert any("malformed" in message and "materials_free=、materials_need_lock=" in message for message in messages)

    # 同一定义（同一 模块.类.方法）再出现不重复告警：装饰器与 AST 扫描共用这一身份。
    assert not material_locks.warn_ignored_material_lock_override(
        f"{free_tips.__module__}.{free_tips.__qualname__}", ["materials_free="]
    )


def test_other_unknown_decorator_arguments_are_still_rejected() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument 'material_free'"):

        @action(material_free=["plate"])  # type: ignore[call-arg]
        def typo(self, plate: ResourceSlot) -> None:
            del plate


def test_ast_and_registry_lock_every_material_parameter(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    module_path = tmp_path / "default_lock_driver.py"
    module_path.write_text(
        '''
from typing import List, Optional
from unilabos.registry.decorators import action, device
from unilabos.registry.placeholder_type import ResourceSlot

@device(id="default_lock_test", category=["test"])
class Driver:
    @action()
    def transfer(self, source: ResourceSlot, targets: List[ResourceSlot], volume: float = 1.0) -> None:
        pass

    @action(materials_free=["reference"])
    def compare(self, sample: ResourceSlot, reference: Optional[ResourceSlot] = None) -> None:
        pass

    @action(materials_need_lock=[])
    def look(self, sample: ResourceSlot) -> None:
        pass

    def shake(self, plate: ResourceSlot, rpm: int = 300) -> None:
        pass
''',
        encoding="utf-8",
    )
    devices, _resources, _workflows = _parse_file(module_path, tmp_path)
    ast_actions = devices[0]["actions"]
    # AST 结果既不带旧的改写，也不再带推导值：推导只发生在注册表条目里
    for name in ("transfer", "compare", "look"):
        assert "materials_need_lock" not in ast_actions[name]["action_args"]
        assert "materials_free" not in ast_actions[name]["action_args"]

    entry = Registry()._build_device_entry_from_ast("default_lock_test", devices[0])
    mappings = entry["class"]["action_value_mappings"]
    assert mappings["transfer"]["materials_need_lock"] == ["source", "targets"]
    # 以前用 materials_free / [] 豁免的物料入参现在同样加锁
    assert mappings["compare"]["materials_need_lock"] == ["sample", "reference"]
    assert mappings["look"]["materials_need_lock"] == ["sample"]
    assert "materials_free" not in mappings["compare"]
    # 未加 @action 的 auto- 动作同样默认给物料入参加锁
    assert mappings["auto-shake"]["materials_need_lock"] == ["plate"]

    messages = _override_warnings(caplog)
    assert len(messages) == 2
    assert any("default_lock_driver.Driver.compare" in message for message in messages)
    assert any("default_lock_driver.Driver.look" in message for message in messages)
