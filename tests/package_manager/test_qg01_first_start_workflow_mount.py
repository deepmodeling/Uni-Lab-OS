"""QG01 工作区（Workspace）首次挂载工作流源码的回归合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unilabos.package_manager import prepare_workspace_startup
from unilabos.workflow import composition
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService

SZLAB_TRANSFER_WORKFLOW_UUID = "e7c53119-9fde-5250-9bf5-264f23d157a8"
CATALOG_FINGERPRINT = f"sha256:{'f' * 64}"


class _SourceOnlyCompiler:
    """为启动回归测试提供不改变应用图的可信编译端口。"""

    compiler_version = "qg01-source-only-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        """把已发现源码投影为不改变应用图的候选编译结果。

        参数：``workflow_uuid``、``workflow_revision`` 与 ``source_uri`` 标识工作流
        （Workflow）编译上下文；``python_source`` 是已授权的工作流源码（Workflow
        Source）；``applied_graph`` 是当前应用图。返回：只保留源码且不创建节点的
        候选编译结果。异常：无；本替身只验证公共启动组合顺序。
        """

        # 这些身份字段由公共组合根提供，本回归编译器不重新解释其业务含义。
        del workflow_uuid, workflow_revision, source_uri
        return CandidateCompilation(
            diagnostics=[],
            graph=applied_graph,
            normalized_python_source=python_source,
            source_map=[],
            changeset={
                "kind": "source_only",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


class _NoopSourceMonitor:
    """在秒级启动测试中保留源码监视器生命周期而不创建后台线程。"""

    def __init__(self, service: WorkflowService) -> None:
        """保存已经恢复完成的工作流服务（WorkflowService）。

        参数：``service`` 是组合根准备公开的本地工作流权威。返回：无。
        异常：无；实例只验证组合边界，不读取或修改领域事实。
        """

        # ``service`` 保持真实组合对象存活，避免测试替身改变资源所有权关系。
        self.service = service

    def start(self) -> None:
        """确认源码监视器可以进入已启动生命周期。

        参数：无。返回：无。异常：无；本测试刻意不创建后台文件监视线程。
        """

    def stop(self) -> None:
        """结束无后台线程的测试监视器。

        参数：无。返回：无。异常：无；重复调用保持幂等。
        """


@pytest.fixture(autouse=True)
def _isolate_workflow_composition() -> Any:
    """隔离每个用例使用的进程级工作流组合根。

    参数：无。返回：pytest 生命周期控制值。异常：清理失败原样传播，防止测试
    进程遗留工作流权威或源码监视器资源。
    """

    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def _write_szlab_transfer_workspace(workspace_root: Path) -> str:
    """创建只声明 SZLab 物料转移工作流的最小真实工作区。

    参数：``workspace_root`` 是公共命令行（CLI）显式授权的工作区根。返回：写入
    的工作流源码（Workflow Source）文本。异常：文件系统写入失败时原样抛出，
    不允许组合根消费不完整夹具。
    """

    # ``package_root`` 是注册表（Registry）和工作流源码发现共享的本地包目录。
    package_root = workspace_root / "szlab_poly_studio"
    # ``source_path`` 是物料转移工作流（Workflow）的稳定源码坐标。
    source_path = package_root / "workflows" / "material_transfer.py"
    source_path.parent.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    python_source = (
        "from unilabos.workflow.authoring import workflow\n"
        f'@workflow(workflow_uuid="{SZLAB_TRANSFER_WORKFLOW_UUID}", '
        'displayname="SZLab 标准物料转运")\n'
        "def material_transfer() -> None:\n"
        "    return None\n"
    )
    source_path.write_text(python_source, encoding="utf-8")
    (workspace_root / "pyproject.toml").write_text(
        '[project]\nname = "szlab-poly-studio"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (workspace_root / "package.yaml").write_text(
        "package:\n"
        "  name: szlab_poly_studio\n"
        "workflows:\n"
        f"  - workflow_uuid: {SZLAB_TRANSFER_WORKFLOW_UUID}\n"
        "    source: szlab_poly_studio/workflows/material_transfer.py\n",
        encoding="utf-8",
    )
    return python_source


def test_empty_database_mounts_szlab_workflow_without_creating_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空数据库首启应挂载 SZLab 工作流定义且不创建工作流任务（WorkflowTask）。

    参数：``tmp_path`` 隔离真实工作区与空 SQLite；``monkeypatch`` 把后台监视器
    替换为保留生命周期的同步测试适配器。返回：无；断言公共工作区准备与工作流
    组合路径可查询源码草稿且任务集合仍为空。异常：若定义未在来源注册前建立，
    当前实现会抛出 ``workflow_not_found`` 并使本回归测试 RED。
    """

    workspace_root = tmp_path / "szlab-workspace"
    python_source = _write_szlab_transfer_workspace(workspace_root)
    # ``startup_arguments`` 模拟公共 ``unilab --workspace`` 解析后的启动参数。
    startup_arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": None,
        "workflow_editable_package_root": None,
        "graph": None,
    }
    startup_plan = prepare_workspace_startup(startup_arguments)
    monkeypatch.setattr(composition, "WorkflowSourceMonitor", _NoopSourceMonitor)

    service = composition.compose_workflow_runtime(
        tmp_path / "unilabos-data",
        compiler=_SourceOnlyCompiler(),
        editable_package_roots=tuple(
            startup_arguments["workflow_editable_package_root"] or ()
        ),
    )

    assert startup_plan is not None
    assert service.get_workflow(SZLAB_TRANSFER_WORKFLOW_UUID)["name"] == (
        "szlab_poly_studio.material_transfer"
    )
    assert service.get_authoring(SZLAB_TRANSFER_WORKFLOW_UUID)["draft"][
        "python_source"
    ] == python_source
    assert service.list_workflow_tasks(page=1, page_size=20)["total"] == 0
