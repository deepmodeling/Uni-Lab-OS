"""把工作区装饰器绑定的轻量物料外形投影为前端公共合同。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import yaml

from .model import PackageCatalog, PackageDefinition
from .sources import WorkspaceSource


class WorkspaceMaterialPlan(Protocol):
    """物料（Material）外形编译所需的工作区只读 Interface。"""

    source: WorkspaceSource
    distribution_name: str
    import_package: str
    package_directory: Path


_PART_TYPES = frozenset(
    {"box", "slab", "cylinder", "lathe", "disc", "rect", "edge", "grid", "sites"}
)
_STYLE_TOKENS = frozenset(
    {
        "plain",
        "frame",
        "plate",
        "board",
        "body",
        "column",
        "module",
        "shell",
        "beam",
        "shaft",
        "probe",
        "deck",
        "gear",
        "motor",
        "foot",
        "glass",
        "cap",
        "hole",
        "bore",
        "port",
        "seat",
        "pad",
        "rim",
        "hairline",
    }
)
_SITE_GENERATORS = frozenset(
    {"open-rack", "stack-shelves", "site-holes", "site-markers"}
)


@dataclass(frozen=True, slots=True)
class CatalogMaterialShapeBinding:
    """保存一个资源模板（ResourceTemplate）与同代编译外形的精确绑定。"""

    # ``template_fqid`` 是包目录（PackageCatalog）分配的跨包稳定模板业务身份。
    template_fqid: str
    # ``shape`` 是已完成同代摘要和公共图元合同校验的 2.5D JSON 对象。
    shape: dict[str, Any]


def compile_catalog_material_shapes(
    source: WorkspaceSource,
    catalog: PackageCatalog,
) -> tuple[dict[str, Any], ...]:
    """从同代包目录（PackageCatalog）编译静态物料外形。

    参数：``source`` 是本次目录编译唯一授权的工作区来源；``catalog`` 是同一来源
    的冻结目录代，提供声明文件、静态 ``registry_entry.model`` 和资产摘要。
    返回：按 ``bundle/id`` 排序且容器互不共享的前端公共外形 tuple。
    异常：来源类型、目录声明路径、外形绑定、资产摘要、YAML 或公共外形合同无效
    时抛出 ``TypeError``/``ValueError``，不读取旧注册表 AST ``file_path``，也不
    返回部分目录。
    """

    # ``shapes_by_identity`` 按发行包与外形 ID 保持跨定义幂等和冲突检测。
    shapes_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for template_binding in compile_catalog_material_shape_bindings(source, catalog):
        # ``shape`` 与模板绑定容器隔离，公共查询可独立按 bundle/id 去重。
        shape = _json_object(template_binding.shape, "公开外形")
        shape_identity = (shape["bundle"], shape["id"])
        existing = shapes_by_identity.get(shape_identity)
        if existing is not None and existing != shape:
            raise ValueError(
                "同一工作区外形身份指向不同内容: "
                f"{shape_identity[0]}/{shape_identity[1]}"
            )
        shapes_by_identity[shape_identity] = shape
    return tuple(
        _json_object(shapes_by_identity[identity], "公开外形")
        for identity in sorted(shapes_by_identity)
    )


def compile_catalog_material_shape_bindings(
    source: WorkspaceSource,
    catalog: PackageCatalog,
) -> tuple[CatalogMaterialShapeBinding, ...]:
    """编译一代包目录中每个模板拥有的完整 2.5D 外形。

    参数：``source`` 是本代唯一工作区文件来源；``catalog`` 是同一来源的不可变
    包目录（PackageCatalog）。返回：按模板 FQID 排序的精确绑定；同一外形可由
    多个模板复用，但每个模板仍保留独立绑定。异常：来源、资产摘要、绑定路径、
    YAML 或公共外形合同无效时抛出 ``TypeError``/``ValueError``，不返回部分结果。
    """

    if not isinstance(source, WorkspaceSource):
        raise TypeError("source 必须是 WorkspaceSource")
    if not isinstance(catalog, PackageCatalog):
        raise TypeError("catalog 必须是 PackageCatalog")
    # ``catalog_assets`` 是本代内容摘要索引，用于拒绝目录编译后的来源漂移。
    catalog_assets = {asset.logical_path: asset.digest for asset in catalog.assets}
    bindings: list[CatalogMaterialShapeBinding] = []
    for definition in (
        *catalog.definitions.devices,
        *catalog.definitions.resources,
    ):
        shape_binding = _catalog_shape_binding(definition)
        if shape_binding is None:
            continue
        # ``logical_shape_path`` 只由同代定义的规范声明文件位置解析。
        logical_shape_path = _catalog_shape_path(
            catalog=catalog,
            definition=definition,
            binding=shape_binding,
        )
        shape_bytes = source.read_bytes(logical_shape_path)
        expected_digest = catalog_assets.get(logical_shape_path)
        actual_digest = "sha256:" + hashlib.sha256(shape_bytes).hexdigest()
        if expected_digest is None or expected_digest != actual_digest:
            raise ValueError(
                f"外形资产不属于当前包目录（PackageCatalog）代或摘要漂移: {logical_shape_path}"
            )
        # ``shape`` 是从摘要已匹配的同一次字节读取编译出的前端公共合同。
        shape = _load_public_shape_bytes(
            shape_bytes,
            logical_shape_path=logical_shape_path,
            bundle=catalog.distribution.name,
        )
        bindings.append(
            CatalogMaterialShapeBinding(
                template_fqid=definition.fqid,
                shape=_json_object(shape, "模板物料外形"),
            )
        )
    return tuple(sorted(bindings, key=lambda binding: binding.template_fqid))


def compile_workspace_material_shapes(
    startup_plan: WorkspaceMaterialPlan,
    registry: Any,
) -> tuple[dict[str, Any], ...]:
    """编译工作区装饰器显式绑定的静态物料外形。

    参数：``startup_plan`` 固定唯一工作区来源与发行包身份；``registry`` 提供已由
    AST 静态发现的设备和资源定义。返回：按 ``bundle/id`` 排序且容器互不共享的
    前端公共外形 tuple。异常：注册表形状、绑定格式、包内路径、YAML 或外形合同
    无效时抛出 ``TypeError``/``ValueError``，不返回部分目录。
    """

    _validate_workspace_material_plan(startup_plan)
    try:
        # ``definitions`` 是注册表（Registry）一次静态扫描后的设备与资源定义全集。
        definitions = (
            *registry.obtain_registry_device_info(),
            *registry.obtain_registry_resource_info(),
        )
    except AttributeError:
        raise TypeError("registry 必须提供设备和资源定义读取接口") from None

    # ``shapes_by_identity`` 以发行包和外形 ID 去重，拒绝同身份内容漂移。
    shapes_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for definition in definitions:
        if not isinstance(definition, Mapping):
            raise TypeError("注册表定义必须是对象")
        declaration_file = _workspace_declaration_file(startup_plan, definition)
        if declaration_file is None:
            continue
        shape_binding = _shape_binding(definition.get("model"))
        if shape_binding is None:
            continue
        # ``logical_shape_path`` 只相对声明装饰器的 Python 文件解析。
        logical_shape_path = _logical_shape_path(
            startup_plan,
            declaration_file,
            shape_binding,
        )
        shape = _load_public_shape(
            startup_plan,
            logical_shape_path,
            bundle=startup_plan.distribution_name,
        )
        shape_identity = (shape["bundle"], shape["id"])
        existing = shapes_by_identity.get(shape_identity)
        if existing is not None and existing != shape:
            raise ValueError(
                "同一工作区外形身份指向不同内容: "
                f"{shape_identity[0]}/{shape_identity[1]}"
            )
        shapes_by_identity[shape_identity] = shape

    return tuple(
        _json_object(shapes_by_identity[identity], "公开外形")
        for identity in sorted(shapes_by_identity)
    )


def _catalog_shape_binding(
    definition: PackageDefinition,
) -> Mapping[str, Any] | None:
    """从同代目录定义读取外形绑定。

    参数：``definition`` 是设备或资源的不可变目录定义。
    返回：未声明外形时为 ``None``，否则返回冻结 ``model.shape`` 映射。
    异常：定义详情缺少规范 ``registry_entry`` 对象时抛出 ``TypeError``；外形绑定
    结构错误由共享 ``_shape_binding`` 校验并传播。
    """

    registry_entry = definition.details.get("registry_entry")
    if not isinstance(registry_entry, Mapping):
        raise TypeError(f"目录定义缺少 registry_entry: {definition.fqid}")
    return _shape_binding(registry_entry.get("model"))


def _catalog_shape_path(
    *,
    catalog: PackageCatalog,
    definition: PackageDefinition,
    binding: Mapping[str, Any],
) -> str:
    """相对目录定义声明文件解析安全的外形资产逻辑路径。

    参数：``catalog`` 提供规范导入包边界；``definition`` 提供同代声明文件；
    ``binding`` 提供外形资产相对入口。
    返回：相对工作区根的 POSIX 逻辑路径。
    异常：声明文件或入口为空、绝对、越出导入包，或包含父目录语义时抛出
    ``ValueError``。
    """

    declaration_path = PurePosixPath(definition.declaring_file)
    package_prefix = PurePosixPath(catalog.import_package)
    if (
        declaration_path.is_absolute()
        or len(declaration_path.parts) < 2
        or declaration_path.parts[:1] != package_prefix.parts
        or any(part in {"", ".", ".."} for part in declaration_path.parts)
    ):
        raise ValueError("包目录（PackageCatalog）声明文件不在规范导入包内")
    entry = binding.get("entry")
    if not isinstance(entry, str) or not entry or "\\" in entry:
        raise ValueError("工作区外形资产入口必须是非空 POSIX 相对路径")
    relative_entry = PurePosixPath(entry)
    if relative_entry.is_absolute() or any(
        part in {"", ".", ".."} for part in relative_entry.parts
    ):
        raise ValueError("工作区外形资产入口不得包含绝对或父目录语义")
    logical_shape_path = declaration_path.parent.joinpath(relative_entry)
    if logical_shape_path.parts[:1] != package_prefix.parts:
        raise ValueError("工作区外形资产入口必须位于导入包内")
    return logical_shape_path.as_posix()


def _workspace_declaration_file(
    startup_plan: WorkspaceMaterialPlan,
    definition: Mapping[str, Any],
) -> Path | None:
    """取得属于当前工作区导入包的装饰器声明文件。

    参数：``startup_plan`` 固定包根；``definition`` 是一条注册表定义。
    返回：工作区内声明文件；其他内置或外部定义返回 ``None``。
    异常：声称属于包内但路径不安全或不存在时抛出 ``ValueError``。
    """

    raw_file_path = definition.get("file_path")
    if not isinstance(raw_file_path, str) or not raw_file_path:
        return None
    declaration_file = Path(raw_file_path)
    if not declaration_file.is_absolute():
        return None
    try:
        declaration_file.relative_to(startup_plan.package_directory)
    except ValueError:
        return None
    try:
        logical_declaration = declaration_file.relative_to(
            startup_plan.source.root
        ).as_posix()
    except ValueError as error:
        raise ValueError("工作区装饰器声明文件越过授权根") from error
    if not startup_plan.source.has_file(logical_declaration):
        raise ValueError("工作区装饰器声明文件不存在")
    return declaration_file


def _shape_binding(model: object) -> Mapping[str, Any] | None:
    """读取一个模型声明中的轻量外形绑定。

    参数：``model`` 是注册表模型字段。返回：没有外形时为 ``None``，否则返回绑定。
    异常：已声明外形但结构或格式不受支持时抛出 ``ValueError``。
    """

    if not isinstance(model, Mapping):
        return None
    binding = model.get("shape")
    if binding is None:
        return None
    if not isinstance(binding, Mapping):
        raise TypeError("工作区外形绑定必须是对象")
    if binding.get("format") != "unilab.shape/v1":
        raise ValueError("工作区外形绑定格式必须是 unilab.shape/v1")
    return binding


def _logical_shape_path(
    startup_plan: WorkspaceMaterialPlan,
    declaration_file: Path,
    binding: Mapping[str, Any],
) -> str:
    """把外形入口解析成工作区授权来源的安全逻辑路径。

    参数：``startup_plan`` 提供包边界；``declaration_file`` 是绑定声明文件；
    ``binding`` 包含相对入口。返回：相对工作区根的 POSIX 路径。
    异常：入口为空、绝对、含反斜杠或父目录语义时抛出 ``ValueError``。
    """

    entry = binding.get("entry")
    if not isinstance(entry, str) or not entry or "\\" in entry:
        raise ValueError("工作区外形资产入口必须是非空 POSIX 相对路径")
    relative_entry = PurePosixPath(entry)
    if relative_entry.is_absolute() or any(
        part in {"", ".", ".."} for part in relative_entry.parts
    ):
        raise ValueError("工作区外形资产入口不得包含绝对或父目录语义")
    declaration_directory = declaration_file.parent.relative_to(
        startup_plan.source.root
    )
    logical_shape = PurePosixPath(declaration_directory.as_posix()).joinpath(
        relative_entry
    )
    package_prefix = PurePosixPath(startup_plan.import_package)
    if logical_shape.parts[:1] != package_prefix.parts:
        raise ValueError("工作区外形资产入口必须位于导入包内")
    return logical_shape.as_posix()


def _load_public_shape(
    startup_plan: WorkspaceMaterialPlan,
    logical_shape_path: str,
    *,
    bundle: str,
) -> dict[str, Any]:
    """读取并校验一份外形 YAML，生成前端公共 wire 对象。

    参数：``startup_plan`` 提供安全文件来源；``logical_shape_path`` 是包内逻辑路径；
    ``bundle`` 是发行包身份。返回：完整公共外形对象。
    异常：编码、YAML、版本或外形字段无效时抛出 ``ValueError``。
    """

    return _load_public_shape_bytes(
        startup_plan.source.read_bytes(logical_shape_path),
        logical_shape_path=logical_shape_path,
        bundle=bundle,
    )


def _load_public_shape_bytes(
    shape_bytes: bytes,
    *,
    logical_shape_path: str,
    bundle: str,
) -> dict[str, Any]:
    """从一次固定读取的资产字节编译前端公共物料外形。

    参数：``shape_bytes`` 是已完成同代摘要校验的 YAML 字节；
    ``logical_shape_path`` 是仅用于诊断的包内逻辑路径；``bundle`` 是发行包身份。
    返回：完成版本和字段校验的公共外形对象。
    异常：编码、YAML、版本或外形字段无效时抛出 ``ValueError``/``TypeError``。
    """

    try:
        manifest = yaml.safe_load(shape_bytes.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"工作区外形 YAML 无效: {logical_shape_path}") from error
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        raise ValueError(f"工作区外形清单版本无效: {logical_shape_path}")
    raw_shape = manifest.get("shape")
    if not isinstance(raw_shape, Mapping):
        raise TypeError(f"工作区外形清单必须包含唯一 shape: {logical_shape_path}")
    return _public_shape(raw_shape, bundle=bundle)


def _public_shape(raw: Mapping[str, Any], *, bundle: str) -> dict[str, Any]:
    """把版本一外形声明转换成前端公共字段。

    参数：``raw`` 是 YAML 中的 shape 对象；``bundle`` 是发行包身份。
    返回：经过有限值与图元白名单校验的公共对象。
    异常：身份、匹配规则、外包尺寸或图元无效时抛出 ``ValueError``。
    """

    shape_id = _required_string(raw.get("id"), "shape.id")
    raw_parts = raw.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ValueError(f"外形 {shape_id} 缺少 parts")
    for part in raw_parts:
        _validate_part(part)
    categories: list[str] = []
    category_tokens: list[str] = []
    applies_to = raw.get("applies_to")
    if not isinstance(applies_to, Iterable) or isinstance(
        applies_to, (str, bytes, Mapping)
    ):
        applies_to = ()
    for rule in applies_to:
        if not isinstance(rule, Mapping):
            continue
        if rule.get("category"):
            categories.append(_normalize_category(str(rule["category"])))
        if rule.get("category_contains"):
            category_tokens.append(_normalize_category(str(rule["category_contains"])))
    if not categories and not category_tokens:
        raise ValueError(f"外形 {shape_id} 缺少 applies_to")
    result: dict[str, Any] = {
        "id": shape_id,
        "bundle": bundle,
        "categories": categories,
        "categoryTokens": category_tokens,
        "priority": int(raw.get("priority") or 0),
        "units": str(raw.get("units") or "mm"),
        "shadow": str(raw.get("shadow") or "box"),
        "sort": str(raw.get("sort") or "center"),
        "parts": [_json_object(part, "shape.parts") for part in raw_parts],
    }
    display_name = raw.get("display_name")
    if isinstance(display_name, str) and display_name:
        result["displayName"] = display_name
    envelope = raw.get("envelope")
    if isinstance(envelope, list) and len(envelope) == 3:
        result["envelope"] = [
            _finite_number(value, "shape.envelope") for value in envelope
        ]
    return result


def _validate_part(raw: object, *, nested: bool = False) -> None:
    """校验一条前端外形图元只使用受支持的关闭集合。

    参数：``raw`` 是可疑图元；``nested`` 标记当前图元是否位于 grid 内。
    返回：无。异常：图元、样式、生成器或嵌套结构无效时抛出 ``ValueError``。
    """

    if not isinstance(raw, Mapping):
        raise TypeError("外形图元必须是对象")
    part_type = str(raw.get("type") or "")
    if part_type not in _PART_TYPES:
        raise ValueError(f"外形图元 type 无效: {part_type}")
    style = str(raw.get("style") or "plain")
    if style not in _STYLE_TOKENS:
        raise ValueError(f"外形图元 style 无效: {style}")
    if part_type == "sites" and raw.get("generator") not in _SITE_GENERATORS:
        raise ValueError(f"外形 sites generator 无效: {raw.get('generator')}")
    if part_type == "grid":
        if nested:
            raise ValueError("外形 grid 不得嵌套 grid")
        _validate_part(raw.get("part"), nested=True)


def _required_string(value: object, field: str) -> str:
    """读取非空字符串字段。

    参数：``value`` 是可疑值；``field`` 是错误字段名。返回：去除边缘空白的字符串。
    异常：值不是非空字符串时抛出 ``ValueError``。
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _normalize_category(value: str) -> str:
    """规范化前端外形匹配分类。

    参数：``value`` 是声明分类。返回：小写连字符形式。异常：无。
    """

    return value.strip().replace("_", "-").casefold()


def _finite_number(value: object, field: str) -> float:
    """把外部数值转换成有限浮点数。

    参数：``value`` 是可疑数值；``field`` 是错误字段名。返回：有限 ``float``。
    异常：布尔值、非数值或无穷值抛出 ``ValueError``。
    """

    if isinstance(value, bool):
        raise TypeError(f"{field} 必须是有限数值")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{field} 必须是有限数值") from error
    if not math.isfinite(result):
        raise ValueError(f"{field} 必须是有限数值")
    return result


def _json_object(value: object, field: str) -> dict[str, Any]:
    """把映射复制成不共享容器的严格 JSON 对象。

    参数：``value`` 是可疑映射；``field`` 是错误字段名。返回：JSON 往返后的字典。
    异常：非映射、非 JSON 值或非有限数值抛出 ``ValueError``。
    """

    if not isinstance(value, Mapping):
        raise TypeError(f"{field} 必须是对象")
    try:
        return json.loads(
            json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} 必须是严格 JSON 对象") from error


def _validate_workspace_material_plan(startup_plan: object) -> None:
    """关闭式验证物料（Material）外形编译所需工作区计划形状。

    参数：``startup_plan`` 是高层工作区运行时（Workspace Runtime）提供的计划。
    返回：无；结构具备安全来源、发行身份、导入包和包目录时完成。
    异常：缺少任一必需只读事实时抛出 ``TypeError``，不反向依赖具体运行时类。
    """

    # ``required_attributes`` 是低层外形编译使用的完整工作区计划事实集。
    required_attributes = (
        "source",
        "distribution_name",
        "import_package",
        "package_directory",
    )
    if not all(hasattr(startup_plan, name) for name in required_attributes):
        raise TypeError("startup_plan 必须提供工作区物料编译计划 Interface")
    if not isinstance(startup_plan.source, WorkspaceSource):
        raise TypeError("startup_plan.source 必须是 WorkspaceSource")


__all__ = [
    "CatalogMaterialShapeBinding",
    "compile_catalog_material_shape_bindings",
    "compile_catalog_material_shapes",
    "compile_workspace_material_shapes",
]
