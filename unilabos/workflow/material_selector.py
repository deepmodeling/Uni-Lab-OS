"""物料来源选择器（MaterialSourceSelector）的唯一结构校验合同。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any

from unilabos.workflow.material_source import MaterialCustodyPolicy, MaterialFlowRole
from unilabos.workflow.models import validate_uuid

MATERIAL_FLOW_ROLE_VALUES = MappingProxyType(
    {member.name: member.value for member in MaterialFlowRole}
)
MATERIAL_FLOW_ROLE_MEMBERS = MappingProxyType(
    {member.value: member.name for member in MaterialFlowRole}
)
MATERIAL_CUSTODY_POLICY_MEMBERS = MappingProxyType(
    {member.value: member.name for member in MaterialCustodyPolicy}
)
MATERIAL_CUSTODY_POLICY_VALUES = MappingProxyType(
    {member.name: member.value for member in MaterialCustodyPolicy}
)

_REQUIRED_SELECTOR_FIELDS = frozenset(
    {
        "mode",
        "resource_template_uuid",
        "mount",
        "material_uuid",
        "site",
        "slot_range",
        "flow_role",
    }
)
_OPTIONAL_SELECTOR_FIELDS = frozenset({"custody_policy"})


class MaterialSelectorError(ValueError):
    """物料来源选择器（MaterialSourceSelector）违反稳定合同。"""

    code = "invalid_material_source"

    def __init__(self, message: str):
        """保存可面向用户展示的中文诊断。

        参数说明：``message`` 解释具体非法字段或组合。返回：无；构造带稳定
        ``invalid_material_source`` 机器码的异常。
        """

        super().__init__(message)
        self.message = message


def validate_material_source_selector(raw_selector: Any) -> dict[str, Any]:
    """校验并分离复制一个完整物料来源选择器。

    参数说明：``raw_selector`` 是来自作者源码或直接图的可疑值。返回：UUID、
    物料流角色（MaterialFlowRole）和库位（Site）选择均规范的副本；结构、模式、
    角色、库位（Site）/库位（Slot）范围组合非法时抛出
    ``MaterialSelectorError``。
    """

    if not isinstance(raw_selector, Mapping):
        raise MaterialSelectorError("物料来源选择器必须是对象")
    selector = deepcopy(dict(raw_selector))
    selector_fields = set(selector)
    if not (
        selector_fields == _REQUIRED_SELECTOR_FIELDS
        or selector_fields == _REQUIRED_SELECTOR_FIELDS | _OPTIONAL_SELECTOR_FIELDS
    ):
        raise MaterialSelectorError("物料来源选择器字段不完整或包含未知字段")
    if not isinstance(selector["mode"], str) or selector["mode"] not in {
        "existing",
        "create_new",
    }:
        raise MaterialSelectorError("物料来源模式必须是 existing 或 create_new")
    try:
        selector["resource_template_uuid"] = validate_canonical_uuid(
            selector["resource_template_uuid"]
        )
        mount = selector["mount"]
        if not isinstance(mount, Mapping) or set(mount) != {"uuid"}:
            raise ValueError("mount 必须是仅含 uuid 的物料引用")
        selector["mount"] = {"uuid": validate_canonical_uuid(mount["uuid"])}
        selector["material_uuid"] = _optional_uuid(selector["material_uuid"])
        selector["site"] = _optional_uuid(selector["site"])
        selector["slot_range"] = _optional_slot_range(selector["slot_range"])
    except (KeyError, TypeError, ValueError) as error:
        raise MaterialSelectorError("物料来源选择器包含非法 UUID") from error
    if selector["site"] is not None and selector["slot_range"] is not None:
        raise MaterialSelectorError(
            "物料来源不能同时指定库位（Site）和库位（Slot）范围"
        )
    if selector["mode"] == "create_new" and selector["material_uuid"] is not None:
        raise MaterialSelectorError("新建物料来源不能绑定现有物料（Material）")
    if (
        not isinstance(selector["flow_role"], str)
        or selector["flow_role"] not in MATERIAL_FLOW_ROLE_MEMBERS
    ):
        raise MaterialSelectorError("物料流角色（MaterialFlowRole）不在规范闭集")
    if "custody_policy" in selector:
        if (
            not isinstance(selector["custody_policy"], str)
            or selector["custody_policy"] not in MATERIAL_CUSTODY_POLICY_MEMBERS
        ):
            raise MaterialSelectorError("物料占用策略（MaterialCustodyPolicy）不在规范闭集")
    return selector


def validate_material_source_node(node: Mapping[str, Any]) -> dict[str, Any]:
    """校验物料来源节点及其唯一选择器。

    参数说明：``node`` 是后端形状工作流节点（WorkflowNode）。返回：经唯一
    选择器校验器规范化的分离副本；顶层 ``material_uuid`` 非空或选择器非法时
    抛出 ``MaterialSelectorError``。
    """

    if node.get("material_uuid") is not None:
        raise MaterialSelectorError("物料来源节点不能在顶层绑定物料（Material）UUID")
    return validate_material_source_selector(node.get("param"))


def validate_canonical_uuid(value: Any) -> str:
    """要求 UUID 原始拼写已经是规范小写连字符形式。

    参数说明：``value`` 是物料来源选择器（MaterialSourceSelector）中的原始
    身份值。返回：与原值完全相同的规范 UUID；类型、nil UUID、大小写、空白或
    连字符形式不规范时抛出 ``ValueError``，绝不静默改写作者或直接图输入。
    """

    canonical = validate_uuid(value)
    if value != canonical:
        raise ValueError("UUID 必须使用规范小写连字符形式")
    return canonical


def _optional_uuid(value: Any) -> str | None:
    """校验一个可选 UUID 值。

    参数说明：``value`` 是选择器原值。返回：``None`` 或规范 UUID；非法值
    抛出 ``ValueError``，由公共选择器边界转换成稳定诊断。
    """

    return None if value is None else validate_canonical_uuid(value)


def _optional_slot_range(value: Any) -> list[str] | None:
    """校验可选库位（Slot）范围。

    参数说明：``value`` 是 UUID 数组或 ``None``。返回：规范、无重复且稳定
    排序的 UUID 列表；空数组、重复、非法 UUID 或非规范顺序抛出
    ``ValueError``。
    """

    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError("库位（Slot）范围必须是非空数组")
    identities = [validate_canonical_uuid(item) for item in value]
    if len(set(identities)) != len(identities):
        raise ValueError("库位（Slot）范围不能包含重复库位")
    ordered = sorted(identities)
    if identities != ordered:
        raise ValueError("库位（Slot）范围必须按 UUID 稳定排序")
    return ordered


__all__ = [
    "MATERIAL_FLOW_ROLE_MEMBERS",
    "MATERIAL_FLOW_ROLE_VALUES",
    "MATERIAL_CUSTODY_POLICY_MEMBERS",
    "MATERIAL_CUSTODY_POLICY_VALUES",
    "MaterialSelectorError",
    "validate_canonical_uuid",
    "validate_material_source_node",
    "validate_material_source_selector",
]
