"""按 ``asset_id`` + ``package_digest`` 从资产库存储拉取并校验工程包。

存储只需提供 ``get(key) -> bytes``（键相对资产库根，例如 ``library.json``）。
下载到 ``<cache>/.staging/<随机>`` 后整包校验，通过才原子改名到
``<cache>/<库内路径>``；已存在的缓存先校验，损坏时保留现场并报错，不静默覆盖。

不强制联网：锁行写了 ``path`` 的直接用本地工程包（给了 ``package_digest`` 就校验）；
其余行先按摘要在缓存里找，找到即用，缺了才打开存储下载；存储为 ``None`` 即离线。
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Protocol, Union

from . import native_package as native

USD_LIBRARY_SCHEMA = "unilab.usd-library/1"


class AssetStore(Protocol):
    """资产库只读存储（OSS、本地镜像等）的最小 Interface。"""

    def get(self, key: str) -> bytes:
        """返回资产库根下 ``key`` 的完整字节；不存在时抛异常。"""

        ...


class AssetFetchError(ValueError):
    """锁、索引或下载结果不满足固定引用。"""


def _retry(operation: Callable[[], bytes], attempts: int = 4) -> bytes:
    for attempt in range(attempts):
        try:
            return operation()
        except native.AssetIntegrityError:
            raise
        except Exception:  # 网络瞬断由外部 SDK 抛出，类型各异
            if attempt == attempts - 1:
                raise
            time.sleep(min(8, 2**attempt))
    raise AssertionError("unreachable")


class LibraryIndex:
    """资产库根 ``library.json`` 的 ``packages`` 行（asset_id/package_digest/path）。"""

    def __init__(self, store: AssetStore) -> None:
        data = json.loads(_retry(lambda: store.get("library.json")))
        if data.get("schema") not in (None, USD_LIBRARY_SCHEMA) and "packages" not in data:
            raise AssetFetchError("未知资产库索引格式：" + str(data.get("schema")))
        self.rows = {(row["asset_id"], row["package_digest"]): row for row in data.get("packages", [])}

    def path_of(self, asset_id: str, package_digest: str) -> str:
        row = self.rows.get((asset_id, package_digest))
        if row is None:
            raise AssetFetchError(
                f"资产库里没有固定版本 {asset_id}@{package_digest[:12]}；需显式迁移锁或先发布该版本"
            )
        return row["path"]


def _verify_named(staging: Path, destination: Path, digest: str) -> None:
    """NP.005 要求包目录名与 USD 入口同名：在同名临时目录里校验。"""

    named = staging.parent / (staging.name + "-package") / destination.name
    named.parent.mkdir()
    staging.rename(named)
    try:
        native.verify_package(named, digest)
    finally:
        named.rename(staging)
        named.parent.rmdir()


def fetch_package(
    store: AssetStore,
    asset_id: str,
    package_digest: str,
    cache: Path,
    *,
    index: LibraryIndex | None = None,
    workers: int = 8,
) -> tuple[Path, dict[str, Any]]:
    """拉取一个固定版本工程包到缓存并完整校验。

    返回：``(包目录, 回执)``；回执 ``action`` 为 ``cache_hit`` 或 ``downloaded``。
    异常：索引缺版本、摘要不符、路径越界时抛 ``AssetFetchError``/``AssetIntegrityError``。
    """

    index = index or LibraryIndex(store)
    relative = index.path_of(asset_id, package_digest)
    cache = Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    destination = native.inside(cache, relative)
    if destination.exists():
        native.verify_package(destination, package_digest)
        return destination, {"status": "passed", "action": "cache_hit", "path": str(destination)}

    raw = _retry(lambda: store.get(f"{relative}/{native.MANIFEST}"))
    if native.sha256_bytes(raw) != package_digest:
        raise AssetFetchError("下载到的工程包清单摘要与锁不符")
    manifest = json.loads(raw)
    if manifest["package_id"] != asset_id:
        raise AssetFetchError("下载到的工程包身份与锁不符")

    staging = cache / ".staging" / uuid.uuid4().hex
    staging.mkdir(parents=True)
    try:
        (staging / native.MANIFEST).write_bytes(raw)
        for meta in manifest["metadata"]:
            rel = ".metadata/" + meta["name"]
            data = _retry(lambda rel=rel: store.get(f"{relative}/{rel}"))
            if native.sha256_bytes(data) != meta["hash"]["sha256"]:
                raise AssetFetchError("元数据下载摘要不符：" + rel)
            target = native.inside(staging, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        bom = native.read_json(staging / native.BOM)
        native.check_manifest(manifest, bom)
        targets = {item["relative_path"]: native.inside(staging, item["relative_path"]) for item in bom["items"]}
        for target in targets.values():
            target.parent.mkdir(parents=True, exist_ok=True)

        def download(item: dict[str, Any]) -> None:
            rel = item["relative_path"]
            data = _retry(lambda: store.get(f"{relative}/{rel}"))
            if len(data) != item["size"] or native.sha256_bytes(data) != item["hash"]["sha256"]:
                raise AssetFetchError("文件下载摘要不符：" + rel)
            targets[rel].write_bytes(data)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(download, bom["items"]))
        if staging.name == destination.name:
            native.verify_package(staging, package_digest)
        else:
            _verify_named(staging, destination, package_digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(destination)
        for path in destination.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
        return destination, {
            "status": "passed",
            "action": "downloaded",
            "path": str(destination),
            "downloaded": len(bom["items"]) + len(manifest["metadata"]) + 1,
        }
    except Exception:
        if staging.exists() and staging.resolve().is_relative_to(cache / ".staging"):
            shutil.rmtree(staging, ignore_errors=True)
        raise


def read_asset_lock(path: Path) -> list[dict[str, Any]]:
    """读取领域包资产锁（``unilab.asset-dependencies/1``）的依赖行。

    资产库行至少有 ``asset_id`` 与 ``package_digest``；本地行写 ``path``（工程包目录，相对锁文件
    所在目录或绝对路径），``package_digest`` 可选（给了就校验，没给即未固定的开发中包）。
    ``key``/``owner_registry_id``/``title``/``revision`` 只用于展示和按键选择。
    """

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != "unilab.asset-dependencies/1":
        raise AssetFetchError("资产锁 schema 必须是 unilab.asset-dependencies/1")
    rows = data.get("dependencies")
    if not isinstance(rows, list) or not rows:
        raise AssetFetchError("资产锁必须包含非空 dependencies 数组")
    keys = set()
    for row in rows:
        if not isinstance(row, dict):
            raise AssetFetchError("资产锁行必须是对象")
        local = row.get("path")
        if local is not None and (not isinstance(local, str) or not local):
            raise AssetFetchError("资产锁行 path 必须是非空路径")
        if local is None and (not isinstance(row.get("asset_id"), str) or not row.get("asset_id")
                              or not isinstance(row.get("package_digest"), str)):
            raise AssetFetchError("资产锁行缺少 asset_id/package_digest（本地包写 path）")
        if row.get("package_digest") is not None and not re.fullmatch(r"[0-9a-f]{64}", str(row["package_digest"])):
            raise AssetFetchError("package_digest 必须是 64 位十六进制 SHA-256")
        key = row.get("key") or row.get("owner_registry_id") or row.get("asset_id") or local
        if not isinstance(key, str) or key in keys:
            raise AssetFetchError("资产锁选择键缺失或重复")
        keys.add(key)
    return rows


StoreSource = Union[AssetStore, Callable[[], AssetStore], None]


def cached_packages(cache: Path) -> dict[str, Path]:
    """缓存里已有的工程包：``{package_digest: 包目录}``（不含下载中的 ``.staging``）。"""

    cache = Path(cache)
    if not cache.is_dir():
        return {}
    found: dict[str, Path] = {}
    for manifest in cache.rglob(native.MANIFEST):
        if ".staging" in manifest.relative_to(cache).parts:
            continue
        found.setdefault(native.sha256_file(manifest), manifest.parent)
    return found


def local_package(lock_path: Path, row: dict[str, Any]) -> Path:
    """锁行 ``path`` 指向的本地工程包目录（相对锁文件所在目录）。"""

    folder = Path(row["path"]).expanduser()
    if not folder.is_absolute():
        folder = Path(lock_path).resolve().parent / folder
    folder = folder.resolve()
    if not (folder / "simready_usd").is_dir():
        raise AssetFetchError(f"本地工程包不存在或缺 simready_usd：{folder}")
    return folder


def fetch_lock(
    store: StoreSource,
    lock_path: Path,
    cache: Path,
    *,
    only: list[str] | None = None,
) -> dict[str, Any]:
    """按资产锁取全部（或 ``only`` 指定键的）工程包。

    参数：``store`` 是资产库存储、返回存储的无参函数（只在缓存缺包时才调用，不缺就不联网），
    或 ``None``（离线：缓存缺包即失败）。
    返回：``{"status", "packages": [{key, asset_id, package_digest, path, action}]}``；
    ``action`` 为 ``local``（锁行本地路径）、``cache_hit`` 或 ``downloaded``。
    任一包失败即抛异常，不返回部分成功。
    """

    rows = read_asset_lock(lock_path)
    def key_of(row):
        return row.get("key") or row.get("owner_registry_id") or row.get("asset_id") or row.get("path")

    if only:
        wanted = set(only)
        rows = [row for row in rows if key_of(row) in wanted]
        missing = wanted - {key_of(row) for row in rows}
        if missing:
            raise AssetFetchError("资产锁里没有这些键：" + ", ".join(sorted(missing)))
    cached: dict[str, Path] | None = None
    opened: AssetStore | None = None
    index: LibraryIndex | None = None
    receipts = []
    for row in rows:
        digest = row.get("package_digest")
        if row.get("path"):
            folder = local_package(lock_path, row)
            if digest:
                native.verify_package(folder, digest)
            action = "local"
        else:
            if cached is None:
                cached = cached_packages(cache)
            folder = cached.get(digest)
            if folder is not None:
                native.verify_package(folder, digest)
                action = "cache_hit"
            else:
                if store is None:
                    raise AssetFetchError(f"离线模式：缓存里没有 {key_of(row)}@{digest[:12]}")
                if opened is None:
                    opened = store() if callable(store) and not hasattr(store, "get") else store
                    index = LibraryIndex(opened)
                folder, receipt = fetch_package(opened, row["asset_id"], digest, cache, index=index)
                action = receipt["action"]
        receipts.append(
            {
                "key": key_of(row),
                "asset_id": row.get("asset_id"),
                "package_digest": digest,
                "path": str(folder),
                "action": action,
            }
        )
    return {"status": "passed", "packages": receipts}


__all__ = [
    "AssetFetchError",
    "AssetStore",
    "LibraryIndex",
    "cached_packages",
    "local_package",
    "fetch_lock",
    "fetch_package",
    "read_asset_lock",
]
