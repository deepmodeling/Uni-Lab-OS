"""工作区刷新协调器（WorkspaceRefreshCoordinator）的稳定输入代合同。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.package_manager import test_workspace_package_runtime as support
from unilabos.package_manager import (
    WorkspaceInputGeneration,
    WorkspacePackageRuntime,
    WorkspaceRefreshCoordinator,
)


class RecordingStableMonitor:
    """只提交完整稳定输入代、不读取或解释文件的测试监视器 Adapter。"""

    def __init__(self) -> None:
        """建立尚未连接协调器的监视器。

        参数：无。
        返回：无；启动、关闭计数与提交回调均为空。
        异常：无。
        """

        self.start_calls = 0
        self.close_calls = 0
        self._submit: Callable[[WorkspaceInputGeneration], object] | None = None

    def start(
        self,
        submit: Callable[[WorkspaceInputGeneration], object],
    ) -> None:
        """保存稳定输入代提交回调。

        参数：``submit`` 是协调器提供的唯一命令接缝。
        返回：无；重复启动由协调器阻止。
        异常：无。
        """

        self.start_calls += 1
        self._submit = submit

    def emit(self, generation: WorkspaceInputGeneration) -> object:
        """提交一个已由监视器完成稳定观察的工作区输入代。

        参数：``generation`` 是完整稳定输入代，不包含待解释的文件事件。
        返回：协调器给出的刷新结果。
        异常：尚未启动时抛出 ``AssertionError``。
        """

        assert self._submit is not None
        return self._submit(generation)

    def close(self) -> None:
        """停止继续提交输入代。

        参数：无。
        返回：无；记录关闭次数并移除回调。
        异常：无。
        """

        self.close_calls += 1
        self._submit = None


class FailOnceClosingMonitor(RecordingStableMonitor):
    """第一次关闭失败、第二次确认停止的测试监视器 Adapter。"""

    def __init__(self) -> None:
        """建立带一次注入关闭故障的监视器。

        参数：无。
        返回：无；继承启动状态并把首次关闭标记为尚未发生。
        异常：无。
        """

        super().__init__()
        self._close_failure_pending = True

    def close(self) -> None:
        """第一次报告无法确认停止，第二次完成真实关闭。

        参数：无。
        返回：第二次及以后关闭时返回无。
        异常：第一次固定抛出 ``RuntimeError``，用于验证协调器保留重试所有权。
        """

        self.close_calls += 1
        if self._close_failure_pending:
            self._close_failure_pending = False
            raise RuntimeError("注入的监视器关闭失败")
        self._submit = None


def test_coordinator_passes_stable_generations_without_interpreting_files(
    tmp_path: Path,
) -> None:
    """协调器只能串行转交稳定输入代并管理两个生命周期。

    参数：``tmp_path`` 隔离初始与变化候选。
    返回：无；断言监视器只启动一次，变化代由运行时热发布，关闭顺序最终使两者
    停止，协调器不读取工作区内任何文件。
    异常：协调器另建扫描/编译权威、重复启动监视器或遗漏关闭时测试失败。
    """

    initial_input = support._input_generation(tmp_path, "input-a")
    changed_input = support._input_generation(tmp_path, "input-b")
    candidates = {
        "input-a": support._candidate(tmp_path / "candidate-a"),
        "input-b": support._candidate(
            tmp_path / "candidate-b",
            workflow_hash="workflow-v2",
        ),
    }
    publisher = support.RecordingGenerationPublisher()

    def prepare_candidate(generation: WorkspaceInputGeneration) -> object:
        """按稳定输入代身份读取本用例的完整候选。

        参数：``generation`` 是协调器提交给运行时的输入代。
        返回：同身份预构造候选。
        异常：收到未知身份时抛出 ``KeyError``。
        """

        return candidates[generation.identity]

    runtime = WorkspacePackageRuntime(
        initial_input,
        prepare_generation=prepare_candidate,
        publisher=publisher,
    )
    monitor = RecordingStableMonitor()
    coordinator = WorkspaceRefreshCoordinator(runtime, monitor)

    coordinator.start()
    coordinator.start()
    result = monitor.emit(changed_input)

    assert result.outcome == "hot_published"
    assert monitor.start_calls == 1
    assert coordinator.status().active_input_identity == "input-b"

    coordinator.close()
    coordinator.close()
    assert monitor.close_calls == 1
    assert coordinator.status().state == "closed"


def test_coordinator_retries_monitor_close_before_releasing_ownership(
    tmp_path: Path,
) -> None:
    """监视器首次关闭失败后必须允许再次确认停止。

    参数：``tmp_path`` 隔离最小候选和输入代。
    返回：无；断言第一次错误后运行时已拒绝刷新，第二次仍调用监视器并最终关闭。
    异常：协调器提前把自己标为已关闭、跳过第二次监视器关闭或重新开放运行时
    时测试失败。
    """

    initial_input = support._input_generation(tmp_path, "input-a")
    initial_candidate = support._candidate(tmp_path / "candidate-a")

    def prepare_initial_candidate(
        _generation: WorkspaceInputGeneration,
    ) -> object:
        """返回本测试唯一初始候选。

        参数：``_generation`` 是运行时请求准备的初始输入代。
        返回：预先构造的不可变候选。
        异常：无。
        """

        return initial_candidate

    runtime = WorkspacePackageRuntime(
        initial_input,
        prepare_generation=prepare_initial_candidate,
        publisher=support.RecordingGenerationPublisher(),
    )
    monitor = FailOnceClosingMonitor()
    coordinator = WorkspaceRefreshCoordinator(runtime, monitor)
    coordinator.start()

    with pytest.raises(RuntimeError, match="注入的监视器关闭失败"):
        coordinator.close()

    assert coordinator.status().state == "closed"
    coordinator.close()
    assert monitor.close_calls == 2
