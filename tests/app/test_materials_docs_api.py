"""物料文档里写给驱动用的设备节点方法必须真实存在（FRAMEWORK_GAPS #33）。

文档曾写 ``node.transfer_resource_to_another(...)``，``DeviceNode`` 没有这个方法；
设备内转移物料用 ``materials.transfer``。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from unilabos.backend.runtime.node import DeviceNode

DOCS = Path(__file__).resolve().parents[2] / "docs/developer_guide/examples"
MATERIAL_DOCS = (
    "materials_operations_guide.md",
    "materials_tutorial.md",
    "materials_construction_guide.md",
    "workstation_architecture.md",
)


@pytest.mark.parametrize("name", MATERIAL_DOCS)
def test_documented_device_node_calls_exist(name: str) -> None:
    text = (DOCS / name).read_text(encoding="utf-8")
    called = set(re.findall(r"\bnode\.([A-Za-z_][A-Za-z0-9_]*)\(", text))
    missing = sorted(method for method in called if not hasattr(DeviceNode, method))
    assert missing == [], f"{name} 写了 DeviceNode 没有的方法: {missing}"
    assert "transfer_resource_to_another" not in text
