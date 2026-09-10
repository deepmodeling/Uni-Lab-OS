"""动作（Action）参数与具名结果（named result）的纯 AST 规范合同门面。"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Never, Self

from unilabos.registry.action_result_schema import (
    ActionResultSchemaError,
    parse_action_result_declaration,
)
from unilabos.registry.annotation_schema import (
    NO_DEFAULT,
    AnnotationSchemaError,
    ResourceTemplateSymbol,
    parse_parameter_annotation,
)
from unilabos.registry.module_scope import (
    DefinitionNode,
    ModuleScopeError,
    resolve_module_scope,
)
from unilabos.registry.site_selector_schema import (
    ParsedSiteSelector,
    SiteSelectorSchemaError,
    validate_site_selector_relations,
)
from unilabos.registry.utils import parse_docstring
from unilabos.workflow.json_codec import strict_json_equal
from unilabos.workflow.schema import (
    WorkflowInputContract,
    WorkflowOutputContract,
    WorkflowSchemaError,
    parse_input_contract,
    parse_output_contract,
)

_ERROR_MESSAGE = "Action 定义不符合 Workflow 版本 1 合同"
_PARSED_ACTION_CONTRACT_TOKEN = object()
_FRAMEWORK_PARAMETERS = frozenset({"sample_uuids"})
_ANY = "typing:Any"
_DICT = "typing:Dict"
_JSON_VALUE = "unilabos.registry.annotations:JSONValue"


class ActionContractError(ValueError):
    """可稳定投影为注册表（Registry）或编译诊断的动作合同（ActionContract）错误。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


class ActionCompatibilityError(ValueError):
    """旧 decorator 兼容断言与 canonical schema 冲突。"""

    def __init__(self, code: str, path: str) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.message = "Action 兼容声明与 canonical contract 冲突"


@dataclass(frozen=True, slots=True, init=False)
class ParsedActionContract:
    """一个不保留源码语法形式的不可变动作合同（ActionContract）。"""

    _input_contract: WorkflowInputContract
    _output_contract: WorkflowOutputContract
    input_resource_templates: tuple[
        tuple[str, tuple[ResourceTemplateSymbol, ...]],
        ...,
    ]
    input_material_lock_free: tuple[str, ...]
    input_site_selectors: tuple[tuple[str, ParsedSiteSelector], ...]
    output_resource_templates: tuple[
        tuple[str, tuple[ResourceTemplateSymbol, ...]],
        ...,
    ]

    def __new__(cls, *_args: Any, **_kwargs: Any) -> Never:
        raise TypeError("请通过 parse_action_contract 创建 ParsedActionContract")

    @classmethod
    def _from_canonical(
        cls,
        input_contract: WorkflowInputContract,
        output_contract: WorkflowOutputContract,
        input_resource_templates: tuple[
            tuple[str, tuple[ResourceTemplateSymbol, ...]],
            ...,
        ],
        output_resource_templates: tuple[
            tuple[str, tuple[ResourceTemplateSymbol, ...]],
            ...,
        ],
        input_material_lock_free: tuple[str, ...],
        input_site_selectors: tuple[tuple[str, ParsedSiteSelector], ...],
        *,
        token: object,
    ) -> Self:
        """仅供解析器组合规范动作合同（Action Contract）。

        参数说明：输入/输出合同保存工作流值；两组资源模板符号保存静态兼容约束；
        ``input_material_lock_free`` 保存显式免锁输入；``input_site_selectors`` 保存
        已完成跨字段验证的库位选择器（SiteSelector）关系；``token`` 防止外部构造
        未验证对象。返回：不可变动作合同。异常：令牌不匹配时抛出 ``TypeError``。
        """

        if token is not _PARSED_ACTION_CONTRACT_TOKEN:
            raise TypeError("ParsedActionContract 只能由模块内 parser 创建")
        contract = object.__new__(cls)
        object.__setattr__(contract, "_input_contract", input_contract)
        object.__setattr__(contract, "_output_contract", output_contract)
        object.__setattr__(
            contract,
            "input_resource_templates",
            input_resource_templates,
        )
        object.__setattr__(
            contract,
            "output_resource_templates",
            output_resource_templates,
        )
        object.__setattr__(
            contract,
            "input_material_lock_free",
            input_material_lock_free,
        )
        object.__setattr__(contract, "input_site_selectors", input_site_selectors)
        return contract

    def to_dict(self) -> dict[str, Any]:
        """返回与内部 canonical contracts 不共享容器的完整 descriptor。"""

        return {
            "input_contract": self._input_contract.to_dict(),
            "output_contract": self._output_contract.to_dict(),
        }

    def to_action_schema(
        self,
        *,
        action_name: str,
        description: str = "",
    ) -> dict[str, Any]:
        """把唯一规范合同投影到既有动作（Action）schema。

        返回的 envelope 是包目录（PackageCatalog）与注册表（Registry）保存的唯一 typed 权威。
        字段顺序和源码 symbol 位于带版本的扩展中，不在旁边复制 input/output
        contract dump。
        """

        descriptor = self.to_dict()
        inputs = descriptor["input_contract"]["parameters"]
        outputs = descriptor["output_contract"]["outputs"]
        goal_properties: dict[str, Any] = {}
        required_inputs: list[str] = []
        site_selectors = dict(self.input_site_selectors)
        for parameter in inputs:
            name = parameter["name"]
            material_lock = None
            if _has_resource_slot(parameter["schema"]):
                material_lock = name not in self.input_material_lock_free
            field = _action_value_schema(
                parameter["schema"],
                material_lock=material_lock,
            )
            site_selector = site_selectors.get(name)
            if site_selector is not None:
                field["format"] = "uuid"
                field["x-unilabos-editor-control"] = "site_selector"
                field["x-unilabos-site-selector"] = site_selector.to_extension()
            if "default" in parameter:
                field["default"] = parameter["default"]
            for key in ("title", "description"):
                if key in parameter:
                    field[key] = parameter[key]
            goal_properties[name] = field
            if parameter["required"]:
                required_inputs.append(name)

        result_properties: dict[str, Any] = {}
        for output in outputs:
            field = _action_value_schema(output["schema"])
            for key in ("title", "description"):
                if key in output:
                    field[key] = output[key]
            result_properties[output["name"]] = field

        return {
            "title": f"{action_name}参数",
            "description": description or f"{action_name}的参数schema",
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": goal_properties,
                    "required": required_inputs,
                    "additionalProperties": False,
                },
                "feedback": {},
                "result": {
                    "type": "object",
                    "properties": result_properties,
                    "required": [output["name"] for output in outputs],
                    "additionalProperties": False,
                },
            },
            "required": ["goal"],
            "x-unilabos-action-contract": {
                "version": 2,
                "input_order": [parameter["name"] for parameter in inputs],
                "output_order": [output["name"] for output in outputs],
                "resource_template_symbols": {
                    "goal": _symbol_projection(self.input_resource_templates),
                    "result": _symbol_projection(self.output_resource_templates),
                },
            },
        }


def _action_value_schema(
    value: Mapping[str, Any],
    *,
    material_lock: bool | None = None,
) -> dict[str, Any]:
    """把严格工作流（Workflow）value schema 渲染为 JSON Schema 展示形状。"""

    if value.get("$slot") == "ResourceSlot":
        rendered = _material_reference_schema()
        if material_lock is not None:
            rendered["x-unilabos-material-lock"] = material_lock
        return rendered

    nullable_members = value.get("anyOf")
    if isinstance(nullable_members, list) and len(nullable_members) == 2:
        non_null = next(
            (
                item
                for item in nullable_members
                if isinstance(item, Mapping) and item.get("type") != "null"
            ),
            None,
        )
        has_null = any(
            isinstance(item, Mapping) and item.get("type") == "null"
            for item in nullable_members
        )
        if non_null is not None and has_null:
            rendered = _action_value_schema(
                non_null,
                material_lock=material_lock,
            )
            kind = rendered.get("type")
            if isinstance(kind, str):
                rendered["type"] = [kind, "null"]
                return rendered

    rendered = {key: _copy_json(item) for key, item in value.items()}
    if rendered.get("type") == "array" and isinstance(value.get("items"), Mapping):
        rendered["items"] = _action_value_schema(
            value["items"],
            material_lock=material_lock,
        )
    if rendered.get("type") == "object":
        rendered["additionalProperties"] = True
    return rendered


def _material_reference_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "uuid": {
                "type": "string",
                "format": "uuid",
            }
        },
        "required": ["uuid"],
        "additionalProperties": False,
    }


def _has_resource_slot(value: Mapping[str, Any]) -> bool:
    if value.get("$slot") == "ResourceSlot":
        return True
    nullable_members = value.get("anyOf")
    if isinstance(nullable_members, list):
        return any(
            isinstance(item, Mapping) and _has_resource_slot(item)
            for item in nullable_members
        )
    return (
        value.get("type") == "array"
        and isinstance(value.get("items"), Mapping)
        and _has_resource_slot(value["items"])
    )


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value


def _symbol_projection(
    groups: tuple[tuple[str, tuple[ResourceTemplateSymbol, ...]], ...],
) -> dict[str, list[str]]:
    return {
        name: [symbol.qualified_name for symbol in symbols]
        for name, symbols in groups
        if symbols
    }


def canonical_goal_defaults(schema: Mapping[str, Any]) -> dict[str, Any]:
    """从唯一动作（Action）schema 派生兼容默认值投影。"""

    properties = schema["properties"]["goal"]["properties"]
    return {
        str(name): _copy_json(value["default"])
        for name, value in properties.items()
        if "default" in value
    }


def validate_legacy_action_assertions(
    schema: Mapping[str, Any],
    *,
    action_name: str,
    goal_default: Any = None,
    handles: Any = None,
) -> dict[str, Any]:
    """校验旧 decorator 值，不合并第二份 contract。"""

    defaults = canonical_goal_defaults(schema)
    if goal_default not in (None, {}):
        if not isinstance(goal_default, Mapping):
            _compatibility_fail(
                "action_default_contract_conflict",
                f"/actions/{action_name}/goal_default",
            )
        for key in sorted(set(goal_default) | set(defaults)):
            if (
                key not in goal_default
                or key not in defaults
                or not strict_json_equal(goal_default[key], defaults[key])
            ):
                _compatibility_fail(
                    "action_default_contract_conflict",
                    f"/actions/{action_name}/goal_default/{key}",
                )
    _validate_legacy_handles(handles, schema=schema, action_name=action_name)
    return defaults


def _validate_legacy_handles(
    raw: Any,
    *,
    schema: Mapping[str, Any],
    action_name: str,
) -> None:
    if raw in (None, [], {}):
        return
    if not isinstance(raw, list):
        _compatibility_fail(
            "action_handle_contract_conflict",
            f"/actions/{action_name}/handles",
        )

    expected: dict[tuple[str, str], tuple[str, str, str, bool]] = {}
    goal = schema["properties"]["goal"]
    required_goal = set(goal.get("required", []))
    for name, value_schema in goal["properties"].items():
        expected[("target", name)] = (
            _legacy_value_type(value_schema),
            "goal",
            name,
            name in required_goal,
        )
    result = schema["properties"]["result"]
    for name, value_schema in result["properties"].items():
        expected[("source", name)] = (
            _legacy_value_type(value_schema),
            "result",
            name,
            False,
        )
    for name, value_schema in goal["properties"].items():
        if (
            _is_material_reference_schema(_base_value_schema(value_schema))
            and ("source", name) not in expected
        ):
            expected[("source", name)] = (
                "ResourceSlot",
                "result",
                name,
                False,
            )

    actual: dict[tuple[str, str], tuple[str, str, str, bool]] = {}
    for index, handle in enumerate(raw):
        if not isinstance(handle, Mapping):
            _compatibility_fail(
                "action_handle_contract_conflict",
                f"/actions/{action_name}/handles/{index}",
            )
        call = str(handle.get("$call") or handle.get("_call") or "")
        kwargs = handle.get("kwargs")
        values = kwargs if isinstance(kwargs, Mapping) else handle
        if call.endswith(("ActionInputHandle", "InputHandle")):
            io_type = "target"
            default_source = "goal"
        elif call.endswith(("ActionOutputHandle", "OutputHandle")):
            io_type = "source"
            default_source = "result"
        else:
            _compatibility_fail(
                "action_handle_contract_conflict",
                f"/actions/{action_name}/handles/{index}",
            )
        key = values.get("key")
        if not isinstance(key, str) or not key:
            _compatibility_fail(
                "action_handle_contract_conflict",
                f"/actions/{action_name}/handles/{index}",
            )
        identity = (io_type, key)
        if identity in actual:
            _compatibility_fail(
                "action_handle_contract_conflict",
                f"/actions/{action_name}/handles/{index}",
            )
        declared_io_type = values.get("io_type")
        if declared_io_type is not None and (
            not isinstance(declared_io_type, str) or declared_io_type.lower() != io_type
        ):
            _compatibility_fail(
                "action_handle_contract_conflict",
                f"/actions/{action_name}/handles",
            )
        expected_handle = expected.get(identity)
        if expected_handle is None:
            _compatibility_fail(
                "action_handle_contract_conflict",
                f"/actions/{action_name}/handles/{index}",
            )
        declared_required = values.get("required", expected_handle[3])
        if not isinstance(declared_required, bool):
            _compatibility_fail(
                "action_handle_contract_conflict",
                f"/actions/{action_name}/handles",
            )
        actual[identity] = (
            str(values.get("data_type") or ""),
            str(values.get("data_source") or default_source).lower(),
            str(values.get("data_key") or key),
            declared_required,
        )
    if actual != expected:
        _compatibility_fail(
            "action_handle_contract_conflict",
            f"/actions/{action_name}/handles",
        )


def _base_value_schema(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    members = schema.get("anyOf")
    if isinstance(members, list):
        return next(
            (
                item
                for item in members
                if isinstance(item, Mapping) and item.get("type") != "null"
            ),
            {},
        )
    return schema


def _legacy_value_type(schema: Mapping[str, Any]) -> str:
    base = _base_value_schema(schema)
    if _is_material_reference_schema(base):
        return "ResourceSlot"
    return str(base.get("type") or "object")


def _is_material_reference_schema(schema: Mapping[str, Any]) -> bool:
    if schema.get("$slot") == "ResourceSlot":
        return True
    kind = schema.get("type")
    if isinstance(kind, list) and "object" not in kind:
        return False
    if kind != "object" and not isinstance(kind, list):
        return False
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return False
    uuid_schema = properties.get("uuid")
    return (
        isinstance(uuid_schema, Mapping)
        and uuid_schema.get("type") == "string"
        and uuid_schema.get("format") == "uuid"
    )


def _compatibility_fail(code: str, path: str) -> Never:
    raise ActionCompatibilityError(code, path)


def _fail(
    path: str,
    *,
    code: str = "invalid_action_contract",
    message: str = _ERROR_MESSAGE,
) -> Never:
    raise ActionContractError(code, path, message)


def _action_context(
    module: ast.Module,
    action: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """返回动作（Action）是否为顶层 class 的直接 method，并验证真实包含关系。"""

    body = getattr(module, "body", None)
    if type(body) is not list:
        _fail(
            "/module/body",
            code="invalid_module_scope",
            message="模块作用域不符合 Workflow 静态解析合同",
        )
    for statement in body:
        if statement is action:
            return False
        if isinstance(statement, ast.ClassDef):
            class_body = getattr(statement, "body", None)
            if type(class_body) is not list:
                _fail("/action")
            if any(item is action for item in class_body):
                return True
    _fail("/action")


def _argument_list(value: object, path: str) -> list[ast.arg]:
    if type(value) is not list:
        _fail(path)
    arguments = value
    for index, argument in enumerate(arguments):
        if not isinstance(argument, ast.arg):
            _fail(f"{path}/{index}")
    return arguments


def _default_list(value: object, path: str) -> list[ast.expr]:
    if type(value) is not list:
        _fail(path)
    defaults = value
    for index, default in enumerate(defaults):
        if not isinstance(default, ast.expr):
            _fail(f"{path}/{index}")
    return defaults


def _keyword_defaults(value: object, expected: int) -> list[ast.expr | None]:
    path = "/parameters/keyword_defaults"
    if type(value) is not list or len(value) != expected:
        _fail(path)
    defaults = value
    for index, default in enumerate(defaults):
        if default is not None and not isinstance(default, ast.expr):
            _fail(f"{path}/{index}")
    return defaults


def _annotation_path(parameter_index: int, child_path: str) -> str:
    base = f"/parameters/{parameter_index}"
    if child_path in {"", "/"}:
        return base
    canonical_prefix = "/parameters/0"
    if child_path == canonical_prefix:
        return base
    if child_path.startswith(f"{canonical_prefix}/"):
        return f"{base}{child_path[len(canonical_prefix) :]}"
    return f"{base}{child_path}"


def _action_doc_metadata(
    action: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[dict[str, str], dict[str, str]]:
    body = getattr(action, "body", None)
    if type(body) is not list:
        _fail("/action/body")
    for index, statement in enumerate(body):
        if not isinstance(statement, ast.stmt):
            _fail(f"/action/body/{index}")
    try:
        parsed = parse_docstring(ast.get_docstring(action, clean=True))
    except (AttributeError, IndexError, TypeError, ValueError):
        _fail("/action/body")
    descriptions = parsed.get("params", {})
    titles = parsed.get("param_display_names", {})
    if not isinstance(descriptions, dict) or not isinstance(titles, dict):
        _fail("/action/body")
    return titles, descriptions


def _validate_action_shape(
    action: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    """在 module resolver 前把 action-local forged AST 定位到动作合同（ActionContract）。"""

    body = getattr(action, "body", None)
    if type(body) is not list:
        _fail("/action/body")
    for index, statement in enumerate(body):
        if not isinstance(statement, ast.stmt):
            _fail(f"/action/body/{index}")

    arguments = getattr(action, "args", None)
    if not isinstance(arguments, ast.arguments):
        _fail("/parameters")
    positional_only = _argument_list(
        getattr(arguments, "posonlyargs", None),
        "/parameters/positional_only",
    )
    positional = _argument_list(
        getattr(arguments, "args", None),
        "/parameters/positional",
    )
    keyword_only = _argument_list(
        getattr(arguments, "kwonlyargs", None),
        "/parameters/keyword_only",
    )
    _default_list(
        getattr(arguments, "defaults", None),
        "/parameters/defaults",
    )
    _keyword_defaults(
        getattr(arguments, "kw_defaults", None),
        len(keyword_only),
    )

    seen_names: set[str] = set()
    for index, argument in enumerate(positional_only + positional + keyword_only):
        name = getattr(argument, "arg", None)
        if type(name) is not str or not name or name in seen_names:
            _fail(f"/parameters/{index}/name")
        seen_names.add(name)


def _parse_parameters(
    action: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    is_method: bool,
    imports: Mapping[str, str],
) -> tuple[
    WorkflowInputContract,
    tuple[tuple[str, tuple[ResourceTemplateSymbol, ...]], ...],
    tuple[str, ...],
    tuple[tuple[str, ParsedSiteSelector], ...],
]:
    """解析动作全部输入并完成跨字段关系验证。

    参数说明：``action`` 是真实定义模块中的动作函数；``is_method`` 决定是否忽略
    receiver；``imports`` 是静态导入身份表。返回：规范输入合同、资源模板符号、
    免物料锁字段及库位选择器（SiteSelector）关系。异常：字段注解、默认值或
    owner/occupant 关系非法时抛出 ``ActionContractError``。
    """

    arguments = getattr(action, "args", None)
    if not isinstance(arguments, ast.arguments):
        _fail("/parameters")

    positional_only = _argument_list(
        getattr(arguments, "posonlyargs", None),
        "/parameters/positional_only",
    )
    positional = _argument_list(
        getattr(arguments, "args", None),
        "/parameters/positional",
    )
    keyword_only = _argument_list(
        getattr(arguments, "kwonlyargs", None),
        "/parameters/keyword_only",
    )
    defaults = _default_list(
        getattr(arguments, "defaults", None),
        "/parameters/defaults",
    )
    keyword_defaults = _keyword_defaults(
        getattr(arguments, "kw_defaults", None),
        len(keyword_only),
    )
    if getattr(arguments, "vararg", None) is not None:
        _fail("/parameters/vararg")
    if getattr(arguments, "kwarg", None) is not None:
        _fail("/parameters/kwarg")

    all_positional = positional_only + positional
    if len(defaults) > len(all_positional):
        _fail("/parameters/defaults")
    first_default = len(all_positional) - len(defaults)
    titles, descriptions = _action_doc_metadata(action)

    scheduled: list[tuple[ast.arg, ast.expr | object]] = []
    for index, argument in enumerate(all_positional):
        default_index = index - first_default
        default = defaults[default_index] if default_index >= 0 else NO_DEFAULT
        scheduled.append((argument, default))
    scheduled.extend(
        (argument, default if default is not None else NO_DEFAULT)
        for argument, default in zip(
            keyword_only,
            keyword_defaults,
            strict=True,
        )
    )

    descriptors: list[dict[str, Any]] = []
    resource_templates: list[tuple[str, tuple[ResourceTemplateSymbol, ...]]] = []
    material_lock_free: list[str] = []
    site_selectors: list[tuple[str, ParsedSiteSelector]] = []
    seen_names: set[str] = set()
    first_positional = all_positional[0] if all_positional else None
    for argument, default in scheduled:
        name = getattr(argument, "arg", None)
        if type(name) is not str or not name:
            _fail(f"/parameters/{len(descriptors)}/name")
        is_receiver = (
            is_method and argument is first_positional and name in {"self", "cls"}
        )
        if is_receiver or name in _FRAMEWORK_PARAMETERS:
            continue
        parameter_index = len(descriptors)
        if name in seen_names:
            _fail(f"/parameters/{parameter_index}/name")
        seen_names.add(name)
        annotation = getattr(argument, "annotation", None)
        if not isinstance(annotation, ast.expr):
            _fail(f"/parameters/{parameter_index}/annotation")
        try:
            parsed = parse_parameter_annotation(
                name,
                annotation,
                default=default,
                imports=imports,
                doc_title=titles.get(name),
                doc_description=descriptions.get(name),
                allow_material_lock=True,
            )
        except AnnotationSchemaError as error:
            _fail(
                _annotation_path(parameter_index, error.path),
                code="invalid_annotation",
                message=error.message,
            )
        except RecursionError:
            _fail(
                f"/parameters/{parameter_index}/annotation",
                code="invalid_annotation",
            )
        except (AttributeError, IndexError, KeyError, TypeError):
            _fail(f"/parameters/{parameter_index}/annotation")
        descriptors.append(parsed.to_dict())
        resource_templates.append((name, parsed.resource_templates))
        if parsed.material_lock_free:
            material_lock_free.append(name)
        if parsed.site_selector is not None:
            site_selectors.append((name, parsed.site_selector))

    try:
        validate_site_selector_relations(descriptors, site_selectors)
    except SiteSelectorSchemaError as error:
        _fail(error.path, code="invalid_annotation", message=error.message)

    try:
        contract = parse_input_contract({"version": 1, "parameters": descriptors})
    except WorkflowSchemaError as error:
        _fail(
            error.path or "/parameters",
            code="invalid_schema",
            message=error.message,
        )
    return (
        contract,
        tuple(resource_templates),
        tuple(material_lock_free),
        tuple(site_selectors),
    )


def _parse_results(
    action: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    definitions: Mapping[str, DefinitionNode],
    imports: Mapping[str, str],
) -> tuple[
    WorkflowOutputContract,
    tuple[tuple[str, tuple[ResourceTemplateSymbol, ...]], ...],
]:
    declaration = getattr(action, "returns", None)
    if declaration is None:
        _fail("/return")
    if isinstance(declaration, ast.expr) and _opaque_dict_result(
        declaration,
        imports,
    ):
        try:
            return parse_output_contract({"version": 1, "outputs": []}), ()
        except WorkflowSchemaError as error:  # pragma: no cover - 常量合同防御
            _fail(error.path or "/return", code="invalid_schema")
    if isinstance(declaration, ast.Name):
        name = getattr(declaration, "id", None)
        if type(name) is not str:
            _fail("/return", code="invalid_action_result")
        resolved = definitions.get(name)
        if not isinstance(resolved, ast.ClassDef):
            _fail("/return", code="invalid_action_result")
        declaration = resolved
    try:
        parsed = parse_action_result_declaration(
            declaration,
            imports=imports,
        )
    except ActionResultSchemaError as error:
        _fail(
            error.path,
            code="invalid_action_result",
            message=error.message,
        )
    except RecursionError:
        _fail("/return", code="invalid_action_result")
    except (AttributeError, IndexError, KeyError, TypeError):
        _fail("/return", code="invalid_action_result")
    try:
        contract = parse_output_contract(parsed.to_dict())
    except WorkflowSchemaError as error:
        _fail(
            error.path or "/return",
            code="invalid_schema",
            message=error.message,
        )
    return contract, parsed.resource_templates


def _opaque_dict_result(
    declaration: ast.expr,
    imports: Mapping[str, str],
) -> bool:
    """只接受顶层兼容 opaque JSON mapping，不放宽字段 annotation。"""

    if isinstance(declaration, ast.Name):
        return declaration.id == "dict" and declaration.id not in imports
    if not isinstance(declaration, ast.Subscript):
        return False
    origin = declaration.value
    if isinstance(origin, ast.Name):
        valid_origin = (
            origin.id == "dict" and origin.id not in imports
        ) or imports.get(origin.id) == _DICT
    elif isinstance(origin, ast.Attribute) and isinstance(origin.value, ast.Name):
        valid_origin = (
            imports.get(origin.value.id) == "typing" and origin.attr == "Dict"
        )
    else:
        valid_origin = False
    if not valid_origin:
        return False
    members = (
        list(declaration.slice.elts)
        if isinstance(declaration.slice, ast.Tuple)
        else [declaration.slice]
    )
    if len(members) != 2:
        return False
    key, value = members
    return (
        isinstance(key, ast.Name)
        and key.id == "str"
        and key.id not in imports
        and isinstance(value, ast.Name)
        and imports.get(value.id) in {_ANY, _JSON_VALUE}
    )


def parse_action_contract(
    module: ast.Module,
    action: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    module_name: str,
) -> ParsedActionContract:
    """从真实定义模块 AST 解析完整规范动作合同（Action Contract）。

    参数说明：``module`` 是不执行作者代码的完整 Python 语法树；``action`` 是该
    语法树中的动作函数；``module_name`` 是解析可信导入绑定所需的模块身份。返回：
    包含输入、输出、物料锁、库位选择（Site Selection）和展示信息的不可变合同。

    异常说明：动作形状、注解、导入绑定、文档注释或合同不变量非法时抛出
    ``ActionContractError``；递归源码结构也按稳定合同错误失败关闭。
    """

    if not isinstance(action, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _fail("/action")
    _validate_action_shape(action)
    try:
        scope = resolve_module_scope(module, module_name=module_name)
    except ModuleScopeError as error:
        _fail(error.path, code=error.code, message=error.message)
    except RecursionError:
        _fail("/module")

    is_method = _action_context(module, action)
    (
        input_contract,
        input_templates,
        input_material_lock_free,
        input_site_selectors,
    ) = _parse_parameters(
        action,
        is_method=is_method,
        imports=scope.annotation_bindings,
    )
    output_contract, output_templates = _parse_results(
        action,
        definitions=scope.definitions,
        imports=scope.annotation_bindings,
    )
    return ParsedActionContract._from_canonical(
        input_contract,
        output_contract,
        input_templates,
        output_templates,
        input_material_lock_free,
        input_site_selectors,
        token=_PARSED_ACTION_CONTRACT_TOKEN,
    )


__all__ = [
    "ActionCompatibilityError",
    "ActionContractError",
    "ParsedActionContract",
    "canonical_goal_defaults",
    "parse_action_contract",
    "validate_legacy_action_assertions",
]
