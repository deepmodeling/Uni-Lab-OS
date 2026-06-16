from __future__ import annotations

import json
from pathlib import Path

from tests.psuedo_devices.common.run_opcua_ci import (
    OpcUaCiCase,
    discover_manifests,
    render_value,
)


def test_discover_manifests_loads_enabled_cases(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "tests" / "psuedo_devices" / "demo"
    manifest_dir.mkdir(parents=True)
    manifest = manifest_dir / "ci.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "demo",
                "port": 50123,
                "object_name": "DemoDevice",
                "csv": "tests/demo.csv",
                "daemon_flow": "tests/demo_flow.json",
                "pytest": "tests/test_demo.py",
            }
        ),
        encoding="utf-8",
    )

    cases = discover_manifests(tmp_path)

    assert cases == [
        OpcUaCiCase(
            name="demo",
            manifest_path=manifest,
            port=50123,
            object_name="DemoDevice",
            csv="tests/demo.csv",
            daemon_flow="tests/demo_flow.json",
            pytest="tests/test_demo.py",
        )
    ]


def test_render_value_supports_endpoint_workspace_and_port() -> None:
    assert (
        render_value("{endpoint}::{workspace}::{port}", workspace="/repo",
                     endpoint="opc.tcp://localhost:50123/", port=50123)
        == "opc.tcp://localhost:50123/::/repo::50123"
    )
