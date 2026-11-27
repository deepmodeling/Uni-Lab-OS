"""公开物料谱系申请与回执；稳定输出键不能随重传改变。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.events import validate_outcome_refs


class MaterialAllocationError(ValueError):
    """谱系映射与已持久的申请或库存事实不一致。"""


def canonical_uuid(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise MaterialAllocationError("物料身份必须为规范 UUID") from None
    return value


def validate_allocation_receipt(raw: Any, *, invocation_uuid: str, instance_uuid: str,
                                station_uuid: str, request: dict[str, Any]) -> dict[str, Any]:
    """回执必须完整覆盖原申请；改变 UUID、版本、父项或条目顺序均不接受。"""
    if (not isinstance(raw, dict) or set(raw) != {
            "station_invocation_uuid", "owner_instance_uuid", "task_payload_digest", "request_digest", "items"}
            or raw["station_invocation_uuid"] != invocation_uuid or raw["owner_instance_uuid"] != instance_uuid
            or raw["task_payload_digest"] != request["task_payload_digest"]
            or raw["request_digest"] != digest(request) or not isinstance(raw["items"], list)
            or len(raw["items"]) != len(request["items"])):
        raise MaterialAllocationError("谱系回执身份、申请摘要或条目数量不匹配")
    refs = []
    for proposal, item in zip(request["items"], raw["items"], strict=True):
        if (not isinstance(item, dict) or set(item) != {"material_port", "item_key", "origin", "material_ref"}
                or any(item[k] != proposal[k] for k in ("material_port", "item_key", "origin"))):
            raise MaterialAllocationError("谱系回执与原物料端口、稳定键或父项不匹配")
        ref = item["material_ref"]
        if (not isinstance(ref, dict) or ref.get("local_material_uuid") != proposal["local_material_uuid"]
                or ref.get("local_revision") != proposal["local_revision"]):
            raise MaterialAllocationError("谱系回执改变了本站物料 UUID 或修订")
        if proposal["origin"]["kind"] == "retained":
            if ref.get("material_lineage_uuid") != proposal["origin"]["material_lineage_uuid"]:
                raise MaterialAllocationError("纯搬运不能改变原谱系")
        elif ref.get("handoff_item_uuid") is not None:
            raise MaterialAllocationError("新谱系不能生成虚构交接身份")
        refs.append(ref)
    validate_outcome_refs({"material_refs": refs, "container_refs": [], "artifact_refs": []}, station_uuid=station_uuid)
    return deepcopy(raw)


def validate_boundary_receipt(raw: Any, *, invocation_uuid: str, instance_uuid: str,
                              request: dict[str, Any]) -> dict[str, Any]:
    """容器边界确认必须指向原调用、owner 和完整正文摘要。"""
    canonical_uuid(invocation_uuid)
    canonical_uuid(instance_uuid)
    if (not isinstance(raw, dict)
            or set(raw) != {"station_invocation_uuid", "owner_instance_uuid", "request_digest"}
            or raw["station_invocation_uuid"] != invocation_uuid
            or raw["owner_instance_uuid"] != instance_uuid
            or raw["request_digest"] != digest(request)):
        raise MaterialAllocationError("容器边界回执与原调用、owner 或正文摘要不符")
    return deepcopy(raw)
