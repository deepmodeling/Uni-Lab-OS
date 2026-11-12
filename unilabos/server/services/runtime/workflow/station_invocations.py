"""V1 工站调用（``POST /api/v1/station/workflow-invocations``）：上游 Backend 按发布修订或工作流名称提交任务。

V1 合同（``workflow/station_workflow_submission.py`` + ``WorkflowService.submit_station_workflow``）：

- 两种调用形状二选一：规范调用 ``task_uuid + workflow_id + revision_fingerprint + normalized_input``，
  兼容调用 ``backend_task_uuid + workflow_name + input``；只携带入口参数，不接受 DAG 中间节点参数；
- ``(backend_task_uuid, invocation_key)`` 标识一次上游调用：同请求重放返回同一任务，同键不同请求按冲突拒绝；
- 名称调用只解析唯一的当前工作流（不存在 3002，不唯一 3003）；规范调用的发布修订指纹不存在或不匹配为
  ``workflow_revision_conflict``（3003）。

dev 落地：任务经 ``WorkflowService.create_workflow_task`` 的幂等键建立（键由调用身份派生，长度受控），
上游身份与 V1 请求指纹记在任务 ``meta_data.station_invocation``。dev 按工作流当前图编排、不另存发布冻结图，
所以规范调用还要求工作流当前修订仍是该发布修订，否则同样按 ``workflow_revision_conflict`` 拒绝。
V1 的浮点优先级大于默认 1.0 时映射为 dev 的 ``high``，原值保留在 ``station_invocation.priority``。
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from unilabos.protocol.runtime.workflow import validate_uuid
from unilabos.protocol.utils.json_codec import encode_json

_REVISION_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
#: V1 工站调用的缺省优先级；大于它的调用按 dev 的高优先级任务处理
DEFAULT_STATION_PRIORITY = 1.0


def _normalize_deadline(value: Optional[str]) -> Optional[str]:
    """可选 RFC3339 截止时间规范为 UTC；无时区或非法值拒绝（V1 同口径）。"""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError("deadline 不能为空")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("deadline 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def station_idempotency_key(backend_task_uuid: str, invocation_key: str) -> str:
    """上游调用身份 → dev 任务幂等键（定长，不受 invocation_key 长度影响）。"""

    digest = hashlib.sha256(f"{backend_task_uuid}\n{invocation_key}".encode("utf-8")).hexdigest()
    return f"station:{digest}"


def _request_fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(encode_json(dict(payload), sort_keys=True)).hexdigest()


def _resolve_by_name(service: Any, workflow_name: str) -> str:
    from unilabos.server.services.runtime.workflow.service import WorkflowConflict, WorkflowError

    matches: List[Dict[str, Any]] = []
    page = 1
    while True:
        result = service.list_workflows(page=page, page_size=100, name=workflow_name)
        matches.extend(item for item in result["items"] if item.get("name") == workflow_name)
        if page * int(result["page_size"]) >= int(result["total"]):
            break
        page += 1
    if not matches:
        raise WorkflowError("workflow_not_found")
    if len(matches) != 1:
        raise WorkflowConflict("conflict", detail="工站工作流名称不唯一，无法安全选择运行定义")
    return str(matches[0]["uuid"])


def _resolve_by_publication(service: Any, workflow_id: str, revision_fingerprint: str) -> int:
    """发布修订指纹 → 工作流修订；当前图必须仍是该修订（dev 不另存发布冻结图）。"""

    from unilabos.server.services.runtime.workflow.service import WorkflowConflict

    publications = service.list_workflow_publications(workflow_id)
    match = next((item for item in publications if item.get("source_hash") == revision_fingerprint), None)
    if match is None:
        raise WorkflowConflict("workflow_revision_conflict", detail="请求的工作流发布修订不存在或指纹不匹配")
    revision = int(match["revision"])
    if int(service.get_workflow(workflow_id)["revision"]) != revision:
        raise WorkflowConflict("workflow_revision_conflict", detail="工作流已在该发布修订之后被修改，请重新发布后调用")
    return revision


def submit_station_workflow(
    service: Any,
    *,
    backend_task_uuid: str,
    invocation_key: str,
    workflow_name: Optional[str],
    workflow_id: Optional[str],
    revision_fingerprint: Optional[str],
    input_value: Mapping[str, Any],
    priority: float,
    deadline: Optional[str],
    inventory_bindings: List[Dict[str, Any]],
    description: Optional[str],
    meta_data: Mapping[str, Any],
) -> Dict[str, Any]:
    """幂等创建一次工站工作流调用，返回任务投影（首次与重放相同）。"""

    from unilabos.server.services.runtime.workflow.service import WorkflowConflict, WorkflowError

    try:
        backend_task_uuid = validate_uuid(backend_task_uuid)
        invocation_key = str(invocation_key or "").strip()
        workflow_name = str(workflow_name or "").strip() or None
        workflow_id = validate_uuid(workflow_id) if workflow_id is not None else None
        revision_fingerprint = str(revision_fingerprint).strip() if revision_fingerprint is not None else None
        normalized_priority = float(priority)
        normalized_deadline = _normalize_deadline(deadline)
        description = (description or "").strip() or None
        if (
            not invocation_key or len(invocation_key) > 256
            or (workflow_name is not None and len(workflow_name) > 256)
            or not math.isfinite(normalized_priority)
            or (workflow_id is None and workflow_name is None)
            or ((workflow_id is None) != (revision_fingerprint is None))
            or (revision_fingerprint is not None and _REVISION_FINGERPRINT.fullmatch(revision_fingerprint) is None)
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise WorkflowError("invalid_input", detail="工站工作流调用参数非法") from None
    bindings = [dict(item) for item in inventory_bindings]
    if workflow_id is None:
        fingerprint_payload: Dict[str, Any] = {
            "backend_task_uuid": backend_task_uuid, "invocation_key": invocation_key,
            "workflow_name": workflow_name, "input": dict(input_value), "priority": normalized_priority,
            "inventory_bindings": bindings, "description": description, "meta_data": dict(meta_data),
        }
    else:
        fingerprint_payload = {
            "task_uuid": backend_task_uuid, "invocation_key": invocation_key, "workflow_id": workflow_id,
            "revision_fingerprint": revision_fingerprint, "normalized_input": dict(input_value),
            "priority": normalized_priority, "deadline": normalized_deadline, "inventory_bindings": bindings,
            "description": description, "meta_data": dict(meta_data),
        }
    fingerprint = _request_fingerprint(fingerprint_payload)
    key = station_idempotency_key(backend_task_uuid, invocation_key)
    existing = service.find_task_by_idempotency_key("workflow", key)
    if existing is not None:
        recorded = ((existing.get("meta_data") or {}).get("station_invocation") or {}).get("request_fingerprint")
        if recorded != fingerprint:
            raise WorkflowConflict("conflict", detail="同一工站调用键已用于不同的请求")
        return existing

    expected_revision: Optional[int] = None
    if workflow_id is not None:
        expected_revision = _resolve_by_publication(service, workflow_id, revision_fingerprint or "")
        workflow_uuid = workflow_id
    else:
        workflow_uuid = _resolve_by_name(service, workflow_name or "")
    station = {
        "backend_task_uuid": backend_task_uuid,
        "invocation_key": invocation_key,
        "workflow_name": workflow_name,
        "workflow_id": workflow_id,
        "revision_fingerprint": revision_fingerprint,
        "priority": normalized_priority,
        "deadline": normalized_deadline,
        "request_fingerprint": fingerprint,
    }
    return service.create_workflow_task(
        workflow_uuid=workflow_uuid,
        run_mode="normal",
        target_node_uuid=None,
        input_value=dict(input_value),
        description=description,
        meta_data={**dict(meta_data), "station_invocation": station},
        idempotency_key=key,
        expected_revision=expected_revision,
        priority="high" if normalized_priority > DEFAULT_STATION_PRIORITY else "normal",
        inventory_bindings=bindings,
    )


__all__ = ["DEFAULT_STATION_PRIORITY", "station_idempotency_key", "submit_station_workflow"]
