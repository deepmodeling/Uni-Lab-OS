"""工作区输入代聚合显式外部包目录（PackageCatalog）的合同。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.package_manager.test_package_dependency_lock import _write_package
from unilabos.app.community_packages import prepare_community_packages
from unilabos.app.workspace_package_bootstrap import local_package_namespaces
from unilabos.package_manager import (
    PackageCatalog,
    PackageDependencyManager,
    WorkspaceSource,
    compile_package_source,
    prepare_workspace_registry_runtime,
)

EXTERNAL_WORKFLOW_UUID = "73333333-3333-4333-8333-333333333333"


def _prepare_external_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """建立主工作区、显式锁定外部包和引用外部设备的物理图（Graph）。

    参数：``tmp_path`` 是测试隔离父目录。
    返回：主工作区根和外部包根。
    异常：文件写入、软件包编译或依赖锁发布失败时传播原异常。
    """

    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external_lab"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader",),
        resource_ids=("plate",),
    )
    workspace_root.joinpath("graph.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "external-reader-a",
                        "class": "community.external_lab.reader",
                        "type": "device",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    PackageDependencyManager(
        workspace_root,
        compile_catalog=compile_package_source,
    ).add("../external_lab")
    return workspace_root, external_root


def _write_external_workflow_and_shape(external_root: Path) -> None:
    """给外部包增加只读工作流源码与一个显式物料外形。

    参数：``external_root`` 是尚未写入依赖锁的显式外部工作区根。
    返回：无；写入的软件包清单、工作流源码（Workflow Source）与外形资产共同
    属于下一次完整包目录（PackageCatalog）静态编译代。
    异常：文件系统写入失败时传播原始异常。
    """

    # ``external_package_root`` 是锁定外部包唯一允许静态编译和后续有限导入的根。
    external_package_root = external_root / "external_lab"
    external_root.joinpath("package.yaml").write_text(
        "package: {name: external_lab}\n"
        "workflows:\n"
        f"  - workflow_uuid: {EXTERNAL_WORKFLOW_UUID}\n"
        "    source: external_lab/workflows/inspect.py\n",
        encoding="utf-8",
    )
    workflow_root = external_package_root / "workflows"
    workflow_root.mkdir(parents=True, exist_ok=True)
    workflow_root.joinpath("inspect.py").write_text(
        "from unilabos.workflow.authoring import workflow\n\n"
        f'@workflow(workflow_uuid="{EXTERNAL_WORKFLOW_UUID}", '
        'displayname="外部只读检查")\n'
        "def inspect_external():\n"
        "    return {}\n",
        encoding="utf-8",
    )
    shaped_resource = external_package_root / "shaped_resource.py"
    shaped_resource.write_text(
        "from unilabos.registry.decorators import resource\n\n"
        "@resource(\n"
        "    id='shaped_plate',\n"
        "    category=['container'],\n"
        "    model={'shape': {\n"
        "        'format': 'unilab.shape/v1',\n"
        "        'entry': 'models/shape.yml',\n"
        "    }},\n"
        ")\n"
        "def make_shaped_plate(name: str):\n"
        "    return name\n",
        encoding="utf-8",
    )
    shape_path = external_package_root / "models" / "shape.yml"
    shape_path.parent.mkdir(parents=True, exist_ok=True)
    shape_path.write_text(
        "schema_version: 1\n"
        "shape:\n"
        "  id: external-shaped-plate\n"
        "  display_name: 外部孔板\n"
        "  applies_to: [{category: container}]\n"
        "  envelope: [127, 85, 15]\n"
        "  parts:\n"
        "    - {type: box, from: [0, 0, 0], to: [127, 85, 15]}\n",
        encoding="utf-8",
    )


def test_runtime_generation_aggregates_only_explicit_locked_catalogs(
    tmp_path: Path,
) -> None:
    """工作区运行代必须把主目录与显式锁定外部目录一起完整校验。

    参数：``tmp_path`` 提供主包和外部包。
    返回：无；断言外部设备/资源完整可查询，物理图（Graph）可以有限选择外部设备，
    且运行代记录依赖声明与锁的稳定摘要。
    异常：若准备路径扫描环境、漏掉锁定包或在聚合前解析物理图则测试失败。
    """

    workspace_root, _external_root = _prepare_external_workspace(tmp_path)

    runtime = prepare_workspace_registry_runtime(
        {
            "workspace": str(workspace_root),
            "graph": "graph.json",
            "devices": None,
            "workflow_editable_package_root": None,
        }
    )

    assert runtime is not None
    assert tuple(
        catalog.namespace for catalog in runtime.registry_snapshot.package_catalogs
    ) == ("community.external_lab", "community.workspace_lab")
    assert tuple(item.fqid for item in runtime.registry_snapshot.resources) == (
        "community.external_lab.plate",
    )
    assert runtime.activation_plan.selected_definition_fqids == (
        "community.external_lab.reader",
    )
    assert runtime.dependency_revision.startswith("sha256:")


def test_locked_external_namespace_never_falls_back_to_remote_resolution(
    tmp_path: Path,
) -> None:
    """显式锁定外部包必须在遗留社区包链中保持本地来源身份。

    参数：``tmp_path`` 提供引用外部设备的主工作区、外部包和隔离缓存目录。
    返回：无；断言主包与外部包都进入本地命名空间映射，离线准备不产生远端目录。
    异常：外部包被误判为缺失社区包、触发远端解析或形成第二来源时测试失败。
    """

    workspace_root, external_root = _prepare_external_workspace(tmp_path)
    runtime = prepare_workspace_registry_runtime(
        {
            "workspace": str(workspace_root),
            "graph": "graph.json",
            "devices": None,
            "workflow_editable_package_root": None,
        }
    )
    assert runtime is not None
    # ``provided_namespaces`` 是产品组合根交给遗留社区包解析器的完整本地来源表。
    provided_namespaces = local_package_namespaces(runtime)
    result = prepare_community_packages(
        runtime.graph_copy(),
        working_dir=tmp_path / "community-cache",
        http_client=None,
        available_namespaces=provided_namespaces,
    )

    assert provided_namespaces == {
        str(workspace_root / "workspace_lab"): "community.workspace_lab",
        str(external_root / "external_lab"): "community.external_lab",
    }
    assert result.devices_dirs == []
    assert result.namespaces == provided_namespaces


def test_dependency_declaration_and_lock_bytes_belong_to_input_digest(
    tmp_path: Path,
) -> None:
    """依赖声明或锁的字节变化必须推进稳定工作区输入摘要。

    参数：``tmp_path`` 提供一对合法依赖文件。
    返回：无；断言只增加 YAML 注释也会产生新依赖观察代，同时外部目录内容不变。
    异常：运行时代摘要遗漏依赖文件时测试失败。
    """

    workspace_root, _external_root = _prepare_external_workspace(tmp_path)
    arguments = {
        "workspace": str(workspace_root),
        "graph": "graph.json",
        "devices": None,
        "workflow_editable_package_root": None,
    }
    first = prepare_workspace_registry_runtime(dict(arguments))
    declaration_path = workspace_root / "unilabos.packages.yaml"
    declaration_path.write_text(
        declaration_path.read_text(encoding="utf-8") + "# stable generation marker\n",
        encoding="utf-8",
    )
    second = prepare_workspace_registry_runtime(dict(arguments))

    assert first is not None and second is not None
    assert first.registry_snapshot.fingerprint == second.registry_snapshot.fingerprint
    assert first.dependency_revision != second.dependency_revision


def test_runtime_compiles_each_explicit_package_exactly_once(
    tmp_path: Path,
) -> None:
    """工作区运行时（Workspace Runtime）对主包和每个显式外部包各编译一次。

    参数：``tmp_path`` 提供主工作区和外部包。
    返回：无；断言公开运行时准备 Interface 使用调用者注入的统一编译器，且不会
    为聚合校验重复编译主包或依赖包目录（PackageCatalog）。
    异常：若实现丢弃已编译目录或在验证阶段二次编译，调用次数断言失败。
    """

    # ``workspace_root`` 是主包来源；``external_root`` 是锁定外部包的唯一来源根。
    workspace_root, external_root = _prepare_external_workspace(tmp_path)
    # ``compile_roots`` 记录依赖编排实际观察的来源根，证明每项只编译一次。
    compile_roots: list[Path] = []

    def compile_generation_once(source: WorkspaceSource) -> PackageCatalog:
        """记录完整工作区候选代执行的每次静态目录编译。

        参数：``source`` 是主包或锁定外部包的显式工作区来源。
        返回：统一编译器生成的不可变包目录（PackageCatalog）。
        异常：编译失败时传播原始异常，且不返回部分目录。
        """

        # ``compiled_source_root`` 标识本稳定输入代实际编译的软件包来源。
        compiled_source_root = source.root
        compile_roots.append(compiled_source_root)
        return compile_package_source(source)

    # ``runtime`` 是从主包与全部锁定外部包恰好各编译一次的完整候选代。
    runtime = prepare_workspace_registry_runtime(
        {
            "workspace": str(workspace_root),
            "graph": "graph.json",
            "devices": None,
            "workflow_editable_package_root": None,
        },
        compile_catalog=compile_generation_once,
    )

    assert runtime is not None
    assert compile_roots == [workspace_root.resolve(), external_root.resolve()]


def test_selected_external_package_root_is_finitely_activated(
    tmp_path: Path,
) -> None:
    """只为物理图（Graph）选中的外部包开放作者模块导入根。

    参数：``tmp_path`` 提供主包、选中外部包和未选外部包。
    返回：无；断言选中外部设备驱动可由注册表（Registry）定义导入，未选外部
    包根及其驱动模块均不进入运行激活。
    异常：完整包目录（PackageCatalog）来源丢失或把所有依赖加入 ``sys.path`` 时
    测试失败。
    """

    workspace_root, external_root = _prepare_external_workspace(tmp_path)
    idle_root = tmp_path / "idle_lab"
    _write_package(
        idle_root,
        distribution_name="idle-lab",
        package_name="idle_lab",
        device_ids=("idle_reader",),
    )
    PackageDependencyManager(
        workspace_root,
        compile_catalog=compile_package_source,
    ).add("../idle_lab")
    runtime = prepare_workspace_registry_runtime(
        {
            "workspace": str(workspace_root),
            "graph": "graph.json",
            "devices": None,
            "workflow_editable_package_root": None,
        }
    )
    assert runtime is not None
    original_sys_path = list(sys.path)

    class RegistryStub:
        """提供完整包目录代发布所需的最小注册表映射。

        该替身不扫描源码，也不提供第二套包目录（PackageCatalog）解析。
        """

        def __init__(self) -> None:
            """建立相互独立的空设备与资源注册表映射。

            参数：无。
            返回：无；两个映射等待完整注册表快照（Registry Snapshot）发布。
            异常：无。
            """

            self.device_type_registry: dict[str, object] = {}
            self.resource_type_registry: dict[str, object] = {}

    try:
        runtime.publish(RegistryStub())
        runtime.activate_import_path()

        assert sys.path[0] == str(workspace_root.resolve())
        assert str(external_root.resolve()) in sys.path
        assert str(idle_root.resolve()) not in sys.path
        assert "external_lab.definitions" not in sys.modules
        assert "idle_lab.definitions" not in sys.modules

        # ``selected_driver_entry`` 来自完整目录，但导入只在选中设备真正激活时发生。
        selected_driver_entry = runtime.registry_snapshot.resolve(
            "device",
            "community.external_lab.reader",
        ).details["registry_entry"]
        module_name = selected_driver_entry["class"]["module"].split(":", 1)[0]
        __import__(module_name)

        assert module_name == "external_lab.definitions"
        assert "external_lab.definitions" in sys.modules
        assert "idle_lab.definitions" not in sys.modules
    finally:
        sys.path[:] = original_sys_path
        sys.modules.pop("external_lab.definitions", None)
        sys.modules.pop("idle_lab.definitions", None)


def test_complete_generation_exposes_external_shapes_and_read_only_workflows(
    tmp_path: Path,
) -> None:
    """主包与外部包的资源外形、工作流源码和资产必须来自同一完整候选代。

    参数：``tmp_path`` 提供主工作区和含外形及工作流的显式外部包。
    返回：无；断言外部资源外形进入聚合投影并保留模板 FQID 精确归属，外部
    工作流（Workflow）只存在于注册表快照（Registry Snapshot）查询，不进入
    主包可编辑源码计划，也不产生工作流任务（WorkflowTask）。
    异常：若实现只编译主包外形、遗漏外部资产或授权编辑外部源码则测试失败。
    """

    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external_lab"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader",),
    )
    _write_external_workflow_and_shape(external_root)
    workspace_root.joinpath("graph.json").write_text(
        json.dumps({"nodes": []}),
        encoding="utf-8",
    )
    PackageDependencyManager(
        workspace_root,
        compile_catalog=compile_package_source,
    ).add("../external_lab")

    runtime = prepare_workspace_registry_runtime(
        {
            "workspace": str(workspace_root),
            "graph": "graph.json",
            "devices": None,
            "workflow_editable_package_root": None,
        }
    )

    assert runtime is not None
    assert [shape["id"] for shape in runtime.material_shapes] == [
        "external-shaped-plate"
    ]
    # ``material_shapes_by_template`` 保留外部资源模板到同代编译外形的精确归属，
    # 后续模板同步不得再按 category 或外形 ID 猜测拥有者。
    assert runtime.material_shapes_by_template == {
        "community.external_lab.shaped_plate": {
            "schema_version": "unilab.shape/v1",
            "id": "external-shaped-plate",
            "bundle": "external-lab",
            "displayName": "外部孔板",
            "categories": ["container"],
            "categoryTokens": [],
            "priority": 0,
            "envelope": [127.0, 85.0, 15.0],
            "units": "mm",
            "shadow": "box",
            "sort": "center",
            "parts": [
                {
                    "type": "box",
                    "from": [0, 0, 0],
                    "to": [127, 85, 15],
                }
            ],
        }
    }
    assert tuple(item.fqid for item in runtime.registry_snapshot.workflows) == (
        "community.external_lab.inspect_external",
    )
    assert runtime.workflow_source_plan.registrations == ()
    assert not hasattr(runtime, "workflow_task")
