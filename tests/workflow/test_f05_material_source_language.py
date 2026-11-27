"""F05.1 物料来源（MaterialSource）创作语言公共合同测试。"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from unilabos.workflow.authoring import (
    MATERIAL_CUSTODY_POLICY_LABELS_ZH,
    MATERIAL_FLOW_ROLE_LABELS_ZH,
    MaterialCustodyPolicy,
    MaterialFlowRole,
    material_source,
    resource_ref,
)


def test_material_flow_role_is_a_closed_immutable_chinese_catalog() -> None:
    """物料流角色（MaterialFlowRole）应同时固化 wire 值和权威中文显示名。

    参数：无。
    返回：无；通过枚举与只读映射断言公共合同。
    异常：修改只读目录必须抛出 ``TypeError``。
    """

    # ``expected_labels`` 是已接受的工作流局部物料流角色闭集，不是全局物料属性。
    expected_labels = {
        "primary_sample": "主样品",
        "aliquot_sample": "分装样品",
        "reagent": "试剂",
        "consumable": "耗材",
    }

    assert isinstance(MATERIAL_FLOW_ROLE_LABELS_ZH, Mapping)
    assert dict(MATERIAL_FLOW_ROLE_LABELS_ZH) == expected_labels
    assert {role.value for role in MaterialFlowRole} == set(expected_labels)
    with pytest.raises(TypeError):
        MATERIAL_FLOW_ROLE_LABELS_ZH["primary_sample"] = "可变标签"  # type: ignore[index]


def test_material_custody_policy_is_a_closed_immutable_chinese_catalog() -> None:
    """物料占用策略必须只包含 Task 独占和动作期间共享两种 wire 值。"""

    expected_labels = {
        "task_exclusive": "Task 全程独占",
        "shared_source": "动作期间共享",
    }
    assert dict(MATERIAL_CUSTODY_POLICY_LABELS_ZH) == expected_labels
    assert {policy.value for policy in MaterialCustodyPolicy} == set(expected_labels)
    with pytest.raises(TypeError):
        MATERIAL_CUSTODY_POLICY_LABELS_ZH["shared_source"] = "可变标签"  # type: ignore[index]


def test_material_source_markers_are_compile_only() -> None:
    """物料来源（MaterialSource）标记不得被误当成运行时物料权威。

    参数：无。
    返回：无；通过公共 Python 标记的失败关闭行为完成断言。
    异常：``resource_ref`` 与 ``material_source`` 被真实执行时均抛出
    ``RuntimeError``，防止绕过可信创作编译器。
    """

    # ``mount_uuid`` 是仅供静态编译器读取的挂载物料稳定身份。
    mount_uuid = "50000000-0000-4000-8000-000000000001"
    with pytest.raises(RuntimeError, match="静态编译"):
        resource_ref(mount_uuid)
    with pytest.raises(RuntimeError, match="静态编译"):
        material_source(
            resource_template=object(),
            mode="existing",
            mount={"uuid": mount_uuid},
            material_uuid=None,
            site=None,
            slot_range=None,
            flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
            custody_policy=MaterialCustodyPolicy.TASK_EXCLUSIVE,
        )
