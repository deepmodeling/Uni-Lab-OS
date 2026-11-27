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
from unilabos.workflow.resource_lock_key import device_lock_key

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
# - workflow_task.status: pending/running/paused/canceling/success/failed/canceled/timeout
# - workflow_node_job.status 终态同样使用 success（不是 succeeded）
# Edge 额外扩展：ready/dispatched（节点内部推进态）、waiting_for_material（等料）、
# interrupted（进程重启标记，仅历史库）；上报云端时终态词汇与云端枚举一字不差。
class NodeState(str, Enum):
    ERROR_WAITING = "error_waiting"  # 旧 attempt 保留错误，当前节点等待人工决定
    PENDING = "pending"  # 尚有未完成的前置依赖
    READY = "ready"  # 依赖已清零，等待排序/下发
    DISPATCHED = "dispatched"  # 已下发给设备执行（对应云端 job running）
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMEOUT = "timeout"  # 词汇对齐云端 job 状态；Edge 调度器当前不主动产生


class WorkflowState(str, Enum):
    PENDING = "pending"  # 尚未进入执行，例如在串行队列中等待库存准入
    RUNNING = "running"
    WAITING_MATERIAL = "waiting_for_material"  # Edge 扩展：物料预留不足，等待补料后重试
    PAUSED = "paused"  # 词汇对齐云端；Edge 调度器当前不主动产生
    CANCELING = "canceling"  # 已停止后续派发，等待在途设备作业明确终态
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
    # 执行计划中冻结的值类型。时间规划只对明确的 ResourceSlot 透传做静态
    # 解析；缺省值保持旧工作流兼容，并按动态执行结果处理。
    value_type: str = ""


@dataclass
class WorkflowNode:
    """WorkflowNode 子集：Edge 执行一个设备动作所需的全部信息。"""

    id: str  # 节点 id（uuid 或云端 node_id 字符串化）
    # Python 作者源码中的结果变量名；条件表达式只通过该稳定计划字段读前序结果。
    result_name: str = ""
    # 已存在的工作流节点作业（WorkflowNodeJob）UUID；空值保持旧路径随机生成。
    job_id: str = ""
    device_id: str = ""  # 目标设备
    # 设备作为工站资源图 Material 的稳定身份；缺省时兼容旧本地业务 ID。
    device_material_uuid: str = ""
    # 动态设备选择器只冻结类型身份，运行时由调度器选择在线实例。
    device_selector: Dict[str, Any] = field(default_factory=dict)
    action_name: str = ""  # 设备动作名
    action_type: str = ""  # goal / goal_sequence 等
    param: Dict[str, Any] = field(
        default_factory=dict
    )  # action 参数（会被父节点传参覆写）
    # 任务创建时冻结的动作合同（Action Contract）；None 仅表示遗留直接调用。
    param_schema: dict[str, Any] | None = None
    # 冻结计划中的执行责任；物料转移仍由设备动作通道执行，用于物料事实结算，
    # 不创建由 PLC 负责的机械碰撞区域软件锁。
    executor_kind: str = "device_action"
    # 冻结的节点执行策略；调度阶段禁止回读可变工作流图补齐。
    execution_policy: Dict[str, Any] = field(default_factory=dict)
    # AST 从动作声明编译出的资源语义；工作流实例不得覆盖设备托管或转运角色。
    action_resource_contract: Dict[str, Any] = field(default_factory=dict)
    # 由编译器沿物料数据边推导，不能由动作参数或外部调度请求直接声明。
    runtime_material_parameters: List[str] = field(default_factory=list)
    # 与云端 workflow_node 类型枚举一致：Group / ILab / py_script / tool_call /
    # manual_confirm / Transfer（Edge 目前只执行 ILab；Transfer 仅规范化/透传，
    # 比较请用 is_ilab()，容忍大小写差异）
    node_type: str = "ILab"
    # 人工确认是设备动作的人工批准阶段；配置与真实动作参数严格分离。
    manual_confirmation: Dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    # 注册表动作声明的免设备排队语义；仍受物料/库位执行资源键约束。
    always_free: bool = False
    # 可选物料需求（向后兼容：空列表 = 无物料，行为与旧 workflow 完全一致）
    material_requirements: List[MaterialRequirement] = field(default_factory=list)
    # 目标 Handle UUID 到祖先 RepeatUntil carry 键的冻结绑定；每轮物化时解析。
    carry_bindings: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # 资源计划中该节点覆盖的占用区间和新增集合；空值保持旧计划兼容。
    resource_plan_id: str = ""
    resource_interval_ids: List[str] = field(default_factory=list)
    resource_acquire_set_id: str = ""
    # 已由工作流合同证明的同节点 ResourceSlot 输入/输出透传：
    # output_handle_uuid -> input_handle_uuid。它只用于时间规划快照，真实派发
    # 仍以设备返回值和执行前校验为准。
    material_passthrough_handles: Dict[str, str] = field(default_factory=dict)

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

        return device_lock_key(self.device_material_uuid or self.device_id)

    def is_ilab(self) -> bool:
        return normalize_node_type(self.node_type) == "ILab"

    def is_manual_confirm(self) -> bool:
        return normalize_node_type(self.node_type) == "manual_confirm"

    def is_material_io(self) -> bool:
        """返回节点是否由调度器本地执行 Material I/O。"""

        return self.executor_kind == "material_io"

    def waits_for_manual_confirmation(self) -> bool:
        """返回节点是否必须先持有资源再等待人工批准。"""

        return self.is_manual_confirm() or self.is_material_io()


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
    # 恢复时以上一次 workflow span 为父上下文，使 Trace ID 跨进程重启稳定。
    trace_context: Dict[str, str] = field(default_factory=dict)
    repeat_regions: Dict[str, "RepeatUntilRegion"] = field(default_factory=dict)
    # 已绑定的资源计划 JSON 投影；旧工作流为空，不改变旧调度数据模型。
    resource_plan: Dict[str, Any] | None = None
    # 不进入旧调度 DAG、但资源计划仍保留图身份的协调器节点。该内部投影只由
    # WorkflowSpecCompiler 写入，spec_from_dict 不接收；区间完成判断只能跳过
    # 这个显式集合，不能把任意缺失物理节点都推断成成功。
    resource_coordinator_node_ids: List[str] = field(default_factory=list)

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
class RepeatUntilRegion:
    """一个 RepeatUntil 控制节点拥有的冻结循环体模板。"""

    control_node_id: str
    nodes: List[WorkflowNode]
    edges: List[WorkflowEdge] = field(default_factory=list)
    handles: List[Handle] = field(default_factory=list)
    # 只保存本区域直接拥有的嵌套循环；每次物化父轮次时同步改写其控制身份。
    repeat_regions: Dict[str, "RepeatUntilRegion"] = field(default_factory=dict)
    # 跨区域值边：区域外节点输出 → 循环体动作参数，按模板节点身份索引。
    # 顶层 DAG 只保留顶层端点、区域模板只保留内部边，这类边在两边都不会出现；
    # 物化每一轮时用外层已完成结果把值注入模板参数（与 carry 同一入口）。
    # 每项为 {"source_node_id", "source_data_key", "target_data_key"}；嵌套循环
    # 克隆时来源若属于父轮次则改写为运行身份，否则预先解析成 {"literal": 值}。
    inbound_bindings: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)


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
    device_material_uuid: str = ""
    action_name: str = ""
    # 人工确认节点在批准前保存已解析参数；批准后用同一 Job 身份下发真实动作。
    resolved_args: Dict[str, Any] = field(default_factory=dict)
    # 派发意图越过 Gate 8 时冻结的七类凭据；人工确认继续真实动作时必须原样复用。
    dispatch_credentials: Dict[str, Any] = field(default_factory=dict)
    # 人工等待与批准后的设备动作始终复用同一组 Claim/Fence 与资源占用。
    operator_released_keys: set[str] = field(default_factory=set)
    resource_lock_keys: set[str] = field(default_factory=set)
    # 连续作用域中的 reservation 可由同 Task 兄弟共享；本集合只表示当前
    # Job 正在操作的物料/设备，始终全局互斥并随 Job 终态立即释放。
    active_resource_lock_keys: set[str] = field(default_factory=set)
    # 绑定资源计划的只读运行投影；旧恢复记录为空时仍走兼容 lock key。
    resource_plan_id: str = ""
    resource_interval_ids: List[str] = field(default_factory=list)
    resource_acquire_set_id: str = ""
    manual_confirmation_approved: bool = False
    manual_action_dispatched: bool = False
    # 下发时刻的预估执行时长（泳道图预估终点）与来源（declared/historical/default）
    estimated_s: float = 0.0
    estimate_source: str = "default"
    # 只有本进程亲自记录的首次派发起点能用于完整耗时样本；恢复推断不算观测。
    timing_origin_observed: bool = False


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
    raw_execution_policy = data.get("execution_policy") or {}
    if not isinstance(raw_execution_policy, Mapping):
        raise TypeError("execution_policy 必须是对象")
    raw_device_selector = data.get("device_selector") or {}
    if not isinstance(raw_device_selector, Mapping):
        raise TypeError("device_selector 必须是对象")
    raw_resource_contract = data.get("action_resource_contract") or {}
    if not isinstance(raw_resource_contract, Mapping):
        raise TypeError("action_resource_contract 必须是对象")
    # 新执行计划通常将合同元数据放在 meta_data.unilab；保留顶层字段兼容
    # 直接调度输入和旧快照。两种位置同时存在时顶层字段优先，避免旧嵌套值
    # 意外覆盖调用方明确提供的运行快照。
    raw_passthrough = data.get("material_passthrough_handles")
    if raw_passthrough is None:
        raw_metadata = data.get("meta_data") or {}
        raw_unilab = (
            raw_metadata.get("unilab")
            if isinstance(raw_metadata, Mapping)
            and isinstance(raw_metadata.get("unilab"), Mapping)
            else {}
        )
        raw_passthrough = raw_unilab.get("material_passthrough_handles", {})
    if not isinstance(raw_passthrough, Mapping):
        raise TypeError("material_passthrough_handles 必须是对象")
    raw_interval_ids = data.get("resource_interval_ids") or []
    if not isinstance(raw_interval_ids, list) or any(
        not isinstance(value, str) or not value.strip() for value in raw_interval_ids
    ):
        raise TypeError("resource_interval_ids 必须是字符串数组")
    return WorkflowNode(
        id=str(data["id"]),
        result_name=str(data.get("result_name") or ""),
        job_id=str(data.get("job_id", "") or ""),
        device_id=data.get("device_id", "") or "",
        device_material_uuid=str(data.get("device_material_uuid") or ""),
        device_selector=dict(raw_device_selector),
        action_name=data.get("action_name", "") or "",
        action_type=data.get("action_type", "") or "",
        param=dict(data.get("param") or {}),
        param_schema=param_schema,
        executor_kind=str(data.get("executor_kind") or "device_action").strip(),
        execution_policy=dict(raw_execution_policy),
        action_resource_contract=dict(raw_resource_contract),
        node_type=normalize_node_type(data.get("node_type") or data.get("type")),
        manual_confirmation=dict(data.get("manual_confirmation") or {}),
        disabled=bool(data.get("disabled", False)),
        always_free=bool(data.get("always_free", False)),
        material_requirements=[
            MaterialRequirement.from_dict(r)
            for r in (data.get("material_requirements") or [])
        ],
        carry_bindings={
            str(handle_uuid): {
                "control_region_uuid": str(binding.get("control_region_uuid") or ""),
                "key": str(binding.get("key") or ""),
            }
            for handle_uuid, binding in (data.get("carry_bindings") or {}).items()
            if isinstance(binding, Mapping)
        },
        resource_plan_id=str(data.get("resource_plan_id") or ""),
        resource_interval_ids=[str(value) for value in raw_interval_ids],
        resource_acquire_set_id=str(data.get("resource_acquire_set_id") or ""),
        material_passthrough_handles={
            str(output_uuid): str(input_uuid)
            for output_uuid, input_uuid in raw_passthrough.items()
            if str(output_uuid).strip() and str(input_uuid).strip()
        },
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
        value_type=str(data.get("value_type", data.get("type", "")) or ""),
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
        resource_plan=(
            dict(data["resource_plan"])
            if isinstance(data.get("resource_plan"), Mapping)
            else None
        ),
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
    "RepeatUntilRegion",
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
