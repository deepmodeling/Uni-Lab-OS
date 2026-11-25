"""PLR 附加类注册钩子：内置模块、驱动包登记、失败隔离与幂等。"""

from __future__ import annotations

import sys

import pytest

from unilabos.resources import plr_additional_res_reg as hook


@pytest.fixture()
def isolated_hook(monkeypatch):
    """每个用例使用独立的登记表，不污染进程内全局状态。"""

    monkeypatch.setattr(hook, "_registered", dict(hook._registered))
    monkeypatch.setattr(hook, "_processed", set(hook._processed))
    return hook


@pytest.fixture()
def driver_pkg(tmp_path, monkeypatch):
    """在临时目录生成一个模拟外部驱动包，导入后自行登记资源模块。"""

    pkg = tmp_path / "fake_driver_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from unilabos.resources.plr_additional_res_reg import register_plr_module\n"
        "register_plr_module('fake_driver_pkg.labware')\n",
        encoding="utf-8",
    )
    (pkg / "labware.py").write_text(
        "from pylabrobot.resources import Deck\n"
        "class FakeDriverDeck(Deck):\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("fake_driver_pkg", "fake_driver_pkg.labware"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield "fake_driver_pkg"
    for name in ("fake_driver_pkg", "fake_driver_pkg.labware"):
        sys.modules.pop(name, None)


def test_builtin_plr_classes_register_through_the_hook(isolated_hook):
    from pylabrobot.liquid_handling.backends.backend import LiquidHandlerBackend
    from pylabrobot.resources import Resource
    from pylabrobot.utils.object_parsing import find_subclass

    assert set(hook._BUILTIN_PLR_MODULES) <= set(isolated_hook.registered_plr_modules())
    isolated_hook.register()
    assert find_subclass("WorkStationContainer", Resource) is not None
    assert find_subclass("UniLiquidHandlerRvizBackend", LiquidHandlerBackend) is not None


def test_driver_package_registers_modules_lazily(isolated_hook, driver_pkg):
    from pylabrobot.resources import Resource
    from pylabrobot.utils.object_parsing import find_subclass

    isolated_hook.register_plr_module(driver_pkg)
    assert "fake_driver_pkg.labware" not in sys.modules
    isolated_hook.register()
    # 包 __init__ 导入期间登记的子模块在同一次 register() 内被导入
    assert "fake_driver_pkg.labware" in isolated_hook.registered_plr_modules()
    assert find_subclass("FakeDriverDeck", Resource) is not None


def test_failed_module_is_logged_once_and_does_not_block_others(isolated_hook, driver_pkg, monkeypatch):
    errors: list[str] = []
    monkeypatch.setattr(hook.logger, "error", lambda message, *a, **k: errors.append(message))

    isolated_hook.register_plr_module("fake_driver_pkg_missing.labware", driver_pkg)
    isolated_hook.register()
    isolated_hook.register()

    assert len(errors) == 1 and "fake_driver_pkg_missing.labware" in errors[0]
    assert "fake_driver_pkg" in sys.modules


def test_register_plr_class_records_module_and_returns_class(isolated_hook):
    from pylabrobot.resources import Deck

    @isolated_hook.register_plr_class
    class LocalDeck(Deck):
        pass

    assert LocalDeck.__name__ == "LocalDeck"
    assert __name__ in isolated_hook.registered_plr_modules()


def test_register_plr_module_rejects_empty_path(isolated_hook):
    with pytest.raises(ValueError):
        isolated_hook.register_plr_module("")


def test_core_preset_classes_deserialize_in_a_fresh_process(tmp_path):
    """FRAMEWORK_GAPS #31：核心预设类（载架、仓库、瓶子、容器）由内置模块登记。

    新进程里没有任何代码先 import ``unilabos.resources.presets``（Host 按 uuid 拉取
    别的设备的物料、脚本恢复一棵树）时，反序列化也必须能找到这些类。
    """

    import json
    import os
    import subprocess

    from pylabrobot.resources import Coordinate, Resource, ResourceHolder

    from unilabos.resources.presets.container import RegularContainer
    from unilabos.resources.presets.itemized_carrier import Bottle, BottleCarrier
    from unilabos.resources.presets.warehouse import warehouse_factory
    from unilabos.resources.resource_tracker import ResourceTreeSet

    holder = ResourceHolder("bc_A1", size_x=20, size_y=20, size_z=0, child_location=Coordinate.zero())
    holder.location = Coordinate(5, 5, 0)
    carrier = BottleCarrier("bc", size_x=40, size_y=40, size_z=10, sites={"A1": holder})
    carrier["A1"] = Bottle("bottle", diameter=10, height=30, max_volume=100)
    warehouse = warehouse_factory("wh", num_items_x=1, num_items_y=1, num_items_z=1)
    bench = Resource("bench", size_x=500, size_y=500, size_z=10)
    bench.assign_child_resource(carrier, Coordinate(0, 0, 0))
    bench.assign_child_resource(warehouse, Coordinate(100, 0, 0))
    bench.assign_child_resource(
        RegularContainer("beaker", size_x=10, size_y=10, size_z=20, max_volume=50),
        Coordinate(300, 0, 0),
    )
    dumped = tmp_path / "tree.json"
    dumped.write_text(
        json.dumps(ResourceTreeSet.from_plr_resources([bench], known_random_uuid=True).dump()),
        encoding="utf-8",
    )
    script = (
        "import json, sys\n"
        "from unilabos.resources.resource_tracker import ResourceTreeSet\n"
        "assert not [m for m in sys.modules if m.startswith('unilabos.resources.presets')]\n"
        "with open(sys.argv[1], encoding='utf-8') as fh:\n"
        "    bench = ResourceTreeSet.load(json.load(fh)).to_plr_resources()[0]\n"
        "print(json.dumps(sorted(type(r).__name__ for r in bench.get_all_children())))\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [os.getcwd(), os.environ.get("PYTHONPATH")]))}
    completed = subprocess.run(
        [sys.executable, "-c", script, str(dumped)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    classes = set(json.loads(completed.stdout.strip().splitlines()[-1]))
    assert {"BottleCarrier", "Bottle", "WareHouse", "RegularContainer"} <= classes


def test_class_check_finds_core_presets_before_any_tree_is_rebuilt():
    """Host 重启后先逐台核对设备物料的 config.type（``require_plr_config_type``），再建 PLR 实例。

    核对本身也要先导入登记的模块：否则新进程里第一台持有 ``BottleCarrier`` / ``WareHouse`` 的设备
    在任何 ``to_plr_resources`` 之前被核对，类「不存在」，设备误进维护模式（是否出错取决于设备启动顺序）。
    """

    import os
    import subprocess

    script = (
        "import sys\n"
        "from unilabos.resources.resource_tracker import find_plr_resource_class, require_plr_config_type\n"
        "assert not [m for m in sys.modules if m.startswith('unilabos.resources.presets')]\n"
        "for name in ('BottleCarrier', 'Bottle', 'WareHouse', 'RegularContainer'):\n"
        "    require_plr_config_type({'type': name}, uuid='u-' + name, template_name='t')\n"
        "    assert find_plr_resource_class(name).__name__ == name\n"
        "print('ok')\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [os.getcwd(), os.environ.get("PYTHONPATH")]))}
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8", env=env, timeout=300
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    assert completed.stdout.strip().splitlines()[-1] == "ok"


@pytest.fixture()
def registry_pkg(tmp_path, monkeypatch):
    """注册表里登记了工厂函数、自己不主动登记的驱动包：类只有 import 工厂所在的模块后才可见。"""

    from unilabos.registry.registry import lab_registry

    pkg = tmp_path / "fake_registry_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "labware.py").write_text(
        "from pylabrobot.resources import Resource\n"
        "class FakeRegistryPlate(Resource):\n"
        "    def __init__(self, name, size_x=10.0, size_y=10.0, size_z=10.0, category=None, model=None, **kwargs):\n"
        "        super().__init__(name=name, size_x=size_x, size_y=size_y, size_z=size_z, category=category, model=model)\n"
        "def make_fake_plate(name):\n"
        "    return FakeRegistryPlate(name)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("fake_registry_pkg", "fake_registry_pkg.labware"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(
        lab_registry.resource_type_registry,
        "fake_registry_plate",
        {"class": {"module": "fake_registry_pkg.labware:make_fake_plate", "type": "pylabrobot"}},
    )
    yield "fake_registry_plate"
    for name in ("fake_registry_pkg", "fake_registry_pkg.labware"):
        sys.modules.pop(name, None)


def test_registry_name_registers_the_module_its_factory_lives_in(isolated_hook, registry_pkg):
    from pylabrobot.resources import Resource
    from pylabrobot.utils.object_parsing import find_subclass

    # 注册表名 → class.module 的模块部分；PLR 类名（孔位）与空名字跳过
    assert isolated_hook.register_registry_resources(registry_pkg, "Well", "") == ("fake_registry_pkg.labware",)
    assert isolated_hook.register_registry_resources(registry_pkg) == ()
    assert "fake_registry_pkg.labware" not in sys.modules

    isolated_hook.register()

    assert "fake_registry_pkg.labware" in sys.modules
    assert find_subclass("FakeRegistryPlate", Resource) is not None


def test_rebuild_registers_the_tree_classes_and_deserializes_by_config_type(isolated_hook, registry_pkg):
    from uuid import uuid4

    from unilabos.resources.resource_tracker import (
        ResourceDict,
        ResourceDictInstance,
        ResourceTreeInstance,
        ResourceTreeSet,
    )

    node = ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": "plate_1",
                "uuid": str(uuid4()),
                "name": "plate_1",
                "type": "plate",
                "class": registry_pkg,
                "template_name": registry_pkg,
                "config": {"type": "FakeRegistryPlate", "name": "plate_1"},
                "data": {},
                "extra": {},
                "pose": {"size": {"width": 10, "height": 10, "depth": 10}},
            }
        )
    )

    (plate,) = ResourceTreeSet([ResourceTreeInstance(node)]).to_plr_resources()

    assert type(plate).__name__ == "FakeRegistryPlate"
    assert "fake_registry_pkg.labware" in isolated_hook.registered_plr_modules()
