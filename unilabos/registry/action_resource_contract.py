"""动作资源合同（ActionResourceContract）的纯声明式规范化。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

TRANSFER_CONTRACT_FIELDS: tuple[str, ...] = (
    "material_param",
    "source_owner_param",
    "source_site_uuid_param",
    "source_site_name_param",
    "target_owner_param",
    "target_site_uuid_param",
    "target_site_name_param",
    "gripper_site_role",
)
TRANSFER_RESOURCE_ROLE_FIELDS: tuple[str, ...] = (
    "motion_resource_roles",
    "tool_resource_roles",
)
RESOURCE_PARAM_ROLES = frozenset({"device", "tool", "motion", "site", "material"})


class ActionResourceContractError(ValueError):
    """动作资源合同无法由 AST 安全编译。

    参数：``code`` 是稳定诊断码，``path`` 是合同内 JSON Pointer，``message``
    是中文诊断。返回：异常对象。异常：构造过程不抛出其他异常。
    """

    def __init__(self, code: str, path: str, message: str) -> None:
        """保存稳定诊断字段。

        参数：``code``、``path``、``message`` 分别表示机器码、字段路径和中文原因。
        返回：无。异常：不主动抛出其他异常。
        """

        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


def normalize_action_resource_contract(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把装饰器静态字面量编译成版本化动作资源合同。

    参数：``value`` 是 ``@action(resource_contract=...)`` 经 AST 提取的 JSON
    对象；只允许参数名和资源角色，不允许运行时取得/释放代码。返回：字段顺序稳定、
    可直接嵌入动作 Schema 的第 1/2 版合同；省略时返回空字典。异常：字段未知、版本、
    参数名、设备托管或转运角色非法时抛 ``ActionResourceContractError``。
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        _fail("invalid_action_resource_contract", "/", "动作资源合同必须是对象")
    allowed = {
        "version",
        "required_device_params",
        "resource_params",
        "device_tenancy",
        "transfer",
        "operate_in_place",
        "transfer_step",
        "order_sensitive",
        "aliquot",
    }
    unknown = set(value) - allowed
    if unknown:
        _fail(
            "unknown_action_resource_field",
            "/",
            "动作资源合同包含未知字段：" + ",".join(sorted(unknown)),
        )
    version = value.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version not in {1, 2}:
        _fail(
            "unsupported_action_resource_version",
            "/version",
            "动作资源合同版本必须是 1 或 2",
        )
    if version == 1 and "resource_params" in value:
        _fail(
            "unsupported_action_resource_field",
            "/resource_params",
            "resource_params 需要动作资源合同版本 2",
        )
    raw_transfer = value.get("transfer")
    if version == 1 and isinstance(raw_transfer, Mapping):
        unsupported_transfer_fields = set(raw_transfer) & set(TRANSFER_RESOURCE_ROLE_FIELDS)
        if unsupported_transfer_fields:
            _fail(
                "unsupported_action_resource_field",
                "/transfer",
                "motion/tool 资源角色需要动作资源合同版本 2",
            )
    normalized: dict[str, Any] = {"version": version}
    legacy_device_params: tuple[str, ...] = ()
    if "required_device_params" in value:
        legacy_device_params = _parameter_names(
            value["required_device_params"],
            "/required_device_params",
        )
        normalized["required_device_params"] = list(legacy_device_params)
    if "resource_params" in value:
        normalized["resource_params"] = _resource_params(
            value["resource_params"],
            "/resource_params",
            legacy_device_params=legacy_device_params,
        )
    elif version == 2 and legacy_device_params:
        normalized["resource_params"] = [
            {"param": name, "role": "device"} for name in legacy_device_params
        ]
    if value.get("device_tenancy") is not None:
        normalized["device_tenancy"] = _device_tenancy(value["device_tenancy"])
    if value.get("transfer") is not None:
        normalized["transfer"] = _transfer(value["transfer"])
    if value.get("operate_in_place") is not None:
        normalized["operate_in_place"] = _operate_in_place(value["operate_in_place"])
    if value.get("aliquot") is not None:
        normalized["aliquot"] = _aliquot(value["aliquot"])
    if "order_sensitive" in value:
        if not isinstance(value["order_sensitive"], bool):
            _fail("invalid_order_sensitive", "/order_sensitive", "order_sensitive 必须是布尔值")
        normalized["order_sensitive"] = value["order_sensitive"]
    if "transfer_step" in value:
        step = value["transfer_step"]
        fields = {"operation", "material_param", "owner_param", "site_param", "carrier_params"}
        if (
            version != 2
            or not isinstance(step, Mapping)
            or set(step) != fields
            or step.get("operation") not in {"pick", "place"}
        ):
            _fail(
                "invalid_transfer_step",
                "/transfer_step",
                "transfer_step 需要 v2、pick/place 和完整参数映射",
            )
        normalized["transfer_step"] = {
            "operation": step["operation"],
            **{
                key: _parameter_name(step[key], f"/transfer_step/{key}")
                for key in ("material_param", "owner_param", "site_param")
            },
            "carrier_params": list(
                _parameter_names(step["carrier_params"], "/transfer_step/carrier_params")
            ),
        }
        if not normalized["transfer_step"]["carrier_params"]:
            _fail("invalid_transfer_step", "/transfer_step/carrier_params", "搬运器参数不能为空")
    if len(normalized) == 1:
        _fail(
            "empty_action_resource_contract",
            "/",
            "动作资源合同除版本外至少声明一种资源语义",
        )
    return normalized


def validate_action_resource_contract_schema(
    contract: Mapping[str, Any],
    action_schema: Mapping[str, Any],
) -> None:
    """证明资源合同引用的参数存在且具有匹配的静态值类型。

    参数：``contract`` 是已规范化资源合同；``action_schema`` 是同一动作由 AST
    编译的完整第 2 版 Schema。返回：无。异常：设备/物料参数不是 ResourceSlot、
    库位参数不是字符串或任一参数不存在时抛 ``ActionResourceContractError``，
    防止运行时按名称猜测资源身份。
    """

    properties = action_schema.get("properties")
    goal = properties.get("goal") if isinstance(properties, Mapping) else None
    goal_properties = goal.get("properties") if isinstance(goal, Mapping) else None
    if not isinstance(goal_properties, Mapping):
        _fail(
            "invalid_action_schema",
            "/",
            "动作资源合同缺少可验证的 Goal Schema",
        )
    resource_fields: list[tuple[str, str]] = []
    resource_params = contract.get("resource_params", [])
    if not isinstance(resource_params, Sequence) or isinstance(resource_params, (str, bytes)):
        _fail("invalid_resource_params", "/resource_params", "resource_params 必须是数组")
    for index, item in enumerate(resource_params):
        if not isinstance(item, Mapping):
            _fail(
                "invalid_resource_param",
                f"/resource_params/{index}",
                "资源参数项必须是对象",
            )
        name = item.get("param")
        role = item.get("role")
        if not isinstance(name, str) or not name:
            _fail(
                "invalid_resource_param",
                f"/resource_params/{index}/param",
                "资源参数名无效",
            )
        if role not in RESOURCE_PARAM_ROLES:
            _fail(
                "invalid_resource_role",
                f"/resource_params/{index}/role",
                "资源参数角色必须是 device、tool、motion、site 或 material",
            )
        resource_fields.append((name, f"/resource_params/{index}/param"))
    for index, name in enumerate(contract.get("required_device_params", [])):
        resource_fields.append((str(name), f"/required_device_params/{index}"))
    tenancy = contract.get("device_tenancy")
    if isinstance(tenancy, Mapping):
        resource_fields.append((str(tenancy["material_param"]), "/device_tenancy/material_param"))
        for field in ("acquire_device_param", "release_device_param"):
            if tenancy.get(field):
                resource_fields.append((str(tenancy[field]), f"/device_tenancy/{field}"))
    step = contract.get("transfer_step")
    if isinstance(step, Mapping):
        for field in ("material_param", "owner_param"):
            resource_fields.append((str(step[field]), f"/transfer_step/{field}"))
        for name in step["carrier_params"]:
            resource_fields.append((str(name), "/transfer_step/carrier_params"))
        site_schema = goal_properties.get(step["site_param"])
        if not isinstance(site_schema, Mapping) or site_schema.get("type") != "string":
            _fail(
                "invalid_site_parameter_type",
                "/transfer_step/site_param",
                "搬运端点 Site 参数必须是字符串",
            )
    transfer = contract.get("transfer")
    if isinstance(transfer, Mapping):
        resource_fields.extend(
            (
                (str(transfer["material_param"]), "/transfer/material_param"),
                *(
                    (
                        (
                            str(transfer["source_owner_param"]),
                            "/transfer/source_owner_param",
                        ),
                    )
                    if transfer.get("source_owner_param")
                    else ()
                ),
                (
                    str(transfer["target_owner_param"]),
                    "/transfer/target_owner_param",
                ),
            )
        )
        for field in (
            "source_site_uuid_param",
            "source_site_name_param",
            "target_site_uuid_param",
            "target_site_name_param",
        ):
            name = str(transfer.get(field) or "")
            if not name:
                continue
            schema = goal_properties.get(name)
            if not isinstance(schema, Mapping):
                _fail(
                    "unknown_action_resource_parameter",
                    f"/transfer/{field}",
                    f"动作资源合同引用不存在的参数 {name}",
                )
            field_type = schema.get("type")
            allowed_types = set(field_type) if isinstance(field_type, list) else {field_type}
            if "string" not in allowed_types:
                _fail(
                    "invalid_site_parameter_type",
                    f"/transfer/{field}",
                    f"库位参数 {name} 必须是字符串",
                )
    operate_in_place = contract.get("operate_in_place")
    if isinstance(operate_in_place, Mapping):
        resource_fields.append(
            (
                str(operate_in_place["material_param"]),
                "/operate_in_place/material_param",
            )
        )
    aliquot = contract.get("aliquot")
    if isinstance(aliquot, Mapping):
        resource_fields.append(
            (str(aliquot["source_material_param"]), "/aliquot/source_material_param")
        )
        for index, name in enumerate(aliquot["target_material_params"]):
            resource_fields.append((str(name), f"/aliquot/target_material_params/{index}"))
    for name, path in resource_fields:
        schema = goal_properties.get(name)
        if not isinstance(schema, Mapping):
            _fail(
                "unknown_action_resource_parameter",
                path,
                f"动作资源合同引用不存在的参数 {name}",
            )
        if not _is_resource_reference_schema(schema):
            _fail(
                "invalid_resource_parameter_type",
                path,
                f"资源参数 {name} 必须是 ResourceSlot",
            )


def _is_resource_reference_schema(value: Mapping[str, Any]) -> bool:
    """判断动作字段是否是规范 ResourceSlot 引用 Schema。

    参数：``value`` 是 Goal 字段 Schema。返回：字段要求 ``uuid`` 字符串时为真，
    否则为假。异常：不主动抛出异常。
    """

    properties = value.get("properties")
    uuid_schema = properties.get("uuid") if isinstance(properties, Mapping) else None
    required = value.get("required")
    return (
        value.get("type") == "object"
        and isinstance(uuid_schema, Mapping)
        and uuid_schema.get("type") == "string"
        and isinstance(required, list)
        and "uuid" in required
    )


def _device_tenancy(value: Any) -> dict[str, Any]:
    """规范装载期间设备托管声明。

    参数：``value`` 是 ``device_tenancy`` 字面量。返回：稳定参数名合同。异常：
    模式、字段或取得/释放关系非法时抛 ``ActionResourceContractError``。
    """

    if not isinstance(value, Mapping):
        _fail(
            "invalid_device_tenancy",
            "/device_tenancy",
            "device_tenancy 必须是对象",
        )
    allowed = {
        "mode",
        "material_param",
        "acquire_device_param",
        "release_device_param",
    }
    if set(value) - allowed:
        _fail(
            "unknown_device_tenancy_field",
            "/device_tenancy",
            "device_tenancy 包含未知字段",
        )
    if value.get("mode") != "task_while_loaded":
        _fail(
            "invalid_device_tenancy_mode",
            "/device_tenancy/mode",
            "设备托管模式必须是 task_while_loaded",
        )
    material_param = _parameter_name(
        value.get("material_param"),
        "/device_tenancy/material_param",
    )
    acquire = _optional_parameter_name(
        value.get("acquire_device_param"),
        "/device_tenancy/acquire_device_param",
    )
    release = _optional_parameter_name(
        value.get("release_device_param"),
        "/device_tenancy/release_device_param",
    )
    if not acquire and not release:
        _fail(
            "empty_device_tenancy_transition",
            "/device_tenancy",
            "设备托管至少声明取得或释放设备",
        )
    if acquire and acquire == release:
        _fail(
            "invalid_device_tenancy_transition",
            "/device_tenancy",
            "同一动作不能取得并释放同一设备参数",
        )
    return {
        "mode": "task_while_loaded",
        "material_param": material_param,
        "acquire_device_param": acquire,
        "release_device_param": release,
    }


def _transfer(value: Any) -> dict[str, Any]:
    """规范机械臂转运完整资源集参数映射。

    参数：``value`` 是 ``transfer`` 字面量。返回：待搬物料、目标父物料、目标
    库位 UUID/名称和机械臂夹爪库位角色的稳定映射。异常：字段缺失、未知或参数名
    非法时抛 ``ActionResourceContractError``。
    """

    if not isinstance(value, Mapping):
        _fail("invalid_transfer_contract", "/transfer", "transfer 必须是对象")
    allowed = set(TRANSFER_CONTRACT_FIELDS) | set(TRANSFER_RESOURCE_ROLE_FIELDS)
    if set(value) - allowed:
        _fail(
            "unknown_transfer_field",
            "/transfer",
            "transfer 包含未知字段",
        )
    material_param = _parameter_name(
        value.get("material_param"),
        "/transfer/material_param",
    )
    source_owner_param = _optional_parameter_name(
        value.get("source_owner_param"),
        "/transfer/source_owner_param",
    )
    source_site_uuid_param = _optional_parameter_name(
        value.get("source_site_uuid_param"),
        "/transfer/source_site_uuid_param",
    )
    source_site_name_param = _optional_parameter_name(
        value.get("source_site_name_param"),
        "/transfer/source_site_name_param",
    )
    if source_owner_param and not (source_site_uuid_param or source_site_name_param):
        _fail(
            "transfer_source_site_parameter_missing",
            "/transfer",
            "声明来源父资源时必须同时声明来源库位 UUID 或名称参数",
        )
    if (source_site_uuid_param or source_site_name_param) and not source_owner_param:
        _fail(
            "transfer_source_owner_parameter_missing",
            "/transfer",
            "声明来源库位时必须同时声明来源父资源参数",
        )
    target_owner_param = _parameter_name(
        value.get("target_owner_param"),
        "/transfer/target_owner_param",
    )
    site_uuid_param = _optional_parameter_name(
        value.get("target_site_uuid_param"),
        "/transfer/target_site_uuid_param",
    )
    site_name_param = _optional_parameter_name(
        value.get("target_site_name_param"),
        "/transfer/target_site_name_param",
    )
    if not site_uuid_param and not site_name_param:
        _fail(
            "transfer_site_parameter_missing",
            "/transfer",
            "transfer 至少声明目标库位 UUID 或名称参数",
        )
    gripper_role = _parameter_name(
        value.get("gripper_site_role"),
        "/transfer/gripper_site_role",
    )
    normalized_transfer = {
        "material_param": material_param,
        "source_owner_param": source_owner_param,
        "source_site_uuid_param": source_site_uuid_param,
        "source_site_name_param": source_site_name_param,
        "target_owner_param": target_owner_param,
        "target_site_uuid_param": site_uuid_param,
        "target_site_name_param": site_name_param,
        "gripper_site_role": gripper_role,
    }
    for field in ("motion_resource_roles", "tool_resource_roles"):
        if field in value:
            normalized_transfer[field] = list(
                _resource_roles(value[field], f"/transfer/{field}")
            )
    return normalized_transfer


def _operate_in_place(value: Any) -> dict[str, str]:
    """规范“物料必须仍在实际执行设备内”的动作资源语义。"""

    if not isinstance(value, Mapping):
        _fail(
            "invalid_operate_in_place_contract",
            "/operate_in_place",
            "operate_in_place 必须是对象",
        )
    if set(value) != {"material_param"}:
        _fail(
            "invalid_operate_in_place_contract",
            "/operate_in_place",
            "operate_in_place 只能且必须声明 material_param",
        )
    return {
        "material_param": _parameter_name(
            value.get("material_param"),
            "/operate_in_place/material_param",
        )
    }


def _aliquot(value: Any) -> dict[str, Any]:
    """规范一次来源容器向全部声明目标容器分装的资源合同。"""

    if not isinstance(value, Mapping) or set(value) != {
        "source_material_param",
        "target_material_params",
    }:
        _fail(
            "invalid_aliquot_contract",
            "/aliquot",
            "aliquot 必须且只能声明 source_material_param 与 target_material_params",
        )
    source = _parameter_name(
        value.get("source_material_param"), "/aliquot/source_material_param"
    )
    targets = _parameter_names(
        value.get("target_material_params"), "/aliquot/target_material_params"
    )
    if not targets or source in targets:
        _fail(
            "invalid_aliquot_contract",
            "/aliquot/target_material_params",
            "aliquot 至少需要一个与来源不同的目标参数",
        )
    return {
        "source_material_param": source,
        "target_material_params": list(targets),
    }


def _parameter_names(value: Any, path: str) -> tuple[str, ...]:
    """校验无重复参数名数组。

    参数：``value`` 是可疑数组，``path`` 是诊断路径。返回：稳定参数名元组。
    异常：值非数组或包含重复项时抛 ``ActionResourceContractError``。
    """

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _fail("invalid_parameter_names", path, "设备参数声明必须是数组")
    names = tuple(_parameter_name(item, path) for item in value)
    if len(set(names)) != len(names):
        _fail("duplicate_parameter_name", path, "设备参数声明包含重复项")
    return names


def _resource_params(
    value: Any,
    path: str,
    *,
    legacy_device_params: Sequence[str] = (),
) -> list[dict[str, str]]:
    """规范 v2 资源参数及其角色，并拒绝重复参数。"""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _fail("invalid_resource_params", path, "resource_params 必须是数组")
    result: list[dict[str, str]] = [
        {"param": name, "role": "device"} for name in legacy_device_params
    ]
    seen = {item["param"] for item in result}
    for index, item in enumerate(value):
        item_path = f"{path}/{index}"
        if not isinstance(item, Mapping) or set(item) != {"param", "role"}:
            _fail(
                "invalid_resource_param",
                item_path,
                "资源参数项必须且只能包含 param 与 role",
            )
        name = _parameter_name(item.get("param"), f"{item_path}/param")
        role = item.get("role")
        if role not in RESOURCE_PARAM_ROLES:
            _fail(
                "invalid_resource_role",
                f"{item_path}/role",
                "资源参数角色必须是 device、tool、motion、site 或 material",
            )
        if name in seen:
            _fail(
                "duplicate_resource_parameter",
                f"{item_path}/param",
                f"资源参数重复：{name}",
            )
        seen.add(name)
        result.append({"param": name, "role": str(role)})
    if not result:
        _fail("empty_resource_params", path, "resource_params 至少声明一项资源参数")
    return result


def _resource_roles(value: Any, path: str) -> tuple[str, ...]:
    """规范 transfer 的 motion/tool 资源角色列表。"""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _fail("invalid_resource_roles", path, "资源角色必须是字符串数组")
    roles: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item or item != item.strip():
            _fail("invalid_resource_role", f"{path}/{index}", "资源角色必须是非空字符串")
        if item in roles:
            _fail("duplicate_resource_role", path, f"资源角色重复：{item}")
        roles.append(item)
    if not roles:
        _fail("empty_resource_roles", path, "资源角色数组不能为空")
    return tuple(roles)


def _parameter_name(value: Any, path: str) -> str:
    """校验一个声明参数名或资源角色名。

    参数：``value`` 是可疑值，``path`` 是诊断路径。返回：原始非空字符串。
    异常：值不是无首尾空白的字符串时抛 ``ActionResourceContractError``。
    """

    if not isinstance(value, str) or not value or value != value.strip():
        _fail("invalid_parameter_name", path, "参数名必须是无首尾空白的非空字符串")
    return value


def _optional_parameter_name(value: Any, path: str) -> str:
    """校验一个可省略的声明参数名。

    参数：``value`` 是可疑值、``None`` 或已规范化的空字符串，``path`` 是诊断
    路径。返回：省略时为空字符串，否则返回规范名称。异常：非空值非法时抛
    ``ActionResourceContractError``；重复规范化保持幂等。
    """

    if value is None or value == "":
        return ""
    return _parameter_name(value, path)


def _fail(code: str, path: str, message: str) -> None:
    """抛出稳定动作资源合同诊断。

    参数：``code``、``path``、``message`` 是异常字段。返回：永不返回。异常：始终
    抛出 ``ActionResourceContractError``。
    """

    raise ActionResourceContractError(code, path, message)


__all__ = [
    "ActionResourceContractError",
    "normalize_action_resource_contract",
    "validate_action_resource_contract_schema",
]
