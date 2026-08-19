"""Edge scheduler 数据模型。

对齐 Go uni-lab-backend 的 workflow 模型子集
（pkg/repo/model WorkflowNode / WorkflowEdge / WorkflowHandleTemplate），
字段名尽量沿用 Go JSON tag，方便云端后续直接透传整图。

只用 dataclass + 标准库，保持调度内核零三方依赖（pydantic 仅在 api 层使用）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

from unilabos.app.scheduler.inventory.domain import MaterialRequirement

# 与 Go engine.DataKeySplit 一致（pkg/core/schedule/engine/model.go）
DATA_KEY_SPLIT = "@@@"

# 云端 workflow_node 类型枚举的规范拼写（大小写与后端模型一字不差）
NODE_TYPES = (
    "Group",
    "ILab",
    "py_script",
    "tool_call",
    "manual_confirm",
    "Transfer",
)
_NODE_TYPE_CANONICAL = {t.lower(): t for t in NODE_TYPES}


def normalize_node_type(value: Any) -> str:
    """把节点类型归一到云端规范拼写；未知值原样保留（不吞新类型）。"""
    text = str(value or "").strip()
    if not text:
        return "ILab"
    return _NODE_TYPE_CANONICAL.get(text.lower(), text)


# 本枚举服务 Edge Local REST v1/旧调度器内部兼容，不是 Backend canonical 状态目录。
# Backend d552078 的 Workflow Task/Job 公共成功终态是 succeeded；Local v1 响应应由
# Adapter 映射 succeeded → success，进入 Backend-shaped 共享模型时必须反向规范化。
# WebSocket/Backend-shaped 上行统一经 ``to_backend_workflow_status`` 适配；
# 本地快照和 Local REST 仍保留旧值，不能据此扩展 Backend 状态。
# Edge 还保留 ready/dispatched（内部推进态）、waiting_for_material（等料）和
# interrupted（仅历史库）；这些值不能直接扩展 Backend 表 CHECK 或公共 DTO。
class NodeState(str, Enum):
    PENDING = "pending"      # 尚有未完成的前置依赖
    READY = "ready"          # 依赖已清零，等待排序/下发
    DISPATCHED = "dispatched"  # 已下发给设备执行（对应云端 job running）
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMEOUT = "timeout"      # 词汇对齐云端 job 状态；Edge 调度器当前不主动产生


class WorkflowState(str, Enum):
    PENDING = "pending"      # 词汇对齐云端 workflow_task；Edge 提交即 running
    RUNNING = "running"
    WAITING_MATERIAL = "waiting_for_material"  # Edge 扩展：物料预留不足，等待补料后重试
    PAUSED = "paused"        # 词汇对齐云端；Edge 调度器当前不主动产生
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMEOUT = "timeout"      # 词汇对齐云端；超时判定由云端 Cron/调度器负责


def to_backend_workflow_status(value: Any) -> str:
    """Normalize a local scheduler status at the Backend wire boundary."""

    status = str(value.value if isinstance(value, Enum) else value or "").strip()
    return "succeeded" if status == "success" else status


def from_backend_workflow_status(value: Any) -> str:
    """Project a Backend status into the explicitly legacy Local REST model."""

    status = str(value.value if isinstance(value, Enum) else value or "").strip()
    return "success" if status == "succeeded" else status


@dataclass
class Handle:
    """workflow_handle_template 子集。

    传参解析只用 data_source / handle_key / data_key 三个字段。
    云端 ER 白板定稿：workflow_edge 以 handle **uuid** 引用连接点（规范路径）；
    handle_key 是模板内端口语义键（唯一性待云端确认），作为 payload 未带 uuid
    时的兼容寻址——此时需要 node_id 限定归属（同名 key 可能出现在多个节点）。
    """

    uuid: str = ""
    data_source: str = ""   # "executor" 表示取自父节点执行返回值
    handle_key: str = ""    # "ready" 的 handle 只表达顺序依赖，不传参
    data_key: str = ""      # gjson 取值路径；target 侧可含 "@@@" 分隔的嵌套键
    node_id: str = ""       # 该 handle 挂在哪个 workflow_node 上（key 寻址时必需）
    io_type: str = ""       # source / target（对齐 workflow_handle_template.io_type）


@dataclass
class WorkflowNode:
    """WorkflowNode 子集：Edge 执行一个设备动作所需的全部信息。"""

    id: str                       # 节点 id（uuid 或云端 node_id 字符串化）
    device_id: str = ""           # 目标设备
    action_name: str = ""         # 设备动作名
    action_type: str = ""         # goal / goal_sequence 等
    param: Dict[str, Any] = field(default_factory=dict)  # action 参数（会被父节点传参覆写）
    # 与云端 workflow_node 类型枚举一致：Group / ILab / py_script / tool_call /
    # manual_confirm / Transfer（Edge 目前只执行 ILab；Transfer 仅规范化/透传，
    # 比较请用 is_ilab()，容忍大小写差异）
    node_type: str = "ILab"
    disabled: bool = False
    # 可选物料需求（向后兼容：空列表 = 无物料，行为与旧 workflow 完全一致）
    material_requirements: List[MaterialRequirement] = field(default_factory=list)

    @property
    def device_action_key(self) -> str:
        """与 ws_client 一致的设备动作锁 key。"""
        return f"/devices/{self.device_id}/{self.action_name}"

    def is_ilab(self) -> bool:
        return normalize_node_type(self.node_type) == "ILab"

    def is_manual_confirm(self) -> bool:
        return normalize_node_type(self.node_type) == "manual_confirm"


@dataclass
class WorkflowEdge:
    """workflow_edge 子集：节点依赖 + handle 传参对。

    云端 ER 白板定稿：workflow_edge 存 source/target_handle_uuid（四元组
    有效数据唯一），uuid 是规范引用；*_handle_key 为 payload 未带 uuid 时的
    兼容寻址字段。解析优先级：uuid → (node_id, handle_key) → 全局唯一 key。
    """

    uuid: str
    source_node_id: str
    target_node_id: str
    source_handle_uuid: str = ""
    target_handle_uuid: str = ""
    source_handle_key: str = ""
    target_handle_key: str = ""


@dataclass
class WorkflowSpec:
    """一次工作流提交（云端下发整图，或本地 API 提交）。"""

    workflow_id: str
    nodes: List[WorkflowNode]
    edges: List[WorkflowEdge] = field(default_factory=list)
    handles: List[Handle] = field(default_factory=list)
    # 排序输入：与 lab-scheduler Priority 枚举/float 权重语义一致
    priority: Any = 1.0
    submitted_at: float = field(default_factory=time.time)
    lab_id: str = ""
    task_id: str = ""             # 云端 WorkflowTask uuid（可空，Edge 本地提交时等于 workflow_id）

    def __post_init__(self) -> None:
        if not self.task_id:
            self.task_id = self.workflow_id

    def material_requirements_by_node(self) -> Dict[str, List[MaterialRequirement]]:
        """按节点汇总物料需求（无需求节点不出现；空 dict = 全 DAG 无物料）。"""
        return {
            node.id: node.material_requirements
            for node in self.nodes
            if node.material_requirements and not node.disabled
        }


@dataclass
class HandlePair:
    """Go engine.HandlePair 等价：target 节点的一条传参边。"""

    source_node_id: str
    source_handle: Handle
    target_handle: Handle


@dataclass
class ReadyTask:
    """一次重排的输入单元：某工作流中一个 ready 节点。"""

    workflow_id: str
    node: WorkflowNode
    priority_weight: float
    submitted_at: float


@dataclass
class DispatchedJob:
    """已下发、未完结的 job（资源锁跟踪、完成回调路由与泳道图时间线）。"""

    job_id: str
    workflow_id: str
    node_id: str
    device_action_key: str
    dispatched_at: float = field(default_factory=time.time)
    device_id: str = ""
    action_name: str = ""
    # 下发时刻的预估执行时长（泳道图预估终点）与来源（declared/historical/default）
    estimated_s: float = 0.0
    estimate_source: str = "default"


# 与 lab-scheduler api/schemas.py PRIORITY_WEIGHTS 一致
PRIORITY_WEIGHTS: Dict[str, float] = {
    "urgent": 300.0,
    "high": 200.0,
    "normal": 100.0,
    "low": 50.0,
}


def priority_weight(priority: Any) -> float:
    """priority 字符串枚举或数值 → 权重，语义对齐 lab-scheduler Task.weight。"""
    if isinstance(priority, str):
        try:
            return PRIORITY_WEIGHTS[priority]
        except KeyError:
            return float(priority)
    return float(priority)


def node_from_dict(data: Dict[str, Any]) -> WorkflowNode:
    return WorkflowNode(
        id=str(data["id"]),
        device_id=data.get("device_id", "") or "",
        action_name=data.get("action_name", "") or "",
        action_type=data.get("action_type", "") or "",
        param=dict(data.get("param") or {}),
        node_type=normalize_node_type(data.get("node_type") or data.get("type")),
        disabled=bool(data.get("disabled", False)),
        material_requirements=[
            MaterialRequirement.from_dict(r) for r in (data.get("material_requirements") or [])
        ],
    )


def edge_from_dict(data: Dict[str, Any]) -> WorkflowEdge:
    return WorkflowEdge(
        uuid=str(data.get("uuid", "") or f"{data['source_node_id']}->{data['target_node_id']}"),
        source_node_id=str(data["source_node_id"]),
        target_node_id=str(data["target_node_id"]),
        source_handle_uuid=str(data.get("source_handle_uuid", "") or ""),
        target_handle_uuid=str(data.get("target_handle_uuid", "") or ""),
        source_handle_key=str(data.get("source_handle_key", "") or ""),
        target_handle_key=str(data.get("target_handle_key", "") or ""),
    )


def handle_from_dict(data: Dict[str, Any]) -> Handle:
    return Handle(
        uuid=str(data.get("uuid", "") or ""),
        data_source=data.get("data_source", "") or "",
        handle_key=data.get("handle_key", "") or "",
        data_key=data.get("data_key", "") or "",
        node_id=str(data.get("node_id", "") or ""),
        io_type=data.get("io_type", "") or "",
    )


def spec_from_dict(data: Dict[str, Any]) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=str(data["workflow_id"]),
        nodes=[node_from_dict(n) for n in data.get("nodes", [])],
        edges=[edge_from_dict(e) for e in data.get("edges", [])],
        handles=[handle_from_dict(h) for h in data.get("handles", [])],
        priority=data.get("priority", 1.0),
        submitted_at=float(data.get("submitted_at") or time.time()),
        lab_id=str(data.get("lab_id", "") or ""),
        task_id=str(data.get("task_id", "") or ""),
    )


__all__ = [
    "DATA_KEY_SPLIT",
    "DispatchedJob",
    "Handle",
    "HandlePair",
    "MaterialRequirement",
    "NODE_TYPES",
    "NodeState",
    "PRIORITY_WEIGHTS",
    "ReadyTask",
    "WorkflowEdge",
    "WorkflowNode",
    "WorkflowSpec",
    "WorkflowState",
    "edge_from_dict",
    "handle_from_dict",
    "node_from_dict",
    "normalize_node_type",
    "priority_weight",
    "spec_from_dict",
]
