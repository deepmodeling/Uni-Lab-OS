"""时间规划对已证明 ResourceSlot 透传的静态参数快照。"""

from unilabos.app.scheduler.dag_state import WorkflowRun
from unilabos.app.scheduler.models import (
    Handle,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    node_from_dict,
)
from unilabos.app.scheduler.param_resolver import ParamResolveError


def _chain(*, value_type: str = "ResourceSlot") -> WorkflowRun:
    nodes = [
        WorkflowNode(
            id="a",
            param={"resource": {"uuid": "material-1"}},
            material_passthrough_handles={"a-out": "a-in"},
        ),
        WorkflowNode(
            id="b",
            material_passthrough_handles={"b-out": "b-in"},
        ),
        WorkflowNode(id="c"),
    ]
    handles = [
        Handle("a-in", "", "resource", "resource", "a", "target", value_type),
        Handle("a-out", "executor", "resource", "resource", "a", "source", value_type),
        Handle("b-in", "", "resource", "resource", "b", "target", value_type),
        Handle("b-out", "executor", "resource", "resource", "b", "source", value_type),
        Handle("c-in", "", "resource", "resource", "c", "target", value_type),
    ]
    edges = [
        WorkflowEdge("a-b", "a", "b", "a-out", "b-in"),
        WorkflowEdge("b-c", "b", "c", "b-out", "c-in"),
    ]
    return WorkflowRun(WorkflowSpec("workflow", nodes, edges, handles))


def test_resource_slot_passthrough_resolves_entire_pending_chain() -> None:
    """未收到设备回执时，合同证明的 ResourceSlot 透传仍可用于时间快照。"""

    run = _chain()

    assert run.resolve_params("b") == {"resource": {"uuid": "material-1"}}
    assert run.resolve_params("c") == {"resource": {"uuid": "material-1"}}
    # 静态预测不写入真实回执，条件/运行态仍等待设备反馈。
    assert run.snapshot()["nodes"]["a"]["state"] == "pending"


def test_node_from_dict_reads_nested_and_top_level_passthrough_metadata() -> None:
    """执行计划两种元数据布局都能恢复静态透传合同。"""

    nested = node_from_dict(
        {
            "id": "nested",
            "meta_data": {
                "unilab": {
                    "material_passthrough_handles": {"output": "input"}
                }
            },
        }
    )
    top_level = node_from_dict(
        {
            "id": "top-level",
            "material_passthrough_handles": {"output": "input"},
            "meta_data": {
                "unilab": {
                    "material_passthrough_handles": {"old-output": "old-input"}
                }
            },
        }
    )

    assert nested.material_passthrough_handles == {"output": "input"}
    assert top_level.material_passthrough_handles == {"output": "input"}


def test_non_resource_edges_do_not_get_static_passthrough() -> None:
    """普通标量边不能因为同名字段而被猜测。"""

    run = _chain(value_type="string")

    try:
        run.resolve_params("b")
    except ParamResolveError:
        pass
    else:
        raise AssertionError("非 ResourceSlot 透传不应产生静态参数")
