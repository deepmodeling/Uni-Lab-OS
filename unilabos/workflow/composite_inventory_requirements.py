"""组合调用展开时携带子工作流的数量型库存需求。

子工作流（实验操作）里声明的 ``quantity_requirement`` 属于子图中的消费动作和物料来源；
父工作流展开调用后，这些节点以新的 UUID 出现在父图里。本模块把子需求改写到展开后的
节点身份，并把“用量绑定到子工作流输入”改写为父级实际传入的值：

- 字面量实参：绑定为字面量；
- 父工作流输入实参：绑定为父工作流输入，由任务创建时按任务输入求值；
- 运行时值（上游节点输出）或资源引用：不支持，关闭式失败，绝不静默丢弃。

需求身份与键按调用命名空间派生，同一子工作流被多次调用、多层嵌套时互不冲突。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid5


class CompositeInventoryRequirementError(ValueError):
    """子工作流数量需求无法安全地带入父工作流。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


def _unilab(requirement: Mapping[str, Any]) -> dict[str, Any]:
    meta = requirement.get("meta_data")
    unilab = meta.get("unilab") if isinstance(meta, Mapping) else None
    return dict(unilab) if isinstance(unilab, Mapping) else {}


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def remap_requirement_nodes(
    requirement: Mapping[str, Any], node_uuid_map: Mapping[str, str], *, path: str
) -> dict[str, Any]:
    """把消费节点与物料来源节点改写为展开后的身份；任一缺失即失败。"""

    copied = deepcopy(dict(requirement))
    consume = str(copied.get("consume_node_uuid") or "")
    if consume not in node_uuid_map:
        raise CompositeInventoryRequirementError(
            "composite_quantity_requirement_unmapped", f"{path}/consume_node_uuid",
            "子工作流数量需求的消费动作不在展开结果中",
        )
    copied["consume_node_uuid"] = node_uuid_map[consume]
    unilab = _unilab(copied)
    source = str(unilab.get("material_source_node_uuid") or "")
    if source:
        if source not in node_uuid_map:
            raise CompositeInventoryRequirementError(
                "composite_quantity_requirement_unmapped", f"{path}/material_source_node_uuid",
                "子工作流数量需求的物料来源不在展开结果中",
            )
        unilab["material_source_node_uuid"] = node_uuid_map[source]
    copied["meta_data"] = {**dict(copied.get("meta_data") or {}), "unilab": unilab}
    return copied


def bind_requirement_to_invocation(
    requirement: Mapping[str, Any],
    *,
    invocation_uuid: str,
    child_workflow_uuid: str,
    arguments: Mapping[str, Any],
    path: str,
) -> dict[str, Any]:
    """按本次调用实参重绑用量，并派生调用命名空间内的身份与键。

    参数：``requirement`` 的节点身份已是展开后身份，用量绑定仍以子工作流输入表达；
    ``arguments`` 是规范化后的调用实参（已补子工作流默认值）。返回：父图可直接保存的
    需求。异常：用量绑定到运行时值、资源引用或非数值时抛出
    ``CompositeInventoryRequirementError``。
    """

    copied = deepcopy(dict(requirement))
    unilab = _unilab(copied)
    binding = unilab.get("quantity_binding")
    scale = unilab.get("quantity_scale", 1.0)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not scale > 0:
        raise CompositeInventoryRequirementError(
            "composite_quantity_binding_unsupported", f"{path}/quantity_scale", "数量需求倍率无效",
        )
    if isinstance(binding, Mapping) and binding.get("kind") == "workflow_input":
        parameter = str(binding.get("parameter") or "")
        if parameter not in arguments:
            raise CompositeInventoryRequirementError(
                "composite_quantity_binding_unsupported", f"{path}/quantity_binding",
                f"组合调用没有为数量参数 {parameter} 提供实参",
            )
        value = arguments[parameter]
        if isinstance(value, Mapping) and value.get("kind") == "workflow_input":
            unilab["quantity_binding"] = {"kind": "workflow_input", "parameter": str(value["parameter"])}
        elif isinstance(value, Mapping):
            raise CompositeInventoryRequirementError(
                "composite_quantity_binding_unsupported", f"{path}/quantity_binding",
                f"数量参数 {parameter} 绑定到运行时结果或资源引用；数量需求只接受字面量或工作流输入",
            )
        else:
            number = _positive_number(value)
            if number is None:
                raise CompositeInventoryRequirementError(
                    "composite_quantity_binding_unsupported", f"{path}/quantity_binding",
                    f"数量参数 {parameter} 的实参不是有限非负数",
                )
            unilab["quantity_binding"] = {"kind": "literal", "value": value}
            unilab.pop("quantity_default_inactive", None)
            if number == 0:
                # 用量为 0 的调用本次不需要库存；与任务输入为 0 时跳过需求的语义一致。
                return {}
            copied["required_quantity"] = number * float(scale)
    elif not (isinstance(binding, Mapping) and binding.get("kind") == "literal"):
        raise CompositeInventoryRequirementError(
            "composite_quantity_binding_unsupported", f"{path}/quantity_binding", "数量需求绑定形状无效",
        )
    origin_key = str(copied.get("requirement_key") or "")
    origin_uuid = str(copied.get("uuid") or "")
    namespace = UUID(invocation_uuid)
    copied["uuid"] = str(uuid5(namespace, f"composite-inventory-requirement:{origin_uuid or origin_key}"))
    copied["requirement_key"] = f"{invocation_uuid}/{origin_key}"
    origins = list(unilab.get("composite_origin") or [])
    origins.insert(0, {
        "invocation_uuid": invocation_uuid,
        "child_workflow_uuid": child_workflow_uuid,
        "child_requirement_uuid": origin_uuid,
        "child_requirement_key": origin_key,
    })
    unilab["composite_origin"] = origins
    copied["meta_data"] = {**dict(copied.get("meta_data") or {}), "unilab": unilab}
    return copied


def expand_child_inventory_requirements(
    snapshot_requirements: Sequence[Mapping[str, Any]],
    *,
    nested_requirements: Sequence[Mapping[str, Any]] = (),
    invocation_uuid: str,
    child_workflow_uuid: str,
    node_uuid_map: Mapping[str, str],
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """把子快照的需求（含子图中已物化的孙调用需求）与递归展开得到的孙需求带入父图。

    ``snapshot_requirements`` 的节点身份属于子快照，经 ``node_uuid_map`` 改写；
    ``nested_requirements`` 来自本层递归展开，身份已是最终身份。两者的用量绑定都以
    子工作流输入表达，统一按本次调用实参重绑。
    """

    expanded: list[dict[str, Any]] = []
    for index, requirement in enumerate(snapshot_requirements):
        path = f"/child/inventory_requirements/{index}"
        remapped = remap_requirement_nodes(requirement, node_uuid_map, path=path)
        bound = bind_requirement_to_invocation(
            remapped, invocation_uuid=invocation_uuid, child_workflow_uuid=child_workflow_uuid,
            arguments=arguments, path=path,
        )
        if bound:
            expanded.append(bound)
    for index, requirement in enumerate(nested_requirements):
        path = f"/nested/inventory_requirements/{index}"
        bound = bind_requirement_to_invocation(
            requirement, invocation_uuid=invocation_uuid, child_workflow_uuid=child_workflow_uuid,
            arguments=arguments, path=path,
        )
        if bound:
            expanded.append(bound)
    keys = [item["requirement_key"] for item in expanded]
    if len(keys) != len(set(keys)):
        raise CompositeInventoryRequirementError(
            "composite_quantity_requirement_conflict", "/inventory_requirements", "展开后的数量需求键重复",
        )
    return tuple(expanded)
