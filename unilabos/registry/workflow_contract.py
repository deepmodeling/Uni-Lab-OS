"""设备包工作流输入输出声明：复用规范注解解析器，不求值 Python 注解。"""
from __future__ import annotations

import ast
import inspect
import os
import tokenize
from typing import Any, Callable

from unilabos.registry.annotation_schema import NO_DEFAULT, parse_parameter_annotation
from unilabos.registry.action_result_schema import parse_action_result_declaration


def source_symbols(fn: Callable[..., Any]) -> tuple[dict[str, str], dict[str, ast.ClassDef]]:
    filename = inspect.getsourcefile(fn)
    if not filename:
        raise ValueError("工作流合同需要可读取的源码文件")
    with tokenize.open(filename) as stream:
        tree = ast.parse(stream.read())
    return module_symbols(
        tree,
        module_name=getattr(fn, "__module__", "") or "",
        is_package=os.path.basename(filename) == "__init__.py",
    )


def module_symbols(
    tree: ast.Module, *, module_name: str = "", is_package: bool = False,
) -> tuple[dict[str, str], dict[str, ast.ClassDef]]:
    """模块级 ``from x import y`` 与类定义（结果记录）；不导入、不求值。

    相对导入按 ``module_name``（``is_package`` 表示包的 ``__init__.py``）解析成绝对身份；
    没有模块身份（如上传的源码文本）或越过顶层包时跳过。
    """

    from unilabos.registry.module_scope import resolve_relative_module

    imports: dict[str, str] = {}
    records: dict[str, ast.ClassDef] = {}
    for item in tree.body:
        if isinstance(item, ast.ImportFrom) and (item.module or item.level):
            module = item.module
            if item.level:
                if not module_name:
                    continue
                try:
                    module = resolve_relative_module(module_name, item.level, item.module, is_package=is_package)
                except ValueError:
                    continue
            for alias in item.names:
                if alias.name != "*":
                    imports[alias.asname or alias.name] = f"{module}:{alias.name}"
        elif isinstance(item, ast.ClassDef):
            records[item.name] = item
    return imports, records


def parse_workflow_contract(fn: Callable[..., Any], function: ast.FunctionDef) -> tuple[dict[str, Any], dict[str, Any]]:
    imports, records = source_symbols(fn)
    return parse_workflow_contract_symbols(function, imports=imports, records=records)


def parse_workflow_contract_symbols(
    function: ast.FunctionDef, *, imports: dict[str, str], records: dict[str, ast.ClassDef],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按函数签名（仅关键字参数注解 + 返回注解）解析输入输出合同；作者源码文本即可，无需函数对象。"""

    args = function.args
    if len(args.args) != 1 or args.posonlyargs or args.vararg or args.kwarg or args.defaults:
        raise ValueError("静态工作流只接受一个 ctx 位置参数；工作流输入使用带注解的仅关键字参数")
    parameters = []
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if arg.annotation is None:
            raise ValueError(f"工作流输入 {arg.arg!r} 缺少类型注解")
        parsed = parse_parameter_annotation(arg.arg, arg.annotation,
            default=NO_DEFAULT if default is None else default, imports=imports)
        if parsed.resource_templates:
            raise ValueError("工作流输入的资源模板筛选尚未绑定注册表；不能丢弃 AllowedResourceTemplates 声明")
        parameters.append(parsed.to_dict())
    declaration = function.returns
    if isinstance(declaration, ast.Constant) and isinstance(declaration.value, str):
        declaration = ast.parse(declaration.value, mode="eval").body
    if isinstance(declaration, ast.Name):
        declaration = records.get(declaration.id)
        if declaration is None:
            raise ValueError("工作流返回类型必须是本模块的 TypedDict 或冻结 dataclass 结果记录")
    parsed_results = parse_action_result_declaration(
        declaration if declaration is not None else ast.Constant(value=None), imports=imports)
    if any(symbols for _, symbols in parsed_results.resource_templates):
        raise ValueError("工作流输出的资源模板筛选尚未绑定注册表")
    outputs = parsed_results.to_dict()
    explicit = {item["name"] for item in outputs["outputs"]}
    def resource_schema(schema: dict[str, Any]) -> bool:
        return schema.get("$slot") == "ResourceSlot" or (
            isinstance(schema.get("items"), dict) and resource_schema(schema["items"])
        ) or any(resource_schema(member) for member in schema.get("anyOf", []))
    for parameter in parameters:
        if resource_schema(parameter["schema"]) and parameter["name"] not in explicit:
            outputs["outputs"].append({"name": parameter["name"], "schema": parameter["schema"], "implicit": True})
    return {"version": 1, "parameters": parameters}, outputs
