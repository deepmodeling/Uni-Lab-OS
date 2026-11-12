"""Backend-neutral Registry contract for the built-in host service."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

_INSPECT_SCRIPT = r"""
import json
import sys
from pathlib import Path

from unilabos.config.config import BasicConfig

root = Path.cwd()
BasicConfig.backend = sys.argv[1]
BasicConfig.enable_resource_load = False

from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.registry import Registry
from unilabos.server.backend.legacy_adaptor.sync.templates import _template_definition

devices, _resources, _workflows = _parse_file(
    root / "unilabos" / "backend" / "host_services.py",
    root,
)
registry = Registry()
registry._host_node_ast_entry = registry._build_device_entry_from_ast(
    "host_node",
    devices[0],
)
registry._setup_host_node()
registry.resolve_all_types()
host = next(
    item for item in registry.obtain_registry_device_info()
    if item["id"] == "host_node"
)
definition = _template_definition(host, expected_type="device")

_data, _complete, yaml_valid, yaml_ids = registry._load_single_device_file(
    root / "unilabos" / "registry" / "devices" / "virtual_device.yaml",
    complete_registry=False,
)
print(
    "HOST_INSPECTION="
    + json.dumps(
        {
            "definition": definition,
            "yaml_valid": yaml_valid,
            "yaml_ids": sorted(yaml_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
)
"""


def _inspect_backend(backend: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _INSPECT_SCRIPT, backend],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    prefix = "HOST_INSPECTION="
    line = next(
        line for line in result.stdout.splitlines() if line.startswith(prefix)
    )
    return json.loads(line.removeprefix(prefix))


@pytest.mark.skipif(
    importlib.util.find_spec("rosidl_parser") is None or importlib.util.find_spec("rclpy") is None,
    reason="对比 ros2 后端需要 ROS 2 运行时",
)
def test_host_node_registry_definition_is_backend_neutral(tmp_path) -> None:
    from unilabos.server.services.runtime.registry import RegistryService

    hostlink = _inspect_backend("hostlink")
    ros2 = _inspect_backend("ros2")

    assert hostlink["definition"] == ros2["definition"]
    assert hostlink["definition"]["class"]["module"] == (
        "unilabos.backend.host_services:HostServices"
    )
    assert set(hostlink["definition"]["class"]["action_value_mappings"]) == {
        "apply_deduct_resource",
        "discard_resource",
        "manual_confirm",
        "set_substance",
        "test_latency",
        "test_resource",
        "transfer_resource",
    }
    assert hostlink["yaml_valid"] is True
    assert hostlink["yaml_ids"]

    authority = RegistryService(tmp_path / "runtime.db")
    try:
        authority.report([hostlink["definition"]])
        repeated = authority.report([ros2["definition"]])
        assert repeated["summary"]["counts"]["unchanged"] == 1
        assert repeated["summary"]["counts"]["updated"] == 0
        assert repeated["summary"]["counts"]["pending"] == 0
    finally:
        authority.close()
