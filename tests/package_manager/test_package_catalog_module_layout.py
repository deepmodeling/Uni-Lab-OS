"""包目录（PackageCatalog）分层 Module 的规范依赖合同。"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from tests.package_manager.test_package_catalog_compiler import _write_package


def test_new_package_catalog_interface_compiles_the_same_catalog(
    tmp_path: Path,
) -> None:
    """根门面与分层编译 Interface 产生相同包目录（PackageCatalog）。

    参数：``tmp_path`` 提供唯一隔离软件包来源。
    返回：无；断言正式根门面与底层编译 Interface 产生相同规范字节。
    异常：新路径、公开编译入口或输入反转接缝缺失时测试失败。
    """

    from unilabos.package_manager import (
        WorkspaceSource,
    )
    from unilabos.package_manager import (
        compile_package_source as compile_facade_package_source,
    )
    from unilabos.package_manager.package_catalog import (
        PackageCatalog,
    )
    from unilabos.package_manager.package_catalog import (
        compile_package_source as compile_layered_package_source,
    )
    from unilabos.package_manager.workspace_runtime import compile_workspace_startup

    # ``workspace_root`` 是两个正式编译入口共同观察的唯一可编辑包来源根。
    workspace_root = tmp_path / "workspace"
    _write_package(workspace_root)
    # ``source`` 固定文件读取权威；``startup_plan`` 是根编排层交给纯编译层的输入。
    source = WorkspaceSource(workspace_root)
    startup_plan = compile_workspace_startup(source)

    # ``facade_catalog`` 是根门面编排入口产生的行为基准包目录（PackageCatalog）。
    facade_catalog = compile_facade_package_source(
        source,
        startup_plan=startup_plan,
    )
    # ``layered_catalog`` 是新 Module 对同一冻结输入产生的包目录（PackageCatalog）。
    layered_catalog = compile_layered_package_source(
        source,
        startup_plan=startup_plan,
    )

    assert isinstance(layered_catalog, PackageCatalog)
    assert type(layered_catalog) is type(facade_catalog)
    assert layered_catalog.to_canonical_bytes() == facade_catalog.to_canonical_bytes()


def test_flat_package_catalog_wrapper_modules_do_not_exist() -> None:
    """本次尚未发布的包目录（PackageCatalog）平铺包装必须直接删除。

    参数：无。
    返回：无；断言根目录只保留正式门面和有独立职责的产品模块。
    异常：任一未发布平铺包装仍存在时测试失败。
    """

    # ``package_manager_root`` 是尚未发布分层重构的源码边界。
    package_manager_root = (
        Path(__file__).resolve().parents[2] / "unilabos" / "package_manager"
    )
    # ``flat_wrapper_names`` 是本轮必须物理删除的包目录平铺包装文件。
    flat_wrapper_names = {
        "_registry_catalog.py",
        "_workflow_catalog.py",
        "catalog.py",
        "registry_snapshot.py",
        "sources.py",
    }

    assert {path.name for path in package_manager_root.glob("*.py")}.isdisjoint(
        flat_wrapper_names
    )


def test_facade_compiler_preserves_invalid_source_diagnostic(tmp_path: Path) -> None:
    """正式根门面（Facade）把工作区发现失败归一为既有结构化诊断。

    参数：``tmp_path`` 提供缺少项目清单的隔离工作区来源。
    返回：无；断言正式门面（Facade）仍抛出 ``package_source_invalid``。
    异常：输入反转使高层解析异常泄漏为原始 ``ValueError`` 时测试失败。
    """

    import pytest

    from unilabos.package_manager import (
        PackageCompileError,
        WorkspaceSource,
        compile_package_source,
    )

    # ``invalid_workspace_root`` 是存在但缺少 pyproject.toml 的非法包来源。
    invalid_workspace_root = tmp_path / "invalid-workspace"
    invalid_workspace_root.mkdir()

    # ``caught`` 保存正式根门面（Facade）对非法来源产生的结构化编译诊断。
    with pytest.raises(PackageCompileError) as caught:
        compile_package_source(WorkspaceSource(invalid_workspace_root))

    assert [item.code for item in caught.value.diagnostics] == [
        "package_source_invalid"
    ]


def test_package_catalog_module_has_no_reverse_dependency_on_parent_layers() -> None:
    """包目录（PackageCatalog）Module 不反向依赖根门面或后续生命周期 Module。

    参数：无。
    返回：无；逐个解析新 Module 的 import，并断言只依赖自身或更底层能力。
    异常：出现已删除平铺路径、包分发、工作区运行时或候选驱动运行时依赖时
    测试失败。
    """

    # ``module_root`` 是包目录（PackageCatalog）新 Module 的唯一源码边界。
    module_root = (
        Path(__file__).resolve().parents[2]
        / "unilabos"
        / "package_manager"
        / "package_catalog"
    )
    # ``forbidden_prefixes`` 标识会使低层编译重新依赖高层生命周期的导入方向。
    forbidden_prefixes = (
        "unilabos.package_manager.catalog",
        "unilabos.package_manager.compiler",
        "unilabos.package_manager.registry_snapshot",
        "unilabos.package_manager.sources",
        "unilabos.package_manager._registry_catalog",
        "unilabos.package_manager._workflow_catalog",
        "unilabos.package_manager.workspace_startup",
        "unilabos.package_manager.package_distribution",
        "unilabos.package_manager.workspace_runtime",
        "unilabos.package_manager.driver_runtime",
    )
    # ``violations`` 记录源码路径、行号和越层导入身份，便于直接定位依赖反转。
    violations: list[str] = []
    for source_file in sorted(module_root.rglob("*.py")):
        # ``relative_file`` 是守卫报告使用的 Module 内路径，不绑定工作树绝对位置。
        relative_file = source_file.relative_to(module_root)
        # ``module_parts`` 从源码相对路径恢复 Python Module 身份的组成部分。
        module_parts = list(relative_file.with_suffix("").parts)
        if module_parts[-1] == "__init__":
            module_parts.pop()
        # ``package_name`` 是解析相对 import 所需的当前 Python 包身份。
        package_name = ".".join(
            ["unilabos", "package_manager", "package_catalog", *module_parts[:-1]]
        )
        if relative_file.name == "__init__.py":
            package_name = ".".join(
                ["unilabos", "package_manager", "package_catalog", *module_parts]
            )
        # ``syntax_tree`` 只用于检查 import 依赖，不执行被审查 Module。
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(syntax_tree):
            if isinstance(node, ast.Import):
                # ``imported_names`` 是普通 import 声明的完整绝对 Module 身份集合。
                imported_names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # ``imported_name`` 先保留源码声明，再结合当前包解析为绝对身份。
                imported_name = node.module or ""
                if node.level:
                    imported_name = importlib.util.resolve_name(
                        "." * node.level + imported_name,
                        package_name,
                    )
                imported_names = [imported_name]
            else:
                continue
            for imported_name in imported_names:
                if (
                    imported_name == "unilabos.package_manager"
                    or imported_name.startswith(forbidden_prefixes)
                ):
                    violations.append(f"{relative_file}:{node.lineno}:{imported_name}")

    assert violations == []
