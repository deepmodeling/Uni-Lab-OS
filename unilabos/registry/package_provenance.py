"""注册表条目来自哪个驱动包：Host 上报时给每个条目附 ``package`` / ``package_version``。

调度权威的规则 A（驱动包版本号升高的变化自动接受）拿它与已接受内容的包版本比较。按条目的
源文件（注册表条目的 ``file_path``；工作流模板按已导入模块的 ``__file__``）依次查：

1. Host 的驱动包台账（``<working_dir>/driver_packages.json``）：文件在某个已登记包的源码根
   或包目录下 → 台账里的包名与版本；
2. unilabos 自身（内置设备、YAML 资源、host_node）→ ``unilabos`` 与 ``unilabos.__version__``；
3. 向上找最近的 ``pyproject.toml``（``--devices`` 挂载的源码树、社区包）→ ``[project]`` 的
   name / version（动态版本取不到时为空）；
4. 都没有 → 空，规则 A 对该条目不适用。

只读文件，不 import 任何驱动模块。
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from unilabos.utils import logger

Package = Tuple[str, str]
_NO_PACKAGE: Package = ("", "")


def _resolved(path: Any) -> Optional[Path]:
    try:
        return Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


class PackageResolver:
    """按源文件路径解析所属驱动包（结果按目录缓存）。"""

    def __init__(self, working_dir: Optional[str] = None) -> None:
        import unilabos

        self._unilabos_root = Path(unilabos.__file__).resolve().parent
        self._unilabos: Package = ("unilabos", str(getattr(unilabos, "__version__", "") or ""))
        self._ledger: List[Tuple[Path, Package]] = []
        self._ledger_tops: Dict[str, Package] = {}
        if working_dir:
            self._load_ledger(working_dir)
        self._by_dir: Dict[Path, Package] = {}

    def _load_ledger(self, working_dir: str) -> None:
        from unilabos.server.services.driver_packages import DriverPackageError, load_ledger

        try:
            records = load_ledger(working_dir)
        except DriverPackageError as exc:
            logger.warning(f"[Registry] 读取驱动包台账失败，条目不带台账里的包版本: {exc}")
            return
        for record in records.values():
            package = (str(record.name or ""), str(record.version or ""))
            roots = [record.package_root, *record.package_dirs]
            for root in roots:
                path = _resolved(root) if root else None
                if path is not None:
                    self._ledger.append((path, package))
            for item in record.package_dirs:
                self._ledger_tops.setdefault(Path(str(item)).name, package)
        # 更具体（更深）的根优先：包目录嵌在别的包的源码根里时不被外层吞掉
        self._ledger.sort(key=lambda item: len(item[0].parts), reverse=True)

    def for_file(self, file_path: Any) -> Package:
        path = _resolved(file_path) if file_path else None
        if path is None:
            return _NO_PACKAGE
        for root, package in self._ledger:
            if path.is_relative_to(root):
                return package
        if path.is_relative_to(self._unilabos_root):
            return self._unilabos
        return self._from_pyproject(path.parent)

    def for_module(self, module: str) -> Package:
        """已导入模块按 ``__file__`` 解析；没导入的只按顶层包名对照 unilabos 与台账。"""

        module_name = str(module or "").partition(":")[0].strip()
        loaded = sys.modules.get(module_name)
        file_path = getattr(loaded, "__file__", None) if loaded is not None else None
        if file_path:
            return self.for_file(file_path)
        top = module_name.partition(".")[0]
        if top == "unilabos":
            return self._unilabos
        return self._ledger_tops.get(top, _NO_PACKAGE)

    def for_entry(self, entry: Mapping[str, Any]) -> Package:
        file_path = entry.get("file_path")
        if file_path:
            return self.for_file(file_path)
        klass = entry.get("class")
        module = klass.get("module") if isinstance(klass, Mapping) else None
        return self.for_module(str(module or "")) if module else _NO_PACKAGE

    def _from_pyproject(self, directory: Path) -> Package:
        visited: List[Path] = []
        current: Optional[Path] = directory
        found = _NO_PACKAGE
        while current is not None:
            if current in self._by_dir:
                found = self._by_dir[current]
                break
            visited.append(current)
            pyproject = current / "pyproject.toml"
            if pyproject.is_file():
                found = _read_project(pyproject)
                break
            parent = current.parent
            current = parent if parent != current else None
        for item in visited:
            self._by_dir[item] = found
        return found


def _read_project(pyproject: Path) -> Package:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return _NO_PACKAGE
    project = data.get("project") if isinstance(data, dict) else None
    if not isinstance(project, dict):
        return _NO_PACKAGE
    name = str(project.get("name") or "").strip()
    return (name, str(project.get("version") or "").strip()) if name else _NO_PACKAGE


def registry_entry_packages(
    registry: Any,
    workflow_templates: Iterable[Mapping[str, Any]] = (),
    *,
    working_dir: Optional[str] = None,
) -> Dict[str, Package]:
    """设备 / 资源 / 工作流模板条目 id → (驱动包名, 包版本)；解析不出的条目不在结果里。"""

    if working_dir is None:
        from unilabos.config.config import BasicConfig

        working_dir = str(BasicConfig.working_dir or "") or None
    resolver = PackageResolver(working_dir)
    result: Dict[str, Package] = {}
    for table in (
        getattr(registry, "device_type_registry", {}) or {},
        getattr(registry, "resource_type_registry", {}) or {},
    ):
        for entry_id, entry in table.items():
            if isinstance(entry, Mapping):
                package = resolver.for_entry(entry)
                if package[0]:
                    result[str(entry_id)] = package
    for template in workflow_templates:
        module = str(template.get("module") or template.get("id") or "")
        package = resolver.for_module(module)
        if package[0]:
            result[str(template.get("id") or "")] = package
    return result


__all__ = ["PackageResolver", "registry_entry_packages"]
