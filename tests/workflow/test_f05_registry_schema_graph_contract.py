"""设备注册表模板投影（Registry Template Projection）的全图参数合同测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from unilabos.registry.action_template_projection import goal_parameter_schema
from unilabos.workflow.graph_validation import GraphValidationError, validate_graph
from unilabos.workflow.models import WorkflowNodeWrite

# ``TEMPLATE_UUID`` 是被工作流节点（WorkflowNode）引用的节点模板稳定身份。
TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
# ``NODE_UUID`` 是参与公共全图校验的工作流节点稳定身份。
NODE_UUID = "20000000-0000-4000-8000-000000000001"


def _validate_single_node_schema(raw_schema: Any, param: dict[str, Any]) -> None:
    """通过公共全图校验接缝验证一个节点参数 JSON Schema。

    参数说明：``raw_schema`` 是节点模板保存的原始 JSON Schema，``param`` 是
    工作流节点（WorkflowNode）的最终参数。返回：无；参数满足模板合同则正常
    返回。异常：Schema 容器或参数值无效时抛出 ``GraphValidationError``。
    """

    # ``workflow_node`` 模拟引用设备注册表模板投影的单个计算节点。
    workflow_node = WorkflowNodeWrite(
        uuid=NODE_UUID,
        workflow_node_template_uuid=TEMPLATE_UUID,
        name="校验节点参数",
        type="compute",
        param=param,
    )
    validate_graph(
        nodes=[workflow_node],
        edges=[],
        templates={
            TEMPLATE_UUID: {
                "uuid": TEMPLATE_UUID,
                "node_type": "compute",
                "schema": raw_schema,
            }
        },
        handles={},
        effective_params={NODE_UUID: param},
        workflow_meta_data={},
        node_meta_data={NODE_UUID: {}},
    )


def test_projected_mapping_schema_crosses_public_graph_validation() -> None:
    """验证投影字典参数 Schema 可通过公共全图校验且原输入保持不变。

    参数说明：无。返回：无。异常/断言：公共校验拒绝投影 Schema，或校验过程
    修改调用方持有的模板参数 Schema 时断言失败。
    """

    # ``projected_schema`` 使用设备注册表模板投影生产路径的同一编译函数生成。
    projected_schema = goal_parameter_schema(
        {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": {"volume": {"type": "number"}},
                    "required": ["volume"],
                    "additionalProperties": False,
                }
            },
        }
    )
    # ``original_schema`` 是验证深分离和原输入不变的模板参数 Schema 基准副本。
    original_schema = deepcopy(projected_schema)

    _validate_single_node_schema(projected_schema, {"volume": 1.5})

    assert projected_schema == original_schema


@pytest.mark.parametrize(
    "raw_schema",
    [
        pytest.param(
            '{"type":"object","properties":{"volume":{"type":"number"}}}',
            id="json-object-string",
        ),
        pytest.param(True, id="boolean-true"),
        pytest.param("true", id="json-boolean-string"),
    ],
)
def test_compatible_serialized_and_boolean_schemas_remain_valid(
    raw_schema: Any,
) -> None:
    """验证合法 JSON 字符串和放行布尔 Schema 保持既有兼容行为。

    参数说明：``raw_schema`` 是一种合法的既有节点模板 Schema 表示。返回：无；
    异常/断言：公共全图校验拒绝该表示时测试失败，证明投影字典支持破坏了线协议
    兼容性。
    """

    _validate_single_node_schema(raw_schema, {"volume": 1.5})


@pytest.mark.parametrize(
    "raw_schema",
    [
        pytest.param([{"type": "object"}], id="array-root"),
        pytest.param({"x-invalid": object()}, id="non-json-object-member"),
        pytest.param("{'type': 'object'}", id="python-repr-string"),
        pytest.param("{", id="malformed-json-string"),
    ],
)
def test_invalid_schema_representations_fail_closed(raw_schema: Any) -> None:
    """验证非法 Schema 对象或字符串不能绕过公共全图参数校验。

    参数说明：``raw_schema`` 是不符合节点模板 JSON Schema 合同的表示。返回：
    无；异常/断言：校验未稳定抛出 ``GraphValidationError`` 时断言失败，确保
    Python repr 或非 JSON 对象不会被误当作有效 Schema。
    """

    with pytest.raises(GraphValidationError, match="节点参数 JSON Schema"):
        _validate_single_node_schema(raw_schema, {"volume": 1.5})


@pytest.mark.parametrize(
    "raw_schema",
    [
        pytest.param(False, id="boolean-false"),
        pytest.param("false", id="json-boolean-false-string"),
    ],
)
def test_rejecting_boolean_schemas_keep_their_json_schema_semantics(
    raw_schema: Any,
) -> None:
    """验证拒绝布尔 Schema 继续拒绝任意节点参数。

    参数说明：``raw_schema`` 是对象值或 JSON 字符串形式的拒绝布尔 Schema。
    返回：无；异常/断言：公共全图校验未按 JSON Schema 语义抛出
    ``GraphValidationError`` 时断言失败。
    """

    with pytest.raises(GraphValidationError, match="不满足 JSON Schema"):
        _validate_single_node_schema(raw_schema, {"volume": 1.5})
