"""工作流输入/输出（Workflow I/O）公共校验器的领域不变量测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_io,
)

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
NODE_UUID = "10000000-0000-4000-8000-000000000002"
FOREIGN_NODE_UUID = "10000000-0000-4000-8000-000000000003"
NODE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000004"
SOURCE_HANDLE_UUID = "10000000-0000-4000-8000-000000000006"
TARGET_HANDLE_UUID = "10000000-0000-4000-8000-000000000007"
RESOURCE_TEMPLATE_A_UUID = "10000000-0000-4000-8000-000000000008"
RESOURCE_TEMPLATE_B_UUID = "10000000-0000-4000-8000-000000000009"

EMPTY_IO = {
    "input_contract": {"version": 1, "parameters": []},
    "output_contract": {"version": 1, "outputs": []},
    "output_bindings": {},
}


def _handle(
    *,
    handle_uuid: str,
    io_type: str,
    value_schema: dict[str, object],
    value_type: str,
) -> dict[str, object]:
    """构造连接点（Handle）投影。

    参数说明：`handle_uuid` 是稳定身份，`io_type` 是输入或输出方向，
    `value_schema` 是可赋值集合，`value_type` 仅保留旧接口显示类型。
    返回值是公共校验器消费的最小连接点（Handle）投影。
    """

    unilab: dict[str, object] = {"value_schema": deepcopy(value_schema)}
    base_schema = value_schema
    if "anyOf" in value_schema:
        base_schema = value_schema["anyOf"][0]  # type: ignore[index]
    if base_schema.get("$slot") == "ResourceSlot":
        unilab["allowed_resource_template_uuids"] = base_schema.get(
            "allowed_resource_template_uuids"
        )
    return {
        "uuid": handle_uuid,
        "meta_data": {"unilab": unilab},
        "workflow_node_template_uuid": NODE_TEMPLATE_UUID,
        "handle_key": "result",
        "io_type": io_type,
        "type": value_type,
        "required": False,
        "data_source": "result",
        "data_key": "result",
    }


def _producer_bundle(
    source_schema: dict[str, object] | None = None,
    *,
    source_type: str = "number",
) -> dict[str, Any]:
    """构造含一个生产节点的工作流输入/输出（Workflow I/O）校验包。

    参数说明：`source_schema` 描述生产端值集合，`source_type` 是旧连接点
    （Handle）类型兼容字段。返回值包含工作流元数据、节点、节点元数据和
    连接点（Handle）。
    """

    node = WorkflowNodeWrite(
        uuid=NODE_UUID,
        workflow_node_template_uuid=NODE_TEMPLATE_UUID,
        name="producer",
        type="compute",
        pose={},
        param={},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={"unilab": {"input_bindings": {}}},
    )
    handles = {
        SOURCE_HANDLE_UUID: _handle(
            handle_uuid=SOURCE_HANDLE_UUID,
            io_type="source",
            value_schema=source_schema or {"type": "number"},
            value_type=source_type,
        ),
        TARGET_HANDLE_UUID: _handle(
            handle_uuid=TARGET_HANDLE_UUID,
            io_type="target",
            value_schema={"type": "number"},
            value_type="number",
        ),
    }
    return {
        "workflow_meta_data": {"unilab": deepcopy(EMPTY_IO)},
        "nodes": {NODE_UUID: node},
        "node_meta_data": {NODE_UUID: node.meta_data},
        "handles": handles,
    }


def _declare_output(
    bundle: dict[str, Any],
    *,
    schema: dict[str, object],
    binding: dict[str, object] | None,
) -> None:
    """在校验包中声明一个工作流输出（Workflow Output）。

    参数说明：`bundle` 是待修改校验包，`schema` 是输出承诺，`binding`
    是根绑定；传入 `None` 用来表达缺失绑定的失败场景。函数原地修改包。
    """

    unilab = bundle["workflow_meta_data"]["unilab"]
    unilab["output_contract"] = {
        "version": 1,
        "outputs": [
            {
                "name": "result",
                "schema": deepcopy(schema),
                "implicit": False,
            }
        ],
    }
    unilab["output_bindings"] = (
        {} if binding is None else {"result": deepcopy(binding)}
    )


def _declare_input_binding(
    bundle: dict[str, Any],
    *,
    producer_schema: dict[str, object],
    consumer_schema: dict[str, object],
    consumer_type: str,
    producer_required: bool = True,
) -> None:
    """声明工作流输入到节点目标连接点（Handle）的绑定。

    参数说明：`producer_schema` 是工作流输入保证，`consumer_schema` 是节点
    接受集合，`consumer_type` 是旧显示类型，`producer_required` 决定可空性。
    物料占位符（ResourceSlot）输入同时创建服务端管理的同名隐式输出。
    """

    parameter: dict[str, object] = {
        "name": "input",
        "schema": deepcopy(producer_schema),
        "required": producer_required,
    }
    if not producer_required:
        parameter["default"] = None
    unilab = bundle["workflow_meta_data"]["unilab"]
    unilab["input_contract"] = {
        "version": 1,
        "parameters": [parameter],
    }
    bundle["node_meta_data"][NODE_UUID]["unilab"]["input_bindings"] = {
        TARGET_HANDLE_UUID: {"parameter": "input"}
    }
    bundle["handles"][TARGET_HANDLE_UUID] = _handle(
        handle_uuid=TARGET_HANDLE_UUID,
        io_type="target",
        value_schema=consumer_schema,
        value_type=consumer_type,
    )
    if producer_schema.get("$slot") == "ResourceSlot":
        unilab["output_contract"] = {
            "version": 1,
            "outputs": [
                {
                    "name": "input",
                    "schema": deepcopy(producer_schema),
                    "implicit": True,
                }
            ],
        }
        unilab["output_bindings"] = {
            "input": {"kind": "workflow_input", "parameter": "input"}
        }


def _validate(bundle: dict[str, Any]):
    """调用唯一公开的工作流输入/输出（Workflow I/O）校验接缝。

    参数说明：`bundle` 是传输层（Transport）无关的完整校验事实集合。
    返回值是冻结的 `ValidatedWorkflowIO`，异常必须统一为
    `WorkflowIOValidationError`。
    """

    return validate_workflow_io(**bundle)


def _node_output_binding(
    *,
    workflow_node_uuid: str = NODE_UUID,
    source_handle_uuid: str = SOURCE_HANDLE_UUID,
) -> dict[str, object]:
    """构造节点输出根绑定。

    参数说明：两个 UUID 分别标识生产节点和其来源连接点（Handle）；返回闭合
    绑定对象。
    """

    return {
        "kind": "node_output",
        "workflow_node_uuid": workflow_node_uuid,
        "source_handle_uuid": source_handle_uuid,
    }


def test_rejects_declared_output_without_root_binding() -> None:
    """显式工作流输出（Workflow Output）必须且只能有一个根绑定。"""

    bundle = _producer_bundle()
    _declare_output(bundle, schema={"type": "number"}, binding=None)

    with pytest.raises(WorkflowIOValidationError):
        _validate(bundle)


def test_rejects_scalar_output_marked_as_implicit() -> None:
    """隐式输出只服务于物料占位符（ResourceSlot）透传，不能用于标量。"""

    bundle = _producer_bundle()
    _declare_output(
        bundle,
        schema={"type": "number"},
        binding=_node_output_binding(),
    )
    output = bundle["workflow_meta_data"]["unilab"]["output_contract"]["outputs"][0]
    output["implicit"] = True

    with pytest.raises(WorkflowIOValidationError):
        _validate(bundle)


def test_rejects_deleted_implicit_resource_slot_passthrough() -> None:
    """物料占位符（ResourceSlot）输入不得删除服务端管理的同名输出。"""

    bundle = _producer_bundle()
    slot_schema = {
        "$slot": "ResourceSlot",
        "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_A_UUID],
    }
    _declare_input_binding(
        bundle,
        producer_schema=slot_schema,
        consumer_schema=slot_schema,
        consumer_type="ResourceSlot",
    )
    unilab = bundle["workflow_meta_data"]["unilab"]
    unilab["output_contract"] = {"version": 1, "outputs": []}
    unilab["output_bindings"] = {}

    with pytest.raises(WorkflowIOValidationError):
        _validate(bundle)


@pytest.mark.parametrize(
    "binding",
    [
        pytest.param(
            _node_output_binding(workflow_node_uuid=FOREIGN_NODE_UUID),
            id="foreign-node",
        ),
        pytest.param(
            {"kind": "node_output", "workflow_node_uuid": NODE_UUID},
            id="missing-source-handle",
        ),
        pytest.param(
            _node_output_binding(source_handle_uuid=TARGET_HANDLE_UUID),
            id="target-direction-handle",
        ),
        pytest.param(
            {"kind": "workflow_input", "parameter": "unknown"},
            id="unknown-workflow-input",
        ),
    ],
)
def test_rejects_invalid_output_binding_identity(
    binding: dict[str, object],
) -> None:
    """输出绑定必须引用当前图中的正确节点、方向和稳定身份。"""

    bundle = _producer_bundle()
    _declare_output(bundle, schema={"type": "number"}, binding=binding)

    with pytest.raises(WorkflowIOValidationError):
        _validate(bundle)


@pytest.mark.parametrize(
    ("source_schema", "source_type", "output_schema"),
    [
        pytest.param(
            {"type": "number"},
            "number",
            {"type": "integer"},
            id="number-cannot-promise-integer",
        ),
        pytest.param(
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "string",
            {"type": "string"},
            id="nullable-cannot-promise-non-null",
        ),
        pytest.param(
            {"$slot": "ResourceSlot"},
            "ResourceSlot",
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_A_UUID],
            },
            id="unconstrained-placeholder-cannot-promise-restricted-placeholder",
        ),
    ],
)
def test_rejects_output_schema_not_guaranteed_by_producer(
    source_schema: dict[str, object],
    source_type: str,
    output_schema: dict[str, object],
) -> None:
    """生产端不能把比自身值集合更窄的类型承诺为工作流输出。"""

    bundle = _producer_bundle(source_schema, source_type=source_type)
    _declare_output(
        bundle,
        schema=output_schema,
        binding=_node_output_binding(),
    )

    with pytest.raises(WorkflowIOValidationError):
        _validate(bundle)


@pytest.mark.parametrize(
    ("source_schema", "source_type", "output_schema"),
    [
        pytest.param(
            {"type": "array", "items": {"type": "integer"}},
            "array",
            {"type": "array", "items": {"type": "number"}},
            id="integer-list-to-number-list",
        ),
        pytest.param(
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_A_UUID],
            },
            "ResourceSlot",
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [
                    RESOURCE_TEMPLATE_A_UUID,
                    RESOURCE_TEMPLATE_B_UUID,
                ],
            },
            id="placeholder-allowlist-subset",
        ),
    ],
)
def test_accepts_assignable_output_schema(
    source_schema: dict[str, object],
    source_type: str,
    output_schema: dict[str, object],
) -> None:
    """生产端全部值都落入消费端集合时允许建立输出绑定。"""

    bundle = _producer_bundle(source_schema, source_type=source_type)
    _declare_output(
        bundle,
        schema=output_schema,
        binding=_node_output_binding(),
    )

    validated = _validate(bundle)

    assert validated.output_bindings["result"]["kind"] == "node_output"


@pytest.mark.parametrize(
    ("producer_schema", "consumer_schema"),
    [
        pytest.param(
            {"$slot": "ResourceSlot"},
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_A_UUID],
            },
            id="unconstrained-producer",
        ),
        pytest.param(
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_A_UUID],
            },
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_B_UUID],
            },
            id="disjoint-allowlists",
        ),
    ],
)
def test_rejects_workflow_input_placeholder_not_guaranteed_for_handle(
    producer_schema: dict[str, object],
    consumer_schema: dict[str, object],
) -> None:
    """工作流输入的物料占位符集合必须满足节点连接点（Handle）。"""

    bundle = _producer_bundle()
    _declare_input_binding(
        bundle,
        producer_schema=producer_schema,
        consumer_schema=consumer_schema,
        consumer_type="ResourceSlot",
    )

    with pytest.raises(WorkflowIOValidationError):
        _validate(bundle)


@pytest.mark.parametrize(
    "producer_allowlist",
    [
        pytest.param([RESOURCE_TEMPLATE_A_UUID], id="proper-subset"),
        pytest.param(
            [RESOURCE_TEMPLATE_A_UUID, RESOURCE_TEMPLATE_B_UUID],
            id="same-set",
        ),
    ],
)
def test_accepts_workflow_input_placeholder_guaranteed_for_handle(
    producer_allowlist: list[str],
) -> None:
    """物料模板允许集合为节点集合子集时，物料占位符绑定可赋值。"""

    consumer_allowlist = [RESOURCE_TEMPLATE_A_UUID, RESOURCE_TEMPLATE_B_UUID]
    producer_schema = {
        "$slot": "ResourceSlot",
        "allowed_resource_template_uuids": producer_allowlist,
    }
    bundle = _producer_bundle()
    _declare_input_binding(
        bundle,
        producer_schema=producer_schema,
        consumer_schema={
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": consumer_allowlist,
        },
        consumer_type="ResourceSlot",
    )

    validated = _validate(bundle)

    assert validated.input_bindings[NODE_UUID][TARGET_HANDLE_UUID] == {
        "parameter": "input"
    }


def test_rejects_nullable_workflow_input_for_non_null_handle() -> None:
    """可空工作流输入不能赋给不接受空值的目标连接点（Handle）。"""

    bundle = _producer_bundle()
    _declare_input_binding(
        bundle,
        producer_schema={"anyOf": [{"type": "string"}, {"type": "null"}]},
        consumer_schema={"type": "string"},
        consumer_type="string",
        producer_required=False,
    )

    with pytest.raises(WorkflowIOValidationError):
        _validate(bundle)
