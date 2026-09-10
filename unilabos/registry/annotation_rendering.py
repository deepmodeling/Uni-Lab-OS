"""规范参数注解的确定性 Python AST 渲染模块。"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.registry.site_selector_schema import (
    ParsedSiteSelector,
    render_site_selector,
)


def render_parameter_descriptor(
    descriptor: Mapping[str, Any],
    *,
    resource_template_names: Sequence[str],
    material_lock_free: bool,
    site_selector: ParsedSiteSelector | None,
) -> ast.expr:
    """从规范参数事实生成确定性注解 AST。

    参数说明：``descriptor`` 保存类型、展示和约束；``resource_template_names``
    保存源码局部符号；``material_lock_free`` 保存显式免物料锁声明；
    ``site_selector`` 保存可选库位选择器（SiteSelector）关系。返回：补齐位置的
    Python 注解表达式；不读取作者源码或数据库。
    """

    rendered = _render_schema(dict(descriptor["schema"]))
    # ``metadata`` 使用稳定顺序，保证同一合同跨进程生成相同 Python 源码。
    metadata: list[ast.expr] = []
    if resource_template_names:
        metadata.append(_render_templates(resource_template_names))
    if material_lock_free:
        metadata.append(_render_material_lock_free())
    if site_selector is not None:
        metadata.append(render_site_selector(site_selector))
    field_keywords = _field_keywords(descriptor)
    if field_keywords:
        metadata.append(
            ast.Call(
                func=ast.Name(id="Field", ctx=ast.Load()),
                args=[],
                keywords=field_keywords,
            )
        )
    if metadata:
        rendered = ast.Subscript(
            value=ast.Name(id="Annotated", ctx=ast.Load()),
            slice=ast.Tuple(elts=[rendered, *metadata], ctx=ast.Load()),
            ctx=ast.Load(),
        )
    return ast.fix_missing_locations(rendered)


def _constant(value: Any) -> ast.Constant:
    """把规范 JSON 标量包装为 AST 常量。

    参数说明：``value`` 是已校验的合同标量。返回：对应常量节点；异常由 Python
    AST 构造器按非法值类型抛出。
    """

    return ast.Constant(value=value)


def _render_schema(schema: dict[str, Any]) -> ast.expr:
    """把规范工作流值模式渲染为 Python 类型注解。

    参数说明：``schema`` 是规范值模式的分离副本。返回：标量、枚举、数组、对象、
    可空或物料占位符（ResourceSlot）类型 AST；未知类型表示内部不变量破坏并抛出
    ``AssertionError``。
    """

    if "anyOf" in schema:
        return ast.BinOp(
            left=_render_schema(schema["anyOf"][0]),
            op=ast.BitOr(),
            right=_constant(None),
        )
    if "$slot" in schema:
        return ast.Name(id="ResourceSlot", ctx=ast.Load())
    kind = schema["type"]
    if "enum" in schema:
        values = [_constant(value) for value in schema["enum"]]
        # ``slice_node`` 保持单成员 Literal 不产生多余 tuple。
        slice_node: ast.expr = (
            values[0] if len(values) == 1 else ast.Tuple(elts=values, ctx=ast.Load())
        )
        return ast.Subscript(
            value=ast.Name(id="Literal", ctx=ast.Load()),
            slice=slice_node,
            ctx=ast.Load(),
        )
    names = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
    }
    if kind in names:
        return ast.Name(id=names[kind], ctx=ast.Load())
    if kind == "object":
        return ast.Subscript(
            value=ast.Name(id="dict", ctx=ast.Load()),
            slice=ast.Tuple(
                elts=[
                    ast.Name(id="str", ctx=ast.Load()),
                    ast.Name(id="JSONValue", ctx=ast.Load()),
                ],
                ctx=ast.Load(),
            ),
            ctx=ast.Load(),
        )
    if kind == "array":
        return ast.Subscript(
            value=ast.Name(id="list", ctx=ast.Load()),
            slice=_render_schema(schema["items"]),
            ctx=ast.Load(),
        )
    raise AssertionError(f"unsupported canonical schema kind: {kind}")


def _render_templates(resource_template_names: Sequence[str]) -> ast.Call:
    """渲染资源模板（ResourceTemplate）允许集。

    参数说明：``resource_template_names`` 是已验证源码局部名称。返回：
    ``AllowedResourceTemplates(...)`` 调用 AST。
    """

    return ast.Call(
        func=ast.Name(id="AllowedResourceTemplates", ctx=ast.Load()),
        args=[ast.Name(id=name, ctx=ast.Load()) for name in resource_template_names],
        keywords=[],
    )


def _render_material_lock_free() -> ast.Call:
    """渲染唯一显式免物料锁注解。

    参数：无。返回：``MaterialLock(free=True)`` 调用 AST。
    """

    return ast.Call(
        func=ast.Name(id="MaterialLock", ctx=ast.Load()),
        args=[],
        keywords=[ast.keyword(arg="free", value=_constant(True))],
    )


def _field_keywords(descriptor: Mapping[str, Any]) -> list[ast.keyword]:
    """从参数合同投影 Pydantic ``Field`` 关键字。

    参数说明：``descriptor`` 是已校验参数描述。返回：按规范顺序排列的展示、
    数值和长度约束 AST 关键字；没有约束时返回空列表。
    """

    keywords: list[ast.keyword] = []
    if "title" in descriptor:
        keywords.append(ast.keyword(arg="title", value=_constant(descriptor["title"])))
    if "description" in descriptor:
        keywords.append(
            ast.keyword(
                arg="description",
                value=_constant(descriptor["description"]),
            )
        )
    schema = descriptor["schema"]
    base = schema["anyOf"][0] if "anyOf" in schema else schema
    for schema_key, field_key in (
        ("minimum", "ge"),
        ("maximum", "le"),
        ("minLength", "min_length"),
        ("maxLength", "max_length"),
        ("minItems", "min_length"),
        ("maxItems", "max_length"),
    ):
        if schema_key in base:
            keywords.append(
                ast.keyword(
                    arg=field_key,
                    value=_constant(base[schema_key]),
                )
            )
    return keywords


__all__ = ["render_parameter_descriptor"]
