"""把第 2 版动作合同（Action Contract）编译为 Backend 形状连接点。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from unilabos.workflow.models import validate_uuid


class ActionTemplateProjectionError(ValueError):
    """动作合同无法安全编译为工作流动作模板。"""


def compile_action_template_handles(
    schema: Mapping[str, Any],
    *,
    node_business_key: tuple[str, str],
    resource_template_identity_resolver: Callable[[str], str] | None,
) -> list[dict[str, Any]]:
    """生成 Backend 规范的控制连接点和动作数据连接点。

    参数说明：``schema`` 是完整第 2 版动作合同；``node_business_key`` 在原子
    投影事务中解析父节点 UUID；``resource_template_identity_resolver`` 把源码
    资源模板身份解析为本地稳定 UUID；传入 ``None`` 时只编译 Backend 旧式 DTO
    可表达的基础连接点。返回值包含两个 ready 控制连接点、显式输入/输出，以及
    缺少同名显式输出时的隐式物料传递连接点。
    """

    contract = schema.get("x-unilabos-action-contract")
    if not isinstance(contract, Mapping) or contract.get("version") != 2:
        raise ActionTemplateProjectionError("只接受第 2 版动作合同")
    goal_schema = _object_property(schema, "goal")
    result_schema = _object_property(schema, "result")
    input_order = _ordered_keys(contract, "input_order")
    output_order = _ordered_keys(contract, "output_order")
    symbols = _resource_template_symbols(
        contract,
        goal_fields=_property_keys(goal_schema),
        result_fields=_property_keys(result_schema),
    )

    # ``input_handles`` 与 ``output_handles`` 是动作合同显式声明的数据连接点。
    input_handles = _compile_data_handles(
        node_business_key=node_business_key,
        io_type="target",
        data_source="goal",
        ordered_keys=input_order,
        object_schema=goal_schema,
        symbol_fields=symbols["goal"],
        resource_template_identity_resolver=resource_template_identity_resolver,
        order_offset=0,
    )
    output_handles = _compile_data_handles(
        node_business_key=node_business_key,
        io_type="source",
        data_source="executor",
        ordered_keys=output_order,
        object_schema=result_schema,
        symbol_fields=symbols["result"],
        resource_template_identity_resolver=resource_template_identity_resolver,
        order_offset=len(input_handles),
    )
    # ``passthrough_handles`` 保持输入物料身份跨动作节点连续可追踪。
    passthrough_handles = _implicit_material_passthrough_handles(
        input_handles=input_handles,
        output_handles=output_handles,
        order_offset=len(input_handles) + len(output_handles),
    )
    return [
        *_ready_handles(node_business_key=node_business_key),
        *input_handles,
        *output_handles,
        *passthrough_handles,
    ]


def goal_parameter_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """取得 Backend 节点模板使用的 goal 参数子模式。

    参数说明：``schema`` 是完整第 2 版动作合同；返回与输入容器不共享的 goal
    参数模式，使 OS 与 Backend 的 ``workflow_node_template.schema`` 语义一致。
    """

    return _copy_json(_object_property(schema, "goal"))


def compile_backend_action_handles(
    schema: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """把同一 v2 推导结果降级为 Backend 当前模板同步 Handles DTO。

    参数说明：``schema`` 是完整第 2 版动作合同；返回 ``input/output`` 两组基础
    字段，不发送 ready（由 Backend 生成），也不伪造 Backend 当前 DTO 无法承载的
    必填性或元数据。物料、库位（Site）和隐式传递的推导仍与本地投影共源。
    """

    candidates = compile_action_template_handles(
        schema,
        node_business_key=("backend-sync", "action"),
        resource_template_identity_resolver=None,
    )
    result: dict[str, list[dict[str, Any]]] = {"input": [], "output": []}
    for candidate in candidates:
        if candidate["handle_key"] == "ready":
            continue
        direction = "input" if candidate["io_type"] == "target" else "output"
        result[direction].append(
            {
                "label": candidate["display_name"],
                "data_key": candidate["data_key"],
                "data_type": candidate["type"],
                "data_source": candidate["data_source"],
                "handler_key": candidate["handle_key"],
            }
        )
    return result


def _ready_handles(
    *,
    node_business_key: tuple[str, str],
) -> list[dict[str, Any]]:
    """生成 Backend 规范的一对 ready 结构控制连接点。

    参数说明：``node_business_key`` 在同一投影事务中解析父节点 UUID；返回一个
    输入侧 ``target:ready`` 和一个输出侧 ``source:ready``。它们只表达工作流
    控制依赖，不带动作参数的数据键或物料占位符（ResourceSlot）元数据。
    """

    return [
        {
            "node_business_key": node_business_key,
            "handle_key": "ready",
            "io_type": io_type,
            "display_name": "ready",
            "description": None,
            "type": "default",
            "required": False,
            "data_source": None,
            "data_key": None,
            "meta_data": {},
        }
        for io_type in ("target", "source")
    ]


def _compile_data_handles(
    *,
    node_business_key: tuple[str, str],
    io_type: str,
    data_source: str,
    ordered_keys: Sequence[str],
    object_schema: Mapping[str, Any],
    symbol_fields: Mapping[str, Sequence[str]],
    resource_template_identity_resolver: Callable[[str], str] | None,
    order_offset: int,
) -> list[dict[str, Any]]:
    """按动作合同稳定顺序编译一组显式数据连接点。

    参数说明：``node_business_key`` 标识父节点业务身份；``io_type`` 与
    ``data_source`` 分别表达连接方向和 Backend 数据来源；``ordered_keys`` 是合同
    顺序；``object_schema`` 提供字段模式和必填性；``symbol_fields`` 保存资源模板
    源码约束；``resource_template_identity_resolver`` 解析本地 UUID；
    ``order_offset`` 合并输入输出顺序。返回完整连接点候选。
    """

    properties = object_schema.get("properties") or {}
    required_keys = object_schema.get("required") or []
    if not isinstance(properties, Mapping):
        raise ActionTemplateProjectionError("动作字段 properties 必须是对象")
    if not isinstance(required_keys, Sequence) or isinstance(
        required_keys, (str, bytes)
    ):
        raise ActionTemplateProjectionError("动作字段 required 必须是数组")
    handles: list[dict[str, Any]] = []
    for index, handle_key in enumerate(ordered_keys):
        value_schema = properties.get(handle_key)
        if not isinstance(value_schema, Mapping):
            raise ActionTemplateProjectionError("动作合同顺序引用未知字段")
        # ``is_material`` 表示虚拟物料传递语义，不等于执行时已经取得物料锁。
        is_material = _is_material_schema(value_schema)
        editor_control = value_schema.get("x-unilabos-editor-control")
        if is_material:
            editor_control = "material_port"
        elif editor_control != "site_selector":
            editor_control = "variable_selector"
        # ``raw_site_selector`` 是库位选择（Site Selection）的完整关系合同；它与
        # 控件类型必须成对出现，投影层禁止把破损合同降级为普通变量。
        raw_site_selector = value_schema.get("x-unilabos-site-selector")
        if editor_control == "site_selector":
            if not isinstance(raw_site_selector, Mapping):
                raise ActionTemplateProjectionError(
                    "库位选择控件缺少完整库位选择合同"
                )
        elif raw_site_selector is not None:
            raise ActionTemplateProjectionError(
                "非库位选择控件不能携带库位选择合同"
            )
        # ``site_selector`` 是已由动作合同编译器验证的完整库位关系；连接点投影
        # 直接携带副本，使前端不必按参数名或动作根 Schema 猜测 owner/occupant。
        site_selector = raw_site_selector
        allowed_template_uuids = _resolve_allowed_template_uuids(
            symbol_fields.get(handle_key),
            resolver=resource_template_identity_resolver,
        )
        handles.append(
            {
                "node_business_key": node_business_key,
                "handle_key": handle_key,
                "io_type": io_type,
                "display_name": value_schema.get("title") or handle_key,
                "description": value_schema.get("description"),
                "type": _handle_type(value_schema),
                "required": handle_key in required_keys if io_type == "target" else False,
                "data_source": data_source,
                "data_key": handle_key,
                "meta_data": {
                    "unilab": {
                        "value_schema": _copy_json(value_schema),
                        "contract_order": order_offset + index,
                        "editor_control": editor_control,
                        "site_selector": (
                            _copy_json(site_selector)
                            if isinstance(site_selector, Mapping)
                            else None
                        ),
                        "allowed_resource_template_uuids": allowed_template_uuids,
                        "implicit_passthrough": False,
                        "structural_role": None,
                    }
                },
            }
        )
    return handles


def _implicit_material_passthrough_handles(
    *,
    input_handles: Sequence[Mapping[str, Any]],
    output_handles: Sequence[Mapping[str, Any]],
    order_offset: int,
) -> list[dict[str, Any]]:
    """为缺少同名显式输出的物料输入生成隐式 source 连接点。

    参数说明：``input_handles`` 和 ``output_handles`` 是显式连接点；
    ``order_offset`` 是隐式输出起始顺序。返回值不会复制普通标量，也不会保留只
    属于动作输入的物料锁标记。
    """

    output_keys = {str(handle["handle_key"]) for handle in output_handles}
    passthrough_handles: list[dict[str, Any]] = []
    for input_handle in input_handles:
        unilab_meta = input_handle["meta_data"]["unilab"]
        value_schema = unilab_meta["value_schema"]
        if not _is_material_schema(value_schema):
            continue
        handle_key = str(input_handle["handle_key"])
        if handle_key in output_keys:
            continue
        # ``output_schema`` 删除物料锁标记，防止输出被误解释为新的锁申请。
        output_schema = _without_material_lock_marker(value_schema)
        passthrough_handles.append(
            {
                "node_business_key": input_handle["node_business_key"],
                "handle_key": handle_key,
                "io_type": "source",
                "display_name": input_handle["display_name"],
                "description": input_handle.get("description"),
                "type": _handle_type(output_schema),
                "required": False,
                "data_source": "executor",
                "data_key": handle_key,
                "meta_data": {
                    "unilab": {
                        "value_schema": output_schema,
                        "contract_order": order_offset + len(passthrough_handles),
                        "editor_control": "material_port",
                        "allowed_resource_template_uuids": unilab_meta[
                            "allowed_resource_template_uuids"
                        ],
                        "implicit_passthrough": True,
                        "structural_role": None,
                    }
                },
            }
        )
    return passthrough_handles


def _object_property(schema: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """取得动作合同中的对象子模式。

    参数说明：``schema`` 是动作根模式，``key`` 是 goal 或 result；返回对象模式，
    缺失 result 时使用空对象以支持无输出动作。
    """

    properties = schema.get("properties") or {}
    if not isinstance(properties, Mapping):
        raise ActionTemplateProjectionError("动作 Schema properties 必须是对象")
    value = properties.get(key) or {}
    if not isinstance(value, Mapping):
        raise ActionTemplateProjectionError(f"动作 {key} Schema 必须是对象")
    return value


def _property_keys(schema: Mapping[str, Any]) -> set[str]:
    """返回对象模式的字符串字段名集合。

    参数说明：``schema`` 是 goal/result 对象模式；返回 properties 的键集合，结构
    非法时关闭式失败。
    """

    properties = schema.get("properties") or {}
    if not isinstance(properties, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, Mapping)
        for key, value in properties.items()
    ):
        raise ActionTemplateProjectionError("动作字段 Schema 结构非法")
    return set(properties)


def _ordered_keys(contract: Mapping[str, Any], field: str) -> tuple[str, ...]:
    """读取并验证动作合同字段顺序。

    参数说明：``contract`` 是版本扩展，``field`` 是 input_order/output_order；返回
    不含重复或空值的字符串元组。
    """

    raw_order = contract.get(field) or []
    if not isinstance(raw_order, Sequence) or isinstance(raw_order, (str, bytes)):
        raise ActionTemplateProjectionError("动作合同字段顺序必须是数组")
    ordered_keys = tuple(raw_order)
    if any(not isinstance(key, str) or not key for key in ordered_keys):
        raise ActionTemplateProjectionError("动作合同字段顺序包含非法字段")
    if len(set(ordered_keys)) != len(ordered_keys):
        raise ActionTemplateProjectionError("动作合同字段顺序包含重复字段")
    return ordered_keys


def _resource_template_symbols(
    contract: Mapping[str, Any],
    *,
    goal_fields: set[str],
    result_fields: set[str],
) -> dict[str, Mapping[str, Sequence[str]]]:
    """验证并返回 goal/result 的资源模板源码约束。

    参数说明：``contract`` 是版本扩展；两个字段集合限制符号只能引用真实动作
    字段。返回两组只读映射；非法成员、空符号或未知字段都会关闭式失败。
    """

    raw_symbols = contract.get("resource_template_symbols")
    if not isinstance(raw_symbols, Mapping):
        raise ActionTemplateProjectionError("动作合同缺少资源模板符号")
    result: dict[str, Mapping[str, Sequence[str]]] = {}
    for section, field_names in (("goal", goal_fields), ("result", result_fields)):
        fields = raw_symbols.get(section)
        if not isinstance(fields, Mapping):
            raise ActionTemplateProjectionError("资源模板符号分组必须是对象")
        normalized: dict[str, Sequence[str]] = {}
        for field_name, values in fields.items():
            if not isinstance(field_name, str) or field_name not in field_names:
                raise ActionTemplateProjectionError("资源模板符号引用未知字段")
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise ActionTemplateProjectionError("资源模板符号列表必须是数组")
            if not values or any(not isinstance(value, str) or not value for value in values):
                raise ActionTemplateProjectionError("资源模板符号不能为空")
            normalized[field_name] = tuple(values)
        result[section] = normalized
    return result


def _resolve_allowed_template_uuids(
    symbols: Sequence[str] | None,
    *,
    resolver: Callable[[str], str] | None,
) -> list[str] | None:
    """把资源模板源码身份解析为稳定本地 UUID 允许集。

    参数说明：``symbols`` 缺失表示无限制并返回 ``None``；``resolver`` 查询本地
    资源模板身份映射；``None`` 表示 Backend 当前 DTO 无法承载允许集，只供基础
    Handles 降级投影使用。返回值按声明顺序去重；本地解析缺失或非法 UUID 时
    关闭式失败。
    """

    if symbols is None:
        return None
    if resolver is None:
        return None
    identities: list[str] = []
    for symbol in symbols:
        try:
            identity = validate_uuid(resolver(symbol))
        except (KeyError, LookupError, TypeError, ValueError):
            raise ActionTemplateProjectionError("资源模板源码身份无法解析") from None
        if identity not in identities:
            identities.append(identity)
    return identities


def _handle_type(value_schema: Mapping[str, Any]) -> str:
    """把 JSON Schema 字段映射为共享连接点类型。

    参数说明：``value_schema`` 是单个输入或输出字段；物料数组保留 ``array``
    外形，单物料映射为代码类型 ``ResourceSlot``（中文：物料占位符），其他字段
    保留非空 JSON 类型。
    """

    json_type = value_schema.get("type")
    if json_type == "array":
        return "array"
    if _is_material_schema(value_schema):
        return "ResourceSlot"
    if isinstance(json_type, list):
        non_null_types = [item for item in json_type if item != "null"]
        return str(non_null_types[0]) if len(non_null_types) == 1 else "any"
    return str(json_type) if isinstance(json_type, str) else "any"


def _is_material_schema(value_schema: Mapping[str, Any]) -> bool:
    """判断字段是否表达一个或多个物料 UUID 引用。

    参数说明：``value_schema`` 是字段模式；返回值同时识别 true/false 锁标记、
    数组成员、可空联合以及无锁标记的动作结果物料引用。
    """

    if isinstance(value_schema.get("x-unilabos-material-lock"), bool):
        return True
    items = value_schema.get("items")
    if isinstance(items, Mapping) and _is_material_schema(items):
        return True
    members = value_schema.get("anyOf")
    if (
        isinstance(members, Sequence)
        and not isinstance(members, (str, bytes))
        and any(
            isinstance(member, Mapping) and _is_material_schema(member)
            for member in members
        )
    ):
        return True
    properties = value_schema.get("properties")
    required = value_schema.get("required") or []
    if not isinstance(properties, Mapping) or set(properties) != {"uuid"}:
        return False
    uuid_schema = properties.get("uuid")
    return (
        isinstance(uuid_schema, Mapping)
        and uuid_schema.get("type") == "string"
        and uuid_schema.get("format") == "uuid"
        and "uuid" in required
    )


def _without_material_lock_marker(value: Any) -> Any:
    """深复制字段模式并移除输入专用物料锁标记。

    参数说明：``value`` 是任意 JSON 值；返回新容器，递归清理数组成员和嵌套
    对象，避免隐式 source 被误解释为新的物料锁申请。
    """

    if isinstance(value, Mapping):
        return {
            str(key): _without_material_lock_marker(item)
            for key, item in value.items()
            if key != "x-unilabos-material-lock"
        }
    if isinstance(value, list):
        return [_without_material_lock_marker(item) for item in value]
    return value


def _copy_json(value: Any) -> Any:
    """复制 JSON 兼容值并断开调用方可变容器共享。

    参数说明：``value`` 是模式片段；返回只含字典、列表和标量的新值。
    """

    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value


__all__ = [
    "ActionTemplateProjectionError",
    "compile_action_template_handles",
    "compile_backend_action_handles",
    "goal_parameter_schema",
]
