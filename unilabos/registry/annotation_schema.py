"""共享的 Parameter Annotation v1 解析与确定性渲染。"""

from __future__ import annotations

import ast
import math
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Never, Self

from unilabos.registry.annotation_rendering import render_parameter_descriptor
from unilabos.registry.site_selector_schema import (
    ParsedSiteSelector,
    SiteSelectorSchemaError,
    parse_site_selector_call,
)
from unilabos.workflow.schema import (
    WorkflowInputContract,
    WorkflowOutputContract,
    WorkflowSchemaError,
    parse_input_contract,
    parse_output_contract,
)

NO_DEFAULT = object()
_PARSED_PARAMETER_TOKEN = object()
_PARSED_RESULT_TOKEN = object()

_ANNOTATED = "typing:Annotated"
_DICT = "typing:Dict"
_FIELD = "pydantic:Field"
_JSON_VALUE = "unilabos.registry.annotations:JSONValue"
_LIST = "typing:List"
_LITERAL = "typing:Literal"
_MATERIAL_LOCK = "unilabos.registry.annotations:MaterialLock"
_OPTIONAL = "typing:Optional"
_RESOURCE_SLOT = "unilabos.registry.placeholder_type:ResourceSlot"
_RESOURCE_TEMPLATES = "unilabos.registry.annotations:AllowedResourceTemplates"
_SITE_SELECTOR = "unilabos.registry.annotations:SiteSelector"
_ERROR_MESSAGE = "参数注解不符合 Workflow 版本 1 合同"
_AUTHORING_INTEGER_DIGITS = 4096
_AUTHORING_INTEGER_LIMIT = 10**_AUTHORING_INTEGER_DIGITS


class AnnotationSchemaError(ValueError):
    """可稳定投影为编译诊断的参数注解错误。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


@dataclass(frozen=True, slots=True)
class ResourceTemplateSymbol:
    """源码局部名称及其静态 import identity。"""

    local_name: str
    qualified_name: str


@dataclass(frozen=True, slots=True, init=False)
class ParsedParameter:
    """一个不可变的 canonical 参数合同及其待解析模板 symbol。"""

    _contract: WorkflowInputContract
    resource_templates: tuple[ResourceTemplateSymbol, ...]
    material_lock_free: bool
    site_selector: ParsedSiteSelector | None

    def __new__(cls, *_args: Any, **_kwargs: Any) -> Never:
        raise TypeError("请通过 parse_parameter_annotation 创建 ParsedParameter")

    @classmethod
    def _from_canonical(
        cls,
        contract: WorkflowInputContract,
        resource_templates: tuple[ResourceTemplateSymbol, ...],
        material_lock_free: bool,
        site_selector: ParsedSiteSelector | None,
        *,
        token: object,
    ) -> Self:
        """仅供解析器从规范参数合同构造不可变结果。

        参数说明：``contract`` 是已校验工作流输入合同；``resource_templates``
        保存资源模板源码符号；``material_lock_free`` 保存显式免物料锁声明；
        ``site_selector`` 保存可选库位选择器（SiteSelector）关系；``token`` 防止
        外部绕过解析边界。返回：不可变解析参数。异常：令牌不匹配时抛出
        ``TypeError``。
        """

        if token is not _PARSED_PARAMETER_TOKEN:
            raise TypeError("ParsedParameter 只能由模块内 parser 创建")
        parameter = object.__new__(cls)
        object.__setattr__(parameter, "_contract", contract)
        object.__setattr__(
            parameter,
            "resource_templates",
            resource_templates,
        )
        object.__setattr__(parameter, "material_lock_free", material_lock_free)
        object.__setattr__(parameter, "site_selector", site_selector)
        return parameter

    def to_dict(self) -> dict[str, Any]:
        """返回与内部 canonical contract 不共享容器的 descriptor。"""

        return self._contract.to_dict()["parameters"][0]


@dataclass(frozen=True, slots=True, init=False)
class ParsedResult:
    """一个不可变的 canonical 显式结果字段及其待解析模板 symbol。"""

    _contract: WorkflowOutputContract
    resource_templates: tuple[ResourceTemplateSymbol, ...]

    def __new__(cls, *_args: Any, **_kwargs: Any) -> Never:
        raise TypeError("请通过 parse_result_annotation 创建 ParsedResult")

    @classmethod
    def _from_canonical(
        cls,
        contract: WorkflowOutputContract,
        resource_templates: tuple[ResourceTemplateSymbol, ...],
        *,
        token: object,
    ) -> Self:
        if token is not _PARSED_RESULT_TOKEN:
            raise TypeError("ParsedResult 只能由模块内 parser 创建")
        result = object.__new__(cls)
        object.__setattr__(result, "_contract", contract)
        object.__setattr__(
            result,
            "resource_templates",
            resource_templates,
        )
        return result

    def to_dict(self) -> dict[str, Any]:
        """返回与内部 canonical contract 不共享容器的 output descriptor。"""

        return self._contract.to_dict()["outputs"][0]


def _fail(
    path: str,
    *,
    code: str = "invalid_annotation",
    message: str = _ERROR_MESSAGE,
) -> Never:
    raise AnnotationSchemaError(code, path, message)


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_builtin(
    node: ast.AST,
    name: str,
    imports: Mapping[str, str],
) -> bool:
    return _is_name(node, name) and name not in imports


def _is_import(
    node: ast.AST,
    qualified_name: str,
    imports: Mapping[str, str],
) -> bool:
    return isinstance(node, ast.Name) and imports.get(node.id) == qualified_name


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _subscript_members(node: ast.Subscript) -> list[ast.expr]:
    if isinstance(node.slice, ast.Tuple):
        return list(node.slice.elts)
    return [node.slice]


def _literal_value(node: ast.expr, *, path: str) -> Any:
    try:
        value = ast.literal_eval(node)
    except (OverflowError, RecursionError, TypeError, ValueError):
        _fail(path)
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is int:
            if abs(item) >= _AUTHORING_INTEGER_LIMIT:
                _fail(path)
        elif type(item) in {list, tuple, set}:
            pending.extend(item)
        elif type(item) is dict:
            pending.extend(item.keys())
            pending.extend(item.values())
    return value


def _has_duplicate(values: list[Any]) -> bool:
    ordered = sorted(values)
    return any(left == right for left, right in pairwise(ordered))


def _parse_literal(
    node: ast.Subscript,
    *,
    path: str,
) -> dict[str, Any]:
    members = _subscript_members(node)
    if not members:
        _fail(path)

    values = [_literal_value(member, path=path) for member in members]
    families: list[str] = []
    for value in values:
        if type(value) is str:
            families.append("string")
        elif type(value) is bool:
            families.append("boolean")
        elif type(value) is int:
            families.append("integer")
        elif type(value) is float and math.isfinite(value):
            families.append("number")
        else:
            _fail(path)

    family_set = set(families)
    if family_set <= {"integer", "number"}:
        kind = "number" if "number" in family_set else "integer"
    elif len(family_set) == 1:
        kind = families[0]
    else:
        _fail(path)

    if _has_duplicate(values):
        _fail(path)
    return {"type": kind, "enum": values}


def _parse_nullable(
    node: ast.expr,
    *,
    imports: Mapping[str, str],
    path: str,
    allow_nullable: bool,
) -> dict[str, Any] | None:
    base_node: ast.expr | None = None
    if isinstance(node, ast.Subscript) and _is_import(node.value, _OPTIONAL, imports):
        if not allow_nullable or isinstance(node.slice, ast.Tuple):
            _fail(path)
        base_node = node.slice
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        if not allow_nullable:
            _fail(path)
        left_none = _is_none(node.left)
        right_none = _is_none(node.right)
        if left_none == right_none:
            _fail(path)
        base_node = node.right if left_none else node.left

    if base_node is None:
        return None
    base = _parse_type(
        base_node,
        imports=imports,
        path=path,
        allow_array=True,
        allow_nullable=False,
    )
    return {"anyOf": [base, {"type": "null"}]}


def _parse_type(
    node: ast.expr,
    *,
    imports: Mapping[str, str],
    path: str,
    allow_array: bool = True,
    allow_nullable: bool = True,
) -> dict[str, Any]:
    nullable = _parse_nullable(
        node,
        imports=imports,
        path=path,
        allow_nullable=allow_nullable,
    )
    if nullable is not None:
        return nullable

    primitives = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
    }
    for name, kind in primitives.items():
        if _is_builtin(node, name, imports):
            return {"type": kind}

    if _is_import(node, _RESOURCE_SLOT, imports):
        return {"$slot": "ResourceSlot"}

    if not isinstance(node, ast.Subscript):
        _fail(path)

    if _is_import(node.value, _LITERAL, imports):
        return _parse_literal(node, path=path)

    is_list = _is_builtin(node.value, "list", imports) or _is_import(
        node.value,
        _LIST,
        imports,
    )
    if is_list:
        if not allow_array or isinstance(node.slice, ast.Tuple):
            _fail(path)
        item = _parse_type(
            node.slice,
            imports=imports,
            path=f"{path}/items",
            allow_array=False,
            allow_nullable=False,
        )
        return {"type": "array", "items": item}

    is_dict = _is_builtin(node.value, "dict", imports) or _is_import(
        node.value,
        _DICT,
        imports,
    )
    if is_dict:
        members = _subscript_members(node)
        if (
            len(members) != 2
            or not _is_builtin(members[0], "str", imports)
            or not _is_import(members[1], _JSON_VALUE, imports)
        ):
            _fail(path)
        return {"type": "object"}

    _fail(path)


def _schema_base(schema: dict[str, Any]) -> dict[str, Any]:
    if "anyOf" in schema:
        return schema["anyOf"][0]
    return schema


def _slot_shape(schema: dict[str, Any]) -> bool:
    base = _schema_base(schema)
    if "$slot" in base:
        return True
    return base.get("type") == "array" and "$slot" in base.get("items", {})


def _trim_presentation(value: Any, *, path: str) -> str:
    if type(value) is not str:
        _fail(path)
    normalized = value.strip()
    if not normalized:
        _fail(path)
    return normalized


def _finite_number(value: Any, *, path: str) -> int | float:
    if type(value) not in {int, float}:
        _fail(path)
    if type(value) is float and not math.isfinite(value):
        _fail(path)
    return value


def _parse_field(
    call: ast.Call,
    schema: dict[str, Any],
    *,
    path: str,
) -> tuple[str | None, str | None]:
    if call.args:
        _fail(path)
    allowed = {
        "title",
        "description",
        "ge",
        "le",
        "min_length",
        "max_length",
    }
    values: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg not in allowed or keyword.arg in values:
            _fail(path)
        values[keyword.arg] = _literal_value(
            keyword.value,
            path=f"{path}/{keyword.arg}",
        )

    title = None
    description = None
    if "title" in values:
        title = _trim_presentation(values["title"], path=f"{path}/title")
    if "description" in values:
        description = _trim_presentation(
            values["description"],
            path=f"{path}/description",
        )

    base = _schema_base(schema)
    kind = base.get("type")
    for source, target in (("ge", "minimum"), ("le", "maximum")):
        if source not in values:
            continue
        if kind not in {"integer", "number"}:
            _fail(f"{path}/{source}")
        bound = _finite_number(values[source], path=f"{path}/{source}")
        if kind == "integer":
            if isinstance(bound, float) and not bound.is_integer():
                _fail(f"{path}/{source}")
            bound = int(bound)
        base[target] = bound

    for source, target in (
        ("min_length", "minLength" if kind == "string" else "minItems"),
        ("max_length", "maxLength" if kind == "string" else "maxItems"),
    ):
        if source not in values:
            continue
        if kind not in {"string", "array"}:
            _fail(f"{path}/{source}")
        bound = values[source]
        if type(bound) is not int or bound < 0:
            _fail(f"{path}/{source}")
        base[target] = bound

    if base.get("minimum", -math.inf) > base.get("maximum", math.inf):
        _fail(path)
    if base.get("minLength", 0) > base.get("maxLength", math.inf):
        _fail(path)
    if base.get("minItems", 0) > base.get("maxItems", math.inf):
        _fail(path)
    return title, description


def _parse_resource_templates(
    call: ast.Call,
    schema: dict[str, Any],
    imports: Mapping[str, str],
    *,
    path: str,
) -> tuple[ResourceTemplateSymbol, ...]:
    if not _slot_shape(schema) or not call.args or call.keywords:
        _fail(path)
    symbols: list[ResourceTemplateSymbol] = []
    local_names: set[str] = set()
    qualified_names: set[str] = set()
    for index, argument in enumerate(call.args):
        item_path = f"{path}/{index}"
        if not isinstance(argument, ast.Name):
            _fail(item_path)
        local_name = argument.id
        qualified_name = imports.get(local_name)
        if (
            qualified_name is None
            or qualified_name.count(":") != 1
            or not all(qualified_name.split(":", 1))
            or local_name in local_names
            or qualified_name in qualified_names
        ):
            _fail(item_path)
        local_names.add(local_name)
        qualified_names.add(qualified_name)
        symbols.append(ResourceTemplateSymbol(local_name, qualified_name))
    return tuple(symbols)


def _parse_material_lock(
    call: ast.Call,
    schema: dict[str, Any],
    *,
    path: str,
) -> bool:
    """解析动作输入物料锁（Material Lock）的唯一显式免锁形式。"""

    if not _slot_shape(schema) or call.args or len(call.keywords) != 1:
        _fail(path)
    keyword = call.keywords[0]
    if keyword.arg != "free":
        _fail(path)
    free = _literal_value(keyword.value, path=f"{path}/free")
    if free is not True:
        _fail(f"{path}/free")
    return True


def _parse_annotation(
    annotation: ast.expr,
    imports: Mapping[str, str],
    *,
    allow_material_lock: bool,
) -> tuple[
    dict[str, Any],
    str | None,
    str | None,
    tuple[ResourceTemplateSymbol, ...],
    bool,
    ParsedSiteSelector | None,
]:
    """解析一个参数或结果注解及其有限元数据。

    参数说明：``annotation`` 是未执行的注解 AST；``imports`` 证明每个名称的
    静态导入身份；``allow_material_lock`` 同时控制仅动作输入可用的物料锁和
    库位选择器（SiteSelector）元数据。返回：规范值模式、展示文本、资源模板
    符号、免锁标记及可选库位关系；非法或重复元数据抛出
    ``AnnotationSchemaError``。
    """

    metadata: list[ast.expr] = []
    type_node = annotation
    if isinstance(annotation, ast.Subscript) and _is_import(
        annotation.value, _ANNOTATED, imports
    ):
        members = _subscript_members(annotation)
        if len(members) < 2:
            _fail("/annotation")
        type_node, *metadata = members

    schema = _parse_type(
        type_node,
        imports=imports,
        path="/annotation",
    )
    title = None
    description = None
    templates: tuple[ResourceTemplateSymbol, ...] = ()
    field_seen = False
    material_lock_seen = False
    templates_seen = False
    material_lock_free = False
    site_selector_seen = False
    site_selector = None
    for index, item in enumerate(metadata):
        path = f"/annotation/metadata/{index}"
        if not isinstance(item, ast.Call):
            _fail(path)
        if _is_import(item.func, _FIELD, imports):
            if field_seen:
                _fail(path)
            field_seen = True
            title, description = _parse_field(item, schema, path=path)
        elif _is_import(item.func, _RESOURCE_TEMPLATES, imports):
            if templates_seen:
                _fail(path)
            templates_seen = True
            templates = _parse_resource_templates(
                item,
                schema,
                imports,
                path=path,
            )
        elif allow_material_lock and _is_import(item.func, _MATERIAL_LOCK, imports):
            if material_lock_seen:
                _fail(path)
            material_lock_seen = True
            material_lock_free = _parse_material_lock(item, schema, path=path)
        elif allow_material_lock and _is_import(item.func, _SITE_SELECTOR, imports):
            if site_selector_seen:
                _fail(path)
            site_selector_seen = True
            try:
                site_selector = parse_site_selector_call(item, schema, path=path)
            except SiteSelectorSchemaError as error:
                _fail(error.path, message=error.message)
        else:
            _fail(path)
    return schema, title, description, templates, material_lock_free, site_selector


def _optional_presentation(value: str | None, *, path: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        _fail(path)
    normalized = value.strip()
    return normalized or None


def _parse_default(default: ast.expr, *, path: str) -> Any:
    return _literal_value(default, path=path)


def parse_parameter_annotation(
    name: str,
    annotation: ast.expr,
    *,
    default: ast.expr | object,
    imports: Mapping[str, str],
    doc_title: str | None = None,
    doc_description: str | None = None,
    allow_material_lock: bool = False,
) -> ParsedParameter:
    """把一个源码参数静态解析为规范第 1 版输入合同。

    参数说明：``name`` 是参数名；``annotation`` 是不执行的类型注解 AST；
    ``default`` 是默认值 AST 或 ``NO_DEFAULT``；``imports`` 是可信导入绑定；
    ``doc_title``/``doc_description`` 是文档注释展示信息；
    ``allow_material_lock`` 决定是否允许输入侧物料锁与库位选择元数据。返回：解析器
    签发的不可变参数合同。

    异常说明：注解、默认值、导入、展示文本或规范输入合同非法时抛出
    ``AnnotationSchemaError``，且不执行任何作者代码。
    """

    if not isinstance(annotation, ast.expr):
        _fail("/annotation")
    if not isinstance(imports, Mapping):
        _fail("/imports")

    (
        schema,
        field_title,
        field_description,
        templates,
        material_lock_free,
        site_selector,
    ) = _parse_annotation(
        annotation,
        imports,
        allow_material_lock=allow_material_lock,
    )
    title = field_title or _optional_presentation(
        doc_title,
        path="/doc/title",
    )
    description = field_description or _optional_presentation(
        doc_description,
        path="/doc/description",
    )
    descriptor: dict[str, Any] = {
        "name": name,
        "schema": schema,
        "required": default is NO_DEFAULT,
    }
    if default is not NO_DEFAULT:
        if not isinstance(default, ast.expr):
            _fail("/default")
        descriptor["default"] = _parse_default(default, path="/default")
    if title is not None:
        descriptor["title"] = title
    if description is not None:
        descriptor["description"] = description

    try:
        contract = parse_input_contract({"version": 1, "parameters": [descriptor]})
    except WorkflowSchemaError as error:
        path = error.path or "/"
        _fail(path, code=error.code)
    return ParsedParameter._from_canonical(
        contract,
        templates,
        material_lock_free,
        site_selector,
        token=_PARSED_PARAMETER_TOKEN,
    )


def parse_result_annotation(
    name: str,
    annotation: ast.expr,
    *,
    imports: Mapping[str, str],
) -> ParsedResult:
    """把一个源码结果字段静态解析为规范第 1 版输出合同。

    参数说明：``name`` 是输出字段名；``annotation`` 是不执行的类型注解 AST；
    ``imports`` 是可信导入绑定。返回：解析器签发的不可变输出合同；输出侧不接受
    物料锁或库位选择（Site Selection）元数据。

    异常说明：注解、导入或规范输出合同非法时抛出
    ``AnnotationSchemaError``，且不执行任何作者代码。
    """

    if not isinstance(annotation, ast.expr):
        _fail("/annotation")
    if not isinstance(imports, Mapping):
        _fail("/imports")

    schema, title, description, templates, _material_lock_free, _site_selector = (
        _parse_annotation(
            annotation,
            imports,
            allow_material_lock=False,
        )
    )
    descriptor: dict[str, Any] = {
        "name": name,
        "schema": schema,
    }
    if title is not None:
        descriptor["title"] = title
    if description is not None:
        descriptor["description"] = description

    try:
        contract = parse_output_contract({"version": 1, "outputs": [descriptor]})
    except WorkflowSchemaError as error:
        path = error.path or "/"
        _fail(path, code=error.code)
    return ParsedResult._from_canonical(
        contract,
        templates,
        token=_PARSED_RESULT_TOKEN,
    )


def render_parameter_annotation(parameter: ParsedParameter) -> ast.expr:
    """只从规范参数事实生成确定性的 annotation expression。

    参数说明：``parameter`` 是唯一解析器创建的不可变参数合同。返回：包含资源
    模板、免物料锁、库位选择器（SiteSelector）和 ``Field`` 展示约束的 Python
    AST；类型非法时抛出 ``AnnotationSchemaError``。
    """

    if not isinstance(parameter, ParsedParameter):
        _fail("/parameter")
    return render_parameter_descriptor(
        parameter.to_dict(),
        resource_template_names=tuple(
            symbol.local_name for symbol in parameter.resource_templates
        ),
        material_lock_free=parameter.material_lock_free,
        site_selector=parameter.site_selector,
    )
