"""OS 本地 DAG 执行器 — 数据结构与 task_dag 载荷解析。

本模块只负责“图的静态表示与合法性校验”，不含任何调度/执行逻辑
（走图在 dag_executor.py）。字段严格镜像 backend 契约，保证接口与后端一致：

- 节点字段镜像 SendActionData（uni-lab-backend pkg/core/schedule/engine/model.go:90）
- 边字段镜像 WorkflowEdge（uni-lab-backend pkg/repo/model/workflow.go:71）
  即 source_node_uuid / target_node_uuid（source 先于 target）

契约细节见 docs/features/F002-os-local-dag-executor/interface-design.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class NodeState(str, Enum):
    """节点状态机：PENDING -> READY -> RUNNING -> SUCCESS | FAILED | CANCELLED。"""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 终态集合：进入其一后节点不再流转
TERMINAL_STATES: frozenset[NodeState] = frozenset(
    {NodeState.SUCCESS, NodeState.FAILED, NodeState.CANCELLED}
)


class DagValidationError(ValueError):
    """task_dag 载荷非法时抛出：缺字段 / node_id 重复 / 悬空边 / 含环（I5）。"""


@dataclass
class DagNode:
    """一个可执行动作节点。字段镜像 backend SendActionData。

    node_id 同时用作该节点的 job_id，幂等键为 (task_id, node_id)。
    """

    node_id: str
    device_id: str
    action: str
    action_type: str = ""
    action_args: dict[str, Any] = field(default_factory=dict)
    # sample_material：uuid->uuid 映射，可空（镜像 SendActionData.sample_material）
    sample_material: dict[str, str] = field(default_factory=dict)
    # 可选；缺省由 registry 决定，OS 不强依赖此字段
    always_free: bool = False

    @property
    def device_action_key(self) -> str:
        """与 JobExecutionBackend 一致的每设备互斥键。

        同 device_action_key 的非 always_free 节点由 DeviceActionManager
        每设备锁天然串行（I3），DagExecutor 不在此层做互斥。
        """
        return f"/devices/{self.device_id}/{self.action}"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DagNode":
        if not isinstance(d, dict):
            raise DagValidationError(f"节点必须是对象，收到 {type(d).__name__}")
        node_id = d.get("node_id")
        device_id = d.get("device_id")
        action = d.get("action")
        if not node_id:
            raise DagValidationError("节点缺少 node_id")
        if not device_id:
            raise DagValidationError(f"节点 {node_id} 缺少 device_id")
        if not action:
            raise DagValidationError(f"节点 {node_id} 缺少 action")
        return cls(
            node_id=str(node_id),
            device_id=str(device_id),
            action=str(action),
            action_type=str(d.get("action_type", "") or ""),
            action_args=dict(d.get("action_args") or {}),
            sample_material=dict(d.get("sample_material") or {}),
            always_free=bool(d.get("always_free", False)),
        )


@dataclass
class DagEdge:
    """依赖边：source_node_uuid 终态后 target_node_uuid 方可起跑。"""

    source_node_uuid: str
    target_node_uuid: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DagEdge":
        if not isinstance(d, dict):
            raise DagValidationError(f"边必须是对象，收到 {type(d).__name__}")
        src = d.get("source_node_uuid")
        tgt = d.get("target_node_uuid")
        if not src or not tgt:
            raise DagValidationError(
                f"边缺少 source_node_uuid/target_node_uuid: {d!r}"
            )
        return cls(source_node_uuid=str(src), target_node_uuid=str(tgt))


@dataclass
class TaskDag:
    """整张任务 DAG。task_id 是全图的任务 id（= 现有 job_status.task_id）。"""

    task_id: str
    notebook_id: str
    server_info: dict[str, Any]
    nodes: dict[str, DagNode]  # node_id -> DagNode
    edges: list[DagEdge]

    @classmethod
    def from_message(cls, data: dict[str, Any]) -> "TaskDag":
        """解析 task_dag 载荷并校验合法性（含拒环，I5）。

        入参 data 是 WebSocket 消息 { "action": "task_dag", "data": <此处> } 的 data 段。
        """
        if not isinstance(data, dict):
            raise DagValidationError(f"task_dag data 必须是对象，收到 {type(data).__name__}")

        task_id = data.get("task_id")
        if not task_id:
            raise DagValidationError("task_dag 缺少 task_id")

        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise DagValidationError("task_dag 缺少非空 nodes 列表")

        nodes: dict[str, DagNode] = {}
        for raw in raw_nodes:
            node = DagNode.from_dict(raw)
            if node.node_id in nodes:
                raise DagValidationError(f"node_id 重复: {node.node_id}")
            nodes[node.node_id] = node

        raw_edges = data.get("edges") or []
        if not isinstance(raw_edges, list):
            raise DagValidationError("task_dag edges 必须是列表")

        edges: list[DagEdge] = []
        for raw in raw_edges:
            edge = DagEdge.from_dict(raw)
            # 悬空边（引用不存在节点）即拒，避免依赖永不满足静默挂起
            if edge.source_node_uuid not in nodes:
                raise DagValidationError(
                    f"边 source_node_uuid 指向不存在节点: {edge.source_node_uuid}"
                )
            if edge.target_node_uuid not in nodes:
                raise DagValidationError(
                    f"边 target_node_uuid 指向不存在节点: {edge.target_node_uuid}"
                )
            edges.append(edge)

        dag = cls(
            task_id=str(task_id),
            notebook_id=str(data.get("notebook_id", "") or ""),
            server_info=dict(data.get("server_info") or {}),
            nodes=nodes,
            edges=edges,
        )
        dag._assert_acyclic()  # 解析期即拒环（I5）
        return dag

    def build_indegree(self) -> dict[str, int]:
        """构建 in-degree 表：indeg[node_id] = 指向它的边数。"""
        indeg = {node_id: 0 for node_id in self.nodes}
        for edge in self.edges:
            indeg[edge.target_node_uuid] += 1
        return indeg

    def successors(self, node_id: str) -> list[str]:
        """node_id 的直接后继（out-edge 的 target）。"""
        return [e.target_node_uuid for e in self.edges if e.source_node_uuid == node_id]

    def adjacency(self) -> dict[str, list[str]]:
        """邻接表 node_id -> 后继列表（一次构建，避免重复扫边）。"""
        adj: dict[str, list[str]] = {node_id: [] for node_id in self.nodes}
        for edge in self.edges:
            adj[edge.source_node_uuid].append(edge.target_node_uuid)
        return adj

    def _assert_acyclic(self) -> None:
        """Kahn 拓扑消解：若无法消解全部节点，则存在环 -> 拒绝。"""
        indeg = self.build_indegree()
        adj = self.adjacency()
        ready = [n for n, d in indeg.items() if d == 0]
        processed = 0
        while ready:
            n = ready.pop()
            processed += 1
            for succ in adj[n]:
                indeg[succ] -= 1
                if indeg[succ] == 0:
                    ready.append(succ)
        if processed != len(self.nodes):
            stuck = sorted(n for n, d in indeg.items() if d > 0)
            raise DagValidationError(f"task_dag 含环，无法拓扑消解，涉及节点: {stuck}")
