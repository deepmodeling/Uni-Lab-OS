"""``--external_devices_only`` 只保留驱动包自己的条目，但核心的 ``container`` 资源模板必须在：
图里的容器节点都引用它。"""

from __future__ import annotations

import sys
from pathlib import Path

from unilabos.registry import registry as registry_module
from unilabos.registry.registry import Registry


def test_external_only_keeps_the_core_container_template(monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    registry = Registry()
    registry.setup(external_only=True)

    container = registry.resource_type_registry.get("container")
    assert container is not None, sorted(registry.resource_type_registry)
    assert container["class"]["module"] == "unilabos.resources.presets.container:get_regular_container"
    assert {handle["handler_key"] for handle in container["handles"]} >= {"top", "bottom"}
    # 其它内置条目照旧不加载
    assert "virtual_pump" not in registry.device_type_registry
    assert set(registry.resource_type_registry) == {"container"}


def test_external_only_core_files_exist() -> None:
    core = Path(registry_module.__file__).resolve().parent
    for relative in registry_module.EXTERNAL_ONLY_CORE_RESOURCE_FILES:
        assert (core / relative).is_file(), relative
