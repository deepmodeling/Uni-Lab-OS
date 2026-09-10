"""工作区软件包源码的只读创作挂载投影。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..package_catalog import PackageCatalog
from ..package_catalog.sources import WorkspaceSource
from .package_source import PackageCatalogSource


@dataclass(frozen=True, slots=True)
class WorkspacePackageMount:
    """一个由同代包目录证明的本地源码挂载。"""

    package_id: str
    distribution_name: str
    version: str
    namespace: str
    editable: bool
    source_kind: Literal["workspace"]
    import_root: Path
    package_root: Path
    content_digest: str
    catalog_digest: str

    def __post_init__(self) -> None:
        """关闭式校验挂载身份和规范路径。

        参数：无；读取构造字段。返回：无。异常：身份为空、路径不是规范绝对
        目录或包根越过导入根时抛出 ``ValueError``。
        """

        text_fields = (
            self.package_id,
            self.distribution_name,
            self.version,
            self.namespace,
            self.content_digest,
            self.catalog_digest,
        )
        if any(not isinstance(value, str) or not value.strip() for value in text_fields):
            raise ValueError("工作区软件包挂载身份字段不能为空")
        if self.source_kind != "workspace":
            raise ValueError("工作区软件包挂载来源类型非法")
        if (
            not self.import_root.is_absolute()
            or not self.package_root.is_absolute()
            or self.import_root.resolve(strict=True) != self.import_root
            or self.package_root.resolve(strict=True) != self.package_root
            or not self.package_root.is_relative_to(self.import_root)
        ):
            raise ValueError("工作区软件包挂载必须使用规范绝对目录")

    def to_dict(self) -> dict[str, object]:
        """返回不共享内部状态的 wire-safe 挂载对象。

        参数：无。返回：供本地 Workbench 消费的精确包身份和文件 URI。
        异常：无；路径和身份已在构造阶段验证。
        """

        return {
            "packageId": self.package_id,
            "distributionName": self.distribution_name,
            "version": self.version,
            "namespace": self.namespace,
            "editable": self.editable,
            "readOnly": not self.editable,
            "sourceKind": self.source_kind,
            "importRootUri": self.import_root.as_uri(),
            "packageRootUri": self.package_root.as_uri(),
            "contentDigest": self.content_digest,
            "catalogDigest": self.catalog_digest,
        }


@dataclass(frozen=True, slots=True)
class WorkspacePackageMountProjection:
    """一次完整工作区候选代的精确源码挂载集合。"""

    editable_package_id: str
    dependency_revision: str
    catalog_revision: str
    mount_revision: str
    items: tuple[WorkspacePackageMount, ...]
    schema_version: Literal["workspace-package-mounts/v1"] = (
        "workspace-package-mounts/v1"
    )

    def __post_init__(self) -> None:
        """验证投影只有一个可编辑包且包身份无冲突。

        参数：无。返回：无。异常：投影为空、身份重复或可编辑包不唯一时抛出
        ``ValueError``。
        """

        if not self.items:
            raise ValueError("工作区软件包挂载投影不能为空")
        package_ids = [item.package_id for item in self.items]
        if len(package_ids) != len(set(package_ids)):
            raise ValueError("工作区软件包挂载存在重复 packageId")
        editable_items = [item for item in self.items if item.editable]
        if (
            len(editable_items) != 1
            or editable_items[0].package_id != self.editable_package_id
        ):
            raise ValueError("工作区软件包挂载必须且只能包含一个可编辑包")
        if not self.catalog_revision or not self.mount_revision:
            raise ValueError("工作区软件包挂载投影摘要不能为空")

    def to_dict(self) -> dict[str, object]:
        """返回公共 Backend 信封中的纯 JSON 数据。

        参数：无。返回：稳定排序的挂载投影。异常：无。
        """

        return {
            "schemaVersion": self.schema_version,
            "editablePackageId": self.editable_package_id,
            "dependencyRevision": self.dependency_revision,
            "catalogRevision": self.catalog_revision,
            "mountRevision": self.mount_revision,
            "items": [item.to_dict() for item in self.items],
        }


def compile_workspace_package_mount_projection(
    packages: tuple[PackageCatalogSource, ...],
    *,
    editable_source: WorkspaceSource,
    dependency_revision: str,
) -> WorkspacePackageMountProjection:
    """从完整候选代编译 Workbench 唯一允许使用的源码挂载表。

    参数：``packages`` 是主包与锁定依赖的来源/目录配对；``editable_source`` 是
    唯一可写来源；``dependency_revision`` 是依赖声明与锁的固定摘要。返回：包含
    移动无关目录摘要和路径相关挂载摘要的不可变投影。异常：来源配对、包根或身份
    非法时关闭式失败，不猜测 ``sys.path`` 或候选目录。
    """

    mounts = tuple(
        sorted(
            (
                _compile_mount(package, editable_source=editable_source)
                for package in packages
            ),
            key=lambda item: item.package_id,
        )
    )
    editable_items = [item for item in mounts if item.editable]
    if len(editable_items) != 1:
        raise ValueError("完整候选代必须且只能包含一个可编辑软件包来源")
    catalog_payload = {
        "dependencyRevision": dependency_revision,
        "packages": [
            {
                "packageId": item.package_id,
                "contentDigest": item.content_digest,
                "catalogDigest": item.catalog_digest,
                "editable": item.editable,
            }
            for item in mounts
        ],
    }
    catalog_revision = _sha256_json(catalog_payload)
    mount_revision = _sha256_json(
        {
            "catalogRevision": catalog_revision,
            "mounts": [
                {
                    "packageId": item.package_id,
                    "importRootUri": item.import_root.as_uri(),
                    "packageRootUri": item.package_root.as_uri(),
                }
                for item in mounts
            ],
        }
    )
    return WorkspacePackageMountProjection(
        editable_package_id=editable_items[0].package_id,
        dependency_revision=dependency_revision,
        catalog_revision=catalog_revision,
        mount_revision=mount_revision,
        items=mounts,
    )


def _compile_mount(
    package: PackageCatalogSource,
    *,
    editable_source: WorkspaceSource,
) -> WorkspacePackageMount:
    """把一个已验证配对转换为精确源码挂载。"""

    if not isinstance(package, PackageCatalogSource):
        raise TypeError("工作区软件包挂载只能来自 PackageCatalogSource")
    if not isinstance(package.catalog, PackageCatalog):
        raise TypeError("工作区软件包挂载缺少 PackageCatalog")
    package_root = package.import_root / package.catalog.import_package
    return WorkspacePackageMount(
        package_id=package.catalog.import_package,
        distribution_name=package.catalog.distribution.name,
        version=package.catalog.distribution.version,
        namespace=package.catalog.namespace,
        editable=package.source.root == editable_source.root,
        source_kind=package.source.source_kind,
        import_root=package.import_root,
        package_root=package_root,
        content_digest=package.catalog.content_digest,
        catalog_digest=package.catalog.catalog_digest,
    )


def _sha256_json(value: object) -> str:
    """计算仅依赖 JSON 事实的稳定摘要。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "WorkspacePackageMount",
    "WorkspacePackageMountProjection",
    "compile_workspace_package_mount_projection",
]
