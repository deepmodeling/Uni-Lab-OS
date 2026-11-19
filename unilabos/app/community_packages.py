"""社区设备包的本地缓存发现与图引用解析。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from unilabos.utils import logger
from unilabos.utils.banner_print import print_status


COMMUNITY_PREFIX = "community."
COMMUNITY_CACHE_DIR = "community_devices"
MANIFEST_FILENAME = "manifest.json"


class CommunityPackageError(RuntimeError):
    """图引用的社区包尚未安装或本地缓存不可用。"""


@dataclass
class CommunityPackagePrepareResult:
    devices_dirs: List[str] = field(default_factory=list)
    aliases: Dict[str, str] = field(default_factory=dict)
    classes: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    # 已解析的包目录绝对路径 -> class_namespace(community.<ns>)。
    namespaces: Dict[str, str] = field(default_factory=dict)


def extract_community_classes(graph_data: Optional[Dict[str, Any]]) -> List[str]:
    if not graph_data:
        return []

    result: List[str] = []
    for node in graph_data.get("nodes", []):
        if not isinstance(node, dict):
            continue
        class_name = str(node.get("template_name") or node.get("class") or "").strip()
        if class_name.startswith(COMMUNITY_PREFIX):
            result.append(class_name)
    return sorted(set(result))


def community_namespace(class_name: str) -> str:
    parts = class_name.split(".")
    if len(parts) < 2 or parts[0] != "community":
        raise ValueError(f"Invalid community class: {class_name}")
    return ".".join(parts[:2])


def infer_alias_target(class_name: str) -> str:
    namespace = community_namespace(class_name)
    prefix = namespace + "."
    if class_name.startswith(prefix) and len(class_name) > len(prefix):
        return class_name[len(prefix) :]
    return class_name.rsplit(".", 1)[-1]


def load_manifest(working_dir: str | Path) -> Dict[str, Any]:
    manifest_path = _manifest_path(working_dir)
    if not manifest_path.is_file():
        return {"packages": {}}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("packages", {})
            return data
    except Exception as exc:  # noqa: BLE001 - 损坏缓存按未安装处理
        logger.warning(f"[CommunityPackage] manifest 读取失败: {exc}")
    return {"packages": {}}


def save_manifest(working_dir: str | Path, manifest: Dict[str, Any]) -> None:
    manifest_path = _manifest_path(working_dir)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = manifest_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(manifest_path)


def prepare_community_packages(
    graph_data: Optional[Dict[str, Any]],
    working_dir: str | Path,
) -> CommunityPackagePrepareResult:
    """从本地安装清单解析图中的 community 设备包。

    包缓存由部署流程准备；启动阶段只从本地 manifest 解析图中的包引用。
    """

    classes = extract_community_classes(graph_data)
    if not classes:
        return CommunityPackagePrepareResult()

    print_status(f"发现 community 设备引用: {', '.join(classes)}", "info")
    manifest = load_manifest(working_dir)
    packages = manifest.setdefault("packages", {})

    devices_dirs: List[str] = []
    aliases: Dict[str, str] = {}
    dependencies: List[str] = []
    namespaces: Dict[str, str] = {}
    missing_namespaces = {community_namespace(item) for item in classes}

    for namespace in list(missing_namespaces):
        cached = packages.get(namespace)
        if not isinstance(cached, dict):
            continue
        package_dir = Path(str(cached.get("package_dir") or ""))
        if not package_dir.is_dir():
            continue
        resolved_dir = str(package_dir.resolve())
        devices_dirs.append(resolved_dir)
        namespaces[resolved_dir] = namespace
        missing_namespaces.discard(namespace)
        cached_aliases = cached.get("aliases") or {}
        if isinstance(cached_aliases, dict):
            aliases.update({str(key): str(value) for key, value in cached_aliases.items()})
        dependencies.extend(cached.get("dependencies") or [])
        logger.trace(
            f"[CommunityPackage] 本地缓存命中: {namespace}@{cached.get('version')} "
            f"dir={resolved_dir}"
        )

    for class_name in classes:
        aliases.setdefault(class_name, infer_alias_target(class_name))

    if missing_namespaces:
        raise CommunityPackageError(
            "无法加载 community 设备包: "
            + ", ".join(sorted(missing_namespaces))
            + "。请先由部署流程写入本地 community manifest，或用 --devices 指定包目录。"
        )

    result = CommunityPackagePrepareResult(
        devices_dirs=_dedupe_existing_dirs(devices_dirs),
        aliases=aliases,
        classes=classes,
        dependencies=_dedupe_preserve_order(dependencies),
        namespaces=namespaces,
    )
    if result.devices_dirs:
        print_status(
            f"community 设备包挂载目录: {', '.join(result.devices_dirs)}",
            "info",
        )
    save_manifest(working_dir, manifest)
    return result


def _manifest_path(working_dir: str | Path) -> Path:
    return _cache_root(working_dir) / MANIFEST_FILENAME


def _cache_root(working_dir: str | Path) -> Path:
    root = Path(working_dir) / COMMUNITY_CACHE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _dedupe_existing_dirs(paths: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for path in paths:
        resolved = str(Path(path).resolve())
        if resolved in seen or not Path(resolved).is_dir():
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
