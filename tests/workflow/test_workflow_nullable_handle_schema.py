"""工作流连接点（Workflow Handle）可空 JSON Schema 边界合同测试。"""

from __future__ import annotations

import pytest

from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    handle_value_schema,
)


def _handle(value_schema: dict[str, object], *, value_type: str) -> dict[str, object]:
    """构造动作目录进入工作流边界时使用的最小连接点（Handle）投影。

    参数：``value_schema`` 是动作（Action）JSON Schema 字段投影；``value_type``
    是只用于旧界面显示兼容的连接点类型。
    返回：包含 ``meta_data.unilab.value_schema`` 的连接点投影字典。
    异常：无；输入不在此辅助函数中校验，由公开边界 ``handle_value_schema``
    统一失败关闭。
    """

    return {
        "type": value_type,
        "meta_data": {"unilab": {"value_schema": value_schema}},
    }


def test_nullable_scalar_type_union_becomes_canonical_workflow_schema() -> None:
    """验证可空标量 JSON Schema 被规范化为工作流（Workflow）唯一可空形式。

    参数：无。
    返回：无；断言 ``type: [integer, null]`` 确定性转为非空成员在前的 ``anyOf``。
    异常：规范化丢失可空性、默认值污染值集合或连接点拒绝合法输入时断言失败。
    """

    # ``handle`` 模拟动作合同为兼容泵参数生成的可空整数连接点（Handle）。
    handle = _handle(
        {"type": ["integer", "null"], "default": None},
        value_type="integer",
    )

    assert handle_value_schema(handle).to_dict() == {
        "anyOf": [{"type": "integer"}, {"type": "null"}]
    }


def test_nullable_material_reference_preserves_resource_slot_semantics() -> None:
    """验证可选物料引用仍投影为可空物料占位符（ResourceSlot）而非自由对象。

    参数：无。
    返回：无；断言动作物料锁（Action Material Lock）标记不进入工作流值类型，
    同时 ``null`` 选择保持在规范可空包装中。
    异常：UUID 引用形状、可空性或物料占位符语义丢失时断言失败。
    """

    # ``material_reference_schema`` 是动作输入目录提供的可选物料引用 JSON Schema；
    # 锁标记属于执行安全声明，不属于连接点（Handle）传递的值。
    material_reference_schema: dict[str, object] = {
        "type": ["object", "null"],
        "x-unilabos-material-lock": True,
        "properties": {
            "uuid": {"type": "string", "format": "uuid"},
        },
        "required": ["uuid"],
        "additionalProperties": False,
        "default": None,
    }

    assert handle_value_schema(
        _handle(material_reference_schema, value_type="ResourceSlot")
    ).to_dict() == {"anyOf": [{"$slot": "ResourceSlot"}, {"type": "null"}]}


def test_invalid_json_schema_type_union_fails_closed() -> None:
    """验证不是“一个受支持类型加 null”的 ``type`` 数组被连接点边界拒绝。

    参数：无。
    返回：无；断言多种非空类型不会被猜测、降级或宽松接受。
    异常：预期 ``WorkflowIOValidationError``；若未抛出则测试失败。
    """

    # ``invalid_handle`` 同时声明整数和字符串，已超出当前工作流值 Schema 闭集。
    invalid_handle = _handle(
        {"type": ["integer", "string"]},
        value_type="object",
    )

    with pytest.raises(
        WorkflowIOValidationError,
        match="连接点（Handle）value_schema 无效",
    ):
        handle_value_schema(invalid_handle)


@pytest.mark.parametrize(
    "invalid_type",
    (
        ["object", "null", "null"],
        ["object", "string"],
        ["null"],
    ),
)
def test_invalid_nullable_material_type_union_fails_closed(
    invalid_type: list[str],
) -> None:
    """验证物料引用不能在严格联合校验前被投影成物料占位符（ResourceSlot）。

    参数：``invalid_type`` 分别表达重复 ``null``、多个非空成员和缺少非空成员的
    非法 JSON Schema 联合。
    返回：无；断言动作物料锁（Action Material Lock）元数据不能绕过失败关闭。
    异常：预期 ``WorkflowIOValidationError``；若非法联合被接受则测试失败。
    """

    # ``invalid_material_schema`` 保持合法 UUID 引用外形，确保 RED 只证明联合
    # 基数检查不能被物料语义投影短路。
    invalid_material_schema: dict[str, object] = {
        "type": invalid_type,
        "x-unilabos-material-lock": True,
        "properties": {
            "uuid": {"type": "string", "format": "uuid"},
        },
        "required": ["uuid"],
        "additionalProperties": False,
    }

    with pytest.raises(
        WorkflowIOValidationError,
        match="连接点（Handle）value_schema 无效",
    ):
        handle_value_schema(_handle(invalid_material_schema, value_type="ResourceSlot"))
