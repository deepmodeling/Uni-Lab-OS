"""工站基类的导入路径：框架代码从 unilabos.workstation 导入，旧路径只做转发。

这里只做静态检查，不导入模块，无 ROS 的套件也能跑。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD_MODULE = "unilabos.devices.workstation.workstation_base"
SHIM = ROOT / "unilabos" / "devices" / "workstation" / "workstation_base.py"
PUBLIC_INIT = ROOT / "unilabos" / "workstation" / "__init__.py"


def _all_names(path: Path) -> list[str]:
    """读出模块里 ``__all__`` 的字面量。"""

    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            return list(ast.literal_eval(node.value))
    raise AssertionError(f"{path.relative_to(ROOT)} 没有 __all__")


def _references_old_module(node: ast.AST) -> bool:
    """判断语法节点是否按模块名引用了旧路径，包括字符串形式的动态导入。"""

    if isinstance(node, ast.ImportFrom):
        return node.module == OLD_MODULE
    if isinstance(node, ast.Import):
        return any(alias.name == OLD_MODULE for alias in node.names)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == OLD_MODULE or node.value.startswith(OLD_MODULE + ":")
    return False


def test_old_path_only_reexports_the_public_api() -> None:
    tree = ast.parse(SHIM.read_text(encoding="utf-8"))
    sources = [node.module for node in tree.body if isinstance(node, ast.ImportFrom)]
    assert sources == ["unilabos.workstation.base"]
    implementations = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert implementations == [], "旧路径只能转发，不能再放实现"
    assert sorted(_all_names(SHIM)) == sorted(_all_names(PUBLIC_INIT))


def test_framework_code_uses_the_public_path() -> None:
    offenders = []
    for path in sorted((ROOT / "unilabos").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if path == SHIM or relative.startswith("unilabos/devices/"):
            continue  # 设备驱动可以暂时沿用旧路径，由转发模块兼容
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if _references_old_module(node):
                offenders.append(f"{relative}:{node.lineno}")
    for path in sorted((ROOT / "unilabos" / "registry").rglob("*.yaml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.lstrip().startswith("#") and OLD_MODULE + ":" in line:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    assert offenders == [], "框架代码要从 unilabos.workstation 导入：\n" + "\n".join(offenders)
