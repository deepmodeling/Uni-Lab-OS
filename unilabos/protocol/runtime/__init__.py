"""runtime.db 域的线上协议（统一版本 ``runtime.v1``）。

Edge 与 runtime（微后端/Backend）之间的通信只有一个协议版本
``runtime.v1``，内部按数据与业务分域——替换后端时数据源与控制源整体
切换，不做独立版本协商：

- ``data``：数据/执行边界（会话、endpoint 快照、Job 生命周期、
  命令与事件队列），后端、微后端与执行 adapter 共用；
- ``control``：业务控制面（命令下发通知、命令正文、事件回收），
  Backend/Edge 的轻通知与 HTTP 权威文档；
- ``lifecycle``：状态、排空、重启与重置的管理协议；一个 backend 对应一个主 runtime；
- ``workflow``：节点/边写入 DTO、JSON 值约束与 UUID 规范化；
- ``loop``：循环容器节点（``type="loop"``）的参数契约、条件求值与迭代变量；
- ``registry``：Registry Authority 条目状态、挂起冲突、上报批次统计，以及
  按内容哈希增量上报的 digest / report 形状
  （与 edge-ui ``@openlab/protocol`` registry 域同名字段对齐）。
"""

from unilabos.protocol.runtime.control import (
    BackendCommandDocument,
    BackendCommandNotice,
    BackendHttpRequest,
    BackendSessionNotice,
    CancelJobContent,
    CommandType,
    EdgeChangeAck,
    EdgeChangeNotice,
    EdgeHttpResponse,
    ErrorDecisionContent,
    ExecuteJobContent,
    PingNotice,
    PongNotice,
)
from unilabos.protocol.runtime.data import (
    RUNTIME_PROTOCOL_VERSION,
    AdapterCommandAck,
    AdapterCommandClaim,
    AdapterCommandEnqueue,
    BackendEventAck,
    BackendEventClaim,
    BackendEventEnqueue,
    BackendSessionUpsert,
    CommandEnvelope,
    CommandReceipt,
    EndpointSnapshotResult,
    EndpointSnapshotUpsert,
    ErrorGateDecision,
    ErrorGateOpen,
    ErrorGateResume,
    ExecutionJobCancel,
    ExecutionJobCreate,
    ExecutionJobFeedback,
    ExecutionJobTransition,
)
from unilabos.protocol.runtime.loop import (
    LOOP_ITERATION_TRIGGER,
    LOOP_NODE_TYPE,
    LoopCondition,
    LoopSpec,
    parse_loop_spec,
    substitute_loop_placeholders,
)
from unilabos.protocol.runtime.registry import (
    REGISTRY_PROTOCOL_VERSION,
    RegistryAffectedNode,
    RegistryConflict,
    RegistryConflictReason,
    RegistryDigest,
    RegistryEntryStatus,
    RegistryEntrySummary,
    RegistryPendingImpact,
    RegistryPendingItem,
    RegistryReport,
    RegistryReportCounts,
    RegistryReportEntry,
    RegistryReportResult,
    RegistryReportSummary,
    RegistryTemplateIdentity,
    RegistryUnusableItem,
)
from unilabos.protocol.runtime.workflow import (
    CandidateChangeset,
    CandidateCompilation,
    CandidateDiagnostic,
    CandidateSourceMapEntry,
    DiagnosticSourceRange,
    JsonArray,
    JsonObject,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    normalize_json_array,
    normalize_json_object,
    validate_json_value,
    validate_uuid,
)

__all__ = [
    # data（runtime.v1 数据/执行边界）
    "AdapterCommandAck",
    "AdapterCommandClaim",
    "AdapterCommandEnqueue",
    "BackendEventAck",
    "BackendEventClaim",
    "BackendEventEnqueue",
    "BackendSessionUpsert",
    "CommandEnvelope",
    "CommandReceipt",
    "EndpointSnapshotResult",
    "EndpointSnapshotUpsert",
    "ErrorGateDecision",
    "ErrorGateOpen",
    "ErrorGateResume",
    "ExecutionJobCancel",
    "ExecutionJobCreate",
    "ExecutionJobFeedback",
    "ExecutionJobTransition",
    "RUNTIME_PROTOCOL_VERSION",
    # control（runtime.v1 业务控制面）
    "BackendCommandDocument",
    "BackendCommandNotice",
    "BackendHttpRequest",
    "BackendSessionNotice",
    "CancelJobContent",
    "CommandType",
    "EdgeChangeAck",
    "EdgeChangeNotice",
    "EdgeHttpResponse",
    "ErrorDecisionContent",
    "ExecuteJobContent",
    "PingNotice",
    "PongNotice",
    # workflow
    "CandidateChangeset",
    "CandidateCompilation",
    "CandidateDiagnostic",
    "CandidateSourceMapEntry",
    "DiagnosticSourceRange",
    "JsonArray",
    "JsonObject",
    "WorkflowEdgeWrite",
    "WorkflowNodeWrite",
    "normalize_json_array",
    "normalize_json_object",
    "validate_json_value",
    "validate_uuid",
    # loop
    "LOOP_ITERATION_TRIGGER",
    "LOOP_NODE_TYPE",
    "LoopCondition",
    "LoopSpec",
    "parse_loop_spec",
    "substitute_loop_placeholders",
    # registry
    "REGISTRY_PROTOCOL_VERSION",
    "RegistryAffectedNode",
    "RegistryConflict",
    "RegistryConflictReason",
    "RegistryDigest",
    "RegistryEntryStatus",
    "RegistryEntrySummary",
    "RegistryPendingImpact",
    "RegistryPendingItem",
    "RegistryReport",
    "RegistryReportCounts",
    "RegistryReportEntry",
    "RegistryReportResult",
    "RegistryReportSummary",
    "RegistryTemplateIdentity",
    "RegistryUnusableItem",
]
