"""Workspace Host 固定业务服务端口转发契约。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from unilabos.workspace_host.discovery import ensure_local_token
from unilabos.workspace_host.host import WorkspaceHost
from unilabos.workspace_host.launch import LaunchPlan
from unilabos.workspace_host.model import WorkspacePaths


def test_workspace_host_forwards_explicit_backend_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """容器部署应只把固定 Backend 端口交给启动计划。

    参数：``tmp_path`` 提供隔离工作区，``monkeypatch`` 隔离真实子进程。返回：
    无；断言 Host 代际始终复用显式端口，使 Kubernetes Service 可稳定指向
    Backend；未由业务进程监听的 HostLink 不应成为部署接口。异常：端口转发缺失
    或被改写时断言失败。
    """

    workspace = tmp_path / "workspace"
    (workspace / "deployment" / "graphs").mkdir(parents=True)
    (workspace / "deployment" / "graphs" / "graph.json").write_text("{}\n")
    (workspace / "deployment" / "local_config.py").write_text("# fixture\n")
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    captured: dict[str, object] = {}

    def fake_backend(*_args: object, **kwargs: object) -> LaunchPlan:
        """记录 Host 传给 Backend 启动解析器的显式端口。

        参数：``_args`` 是本测试不使用的位置参数，``kwargs`` 包含 Host 传入的
        启动选项。返回：无需创建真实进程的 Backend ``LaunchPlan``。异常：无；
        测试计划仅写入隔离临时目录。
        """

        captured.update(kwargs)
        return LaunchPlan(
            component="backend",
            command=(sys.executable, "-c", "pass"),
            cwd=workspace,
            environment=dict(os.environ),
            generation="fixed-port-generation",
            log_path=paths.logs / "backend.log",
            address="http://127.0.0.1:48111",
            ready_url="http://127.0.0.1:48111/api/v1/readiness",
            metadata={"runtimeMode": "dry-run"},
        )

    monkeypatch.setattr(
        "unilabos.workspace_host.host.resolve_backend_launch",
        fake_backend,
    )
    host = WorkspaceHost(
        paths,
        ensure_local_token(paths),
        readiness_timeout=0.1,
        backend_port=48_111,
    )
    monkeypatch.setattr(host, "_spawn", Mock(return_value=None))
    monkeypatch.setattr(host, "_wait_backend_ready", Mock(return_value=[]))
    try:
        host._start_backend({"runtimeMode": "dry-run"})
    finally:
        host.close()

    assert captured["backend_port"] == 48_111
    assert "hostlink_port" not in captured
