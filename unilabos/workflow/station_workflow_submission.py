"""Backend 工站调用的规范化请求与稳定幂等身份。"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.models import WorkflowTaskPriority, normalize_json_object, validate_uuid


class StationWorkflowSubmissionInvalid(ValueError):
    """工站调用在进入工作流服务编排前未通过边界校验。"""


@dataclass(frozen=True, slots=True)
class StationWorkflowSubmission:
    """一次规范且可确定性重放的 Backend 工站工作流调用。"""

    backend_task_uuid: str
    invocation_key: str
    workflow_name: str | None
    workflow_id: str | None
    revision_fingerprint: str | None
    input_value: dict[str, Any]
    priority: str | float
    deadline: str | None
    inventory_bindings: tuple[dict[str, Any], ...]
    description: str | None
    meta_data: dict[str, Any]
    request_fingerprint: str


_REVISION_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


def prepare_station_workflow_submission(
    *,
    backend_task_uuid: str,
    invocation_key: str,
    workflow_name: str | None,
    input_value: dict[str, Any],
    priority: WorkflowTaskPriority | str | float,
    inventory_bindings: list[dict[str, Any]] | None,
    description: str | None,
    meta_data: dict[str, Any] | None,
    workflow_id: str | None = None,
    revision_fingerprint: str | None = None,
    deadline: str | None = None,
) -> StationWorkflowSubmission:
    """校验工站调用并生成包含冻结发布修订的稳定请求指纹。

    参数：Backend Task UUID 与调用键标识一次上游调用；工作流名称、入口参数、
    优先级（normal/high 或兼容的有限数值）、库存绑定和公开元数据构成完整请求事实。
    枚举按字符串参与指纹，显式数值沿用原指纹算法。返回：不可变规范命令。异常：
    UUID、文本、JSON 对象或有限优先级非法时抛
    ``StationWorkflowSubmissionInvalid``，不访问数据库且不产生 Task/Job。
    """

    try:
        normalized_backend_task_uuid = validate_uuid(backend_task_uuid)
        normalized_invocation_key = str(invocation_key or "").strip()
        normalized_workflow_name = str(workflow_name or "").strip() or None
        normalized_workflow_id = (
            validate_uuid(workflow_id) if workflow_id is not None else None
        )
        normalized_revision_fingerprint = (
            str(revision_fingerprint).strip()
            if revision_fingerprint is not None
            else None
        )
        normalized_input = normalize_json_object(input_value)
        normalized_meta_data = normalize_json_object(meta_data or {})
        normalized_bindings = tuple(
            normalize_json_object(binding) for binding in (inventory_bindings or [])
        )
        try:
            normalized_priority: str | float = WorkflowTaskPriority(priority).value
        except (TypeError, ValueError):
            normalized_priority = float(priority)
        normalized_description = (
            description.strip() if description is not None else None
        )
        normalized_description = normalized_description or None
        normalized_deadline = _normalize_deadline(deadline)
        if (
            not normalized_invocation_key
            or len(normalized_invocation_key) > 256
            or (
                normalized_workflow_name is not None
                and len(normalized_workflow_name) > 256
            )
            or (
                not isinstance(normalized_priority, str)
                and not math.isfinite(normalized_priority)
            )
            or (
                normalized_workflow_id is None
                and normalized_workflow_name is None
            )
            or (
                (normalized_workflow_id is None)
                != (normalized_revision_fingerprint is None)
            )
            or (
                normalized_revision_fingerprint is not None
                and _REVISION_FINGERPRINT.fullmatch(
                    normalized_revision_fingerprint
                )
                is None
            )
        ):
            raise ValueError
    except (AttributeError, TypeError, ValueError) as error:
        raise StationWorkflowSubmissionInvalid("工站工作流调用参数非法") from error

    if normalized_workflow_id is None:
        # 旧版名称调用的指纹载荷必须保持逐字节稳定，避免升级后把已持久化调用
        # 的合法网络重放误判为同键异参冲突。
        payload = {
            "backend_task_uuid": normalized_backend_task_uuid,
            "invocation_key": normalized_invocation_key,
            "workflow_name": normalized_workflow_name,
            "input": normalized_input,
            "priority": normalized_priority,
            "inventory_bindings": list(normalized_bindings),
            "description": normalized_description,
            "meta_data": normalized_meta_data,
        }
    else:
        payload = {
            "task_uuid": normalized_backend_task_uuid,
            "invocation_key": normalized_invocation_key,
            "workflow_id": normalized_workflow_id,
            "revision_fingerprint": normalized_revision_fingerprint,
            "normalized_input": normalized_input,
            "priority": normalized_priority,
            "deadline": normalized_deadline,
            "inventory_bindings": list(normalized_bindings),
            "description": normalized_description,
            "meta_data": normalized_meta_data,
        }
    fingerprint = hashlib.sha256(encode_json(payload, sort_keys=True)).hexdigest()
    return StationWorkflowSubmission(
        backend_task_uuid=normalized_backend_task_uuid,
        invocation_key=normalized_invocation_key,
        workflow_name=normalized_workflow_name,
        workflow_id=normalized_workflow_id,
        revision_fingerprint=normalized_revision_fingerprint,
        input_value=normalized_input,
        priority=normalized_priority,
        deadline=normalized_deadline,
        inventory_bindings=normalized_bindings,
        description=normalized_description,
        meta_data=normalized_meta_data,
        request_fingerprint=fingerprint,
    )


def _normalize_deadline(value: str | None) -> str | None:
    """把可选 RFC3339 截止时间规范为 UTC；无时区或非法值关闭式拒绝。"""

    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("deadline 不能为空")
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("deadline 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "StationWorkflowSubmission",
    "StationWorkflowSubmissionInvalid",
    "prepare_station_workflow_submission",
]
