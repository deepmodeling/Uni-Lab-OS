"""把已编译包目录（PackageCatalog）与唯一授权来源保持在同一候选代。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ..package_catalog import (
    PackageCatalog,
    RegistryActivationPlan,
    WorkspaceSource,
)
from ..package_catalog.material_shapes import (
    compile_catalog_material_shape_bindings,
)


@dataclass(frozen=True, slots=True)
class PackageCatalogSource:
    """保存一个已编译包目录（PackageCatalog）及其显式来源根。"""

    source: WorkspaceSource
    catalog: PackageCatalog

    def __post_init__(self) -> None:
        """关闭式校验来源与包目录（PackageCatalog）的配对形状。

        参数：无；读取构造时传入的 ``source`` 与 ``catalog``。
        返回：无；合法配对保持不可变。
        异常：来源或目录类型错误时抛出 ``TypeError``，禁止候选代丢失可导入根。
        """

        if not isinstance(self.source, WorkspaceSource):
            raise TypeError("包目录来源必须是 WorkspaceSource")
        if not isinstance(self.catalog, PackageCatalog):
            raise TypeError("包目录来源必须配对 PackageCatalog")

    @property
    def import_root(self) -> Path:
        """返回作者 Python 包可导入所需的显式来源根。

        参数：无。
        返回：已由 ``WorkspaceSource`` 规范化和验证的绝对工作区根。
        异常：无；来源构造阶段已经完成路径安全校验。
        """

        return self.source.root


@dataclass(frozen=True, slots=True)
class MaterialShapeGeneration:
    """保存同一候选代的公共外形清单和模板精确绑定。"""

    # ``public_shapes`` 供 OS 本地只读 `/api/v1/material-shapes` 投影消费。
    public_shapes: tuple[dict[str, object], ...]
    # ``shapes_by_template`` 供显式模板同步按 FQID 写入 `model.shape`。
    shapes_by_template: Mapping[str, dict[str, object]]

    def __post_init__(self) -> None:
        """冻结模板绑定根映射，避免同步调用方增删候选成员。

        参数：无；读取构造时的 ``shapes_by_template``。返回：无；将根映射替换为
        按模板 FQID 排序的只读视图。异常：无；外形对象已由编译器验证为严格 JSON。
        """

        object.__setattr__(
            self,
            "shapes_by_template",
            MappingProxyType(dict(sorted(self.shapes_by_template.items()))),
        )


def _package_namespace(package: PackageCatalogSource) -> str:
    """读取包目录来源配对的规范社区命名空间排序键。

    参数：``package`` 是已验证的来源/包目录配对。
    返回：目录的稳定社区命名空间。
    异常：无。
    """

    return package.catalog.namespace


def compile_generation_material_shapes(
    packages: tuple[PackageCatalogSource, ...],
) -> tuple[dict[str, object], ...]:
    """从同一完整候选代聚合全部包的物料外形查询投影。

    参数：``packages`` 是主包和全部显式外部包的来源/目录不可变配对。
    返回：按发行包与外形身份稳定排序、容器彼此独立的物料外形元组。
    异常：配对类型、来源资产摘要、外形合同或跨包身份冲突无效时抛出
    ``TypeError``/``ValueError``，不返回部分投影。
    """

    return compile_material_shape_generation(packages).public_shapes


def compile_material_shape_generation(
    packages: tuple[PackageCatalogSource, ...],
) -> MaterialShapeGeneration:
    """一次编译完整候选代的公共 2.5D 外形及模板归属。

    参数：``packages`` 是主包和全部显式锁定外部包的来源/目录配对。返回：公共
    查询清单与 ``template_fqid -> shape`` 精确绑定组成的同代结果。异常：配对、
    外形身份、模板身份或跨包内容冲突无效时抛出 ``TypeError``/``ValueError``，
    不产生部分代。
    """

    # ``material_shapes_by_identity`` 让跨包外形聚合保持全有或全无和确定顺序；
    # ``material_shapes_by_template`` 则保留模板同步所需的唯一所有者关系。
    material_shapes_by_identity: dict[tuple[str, str], dict[str, object]] = {}
    material_shapes_by_template: dict[str, dict[str, object]] = {}
    for package in packages:
        if not isinstance(package, PackageCatalogSource):
            raise TypeError("完整候选代只能包含 PackageCatalogSource")
        for binding in compile_catalog_material_shape_bindings(
            package.source,
            package.catalog,
        ):
            # ``shape`` 是本次聚合独占的前端公共物料外形对象。
            shape = dict(binding.shape)
            bundle = shape.get("bundle")
            shape_id = shape.get("id")
            if not isinstance(bundle, str) or not isinstance(shape_id, str):
                raise TypeError("物料外形必须提供字符串 bundle 和 id")
            shape_identity = (bundle, shape_id)
            existing = material_shapes_by_identity.get(shape_identity)
            if existing is not None and existing != shape:
                raise ValueError(
                    "完整候选代存在冲突物料外形: "
                    f"{shape_identity[0]}/{shape_identity[1]}"
                )
            material_shapes_by_identity[shape_identity] = shape
            # ``published_shape`` 是待合并到资源模板（ResourceTemplate）的完整
            # 公共外形字段；设备包原有 format/entry 由模板同步阶段原样保留。
            published_shape = {
                "schema_version": "unilab.shape/v1",
                **shape,
            }
            prior_shape = material_shapes_by_template.get(binding.template_fqid)
            if prior_shape is not None and prior_shape != published_shape:
                raise ValueError(
                    "同一资源模板绑定不同物料外形: " + binding.template_fqid
                )
            material_shapes_by_template[binding.template_fqid] = published_shape
    return MaterialShapeGeneration(
        public_shapes=tuple(
            material_shapes_by_identity[identity]
            for identity in sorted(material_shapes_by_identity)
        ),
        shapes_by_template=material_shapes_by_template,
    )


def selected_package_import_roots(
    packages: tuple[PackageCatalogSource, ...],
    activation_plan: RegistryActivationPlan,
    *,
    editable_source: WorkspaceSource,
) -> tuple[Path, ...]:
    """计算主可编辑包与物理图（Graph）选中外部包的有限导入根。

    参数：``packages`` 是完整候选代；``activation_plan`` 是物理图有限选择结果；
    ``editable_source`` 是唯一允许编辑工作流源码（Workflow Source）的主工作区。
    返回：主工作区始终位于首位，随后仅包含至少一个选中设备或资源定义的外部
    包来源根；路径去重且外部包按命名空间稳定排序。
    异常：候选配对或激活计划类型无效、选中定义找不到所属显式包时抛出
    ``TypeError``/``ValueError``，禁止回退到环境包扫描。
    """

    if not isinstance(activation_plan, RegistryActivationPlan):
        raise TypeError("有限导入根需要 RegistryActivationPlan")
    if not isinstance(editable_source, WorkspaceSource):
        raise TypeError("主可编辑来源必须是 WorkspaceSource")
    # ``selected_definition_fqids`` 是允许作者实现进入运行导入阶段的完整有限集合。
    selected_definition_fqids = set(activation_plan.selected_definition_fqids)
    selected_packages: list[PackageCatalogSource] = []
    resolved_fqids: set[str] = set()
    for package in packages:
        if not isinstance(package, PackageCatalogSource):
            raise TypeError("完整候选代只能包含 PackageCatalogSource")
        package_fqids = {
            definition.fqid
            for definition in (
                *package.catalog.definitions.devices,
                *package.catalog.definitions.resources,
            )
        }
        package_selected_fqids = selected_definition_fqids & package_fqids
        if package_selected_fqids:
            selected_packages.append(package)
            resolved_fqids.update(package_selected_fqids)
    unresolved_fqids = selected_definition_fqids - resolved_fqids
    if unresolved_fqids:
        raise ValueError(
            "激活计划包含不属于完整候选代的定义: " + ", ".join(sorted(unresolved_fqids))
        )
    # ``ordered_roots`` 保留主包工作流创作资格，并只追加图实际选中的外部包。
    ordered_roots = [editable_source.root]
    for package in sorted(
        selected_packages,
        key=_package_namespace,
    ):
        if package.source.root not in ordered_roots:
            ordered_roots.append(package.source.root)
    return tuple(ordered_roots)


__all__ = [
    "MaterialShapeGeneration",
    "PackageCatalogSource",
    "compile_generation_material_shapes",
    "compile_material_shape_generation",
    "selected_package_import_roots",
]
