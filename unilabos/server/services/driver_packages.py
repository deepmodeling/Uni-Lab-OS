"""驱动包（设备包）管理：源码树落 unilabos_data、按目录挂载、依赖用 uv 预装。

驱动包 = 含 ``@device`` / ``@resource`` 的 Python 源码树（仓库 / 目录），**不经 pip 安装**，与
``unilab --devices <目录>`` 是同一套机制：

- **来源**：GitHub 仓库地址（``https://github.com/<owner>/<repo>[@ref]``、``git+https://…``）、
  zip / tar.gz 归档地址，或本机目录。远端来源下载后解压到
  ``<working_dir>/driver_packages/<name>/<version>/``（working_dir 即 unilabos_data 目录）；
  本机目录原地登记、不复制；
- **依赖**：读源码树 ``pyproject.toml`` 的 ``[project].dependencies``，用 ``uv pip install``
  （不可用时回退 ``python -m pip``）装进当前解释器——包体本身不装，只装它依赖的第三方库；
- **台账**：``<working_dir>/driver_packages.json`` 记录每个包的来源、版本、sha256、源码根、
  要挂载的包目录（顶层 Python 包，其父目录进 ``sys.path``）、扫描到的设备类与启用状态；
- **挂载**：``enabled_package_dirs()`` 把已启用包的目录并入 Host 的 ``--devices`` 扫描
  （重启后生效）；受管设备进程按台账把同样的目录传给子进程，不需要重启 Host。

安装 / 卸载是后台线程里的长操作，用 operation 记录进度与日志供前端轮询。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from unilabos.utils import logger
from unilabos.server.services.driver_source_changes import get_source_changes, source_roots
from unilabos.server.services import driver_package_journal as journal
from unilabos.server.services.host_mutations import HostMutationBusy, get_host_mutation_gate

LEDGER_FILENAME = "driver_packages.json"
CATALOG_FILENAME = "driver_package_catalog.json"
PACKAGES_DIRNAME = "driver_packages"
OPERATION_HISTORY_LIMIT = 30
DOWNLOAD_TIMEOUT_S = 120
DEPENDENCY_INSTALL_TIMEOUT_S = 900
CATALOG_FETCH_TIMEOUT_S = 8
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024

#: 源码树里不当作驱动包目录的顶层目录名。
_NON_PACKAGE_DIRS = frozenset(
    {"tests", "test", "docs", "doc", "examples", "example", "graph", "graphs", "build", "dist", "scripts", "node_modules"}
)
#: 依赖里跳过的名字：包体自身与 unilabos 本体由部署流程管理。
_SKIP_DEPENDENCIES = frozenset({"unilabos", "uni-lab-os", "unilab"})

_GITHUB_REPO_RE = re.compile(
    r"^(?:git\+)?https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/tree/(?P<tree>[^\s]+?))?/?(?:@(?P<at>[^\s]+))?$"
)
_ARCHIVE_URL_RE = re.compile(r"^https?://\S+\.(zip|tar\.gz|tgz|tar)(\?\S*)?$", re.IGNORECASE)


class DriverPackageError(RuntimeError):
    """驱动包操作的可预期错误（参数非法 / 不存在 / 冲突）。"""


class DriverPackageBusyError(DriverPackageError):
    """同一 Host 已在修改源码、依赖或台账；写请求需等待当前操作结束。"""


@dataclass
class DriverPackageRecord:
    name: str
    spec: str = ""
    version: str = ""
    #: github | archive | local
    source_kind: str = ""
    #: 源码树根目录（含 pyproject.toml）；local 来源即用户给的目录
    package_root: str = ""
    #: 挂载给注册表 / 子进程的包目录（顶层 Python 包），父目录进 sys.path
    package_dirs: List[str] = field(default_factory=list)
    device_ids: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    sha256: str = ""
    enabled: bool = True
    #: 依赖用什么装的：uv | pip | ""（无依赖）
    installer: str = ""
    installed_at_ms: int = 0
    updated_at_ms: int = 0


@dataclass
class DriverPackageOperation:
    operation_id: str
    kind: str  # install | uninstall
    spec: str
    status: str = "running"  # running | succeeded | failed
    package_name: str = ""
    started_at_ms: int = 0
    finished_at_ms: Optional[int] = None
    log: str = ""
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    version: int = 1
    requires_confirmation: bool = False
    confirmation: Optional[Dict[str, Any]] = None
    recovery: Dict[str, Any] = field(default_factory=dict)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def ledger_path(working_dir: str | Path) -> Path:
    return Path(working_dir) / LEDGER_FILENAME


def catalog_path(working_dir: str | Path) -> Path:
    """实验室自维护的可安装目录（可选文件）。"""
    return Path(working_dir) / CATALOG_FILENAME


def packages_root(working_dir: str | Path) -> Path:
    """远端来源解压落盘的根目录：``<working_dir>/driver_packages/<name>/<version>/``。"""
    return Path(working_dir) / PACKAGES_DIRNAME


def _catalog_entries(data: Any) -> List[Dict[str, Any]]:
    """目录 JSON 既接受 ``{"packages": [...]}`` 也接受裸数组。"""
    items = data.get("packages") if isinstance(data, dict) else data
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _catalog_entry(raw: Dict[str, Any], *, source: str, installed: set[str]) -> Optional[Dict[str, Any]]:
    name = str(raw.get("name") or "").strip()
    spec = str(raw.get("spec") or raw.get("source") or raw.get("repo") or "").strip()
    if not name or not spec:
        return None

    def _strings(value: Any) -> List[str]:
        return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []

    return {
        "name": name,
        "spec": spec,
        "version": str(raw.get("version") or ""),
        "description": str(raw.get("description") or ""),
        "homepage": str(raw.get("homepage") or ""),
        "devices": _strings(raw.get("devices")),
        "tags": _strings(raw.get("tags")),
        # 官方索引里的条目默认就是官方包；本地目录默认社区 / 自用，除非显式标 official
        "official": bool(raw.get("official", source == "remote")),
        "source": source,
        "installed": _normalize(name) in installed,
    }


def load_ledger(working_dir: str | Path) -> Dict[str, DriverPackageRecord]:
    path = ledger_path(working_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise DriverPackageError(f"驱动包台账读取失败，拒绝按空台账覆盖：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("packages"), list):
        raise DriverPackageError("驱动包台账格式损坏")
    records: Dict[str, DriverPackageRecord] = {}
    for item in data.get("packages", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        known = {key: item[key] for key in DriverPackageRecord.__dataclass_fields__ if key in item}
        records[_normalize(str(item["name"]))] = DriverPackageRecord(**known)
    return records


def save_ledger(working_dir: str | Path, records: Dict[str, DriverPackageRecord]) -> None:
    path = ledger_path(working_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 2, "packages": [asdict(record) for record in records.values()]}
    journal.atomic_json(path, payload)


def enabled_package_dirs(working_dir: str | Path) -> List[str]:
    """已启用且目录仍存在的驱动包目录（启动期并入 ``--devices``）。"""
    dirs: List[str] = []
    for record in load_ledger(working_dir).values():
        if not record.enabled:
            continue
        for item in record.package_dirs:
            if Path(item).is_dir() and item not in dirs:
                dirs.append(item)
    return dirs


# ── 来源解析 ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class PackageSource:
    kind: str  # github | archive | local
    spec: str
    #: 远端：下载地址候选（按顺序尝试，GitHub 默认分支 main / master）；本地：空
    urls: Tuple[str, ...] = ()
    path: Optional[Path] = None
    #: GitHub 仓库名 / 归档文件名，pyproject 缺失时兜底当包名
    fallback_name: str = ""
    ref: str = ""


def _spec_looks_like_path(spec: str) -> bool:
    s = spec.strip()
    if s.startswith(("git+", "http://", "https://")):
        return False
    return (
        s.startswith(("file:", ".", "/", "~"))
        or re.match(r"^[A-Za-z]:[\\/]", s) is not None
        or "\\" in s
        or "/" in s
    )


def resolve_source(spec: str) -> PackageSource:
    """把用户给的来源归一：GitHub 仓库 → codeload 归档；归档 URL 原样；本机目录原地登记。"""

    text = spec.strip()
    if not text:
        raise DriverPackageError("缺少安装来源：GitHub 仓库地址、zip/tar.gz 归档地址或本机目录")
    if any(token in text for token in ("\n", "\r", "\x00")):
        raise DriverPackageError("安装来源含非法字符")

    match = _GITHUB_REPO_RE.match(text)
    if match and not _ARCHIVE_URL_RE.match(text):
        owner, repo = match.group("owner"), match.group("repo")
        ref = (match.group("at") or match.group("tree") or "").strip()
        base = f"https://codeload.github.com/{owner}/{repo}"
        if ref:
            urls = (f"{base}/zip/refs/heads/{ref}", f"{base}/zip/refs/tags/{ref}", f"{base}/zip/{ref}")
        else:
            urls = (f"{base}/zip/refs/heads/main", f"{base}/zip/refs/heads/master")
        return PackageSource(kind="github", spec=text, urls=urls, fallback_name=repo, ref=ref)

    if text.startswith(("http://", "https://")):
        if not _ARCHIVE_URL_RE.match(text):
            raise DriverPackageError("远端来源只支持 GitHub 仓库地址或 zip / tar.gz 归档地址")
        stem = re.sub(r"\.(zip|tar\.gz|tgz|tar)(\?.*)?$", "", text.rsplit("/", 1)[-1], flags=re.IGNORECASE)
        return PackageSource(kind="archive", spec=text, urls=(text,), fallback_name=stem)

    if text.startswith("git+"):
        raise DriverPackageError("只支持 GitHub 的 git 地址（git+https://github.com/<owner>/<repo>.git）")

    raw = text[5:] if text.startswith("file:") else text
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise DriverPackageError(f"本机目录不存在：{path}")
    return PackageSource(kind="local", spec=text, path=path.resolve(), fallback_name=path.resolve().name)


# ── 源码树解析 ───────────────────────────────────────────────────


def read_project_metadata(root: Path) -> Dict[str, Any]:
    """源码树的 ``pyproject.toml`` ``[project]``：name / version / dependencies；没有文件时全空。"""

    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return {"name": "", "version": "", "dependencies": [], "include": []}
    from unilabos.app.cli.package import _load_toml

    data = _load_toml(pyproject)
    project = data.get("project", {}) if isinstance(data, dict) else {}
    if not isinstance(project, dict):
        project = {}
    tool = data.get("tool", {}) if isinstance(data, dict) else {}
    find = ((tool.get("setuptools") or {}).get("packages") or {}).get("find") if isinstance(tool, dict) else None
    include = find.get("include") if isinstance(find, dict) else None
    dependencies = project.get("dependencies")
    return {
        "name": str(project.get("name") or "").strip(),
        "version": str(project.get("version") or "").strip(),
        "dependencies": [str(item).strip() for item in dependencies if str(item).strip()]
        if isinstance(dependencies, list)
        else [],
        "include": [str(item) for item in include] if isinstance(include, list) else [],
    }


def find_source_root(extracted: Path) -> Path:
    """解压目录里的源码根：最浅的 ``pyproject.toml`` 所在目录；没有就是唯一的顶层目录 / 解压目录本身。"""

    candidates = sorted(extracted.rglob("pyproject.toml"), key=lambda item: len(item.parts))
    if candidates:
        return candidates[0].parent
    children = [item for item in extracted.iterdir() if item.is_dir() and not item.name.startswith(".")]
    return children[0] if len(children) == 1 else extracted


def detect_package_dirs(root: Path, include_patterns: List[str] | None = None) -> List[str]:
    """源码树里要挂载的顶层 Python 包目录（有 ``__init__.py``），``src/`` 布局同样识别。

    ``[tool.setuptools.packages.find] include = ["site_demo*"]`` 存在时按前缀过滤；根目录自己就是
    Python 包（``__init__.py`` 在根）时挂载根目录。
    """

    def _matches(name: str) -> bool:
        if not include_patterns:
            return True
        return any(name == pattern.rstrip("*") or (pattern.endswith("*") and name.startswith(pattern[:-1])) for pattern in include_patterns)

    if (root / "__init__.py").is_file():
        return [str(root.resolve())]
    bases = [root / "src", root] if (root / "src").is_dir() else [root]
    found: List[str] = []
    for base in bases:
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")) or child.name in _NON_PACKAGE_DIRS:
                continue
            if (child / "__init__.py").is_file() and _matches(child.name):
                found.append(str(child.resolve()))
        if found:
            break
    return found


def scan_device_ids(package_dirs: List[str]) -> List[str]:
    """AST 扫描包目录里的 ``@device``（不 import），返回设备类 id。

    模块路径按各目录的导入根推导（与 ``--devices`` 挂载、``package inspect`` 一致）。
    """

    from unilabos.registry.package_layout import scan_package_dirs

    result = scan_package_dirs(package_dirs)
    return sorted(str(device_id) for device_id in result.get("devices", {}))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_safe_member(target: Path, member: str) -> None:
    root = target.resolve()
    resolved = (target / member).resolve()
    if root != resolved and root not in resolved.parents:
        raise DriverPackageError(f"归档含非法路径：{member}")


def extract_archive(archive: Path, target: Path) -> None:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                _assert_safe_member(target, member)
            zf.extractall(target)
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                _assert_safe_member(target, member.name)
            tf.extractall(target)
        return
    raise DriverPackageError("归档只支持 zip / tar / tar.gz")


def _dependency_filter(dependencies: List[str], package_name: str) -> List[str]:
    skip = {_normalize(package_name)} | {_normalize(item) for item in _SKIP_DEPENDENCIES}
    result: List[str] = []
    for item in dependencies:
        head = re.split(r"[\s<>=!~;\[]", item, maxsplit=1)[0]
        if _normalize(head) in skip:
            continue
        result.append(item)
    return result


def _dependency_install_command(installer: str, dependencies: List[str], upgrade: bool) -> List[str]:
    """与 environment_check._install_command 同一套约定：uv 显式 --python，中文 locale 走清华源。"""

    from unilabos.utils.environment_check import _is_chinese_locale

    chinese = _is_chinese_locale()
    if installer == "uv":
        command = ["uv", "pip", "install", "--python", sys.executable]
        if upgrade:
            command.append("--upgrade")
        command.extend(dependencies)
        if chinese:
            command.extend(["--index-url", "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"])
        return command
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    if upgrade:
        command.append("--upgrade")
    command.extend(dependencies)
    if chinese:
        command.extend(["-i", "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"])
    return command


class DriverPackageService:
    """驱动包台账 + 后台安装 / 卸载操作。"""

    def __init__(self, working_dir: str | Path) -> None:
        self.working_dir = Path(working_dir)
        self._lock = threading.Lock()
        self._mutation_lock = threading.Lock()
        self._operations: Dict[str, DriverPackageOperation] = {}
        self._order: List[str] = []
        self._scan_dirs: List[str] = []
        self._restart_required = False
        self._external_only = False
        self.source_changes = get_source_changes(self.working_dir)
        self.mutation_gate = get_host_mutation_gate(self.working_dir)
        self._mutation_token = None
        try:
            with self.mutation_gate.hold('recovery', recovery=True):
                records = journal.recover_operations(self.working_dir)
        except HostMutationBusy:
            # 另一服务实例/进程仍活跃时只读，不把其操作误判为崩溃。
            records = journal.load_operations(self.working_dir)
        self._load_operations(records)

    def _load_operations(self, records: List[Dict[str, Any]]) -> None:
        self._operations = {item['operation_id']: DriverPackageOperation(**item) for item in records}
        self._order = list(self._operations)

    def _persist_operations(self) -> None:
        journal.save_operations(self.working_dir, [asdict(self._operations[key]) for key in self._order])

    def _checkpoint(self, operation_id: str, **values: Any) -> None:
        with self._lock:
            operation = self._operations[operation_id]
            operation.recovery.update(values)
            operation.version += 1
            self._persist_operations()

    def _commit_ledger(self, operation_id: str, records: Dict[str, DriverPackageRecord]) -> None:
        self._checkpoint(operation_id, expected_ledger={'version': 2, 'packages': [asdict(item) for item in records.values()]})
        save_ledger(self.working_dir, records)
        self._checkpoint(operation_id, phase='ledger_committed')

    # ── 运行态上下文（启动时由 main 注入） ─────────────────────────

    def configure(self, scan_dirs: List[str], external_only: bool, *, watch: bool = False) -> None:
        self._scan_dirs = [str(Path(item).resolve()) for item in scan_dirs if item]
        self._external_only = external_only
        roots, packages = source_roots(self._scan_dirs, load_ledger(self.working_dir))
        self.source_changes.register("host", kind="host", name="Host", pid=os.getpid(), roots=roots, packages=packages)
        if watch:
            self.source_changes.start()

    # ── 读 ────────────────────────────────────────────────────────

    def inventory(self) -> Dict[str, Any]:
        records = load_ledger(self.working_dir)
        loaded_ids = self._loaded_device_ids()
        packages = []
        for record in sorted(records.values(), key=lambda item: item.name.lower()):
            dirs_exist = all(Path(item).is_dir() for item in record.package_dirs)
            mounted = record.enabled and bool(record.package_dirs) and all(
                item in self._scan_dirs for item in record.package_dirs
            )
            packages.append(
                {
                    **asdict(record),
                    "dirs_exist": dirs_exist,
                    "mounted": mounted,
                    "loaded_device_ids": [item for item in record.device_ids if item in loaded_ids],
                }
            )
        changes = self.source_changes.status()
        return {
            "python": {"executable": sys.executable, "version": sys.version.split()[0]},
            "working_dir": str(self.working_dir),
            "packages_root": str(packages_root(self.working_dir)),
            "ledger_path": str(ledger_path(self.working_dir)),
            "scan_dirs": list(self._scan_dirs),
            "external_only": self._external_only,
            "restart_required": self._restart_required or changes["restart_required"],
            "source_changes": changes,
            "packages": packages,
            "operations": self.operations(limit=5),
            "mutation": self.mutation_gate.status(),
        }

    def catalog(self) -> Dict[str, Any]:
        """可安装目录 = 官方索引（``HTTPConfig.driver_package_index_url``）∪ 本地
        ``<working_dir>/driver_package_catalog.json``；同名以官方为准，并标出台账里已登记的。

        两个来源都是 ``{"packages": [{name, spec, version?, description?, homepage?,
        devices?, tags?, official?}]}``，``spec`` 是安装来源（GitHub 仓库 / 归档地址）。
        """
        installed = set(load_ledger(self.working_dir).keys())
        sources: List[Dict[str, Any]] = []
        packages: List[Dict[str, Any]] = []
        seen: set[str] = set()

        def _collect(entries: List[Dict[str, Any]], source: str) -> int:
            count = 0
            for raw in entries:
                entry = _catalog_entry(raw, source=source, installed=installed)
                if entry is None or _normalize(entry["name"]) in seen:
                    continue
                seen.add(_normalize(entry["name"]))
                packages.append(entry)
                count += 1
            return count

        url = self._index_url()
        if url:
            try:
                entries = _catalog_entries(self._fetch_json(url))
                sources.append({"kind": "remote", "location": url, "ok": True, "count": _collect(entries, "remote")})
            except Exception as exc:  # noqa: BLE001 - 离线 / 索引损坏不影响本地目录
                sources.append({"kind": "remote", "location": url, "ok": False, "count": 0, "error": str(exc)})

        local = catalog_path(self.working_dir)
        if local.is_file():
            try:
                entries = _catalog_entries(json.loads(local.read_text(encoding="utf-8")))
                sources.append({"kind": "local", "location": str(local), "ok": True, "count": _collect(entries, "local")})
            except Exception as exc:  # noqa: BLE001
                sources.append({"kind": "local", "location": str(local), "ok": False, "count": 0, "error": str(exc)})
        else:
            sources.append({"kind": "local", "location": str(local), "ok": True, "count": 0, "missing": True})
        return {"sources": sources, "packages": packages}

    @staticmethod
    def _index_url() -> str:
        try:
            from unilabos.config.config import HTTPConfig

            return str(getattr(HTTPConfig, "driver_package_index_url", "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _fetch_json(url: str) -> Any:
        import urllib.request

        with urllib.request.urlopen(url, timeout=CATALOG_FETCH_TIMEOUT_S) as response:  # noqa: S310 - 管理员配置的索引地址
            return json.loads(response.read().decode("utf-8"))

    def operations(self, limit: int = OPERATION_HISTORY_LIMIT) -> List[Dict[str, Any]]:
        with self._lock:
            records = journal.load_operations(self.working_dir)
        pending = [item for item in reversed(records) if item.get('requires_confirmation')]
        rest = [item for item in reversed(records) if not item.get('requires_confirmation')]
        return pending + rest[:max(0, limit - len(pending))]

    def operation(self, operation_id: str) -> Dict[str, Any]:
        with self._lock:
            record = next((item for item in journal.load_operations(self.working_dir) if item['operation_id'] == operation_id), None)
        if record is None:
            raise DriverPackageError(f"operation not found: {operation_id}")
        return record

    def confirm_operation(self, operation_id: str, expected_version: int, reason: str) -> Dict[str, Any]:
        if not reason.strip():
            raise DriverPackageError('确认中断操作必须填写环境核实说明')
        try:
            with self.mutation_gate.hold('recovery_confirmation', recovery=True), self._lock:
                self._load_operations(journal.load_operations(self.working_dir))
                operation = self._operations.get(operation_id)
                if operation is None:
                    raise DriverPackageError(f'operation not found: {operation_id}')
                if operation.confirmation and operation.confirmation.get('expected_version') == expected_version and operation.confirmation['reason'] == reason.strip():
                    return asdict(operation)
                if operation.version != expected_version or not operation.requires_confirmation:
                    raise DriverPackageBusyError('包操作状态已变化，请刷新后重新核实')
                if operation.recovery.get('outcome') == 'manual_repair_required':
                    raise DriverPackageBusyError('源码或台账尚未恢复一致，请先修复并重新检查恢复状态')
                operation.requires_confirmation = False
                operation.version += 1
                operation.confirmation = {'expected_version': expected_version, 'reason': reason.strip(), 'at_ms': _now_ms()}
                self._persist_operations()
                self._restart_required = True
                self.source_changes.package_changed(operation.package_name or operation.spec,
                    reason='package_recovery_confirmed', shared_dependencies=True)
                return asdict(operation)
        except HostMutationBusy as exc:
            raise DriverPackageBusyError(str(exc)) from exc

    def retry_recovery(self, operation_id: str, expected_version: int, reason: str) -> Dict[str, Any]:
        if not reason.strip():
            raise DriverPackageError('重新恢复必须填写已修复的内容')
        try:
            with self.mutation_gate.hold('recovery_confirmation', recovery=True), self._lock:
                self._load_operations(journal.load_operations(self.working_dir))
                operation = self._operations.get(operation_id)
                if operation is None:
                    raise DriverPackageError(f'operation not found: {operation_id}')
                if operation.version != expected_version or operation.recovery.get('outcome') != 'manual_repair_required':
                    raise DriverPackageBusyError('恢复状态已变化，请刷新后重试')
                self._load_operations(journal.recover_operations(self.working_dir))
                operation = self._operations[operation_id]
                operation.log = (operation.log + f'[recovery] {reason.strip()}\n')[-20000:]
                self._persist_operations()
                return asdict(operation)
        except HostMutationBusy as exc:
            raise DriverPackageBusyError(str(exc)) from exc

    # ── 写 ────────────────────────────────────────────────────────

    def _acquire_mutation(self) -> None:
        if not self._mutation_lock.acquire(blocking=False):
            raise DriverPackageBusyError("已有驱动包变更正在进行，请查询 operations 并在完成后重试")
        try:
            self._mutation_token = self.mutation_gate.acquire('package')
            self._load_operations(journal.load_operations(self.working_dir))
        except BaseException as exc:
            if self._mutation_token is not None:
                self.mutation_gate.release(self._mutation_token)
                self._mutation_token = None
            self._mutation_lock.release()
            if isinstance(exc, HostMutationBusy):
                raise DriverPackageBusyError(str(exc)) from exc
            raise
        self.source_changes.mutation(True)

    def _release_mutation(self) -> None:
        try:
            self.source_changes.mutation(False)
        finally:
            self.mutation_gate.release(self._mutation_token)
            self._mutation_token = None
            self._mutation_lock.release()

    def set_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        self._acquire_mutation()
        operation = None
        try:
            records = load_ledger(self.working_dir)
            key = _normalize(name)
            record = records.get(key)
            if record is None:
                raise DriverPackageError(f"driver package not found: {name}")
            operation = self._new_operation('enable' if enabled else 'disable', name)
            if record.enabled != enabled:
                record.enabled = enabled
                record.updated_at_ms = _now_ms()
                self._commit_ledger(operation.operation_id, records)
                self._restart_required = True
                self.source_changes.package_changed(name, reason="package_mount_changed", host_mount=True)
            result = {**asdict(record), "source_changes": self.source_changes.status()}
            self._finish(operation.operation_id, result=result)
            return result
        except Exception as exc:
            if operation:
                self._finish(operation.operation_id, error=str(exc))
            raise
        finally:
            self._release_mutation()

    def _start_mutation(
        self, kind: str, spec: str, worker: Callable[..., Dict[str, Any]],
        *args: Any, package_name: str = "",
    ) -> Dict[str, Any]:
        self._acquire_mutation()
        operation = None
        try:
            operation = self._new_operation(kind, spec)
            operation.package_name = package_name
            with self._lock:
                self._persist_operations()
            accepted = asdict(operation)
            threading.Thread(
                target=self._execute_mutation,
                args=(operation.operation_id, worker, args),
                name=f"DriverPackage-{operation.operation_id[:8]}", daemon=True,
            ).start()
            return accepted
        except Exception as exc:
            if operation is not None:
                self._finish(operation.operation_id, error=str(exc))
            self._release_mutation()
            raise DriverPackageError(f"无法启动驱动包操作：{exc}") from exc

    def _execute_mutation(
        self, operation_id: str, worker: Callable[..., Dict[str, Any]], args: tuple[Any, ...],
    ) -> None:
        result = None
        error = None
        try:
            result = worker(operation_id, *args)
        except Exception as exc:  # noqa: BLE001 - 后台操作统一记录失败并释放写锁
            error = str(exc) or type(exc).__name__
            self._append_log(operation_id, f"[error] {error}")
            if not isinstance(exc, DriverPackageError):
                logger.exception("[DriverPackages] 驱动包变更失败")
        finally:
            try:
                if result is not None:
                    result["source_changes"] = self.source_changes.status()
                # 持久化完成后才允许重启，消除退出发生在最后一次台账写入前的窗口。
                self._finish(operation_id, error=error, result=result)
            finally:
                self._release_mutation()

    def start_install(
        self, spec: str, enable: bool = True, upgrade: bool = False, name: str = ""
    ) -> Dict[str, Any]:
        """源码树没有 pyproject 时使用调用方提供的 name；同一 Host 只接受一个包变更。"""
        source = resolve_source(spec)
        name = (name or "").strip()
        if name and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise DriverPackageError(f"包名不合法：{name}")
        return self._start_mutation(
            "install", source.spec, self._run_install, source, enable, upgrade, name, package_name=name,
        )

    def start_uninstall(self, name: str) -> Dict[str, Any]:
        record = load_ledger(self.working_dir).get(_normalize(name))
        if record is None:
            raise DriverPackageError(f"driver package not found: {name}")
        return self._start_mutation(
            "uninstall", record.name, self._run_uninstall, record.name, package_name=record.name,
        )

    # ── 内部 ──────────────────────────────────────────────────────

    def _new_operation(self, kind: str, spec: str) -> DriverPackageOperation:
        operation = DriverPackageOperation(
            operation_id=str(uuid.uuid4()),
            kind=kind,
            spec=spec,
            started_at_ms=_now_ms(),
            recovery={'phase': 'preparing', 'previous_ledger': journal.ledger_data(self.working_dir)},
        )
        with self._lock:
            self._operations[operation.operation_id] = operation
            self._order.append(operation.operation_id)
            completed = [key for key in self._order if self._operations[key].status != 'running'
                         and not self._operations[key].requires_confirmation]
            for stale in completed[:-OPERATION_HISTORY_LIMIT]:
                self._order.remove(stale)
                self._operations.pop(stale, None)
            self._persist_operations()
        return operation

    def _append_log(self, operation_id: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is not None:
                operation.log = (operation.log + text.rstrip() + "\n")[-20000:]
                self._persist_operations()

    def _finish(self, operation_id: str, *, error: Optional[str] = None, result: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                return
            operation.status = "failed" if error else "succeeded"
            operation.error = error
            operation.result = result
            operation.finished_at_ms = _now_ms()
            operation.requires_confirmation = bool(error and (
                operation.recovery.get('dependencies_started')
                or operation.recovery.get('outcome') == 'manual_repair_required'
                or operation.recovery.get('phase') == 'ledger_committed'))
            operation.version += 1
            self._persist_operations()

    # 下面三个是可替换的"外部世界"入口：测试用桩，生产用真实网络 / 子进程。

    def _download(self, url: str, destination: Path, log: Callable[[str], None]) -> None:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, headers={"User-Agent": "unilabos-driver-packages"})
        try:
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:  # noqa: S310 - 用户给的来源
                total = 0
                with destination.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES:
                            raise DriverPackageError(f"归档超过 {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB 上限")
                        handle.write(chunk)
        except urllib.error.HTTPError as exc:
            raise DriverPackageError(f"下载失败 HTTP {exc.code}: {url}") from exc
        except urllib.error.URLError as exc:
            raise DriverPackageError(f"下载失败: {exc.reason}") from exc
        log(f"[download] {url} -> {destination.stat().st_size} bytes")

    def _install_dependencies(self, dependencies: List[str], upgrade: bool, log: Callable[[str], None]) -> str:
        """返回实际使用的安装器（uv / pip）；无依赖返回空串。"""

        if not dependencies:
            log("[deps] 没有第三方依赖，跳过")
            return ""
        from unilabos.utils.environment_check import _installer_candidates

        last_error = ""
        for installer in _installer_candidates():
            command = _dependency_install_command(installer, dependencies, upgrade)
            log("$ " + " ".join(command))
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=DEPENDENCY_INSTALL_TIMEOUT_S)
            except FileNotFoundError:
                log(f"[deps] {installer} 不可用，换下一个安装器")
                continue
            except subprocess.TimeoutExpired:
                last_error = f"{installer} 超过 {DEPENDENCY_INSTALL_TIMEOUT_S}s"
                log(f"[deps] {last_error}")
                continue
            log(proc.stdout)
            log(proc.stderr)
            if proc.returncode == 0:
                return installer
            last_error = f"{installer} 退出码 {proc.returncode}"
        raise DriverPackageError(f"依赖安装失败（{last_error}）：{', '.join(dependencies)}")

    def _fetch_source_tree(self, source: PackageSource, log: Callable[[str], None]) -> Tuple[Path, Path, str]:
        """远端来源：下载 + 校验 + 解压到临时目录，返回 (临时根, 源码根, sha256)。"""

        staging = Path(tempfile.mkdtemp(prefix="driver-package-", dir=str(self._staging_root())))
        archive = staging / "package.archive"
        errors: List[str] = []
        for url in source.urls:
            try:
                self._download(url, archive, log)
                break
            except DriverPackageError as exc:
                errors.append(str(exc))
                log(f"[download] 失败，尝试下一个候选：{exc}")
        else:
            shutil.rmtree(staging, ignore_errors=True)
            raise DriverPackageError("；".join(errors) or "没有可用的下载地址")
        sha256 = _sha256_file(archive)
        extracted = staging / "extract"
        extracted.mkdir()
        extract_archive(archive, extracted)
        archive.unlink(missing_ok=True)
        root = find_source_root(extracted)
        log(f"[extract] 源码根 {root}")
        return staging, root, sha256

    def _staging_root(self) -> Path:
        root = packages_root(self.working_dir) / ".staging"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _run_install(
        self, operation_id: str, source: PackageSource, enable: bool, upgrade: bool, name_hint: str = ""
    ) -> Dict[str, Any]:
        log = lambda text: self._append_log(operation_id, text)  # noqa: E731
        staging: Optional[Path] = None
        try:
            if source.kind == "local":
                root = source.path
                assert root is not None
                sha256 = ""
                log(f"[local] 原地登记目录 {root}")
            else:
                staging, root, sha256 = self._fetch_source_tree(source, log)

            root = root.resolve()
            project = read_project_metadata(root)
            name = project["name"] or name_hint or source.fallback_name
            if not name:
                raise DriverPackageError("无法确定包名：源码树没有 pyproject.toml，请在请求里给 name")
            if name_hint and project["name"] and _normalize(name_hint) != _normalize(project["name"]):
                log(f"[warn] 索引里的包名 {name_hint} 与 pyproject 的 {project['name']} 不同，按 pyproject 登记")
            version = project["version"] or source.ref or ("local" if source.kind == "local" else "main")
            if source.kind != "local":
                self._package_target(name, version)  # 在依赖安装和文件移动之前验证落盘边界
            dependencies = _dependency_filter(project["dependencies"], name)
            log(f"[project] {name} {version} 依赖 {dependencies or '-'}")

            # 在候选源码上校验，不能先删旧版本再发现包无效。
            source_dirs = detect_package_dirs(root, project["include"])
            if not source_dirs:
                raise DriverPackageError(f"源码树里没有可挂载的 Python 包目录（含 __init__.py）：{root}")
            device_ids = scan_device_ids(source_dirs)
            if dependencies:
                # 安装器失败也可能已部分改变共享环境，因此提前标记受影响进程。
                self.source_changes.package_changed(name, reason="shared_dependencies_changed", shared_dependencies=True)
                self._checkpoint(operation_id, dependencies_started=True)
            installer = self._install_dependencies(dependencies, upgrade, log)
            placement = nullcontext(root) if source.kind == "local" else self._place_source_tree(root, name, version, log, operation_id)
            with placement as package_root:
                package_dirs = source_dirs if source.kind == "local" else [
                    str(package_root / Path(item).relative_to(root)) for item in source_dirs
                ]
                log(f"[scan] 包目录 {package_dirs} 设备 {', '.join(device_ids) or '-'}")
                records = load_ledger(self.working_dir)
                key = _normalize(name)
                previous = records.get(key)
                record = DriverPackageRecord(
                    name=name, spec=source.spec, version=version, source_kind=source.kind,
                    package_root=str(package_root), package_dirs=package_dirs, device_ids=device_ids,
                    dependencies=dependencies, sha256=sha256,
                    enabled=enable if previous is None else previous.enabled,
                    installer=installer,
                    installed_at_ms=previous.installed_at_ms if previous else _now_ms(),
                    updated_at_ms=_now_ms(),
                )
                records[key] = record
                self._commit_ledger(operation_id, records)
            self._restart_required = True
            self.source_changes.package_changed(name, reason="package_installed", host_mount=record.enabled)
            with self._lock:
                self._operations[operation_id].package_name = name
            return {"packages": [asdict(record)], "restart_required": True}
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    def _package_target(self, name: str, version: str) -> Path:
        """包名和版本只能成为缓存下的两个路径分量，不允许符号链接越界。"""
        def safe_component(value: str) -> str:
            component = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "unnamed"
            if component.startswith("."):
                raise DriverPackageError(f"包名或版本不能用作源码目录：{value!r}")
            return component

        managed = packages_root(self.working_dir).resolve()
        package_home = managed / safe_component(name)
        target = package_home / safe_component(version)
        if package_home.is_symlink() or target.is_symlink() or managed not in target.resolve().parents:
            raise DriverPackageError("驱动包源码目录越过缓存边界或使用了符号链接")
        return target

    @contextmanager
    def _source_transaction(self, target: Path, operation_id: str) -> Iterator[None]:
        """源码变更在台账成功写入前可回滚；回滚失败时保留备份供恢复。"""
        managed = packages_root(self.working_dir).resolve()
        if target.is_symlink() or managed not in target.resolve().parents:
            raise DriverPackageError("只能修改驱动包缓存内的源码目录")
        backup_root = Path(tempfile.mkdtemp(prefix="rollback-", dir=str(self._staging_root())))
        backup = backup_root / "previous"
        self._checkpoint(operation_id, phase='source_switch', source={
            'target': str(target), 'backup': str(backup), 'target_existed': target.exists()})
        try:
            if target.exists():
                target.rename(backup)
        except BaseException:
            shutil.rmtree(backup_root, ignore_errors=True)
            raise
        try:
            yield
        except BaseException:
            # 台账已提交后，日志写入失败不能把源码回滚为与新台账不一致的旧版本。
            recovery = self._operations[operation_id].recovery
            if journal.ledger_data(self.working_dir) == recovery.get('expected_ledger'):
                recovery['phase'] = 'ledger_committed'
            else:
                try:
                    if target.exists():
                        shutil.rmtree(target)
                    if backup.exists():
                        backup.rename(target)
                    self._checkpoint(operation_id, source=None, outcome='rolled_back')
                    shutil.rmtree(backup_root, ignore_errors=True)
                except BaseException:
                    recovery['outcome'] = 'manual_repair_required'
                    raise
            raise
        else:
            shutil.rmtree(backup_root, ignore_errors=True)

    @contextmanager
    def _place_source_tree(self, root: Path, name: str, version: str, log: Callable[[str], None], operation_id: str) -> Iterator[Path]:
        target = self._package_target(name, version)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._source_transaction(target, operation_id):
            shutil.move(str(root), str(target))
            log(f"[place] {target}")
            yield target
        # 新台账已提交，才回收不同版本的缓存；失败路径保留原版本。
        try:
            for sibling in target.parent.iterdir():
                if sibling.is_dir() and sibling != target and not sibling.is_symlink():
                    shutil.rmtree(sibling)
                    log(f"[place] 移除旧版本 {sibling.name}")
        except OSError as exc:
            # 提交后的缓存回收失败不改变已成功的安装结果。
            log(f"[warn] 旧版本缓存未完全回收：{exc}")

    def _run_uninstall(self, operation_id: str, name: str) -> Dict[str, Any]:
        log = lambda text: self._append_log(operation_id, text)  # noqa: E731
        records = load_ledger(self.working_dir)
        key = _normalize(name)
        record = records.get(key)
        if record is None:
            raise DriverPackageError(f"driver package not found: {name}")
        root = Path(record.package_root) if record.package_root else None
        managed_root = packages_root(self.working_dir).resolve()
        removal = nullcontext()
        if record.source_kind != "local" and root is not None and root.exists() and managed_root in root.resolve().parents:
            target = root.parent if root.parent.resolve() != managed_root else root
            removal = self._source_transaction(target, operation_id)
            log(f"[remove] {root}")
        else:
            log("[remove] 本机目录原地登记，只移出台账，不删文件")
        with removal:
            if record.dependencies:
                log(f"[deps] 保留已装依赖（可能被其他包共用）：{', '.join(record.dependencies)}")
            records.pop(key, None)
            self._commit_ledger(operation_id, records)
        self._restart_required = True
        self.source_changes.package_changed(name, reason="package_uninstalled", host_mount=record.enabled)
        return {"removed": record.name, "restart_required": True}

    @staticmethod
    def _loaded_device_ids() -> set[str]:
        try:
            from unilabos.registry.registry import lab_registry

            return set(lab_registry.device_type_registry.keys())
        except Exception:  # noqa: BLE001
            return set()


_service: Optional[DriverPackageService] = None
_service_lock = threading.Lock()


def get_driver_package_service() -> DriverPackageService:
    global _service
    with _service_lock:
        if _service is None:
            from unilabos.config.config import BasicConfig

            _service = DriverPackageService(getattr(BasicConfig, "working_dir", None) or Path.cwd())
        return _service


__all__ = [
    "DriverPackageError",
    "DriverPackageOperation",
    "DriverPackageRecord",
    "DriverPackageService",
    "PackageSource",
    "catalog_path",
    "detect_package_dirs",
    "enabled_package_dirs",
    "find_source_root",
    "get_driver_package_service",
    "ledger_path",
    "load_ledger",
    "packages_root",
    "read_project_metadata",
    "resolve_source",
    "save_ledger",
    "scan_device_ids",
]
