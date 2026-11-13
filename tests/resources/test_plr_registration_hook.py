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
