"""旧导入路径与公开路径拿到同一批工站类，注册表里的 ProtocolNode 解析到公开类。

导入工站基类会带入 ROS 节点，需要 rclpy，已登记在 ``tests/ci_ros_required.txt``。
"""

import importlib
from pathlib import Path

import pytest

pytest.importorskip("rclpy")

ROOT = Path(__file__).resolve().parents[2]


def test_old_path_shares_the_same_objects_as_the_public_path() -> None:
    public = importlib.import_module("unilabos.workstation")
    old = importlib.import_module("unilabos.devices.workstation.workstation_base")
    for name in public.__all__:
        assert getattr(old, name) is getattr(public, name), name


def test_subclass_of_old_path_is_a_public_workstation() -> None:
    from unilabos.devices.workstation.workstation_base import WorkstationBase as OldBase
    from unilabos.workstation import WorkstationBase

    class PackageWorkstation(OldBase):
        pass

    assert issubclass(PackageWorkstation, WorkstationBase)


def test_registry_protocol_node_resolves_to_the_public_class() -> None:
    from unilabos.workstation import ProtocolNode

    registry = ROOT / "unilabos" / "registry" / "devices" / "work_station.yaml"
    modules = [
        line.split("module:", 1)[1].strip()
        for line in registry.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("module:") and line.strip().endswith(":ProtocolNode")
    ]
    assert modules == ["unilabos.workstation.base:ProtocolNode"]
    module_name, _, attribute = modules[0].partition(":")
    assert getattr(importlib.import_module(module_name), attribute) is ProtocolNode
