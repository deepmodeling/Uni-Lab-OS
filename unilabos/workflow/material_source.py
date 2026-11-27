"""物料来源（MaterialSource）的闭合创作语言合同。

本模块只定义工作流创作阶段的稳定词汇，不读取或修改库存
（Inventory）、物料（Material）或库位（Site）权威事实。
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType


class MaterialFlowRole(str, Enum):
    """声明工作流局部物料流角色（MaterialFlowRole）闭集。"""

    PRIMARY_SAMPLE = "primary_sample"
    ALIQUOT_SAMPLE = "aliquot_sample"
    REAGENT = "reagent"
    CONSUMABLE = "consumable"


class MaterialCustodyPolicy(str, Enum):
    """声明物料来源的保管策略（MaterialCustodyPolicy）闭集。"""

    TASK_EXCLUSIVE = "task_exclusive"
    SHARED_SOURCE = "shared_source"


# 该映射是 wire 值与权威中文显示名的同源目录；调用方不得另行翻译。
MATERIAL_FLOW_ROLE_LABELS_ZH = MappingProxyType(
    {
        MaterialFlowRole.PRIMARY_SAMPLE.value: "主样品",
        MaterialFlowRole.ALIQUOT_SAMPLE.value: "分装样品",
        MaterialFlowRole.REAGENT.value: "试剂",
        MaterialFlowRole.CONSUMABLE.value: "耗材",
    }
)

# 物料保管策略（MaterialCustodyPolicy）显示名与 Backend wire 值共用一个来源。
MATERIAL_CUSTODY_POLICY_LABELS_ZH = MappingProxyType(
    {
        MaterialCustodyPolicy.TASK_EXCLUSIVE.value: "Task 全程独占",
        MaterialCustodyPolicy.SHARED_SOURCE.value: "动作期间共享",
    }
)


__all__ = [
    "MATERIAL_CUSTODY_POLICY_LABELS_ZH",
    "MATERIAL_FLOW_ROLE_LABELS_ZH",
    "MaterialCustodyPolicy",
    "MaterialFlowRole",
]
