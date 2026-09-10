"""不可变包目录（PackageCatalog）及其规范摘要。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal

import rfc8785

JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | tuple["JSONValue", ...] | Mapping[str, "JSONValue"]


def _json_mapping_key(pair: tuple[Any, Any]) -> str:
    """读取待冻结 JSON 映射成员的稳定文本键。

    参数：``pair`` 是原始映射的一项键值二元组。
    返回：键的字符串表示，用于确定性排序。
    异常：键的 ``__str__`` 实现失败时传播原异常，禁止产生不完整目录。
    """

    return str(pair[0])


def _definition_fqid(definition: PackageDefinition) -> str:
    """读取包定义的规范全限定身份排序键。

    参数：``definition`` 是一个已经验证的设备、资源或工作流定义。
    返回：稳定 ``fqid``。
    异常：无。
    """

    return definition.fqid


def _asset_logical_path(asset: PackageAsset) -> str:
    """读取包资产的规范逻辑路径排序键。

    参数：``asset`` 是已经建立摘要的静态资产。
    返回：工作区相对逻辑路径。
    异常：无。
    """

    return asset.logical_path


def _freeze_json(value: Any) -> JSONValue:
    """把 JSON 值递归冻结，阻止目录发布后被调用者修改。

    参数：``value`` 是待进入包目录（PackageCatalog）的普通 JSON 值。
    返回：映射和数组均不可变的等价值。
    异常：遇到非 JSON 类型时抛出 ``TypeError``，关闭式拒绝不稳定实现对象。
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze_json(item)
                for key, item in sorted(value.items(), key=_json_mapping_key)
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(
        f"包目录（PackageCatalog）只接受 JSON 值，收到 {type(value).__name__}"
    )


def _thaw_json(value: JSONValue) -> Any:
    """把冻结 JSON 值转换为可序列化的新容器。

    参数：``value`` 是包目录（PackageCatalog）拥有的冻结值。
    返回：不共享内部容器的普通 JSON 值。
    异常：无；输入类型已在冻结阶段验证。
    """

    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PackageDiagnostic:
    """阻止或说明一次完整静态编译的结构化诊断。"""

    code: str
    message: str
    path: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """返回不含空可选字段的诊断字典。

        参数：无。
        返回：可写入命令行 JSON 的全新字典。
        异常：无。
        """

        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path is not None:
            result["path"] = self.path
        if self.line is not None:
            result["line"] = self.line
        return result


class PackageCompileError(RuntimeError):
    """表示完整包目录（PackageCatalog）无法安全产生。"""

    def __init__(self, diagnostics: tuple[PackageDiagnostic, ...]) -> None:
        """保存稳定诊断并构造不包含源码正文的错误消息。

        参数：``diagnostics`` 是阻止原子发布的全部已知诊断。
        返回：无。
        异常：无；构造函数本身不读取来源或产生副作用。
        """

        self.diagnostics = tuple(diagnostics)
        super().__init__(
            "; ".join(item.code for item in self.diagnostics)
            or "package_compile_failed"
        )


@dataclass(frozen=True, slots=True)
class PackageDistributionIdentity:
    """软件包发行身份及只读项目元数据。"""

    name: str
    normalized_name: str
    version: str
    description: str = ""
    license: str = ""
    homepage: str = ""
    requires_python: str = ""
    dependencies: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """返回确定性发行身份字典。

        参数：无。
        返回：依赖按编译时稳定顺序保存的全新字典。
        异常：无。
        """

        return {
            "dependencies": list(self.dependencies),
            "description": self.description,
            "homepage": self.homepage,
            "license": self.license,
            "name": self.name,
            "normalized_name": self.normalized_name,
            "requires_python": self.requires_python,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class PackageDefinition:
    """一个静态设备、资源或工作流定义。"""

    kind: Literal["device", "resource", "workflow"]
    id: str
    fqid: str
    module: str
    symbol: str
    declaring_file: str
    content_hash: str
    version: str = "1.0.0"
    title: str = ""
    description: str = ""
    details: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结定义的扩展详情。

        参数：无；使用构造时字段。
        返回：无；通过冻结赋值保存不可变 JSON。
        异常：详情包含非 JSON 对象时抛出 ``TypeError``。
        """

        object.__setattr__(self, "details", _freeze_json(self.details))

    def to_dict(self) -> dict[str, Any]:
        """返回可序列化且不共享内部容器的定义字典。

        参数：无。
        返回：包含稳定身份、源码证据和扩展详情的全新字典。
        异常：无。
        """

        return {
            "content_hash": self.content_hash,
            "declaring_file": self.declaring_file,
            "description": self.description,
            "details": _thaw_json(self.details),
            "fqid": self.fqid,
            "id": self.id,
            "kind": self.kind,
            "module": self.module,
            "symbol": self.symbol,
            "title": self.title,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class PackageDefinitionCatalog:
    """按定义种类稳定排序的完整静态定义集合。"""

    devices: tuple[PackageDefinition, ...] = ()
    resources: tuple[PackageDefinition, ...] = ()
    workflows: tuple[PackageDefinition, ...] = ()

    def __post_init__(self) -> None:
        """按规范全限定身份排序三个定义集合。

        参数：无；使用构造时集合。
        返回：无；冻结后的顺序不依赖扫描线程或文件系统遍历顺序。
        异常：无。
        """

        for field_name in ("devices", "resources", "workflows"):
            # ``definitions`` 是某一类完整定义，按 FQID 保证规范序列化稳定。
            definitions = tuple(sorted(getattr(self, field_name), key=_definition_fqid))
            object.__setattr__(self, field_name, definitions)

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        """返回三类定义的普通 JSON 字典。

        参数：无。
        返回：每个定义均为全新容器的字典。
        异常：无。
        """

        return {
            "devices": [item.to_dict() for item in self.devices],
            "resources": [item.to_dict() for item in self.resources],
            "workflows": [item.to_dict() for item in self.workflows],
        }


@dataclass(frozen=True, slots=True)
class PackageAsset:
    """软件包中可按摘要安全读取的一项静态资产。"""

    logical_path: str
    digest: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        """返回资产身份和完整性证据字典。

        参数：无。
        返回：新的普通字典。
        异常：无。
        """

        return {
            "digest": self.digest,
            "logical_path": self.logical_path,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class PackageCatalog:
    """一次完整静态编译产生的不可变包目录（PackageCatalog）。"""

    schema_version: Literal["1"]
    distribution: PackageDistributionIdentity
    import_package: str
    namespace: str
    definitions: PackageDefinitionCatalog
    assets: tuple[PackageAsset, ...]
    content_digest: str
    catalog_digest: str

    def __post_init__(self) -> None:
        """稳定排序资产集合。

        参数：无；使用构造时资产。
        返回：无。
        异常：无。
        """

        object.__setattr__(
            self,
            "assets",
            tuple(sorted(self.assets, key=_asset_logical_path)),
        )

    @classmethod
    def create(
        cls,
        *,
        distribution: PackageDistributionIdentity,
        import_package: str,
        namespace: str,
        definitions: PackageDefinitionCatalog,
        assets: tuple[PackageAsset, ...],
        content_digest: str,
    ) -> PackageCatalog:
        """创建目录并从规范内容计算目录摘要。

        参数：``distribution`` 是发行身份；``import_package`` 是唯一导入包；
        ``namespace`` 是社区命名空间；``definitions`` 是完整静态定义；``assets`` 是
        完整资产；``content_digest`` 是来源内容摘要。
        返回：带稳定 ``catalog_digest`` 的不可变目录。
        异常：任一详情无法规范 JSON 序列化时传播 ``TypeError``。
        """

        candidate = cls(
            schema_version="1",
            distribution=distribution,
            import_package=import_package,
            namespace=namespace,
            definitions=definitions,
            assets=assets,
            content_digest=content_digest,
            catalog_digest="",
        )
        # ``catalog_digest`` 排除自身，避免递归身份并允许独立复算。
        catalog_digest = (
            "sha256:"
            + hashlib.sha256(
                candidate._canonical_bytes(include_catalog_digest=False)
            ).hexdigest()
        )
        return replace(candidate, catalog_digest=catalog_digest)

    def to_dict(self, *, include_catalog_digest: bool = True) -> dict[str, Any]:
        """返回规范目录的普通 JSON 对象。

        参数：``include_catalog_digest`` 决定是否包含目录自身摘要。
        返回：不共享内部容器的完整目录字典。
        异常：无。
        """

        result: dict[str, Any] = {
            "assets": [item.to_dict() for item in self.assets],
            "content_digest": self.content_digest,
            "definitions": self.definitions.to_dict(),
            "distribution": self.distribution.to_dict(),
            "import_package": self.import_package,
            "namespace": self.namespace,
            "schema_version": self.schema_version,
        }
        if include_catalog_digest:
            result["catalog_digest"] = self.catalog_digest
        return result

    def _canonical_bytes(self, *, include_catalog_digest: bool) -> bytes:
        """生成 RFC 8785 规范 JSON 字节。

        参数：``include_catalog_digest`` 决定是否序列化目录自身摘要。
        返回：与绝对路径、修改时间和字典插入顺序无关的字节。
        异常：目录值不符合 JSON 规范时由 ``rfc8785`` 抛出异常。
        """

        return rfc8785.dumps(
            self.to_dict(include_catalog_digest=include_catalog_digest)
        )

    def to_canonical_bytes(self) -> bytes:
        """生成包含目录摘要的规范 JSON 字节。

        参数：无。
        返回：可跨来源比较和持久化的目录字节。
        异常：目录值不符合 JSON 规范时由 ``rfc8785`` 抛出异常。
        """

        return self._canonical_bytes(include_catalog_digest=True)


__all__ = [
    "PackageAsset",
    "PackageCatalog",
    "PackageCompileError",
    "PackageDefinition",
    "PackageDefinitionCatalog",
    "PackageDiagnostic",
    "PackageDistributionIdentity",
]
