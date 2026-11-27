"""手动动作核对的纯证据投影，供完整与紧凑 Job 读取共享。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

MANUAL_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "canceled"})
MANUAL_REVIEW_BASIS_FIELDS = (
    "uuid", "edge_command_uuid", "attempt", "status", "start_state",
    "uncertainty_reason", "return_info", "error_info", "feedback_data",
    "feedback_sequence", "started_at", "finished_at", "cancel_command_uuid",
    "cancel_accepted_at",
)
MANUAL_REVIEW_JSON_FIELDS = frozenset({"return_info", "error_info", "feedback_data", "control_data"})


def manual_review_projection(job: Mapping[str, Any]) -> dict[str, Any]:
    """核对只绑定真实手动通道；保留原始 hash 算法以兼容已有核对审计。"""
    if job.get("execution_source") != "error_manual":
        return {}
    control = job.get("control_data") or {}
    basis = {field: job.get(field) for field in MANUAL_REVIEW_BASIS_FIELDS}
    basis["control_data"] = {key: value for key, value in control.items() if key != "manual_review"}
    version = hashlib.sha256(json.dumps(
        basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    review = control.get("manual_review") or None
    return {
        "review_version": version,
        "result_reviewed": (
            job.get("status") in MANUAL_TERMINAL_STATUSES
            and isinstance(review, Mapping) and review.get("review_version") == version
        ),
        "manual_review": dict(review) if isinstance(review, Mapping) else None,
    }
