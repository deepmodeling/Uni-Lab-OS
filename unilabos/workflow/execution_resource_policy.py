"""声明式作业资源策略的冻结、运行解析与静态死锁检查。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from unilabos.registry.action_resource_contract import (
    ActionResourceContractError,
    normalize_action_resource_contract,
)
from unilabos.workflow.resource_lock_key import device_lock_key


class ExecutionResourcePolicyError(ValueError):
    """执行资源策略无法安全冻结或解析。"""


@dataclass(frozen=True, slots=True)
class ResolvedExecutionResourcePolicy:
    """一个作业已解析的附加设备、库位组与托管转换。"""

    device_lock_keys: tuple[str, ...]
    target_site_uuids: tuple[str, ...]
    device_tenancy: dict[str, str] | None


def action_device_resource_params(contract: Mapping[str, Any]) -> tuple[str, ...]:
    """把明确声明为参数的设备角色统一为运行时设备需求。"""
    names = list(contract.get("required_device_params", ()))
    names.extend(
        str(item["param"])
        for item in contract.get("resource_params", ())
        if item.get("role") in {"device", "motion", "tool"}
    )
    # 转运的 motion/tool 列表可以是站点别名；只有 resource_params 或旧字段
    # 明确声明为动作参数时，才从最终 Goal 中读取身份。
    return tuple(dict.fromkeys(names))


def merge_action_resource_policy(
    resource_contract: Mapping[str, Any] | None,
    workflow_policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把动作资源权威与工作流实例选择合并为冻结执行策略。

    参数：``resource_contract`` 来自动作模板 AST 合同，拥有设备参数和托管语义；
    ``workflow_policy`` 只允许补充执行超时与部署时显式等价库位组。返回：可由
    ``resolve_execution_resource_policy`` 直接解析的冻结策略。异常：动作合同非法、
    工作流试图覆盖动作资源语义或任一策略字段非法时抛
    ``ExecutionResourcePolicyError``。
    """

    try:
        normalized_contract = normalize_action_resource_contract(
            resource_contract if resource_contract else None
        )
    except ActionResourceContractError as error:
        raise ExecutionResourcePolicyError(error.message) from error
    raw_workflow_policy = workflow_policy or {}
    if not isinstance(raw_workflow_policy, Mapping):
        raise ExecutionResourcePolicyError("execution_policy 必须是对象")
    if normalized_contract and {
        "required_device_params",
        "device_tenancy",
    } & set(raw_workflow_policy):
        raise ExecutionResourcePolicyError("工作流不得覆盖 AST 动作资源合同中的设备或托管语义")
    combined = dict(raw_workflow_policy)
    device_params = action_device_resource_params(normalized_contract)
    if device_params or normalized_contract.get("required_device_params") is not None:
        combined["required_device_params"] = list(device_params)
    if normalized_contract.get("device_tenancy") is not None:
        combined["device_tenancy"] = dict(normalized_contract["device_tenancy"])
    return normalize_execution_resource_policy(combined)


def normalize_execution_resource_policy(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把工作流节点执行策略规范为可冻结的声明式资源合同。"""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ExecutionResourcePolicyError("execution_policy 必须是对象")
    if "access_region" in value:
        raise ExecutionResourcePolicyError(
            "access_region 由 PLC 保证，工作流不得声明软件锁"
        )
    resource_fields = {
        "execution_timeout_seconds",
        "required_device_params",
        "target_site_group",
        "device_tenancy",
    }
    # ``execution_policy`` 早于资源内核，仍承载优先级、队列等其他调度/创作字段。
    # 本模块只拥有四个资源字段，其余 JSON 字段原样保留，避免资源能力吞并公共
    # 执行策略命名空间；PLC 访问区域由上层显式拒绝。
    result: dict[str, Any] = {
        key: deepcopy(field_value)
        for key, field_value in value.items()
        if key not in resource_fields
    }
    if "execution_timeout_seconds" in value:
        timeout = value["execution_timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 0:
            raise ExecutionResourcePolicyError(
                "execution_timeout_seconds 必须是非负整数"
            )
        result["execution_timeout_seconds"] = timeout
    if "required_device_params" in value:
        result["required_device_params"] = list(
            _parameter_names(value["required_device_params"], "required_device_params")
        )
    if "target_site_group" in value:
        result["target_site_group"] = list(
            _uuid_sequence(value["target_site_group"], "target_site_group")
        )
    tenancy = value.get("device_tenancy")
    if tenancy is not None:
        if not isinstance(tenancy, Mapping):
            raise ExecutionResourcePolicyError("device_tenancy 必须是对象")
        allowed_tenancy = {
            "mode",
            "material_param",
            "acquire_device_param",
            "release_device_param",
        }
        if set(tenancy) - allowed_tenancy:
            raise ExecutionResourcePolicyError("device_tenancy 包含未知字段")
        if tenancy.get("mode") != "task_while_loaded":
            raise ExecutionResourcePolicyError("device_tenancy.mode 非法")
        material_param = _parameter_name(
            tenancy.get("material_param"), "device_tenancy.material_param"
        )
        acquire_param = _optional_parameter_name(
            tenancy.get("acquire_device_param"),
            "device_tenancy.acquire_device_param",
        )
        release_param = _optional_parameter_name(
            tenancy.get("release_device_param"),
            "device_tenancy.release_device_param",
        )
        if not acquire_param and not release_param:
            raise ExecutionResourcePolicyError("device_tenancy 至少声明取得或释放设备")
        if acquire_param and acquire_param == release_param:
            raise ExecutionResourcePolicyError("device_tenancy 不能取得并释放同一参数")
        result["device_tenancy"] = {
            "mode": "task_while_loaded",
            "material_param": material_param,
            "acquire_device_param": acquire_param,
            "release_device_param": release_param,
        }
    return result


def resolve_execution_resource_policy(
    policy: Mapping[str, Any] | None,
    params: Mapping[str, Any],
) -> ResolvedExecutionResourcePolicy:
    """用最终动作参数解析附加设备锁、等价库位组和设备托管转换。"""

    normalized = normalize_execution_resource_policy(policy)
    device_keys = {
        _device_lock_key(_resource_uuid(params, name))
        for name in normalized.get("required_device_params", [])
    }
    tenancy_contract = normalized.get("device_tenancy")
    tenancy = None
    if isinstance(tenancy_contract, Mapping):
        material_uuid = _resource_uuid(
            params,
            str(tenancy_contract["material_param"]),
        )
        acquire_name = str(tenancy_contract.get("acquire_device_param") or "")
        release_name = str(tenancy_contract.get("release_device_param") or "")
        acquire_key = (
            _device_lock_key(_resource_uuid(params, acquire_name))
            if acquire_name
            else ""
        )
        release_key = (
            _device_lock_key(_resource_uuid(params, release_name))
            if release_name
            else ""
        )
        device_keys.update(key for key in (acquire_key, release_key) if key)
        tenancy = {
            "mode": "task_while_loaded",
            "material_uuid": material_uuid,
            "acquire_device_lock_key": acquire_key,
            "release_device_lock_key": release_key,
        }
    return ResolvedExecutionResourcePolicy(
        device_lock_keys=tuple(sorted(device_keys)),
        target_site_uuids=tuple(normalized.get("target_site_group", [])),
        device_tenancy=tenancy,
    )


def validate_static_device_tenancy_order(
    planned_nodes: Sequence[Mapping[str, Any]],
) -> None:
    """拒绝违反稳定取得顺序或在任务结束时仍未释放的设备托管图。

    参数：``planned_nodes`` 是按拓扑顺序冻结的计划工作流节点。返回：无。
    异常：跨设备转运逆序取得设备，或主物料在完整计划结束时仍托管设备时抛
    ``ExecutionResourcePolicyError``。并行分支中的长期托管必须由工作流作者
    显式串行化；本检查不会把运行时争用猜测成一个安全顺序。
    """

    active_by_material: dict[str, set[str]] = {}
    for node in planned_nodes:
        policy = normalize_execution_resource_policy(
            node.get("execution_policy")
            if isinstance(node.get("execution_policy"), Mapping)
            else {}
        )
        tenancy = policy.get("device_tenancy")
        if not isinstance(tenancy, Mapping):
            continue
        acquire_name = str(tenancy.get("acquire_device_param") or "")
        release_name = str(tenancy.get("release_device_param") or "")
        params = node.get("param")
        if not isinstance(params, Mapping):
            raise ExecutionResourcePolicyError("设备托管节点参数必须是对象")
        material_name = str(tenancy["material_param"])
        material_identity = _optional_resource_uuid(params, material_name) or (
            f"param:{material_name}"
        )
        active_devices = active_by_material.setdefault(material_identity, set())
        release_uuid = _resource_uuid(params, release_name) if release_name else ""
        acquire_uuid = _resource_uuid(params, acquire_name) if acquire_name else ""
        if release_uuid and release_uuid not in active_devices:
            raise ExecutionResourcePolicyError(
                "设备托管释放顺序非法：主物料并未托管声明的来源设备"
            )
        # ``blocking_predecessors`` 是取得新设备时仍由同一主物料托管的全部设备。
        # 无论释放是否写在同一个节点，都必须先建立这条取得关系；否则把卸载拆到
        # 后续节点就能绕过全站稳定顺序，两个任务仍可能形成 A→B/B→A 循环等待。
        blocking_predecessors = active_devices - {acquire_uuid}
        if acquire_uuid and any(
            predecessor >= acquire_uuid for predecessor in blocking_predecessors
        ):
            raise ExecutionResourcePolicyError(
                "跨设备托管违反稳定资源取得顺序，工作流存在静态死锁风险"
            )
        if release_uuid:
            active_devices.remove(release_uuid)
        if acquire_uuid:
            active_devices.add(acquire_uuid)
    leaked = {
        material: tuple(sorted(devices))
        for material, devices in active_by_material.items()
        if devices
    }
    if leaked:
        raise ExecutionResourcePolicyError(
            "设备托管未在工作流结束前释放："
            + ";".join(
                f"{material}=>{','.join(devices)}"
                for material, devices in sorted(leaked.items())
            )
        )


def _parameter_names(value: Any, field: str) -> tuple[str, ...]:
    """校验无重复的动作参数名列表。"""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ExecutionResourcePolicyError(f"{field} 必须是参数名数组")
    names = tuple(_parameter_name(item, field) for item in value)
    if len(set(names)) != len(names):
        raise ExecutionResourcePolicyError(f"{field} 包含重复参数")
    return names


def _parameter_name(value: Any, field: str) -> str:
    """校验一个无首尾空白的动作参数名。"""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ExecutionResourcePolicyError(f"{field} 必须是非空参数名")
    return value


def _optional_parameter_name(value: Any, field: str) -> str:
    """校验可省略的动作参数名。"""

    if value is None or value == "":
        return ""
    return _parameter_name(value, field)


def _uuid_sequence(value: Any, field: str) -> tuple[str, ...]:
    """校验显式等价组中的稳定库位 UUID。"""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ExecutionResourcePolicyError(f"{field} 必须是非空 UUID 数组")
    uuids = tuple(str(UUID(str(item))) for item in value)
    if len(set(uuids)) != len(uuids):
        raise ExecutionResourcePolicyError(f"{field} 包含重复 UUID")
    return uuids


def _resource_uuid(params: Mapping[str, Any], name: str) -> str:
    """从最终参数的 ResourceSlot 引用中取得规范 UUID。"""

    value = params.get(name)
    if not isinstance(value, Mapping):
        raise ExecutionResourcePolicyError(f"参数 {name} 不是物料引用")
    try:
        return str(UUID(str(value.get("uuid") or "")))
    except ValueError as error:
        raise ExecutionResourcePolicyError(f"参数 {name} 缺少合法 UUID") from error


def _optional_resource_uuid(params: Mapping[str, Any], name: str) -> str:
    """在静态计划尚未绑定物料时返回空值，否则校验并返回稳定 UUID。

    参数：``params`` 是节点冻结参数，``name`` 是物料参数名。返回：参数未绑定时
    返回空字符串，已绑定时返回规范 UUID。异常：存在引用结构但 UUID 非法时抛
    ``ExecutionResourcePolicyError``，避免把损坏绑定当成动态绑定。
    """

    value = params.get(name)
    if value is None:
        return ""
    return _resource_uuid(params, name)


def _device_lock_key(material_uuid: str) -> str:
    """把设备物料身份转换为规范设备执行占用键。"""

    return device_lock_key(material_uuid)


__all__ = [
    "ExecutionResourcePolicyError",
    "ResolvedExecutionResourcePolicy",
    "merge_action_resource_policy",
    "normalize_execution_resource_policy",
    "resolve_execution_resource_policy",
    "validate_static_device_tenancy_order",
]
