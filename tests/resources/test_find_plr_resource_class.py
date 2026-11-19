"""find_plr_resource_class：本进程未 import 的外部资源类按注册表条目懒加载。

外部设备包的注册表由 AST 扫描得到、不 import 模块；Host 侧按 uuid 从权威拉取
Slave 侧物料（外部包自定义 Deck）时，类只在注册表条目里有 module 路径。
"""

from __future__ import annotations

import sys
import textwrap

from unilabos.registry.registry import lab_registry
from unilabos.resources.resource_tracker import find_plr_resource_class


def test_lazy_import_by_registry_entry(tmp_path, monkeypatch) -> None:
    package = tmp_path / "lazy_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "labware.py").write_text(
        textwrap.dedent(
            """
            from pylabrobot.resources import Deck


            class LazyDemoDeck(Deck):
                def __init__(self, name: str, **kwargs):
                    super().__init__(400.0, 320.0, 0.0, name)
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(
        lab_registry.resource_type_registry,
        "lazy_demo_deck",
        {"class": {"module": "lazy_pkg.labware:LazyDemoDeck", "type": "pylabrobot"}},
    )
    assert "lazy_pkg.labware" not in sys.modules

    found = find_plr_resource_class("LazyDemoDeck")

    assert found is not None and found.__name__ == "LazyDemoDeck"
    assert "lazy_pkg.labware" in sys.modules
    monkeypatch.delitem(sys.modules, "lazy_pkg.labware", raising=False)
    monkeypatch.delitem(sys.modules, "lazy_pkg", raising=False)


def test_builtin_class_is_found_without_registry() -> None:
    found = find_plr_resource_class("Plate")
    assert found is not None and found.__name__ == "Plate"


def test_unknown_class_returns_none() -> None:
    assert find_plr_resource_class("DefinitelyNotAResourceClass") is None
