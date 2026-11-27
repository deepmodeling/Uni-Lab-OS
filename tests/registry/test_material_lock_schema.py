"""动作物料锁（Action Material Lock）Schema 解析的公共行为测试。"""

from __future__ import annotations

import pytest

from unilabos.registry.material_lock_schema import (
    MaterialLockSchemaError,
    compile_material_lock_schema,
)

_UUID_A = "11111111-1111-1111-1111-111111111111"
_UUID_B = "22222222-2222-2222-2222-222222222222"


def _material_reference(*, locked: bool, nullable: bool = False) -> dict:
    """构造测试使用的物料稳定引用 Schema。

    Args:
        locked: 该物料引用是否需要生成动作物料锁（Action Material Lock）。
        nullable: 该物料引用是否允许显式传入 ``null``。

    Returns:
        带 ``uuid`` 强校验和物料锁标记的 JSON Schema。
    """

    return {
        "type": ["object", "null"] if nullable else "object",
        "x-unilabos-material-lock": locked,
        "properties": {
            "uuid": {"type": "string", "format": "uuid"},
        },
        "required": ["uuid"],
        "additionalProperties": False,
    }


def _action_schema(goal_schema: dict) -> dict:
    """把 Goal 参数 Schema 包装成动作（Action）Schema。

    Args:
        goal_schema: 最终动作参数对象的 JSON Schema。

    Returns:
        注册表（Registry）保存的动作 Schema envelope。
    """

    return {
        "type": "object",
        "properties": {
            "goal": goal_schema,
            "feedback": {},
            "result": {},
        },
        "required": ["goal"],
    }


def test_extracts_required_optional_array_nested_and_local_ref_materials() -> None:
    """对象、数组、嵌套字段和本地引用应产生稳定去重的物料 UUID。"""

    # ``$defs`` 是动作 Schema 内的本地定义；解析不得读取网络或外部文件。
    raw_schema = _action_schema(
        {
            "$defs": {
                "material": _material_reference(locked=True),
            },
            "type": "object",
            "properties": {
                "plate": {"$ref": "#/$defs/material"},
                "optional": _material_reference(locked=True, nullable=True),
                "tips": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/material"},
                },
                "nested": {
                    "type": "object",
                    "properties": {
                        "material": {"$ref": "#/$defs/material"},
                    },
                    "required": ["material"],
                },
            },
            "required": ["plate", "tips", "nested"],
            "additionalProperties": False,
        }
    )

    compiled = compile_material_lock_schema(raw_schema)
    material_uuids = compiled.material_lock_uuids(
        {
            "plate": {"uuid": _UUID_B},
            "optional": None,
            "tips": [
                {"uuid": _UUID_A},
                {"uuid": _UUID_B},
            ],
            "nested": {"material": {"uuid": _UUID_A}},
        }
    )

    assert material_uuids == (_UUID_A, _UUID_B)


def test_explicit_free_material_is_validated_but_not_locked() -> None:
    """显式免锁的物料仍接受 Schema 校验，但不产生物料锁键。"""

    raw_schema = _action_schema(
        {
            "type": "object",
            "properties": {
                "free_material": _material_reference(locked=False),
            },
            "required": ["free_material"],
            "additionalProperties": False,
        }
    )

    compiled = compile_material_lock_schema(raw_schema)

    assert compiled.material_lock_uuids(
        {"free_material": {"uuid": _UUID_A}}
    ) == ()


def test_material_lock_marker_can_be_a_local_ref_sibling() -> None:
    """本地 ``$ref`` 旁的锁标记必须与引用目标一起生效。"""

    raw_schema = _action_schema(
        {
            "$defs": {
                "material": {
                    "type": "object",
                    "properties": {
                        "uuid": {"type": "string", "format": "uuid"},
                    },
                    "required": ["uuid"],
                    "additionalProperties": False,
                },
            },
            "type": "object",
            "properties": {
                "plate": {
                    "$ref": "#/$defs/material",
                    "x-unilabos-material-lock": True,
                },
            },
            "required": ["plate"],
            "additionalProperties": False,
        }
    )

    compiled = compile_material_lock_schema(raw_schema)

    assert compiled.material_lock_uuids(
        {"plate": {"uuid": _UUID_A}}
    ) == (_UUID_A,)


def test_runtime_resource_projection_keeps_non_material_fields_strict() -> None:
    """运行时 PLR 富对象可提锁，但不得放宽其他动作参数合同。"""

    raw_schema = _action_schema(
        {
            "type": "object",
            "properties": {
                "plate": _material_reference(locked=True),
                "site": {"type": "string"},
            },
            "required": ["plate", "site"],
            "additionalProperties": False,
        }
    )
    compiled = compile_material_lock_schema(raw_schema)
    runtime_param = {
        "plate": {
            # ROS 结果把 PLR ``Resource.unilabos_uuid`` 原样序列化；调度器
            # 内部投影需把它恢复成物料占位符（ResourceSlot）的 ``uuid``。
            "unilabos_uuid": _UUID_A,
            "name": "beaker-1",
            "children": [],
            "unilabos_extra": {"resource_template_uuid": _UUID_B},
        },
        "site": "A1",
    }

    assert compiled.material_lock_uuids(runtime_param) == (_UUID_A,)

    with pytest.raises(MaterialLockSchemaError) as caught:
        compiled.material_lock_uuids({**runtime_param, "unexpected": True})

    assert caught.value.code == "invalid_action_param"
    assert caught.value.path == "/"


@pytest.mark.parametrize(
    ("goal", "expected_code"),
    [
        pytest.param({}, "invalid_action_param", id="missing-required"),
        pytest.param(
            {"plate": {}},
            "invalid_action_param",
            id="missing-uuid",
        ),
        pytest.param(
            {"plate": {"uuid": "not-a-uuid"}},
            "material_lock_resolution_error",
            id="invalid-uuid",
        ),
    ],
)
def test_invalid_final_param_fails_closed(goal: dict, expected_code: str) -> None:
    """缺失必填值或 UUID 非法时必须失败关闭，不能按无锁继续执行。

    Args:
        goal: 合并静态参数和上游输出后的最终动作参数。
        expected_code: 对外稳定的解析错误码。
    """

    raw_schema = _action_schema(
        {
            "type": "object",
            "properties": {
                "plate": _material_reference(locked=True),
            },
            "required": ["plate"],
            "additionalProperties": False,
        }
    )
    compiled = compile_material_lock_schema(raw_schema)

    with pytest.raises(MaterialLockSchemaError) as caught:
        compiled.material_lock_uuids(goal)

    assert caught.value.code == expected_code


def test_external_ref_and_non_boolean_marker_are_rejected_at_compile_time() -> None:
    """外部引用和非布尔锁标记不得进入可执行动作合同（ActionContract）。"""

    # 两个非法 Schema 分别证明“只允许本地引用”和“锁标记必须是布尔值”。
    invalid_schemas = [
        _action_schema(
            {
                "type": "object",
                "properties": {"plate": {"$ref": "https://example.test/material"}},
            }
        ),
        _action_schema(
            {
                "type": "object",
                "properties": {
                    "plate": {
                        **_material_reference(locked=True),
                        "x-unilabos-material-lock": "yes",
                    },
                },
            }
        ),
    ]

    for raw_schema in invalid_schemas:
        with pytest.raises(MaterialLockSchemaError) as caught:
            compile_material_lock_schema(raw_schema)
        assert caught.value.code == "invalid_material_lock_schema"
