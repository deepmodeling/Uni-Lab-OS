import hashlib
import json
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from unilabos.utils import logger
from unilabos.utils.banner_print import print_status


COMMUNITY_PREFIX = "community."
COMMUNITY_CACHE_DIR = "community_devices"
MANIFEST_FILENAME = "manifest.json"


class CommunityPackageError(RuntimeError):
    """Raised when a graph references community packages that cannot be loaded."""


@dataclass
class CommunityPackagePrepareResult:
    devices_dirs: List[str] = field(default_factory=list)
    aliases: Dict[str, str] = field(default_factory=dict)
    classes: List[str] = field(default_factory=list)


def extract_community_classes(graph_data: Optional[Dict[str, Any]]) -> List[str]:
    if not graph_data:
        return []

    result: List[str] = []
    for node in graph_data.get("nodes", []):
        if not isinstance(node, dict):
            continue
        class_name = node.get("class")
        if isinstance(class_name, str) and class_name.startswith(COMMUNITY_PREFIX):
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
        return class_name[len(prefix):]
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
    except Exception as exc:
        logger.warning(f"[CommunityPackage] manifest 读取失败: {exc}")
    return {"packages": {}}


def save_manifest(working_dir: str | Path, manifest: Dict[str, Any]) -> None:
    manifest_path = _manifest_path(working_dir)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(manifest_path)


def prepare_community_packages(
    graph_data: Optional[Dict[str, Any]],
    working_dir: str | Path,
    http_client: Any = None,
) -> CommunityPackagePrepareResult:
    classes = extract_community_classes(graph_data)
    if not classes:
        return CommunityPackagePrepareResult()

    print_status(f"发现 community 设备引用: {', '.join(classes)}", "info")
    manifest = load_manifest(working_dir)
    packages = manifest.setdefault("packages", {})
    remote_items = _resolve_remote_packages(classes, manifest, http_client)

    devices_dirs: List[str] = []
    aliases: Dict[str, str] = {}
    missing_namespaces = {community_namespace(class_name) for class_name in classes}

    for item in remote_items:
        package_dir = _ensure_remote_item_cached(item, working_dir, manifest, http_client=http_client)
        if package_dir:
            devices_dirs.append(str(package_dir))

        namespace = item.get("class_namespace") or (item.get("package_info") or {}).get("class_namespace")
        if namespace:
            missing_namespaces.discard(namespace)
        aliases.update(_normalize_aliases(item, classes))

    for namespace in list(missing_namespaces):
        cached = packages.get(namespace)
        if not cached:
            continue
        package_dir = Path(cached.get("package_dir", ""))
        if package_dir.is_dir():
            devices_dirs.append(str(package_dir))
            missing_namespaces.discard(namespace)
            cached_aliases = cached.get("aliases") or {}
            aliases.update({str(k): str(v) for k, v in cached_aliases.items()})

    for class_name in classes:
        aliases.setdefault(class_name, infer_alias_target(class_name))

    if missing_namespaces:
        raise CommunityPackageError(
            "无法加载 community 设备包: "
            + ", ".join(sorted(missing_namespaces))
            + "。请检查网络、后端 resolve 接口或本地缓存。"
        )

    devices_dirs = _dedupe_existing_dirs(devices_dirs)
    if devices_dirs:
        print_status(f"community 设备包挂载目录: {', '.join(devices_dirs)}", "info")

    save_manifest(working_dir, manifest)
    return CommunityPackagePrepareResult(devices_dirs=devices_dirs, aliases=aliases, classes=classes)


def apply_community_aliases(registry: Any, aliases: Dict[str, str]) -> None:
    if not aliases:
        return

    added: List[str] = []
    for alias, target in aliases.items():
        if alias in registry.device_type_registry or alias in registry.resource_type_registry:
            continue
        if target in registry.device_type_registry:
            registry.device_type_registry[alias] = registry.device_type_registry[target]
            added.append(alias)
        elif target in registry.resource_type_registry:
            registry.resource_type_registry[alias] = registry.resource_type_registry[target]
            added.append(alias)
        else:
            logger.warning(f"[CommunityPackage] alias 目标不存在: {alias} -> {target}")

    if added:
        print_status(f"已注册 community class alias: {', '.join(sorted(added))}", "info")


def _resolve_remote_packages(classes: List[str], manifest: Dict[str, Any], http_client: Any) -> List[Dict[str, Any]]:
    if http_client is None:
        return []
    try:
        current_packages = []
        for namespace, info in (manifest.get("packages") or {}).items():
            current_packages.append(
                {
                    "class_namespace": namespace,
                    "version": info.get("version"),
                    "sha256": info.get("sha256"),
                }
            )

        response = http_client.resolve_community_packages(classes, current_packages=current_packages)
        data = response.get("data", response) if isinstance(response, dict) else []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except Exception as exc:
        logger.warning(f"[CommunityPackage] 远端 resolve 失败，将尝试本地缓存: {exc}")
    return []


def _ensure_remote_item_cached(
    item: Dict[str, Any],
    working_dir: str | Path,
    manifest: Dict[str, Any],
    http_client: Any = None,
) -> Optional[Path]:
    package_info = item.get("package_info") or item
    namespace = item.get("class_namespace") or package_info.get("class_namespace")
    if not namespace:
        return None

    packages = manifest.setdefault("packages", {})
    cached = packages.get(namespace) or {}
    version = str(package_info.get("version") or cached.get("version") or "unknown")
    sha256 = str(package_info.get("sha256") or cached.get("sha256") or "")
    cached_dir = Path(cached.get("package_dir", ""))
    if cached_dir.is_dir() and cached.get("version") == version and cached.get("sha256", "") == sha256:
        return cached_dir

    download_url = package_info.get("download_url")
    if not download_url:
        if cached_dir.is_dir() and package_info.get("allow_cached_fallback"):
            logger.warning(f"[CommunityPackage] {namespace} 无下载地址，使用旧缓存")
            return cached_dir
        raise CommunityPackageError(f"community package {namespace} 缺少 download_url")

    package_dir = _download_and_extract_package(download_url, working_dir, namespace, version, sha256, http_client)
    pyproject = _find_pyproject(package_dir)
    pyproject_meta = read_pyproject_metadata(pyproject)
    aliases = _normalize_aliases(item, [])

    packages[namespace] = {
        "class_namespace": namespace,
        "version": version,
        "sha256": sha256,
        "download_url": download_url,
        "package_dir": str(package_dir),
        "pyproject": pyproject_meta,
        "aliases": aliases,
    }
    (package_dir / "package_info.json").write_text(
        json.dumps(package_info, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return package_dir


def _download_and_extract_package(
    download_url: str,
    working_dir: str | Path,
    namespace: str,
    version: str,
    expected_sha256: str = "",
    http_client: Any = None,
) -> Path:
    import requests

    normalized = _normalize_package_dir_name(namespace)
    target_root = Path(working_dir) / COMMUNITY_CACHE_DIR / normalized / version
    package_dir = target_root / "package"
    tmp_root = Path(tempfile.mkdtemp(prefix=f"{normalized}-{version}-", dir=str(_cache_root(working_dir))))
    archive_path = tmp_root / "package.archive"

    try:
        print_status(f"下载 community 设备包 {namespace}@{version}", "info")
        requester = getattr(http_client, "_session", None) or requests
        with requester.get(download_url, stream=True, timeout=(5, 120)) as response:
            response.raise_for_status()
            with archive_path.open("wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

        if expected_sha256:
            actual = "sha256:" + _sha256_file(archive_path)
            if actual != expected_sha256:
                raise CommunityPackageError(f"{namespace}@{version} sha256 不匹配: {actual} != {expected_sha256}")

        extract_root = tmp_root / "extract"
        extract_root.mkdir(parents=True, exist_ok=True)
        _extract_archive(archive_path, extract_root)
        pyproject = _find_pyproject(extract_root)
        source_root = pyproject.parent

        if target_root.exists():
            shutil.rmtree(target_root)
        target_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root, package_dir)
        return package_dir
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _normalize_aliases(item: Dict[str, Any], classes: Iterable[str]) -> Dict[str, str]:
    raw_aliases = item.get("aliases") or {}
    aliases = {str(k): str(v) for k, v in raw_aliases.items()} if isinstance(raw_aliases, dict) else {}

    namespace = item.get("class_namespace") or (item.get("package_info") or {}).get("class_namespace")
    if namespace:
        for class_name in classes:
            if class_name.startswith(namespace + "."):
                aliases.setdefault(class_name, infer_alias_target(class_name))
    return aliases


def read_pyproject_metadata(pyproject_path: Path) -> Dict[str, str]:
    text = pyproject_path.read_text(encoding="utf-8")
    result: Dict[str, str] = {}
    in_project = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if not in_project or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in {"name", "version"}:
            result[key] = value
    return result


def _manifest_path(working_dir: str | Path) -> Path:
    return _cache_root(working_dir) / MANIFEST_FILENAME


def _cache_root(working_dir: str | Path) -> Path:
    root = Path(working_dir) / COMMUNITY_CACHE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _normalize_package_dir_name(namespace: str) -> str:
    return namespace.replace(COMMUNITY_PREFIX, "", 1).replace(".", "-").replace("_", "-")


def _dedupe_existing_dirs(paths: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for path in paths:
        resolved = str(Path(path).resolve())
        if resolved in seen or not Path(resolved).is_dir():
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_archive(archive_path: Path, target_dir: Path) -> None:
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.namelist():
                _assert_safe_archive_member(target_dir, member)
            zf.extractall(target_dir)
        return
    if tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as tf:
            for member in tf.getmembers():
                _assert_safe_archive_member(target_dir, member.name)
            tf.extractall(target_dir)
        return
    raise CommunityPackageError("community package 只支持 zip/tar/tar.gz 格式")


def _assert_safe_archive_member(target_dir: Path, member_name: str) -> None:
    target_root = target_dir.resolve()
    target_path = (target_dir / member_name).resolve()
    if target_root != target_path and target_root not in target_path.parents:
        raise CommunityPackageError(f"community package 包含非法路径: {member_name}")


def _find_pyproject(root: Path) -> Path:
    candidates = sorted(root.rglob("pyproject.toml"))
    if not candidates:
        raise CommunityPackageError(f"community package 解压后未找到 pyproject.toml: {root}")
    return candidates[0]
