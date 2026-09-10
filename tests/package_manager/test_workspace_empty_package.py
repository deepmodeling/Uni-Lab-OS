"""新工作区包不含工作流源码（Workflow Source）时的启动合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unilabos.package_manager import prepare_workspace_startup
from unilabos.workflow.source_discovery import discover_editable_sources


def _write_empty_workspace(workspace_root: Path, *, workflows_yaml: str) -> None:
    """写入只声明包身份、尚未导出工作流源码的最小工作区。

    参数：``workspace_root`` 是测试显式授权的工作区根；``workflows_yaml`` 是
    ``package.yaml`` 中待验证的 ``workflows`` YAML 值。
    返回：无；创建可导入包骨架与封闭来源清单。
    异常：文件系统写入失败时向测试传播原始异常。
    """

    # ``package_root`` 是注册表（Registry）允许静态扫描、但本测试不激活的包目录。
    package_root = workspace_root / "demo_lab"
    package_root.mkdir(parents=True)
    package_root.joinpath("__init__.py").write_text("", encoding="utf-8")
    workspace_root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "demo-lab"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    workspace_root.joinpath("package.yaml").write_text(
        f"package:\n  name: demo_lab\nworkflows: {workflows_yaml}\n",
        encoding="utf-8",
    )


def test_empty_workflow_manifest_produces_valid_zero_source_startup_plan(
    tmp_path: Path,
) -> None:
    """证明 ``workflows: []`` 是可加载新包的合法空声明。

    参数：``tmp_path`` 提供隔离工作区和包文件。
    返回：无；断言启动计划授权包扫描、登记零项工作流源码（Workflow Source），
    且来源发现不会创建工作流（Workflow）或工作流任务（WorkflowTask）。
    异常：空清单若被错误拒绝，准备函数会抛出异常并使测试失败。
    """

    # ``workspace_root`` 是本次启动唯一显式工作区身份。
    workspace_root = tmp_path / "workspace"
    _write_empty_workspace(workspace_root, workflows_yaml="[]")
    # ``startup_arguments`` 模拟公共命令行（CLI）解析后的可变启动参数。
    startup_arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": None,
        "workflow_editable_package_root": None,
        "graph": None,
    }

    # ``startup_plan`` 是静态校验通过、尚未激活任何运行实例的工作区投影。
    startup_plan = prepare_workspace_startup(startup_arguments)
    # ``source_plan`` 是同一封闭清单产生的工作流源码发现结果。
    source_plan = discover_editable_sources((workspace_root,))

    assert startup_plan is not None
    assert startup_plan.has_workflow_manifest is True
    assert startup_plan.workflow_source_count == 0
    assert startup_arguments["devices"] == [str(workspace_root / "demo_lab")]
    assert startup_arguments["workflow_editable_package_root"] == [str(workspace_root)]
    assert source_plan.registrations == ()


def test_null_workflow_manifest_is_not_treated_as_empty_package(
    tmp_path: Path,
) -> None:
    """证明缺少列表值不能冒充明确的空工作流声明。

    参数：``tmp_path`` 提供隔离工作区和包文件。
    返回：无；断言只有显式 ``[]`` 表示零项工作流源码（Workflow Source）。
    异常：公开启动准备应以稳定 ``ValueError`` 关闭式拒绝空值。
    """

    # ``workspace_root`` 是包含不合法来源清单的隔离工作区。
    workspace_root = tmp_path / "workspace"
    _write_empty_workspace(workspace_root, workflows_yaml="null")
    # ``startup_arguments`` 代表尚未应用任何部分计划的启动输入。
    startup_arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": None,
        "workflow_editable_package_root": None,
        "graph": None,
    }

    with pytest.raises(ValueError, match="package.yaml"):
        prepare_workspace_startup(startup_arguments)

    assert startup_arguments["devices"] is None
    assert startup_arguments["workflow_editable_package_root"] is None
