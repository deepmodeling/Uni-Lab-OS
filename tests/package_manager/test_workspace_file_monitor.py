"""稳定工作区文件输入代监视器的公共生命周期合同。"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unilabos.package_manager import StableWorkspaceFileMonitor


def test_monitor_submits_only_one_stable_complete_workspace_generation(
    tmp_path: Path,
) -> None:
    """多个相邻文件事件必须收敛成一个稳定工作区输入代。

    参数：``tmp_path`` 提供工作区根和测试源码。
    返回：无；断言监视器只提交完整稳定代，不解释文件内容，并在关闭后停止提交。
    异常：线程无法启动、零散事件泄漏或停止失败时测试失败。
    """

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    graph_path = workspace_root / "graph.json"
    source_path = workspace_root / "device.py"
    graph_path.write_text('{"nodes": []}\n', encoding="utf-8")
    source_path.write_text("VERSION = 1\n", encoding="utf-8")
    monitor = StableWorkspaceFileMonitor(
        workspace_root,
        graph_argument="graph.json",
        interval_seconds=0.01,
        settle_seconds=0.04,
    )
    initial_generation = monitor.capture()
    submitted: list[Any] = []
    submitted_event = threading.Event()

    def accept_generation(generation: Any) -> Any:
        """记录监视器提交的一个稳定工作区输入代。

        参数：``generation`` 是监视器完成去抖后的不可变输入代。
        返回：最小 ``pending_restart`` 结果替身，表示该代已经被协调器接收。
        异常：无。
        """

        submitted.append(generation)
        submitted_event.set()
        return SimpleNamespace(outcome="pending_restart")

    monitor.start(accept_generation)
    # 两次紧邻写入代表编辑器的中间态与完整态，前者不得被提交。
    source_path.write_text("VERSION =", encoding="utf-8")
    source_path.write_text("VERSION = 2\n", encoding="utf-8")

    assert submitted_event.wait(timeout=2)
    monitor.close()
    source_path.write_text("VERSION = 3\n", encoding="utf-8")

    assert len(submitted) == 1
    assert submitted[0].identity != initial_generation.identity
    assert submitted[0].workspace_root == workspace_root
    assert submitted[0].graph_argument == "graph.json"


def test_monitor_capture_is_content_stable_and_does_not_start_a_thread(
    tmp_path: Path,
) -> None:
    """同步捕获只计算文件输入代身份，不启动后台生命周期。

    参数：``tmp_path`` 提供一个最小工作区。
    返回：无；断言相同内容重复捕获得到同一身份，且关闭未启动监视器保持幂等。
    异常：文件观察不稳定或关闭产生副作用时测试失败。
    """

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace_root.joinpath("graph.json").write_text(
        '{"nodes": []}\n',
        encoding="utf-8",
    )
    monitor = StableWorkspaceFileMonitor(
        workspace_root,
        graph_argument="graph.json",
    )

    first = monitor.capture()
    second = monitor.capture()
    monitor.close()
    monitor.close()

    assert first == second


def test_monitor_excludes_runtime_build_and_environment_outputs_but_keeps_assets(
    tmp_path: Path,
) -> None:
    """运行输出、构建目录和环境目录不得制造虚假工作区源码代。

    参数：``tmp_path`` 提供含运行数据库、环境目录、构建产物和声明资产的工作区。
    返回：无；断言基础设施文件变化不改变输入身份，而普通资产字节变化会改变。
    异常：监视器解析领域内容、遗漏资产或观察 SQLite/WAL 噪声时测试失败。
    """

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace_root.joinpath("graph.json").write_text(
        '{"nodes": []}\n',
        encoding="utf-8",
    )
    asset_path = workspace_root / "assets" / "icon.svg"
    asset_path.parent.mkdir()
    asset_path.write_text("<svg>v1</svg>\n", encoding="utf-8")
    ignored_files = (
        workspace_root / "unilabos_data" / "workflow_history.db",
        workspace_root / "unilabos_data" / "workflow_history.db-wal",
        workspace_root / "unilabos_data" / "workflow_history.db-shm",
        workspace_root / ".unilabos" / "inventory.db",
        workspace_root / ".unilabos" / "logs" / "runtime.log",
        workspace_root / ".venv" / "installed.py",
        workspace_root / "venv" / "installed.py",
        workspace_root / "build" / "package.whl",
        workspace_root / "dist" / "package.whl",
        workspace_root / "node_modules" / "index.js",
    )
    for ignored_file in ignored_files:
        ignored_file.parent.mkdir(parents=True, exist_ok=True)
        ignored_file.write_text("generation-1\n", encoding="utf-8")
    monitor = StableWorkspaceFileMonitor(
        workspace_root,
        graph_argument="graph.json",
    )

    initial = monitor.capture()
    for ignored_file in ignored_files:
        ignored_file.write_text("generation-2\n", encoding="utf-8")
    after_runtime_writes = monitor.capture()
    asset_path.write_text("<svg>v2</svg>\n", encoding="utf-8")
    after_asset_change = monitor.capture()

    assert after_runtime_writes.identity == initial.identity
    assert after_asset_change.identity != initial.identity


def test_monitor_excludes_agent_native_skill_projections_but_rejects_other_links(
    tmp_path: Path,
) -> None:
    """Agent 技能投影不属于实验源码，其他符号链接仍须关闭式拒绝。"""

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace_root.joinpath("graph.json").write_text(
        '{"nodes": []}\n',
        encoding="utf-8",
    )
    private_skill = workspace_root / ".unilabos" / "agent" / "skill"
    private_skill.parent.mkdir(parents=True)
    private_skill.write_text("runtime skill\n", encoding="utf-8")
    package_root = workspace_root / "editable_package"
    for native_root in (".claude", ".codex", ".cursor"):
        skill_link = package_root / native_root / "skills" / "runtime-skill"
        skill_link.parent.mkdir(parents=True)
        skill_link.symlink_to(private_skill)
    monitor = StableWorkspaceFileMonitor(
        workspace_root,
        graph_argument="graph.json",
    )

    initial = monitor.capture()
    private_skill.write_text("updated runtime skill\n", encoding="utf-8")
    assert monitor.capture().identity == initial.identity

    unsafe_link = workspace_root / "workflows" / "unsafe.py"
    unsafe_link.parent.mkdir()
    unsafe_link.symlink_to(private_skill)
    with pytest.raises(ValueError, match="不得包含符号链接"):
        monitor.capture()


def test_monitor_excludes_explicit_working_directory_inside_workspace(
    tmp_path: Path,
) -> None:
    """显式工作区内运行目录写入不得推进源码输入代。

    参数：``tmp_path`` 提供工作区和非默认运行目录。
    返回：无；断言数据库、WAL 和日志变化均被精确排除，普通源码变化仍可见。
    异常：监视器忽略整个工作区、遗漏显式运行目录或产生虚假刷新时测试失败。
    """

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    graph_path = workspace_root / "graph.json"
    graph_path.write_text('{"nodes": []}\n', encoding="utf-8")
    source_path = workspace_root / "device.py"
    source_path.write_text("VERSION = 1\n", encoding="utf-8")
    # ``runtime_directory`` 模拟调用者通过 ``--working_dir`` 选择的工作区内状态根。
    runtime_directory = workspace_root / "runtime-state"
    runtime_directory.mkdir()
    database_path = runtime_directory / "workflow_history.db"
    database_path.write_text("generation-1\n", encoding="utf-8")
    monitor = StableWorkspaceFileMonitor(
        workspace_root,
        graph_argument="graph.json",
        ignored_paths=(runtime_directory,),
    )

    initial = monitor.capture()
    database_path.write_text("generation-2\n", encoding="utf-8")
    runtime_directory.joinpath("runtime.log").write_text(
        "runtime output\n",
        encoding="utf-8",
    )
    after_runtime_writes = monitor.capture()
    source_path.write_text("VERSION = 2\n", encoding="utf-8")
    after_source_change = monitor.capture()

    assert after_runtime_writes.identity == initial.identity
    assert after_source_change.identity != initial.identity
