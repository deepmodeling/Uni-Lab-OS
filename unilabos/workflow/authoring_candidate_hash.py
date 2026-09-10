"""工作流创作候选哈希（Authoring Candidate Hash）的唯一纯计算规则。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from unilabos.workflow.json_codec import encode_json

_HASHED_FIELDS = (
    "base_workflow_revision",
    "draft_hash",
    "graph",
    "normalized_python_source",
    "source_map",
    "changeset",
    "compiler_version",
    "template_catalog_fingerprint",
)


class AuthoringCandidateHashError(ValueError):
    """候选版本（Candidate）正文不能按规范计算哈希时的稳定内部错误。"""


def compute_authoring_candidate_hash(candidate: Mapping[str, Any]) -> str:
    """按唯一八字段规则计算工作流创作候选哈希（Candidate Hash）。

    参数：``candidate`` 是签发前候选正文或包含额外持久字段的候选版本
    （Candidate）；只有规范定义的八个字段参与哈希。返回：使用既有稳定排序、
    紧凑 JSON 编码和 ``sha256:`` 前缀的哈希文本。异常：正文不是映射、缺少任一
    规范字段或字段不能编码为 JSON 时抛出 ``AuthoringCandidateHashError``。
    """

    if not isinstance(candidate, Mapping):
        raise AuthoringCandidateHashError("候选版本（Candidate）正文必须是对象")
    try:
        # ``hashed_body`` 是签发规则唯一覆盖的八字段候选正文，不包含时间或旧哈希。
        hashed_body = {field: candidate[field] for field in _HASHED_FIELDS}
        canonical_bytes = encode_json(hashed_body, sort_keys=True)
    except (KeyError, TypeError, UnicodeError, ValueError) as error:
        raise AuthoringCandidateHashError(
            "候选版本（Candidate）正文不能按规范计算哈希"
        ) from error
    return f"sha256:{hashlib.sha256(canonical_bytes).hexdigest()}"


__all__ = ["AuthoringCandidateHashError", "compute_authoring_candidate_hash"]
