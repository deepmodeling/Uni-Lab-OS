"""包分发（Package Distribution）的依赖锁模型与稳定身份。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import rfc8785

from ..package_catalog import PackageCatalog, WorkspaceSource

# 声明文件与规范锁文件共同保存同一完整软件包依赖代际，必须成对读写。
DEPENDENCY_DECLARATION_FILE = "unilabos.packages.yaml"
DEPENDENCY_LOCK_FILE = "unilabos.packages.lock.json"
# 变更互斥文件只协调同一主工作区的依赖写入，不属于可发布依赖事实。
DEPENDENCY_MUTATION_GUARD = ".unilabos.packages.mutation.lock"


class PackageDependencyError(RuntimeError):
    """表示显式软件包依赖不能被安全解析、验证或发布。"""


@dataclass(frozen=True, slots=True)
class ResolvedPackageSource:
    """保存包分发（Package Distribution）已经核验的来源与目录配对。"""

    source: WorkspaceSource
    catalog: PackageCatalog

    def __post_init__(self) -> None:
        """关闭式校验解析结果不会丢失安全来源或包目录。

        参数：无；读取构造时传入的 ``source`` 与 ``catalog``。
        返回：无；合法配对保持不可变。
        异常：来源或目录类型错误时抛出 ``TypeError``，禁止高层激活不完整结果。
        """

        if not isinstance(self.source, WorkspaceSource):
            raise TypeError("解析软件包来源必须是 WorkspaceSource")
        if not isinstance(self.catalog, PackageCatalog):
            raise TypeError("解析软件包来源必须配对 PackageCatalog")


@dataclass(frozen=True, slots=True)
class LockedPackage:
    """一项已解析且可重放校验的软件包依赖事实。"""

    # 以下字段共同标识锁定发行、显式来源、目录摘要和完整定义集合。
    distribution_name: str
    normalized_name: str
    namespace: str
    version: str
    source: str
    source_kind: Literal["workspace"]
    catalog_digest: str
    content_digest: str
    definition_fqids: tuple[str, ...] = ()

    @classmethod
    def from_catalog(
        cls,
        *,
        catalog: PackageCatalog,
        source: str,
    ) -> LockedPackage:
        """从已完整编译的目录建立一项锁定依赖。

        参数：``catalog`` 是已验证的包目录（PackageCatalog）；``source`` 是
        相对主工作区保存的显式来源路径。
        返回：包含发行身份、目录摘要和完整定义身份集合的不可变锁条目。
        异常：无；目录字段已由统一静态编译器关闭式验证。
        """

        # ``definition_fqids`` 证明锁定目录覆盖完整定义，而不是部署图的有限子集。
        definition_fqids = tuple(
            sorted(
                item.fqid
                for item in (
                    *catalog.definitions.devices,
                    *catalog.definitions.resources,
                    *catalog.definitions.workflows,
                )
            )
        )
        return cls(
            distribution_name=catalog.distribution.name,
            normalized_name=catalog.distribution.normalized_name,
            namespace=catalog.namespace,
            version=catalog.distribution.version,
            source=source,
            source_kind="workspace",
            catalog_digest=catalog.catalog_digest,
            content_digest=catalog.content_digest,
            definition_fqids=definition_fqids,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LockedPackage:
        """解析并验证一个锁文件条目。

        参数：``value`` 是 JSON 解码后的单条依赖对象。
        返回：形状与值域均已验证的不可变锁条目。
        异常：字段缺失、类型错误或来源种类不支持时抛出
        ``PackageDependencyError``，禁止把损坏锁解释成环境发现请求。
        """

        # ``required_strings`` 是一条依赖锁必须持久化的非空身份与摘要字段。
        required_strings = (
            "distribution_name",
            "normalized_name",
            "namespace",
            "version",
            "source",
            "source_kind",
            "catalog_digest",
            "content_digest",
        )
        if any(
            not isinstance(value.get(key), str) or not str(value[key]).strip()
            for key in required_strings
        ):
            raise PackageDependencyError("软件包依赖锁条目缺少非空字符串字段")
        if value["source_kind"] != "workspace":
            raise PackageDependencyError("当前只接受显式 workspace 软件包来源")
        # ``raw_fqids`` 是锁定代完整定义身份的未验证 JSON 投影。
        raw_fqids = value.get("definition_fqids", [])
        if not isinstance(raw_fqids, list) or any(
            not isinstance(item, str) or not item for item in raw_fqids
        ):
            raise PackageDependencyError("锁定定义身份必须是字符串数组")
        return cls(
            distribution_name=value["distribution_name"],
            normalized_name=value["normalized_name"],
            namespace=value["namespace"],
            version=value["version"],
            source=value["source"],
            source_kind="workspace",
            catalog_digest=value["catalog_digest"],
            content_digest=value["content_digest"],
            definition_fqids=tuple(sorted(raw_fqids)),
        )

    def to_dict(self) -> dict[str, Any]:
        """返回可规范序列化的锁条目。

        参数：无。
        返回：不共享内部容器的普通 JSON 字典。
        异常：无。
        """

        return {
            "catalog_digest": self.catalog_digest,
            "content_digest": self.content_digest,
            "definition_fqids": list(self.definition_fqids),
            "distribution_name": self.distribution_name,
            "namespace": self.namespace,
            "normalized_name": self.normalized_name,
            "source": self.source,
            "source_kind": self.source_kind,
            "version": self.version,
        }


def locked_package_sort_key(package: LockedPackage) -> tuple[str, str]:
    """读取锁定外部包的规范排序键。

    参数：``package`` 是已经验证的依赖锁条目。
    返回：社区命名空间与可移植来源组成的二元组。
    异常：无。
    """

    return package.namespace, package.source


@dataclass(frozen=True, slots=True)
class PackageDependencyLock:
    """主工作区当前完整、不可变的软件包依赖代际。"""

    # ``schema_version`` 与 ``packages`` 共同构成可重放的完整依赖代际。
    schema_version: Literal["1"] = "1"
    packages: tuple[LockedPackage, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """按规范命名空间排序并拒绝重复依赖身份。

        参数：无；读取构造字段。
        返回：无；将 ``packages`` 替换为稳定排序元组。
        异常：发行规范身份或命名空间重复时抛出 ``PackageDependencyError``。
        """

        # ``ordered`` 是按命名空间和来源确定性排序后的完整锁条目集合。
        ordered = tuple(sorted(self.packages, key=locked_package_sort_key))
        if len({item.normalized_name for item in ordered}) != len(ordered):
            raise PackageDependencyError("软件包依赖发行身份重复")
        if len({item.namespace for item in ordered}) != len(ordered):
            raise PackageDependencyError("软件包依赖命名空间重复")
        object.__setattr__(self, "packages", ordered)

    @classmethod
    def from_bytes(cls, raw: bytes) -> PackageDependencyLock:
        """从规范 JSON 读取软件包依赖锁。

        参数：``raw`` 是 ``unilabos.packages.lock.json`` 原始字节。
        返回：已完成字段和身份校验的不可变依赖代际。
        异常：编码、JSON、版本或条目无效时抛出 ``PackageDependencyError``。
        """

        try:
            # ``value`` 是锁文件 JSON 解码后的未验证根对象。
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise PackageDependencyError("软件包依赖锁不是合法 UTF-8 JSON") from error
        if not isinstance(value, dict) or value.get("schema_version") != "1":
            raise PackageDependencyError("软件包依赖锁版本无效")
        # ``raw_packages`` 是根对象中尚未验证的锁条目数组。
        raw_packages = value.get("packages")
        if not isinstance(raw_packages, list) or any(
            not isinstance(item, dict) for item in raw_packages
        ):
            raise PackageDependencyError("软件包依赖锁 packages 必须是对象数组")
        return cls(
            packages=tuple(LockedPackage.from_dict(item) for item in raw_packages)
        )

    def to_canonical_bytes(self) -> bytes:
        """输出稳定的软件包依赖锁 JSON。

        参数：无。
        返回：按 RFC 8785 规范化且末尾带换行的 UTF-8 字节。
        异常：无；内部字段已限制为 JSON 值。
        """

        return (
            rfc8785.dumps(
                {
                    "packages": [item.to_dict() for item in self.packages],
                    "schema_version": self.schema_version,
                }
            )
            + b"\n"
        )


__all__ = [
    "DEPENDENCY_DECLARATION_FILE",
    "DEPENDENCY_LOCK_FILE",
    "DEPENDENCY_MUTATION_GUARD",
    "LockedPackage",
    "PackageDependencyError",
    "PackageDependencyLock",
    "ResolvedPackageSource",
    "locked_package_sort_key",
]
