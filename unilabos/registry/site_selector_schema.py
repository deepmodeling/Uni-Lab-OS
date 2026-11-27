"""库位关系（``MaterialRole.site_picker(...)``）的静态跨字段校验。

库位参数注解为 ``SiteSlot``，它与物料参数的关系写在 ``@action(material_contract=MaterialRole.site_picker(...))``：库位属于
哪个物料参数（``owner``）、放进去的是哪个物料参数（``occupant``）、已占用库位的显示 / 可选策略。注册表
把它写进库位参数 schema 的 ``x-unilabos-site-selector`` 扩展；本模块在生成 schema 时核对关系指向的参数。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class SiteSelectorSchemaError(ValueError):
    """库位关系不符合规范。"""

    def __init__(self, path: str, message: str) -> None:
        """保存稳定错误位置与中文诊断。

        参数说明：``path`` 是参数关系的 JSON Pointer；``message`` 解释失败原因。返回：无；构造异常对象。
        """

        super().__init__(message)
        self.path = path
        self.message = message


def validate_site_selector_relations(
    parameters: Sequence[Mapping[str, Any]],
    selectors: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    device_parameters: Any = (),
) -> None:
    """验证库位关系的跨字段引用。

    参数说明：``parameters`` 是严格解析出的动作输入（``{"name", "schema"}``）；``selectors`` 把库位参数名
    映射到 ``x-unilabos-site-selector`` 扩展；``device_parameters`` 是注解为 ``DeviceSlot`` 的参数名
    （``owner`` 可以指向它，表示所属设备的根节点）。``owner`` 为 ``None`` 表示执行设备的根节点。
    返回：无。异常：引用库位参数自身、``owner`` 不是单个 ``ResourceSlot`` / ``DeviceSlot`` 参数、``occupant``
    不是单个 ``ResourceSlot`` 参数时抛出 ``SiteSelectorSchemaError``。
    """

    device_parameters = set(device_parameters or ())

    # ``parameter_by_name`` 是动作输入字段权威视图，用于拒绝名字猜测与未知引用。
    parameter_by_name = {
        str(parameter.get("name")): (index, parameter)
        for index, parameter in enumerate(parameters)
    }
    for site_name, selector in selectors:
        # ``site_index`` 把跨字段诊断稳定定位到真实动作参数序号。
        site_index = parameter_by_name.get(site_name, (-1, {}))[0]
        for relation_name in ("owner", "occupant"):
            referenced_name = selector.get(relation_name)
            if referenced_name is None:
                continue
            relation_path = f"/parameters/{site_index}/site_selector/{relation_name}"
            keyword = "of" if relation_name == "owner" else "for_material"
            if referenced_name == site_name:
                raise SiteSelectorSchemaError(
                    relation_path,
                    "MaterialRole.site_picker 不能把库位参数自身作为 of / for_material",
                )
            if relation_name == "owner" and referenced_name in device_parameters:
                continue
            referenced_entry = parameter_by_name.get(referenced_name)
            if referenced_entry is None:
                raise SiteSelectorSchemaError(
                    relation_path,
                    f"MaterialRole.site_picker 的 {keyword}={referenced_name!r} 必须是注解为 ResourceSlot"
                    + (" 或 DeviceSlot" if relation_name == "owner" else "") + " 的参数",
                )
            # ``referenced`` 是 owner/occupant 指向的动作输入合同。
            referenced = referenced_entry[1]
            schema = referenced.get("schema")
            if not isinstance(schema, Mapping) or not _is_single_resource_slot(schema):
                raise SiteSelectorSchemaError(
                    relation_path,
                    f"MaterialRole.site_picker 的 {keyword}={referenced_name!r} 必须是单个 ResourceSlot"
                    + (" 或 DeviceSlot" if relation_name == "owner" else "") + " 参数",
                )


def _is_single_resource_slot(value_schema: Mapping[str, Any]) -> bool:
    """判断字段是否传递一个单独物料占位符（ResourceSlot）。

    参数说明：``value_schema`` 是规范工作流值模式。返回：必填或可空的单物料
    占位符为真，数组和其他对象为假。
    """

    if value_schema.get("$slot") == "ResourceSlot":
        return True
    members = value_schema.get("anyOf")
    if not isinstance(members, list) or len(members) != 2:
        return False
    non_null = [
        member
        for member in members
        if isinstance(member, Mapping) and member.get("type") != "null"
    ]
    return len(non_null) == 1 and non_null[0].get("$slot") == "ResourceSlot"


__all__ = [
    "SiteSelectorSchemaError",
    "validate_site_selector_relations",
]
