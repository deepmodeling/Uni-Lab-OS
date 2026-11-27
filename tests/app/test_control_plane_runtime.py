"""验证本地 Scheduler 与 Edge 进程的运行所有权。"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from unilabos.app.control_plane import (
    ControlPlaneMode,
    ControlPlaneRuntimeContext,
    should_mount_embedded_scheduler_routes,
    should_mount_workspace_authoring_routes,
    start_control_plane_runtime,
    validate_control_plane_arguments,
)
from unilabos.app.main import (
    _add_legacy_startup_mode_compatibility,
    parse_args,
)
from unilabos.app.runtime_topology import (
    RuntimeProcessRole,
    publish_edge_runtime_ready_signal,
    resolve_runtime_process_plan,
)
from unilabos.config.config import BasicConfig


def test_control_plane_defaults_to_backend_without_devices() -> None:
    arguments = vars(parse_args().parse_args([]))

    assert arguments["control_plane"] == "local"
    assert validate_control_plane_arguments(arguments) is ControlPlaneMode.LOCAL
    plan = resolve_runtime_process_plan(arguments)
    assert plan.role is RuntimeProcessRole.WORKSPACE_BACKEND
    assert plan.starts_web_server
    assert not plan.initializes_host_devices


def test_removed_combined_role_is_rejected_at_both_entry_points() -> None:
    """命令行和直接构造的运行计划都不能恢复已废弃的角色。"""
    with pytest.raises(SystemExit):
        parse_args().parse_args(["--process_role", "combined"])
    with pytest.raises(ValueError, match="workspace_backend 或 edge_runtime"):
        resolve_runtime_process_plan({"process_role": "combined"})


def test_scheduler_stack_requires_explicit_edge_authority() -> None:
    """缺少跨进程权威时失败，不回退到加载 HostNode 的执行器。"""
    from unilabos.app.scheduler.stack import create_edge_stack

    with pytest.raises(TypeError):
        create_edge_stack()
    with pytest.raises(ValueError, match="跨进程 Edge"):
        create_edge_stack(execution_backend=None)


def test_legacy_workspace_host_startup_mode_is_accepted_without_runtime_effect() -> None:
    """旧 Host 参数只作为滚动升级兼容，不重新引入全局启动模式。"""

    import argparse

    parser = argparse.ArgumentParser()
    _add_legacy_startup_mode_compatibility(parser)

    arguments = vars(parser.parse_args(["--run_mode", "develop"]))

    assert arguments["_legacy_startup_mode"] == "develop"
    assert "startup_mode" not in arguments


def test_workbench_split_runtime_roles_are_orthogonal_to_local_authority() -> None:
    workspace_backend = vars(
        parse_args().parse_args(
            ["--process_role", "workspace_backend", "--app_bridges", "fastapi"]
        )
    )
    edge_runtime = vars(
        parse_args().parse_args(
            [
                "--process_role",
                "edge_runtime",
                "--app_bridges",
                "edge_control",
            ]
        )
    )

    backend_plan = resolve_runtime_process_plan(workspace_backend)
    edge_plan = resolve_runtime_process_plan(edge_runtime)

    assert backend_plan.role is RuntimeProcessRole.WORKSPACE_BACKEND
    assert backend_plan.control_plane is ControlPlaneMode.LOCAL
    assert backend_plan.starts_web_server
    assert not backend_plan.initializes_host_devices
    assert edge_plan.role is RuntimeProcessRole.EDGE_RUNTIME
    assert edge_plan.control_plane is ControlPlaneMode.LOCAL
    assert not edge_plan.starts_web_server
    assert edge_plan.initializes_host_devices


def test_runtime_plan_rejects_removed_backend_control_plane() -> None:
    """运行计划拒绝已移除的 Backend 控制面。

    参数：无。返回：无。异常：解析或计划层仍接受 ``backend`` 时由断言暴露。
    状态不变量：拆分进程只共享本地 Scheduler 的事实源。
    """

    for retired in ("--is_slave", "--slave_no_host", "--hostlink_addr"):
        with pytest.raises(SystemExit):
            parse_args().parse_args([retired])
    with pytest.raises(ValueError, match="不能使用 --is_slave"):
        resolve_runtime_process_plan({"process_role": "edge_runtime", "app_bridges": ["edge_control"], "is_slave": True})
    with pytest.raises(SystemExit):
        parse_args().parse_args(["--control_plane", "backend"])
    with pytest.raises(ValueError, match="仅支持 local"):
        resolve_runtime_process_plan({"control_plane": "backend"})


def test_workspace_backend_starts_local_station_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作区调度进程启动本地工站 Scheduler。

    参数：``tmp_path`` 提供隔离工作目录，``monkeypatch`` 替换真实启动函数。
    返回：无；断言控制面入口委托本地 Scheduler。异常：若误走远程控制面或启动
    其他运行时，断言失败。状态不变量：该进程计划固定为 local。
    """

    from unilabos.app.scheduler import runtime as scheduler_runtime

    expected = object()
    monkeypatch.setattr(
        scheduler_runtime,
        "start_embedded_scheduler_runtime",
        lambda _context: expected,
    )
    context = ControlPlaneRuntimeContext(
        arguments={
            "control_plane": "local",
            "process_role": "workspace_backend",
            "app_bridges": ["fastapi"],
            "is_slave": False,
            "preserve_runtime_databases": True,
        },
        working_dir=str(tmp_path),
        resource_tree_set=object(),
        registry=object(),
        graph_source_id="graph.json",
        material_shapes=(),
        material_model_catalog=None,
    )

    assert start_control_plane_runtime(context) is expected


def test_edge_runtime_ready_signal_is_atomic(tmp_path) -> None:
    ready_path = tmp_path / "edge" / "ready.json"

    publish_edge_runtime_ready_signal(str(ready_path))

    assert ready_path.read_text(encoding="utf-8").startswith(
        '{"schemaVersion": 1, "pid": '
    )
    assert list(ready_path.parent.glob("*.tmp")) == []


def test_backend_control_plane_is_rejected_by_argument_parser() -> None:
    """命令行不再提供 Backend 控制面选项。

    参数：无。返回：无；解析 ``backend`` 必须以 ``SystemExit`` 关闭失败。异常：
    解析器仍接受该值时测试失败。状态不变量：公开命令行只保留 local。
    """

    with pytest.raises(SystemExit):
        parse_args().parse_args(["--control_plane", "backend"])


def test_local_control_plane_rejects_production_bridge() -> None:
    arguments = vars(
        parse_args().parse_args(
            ["--control_plane", "local", "--app_bridges", "edge_control"]
        )
    )

    with pytest.raises(ValueError, match="仅允许 edge_runtime"):
        validate_control_plane_arguments(arguments)


def test_backend_control_plane_is_rejected_by_runtime_validator() -> None:
    """运行时校验不接受手工构造的 Backend 控制面参数。

    参数：无。返回：无。异常：验证必须抛出 ``ValueError``。状态不变量：运行计划
    不会产生 Backend 控制面。
    """

    with pytest.raises(ValueError, match="仅支持 local"):
        validate_control_plane_arguments({"control_plane": "backend"})


def test_removed_backend_runtime_does_not_start_anything(
    tmp_path: Path,
) -> None:
    """手工构造 Backend 计划时直接失败且不启动任何组件。

    参数：``tmp_path`` 提供隔离工作目录。返回：无。异常：控制面验证必须抛出
    ``ValueError``；若创建运行时或连接远端则测试失败。状态不变量：失败发生在
    组件装配前。
    """

    context = ControlPlaneRuntimeContext(
        arguments={
            "control_plane": "backend",
            "app_bridges": ["edge_control", "fastapi"],
            "is_slave": False,
            "preserve_runtime_databases": False,
        },
        working_dir=str(tmp_path),
        resource_tree_set=object(),
        registry=object(),
        graph_source_id="graph.json",
        material_shapes=(),
        material_model_catalog=None,
    )

    with pytest.raises(ValueError, match="仅支持 local"):
        start_control_plane_runtime(context)


@pytest.mark.parametrize(
    ("control_plane", "process_role", "expected"),
    [
        ("local", "combined", False),
        ("local", "workspace_backend", True),
        ("local", "edge_runtime", False),
    ],
)
def test_fastapi_mounts_scheduler_routes_for_station_authority(
    control_plane: str,
    process_role: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证只有工站调度权威进程挂载本地调度接口。

    参数：控制面模式、进程角色和预期值覆盖双进程组合；``monkeypatch`` 隔离
    全局配置。返回无；路由所有权偏离工站调度进程时断言失败。
    """

    monkeypatch.setattr(BasicConfig, "control_plane", control_plane)
    monkeypatch.setattr(BasicConfig, "process_role", process_role)

    assert should_mount_embedded_scheduler_routes() is expected


@pytest.mark.parametrize(
    ("process_role", "expected"),
    [("combined", False), ("workspace_backend", True), ("edge_runtime", False)],
)
def test_workspace_authoring_routes_follow_process_role_not_authority(
    process_role: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证本地工作流创作接口只由 Scheduler 进程提供。

    参数：``process_role`` 是进程角色，``expected`` 是路由挂载结果，
    ``monkeypatch`` 隔离全局配置。返回：无。异常：Edge Runtime 若挂载创作路由
    或 Scheduler 进程未挂载时测试失败。状态不变量：判断不再依赖远端 Authority。
    """

    monkeypatch.setattr(BasicConfig, "process_role", process_role)
    monkeypatch.setattr(BasicConfig, "control_plane", "local")

    assert should_mount_workspace_authoring_routes() is expected


def test_server_rejects_removed_backend_control_plane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web 组合根拒绝使用已移除的 Backend 控制面。

    参数：``tmp_path`` 提供隔离目录，``monkeypatch`` 注入非法控制面配置。返回：
    无。异常：``setup_server`` 必须抛出 ``RuntimeError``。状态不变量：任何路由
    装配前都不会连接远端 Backend。
    """

    monkeypatch.setattr(BasicConfig, "control_plane", "backend")
    monkeypatch.setattr(BasicConfig, "process_role", "workspace_backend")
    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
    monkeypatch.setattr(BasicConfig, "workspace_package_mount_projection", None)
    server = importlib.reload(importlib.import_module("unilabos.app.web.server"))
    with pytest.raises(RuntimeError, match="仅支持 local"):
        server.setup_server()

@pytest.mark.parametrize("key", ["UNILABOS_TWIN_ENABLED", "UNILABOS_TWIN_PROVIDER_CONFIG", "UNILABOS_TWIN_ACTION_SOCKET"])
def test_retired_simulation_configuration_fails_before_runtime_start(monkeypatch, key):
    monkeypatch.setenv(key, "legacy")
    with pytest.raises(ValueError, match="已移除"):
        resolve_runtime_process_plan({"process_role": "workspace_backend"})
