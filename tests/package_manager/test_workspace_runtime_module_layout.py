"""工作区运行时（Workspace Runtime）分层 Module 的规范依赖合同。"""

from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from tests.package_manager.test_registry_snapshot_activation import (
    _compile_snapshot,
    _write_registry_workspace,
)


def test_workspace_runtime_exposes_complete_public_interface() -> None:
    """新 Module 集中公开发现、监视、代际、激活和生命周期 Interface。

    参数：无。
    返回：无；断言调用者只需导入一个工作区运行时（Workspace Runtime）Module。
    异常：新分层入口缺失或遗漏既有公开能力时测试失败。
    """

    # ``runtime_module`` 是工作区运行时（Workspace Runtime）唯一公开 Module 身份。
    runtime_module = importlib.import_module(
        "unilabos.package_manager.workspace_runtime"
    )
    # ``expected_members`` 是调用者不应再从历史平铺文件拼装的稳定 Interface。
    expected_members = {
        "StableWorkspaceFileMonitor",
        "StableWorkspaceGenerationMonitor",
        "WorkspaceGenerationPublisher",
        "WorkspaceInputGeneration",
        "WorkspacePackageRuntime",
        "WorkspaceProductLifecycle",
        "WorkspaceRefreshCoordinator",
        "WorkspaceRefreshResult",
        "WorkspaceRegistryRuntime",
        "WorkspaceRuntimeStatus",
        "WorkspaceSource",
        "WorkspaceStartupPlan",
        "compile_package_source",
        "compile_workspace_startup",
        "prepare_workspace_registry_runtime",
        "prepare_workspace_startup",
        "publish_registry_snapshot",
    }

    assert expected_members <= set(runtime_module.__all__)
    assert all(hasattr(runtime_module, member) for member in expected_members)


def test_flat_workspace_runtime_wrapper_modules_do_not_exist() -> None:
    """本次尚未发布的平铺模块必须直接删除，不能保留兼容包装。

    参数：无。
    返回：无；断言工作区运行时（Workspace Runtime）只有分层目录和根门面。
    异常：任一未发布平铺模块仍存在时测试失败。
    """

    # ``package_manager_root`` 是尚未发布分层重构的源码边界。
    package_manager_root = (
        Path(__file__).resolve().parents[2] / "unilabos" / "package_manager"
    )
    # ``flat_wrapper_names`` 是本轮必须物理删除的工作区运行时平铺包装文件。
    flat_wrapper_names = {
        "compiler.py",
        "product_lifecycle.py",
        "refresh_coordinator.py",
        "runtime_activation.py",
        "runtime_diff.py",
        "workspace_file_monitor.py",
        "workspace_startup.py",
    }

    assert {path.name for path in package_manager_root.glob("*.py")}.isdisjoint(
        flat_wrapper_names
    )


def test_registry_snapshot_publication_is_owned_by_workspace_runtime(
    tmp_path: Path,
) -> None:
    """工作区运行时（Workspace Runtime）独占实时注册表快照发布职责。

    参数：``tmp_path`` 提供含设备和资源定义的隔离工作区。
    返回：无；断言新 Interface 保留内置定义，纯快照不携带实时发布方法。
    异常：运行时发布职责缺失、快照保留越层桥或发生部分发布时测试失败。
    """

    from unilabos.package_manager.workspace_runtime import publish_registry_snapshot

    # ``source`` 是一次性编译的包目录（PackageCatalog）授权来源。
    source = _write_registry_workspace(
        tmp_path / "workspace",
        distribution="runtime-publish-lab",
        import_package="runtime_publish_lab",
        device_ids=("reactor",),
        resource_ids=("plate",),
    )
    # ``snapshot`` 是运行时发布入口消费的不可变注册表快照（Registry Snapshot）。
    snapshot = _compile_snapshot(source)
    # ``registry`` 是观察完整候选代原子发布结果的隔离注册表（Registry）。
    direct_registry = SimpleNamespace(
        device_type_registry={"host_node": {"source": "builtin"}},
        resource_type_registry={"builtin_plate": {"source": "builtin"}},
    )

    publish_registry_snapshot(snapshot, direct_registry)

    assert not hasattr(snapshot, "publish")
    assert direct_registry.device_type_registry["host_node"] == {"source": "builtin"}
    assert direct_registry.resource_type_registry["builtin_plate"] == {
        "source": "builtin"
    }
    assert "community.runtime_publish_lab.reactor" in (
        direct_registry.device_type_registry
    )
    assert "community.runtime_publish_lab.plate" in (
        direct_registry.resource_type_registry
    )


def test_product_callers_depend_directly_on_workspace_runtime_module() -> None:
    """产品调用者依赖工作区运行时（Workspace Runtime），不穿过已删除平铺路径。

    参数：无。
    返回：无；断言根门面和产品组合根采用新 Module 路径。
    异常：内部调用者重新引用已删除平铺路径时测试失败。
    """

    # ``repository_root`` 是解析 OS 产品调用者导入方向的源码根。
    repository_root = Path(__file__).resolve().parents[2]
    # ``expected_imports`` 固定两个产品组合入口应采用的新分层 Module 身份。
    expected_imports = {
        "unilabos/package_manager/__init__.py": {
            "unilabos.package_manager.workspace_runtime",
        },
        "unilabos/app/workspace_package_bootstrap.py": {
            "unilabos.package_manager.workspace_runtime",
        },
    }
    # ``deleted_flat_modules`` 是本次迁移后禁止重新引入的平铺路径。
    deleted_flat_modules = {
        "unilabos.package_manager.compiler",
        "unilabos.package_manager.product_lifecycle",
        "unilabos.package_manager.refresh_coordinator",
        "unilabos.package_manager.runtime_activation",
        "unilabos.package_manager.runtime_diff",
        "unilabos.package_manager.workspace_file_monitor",
        "unilabos.package_manager.workspace_startup",
    }

    for relative_path, required_modules in expected_imports.items():
        # ``source_file`` 是一个必须改用新 Module 的实际产品调用者。
        source_file = repository_root / relative_path
        # ``syntax_tree`` 只观察 import 依赖，不执行产品启动或作者模块。
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        # ``package_name`` 是相对导入解析所需的调用者 Python 包身份。
        package_name = ".".join(source_file.parent.relative_to(repository_root).parts)
        # ``imported_modules`` 保存当前调用者全部直接模块依赖绝对身份。
        imported_modules: set[str] = set()
        for node in ast.walk(syntax_tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported_name = node.module or ""
            if node.level:
                imported_name = importlib.util.resolve_name(
                    "." * node.level + imported_name,
                    package_name,
                )
            imported_modules.add(imported_name)
        assert required_modules <= imported_modules
        assert imported_modules.isdisjoint(deleted_flat_modules)


def test_workspace_runtime_dependency_direction_has_no_deleted_flat_bridge() -> None:
    """新运行时不依赖平铺模块，纯快照也不反向依赖实时发布层。

    参数：无。
    返回：无；断言新 Module 没有反向旧实现，且包目录（PackageCatalog）完全
    不拥有实时发布职责。
    异常：出现越层 import 或任一运行时桥时测试失败。
    """

    # ``package_manager_root`` 是分层 Module 与纯快照所在的共同源码根。
    package_manager_root = (
        Path(__file__).resolve().parents[2] / "unilabos" / "package_manager"
    )
    # ``deleted_flat_module_names`` 是工作区运行时（Workspace Runtime）禁止依赖的路径。
    deleted_flat_module_names = {
        "unilabos.package_manager.compiler",
        "unilabos.package_manager.product_lifecycle",
        "unilabos.package_manager.refresh_coordinator",
        "unilabos.package_manager.runtime_activation",
        "unilabos.package_manager.runtime_diff",
        "unilabos.package_manager.workspace_file_monitor",
        "unilabos.package_manager.workspace_startup",
    }
    # ``violations`` 保存新 Module 对已删除平铺路径的精确依赖位置。
    violations: list[str] = []
    runtime_root = package_manager_root / "workspace_runtime"
    for source_file in sorted(runtime_root.rglob("*.py")):
        # ``module_parts`` 与 ``package_name`` 共同解析文件内相对 import。
        module_parts = source_file.relative_to(runtime_root).with_suffix("").parts
        package_name = "unilabos.package_manager.workspace_runtime"
        if module_parts[-1] != "__init__":
            package_name += "." + ".".join(module_parts[:-1])
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(syntax_tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported_name = node.module or ""
            if node.level:
                imported_name = importlib.util.resolve_name(
                    "." * node.level + imported_name,
                    package_name,
                )
            if imported_name in deleted_flat_module_names:
                violations.append(
                    f"{source_file.relative_to(runtime_root)}:{node.lineno}:"
                    f"{imported_name}"
                )
    assert violations == []

    # ``snapshot_tree`` 用于证明纯快照完全不依赖实时发布 Module。
    snapshot_file = package_manager_root / "package_catalog" / "registry_snapshot.py"
    snapshot_tree = ast.parse(snapshot_file.read_text(encoding="utf-8"))
    # ``runtime_imports`` 保存包目录快照内所有指向运行时层的越层 import。
    runtime_imports = [
        node.module
        for node in ast.walk(snapshot_tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and "workspace_runtime" in node.module
    ]

    assert runtime_imports == []
