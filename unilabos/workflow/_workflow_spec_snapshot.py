"""工作流规格（WorkflowSpec）编译边界的纯身份校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

WORKFLOW_SPEC_COMPILATION_CODES = frozenset(
    {
        "duplicate_handle_identity",
        "duplicate_job_identity",
        "duplicate_node_identity",
        "edge_handle_identity_mismatch",
        "edge_node_identity_mismatch",
        "invalid_action_contract",
        "invalid_edge_identity",
        "invalid_execution_plan",
        "invalid_executor_binding",
        "invalid_handle_identity",
        "invalid_job_identity",
        "invalid_job_param",
        "invalid_job_snapshot",
        "invalid_node_identity",
        "invalid_task_identity",
        "invalid_task_snapshot",
        "invalid_workflow_snapshot",
        "job_node_identity_mismatch",
        "missing_workflow_node_job",
        "unsupported_executor_kind",
    }
)


class WorkflowSpecCompilationError(RuntimeError):
    """冻结任务不能安全转换为调度器（Scheduler）输入。"""

    def __init__(self, code: str, message: str) -> None:
        """建立属于显式闭集的编译错误。

        参数：``code`` 是上层稳定映射的错误码，``message`` 是中文诊断。返回：
        无。异常：未知错误码抛 ``ValueError``，禁止协议意外扩张。
        """

        if code not in WORKFLOW_SPEC_COMPILATION_CODES:
            raise ValueError(f"unknown workflow spec compilation code: {code}")
        super().__init__(message)
        self.code = code


def mapping(value: Any, code: str, field: str) -> Mapping[str, Any]:
    """要求边界值是对象。

    参数：``value`` 是待校验值，``code`` 是闭集失败码，``field`` 是诊断路径。
    返回：原映射。异常：类型不符时抛 ``WorkflowSpecCompilationError``。
    """

    if not isinstance(value, Mapping):
        raise WorkflowSpecCompilationError(code, f"{field} 必须是对象")
    return value


def mapping_sequence(value: Any, code: str, field: str) -> list[Mapping[str, Any]]:
    """要求边界值是只含对象的有序序列。

    参数：``value`` 是待校验值，``code`` 是闭集失败码，``field`` 是诊断路径。
    返回：隔离后的映射列表。异常：序列或成员类型不符时抛编译错误。
    """

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise WorkflowSpecCompilationError(code, f"{field} 必须是对象数组")
    return [
        mapping(item, code, f"{field}[{index}]") for index, item in enumerate(value)
    ]


def canonical_uuid(value: Any, code: str, field: str) -> str:
    """校验稳定身份并返回规范小写连字符 UUID。

    参数：``value`` 是身份边界值，``code`` 是失败码，``field`` 是诊断路径。
    返回：规范 UUID 文本。异常：空值、非法或非规范拼写时抛出编译错误。
    """

    text = str(value or "").strip()
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError, TypeError) as exc:
        raise WorkflowSpecCompilationError(code, f"{field} 必须是规范 UUID") from exc
    canonical = str(parsed)
    if parsed.int == 0 or text != canonical:
        raise WorkflowSpecCompilationError(code, f"{field} 必须是规范 UUID")
    return canonical


def index_objects(
    raw_objects: Sequence[Mapping[str, Any]],
    *,
    identity_code: str,
    duplicate_code: str,
    field: str,
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    """按稳定 UUID 索引执行计划（ExecutionPlan）对象。

    参数：``raw_objects`` 是对象序列，两个错误码分别表示非法/重复身份，
    ``field`` 是诊断路径。返回：身份索引和原有确定性顺序。异常：非法或重复
    身份时抛编译错误，不允许后项覆盖前项。
    """

    indexed: dict[str, Mapping[str, Any]] = {}
    ordered: list[str] = []
    for index, item in enumerate(raw_objects):
        identity = canonical_uuid(
            item.get("uuid"), identity_code, f"{field}[{index}].uuid"
        )
        if identity in indexed:
            raise WorkflowSpecCompilationError(
                duplicate_code, f"{field} 含重复身份：{identity}"
            )
        indexed[identity] = item
        ordered.append(identity)
    return indexed, ordered


def index_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    nodes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """按计划节点索引既有工作流节点作业（WorkflowNodeJob）。

    参数：``jobs`` 是持久作业序列，``nodes`` 是计划节点索引。返回：节点至唯一
    作业映射。异常：结构、身份、引用或重复绑定非法时抛编译错误。
    """

    raw_jobs = mapping_sequence(jobs, "invalid_job_snapshot", "jobs")
    jobs_by_node: dict[str, Mapping[str, Any]] = {}
    seen_job_uuids: set[str] = set()
    for index, job in enumerate(raw_jobs):
        # ``job_uuid``/``node_uuid`` 共同证明持久执行责任的稳定归属。
        job_uuid = canonical_uuid(
            job.get("uuid"), "invalid_job_identity", f"jobs[{index}].uuid"
        )
        node_uuid = canonical_uuid(
            job.get("workflow_node_uuid"),
            "invalid_job_identity",
            f"jobs[{index}].workflow_node_uuid",
        )
        if job_uuid in seen_job_uuids or node_uuid in jobs_by_node:
            raise WorkflowSpecCompilationError(
                "duplicate_job_identity", f"作业身份或计划节点绑定重复：{job_uuid}"
            )
        if node_uuid not in nodes:
            raise WorkflowSpecCompilationError(
                "job_node_identity_mismatch", f"作业引用计划外节点：{node_uuid}"
            )
        seen_job_uuids.add(job_uuid)
        jobs_by_node[node_uuid] = job
    return jobs_by_node


__all__ = [
    "WORKFLOW_SPEC_COMPILATION_CODES",
    "WorkflowSpecCompilationError",
    "canonical_uuid",
    "index_jobs",
    "index_objects",
    "mapping",
    "mapping_sequence",
]
