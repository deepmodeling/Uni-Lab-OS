"""DagExecutor：节点取消（执行面撤单）与节点失败的走图语义。"""

from __future__ import annotations

import asyncio

from unilabos.server.backend.scheduler.dag.executor import DagExecutor
from unilabos.server.backend.scheduler.dag.models import DagEdge, DagNode, NodeState, TaskDag


def _chain(*ids: str) -> TaskDag:
    nodes = {nid: DagNode(node_id=nid, device_id="dev", action="act") for nid in ids}
    edges = [DagEdge(source_node_uuid=a, target_node_uuid=b) for a, b in zip(ids, ids[1:])]
    return TaskDag(task_id="t", notebook_id="", server_info={}, nodes=nodes, edges=edges)


def _run(dag: TaskDag, outcomes: dict[str, NodeState]) -> tuple[dict[str, NodeState], list[str]]:
    submitted: list[str] = []

    async def submit(node: DagNode) -> NodeState:
        submitted.append(node.node_id)
        await asyncio.sleep(0)
        return outcomes.get(node.node_id, NodeState.SUCCESS)

    result = asyncio.run(DagExecutor(dag, submit).run())
    return result, submitted


def test_cancelled_node_stops_the_walk_without_marking_failure() -> None:
    """第 2 步被取消：后继依赖永不满足，剩余节点收敛为 CANCELLED，全图没有 FAILED
    ——调度器据此把任务终态定为 canceled 而不是 failed。"""

    result, submitted = _run(_chain("a", "b", "c", "d"), {"b": NodeState.CANCELLED})

    assert submitted == ["a", "b"]
    assert result == {
        "a": NodeState.SUCCESS,
        "b": NodeState.CANCELLED,
        "c": NodeState.CANCELLED,
        "d": NodeState.CANCELLED,
    }
    assert NodeState.FAILED not in result.values()
    assert all(state != NodeState.PENDING for state in result.values())


def test_failed_node_still_fail_fasts() -> None:
    result, submitted = _run(_chain("a", "b", "c"), {"b": NodeState.FAILED})

    assert submitted == ["a", "b"]
    assert result == {"a": NodeState.SUCCESS, "b": NodeState.FAILED, "c": NodeState.CANCELLED}
