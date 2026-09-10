"""Edge scheduler 数据模型。

对齐 Go uni-lab-backend 的 workflow 模型子集
（pkg/repo/model WorkflowNode / WorkflowEdge / WorkflowHandleTemplate），
字段名尽量沿用 Go JSON tag，方便云端后续直接透传整图。

只用 dataclass + 标准库，保持调度内核零三方依赖（pydantic 仅在 api 层使用）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
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


# 状态词汇与新后端（Uni-Lab-OS/uni-lab-backend）表设计保持一致：
# - workflow_task.status: pending/running/paused/success/failed/canceled/timeout
# - workflow_node_job.status 终态同样使用 success（不是 succeeded）
# Edge 额外扩展：ready/dispatched（节点内部推进态）、waiting_for_material（等料）、
# interrupted（进程重启标记，仅历史库）；上报云端时终态词汇与云端枚举一字不差。
class NodeState(str, Enum):
    PENDING = "pending"  # 尚有未完成的前置依赖
    READY = "ready"  # 依赖已清零，等待排序/下发
    DISPATCHED = "dispatched"  # 已下发给设备执行（对应云端 job running）
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMEOUT = "timeout"  # 词汇对齐云端 job 状态；Edge 调度器当前不主动产生


class WorkflowState(str, Enum):
    PENDING = "pending"  # 词汇对齐云端 workflow_task；Edge 提交即 running
    RUNNING = "running"
    WAITING_MATERIAL = "waiting_for_material"  # Edge 扩展：物料预留不足，等待补料后重试
    PAUSED = "paused"  # 词汇对齐云端；Edge 调度器当前不主动产生
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMEOUT = "timeout"  # 词汇对齐云端；超时判定由云端 Cron/调度器负责


@dataclass
class Handle:
    """workflow_handle_template 子集。

    传参解析只用 data_source / handle_key / data_key 三个字段。
    云端 ER 白板定稿：workflow_edge 以 handle **uuid** 引用连接点（规范路径）；
    handle_key 是模板内端口语义键（唯一性待云端确认），作为 payload 未带 uuid
    时的兼容寻址——此时需要 node_id 限定归属（同名 key 可能出现在多个节点）。
    """

    uuid: str = ""
    data_source: str = ""  # "executor" 表示取自父节点执行返回值
    handle_key: str = ""  # "ready" 的 handle 只表达顺序依赖，不传参
    data_key: str = ""  # gjson 取值路径；target 侧可含 "@@@" 分隔的嵌套键
    node_id: str = ""  # 该 handle 挂在哪个 workflow_node 上（key 寻址时必需）
    io_type: str = ""  # source / target（对齐 workflow_handle_template.io_type）


@dataclass
class WorkflowNode:
    """WorkflowNode 子集：Edge 执行一个设备动作所需的全部信息。"""

    id: str  # 节点 id（uuid 或云端 node_id 字符串化）
    # 已存在的工作流节点作业（WorkflowNodeJob）UUID；空值保持旧路径随机生成。
    job_id: str = ""
    device_id: str = ""  # 目标设备
    action_name: str = ""  # 设备动作名
    action_type: str = ""  # goal / goal_sequence 等
    param: Dict[str, Any] = field(
        default_factory=dict
    )  # action 参数（会被父节点传参覆写）
    # 任务创建时冻结的动作合同（Action Contract）；None 仅表示遗留直接调用。
    param_schema: dict[str, Any] | None = None
    # 与云端 workflow_node 类型枚举一致：Group / ILab / py_script / tool_call /
    # manual_confirm / Transfer（Edge 目前只执行 ILab；Transfer 仅规范化/透传，
    # 比较请用 is_ilab()，容忍大小写差异）
    node_type: str = "ILab"
    disabled: bool = False
    # 可选物料需求（向后兼容：空列表 = 无物料，行为与旧 workflow 完全一致）
    material_requirements: List[MaterialRequirement] = field(default_factory=list)
    # Action 成功后登记的输出物料声明。输出声明只描述期望的物料身份/内容和
    # 位置；实际 output UUID 可由设备返回值提供，或由库存服务幂等生成。
    material_outputs: List[Dict[str, Any]] = field(default_factory=list)
    # 物料位置/持有者前置条件与动作完成后的交接效果。
    material_preconditions: List[Dict[str, Any]] = field(default_factory=list)
    material_effects: List[Dict[str, Any]] = field(default_factory=list)
    # 执行实例归属。空值表示由 WorkflowSpec.run_id 在构图时补齐；一旦补齐后
    # 节点的整个生命周期（排队、派发、回调、历史）都必须携带同一个 run_id。
    run_id: str = ""

    @property
    def device_action_key(self) -> str:
        """与 ws_client 一致的设备动作锁 key。"""
        return f"/devices/{self.device_id}/{self.action_name}"

    @property
    def device_lock_key(self) -> str:
        """返回设备级内存准入互斥键。

        参数：无；设备身份来自当前工作流节点（Workflow Node）的 ``device_id``。
        返回：``/devices/{device_id}`` 形式的稳定进程内键，用于保证同一设备的
        不同动作不会并行派发。
        异常：不主动抛出异常；设备身份合法性仍由上游工作流合同校验。

        该键只是持久调度内核（Durable Scheduler Kernel）落地前的内存安全桥，
        不是持久作业执行占用（JobExecutionClaim），也不提供栅栏（Fence）。
        """

        return f"/devices/{self.device_id}"

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
    task_id: str = ""  # 云端 WorkflowTask uuid（可空，Edge 本地提交时等于 workflow_id）
    run_mode: str = "normal"  # normal / step / single_node
    # 一次可执行运行的稳定身份。旧调用未提供时默认沿用 workflow_id。
    run_id: str = ""

    def __post_init__(self) -> None:
        if not self.task_id:
            self.task_id = self.workflow_id
        if not self.run_id:
            self.run_id = self.workflow_id

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
    # 不能仅凭 workflow_id/node.id 推断归属；队列项显式携带执行实例身份。
    run_id: str = ""
    # 资源解阻动作（例如 TIP 盒补料）在通过设备/资源硬约束后，优先于所有
    # 普通 Workflow Priority；它不提供抢占能力，也不能绕过设备锁。
    is_resource_unblocking: bool = False

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = self.workflow_id


@dataclass
class SchedulableAction(ReadyTask):
    """已进入调度准入队列的 Action。

    ``ReadyTask`` 只表示 DAG 入度归零；本类型表示调度器已经确认该 Action
    属于一个具体 Run，并可以在本轮尝试设备/资源准入。队列键始终是
    ``(run_id, node.id)``，因此相同模板的多个 Run 不会共享队列状态。
    """

    resolved_params: Dict[str, Any] | None = None
    resource_lock_keys: tuple[str, ...] = ()


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
    # Job 与 Run 的显式关联；旧调用默认从 workflow_id 兼容推导。
    run_id: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = self.workflow_id


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
    """把 wire 节点对象转换为旧调度器工作流节点（WorkflowNode）。

    参数：``data`` 是整图或兼容桥输入的节点对象。返回规范化节点；其中
    ``job_id`` 若存在，表示派发必须复用已有工作流节点作业身份；
    ``param_schema`` 是已冻结动作合同（Action Contract）。异常：
    缺失必需节点身份时保留 ``KeyError``；``param_schema`` 非对象且
    非 ``None`` 时抛带中文诊断的 ``TypeError``。
    """

    # ``param_schema`` 隔离 wire 字典顶层，避免后续更换容器改变节点合同。
    raw_param_schema = data.get("param_schema")
    if raw_param_schema is not None and not isinstance(raw_param_schema, Mapping):
        raise TypeError("param_schema 必须是对象或 None")
    param_schema = dict(raw_param_schema) if raw_param_schema is not None else None
    return WorkflowNode(
        id=str(data["id"]),
        job_id=str(data.get("job_id", "") or ""),
        device_id=data.get("device_id", "") or "",
        action_name=data.get("action_name", "") or "",
        action_type=data.get("action_type", "") or "",
        param=dict(data.get("param") or {}),
        param_schema=param_schema,
        node_type=normalize_node_type(data.get("node_type") or data.get("type")),
        disabled=bool(data.get("disabled", False)),
        material_requirements=[
            MaterialRequirement.from_dict(r)
            for r in (data.get("material_requirements") or [])
        ],
        material_outputs=[
            dict(output)
            for output in (data.get("material_outputs") or [])
            if isinstance(output, Mapping)
        ],
        material_preconditions=[
            dict(item)
            for item in (data.get("material_preconditions") or [])
            if isinstance(item, Mapping)
        ],
        material_effects=[
            dict(item)
            for item in (data.get("material_effects") or [])
            if isinstance(item, Mapping)
        ],
        run_id=str(data.get("run_id", "") or ""),
    )


def edge_from_dict(data: Dict[str, Any]) -> WorkflowEdge:
    return WorkflowEdge(
        uuid=str(
            data.get("uuid", "")
            or f"{data['source_node_id']}->{data['target_node_id']}"
        ),
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
        run_mode=str(data.get("run_mode", "normal") or "normal"),
        run_id=str(data.get("run_id", "") or ""),
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
    "SchedulableAction",
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
