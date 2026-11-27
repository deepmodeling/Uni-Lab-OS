"""组合图重写模块依赖方向的回归测试。"""

from __future__ import annotations

import ast
from pathlib import Path


def _imported_modules(path: Path) -> set[str]:
    """返回 Python 文件的显式导入模块集合。"""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            result.add(node.module)
        elif isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
    return result


def test_composite_expanders_do_not_import_each_other() -> None:
    """两条展开路径只能共同依赖中立图重写模块。"""

    workflow_dir = Path(__file__).parents[2] / "unilabos" / "workflow"
    expansion_imports = _imported_modules(workflow_dir / "composite_expansion.py")
    invocation_imports = _imported_modules(workflow_dir / "composite_invocation.py")

    assert "unilabos.workflow.composite_invocation" not in expansion_imports
    assert "unilabos.workflow.composite_expansion" not in invocation_imports
