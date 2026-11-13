"""驱动包目录布局：导入根、包根与 AST 扫描入口（运行时挂载、package inspect、package install 共用）。

同一个驱动包无论经 ``--devices``、``unilab package inspect`` 还是 ``unilab package install``
进入系统，都必须推导出同一套模块路径与设备 id；这里集中这些规则，避免各入口各写一份。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

PathLike = Union[str, Path]


def find_import_root(directory: PathLike) -> Path:
    """返回 *directory* 所在顶层包的父目录，即导入它时应在 ``sys.path`` 上的目录。

    - 常规包（含 ``__init__.py``）沿父目录上溯，直到第一个不含 ``__init__.py`` 的目录：
      挂载 ``<repo>/pkg/devices/family`` 时导入根是 ``<repo>``，模块路径为
      ``pkg.devices.family.<模块>``，与 ``import pkg`` 一致；
    - 本身不是常规包的目录（命名空间目录）按顶层包处理，返回其父目录（``--devices`` 的既有约定）。
    """

    path = Path(directory).resolve()
    if not (path / "__init__.py").is_file():
        return path.parent
    current = path
    while (current / "__init__.py").is_file() and current.parent != current:
        current = current.parent
    return current


def find_project_root(start: PathLike, max_up: int = 6) -> Optional[Path]:
    """从 *start* 起（含自身）向上最多 *max_up* 层，定位含 ``pyproject.toml`` 的包根。"""

    current = Path(start).resolve()
    for _ in range(max_up + 1):
        if (current / "pyproject.toml").is_file():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def scan_package_dirs(
    package_dirs: Iterable[PathLike],
    *,
    executor: Optional[ThreadPoolExecutor] = None,
) -> Dict[str, Any]:
    """AST 扫描一组驱动包目录（不 import），模块路径按各自的导入根推导。

    返回与 ``scan_directory`` 相同形状的合并结果：``devices`` / ``resources`` / ``workflows`` /
    ``_scan_errors``；同一 id 出现在两个目录时抛 ``ValueError``。
    """

    from unilabos.registry.ast_registry_scanner import scan_directory

    merged: Dict[str, Any] = {"devices": {}, "resources": {}, "workflows": {}, "_scan_errors": []}
    own_executor = executor is None
    pool = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="PackageScan")
    try:
        for item in package_dirs:
            directory = Path(item).resolve()
            if not directory.is_dir():
                continue
            result = scan_directory(directory, python_path=find_import_root(directory), executor=pool)
            for kind in ("devices", "resources"):
                for key, meta in result.get(kind, {}).items():
                    if key in merged[kind]:
                        existing = merged[kind][key].get("file_path", "?")
                        raise ValueError(
                            f"@{kind[:-1]} id 重复: '{key}' 同时出现在 {existing} 和 {meta.get('file_path', '?')}"
                        )
                    merged[kind][key] = meta
            merged["workflows"].update(result.get("workflows", {}))
            merged["_scan_errors"].extend(result.get("_scan_errors", []))
    finally:
        if own_executor:
            pool.shutdown(wait=True)
    return merged


def format_scan_errors(errors: List[Dict[str, str]]) -> str:
    return "；".join(f"{item.get('file')}: {item.get('error')}" for item in errors)


__all__ = [
    "find_import_root",
    "find_project_root",
    "format_scan_errors",
    "scan_package_dirs",
]
