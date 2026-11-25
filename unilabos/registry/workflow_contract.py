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
            raise ValueError(
                f"工作流输入 {arg.arg!r} 不能用 Annotated[ResourceSlot, AllowedResourceTemplates(...)] 限定资源模板；"
                f"输入写成 {arg.arg}: ResourceSlot，模板写在装饰器上："
                f"@workflow(..., allowed_templates={{{arg.arg!r}: [\"<注册表资源 id>\", ...]}})"
            )
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


def normalize_allowed_templates(value: Any) -> dict[str, list[str]]:
    """``@workflow(allowed_templates=...)`` 的字面量 → ``{输入名: [注册表资源 id, ...]}``（去重、保序）。

    键是工作流输入名；值是一个或多个注册表资源 id（字符串）。写法不对抛 ``ValueError``。
    """

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("@workflow 的 allowed_templates 必须是 {输入名: [注册表资源 id, ...]} 字典")
    result: dict[str, list[str]] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key.isidentifier():
            raise ValueError(f"@workflow 的 allowed_templates 键 {key!r} 必须是工作流输入名")
        items = [raw] if isinstance(raw, str) else raw
        if not isinstance(items, (list, tuple)) or not items:
            raise ValueError(f"@workflow 的 allowed_templates[{key!r}] 必须是非空的注册表资源 id 列表")
        ids: list[str] = []
        for item in items:
            if not isinstance(item, str) or not item.strip() or item != item.strip():
                raise ValueError(f"@workflow 的 allowed_templates[{key!r}] 里 {item!r} 不是注册表资源 id")
            if item not in ids:
                ids.append(item)
        result[key] = ids
    return result


def _resource_slot_schemas(schema: Any) -> list[dict[str, Any]]:
    """输入 schema 里承载模板筛选的 ResourceSlot 槽位：单个、列表元素或可空成员。"""

    if not isinstance(schema, dict):
        return []
    if schema.get("$slot") == "ResourceSlot":
        return [schema]
    found = _resource_slot_schemas(schema.get("items"))
    for member in schema.get("anyOf") or []:
        found.extend(_resource_slot_schemas(member))
    return found


def apply_allowed_templates(
    input_contract: dict[str, Any],
    allowed_templates: dict[str, list[str]],
    *,
    output_contract: dict[str, Any] | None = None,
    resolve: Callable[[str], str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """校验并落实 ``@workflow(allowed_templates=...)``。

    键必须是注解为 ``ResourceSlot`` / ``List[ResourceSlot]`` 的工作流输入，否则抛 ``ValueError``（注册失败）。
    给了 ``resolve``（注册表资源 id → 资源模板 uuid，未知 id 抛 ``ValueError``）时，把解析出的 uuid 写进该输入
    （及同名隐式输出）的 ResourceSlot schema 扩展 ``allowed_resource_template_uuids``：前端按它筛选，
    预检 / 建任务按它拒绝模板不符的物料。返回新的 ``(input_contract, output_contract)``，不改入参。
    """

    import copy

    inputs = copy.deepcopy(input_contract)
    outputs = copy.deepcopy(output_contract) if output_contract is not None else None
    by_name = {item["name"]: item for item in inputs.get("parameters", [])}
    for name in allowed_templates:
        descriptor = by_name.get(name)
        if descriptor is None:
            raise ValueError(f"@workflow 的 allowed_templates 键 {name!r} 不是工作流输入")
        if not _resource_slot_schemas(descriptor.get("schema")):
            raise ValueError(
                f"@workflow 的 allowed_templates 键 {name!r} 必须是 ResourceSlot / List[ResourceSlot] 输入"
            )
    if resolve is None:
        return inputs, outputs
    implicit = {
        item["name"]: item for item in (outputs or {}).get("outputs", []) if item.get("implicit") is True
    }
    for name, ids in allowed_templates.items():
        uuids: list[str] = []
        for identity in ids:
            template_uuid = resolve(identity)
            if template_uuid not in uuids:
                uuids.append(template_uuid)
        for descriptor in (by_name[name], implicit.get(name)):
            if descriptor is None:
                continue
            descriptor["schema"] = copy.deepcopy(descriptor["schema"])
            for slot in _resource_slot_schemas(descriptor["schema"]):
                slot["allowed_resource_template_uuids"] = list(uuids)
    return inputs, outputs
