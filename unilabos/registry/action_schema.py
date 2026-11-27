"""JSON 动作的统一 schema：注册表按签名给每个 JSON 动作生成同一标准的 schema。

动作参数与返回值只有一个标准，没有 opt-in 开关。规则（注解 → schema → 校验）：

- 严格解析器认识的注解（``str`` / ``int`` / ``float`` / ``bool``、``Literal``、``Optional`` 与
  ``X | None``、``ResourceSlot`` / ``list[ResourceSlot]``、``list[X]``、``dict[str, JSONValue]``，以及
  ``Annotated[..., Field(...)]`` / ``AllowedResourceTemplates``）：严格 schema；
- ``DeviceSlot`` / ``SiteSlot``：设备 ID / 库位 UUID 字符串；``@action(material_contract=MaterialRole.site_picker(...))`` 声明的
  库位关系写进该 ``SiteSlot`` 参数的 ``x-unilabos-site-selector`` 扩展（前端库位选择器、预检与派发门禁用）；
- 没写注解、``Any``、``object``：接受任意 JSON 值（``{}``）；
- 默认值是 ``None`` 的参数按可空处理（``x: str = None`` 与 ``x: Optional[str] = None`` 相同）；
- 严格解析器不认识的注解：该参数退回注册表原来的签名类型推断。本模块只放一个
  ``x-unilabos-infer`` 占位（AST 扫描在线程池里运行，不导入作者模块），由注册表补全；
- ``**kwargs``：goal 允许多传字段；``*args``：无法表示，忽略并按定义告警一次；
- 框架注入的参数不进 schema：``sample_uuids``，以及注解为 ``ActionContext`` / ``Optional[ActionContext]`` /
  ``ActionContext | None`` 的参数（按类型识别，与名字无关；没有这个注解却叫 ``action_context`` 的是注册错误）；
- 没有默认值的参数必填，缺了在调度端 / Host 调用前报错，不再补 0 / 空串；
- 返回注解：没写不校验（``{}``）；``None`` 只接受空返回；``dict`` / ``Dict[str, Any]`` 接受任意对象；
  同模块的 ``TypedDict`` / frozen ``dataclass``：具名输出（每个字段一个输出端口，逐字段严格解析，
  不认识的字段退回推断）；其他注解整体退回推断。

参数注解里已删除的 ``Placeholder(...)`` / ``SiteSelector(...)`` 是注册错误，报错信息给出新写法
（前端选择器写 ``@action(placeholder_keys=...)``，库位关系写 ``MaterialRole.site_picker(...)``）。

ROS 动作类型（``@action(action_type=...)``）不经过这里，照旧按 ROS 消息生成 schema。
"""

from __future__ import annotations

import ast
import logging
import threading
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple

from unilabos.registry.action_context import ast_action_context_parameters
from unilabos.registry.action_contract_schema import _action_value_schema, _has_resource_slot
from unilabos.registry.action_options import (
    REMOVED_ANNOTATION_IDENTITIES,
    check_action_context_parameter,
    removed_annotation_message,
)
from unilabos.registry.action_result_schema import (
    ActionResultSchemaError,
    parse_action_result_declaration,
)
from unilabos.registry.annotation_schema import (
    NO_DEFAULT,
    AnnotationSchemaError,
    parse_parameter_annotation,
    parse_result_annotation,
)
from unilabos.registry.module_scope import ModuleScopeError, resolve_module_scope
from unilabos.registry.site_selector_schema import (
    SiteSelectorSchemaError,
    validate_site_selector_relations,
)

logger = logging.getLogger(__name__)

#: 退回推断的字段占位：``{"x-unilabos-infer": {"annotation": "<注解源码>"}}``，注册表补全后移除。
INFER_KEY = "x-unilabos-infer"
#: 按名字识别、由框架注入的参数（与 AST 扫描的参数跳过名单一致）。动作上下文按类型识别，见
#: :func:`framework_parameter_names`。
FRAMEWORK_PARAMETERS = frozenset({"sample_uuids"})
#: 运行时由 Host 剥离到结果外层的保留返回字段。
RESERVED_RESULT_FIELDS = frozenset({"unilabos_samples"})

_ANY = frozenset({"typing:Any", "typing_extensions:Any", "builtins:object"})
_JSON_VALUE = "unilabos.registry.annotations:JSONValue"
_ANNOTATED = frozenset({"typing:Annotated", "typing_extensions:Annotated"})
_OPTIONAL = frozenset({"typing:Optional", "typing_extensions:Optional"})
_UNION = frozenset({"typing:Union", "typing_extensions:Union"})
_LIST = frozenset({"typing:List", "typing_extensions:List"})
_DICTS = frozenset({"typing:Dict", "typing:Mapping", "collections.abc:Mapping", "typing_extensions:Dict"})
_TYPED_DICT = frozenset({"typing:TypedDict", "typing_extensions:TypedDict"})
_NOT_REQUIRED = frozenset({"typing:NotRequired", "typing_extensions:NotRequired"})
_DATACLASS = "dataclasses:dataclass"
_PLACEHOLDER_MODULE = "unilabos.registry.placeholder_type"
_SITE_SLOT = f"{_PLACEHOLDER_MODULE}:SiteSlot"
_STRING_SLOTS = {
    f"{_PLACEHOLDER_MODULE}:DeviceSlot": "device reference",
    _SITE_SLOT: "site reference (ResourceSite uuid)",
}
#: 字段 schema 上标出槽位类型的扩展：调度器按它给 DeviceSlot 加设备锁、给 SiteSlot 加库位锁。
SLOT_KEY = "x-unilabos-slot"
_SLOT_KINDS = {f"{_PLACEHOLDER_MODULE}:DeviceSlot": "device", _SITE_SLOT: "site"}

_warned: set[str] = set()
_warned_lock = threading.Lock()


class ActionSchemaError(ValueError):
    """动作声明本身写错（不是解析器不认识的注解）：注册失败并指出写法。"""


def warn_once(definition: str, message: str) -> bool:
    """按定义 + 原因告警一次（装饰器与 AST 扫描共用），返回是否首次告警。"""

    key = f"{definition}\0{message}"
    with _warned_lock:
        if key in _warned:
            return False
        _warned.add(key)
    logger.warning("动作 %s：%s", definition, message)
    return True


# ---------------------------------------------------------------------------
# 模块绑定
# ---------------------------------------------------------------------------


class ModuleBindings:
    """一个模块的注解名字绑定与顶层定义，同一模块内的动作共用一份。

    优先用严格的模块作用域解析（识别遮蔽）；作用域写法超出严格解析器时退回 AST 扫描的
    导入表（不识别遮蔽），保证任何写法都能生成 schema。
    """

    def __init__(
        self,
        module: Optional[ast.Module],
        *,
        module_name: str,
        is_package: bool = False,
        import_map: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.imports: Dict[str, str] = {}
        self.definitions: Dict[str, ast.AST] = {}
        scope = None
        if module is not None and module_name:
            try:
                scope = resolve_module_scope(module, module_name=module_name, is_package=is_package)
            except (ModuleScopeError, RecursionError, ValueError, TypeError, AttributeError):
                scope = None
        if scope is not None:
            self.imports = dict(scope.annotation_bindings)
            self.definitions = dict(scope.definitions)
        else:
            self.imports = {
                name: value
                for name, value in (import_map or {}).items()
                if not (module_name and value == f"{module_name}:{name}")
            }
            for node in getattr(module, "body", None) or []:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.definitions[node.name] = node

    def identity(self, node: ast.AST) -> str:
        """``Name`` / ``a.b.Name`` 的静态导入身份（``module:attr``）；未知返回空串。"""

        if isinstance(node, ast.Name):
            return self.imports.get(node.id, "")
        if isinstance(node, ast.Attribute):
            parts: List[str] = []
            current: ast.AST = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if not isinstance(current, ast.Name):
                return ""
            base = self.imports.get(current.id)
            if not base or ":" in base:
                return ""
            parts.reverse()
            return f"{'.'.join([base, *parts[:-1]])}:{parts[-1]}"
        return ""

    def is_builtin(self, node: ast.AST, name: str) -> bool:
        return isinstance(node, ast.Name) and node.id == name and name not in self.imports


# ---------------------------------------------------------------------------
# 注解辅助
# ---------------------------------------------------------------------------


def annotation_source(node: Optional[ast.AST]) -> str:
    """注解的源码文本（推断与统计用）。"""

    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except (AttributeError, TypeError, ValueError, RecursionError):
        return ""


def _is_any(node: ast.AST, bindings: ModuleBindings) -> bool:
    if bindings.is_builtin(node, "object"):
        return True
    return bindings.identity(node) in _ANY


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _subscript_members(node: ast.Subscript) -> List[ast.expr]:
    return list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]


def is_nullable_annotation(node: ast.AST, bindings: ModuleBindings) -> bool:
    """顶层是否已允许 ``None``（``Optional[X]``、``X | None``、``Union[X, None]``）。"""

    if isinstance(node, ast.Subscript):
        origin = bindings.identity(node.value)
        if origin in _ANNOTATED:
            members = _subscript_members(node)
            return bool(members) and is_nullable_annotation(members[0], bindings)
        if origin in _OPTIONAL:
            return True
        if origin in _UNION:
            return any(_is_none(member) for member in _subscript_members(node))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_none(node.left) or _is_none(node.right) or (
            is_nullable_annotation(node.left, bindings) or is_nullable_annotation(node.right, bindings)
        )
    return _is_none(node)


def _nullable(node: ast.expr, bindings: ModuleBindings) -> ast.expr:
    """``X`` → ``X | None``（只用于默认值是 None 的参数）；``Annotated[X, ...]`` 换成 ``Annotated[X | None, ...]``。"""

    if isinstance(node, ast.Subscript) and bindings.identity(node.value) in _ANNOTATED:
        members = _subscript_members(node)
        if len(members) >= 2:
            inner = _nullable(members[0], bindings)
            rebuilt = ast.Subscript(
                value=node.value, slice=ast.Tuple(elts=[inner, *members[1:]], ctx=ast.Load()), ctx=ast.Load()
            )
            return ast.copy_location(rebuilt, node)
    return ast.BinOp(left=node, op=ast.BitOr(), right=ast.Constant(value=None))


def reject_removed_annotations(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    bindings: ModuleBindings,
    *,
    definition: str,
) -> None:
    """参数或返回注解里写了已删除的 ``Placeholder(...)`` / ``SiteSelector(...)``：报错并给出新写法。"""

    arguments = function.args
    annotated = [
        (argument.arg, argument.annotation)
        for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        if argument.annotation is not None
    ]
    if getattr(function, "returns", None) is not None:
        annotated.append(("<return>", function.returns))
    for name, annotation in annotated:
        for node in ast.walk(annotation):
            if not isinstance(node, ast.Call):
                continue
            removed = REMOVED_ANNOTATION_IDENTITIES.get(bindings.identity(node.func))
            if removed is not None:
                label = "返回注解" if name == "<return>" else f"参数 {name}"
                raise ActionSchemaError(f"动作 {definition} {label}：{removed_annotation_message(removed)}")


def _is_site_slot(node: ast.AST, bindings: ModuleBindings) -> bool:
    """单个 ``SiteSlot``（可带 ``Optional`` / ``| None`` / ``Annotated``）：``MaterialRole.site_picker`` 的库位参数只能是它。"""

    if bindings.identity(node) == _SITE_SLOT:
        return True
    if isinstance(node, ast.Subscript):
        origin = bindings.identity(node.value)
        members = _subscript_members(node)
        if origin in _ANNOTATED and members:
            return _is_site_slot(members[0], bindings)
        if origin in _OPTIONAL and len(members) == 1:
            return _is_site_slot(members[0], bindings)
        if origin in _UNION:
            rest = [member for member in members if not _is_none(member)]
            return len(rest) == 1 and len(members) == 2 and _is_site_slot(rest[0], bindings)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        if _is_none(node.right):
            return _is_site_slot(node.left, bindings)
        if _is_none(node.left):
            return _is_site_slot(node.right, bindings)
    return False


def _string_slot(node: ast.AST, bindings: ModuleBindings) -> Optional[Dict[str, Any]]:
    """``DeviceSlot`` / ``SiteSlot``（及其 list / Optional 形态）的字符串 schema。"""

    identity = bindings.identity(node)
    if identity in _STRING_SLOTS:
        return {"type": "string", "description": _STRING_SLOTS[identity], SLOT_KEY: _SLOT_KINDS[identity]}
    if isinstance(node, ast.Subscript):
        origin = bindings.identity(node.value)
        members = _subscript_members(node)
        if (bindings.is_builtin(node.value, "list") or origin in _LIST) and len(members) == 1:
            item = _string_slot(members[0], bindings)
            return {"type": "array", "items": item} if item is not None else None
        if origin in _OPTIONAL and len(members) == 1:
            base = _string_slot(members[0], bindings)
            return _with_null(base) if base is not None else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        if _is_none(node.right):
            base = _string_slot(node.left, bindings)
        elif _is_none(node.left):
            base = _string_slot(node.right, bindings)
        else:
            return None
        return _with_null(base) if base is not None else None
    return None


def slot_kind(field: Any) -> str:
    """字段 schema 的槽位类型：``"resource"`` / ``"resource_list"`` / ``"device"`` / ``"device_list"`` / ``"site"`` / ``""``。

    认 ``ResourceSlot`` 的物料引用形状（``x-unilabos-material-lock`` 或 ``{"uuid"}`` 对象）与
    ``DeviceSlot`` / ``SiteSlot`` 的 ``x-unilabos-slot`` 扩展，可空 / ``anyOf`` 形态同样识别。
    """

    if not isinstance(field, Mapping):
        return ""
    marker = field.get(SLOT_KEY)
    if marker in {"device", "site"}:
        return str(marker)
    if "x-unilabos-material-lock" in field:
        return "resource"
    kinds = field.get("type")
    kinds = set(kinds) if isinstance(kinds, list) else {kinds}
    if "array" in kinds:
        item = slot_kind(field.get("items"))
        return f"{item}_list" if item in {"resource", "device"} else ""
    members = [item for item in field.get("anyOf") or [] if isinstance(item, Mapping) and item.get("type") != "null"]
    if len(members) == 1:
        return slot_kind(members[0])
    return ""


def _with_null(schema: Dict[str, Any]) -> Dict[str, Any]:
    """让一个字段 schema 也接受 null。"""

    if not schema:
        return schema
    if isinstance(schema.get("enum"), list) and None not in schema["enum"]:
        schema = {**schema, "enum": [*schema["enum"], None]}
    kind = schema.get("type")
    if isinstance(kind, str):
        if kind == "null":
            return schema
        return {**schema, "type": [kind, "null"]}
    if isinstance(kind, list):
        return schema if "null" in kind else {**schema, "type": [*kind, "null"]}
    members = schema.get("anyOf")
    if isinstance(members, list) and any(isinstance(item, Mapping) and item.get("type") == "null" for item in members):
        return schema
    return {"anyOf": [schema, {"type": "null"}]}


def _literal(node: ast.AST) -> Tuple[bool, Any]:
    try:
        return True, ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False, None


def _json_value(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return not (isinstance(value, float) and value != value)
    if isinstance(value, (list, tuple)):
        return all(_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    return False


def _json_copy(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_copy(item) for item in value]
    if isinstance(value, list):
        return [_json_copy(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_copy(item) for key, item in value.items()}
    return value


def _docstring(function: ast.AST) -> Tuple[Dict[str, str], Dict[str, str], str]:
    from unilabos.registry.utils.tools import parse_docstring

    try:
        parsed = parse_docstring(ast.get_docstring(function, clean=True))
    except (AttributeError, IndexError, TypeError, ValueError):
        return {}, {}, ""
    titles = parsed.get("param_display_names") or {}
    descriptions = parsed.get("params") or {}
    return (
        {str(k): str(v) for k, v in titles.items() if isinstance(v, str) and v.strip()},
        {str(k): str(v) for k, v in descriptions.items() if isinstance(v, str)},
        str(parsed.get("description") or ""),
    )


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def _strict_field(
    name: str,
    annotation: ast.expr,
    default: Any,
    bindings: ModuleBindings,
    *,
    title: Optional[str],
    description: Optional[str],
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], Tuple[str, ...]]]:
    """严格解析一个参数注解；解析器不认识时返回 None。"""

    try:
        parsed = parse_parameter_annotation(
            name,
            annotation,
            default=default,
            imports=bindings.imports,
            doc_title=title,
            doc_description=description,
            allow_material_lock=True,
        )
    except (AnnotationSchemaError, RecursionError, AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None
    descriptor = parsed.to_dict()
    field = _action_value_schema(
        descriptor["schema"],
        material_lock=True if _has_resource_slot(descriptor["schema"]) else None,
    )
    if "default" in descriptor:
        field["default"] = descriptor["default"]
    for key in ("title", "description"):
        if key in descriptor:
            field[key] = descriptor[key]
    symbols = tuple(symbol.qualified_name for symbol in parsed.resource_templates)
    return field, descriptor, symbols


def framework_parameter_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    bindings: ModuleBindings,
    *,
    definition: str,
) -> set[str]:
    """不属于动作接口、由框架注入的参数名：``sample_uuids``，加上按类型识别的 ``ActionContext`` 参数。

    参数叫 ``action_context`` 却没有 ``ActionContext`` 注解（已删除的按名字注入写法）时报错并给出新写法。
    """

    context = ast_action_context_parameters(function, bindings.identity)
    arguments = function.args
    check_action_context_parameter(
        (argument.arg for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]),
        context,
        definition=definition,
    )
    return set(FRAMEWORK_PARAMETERS) | set(context)


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef, is_method: bool):
    arguments = function.args
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults: List[Any] = [NO_DEFAULT] * (len(positional) - len(arguments.defaults)) + list(arguments.defaults)
    scheduled = list(zip(positional, defaults))
    if is_method and scheduled:
        scheduled = scheduled[1:]
    scheduled.extend(
        (argument, default if default is not None else NO_DEFAULT)
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
    )
    return scheduled


def build_action_schema_draft(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    bindings: ModuleBindings,
    *,
    action_name: str,
    description: str = "",
    definition: str = "",
    is_method: bool = True,
    site_selectors: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """按签名生成一个 JSON 动作的统一 schema 草稿。

    返回 ``{"schema", "inferred", "result_inferred", "skipped"}``：``schema`` 是完整动作
    schema（``x-unilabos-action-contract`` 第 2 版信封），退回推断的字段带 ``x-unilabos-infer`` 占位，
    由注册表 ``finalize_action_schema`` 补全；``inferred`` / ``result_inferred`` 列出退回推断的参数与
    返回字段；``skipped`` 是无法表示、被忽略的参数写法。

    ``site_selectors`` 是 ``MaterialRole.site_picker(...)`` 给出的 ``{库位参数名: x-unilabos-site-selector 扩展}``：
    库位参数必须注解为 ``SiteSlot``，owner / occupant 必须是单个 ``ResourceSlot`` 参数，扩展写进该参数的
    schema。动作声明写错（库位关系不对、注解里写了已删除的 ``Placeholder`` / ``SiteSelector``）抛
    :class:`ActionSchemaError`。
    """

    definition = definition or action_name
    reject_removed_annotations(function, bindings, definition=definition)
    framework = framework_parameter_names(function, bindings, definition=definition)
    titles, descriptions, doc_description = _docstring(function)
    goal: Dict[str, Any] = {}
    required: List[str] = []
    input_order: List[str] = []
    goal_symbols: Dict[str, List[str]] = {}
    strict_descriptors: List[Dict[str, Any]] = []
    site_slots: set[str] = set()
    inferred: List[str] = []
    skipped: List[str] = []

    for argument, default_node in _parameters(function, is_method):
        name = argument.arg
        if name in framework:
            continue
        has_default = default_node is not NO_DEFAULT
        default_known, default_value = _literal(default_node) if has_default else (False, None)
        if default_known and not _json_value(default_value):
            default_known = False
        title, doc = titles.get(name), descriptions.get(name)
        annotation = argument.annotation
        field: Optional[Dict[str, Any]] = None
        if annotation is not None and _is_site_slot(annotation, bindings):
            site_slots.add(name)
        if annotation is None or _is_any(annotation, bindings):
            field = {}
            if default_known:
                field["default"] = _json_copy(default_value)
        else:
            nullable_default = default_known and default_value is None
            candidate = annotation
            if nullable_default and not is_nullable_annotation(annotation, bindings):
                candidate = _nullable(annotation, bindings)
            for candidate in (candidate,):
                if _opaque_dict(annotation, bindings):
                    # dict / Dict[str, Any] / Mapping：任意 JSON 对象
                    field = {"type": ["object", "null"]} if nullable_default else {"type": "object"}
                    if default_known:
                        field["default"] = _json_copy(default_value)
                    break
                slot = _string_slot(candidate, bindings)
                if slot is not None:
                    field = dict(slot)
                    if default_known:
                        field["default"] = _json_copy(default_value)
                    break
                strict = _strict_field(
                    name,
                    candidate,
                    default_node if default_known else NO_DEFAULT,
                    bindings,
                    title=title,
                    description=doc,
                )
                if strict is not None:
                    field, descriptor, symbols = strict
                    strict_descriptors.append(descriptor)
                    if symbols:
                        goal_symbols[name] = list(symbols)
                    break
            if field is None:
                inferred.append(name)
                field = {INFER_KEY: {"annotation": annotation_source(annotation), "nullable": nullable_default}}
                if default_known:
                    field["default"] = _json_copy(default_value)
        if title and "title" not in field:
            field["title"] = title
        if doc is not None and "description" not in field and doc.strip():
            field["description"] = doc
        elif doc is not None and doc.strip() and field.get("description") in {"device reference", "site reference (ResourceSite uuid)"}:
            field["description"] = doc
        goal[name] = field
        input_order.append(name)
        if not has_default:
            required.append(name)

    arguments = function.args
    if arguments.vararg is not None:
        skipped.append(f"*{arguments.vararg.arg}")
        warn_once(definition, f"*{arguments.vararg.arg} 无法表示为具名参数，已忽略")
    allow_extra = arguments.kwarg is not None

    for name, extension in (site_selectors or {}).items():
        if name not in goal:
            raise ActionSchemaError(f"动作 {definition}：MaterialRole.site_picker 引用了签名里没有的参数 {name}")
        if name not in site_slots:
            raise ActionSchemaError(f"动作 {definition}：MaterialRole.site_picker 的库位参数 {name} 必须注解为 SiteSlot")
        try:
            validate_site_selector_relations(
                strict_descriptors,
                [(name, extension)],
                device_parameters={key for key, value in goal.items() if slot_kind(value) == "device"},
            )
        except SiteSelectorSchemaError as error:
            raise ActionSchemaError(f"动作 {definition} 参数 {name}：{error.message}") from None
        field = goal[name]
        # 不限定 uuid 格式：值是库位 uuid，或所属方下的库位名（工作流里写死的工位，派发前在所属方下解析）
        field["x-unilabos-editor-control"] = "site_selector"
        field["x-unilabos-site-selector"] = dict(extension)

    result, output_order, result_symbols, result_inferred = _result_schema(function, bindings)
    display = action_name.removeprefix("auto-")
    schema = {
        "title": f"{display}参数",
        "description": description or doc_description or f"{display}的参数schema",
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": goal,
                "required": required,
                "additionalProperties": allow_extra,
            },
            "feedback": {},
            "result": result,
        },
        "required": ["goal"],
        "x-unilabos-action-contract": {
            "version": 2,
            "input_order": input_order,
            "output_order": output_order,
            "resource_template_symbols": {"goal": goal_symbols, "result": result_symbols},
        },
    }
    return {
        "schema": schema,
        "inferred": inferred,
        "result_inferred": result_inferred,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# 返回值
# ---------------------------------------------------------------------------


def _opaque_dict(node: ast.AST, bindings: ModuleBindings) -> bool:
    """``dict`` / ``Dict`` / ``Mapping`` 及其 ``[str, Any | JSONValue | object]``：任意对象。"""

    if bindings.is_builtin(node, "dict") or bindings.identity(node) in _DICTS:
        return True
    if not isinstance(node, ast.Subscript):
        return False
    if not (bindings.is_builtin(node.value, "dict") or bindings.identity(node.value) in _DICTS):
        return False
    members = _subscript_members(node)
    if len(members) != 2:
        return False
    value = members[1]
    return _is_any(value, bindings) or bindings.identity(value) == _JSON_VALUE


def _infer(node: ast.AST) -> Dict[str, Any]:
    return {INFER_KEY: {"annotation": annotation_source(node), "nullable": False}}


def _record_class(node: ast.AST, bindings: ModuleBindings) -> Optional[ast.ClassDef]:
    """返回注解指向的同模块 TypedDict / dataclass 定义。"""

    if not isinstance(node, ast.Name):
        return None
    declaration = bindings.definitions.get(node.id)
    if not isinstance(declaration, ast.ClassDef):
        return None
    if declaration.bases:
        if len(declaration.bases) == 1 and bindings.identity(declaration.bases[0]) in _TYPED_DICT:
            return declaration
        return None
    for decorator in declaration.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if bindings.identity(target) == _DATACLASS:
            return declaration
    return None


def _record_fields(declaration: ast.ClassDef, bindings: ModuleBindings):
    total = True
    for keyword in declaration.keywords:
        if keyword.arg == "total":
            ok, value = _literal(keyword.value)
            total = value is not False if ok else True
    fields: Dict[str, Dict[str, Any]] = {}
    required: List[str] = []
    symbols: Dict[str, List[str]] = {}
    inferred: List[str] = []
    for statement in declaration.body:
        if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
            continue
        name = statement.target.id
        if name in RESERVED_RESULT_FIELDS or name.startswith("_"):
            continue
        annotation = statement.annotation
        field_required = total
        if isinstance(annotation, ast.Subscript) and bindings.identity(annotation.value) in _NOT_REQUIRED:
            annotation = annotation.slice
            field_required = False
        if _is_any(annotation, bindings):
            field: Dict[str, Any] = {}
        else:
            field = {}
            try:
                parsed = parse_result_annotation(name, annotation, imports=bindings.imports)
            except (AnnotationSchemaError, RecursionError, AttributeError, IndexError, KeyError, TypeError, ValueError):
                parsed = None
            if parsed is not None:
                output = parsed.to_dict()
                field = _action_value_schema(output["schema"])
                for key in ("title", "description"):
                    if key in output:
                        field[key] = output[key]
                if parsed.resource_templates:
                    symbols[name] = [symbol.qualified_name for symbol in parsed.resource_templates]
            else:
                slot = _string_slot(annotation, bindings)
                if slot is not None:
                    field = dict(slot)
                else:
                    field = _infer(annotation)
                    field[INFER_KEY]["nullable"] = is_nullable_annotation(annotation, bindings)
                    inferred.append(name)
        fields[name] = field
        if field_required:
            required.append(name)
    return fields, required, symbols, inferred


def _result_schema(function: ast.AST, bindings: ModuleBindings):
    """返回 ``(result_schema, output_order, result_symbols, inferred_fields)``。"""

    returns = getattr(function, "returns", None)
    if returns is None or _is_any(returns, bindings):
        return {}, [], {}, []
    if _is_none(returns):
        return {"type": "null"}, [], {}, []
    if _opaque_dict(returns, bindings):
        return {"type": "object"}, [], {}, []
    if isinstance(returns, ast.Dict):
        try:
            parsed = parse_action_result_declaration(returns, imports=bindings.imports)
        except (ActionResultSchemaError, RecursionError, AttributeError, IndexError, KeyError, TypeError, ValueError):
            return _infer(returns), [], {}, ["<return>"]
        outputs = parsed.to_dict()["outputs"]
        properties = {}
        for output in outputs:
            field = _action_value_schema(output["schema"])
            for key in ("title", "description"):
                if key in output:
                    field[key] = output[key]
            properties[output["name"]] = field
        symbols = {name: [symbol.qualified_name for symbol in group] for name, group in parsed.resource_templates if group}
        schema = {
            "type": "object",
            "properties": properties,
            "required": [output["name"] for output in outputs],
            "additionalProperties": False,
        }
        return schema, [output["name"] for output in outputs], symbols, []
    declaration = _record_class(returns, bindings)
    if declaration is not None:
        fields, required, symbols, inferred = _record_fields(declaration, bindings)
        if fields:
            schema = {"type": "object", "properties": fields, "required": required, "additionalProperties": False}
            return schema, list(fields), symbols, inferred
    # 非具名结果的单值注解（bool / str / list[float] / Literal / Optional ...）：同一严格解析器
    try:
        value = parse_result_annotation("result", returns, imports=bindings.imports).to_dict()
    except (AnnotationSchemaError, RecursionError, AttributeError, IndexError, KeyError, TypeError, ValueError):
        value = None
    if value is not None:
        return _action_value_schema(value["schema"]), [], {}, []
    slot = _string_slot(returns, bindings)
    if slot is not None:
        return dict(slot), [], {}, []
    nullable = is_nullable_annotation(returns, bindings)
    schema = _infer(returns)
    schema[INFER_KEY]["nullable"] = nullable
    return schema, [], {}, ["<return>"]


# ---------------------------------------------------------------------------
# 注册表补全
# ---------------------------------------------------------------------------


def finalize_action_schema(schema: Dict[str, Any], infer) -> Dict[str, Any]:
    """把草稿里的 ``x-unilabos-infer`` 占位换成推断 schema（就地修改并返回）。

    ``infer(annotation_source) -> dict`` 由注册表提供（按导入表解析类型，可能导入作者模块）。
    整体推断的返回值若是带 properties 的对象，按字段补出输出顺序（具名输出端口）。
    """

    properties = schema.get("properties") or {}
    goal = (properties.get("goal") or {}).get("properties") or {}
    for name, field in list(goal.items()):
        if isinstance(field, dict) and INFER_KEY in field:
            goal[name] = _resolve(field, infer)
    result = properties.get("result")
    if isinstance(result, dict) and INFER_KEY in result:
        resolved = _resolve(result, infer)
        _strip_reserved(resolved)
        properties["result"] = resolved
        result_properties = resolved.get("properties")
        contract = schema.get("x-unilabos-action-contract") or {}
        if resolved.get("type") == "object" and isinstance(result_properties, dict) and result_properties:
            contract["output_order"] = list(result_properties)
    elif isinstance(result, dict):
        for name, field in list((result.get("properties") or {}).items()):
            if isinstance(field, dict) and INFER_KEY in field:
                result["properties"][name] = _resolve(field, infer)
    return schema


def _resolve(field: Dict[str, Any], infer) -> Dict[str, Any]:
    marker = field.get(INFER_KEY) or {}
    try:
        inferred = dict(infer(str(marker.get("annotation") or "")) or {})
    except Exception:  # noqa: BLE001 - 推断失败退回接受任意值，不能让动作注册失败
        inferred = {}
    if marker.get("nullable"):
        inferred = _with_null(inferred)
    rest = {key: value for key, value in field.items() if key != INFER_KEY}
    for key in ("title", "description"):
        if key in rest:
            inferred[key] = rest.pop(key)
    inferred.update(rest)
    return inferred


def _strip_reserved(schema: Dict[str, Any]) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name in RESERVED_RESULT_FIELDS:
            properties.pop(name, None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [name for name in required if name not in RESERVED_RESULT_FIELDS]


__all__ = [
    "INFER_KEY",
    "SLOT_KEY",
    "slot_kind",
    "ActionSchemaError",
    "ModuleBindings",
    "build_action_schema_draft",
    "finalize_action_schema",
    "framework_parameter_names",
    "reject_removed_annotations",
    "warn_once",
]
