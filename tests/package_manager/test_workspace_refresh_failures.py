"""工作区包运行时（WorkspacePackageRuntime）的失败原子性合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.package_manager import test_workspace_package_runtime as support
from unilabos.package_manager import WorkspacePackageRuntime


@pytest.mark.parametrize("failure_kind", ("compile", "publish"))
def test_failed_refresh_preserves_all_old_generation_authorities(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    """候选编译或原子发布失败必须完整保留旧工作区代。

    参数：``tmp_path`` 隔离稳定输入代；``failure_kind`` 选择编译或发布失败点。
    返回：无；断言注册表、模板、工作流源码授权、设备和库存的发布 Adapter
    仍指向旧候选，运行时只增加诊断。
    异常：失败后活跃身份或发布记录发生部分变化时测试失败。
    """

    initial_input = support._input_generation(tmp_path, "input-a")
    changed_input = support._input_generation(tmp_path, "input-b")
    initial_candidate = support._candidate(tmp_path / "candidate-a")
    changed_candidate = support._candidate(
        tmp_path / "candidate-b",
        idle_driver_hash="idle-v2",
    )
    publisher = support.RecordingGenerationPublisher()

    def prepare(generation: Any) -> Any:
        """按测试失败点返回候选或注入编译错误。

        参数：``generation`` 是运行时请求的稳定输入代。
        返回：初始或变化候选。
        异常：编译失败用例对变化代抛出 ``ValueError``。
        """

        if generation is initial_input:
            return initial_candidate
        if failure_kind == "compile":
            raise ValueError("注入的包目录（PackageCatalog）编译失败")
        return changed_candidate

    runtime = WorkspacePackageRuntime(
        initial_input,
        prepare_generation=prepare,
        publisher=publisher,
    )
    runtime.start()
    if failure_kind == "publish":
        publisher.fail_next = True

    result = runtime.refresh(changed_input)

    assert result.outcome == "failed"
    assert result.error is not None
    assert publisher.active is initial_candidate
    assert publisher.replacements == []
    status = runtime.status()
    assert status.active_input_identity == "input-a"
    assert status.observed_input_identity == "input-b"
    assert status.last_outcome == "failed"
    assert status.last_error is not None
