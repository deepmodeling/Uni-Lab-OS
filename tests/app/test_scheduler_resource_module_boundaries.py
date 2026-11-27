"""守护工站调度、库存和工作流存储之间的公开接缝。"""

from __future__ import annotations

import ast
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SCHEDULER_RESOURCE_MODULES = (
    "unilabos/app/scheduler/device_target.py",
    "unilabos/app/scheduler/site_target.py",
    "unilabos/app/scheduler/transfer_resource_set.py",
    "unilabos/workflow/material_transfer_settlement.py",
)


def test_scheduler_resource_modules_do_not_reach_inventory_store_or_sql() -> None:
    """调度资源模块只能调用工站资源库存接口。

    参数：无。返回：无；逐文件解析 Python AST，断言不存在 ``.store`` 穿透和
    SQL 语句常量。异常：文件缺失或语法错误原样传播；任何越层访问使断言失败，
    防止设备、库位（Site）或转运规则重新绑定 SQLite 表结构。
    """

    violations: list[str] = []
    for relative_path in _SCHEDULER_RESOURCE_MODULES:
        # ``module_path`` 是本次边界审核的仓库内稳定源文件位置。
        module_path = _REPOSITORY_ROOT / relative_path
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "store":
                violations.append(f"{relative_path}:{node.lineno}:store")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                statement = node.value.lstrip().upper()
                if statement.startswith(("SELECT ", "INSERT ", "UPDATE ", "DELETE ")):
                    violations.append(f"{relative_path}:{node.lineno}:sql")
    assert violations == []


def test_workflow_persistence_helpers_do_not_use_store_private_members() -> None:
    """工作流持久化扩展不得调用 ``WorkflowStore`` 私有实现。

    参数：无。返回：无；扫描整个工作流包，断言事件写入只走公开模块，扩展
    查询只走 ``read``，写入只走 ``transaction``。异常：文件读取或语法错误
    原样传播；发现对存储私有连接、锁或事件方法的访问时使断言失败。
    """

    violations: list[str] = []
    workflow_root = _REPOSITORY_ROOT / "unilabos/workflow"
    for module_path in sorted(workflow_root.glob("*.py")):
        if module_path.name == "store.py":
            continue
        relative_path = module_path.relative_to(_REPOSITORY_ROOT).as_posix()
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if _uses_workflow_store_private_member(node):
                violations.append(f"{relative_path}:{node.lineno}:{node.attr}")
    assert violations == []


def _uses_workflow_store_private_member(node: ast.AST) -> bool:
    """识别工作流模块对 ``WorkflowStore`` 私有布局的越层访问。

    参数：``node`` 是 Python AST 节点。返回：调用 ``WorkflowStore`` 私有事件
    写入口，或读取 ``self._store`` 私有锁/连接时为真。异常：无；未知 AST 形状
    一律返回假，由 Python 解析器负责语法有效性。
    """

    if not isinstance(node, ast.Attribute):
        return False
    if node.attr in {"_append_event", "_append_runtime_event"}:
        return isinstance(node.value, ast.Name) and node.value.id == "WorkflowStore"
    if node.attr not in {"_conn", "_lock"}:
        return False
    return isinstance(node.value, ast.Attribute) and node.value.attr == "_store"
