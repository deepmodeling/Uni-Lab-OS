"""工作区只读路由：``/api/v1/workspace/package-mounts`` 与 ``/api/v1/file-browser``（V1 合同）。

两者都描述 Host 进程的工作区（驱动包台账、工作目录），挂在带执行面的 Host 上；调度权威经
``edge_proxy`` 转发（前缀 ``workspace`` / ``file-browser``）。统一信封。

- package-mounts：V1 ``workspace-package-mounts/v1`` 投影，数据来自 dev 驱动包台账（已启用、
  目录存在的包），每个挂载目录一项；dev 没有「可编辑工作区包」概念，``editable`` 一律 false；
- file-browser：列出工作目录内的子目录与文件。与 V1 不同，路径限制在工作目录之内（越界 1000）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Query

from unilabos.server.api.envelope import INVALID_PARAMETER, RESOURCE_NOT_FOUND, ApiError, envelope_router

PACKAGE_MOUNTS_SCHEMA = "workspace-package-mounts/v1"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def package_mount_projection(service: Any) -> dict[str, Any]:
    """dev 驱动包台账 → V1 包挂载投影（稳定排序；摘要按内容派生）。"""

    inventory = service.inventory() if service is not None else {"packages": []}
    items = []
    for package in inventory.get("packages", []):
        if not package.get("enabled") or not package.get("dirs_exist"):
            continue
        for package_dir in sorted(package.get("package_dirs") or []):
            root = Path(package_dir)
            items.append({
                "packageId": f"{package['name']}:{root.name}",
                "distributionName": package["name"],
                "version": package.get("version") or "",
                "namespace": root.name,
                "editable": False,
                "readOnly": True,
                "sourceKind": package.get("source_kind") or "local",
                "importRootUri": root.parent.resolve().as_uri(),
                "packageRootUri": root.resolve().as_uri(),
                "contentDigest": package.get("sha256") or _digest([package["name"], package.get("version"), package_dir]),
                "catalogDigest": _digest(package.get("device_ids") or []),
            })
    items.sort(key=lambda item: item["packageId"])
    catalog_revision = _digest([item["catalogDigest"] for item in items])
    return {
        "schemaVersion": PACKAGE_MOUNTS_SCHEMA,
        "editablePackageId": None,
        "dependencyRevision": _digest([(item["distributionName"], item["version"]) for item in items]),
        "catalogRevision": catalog_revision,
        "mountRevision": _digest([item["packageRootUri"] for item in items] + [catalog_revision]),
        "items": items,
    }


def browse(working_dir: Path, path: str = "") -> dict[str, Any]:
    root = working_dir.resolve()
    target = (root / path).resolve() if path else root
    if target != root and not target.is_relative_to(root):
        raise ApiError(INVALID_PARAMETER, f"路径超出工作目录: {path}")
    if not target.exists():
        raise ApiError(RESOURCE_NOT_FOUND, f"路径不存在: {path}")
    if not target.is_dir():
        raise ApiError(INVALID_PARAMETER, f"不是目录: {path}")
    items: list[dict[str, Any]] = []
    if target != root:
        items.append({"name": "..", "type": "directory", "path": str(target.parent), "size": 0, "is_parent": True})
    try:
        for item in sorted(target.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower())):
            is_file = item.is_file()
            items.append({
                "name": item.name,
                "type": "file" if is_file else "directory",
                "path": str(item),
                "size": item.stat().st_size if is_file else 0,
                "is_python": is_file and item.suffix == ".py",
                "is_parent": False,
            })
    except PermissionError:
        raise ApiError(INVALID_PARAMETER, "无权限访问此目录") from None
    return {"current_path": str(target), "working_dir": str(root), "items": items}


def create_workspace_router(*, working_dir: Optional[str | Path] = None, driver_packages: Any = None) -> APIRouter:
    router = envelope_router(prefix="/api/v1", tags=["workspace"])

    def resolve_working_dir() -> Path:
        if working_dir is not None:
            return Path(working_dir)
        from unilabos.config.config import BasicConfig

        return Path(getattr(BasicConfig, "working_dir", None) or os.getcwd())

    def resolve_packages() -> Any:
        if driver_packages is not None:
            return driver_packages
        from unilabos.server.services.driver_packages import get_driver_package_service

        return get_driver_package_service()

    @router.get("/workspace/package-mounts")
    def get_package_mounts() -> Any:
        """当前工作区（驱动包台账）的精确软件包源码挂载。"""

        return package_mount_projection(resolve_packages())

    @router.get("/file-browser")
    def get_file_browser(path: str = Query(default="")) -> Any:
        """浏览工作目录（只读，限制在工作目录内）。"""

        return browse(resolve_working_dir(), path)

    return router


__all__ = ["PACKAGE_MOUNTS_SCHEMA", "browse", "create_workspace_router", "package_mount_projection"]
