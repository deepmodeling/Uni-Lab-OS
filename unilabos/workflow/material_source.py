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
    """声明物料来源在任务并行执行时的占用策略。"""

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


__all__ = [
    "MATERIAL_FLOW_ROLE_LABELS_ZH",
    "MaterialCustodyPolicy",
    "MaterialFlowRole",
]
