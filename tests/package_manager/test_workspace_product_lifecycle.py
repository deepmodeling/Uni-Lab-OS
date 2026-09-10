"""工作区产品生命周期组合根的单编译与关闭式刷新合同。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.package_manager import test_workspace_package_runtime as support
from tests.package_manager.test_package_dependency_lock import _write_package
from tests.package_manager.test_workspace_refresh_coordinator import (
    RecordingStableMonitor,
)
from unilabos.package_manager import (
    WorkspaceGenerationChangedError,
    WorkspaceInputGeneration,
    WorkspaceSource,
    compile_package_source,
    compose_workspace_product_lifecycle,
    prepare_stable_workspace_product_generation,
)
from unilabos.package_manager.workspace_runtime import (
    discovery as workspace_startup_module,
)


def test_product_lifecycle_reuses_precompiled_initial_candidate(
    tmp_path: Path,
) -> None:
    """产品生命周期启动不得再次编译已经准备完成的首代。

    参数：``tmp_path`` 提供候选来源与输入代目录。
    返回：无；断言首代直接发布，后续稳定变化才调用编译接缝，且在完整原子发布
    能力补齐前关闭式进入 ``pending_restart``。
    异常：首代二次编译、监视器未接线或变化发生部分热发布时测试失败。
    """

    initial_candidate = support._candidate(tmp_path / "candidate-a")
    changed_candidate = support._candidate(
        tmp_path / "candidate-b",
        idle_driver_hash="idle-v2",
    )
    initial_input = support._input_generation(tmp_path, "input-a")
    changed_input = support._input_generation(tmp_path, "input-b")
    monitor = RecordingStableMonitor()
    registry = SimpleNamespace(device_type_registry={}, resource_type_registry={})
    compiled_inputs: list[WorkspaceInputGeneration] = []

    def prepare_changed_generation(
        generation: WorkspaceInputGeneration,
    ) -> Any:
        """记录首代之后真正需要编译的稳定输入代。

        参数：``generation`` 是监视器提交的新完整输入代。
        返回：对应的已验证测试候选。
        异常：收到非预期输入身份时测试断言失败。
        """

        compiled_inputs.append(generation)
        assert generation is changed_input
        return changed_candidate

    lifecycle = compose_workspace_product_lifecycle(
        initial_candidate,
        registry=registry,
        initial_input=initial_input,
        monitor=monitor,
        prepare_generation=prepare_changed_generation,
    )

    lifecycle.start()
    result = monitor.emit(changed_input)
    lifecycle.close()

    assert compiled_inputs == [changed_input]
    assert result.outcome == "pending_restart"
    assert result.restart_reasons == ("complete_generation_atomic_publish_unavailable",)
    assert registry.device_type_registry["community.runtime_lab.selected_device"][
        "class"
    ]["module"].endswith("Selected_Device")


def test_stable_product_preparation_rejects_compile_time_file_change(
    tmp_path: Path,
) -> None:
    """首代编译前后文件身份不同不得把新身份错误绑定到旧候选。

    参数：``tmp_path`` 提供可在编译接缝内修改的真实工作区。
    返回：无；断言准备过程关闭式失败，且静态编译器只执行一次。
    异常：变化被忽略、候选被发布或编译被无界重试时测试失败。
    """

    workspace_root = tmp_path / "workspace"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    workspace_root.joinpath("graph.json").write_text(
        '{"nodes": []}\n',
        encoding="utf-8",
    )
    compile_calls = 0

    def compile_then_change(source: Any) -> Any:
        """编译一次目录后模拟编辑器在同一启动窗口写入声明资产。

        参数：``source`` 是产品统一工作区来源。
        返回：文件变化前已经完成的完整包目录（PackageCatalog）。
        异常：真实静态编译失败时传播异常。
        """

        nonlocal compile_calls
        compile_calls += 1
        catalog = compile_package_source(source)
        source.root.joinpath("README.md").write_text(
            "changed during compile\n",
            encoding="utf-8",
        )
        return catalog

    with pytest.raises(WorkspaceGenerationChangedError):
        prepare_stable_workspace_product_generation(
            {
                "workspace": str(workspace_root),
                "graph": "graph.json",
                "devices": None,
                "workflow_editable_package_root": None,
            },
            compile_catalog=compile_then_change,
        )

    assert compile_calls == 1


def test_product_initial_generation_reads_each_manifest_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """产品首代的项目清单与工作流清单必须各自只冻结一次。

    参数：``tmp_path`` 提供含空工作流清单的最小工作区；``monkeypatch`` 记录两个
    安全文件读取接缝。
    返回：无；断言启动计划和包目录（PackageCatalog）复用同一原始字节观察。
    异常：任一清单被二次读取、候选无法准备或摘要回退到磁盘重读时测试失败。
    """

    workspace_root = tmp_path / "workspace"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    workspace_root.joinpath("graph.json").write_text(
        '{"nodes": []}\n',
        encoding="utf-8",
    )
    project_manifest_reads = 0
    workflow_manifest_reads = 0
    original_read_bytes = WorkspaceSource.read_bytes
    original_read_package_root = workspace_startup_module.read_package_root

    def record_project_manifest_read(
        source: WorkspaceSource,
        logical_path: str,
    ) -> bytes:
        """记录通过授权来源读取的项目清单次数。

        参数：``source`` 是产品工作区来源；``logical_path`` 是待读取逻辑路径。
        返回：真实来源读取的原始字节。
        异常：真实安全文件读取失败时传播原异常。
        """

        nonlocal project_manifest_reads
        if logical_path == "pyproject.toml":
            project_manifest_reads += 1
        return original_read_bytes(source, logical_path)

    def record_workflow_manifest_read(
        selected_root: str | Path,
    ) -> object:
        """记录固定目录身份并读取工作流清单的次数。

        参数：``selected_root`` 是显式工作区根。
        返回：真实安全读取器产生的目录快照。
        异常：目录或清单不安全时传播原异常。
        """

        nonlocal workflow_manifest_reads
        workflow_manifest_reads += 1
        return original_read_package_root(selected_root)

    monkeypatch.setattr(WorkspaceSource, "read_bytes", record_project_manifest_read)
    monkeypatch.setattr(
        workspace_startup_module,
        "read_package_root",
        record_workflow_manifest_read,
    )

    prepared = prepare_stable_workspace_product_generation(
        {
            "workspace": str(workspace_root),
            "graph": "graph.json",
            "devices": None,
            "workflow_editable_package_root": None,
        }
    )

    assert prepared is not None
    assert project_manifest_reads == 1
    assert workflow_manifest_reads == 1
