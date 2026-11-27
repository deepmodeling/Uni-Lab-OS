"""工站级作业等待图与运行时循环诊断。"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from unilabos.workflow.json_codec import decode_json_bytes


def build_execution_wait_graph(connection: sqlite3.Connection) -> dict[str, Any]:
    """从持久 Job 等待原因构建整站有向等待图。

    参数：``connection`` 是工作流只读事务。返回：包含稳定排序节点、等待边和所有
    强连通循环的快照；边方向为等待作业指向阻塞作业。异常：SQLite 读取错误原样
    传播；单行损坏 JSON 关闭式标记为 ``invalid_wait_reason``，不伪造依赖边。
    """

    rows = connection.execute(
        """
        SELECT job.uuid AS job_uuid, job.workflow_task_uuid AS task_uuid,
               job.status, job.wait_reason
        FROM workflow_node_job AS job
        JOIN workflow_task AS task ON task.uuid=job.workflow_task_uuid
        WHERE job.deleted_at IS NULL AND task.deleted_at IS NULL
          AND job.status='pending'
          AND task.status IN ('pending', 'running')
          AND job.wait_reason <> '{}'
        ORDER BY task.create_time, task.uuid, job.topological_index, job.uuid
        """
    ).fetchall()
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    adjacency: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        job_uuid = str(row["job_uuid"])
        reason = _decode_wait_reason(row["wait_reason"])
        node = {
            "job_uuid": job_uuid,
            "task_uuid": str(row["task_uuid"]),
            "wait_code": str(reason.get("code") or "invalid_wait_reason"),
            "waiting_since": str(reason.get("waiting_since") or ""),
        }
        blocker = str(reason.get("blocking_job_uuid") or "").strip()
        if blocker:
            node["blocking_job_uuid"] = blocker
            adjacency[job_uuid].add(blocker)
            edges.append(
                {
                    "waiting_job_uuid": job_uuid,
                    "blocking_job_uuid": blocker,
                }
            )
        nodes.append(node)
    cycles = _find_cycles(adjacency)
    cycle_jobs = {job_uuid for cycle in cycles for job_uuid in cycle}
    for node in nodes:
        node["deadlock_cycle"] = node["job_uuid"] in cycle_jobs
    return {
        "nodes": nodes,
        "edges": sorted(
            edges,
            key=lambda item: (
                item["waiting_job_uuid"],
                item["blocking_job_uuid"],
            ),
        ),
        "cycles": [list(cycle) for cycle in cycles],
        "deadlock_detected": bool(cycles),
    }


def _decode_wait_reason(raw: Any) -> Mapping[str, Any]:
    """解码单个等待原因；损坏值返回稳定诊断对象。"""

    try:
        value = decode_json_bytes(str(raw or "{}").encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        return {"code": "invalid_wait_reason"}
    return value if isinstance(value, Mapping) else {"code": "invalid_wait_reason"}


def _find_cycles(adjacency: Mapping[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    """使用 Tarjan 算法返回稳定排序的多节点或自环强连通分量。"""

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        """深度遍历一个节点并在根位置提交循环分量。"""

        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(adjacency.get(node, set())):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while stack:
            current = stack.pop()
            on_stack.remove(current)
            component.append(current)
            if current == node:
                break
        stable = tuple(sorted(component))
        if len(stable) > 1 or node in adjacency.get(node, set()):
            cycles.append(stable)

    all_nodes = set(adjacency)
    all_nodes.update(target for targets in adjacency.values() for target in targets)
    for node in sorted(all_nodes):
        if node not in indices:
            visit(node)
    return tuple(sorted(cycles))


__all__ = ["build_execution_wait_graph"]
