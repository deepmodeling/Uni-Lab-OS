"""Workspace Host 外部设备包加载范围配置契约。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unilabos.workspace_host.discovery import ensure_local_token
from unilabos.workspace_host.host import WorkspaceHost
from unilabos.workspace_host.launch import resolve_backend_launch, resolve_edge_launch
from unilabos.workspace_host.model import WorkspaceHostError, WorkspacePaths


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """创建具备最小图文件与本地配置的测试工作区并返回其根路径。"""

    root = tmp_path / "workspace"
    (root / "deployment" / "graphs").mkdir(parents=True)
    (root / "deployment" / "graphs" / "graph.json").write_text("{}\n")
    (root / "deployment" / "local_config.py").write_text("# fixture\n")
    return root


def test_configuration_update_persists_only_boolean_external_device_scope(
    workspace: Path,
) -> None:
    """配置更新只接受布尔值，并把显式退出外部设备包限制持久化到 Host 快照。"""

    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    try:
        assert host.snapshot()["configuration"]["externalDevicesOnly"] is True
        snapshot = host._dispatch(
            "configuration.update", {"externalDevicesOnly": False}
        )
        assert snapshot["configuration"]["externalDevicesOnly"] is False
        assert json.loads(paths.environment.read_text())["externalDevicesOnly"] is False

        with pytest.raises(WorkspaceHostError) as caught:
            host._dispatch(
                "configuration.update", {"externalDevicesOnly": "false"}
            )
        assert caught.value.code == "configuration_invalid"
    finally:
        host.close()


def test_backend_and_edge_launch_share_external_device_scope(
    workspace: Path,
) -> None:
    """Backend 与 Edge 从同一配置元数据决定是否附加仅加载外部设备包参数。"""

    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    ensure_local_token(paths)

    default_backend = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        backend_port=48_201,
    )
    default_edge = resolve_edge_launch(
        paths,
        {"address": default_backend.address, "metadata": default_backend.metadata},
    )
    assert default_backend.metadata["externalDevicesOnly"] is True
    assert "--external_devices_only" in default_backend.command
    assert "--external_devices_only" in default_edge.command

    paths.environment.write_text(
        json.dumps({"schemaVersion": 1, "externalDevicesOnly": False})
    )
    unrestricted_backend = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        backend_port=48_203,
    )
    unrestricted_edge = resolve_edge_launch(
        paths,
        {
            "address": unrestricted_backend.address,
            "metadata": unrestricted_backend.metadata,
        },
    )
    assert unrestricted_backend.metadata["externalDevicesOnly"] is False
    assert "--external_devices_only" not in unrestricted_backend.command
    assert "--external_devices_only" not in unrestricted_edge.command
