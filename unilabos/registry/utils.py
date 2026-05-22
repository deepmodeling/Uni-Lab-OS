"""
注册表工具函数

从 registry.py 中提取的纯工具函数，包括：
- docstring 解析
- 类型字符串 → JSON Schema 转换
- AST 类型节点解析
- TypedDict / Slot / Handle 等辅助检测
"""

import inspect
import logging
import re
import typing
from typing import Any, Dict, List, Optional, Tuple, Union

from msgcenterpy.instances.typed_dict_instance import TypedDictMessageInstance

from unilabos.utils.cls_creator import import_class
from unilabos.registry.decorators import Side, DataSource, normalize_enum_value

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ROSMsgNotFound(Exception):
    pass


# ---------------------------------------------------------------------------
# Docstring 解析 (Google-style)
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(r"^(\w[\w\s]*):\s*$")
_PARAM_HEADER_RE = re.compile(
    r"^\s*(?P<name>\w[\w]*)\s*(?:\[(?P<display_name>[^\]]+)\])?(?:\s*\([^)]*\))?\s*$"
)


def _parse_docstring_param_header(param_part: str) -> Tuple[str, Optional[str]]:
    """Parse ``name[display_name]`` or Google-style ``name (type)``."""
    match = _PARAM_HEADER_RE.match(param_part.strip())
    if not match:
        return param_part.strip().split("(")[0].strip(), None

    display_name = match.group("display_name")
    if display_name is not None:
        display_name = display_name.strip() or None
    return match.group("name").strip(), display_name


def parse_docstring(docstring: Optional[str]) -> Dict[str, Any]:
    """
    解析 docstring，提取描述和参数说明。

    支持:
      - Google-style ``Args:`` / ``Parameters:`` 小节
      - 直接参数行 ``field: desc``
      - 带显示名参数行 ``field[Display Name]: desc``

    Returns:
        {
            "description": "短描述",
            "params": {"param1": "参数1描述", ...},
            "param_display_names": {"param1": "显示名", ...},
        }
    """
    result: Dict[str, Any] = {"description": "", "params": {}, "param_display_names": {}}
    if not docstring:
        return result

    lines = docstring.strip().splitlines()
    if not lines:
        return result

    in_args = False
    current_section: Optional[str] = None
    current_param: Optional[str] = None
    current_display_name: Optional[str] = None
    current_desc_parts: list = []

    def flush_current_param() -> None:
        nonlocal current_param, current_display_name, current_desc_parts
        if current_param is None:
            return
        result["params"][current_param] = "\n".join(current_desc_parts).strip()
        if current_display_name:
            result["param_display_names"][current_param] = current_display_name
        current_param = None
        current_display_name = None
        current_desc_parts = []

    first_line = lines[0].strip()
    start_index = 0
    if not _SECTION_RE.match(first_line) and ":" not in first_line:
        result["description"] = first_line
        start_index = 1

    for line in lines[start_index:]:
        stripped = line.strip()
        if not stripped:
            if current_param is not None:
                current_desc_parts.append("")
            continue

        section_match = _SECTION_RE.match(stripped)
        if section_match:
            flush_current_param()
            current_section = section_match.group(1).lower()
            in_args = current_section in ("args", "arguments", "parameters", "params")
            continue

        parse_as_param = in_args or current_section is None
        if not parse_as_param:
            continue

        if ":" in stripped:
            flush_current_param()
            param_part, _, desc_part = stripped.partition(":")
            param_name, display_name = _parse_docstring_param_header(param_part)
            current_param = param_name
            current_display_name = display_name
            current_desc_parts = [desc_part.strip()]
        elif current_param is not None:
            aline = line
            if aline.startswith("    "):
                aline = aline[4:]
            elif aline.startswith("\t"):
                aline = aline[1:]
            current_desc_parts.append(aline.strip())

    flush_current_param()

    return result


# ---------------------------------------------------------------------------
# 类型常量
# ---------------------------------------------------------------------------

SIMPLE_TYPE_MAP = {
    "str": "string",
    "string": "string",
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "list": "array",
    "array": "array",
    "dict": "object",
    "object": "object",
}

ARRAY_TYPES = {"list", "List", "tuple", "Tuple", "set", "Set", "Sequence", "Iterable"}
OBJECT_TYPES = {"dict", "Dict", "Mapping"}
WRAPPER_TYPES = {"Optional"}
SLOT_TYPES = {"ResourceSlot", "DeviceSlot"}


# ---------------------------------------------------------------------------
# 简单类型映射
# ---------------------------------------------------------------------------


def get_json_schema_type(type_str: str) -> str:
    """简单类型名 -> JSON Schema type"""
    return SIMPLE_TYPE_MAP.get(type_str.lower(), "string")


# ---------------------------------------------------------------------------
# AST 类型解析
# ---------------------------------------------------------------------------


def parse_type_node(type_str: str):
    """将类型注解字符串解析为 AST 节点，失败返回 None。"""
    import ast as _ast

    try:
        return _ast.parse(type_str.strip(), mode="eval").body
    except Exception:
        return None


def _collect_bitor(node, out: list):
    """递归收集 X | Y | Z 的所有分支。"""
    import ast as _ast

    if isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.BitOr):
        _collect_bitor(node.left, out)
        _collect_bitor(node.right, out)
    else:
        out.append(node)


def type_node_to_schema(
    node,
    import_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """将 AST 类型注解节点递归转换为 JSON Schema dict。

    当提供 import_map 时，对于未知类名会尝试通过 import_map 解析模块路径，
    然后 import 真实类型对象来生成 schema (支持 TypedDict 等)。

    映射规则:
    - Optional[X]                    → X 的 schema (剥掉 Optional)
    - Union[X, Y]                    → {"anyOf": [X_schema, Y_schema]}
    - List[X] / Tuple[X] / Set[X]   → {"type": "array", "items": X_schema}
    - Dict[K, V]                     → {"type": "object", "additionalProperties": V_schema}
    - Literal["a", "b"]             → {"type": "string", "enum": ["a", "b"]}
    - TypedDict (via import_map)     → {"type": "object", "properties": {...}}
    - 基本类型 str/int/...          → {"type": "string"/"integer"/...}
    """
    import ast as _ast

    # --- Name 节点: str / int / dict / ResourceSlot / 自定义类 ---
    if isinstance(node, _ast.Name):
        name = node.id
        if name in SLOT_TYPES:
            return {"$slot": name}
        json_type = SIMPLE_TYPE_MAP.get(name.lower())
        if json_type:
            return {"type": json_type}
        # 尝试通过 import_map 解析并 import 真实类型
        if import_map and name in import_map:
            type_obj = resolve_type_object(import_map[name])
            if type_obj is not None:
                return type_to_schema(type_obj)
        # 未知类名 → 无法转 schema 的自定义类型默认当 object
        return {"type": "object"}

    if isinstance(node, _ast.Constant):
        if isinstance(node.value, str):
            return {"type": SIMPLE_TYPE_MAP.get(node.value.lower(), "string")}
        return {"type": "string"}

    # --- Subscript 节点: List[X], Dict[K,V], Optional[X], Literal[...] 等 ---
    if isinstance(node, _ast.Subscript):
        base_name = node.value.id if isinstance(node.value, _ast.Name) else ""

        # Optional[X] → 剥掉
        if base_name in WRAPPER_TYPES:
            return type_node_to_schema(node.slice, import_map)

        # Union[X, None] → 剥掉 None; Union[X, Y] → anyOf
        if base_name == "Union":
            elts = node.slice.elts if isinstance(node.slice, _ast.Tuple) else [node.slice]
            non_none = [
                e
                for e in elts
                if not (isinstance(e, _ast.Constant) and e.value is None)
                and not (isinstance(e, _ast.Name) and e.id == "None")
            ]
            if len(non_none) == 1:
                return type_node_to_schema(non_none[0], import_map)
            if len(non_none) > 1:
                return {"anyOf": [type_node_to_schema(e, import_map) for e in non_none]}
            return {"type": "string"}

        # Literal["a", "b", 1] → enum
        if base_name == "Literal":
            elts = node.slice.elts if isinstance(node.slice, _ast.Tuple) else [node.slice]
            values = []
            for e in elts:
                if isinstance(e, _ast.Constant):
                    values.append(e.value)
                elif isinstance(e, _ast.Name):
                    values.append(e.id)
            if values:
                return {"type": "string", "enum": values}
            return {"type": "string"}

        # List / Tuple / Set → array
        if base_name in ARRAY_TYPES:
            if isinstance(node.slice, _ast.Tuple) and node.slice.elts:
                inner_node = node.slice.elts[0]
            else:
                inner_node = node.slice
            return {"type": "array", "items": type_node_to_schema(inner_node, import_map)}

        # Dict → object
        if base_name in OBJECT_TYPES:
            schema: Dict[str, Any] = {"type": "object"}
            if isinstance(node.slice, _ast.Tuple) and len(node.slice.elts) >= 2:
                val_node = node.slice.elts[1]
                # Dict[str, Any] → 不加 additionalProperties (Any 等同于无约束)
                is_any = (isinstance(val_node, _ast.Name) and val_node.id == "Any") or (
                    isinstance(val_node, _ast.Constant) and val_node.value is None
                )
                if not is_any:
                    val_schema = type_node_to_schema(val_node, import_map)
                    schema["additionalProperties"] = val_schema
            return schema

    # --- BinOp: X | Y (Python 3.10+) → 当 Union 处理 ---
    if isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.BitOr):
        parts: list = []
        _collect_bitor(node, parts)
        non_none = [
            p
            for p in parts
            if not (isinstance(p, _ast.Constant) and p.value is None)
            and not (isinstance(p, _ast.Name) and p.id == "None")
        ]
        if len(non_none) == 1:
            return type_node_to_schema(non_none[0], import_map)
        if len(non_none) > 1:
            return {"anyOf": [type_node_to_schema(p, import_map) for p in non_none]}
        return {"type": "string"}

    return {"type": "string"}


# ---------------------------------------------------------------------------
# 真实类型对象解析 (import-based)
# ---------------------------------------------------------------------------


def resolve_type_object(type_ref: str) -> Optional[Any]:
    """通过 'module.path:ClassName' 格式的引用 import 并返回真实类型对象。

    对于 typing 内置名 (str, int, List 等) 直接返回 None (由 AST 路径处理)。
    import 失败时静默返回 None。
    """
    if ":" not in type_ref:
        return None
    try:
        return import_class(type_ref)
    except Exception:
        return None


def is_typed_dict_class(obj: Any) -> bool:
    """检查对象是否是 TypedDict 类。"""
    if obj is None:
        return False
    try:
        from typing_extensions import is_typeddict

        return is_typeddict(obj)
    except ImportError:
        if isinstance(obj, type):
            return hasattr(obj, "__required_keys__") and hasattr(obj, "__optional_keys__")
        return False


def type_to_schema(tp: Any) -> Dict[str, Any]:
    """将真实 typing 对象递归转换为 JSON Schema dict。

    支持:
    - 基本类型: str, int, float, bool → {"type": "string"/"integer"/...}
    - typing 泛型: List[X], Dict[K,V], Optional[X], Union[X,Y], Literal[...]
    - TypedDict → {"type": "object", "properties": {...}, "required": [...]}
    - 自定义类 (ResourceSlot 等) → {"$slot": "..."} 或 {"type": "string"}
    """
    origin = getattr(tp, "__origin__", None)
    args = getattr(tp, "__args__", None)

    # --- None / NoneType ---
    if tp is type(None):
        return {"type": "null"}

    # --- 基本类型 ---
    if tp is str:
        return {"type": "string"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is bool:
        return {"type": "boolean"}

    # --- TypedDict ---
    if is_typed_dict_class(tp):
        try:
            return TypedDictMessageInstance.get_json_schema_from_typed_dict(tp)
        except Exception:
            return {"type": "object"}

    # --- Literal ---
    if origin is typing.Literal:
        values = list(args) if args else []
        return {"type": "string", "enum": values}

    # --- Optional / Union ---
    if origin is typing.Union:
        non_none = [a for a in (args or ()) if a is not type(None)]
        if len(non_none) == 1:
            return type_to_schema(non_none[0])
        if len(non_none) > 1:
            return {"anyOf": [type_to_schema(a) for a in non_none]}
        return {"type": "string"}

    # --- List / Sequence / Set / Tuple / Iterable ---
    if origin in (list, tuple, set, frozenset) or (
        origin is not None
        and getattr(origin, "__name__", "") in ("Sequence", "Iterable", "Iterator", "MutableSequence")
    ):
        if args:
            return {"type": "array", "items": type_to_schema(args[0])}
        return {"type": "array"}

    # --- Dict / Mapping ---
    if origin in (dict,) or (origin is not None and getattr(origin, "__name__", "") in ("Mapping", "MutableMapping")):
        schema: Dict[str, Any] = {"type": "object"}
        if args and len(args) >= 2:
            schema["additionalProperties"] = type_to_schema(args[1])
        return schema

    # --- Slot 类型 ---
    if isinstance(tp, type):
        name = tp.__name__
        if name in SLOT_TYPES:
            return {"$slot": name}

    # --- 其他未知类型 fallback ---
    if isinstance(tp, type):
        return {"type": "object"}
    return {"type": "string"}


# ---------------------------------------------------------------------------
# Slot / Placeholder 检测
# ---------------------------------------------------------------------------


def detect_slot_type(ptype) -> Tuple[Optional[str], bool]:
    """检测参数类型是否为 ResourceSlot / DeviceSlot。

    兼容多种格式:
    - runtime: "unilabos.registry.placeholder_type:ResourceSlot"
    - runtime tuple: ("list", "unilabos.registry.placeholder_type:ResourceSlot")
    - AST 裸名: "ResourceSlot", "List[ResourceSlot]", "Optional[ResourceSlot]"

    Returns: (slot_name | None, is_list)
    """
    ptype_str = str(ptype)

    # 快速路径: 字符串里根本没有 Slot
    if "ResourceSlot" not in ptype_str and "DeviceSlot" not in ptype_str:
        return (None, False)

    # runtime 格式: 完整模块路径
    if isinstance(ptype, str):
        if ptype.endswith(":ResourceSlot") or ptype == "ResourceSlot":
            return ("ResourceSlot", False)
        if ptype.endswith(":DeviceSlot") or ptype == "DeviceSlot":
            return ("DeviceSlot", False)
        # AST 复杂格式: List[ResourceSlot], Optional[ResourceSlot] 等
        if "[" in ptype:
            node = parse_type_node(ptype)
            if node is not None:
                schema = type_node_to_schema(node)
                # 直接是 slot
                if "$slot" in schema:
                    return (schema["$slot"], False)
                # array 包裹 slot: {"type": "array", "items": {"$slot": "..."}}
                items = schema.get("items", {})
                if isinstance(items, dict) and "$slot" in items:
                    return (items["$slot"], True)
        return (None, False)

    # runtime tuple 格式
    if isinstance(ptype, tuple) and len(ptype) == 2:
        inner_str = str(ptype[1])
        if "ResourceSlot" in inner_str:
            return ("ResourceSlot", True)
        if "DeviceSlot" in inner_str:
            return ("DeviceSlot", True)

    return (None, False)


def detect_placeholder_keys(params: list) -> Dict[str, str]:
    """Detect parameters that reference ResourceSlot or DeviceSlot."""
    result: Dict[str, str] = {}
    for p in params:
        ptype = p.get("type", "")
        if "ResourceSlot" in str(ptype):
            result[p["name"]] = "unilabos_resources"
        elif "DeviceSlot" in str(ptype):
            result[p["name"]] = "unilabos_devices"
    return result


# ---------------------------------------------------------------------------
# Handle 规范化
# ---------------------------------------------------------------------------


def normalize_ast_handles(handles_raw: Any) -> List[Dict[str, Any]]:
    """Convert AST-parsed handle structures to the standard registry format."""
    if not handles_raw:
        return []

    # handle_type → io_type 映射 (AST 内部类名 → YAML 标准字段值)
    _HANDLE_TYPE_TO_IO_TYPE = {
        "input": "target",
        "output": "source",
        "action_input": "action_target",
        "action_output": "action_source",
    }

    result: List[Dict[str, Any]] = []
    for h in handles_raw:
        if isinstance(h, dict):
            call = h.get("_call", "")
            if "InputHandle" in call:
                handle_type = "input"
            elif "OutputHandle" in call:
                handle_type = "output"
            elif "ActionInputHandle" in call:
                handle_type = "action_input"
            elif "ActionOutputHandle" in call:
                handle_type = "action_output"
            else:
                handle_type = h.get("handle_type", "unknown")

            io_type = _HANDLE_TYPE_TO_IO_TYPE.get(handle_type, handle_type)

            entry: Dict[str, Any] = {
                "handler_key": h.get("key", ""),
                "data_type": h.get("data_type", ""),
                "io_type": io_type,
            }
            side = h.get("side")
            if side:
                entry["side"] = normalize_enum_value(side, Side) or side
            label = h.get("label")
            if label:
                entry["label"] = label
            data_key = h.get("data_key")
            if data_key:
                entry["data_key"] = data_key
            data_source = h.get("data_source")
            if data_source:
                entry["data_source"] = normalize_enum_value(data_source, DataSource) or data_source
            description = h.get("description")
            if description:
                entry["description"] = description

            result.append(entry)
    return result


def normalize_ast_action_handles(handles_raw: Any) -> Dict[str, Any]:
    """Convert AST-parsed action handle list to {"input": [...], "output": [...]}.

    Mirrors the runtime behavior of decorators._action_handles_to_dict:
      - ActionInputHandle  => grouped under "input"
      - ActionOutputHandle => grouped under "output"
    Field mapping: key -> handler_key (matches Pydantic serialization_alias).
    """
    if not handles_raw or not isinstance(handles_raw, list):
        return {}

    input_list: List[Dict[str, Any]] = []
    output_list: List[Dict[str, Any]] = []

    for h in handles_raw:
        if not isinstance(h, dict):
            continue
        call = h.get("_call", "")
        is_input = "ActionInputHandle" in call or "InputHandle" in call
        is_output = "ActionOutputHandle" in call or "OutputHandle" in call

        entry: Dict[str, Any] = {
            "handler_key": h.get("key", ""),
            "data_type": h.get("data_type", ""),
            "label": h.get("label", ""),
        }
        _FIELD_ENUM_MAP = {"side": Side, "data_source": DataSource}
        for opt_key in ("side", "data_key", "data_source", "description", "io_type"):
            val = h.get(opt_key)
            if val is not None:
                if opt_key in _FIELD_ENUM_MAP:
                    val = normalize_enum_value(val, _FIELD_ENUM_MAP[opt_key]) or val
                entry[opt_key] = val

        # io_type: only add when explicitly set; do not default output to "sink" (YAML convention omits it)
        if "io_type" not in entry and is_input:
            entry["io_type"] = "source"

        if is_input:
            input_list.append(entry)
        elif is_output:
            output_list.append(entry)

    result: Dict[str, Any] = {}
    if input_list:
        result["input"] = input_list
    # Always include output (empty list when no outputs) to match YAML
    result["output"] = output_list
    return result


# ---------------------------------------------------------------------------
# Schema 辅助
# ---------------------------------------------------------------------------


def wrap_action_schema(
    goal_schema: Dict[str, Any],
    action_name: str,
    description: str = "",
    result_schema: Optional[Dict[str, Any]] = None,
    feedback_schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    将 goal 参数 schema 包装为标准的 action schema 格式:
    { "properties": { "goal": ..., "feedback": ..., "result": ... }, ... }
    """
    # 去掉 auto- 前缀用于 title/description，与 YAML 路径保持一致
    display_name = action_name.removeprefix("auto-")
    return {
        "title": f"{display_name}参数",
        "description": description or f"{display_name}的参数schema",
        "type": "object",
        "properties": {
            "goal": goal_schema,
            "feedback": feedback_schema or {},
            "result": result_schema or {},
        },
        "required": ["goal"],
    }


def preserve_field_descriptions(new_schema: Dict[str, Any], prev_schema: Dict[str, Any]):
    """递归保留之前 schema 中各字段的 description / title。

    覆盖顶层以及嵌套 properties（如 goal.properties.xxx.description）。
    """
    if not prev_schema or not new_schema:
        return
    prev_props = prev_schema.get("properties", {})
    new_props = new_schema.get("properties", {})
    for field_name, prev_field in prev_props.items():
        if field_name not in new_props:
            continue
        new_field = new_props[field_name]
        if not isinstance(prev_field, dict) or not isinstance(new_field, dict):
            continue
        if "title" in prev_field:
            new_field.setdefault("title", prev_field["title"])
        if "description" in prev_field:
            new_field.setdefault("description", prev_field["description"])
        if "properties" in prev_field and "properties" in new_field:
            preserve_field_descriptions(new_field, prev_field)


def strip_ros_descriptions(schema: Any):
    """递归清除 ROS schema 中自动生成的无意义 description（含 rosidl_parser 内存地址）。"""
    if isinstance(schema, dict):
        desc = schema.get("description", "")
        if isinstance(desc, str) and "rosidl_parser" in desc:
            del schema["description"]
        for v in schema.values():
            strip_ros_descriptions(v)
    elif isinstance(schema, list):
        for item in schema:
            strip_ros_descriptions(item)


# ---------------------------------------------------------------------------
# 深度对比
# ---------------------------------------------------------------------------


def _short(val, limit=120):
    """截断过长的值用于日志显示。"""
    s = repr(val)
    return s if len(s) <= limit else s[:limit] + "..."


def deep_diff(old, new, path="", max_depth=10) -> list:
    """递归对比两个对象，返回所有差异的描述列表。"""
    diffs = []
    if max_depth <= 0:
        if old != new:
            diffs.append(f"{path}: (达到最大深度) OLD≠NEW")
        return diffs

    if type(old) != type(new):
        diffs.append(f"{path}: 类型不同 OLD={type(old).__name__}({_short(old)}) NEW={type(new).__name__}({_short(new)})")
        return diffs

    if isinstance(old, dict):
        old_keys = set(old.keys())
        new_keys = set(new.keys())
        for k in sorted(new_keys - old_keys):
            diffs.append(f"{path}.{k}: 新增字段 (AST有, YAML无) = {_short(new[k])}")
        for k in sorted(old_keys - new_keys):
            diffs.append(f"{path}.{k}: 缺失字段 (YAML有, AST无) = {_short(old[k])}")
        for k in sorted(old_keys & new_keys):
            diffs.extend(deep_diff(old[k], new[k], f"{path}.{k}", max_depth - 1))
    elif isinstance(old, (list, tuple)):
        if len(old) != len(new):
            diffs.append(f"{path}: 列表长度不同 OLD={len(old)} NEW={len(new)}")
        for i in range(min(len(old), len(new))):
            diffs.extend(deep_diff(old[i], new[i], f"{path}[{i}]", max_depth - 1))
        if len(new) > len(old):
            for i in range(len(old), len(new)):
                diffs.append(f"{path}[{i}]: 新增元素 = {_short(new[i])}")
        elif len(old) > len(new):
            for i in range(len(new), len(old)):
                diffs.append(f"{path}[{i}]: 缺失元素 = {_short(old[i])}")
    else:
        if old != new:
            diffs.append(f"{path}: OLD={_short(old)} NEW={_short(new)}")
    return diffs


# ---------------------------------------------------------------------------
# MRO 方法参数解析
# ---------------------------------------------------------------------------


def resolve_method_params_via_import(module_str: str, method_name: str) -> Dict[str, str]:
    """当 AST 方法参数为空 (如 *args, **kwargs) 时, import class 并通过 MRO 获取真实方法参数.

    返回 identity mapping {param_name: param_name}.
    """
    if not module_str or ":" not in module_str:
        return {}
    try:
        cls = import_class(module_str)
    except Exception as e:
        _logger.debug(f"[AST] resolve_method_params_via_import: import_class('{module_str}') failed: {e}")
        return {}

    try:
        for base_cls in cls.__mro__:
            if method_name not in base_cls.__dict__:
                continue
            method = base_cls.__dict__[method_name]
            actual = getattr(method, "__wrapped__", method)
            if isinstance(actual, (staticmethod, classmethod)):
                actual = actual.__func__
            if not callable(actual):
                continue
            sig = inspect.signature(actual, follow_wrapped=True)
            params = [
                p.name for p in sig.parameters.values()
                if p.name not in ("self", "cls")
                and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            ]
            if params:
                return {p: p for p in params}
    except Exception as e:
        _logger.debug(f"[AST] resolve_method_params_via_import: MRO walk for '{method_name}' failed: {e}")
    return {}
