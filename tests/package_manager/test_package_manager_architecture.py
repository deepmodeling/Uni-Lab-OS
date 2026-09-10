"""包管理（Package Manager）最终分层架构守卫。"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_MANAGER_ROOT = REPOSITORY_ROOT / "unilabos" / "package_manager"


def _module_imports(source_file: Path) -> tuple[tuple[int, str], ...]:
    """解析一个包管理源码文件的全部绝对导入身份。

    参数：``source_file`` 是位于 ``package_manager`` 树内的 Python 源文件。
    返回：按 AST 顺序保存的 ``(行号, 绝对模块身份)`` 元组；函数内导入同样包含。
    异常：路径越过包管理根或源码语法无效时传播 ``ValueError``/``SyntaxError``。
    """

    # ``relative_module`` 是源码相对包管理根的稳定 Python 模块路径。
    relative_module = source_file.relative_to(PACKAGE_MANAGER_ROOT).with_suffix("")
    module_parts = list(relative_module.parts)
    if module_parts[-1] == "__init__":
        module_parts.pop()
        current_package = ".".join(("unilabos", "package_manager", *module_parts))
    else:
        current_package = ".".join(("unilabos", "package_manager", *module_parts[:-1]))
    # ``syntax_tree`` 只读取静态依赖，不执行被检查模块或作者代码。
    syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(syntax_tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        declared_module = node.module or ""
        if node.level:
            absolute_module = importlib.util.resolve_name(
                "." * node.level + declared_module,
                current_package,
            )
        else:
            absolute_module = declared_module
        if declared_module:
            imports.append((node.lineno, absolute_module))
        else:
            imports.extend(
                (node.lineno, f"{absolute_module}.{alias.name}") for alias in node.names
            )
    return tuple(imports)


def _layer_dependency_violations(
    module_directory: str,
    forbidden_fragments: tuple[str, ...],
) -> list[str]:
    """查找一个正式 Module 全树的禁止依赖。

    参数：``module_directory`` 是包管理根下的目录名；``forbidden_fragments`` 是
    任一导入身份不得包含的关闭集合。
    返回：按文件和行号稳定记录的违规列表。
    异常：源码读取或解析错误直接传播，不能以跳过文件掩盖依赖方向破坏。
    """

    # ``module_root`` 是本次静态依赖检查的完整源码边界。
    module_root = PACKAGE_MANAGER_ROOT / module_directory
    # ``violations`` 保存人类和 AI 可直接定位的路径、行号与导入身份。
    violations: list[str] = []
    for source_file in sorted(module_root.rglob("*.py")):
        for line_number, imported_module in _module_imports(source_file):
            if any(fragment in imported_module for fragment in forbidden_fragments):
                violations.append(
                    f"{source_file.relative_to(PACKAGE_MANAGER_ROOT)}:"
                    f"{line_number}:{imported_module}"
                )
    return violations


def test_package_manager_root_contains_only_public_entry_modules() -> None:
    """根目录只保留惰性正式门面、CLI Adapter 和目录索引。

    参数：无。
    返回：无；断言所有实现都归入有明确职责的分层 Module。
    异常：旧平铺实现、兼容包装或新的根级实现出现时测试失败。
    """

    # ``root_python_files`` 是包管理根层仍允许暴露的完整 Python 文件集合。
    root_python_files = {path.name for path in PACKAGE_MANAGER_ROOT.glob("*.py")}
    # ``layered_implementation_files`` 是本阶段迁移后必须存在的职责所有者。
    layered_implementation_files = {
        "package_catalog/project_metadata.py",
        "package_catalog/material_models.py",
        "package_catalog/material_shapes.py",
        "package_distribution/errors.py",
        "package_distribution/build.py",
        "package_distribution/inspection.py",
        "package_distribution/archive.py",
        "package_distribution/registry_discovery.py",
        "package_distribution/legacy_projection.py",
        "workspace_runtime/package_source.py",
    }

    assert root_python_files == {"__init__.py", "cli.py"}
    assert {
        str(path.relative_to(PACKAGE_MANAGER_ROOT))
        for path in PACKAGE_MANAGER_ROOT.rglob("*.py")
    } >= layered_implementation_files


def test_each_package_manager_module_has_a_human_and_agent_index() -> None:
    """每个正式 Module 都提供人类与 AI 共用的职责和修改路由索引。

    参数：无。
    返回：无；断言六个层级目录都记录公开 Interface、依赖、不变量和修改路由。
    异常：索引缺失或没有关键导航章节时测试失败。
    """

    # ``module_roots`` 是当前包管理架构全部正式人类导航层级。
    module_roots = (
        PACKAGE_MANAGER_ROOT,
        PACKAGE_MANAGER_ROOT / "package_catalog",
        PACKAGE_MANAGER_ROOT / "package_distribution",
        PACKAGE_MANAGER_ROOT / "package_distribution" / "adapters",
        PACKAGE_MANAGER_ROOT / "workspace_runtime",
        PACKAGE_MANAGER_ROOT / "driver_runtime",
    )
    for module_root in module_roots:
        # ``readme`` 是该 Module 职责和入口的唯一邻近索引。
        readme = module_root / "README.md"
        content = readme.read_text(encoding="utf-8")
        assert "## 职责" in content
        assert "## 公开 Interface" in content
        assert "## 依赖方向" in content
        assert "## 不变量" in content
        assert "## 修改路由" in content

    # 根索引必须把当前四个正式子 Module 与跨语言扩展路线连在一起。
    root_content = PACKAGE_MANAGER_ROOT.joinpath("README.md").read_text(
        encoding="utf-8"
    )
    assert all(
        module_name in root_content
        for module_name in (
            "package_catalog",
            "package_distribution",
            "workspace_runtime",
            "driver_runtime",
            "C#",
            "Rust",
        )
    )


def test_package_manager_layers_follow_the_canonical_dependency_direction() -> None:
    """四个正式 Module 的完整 AST 依赖只能沿接受方向流动。

    参数：无。
    返回：无；断言包目录、包分发、工作区和驱动层没有反向或产品运行时依赖。
    异常：顶层或函数内出现任一禁止导入时测试失败并给出精确位置。
    """

    # ``layer_rules`` 是四个正式 Module 的关闭式禁止依赖矩阵。
    layer_rules = {
        "package_catalog": (
            "unilabos.package_manager.package_distribution",
            "unilabos.package_manager.workspace_runtime",
            "unilabos.package_manager.driver_runtime",
        ),
        "package_distribution": (
            "unilabos.package_manager.workspace_runtime",
            "unilabos.package_manager.driver_runtime",
        ),
        "workspace_runtime": (
            "unilabos.package_manager.driver_runtime",
            "unilabos.ros",
            "unilabos.app.scheduler",
        ),
        "driver_runtime": (
            "unilabos.package_manager.package_distribution",
            "unilabos.package_manager.workspace_runtime",
            "unilabos.ros",
            "scheduler",
            "inventory",
            "backend",
        ),
    }
    # ``violations`` 聚合全部层级，避免一次只修复首个越层导入。
    violations = [
        violation
        for module_directory, forbidden_fragments in layer_rules.items()
        for violation in _layer_dependency_violations(
            module_directory,
            forbidden_fragments,
        )
    ]

    assert violations == []


def test_cold_imports_do_not_execute_author_package_code(tmp_path: Path) -> None:
    """根门面与四个子 Module 的冷导入不执行作者软件包代码。

    参数：``tmp_path`` 提供位于 ``PYTHONPATH`` 但绝不应被加载的作者模块。
    返回：无；逐个隔离进程断言作者模块和副作用标记均不存在。
    异常：任一冷导入失败、扫描环境或执行作者代码时测试失败。
    """

    # ``author_module`` 模拟导入即产生全局副作用的第三方驱动包。
    author_module = tmp_path / "phase5_author_package.py"
    author_module.write_text(
        "import builtins\nbuiltins._phase5_author_package_executed = True\n",
        encoding="utf-8",
    )
    # ``cold_modules`` 覆盖正式根门面与四个子 Module 的公开导入入口。
    cold_modules = (
        "unilabos.package_manager",
        "unilabos.package_manager.package_catalog",
        "unilabos.package_manager.package_distribution",
        "unilabos.package_manager.workspace_runtime",
        "unilabos.package_manager.driver_runtime",
    )
    # ``environment`` 让测试作者模块可被发现，从而证明冷导入没有主动扫描加载。
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (
            str(tmp_path),
            str(REPOSITORY_ROOT),
            environment.get("PYTHONPATH", ""),
        )
        if item
    )
    for module_name in cold_modules:
        # ``probe`` 在全新解释器中只导入一个公开 Module 并报告作者代码状态。
        probe = f"""
import builtins
import importlib
import json
import sys

importlib.import_module({module_name!r})
print(json.dumps({{
    "author_module": "phase5_author_package" in sys.modules,
    "author_side_effect": hasattr(builtins, "_phase5_author_package_executed"),
}}))
"""
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip()) == {
            "author_module": False,
            "author_side_effect": False,
        }
