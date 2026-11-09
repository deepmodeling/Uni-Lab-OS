"""库位选择器（SiteSelector）注解的静态 AST 深模块。"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class SiteSelectorSchemaError(ValueError):
    """库位选择器（SiteSelector）源码或字段关系不符合规范。"""

    def __init__(self, path: str, message: str) -> None:
        """保存稳定错误位置与中文诊断。

        参数说明：``path`` 是动作注解或参数关系的 JSON Pointer；``message``
        解释关闭式失败原因。返回：无；构造异常对象。
        """

        super().__init__(message)
        self.path = path
        self.message = message


@dataclass(frozen=True, slots=True)
class ParsedSiteSelector:
    """一个不执行作者代码的规范库位选择器（SiteSelector）声明。"""

    owner: str
    occupant: str | None
    show_occupied: bool
    allow_occupied: bool

    def to_extension(self) -> dict[str, Any]:
        """生成动作 Schema 使用的稳定库位关系扩展。

        参数：无。返回：包含版本、owner/occupant 字段身份及占用展示策略的
        分离字典；不包含运行时库位（Site）UUID。
        """

        return {
            "version": 1,
            "owner": self.owner,
            "occupant": self.occupant,
            "show_occupied": self.show_occupied,
            "allow_occupied": self.allow_occupied,
        }


def parse_site_selector_call(
    call: ast.Call,
    value_schema: Mapping[str, Any],
    *,
    path: str,
) -> ParsedSiteSelector:
    """静态解析一个 ``SiteSelector(...)`` 注解调用。

    参数说明：``call`` 是未执行的调用 AST；``value_schema`` 是同一参数的规范
    值模式；``path`` 是诊断根路径。返回：不可变库位选择器声明。异常：位置参数、
    动态值、未知/重复字段或非字符串库位值均抛出 ``SiteSelectorSchemaError``。
    """

    if not _is_site_value_schema(value_schema):
        raise SiteSelectorSchemaError(path, "SiteSelector 只允许标记字符串库位参数")
    if call.args:
        raise SiteSelectorSchemaError(path, "SiteSelector 只接受命名参数")
    # ``values`` 是静态作者声明；仅接受四个闭集键，禁止 ** 展开和动态表达式。
    values: dict[str, Any] = {}
    allowed = {"owner", "occupant", "show_occupied", "allow_occupied"}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg not in allowed or keyword.arg in values:
            raise SiteSelectorSchemaError(path, "SiteSelector 包含未知或重复字段")
        try:
            values[keyword.arg] = ast.literal_eval(keyword.value)
        except (TypeError, ValueError):
            raise SiteSelectorSchemaError(
                f"{path}/{keyword.arg}",
                "SiteSelector 字段必须是静态字面量",
            ) from None
    owner = _required_name(values.get("owner"), path=f"{path}/owner")
    occupant = _optional_name(values.get("occupant"), path=f"{path}/occupant")
    show_occupied = _boolean(
        values.get("show_occupied", True),
        path=f"{path}/show_occupied",
    )
    allow_occupied = _boolean(
        values.get("allow_occupied", False),
        path=f"{path}/allow_occupied",
    )
    return ParsedSiteSelector(
        owner=owner,
        occupant=occupant,
        show_occupied=show_occupied,
        allow_occupied=allow_occupied,
    )


def validate_site_selector_relations(
    parameters: Sequence[Mapping[str, Any]],
    selectors: Sequence[tuple[str, ParsedSiteSelector]],
) -> None:
    """验证库位选择器（SiteSelector）的跨字段物料关系。

    参数说明：``parameters`` 是完整动作输入合同；``selectors`` 把库位参数名映射
    到已解析声明。返回：无。异常：owner/occupant 缺失、引用自身或没有指向单个
    物料占位符（ResourceSlot）输入时抛出 ``SiteSelectorSchemaError``。
    """

    # ``parameter_by_name`` 是动作输入字段权威视图，用于拒绝名字猜测与未知引用。
    parameter_by_name = {
        str(parameter.get("name")): (index, parameter)
        for index, parameter in enumerate(parameters)
    }
    for site_name, selector in selectors:
        # ``site_index`` 把跨字段诊断稳定定位到真实动作参数序号。
        site_index = parameter_by_name.get(site_name, (-1, {}))[0]
        for relation_name, referenced_name in (
            ("owner", selector.owner),
            ("occupant", selector.occupant),
        ):
            if referenced_name is None:
                continue
            relation_path = (
                f"/parameters/{site_index}/annotation/site_selector/{relation_name}"
            )
            if referenced_name == site_name:
                raise SiteSelectorSchemaError(
                    relation_path,
                    "SiteSelector 不能把库位字段自身作为物料关系",
                )
            referenced_entry = parameter_by_name.get(referenced_name)
            if referenced_entry is None:
                raise SiteSelectorSchemaError(
                    relation_path,
                    "SiteSelector 引用了不存在的动作输入字段",
                )
            # ``referenced`` 是 owner/occupant 指向的动作输入合同。
            referenced = referenced_entry[1]
            schema = referenced.get("schema")
            if not isinstance(schema, Mapping) or not _is_single_resource_slot(schema):
                raise SiteSelectorSchemaError(
                    relation_path,
                    "SiteSelector 关系必须指向单个物料占位符输入",
                )


def render_site_selector(selector: ParsedSiteSelector) -> ast.Call:
    """把规范库位选择器（SiteSelector）确定性渲染回 Python AST。

    参数说明：``selector`` 是静态解析结果。返回：保持四字段完整声明的调用 AST；
    不读取数据库或解析任何库位（Site）身份。
    """

    return ast.Call(
        func=ast.Name(id="SiteSelector", ctx=ast.Load()),
        args=[],
        keywords=[
            ast.keyword(arg="owner", value=ast.Constant(selector.owner)),
            ast.keyword(arg="occupant", value=ast.Constant(selector.occupant)),
            ast.keyword(
                arg="show_occupied",
                value=ast.Constant(selector.show_occupied),
            ),
            ast.keyword(
                arg="allow_occupied",
                value=ast.Constant(selector.allow_occupied),
            ),
        ],
    )


def _required_name(value: Any, *, path: str) -> str:
    """校验必填动作字段名。

    参数说明：``value`` 是注解字面量，``path`` 是诊断位置。返回：无首尾空白的
    非空字段名；非法值抛出 ``SiteSelectorSchemaError``。
    """

    if not isinstance(value, str) or not value or value != value.strip():
        raise SiteSelectorSchemaError(path, "SiteSelector owner 必须是非空字段名")
    return value


def _optional_name(value: Any, *, path: str) -> str | None:
    """校验可选动作字段名。

    参数说明：``value`` 是 occupant 字面量，``path`` 是诊断位置。返回：``None``
    或规范字段名；非法值抛出 ``SiteSelectorSchemaError``。
    """

    if value is None:
        return None
    return _required_name(value, path=path)


def _boolean(value: Any, *, path: str) -> bool:
    """校验占用展示策略布尔值。

    参数说明：``value`` 是策略字面量，``path`` 是诊断位置。返回：原布尔值；
    整数或其他真值不会被隐式接受。
    """

    if type(value) is not bool:
        raise SiteSelectorSchemaError(path, "SiteSelector 策略必须是布尔值")
    return value


def _is_site_value_schema(value_schema: Mapping[str, Any]) -> bool:
    """判断参数是否为必填或可空字符串库位值。

    参数说明：``value_schema`` 是规范值模式。返回：仅纯字符串或
    ``string | null`` 为真，数组、物料和普通对象均为假。
    """

    if value_schema.get("type") == "string":
        return True
    members = value_schema.get("anyOf")
    if not isinstance(members, list) or len(members) != 2:
        return False
    kinds = {member.get("type") for member in members if isinstance(member, Mapping)}
    return kinds == {"string", "null"}


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
    "ParsedSiteSelector",
    "SiteSelectorSchemaError",
    "parse_site_selector_call",
    "render_site_selector",
    "validate_site_selector_relations",
]
