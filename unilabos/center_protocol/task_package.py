"""任务包的唯一纯协议校验；网络入口与工作流事务复用同一规则。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping
from uuid import UUID

from unilabos.center_protocol.digest import DigestError, digest, is_digest


class TaskPackageError(RuntimeError):
    """任务包协议、固定身份或摘要不合法。"""


def validate_task_package(body: Any) -> dict[str, Any]:
    """验证完整冻结包并返回保留数值类型的副本，不规范化或改写原身份。"""
    if not isinstance(body, Mapping):
        raise TaskPackageError("任务包必须为对象")
    payload = body.get("payload")
    if not isinstance(payload, Mapping):
        raise TaskPackageError("任务包缺少 payload")
    expected = {
        "protocol_version", "global_task_uuid", "global_workflow_revision_uuid",
        "station_invocation_uuid", "invocation_key", "station_uuid", "contract_uuid",
        "workflow_uuid", "publication_version", "revision_fingerprint", "contract_digest",
        "projection_digest", "adapter_version", "input", "handoff_uuids",
        "material_refs", "container_refs", "artifact_refs",
    }
    for name in ("handoff_uuids", "material_refs", "container_refs", "artifact_refs"):
        if not isinstance(payload.get(name), list):
            raise TaskPackageError(f"任务包 {name} 必须为数组")
    if "material_source_bindings" in payload:
        expected.add("material_source_bindings")
        from unilabos.center_protocol.material_source import validate_material_source_bindings
        try:
            validate_material_source_bindings(payload["material_source_bindings"], station_uuid=payload.get("station_uuid", ""))
        except (ValueError, TypeError, KeyError) as error:
            raise TaskPackageError(str(error)) from error
    if "transport" in payload:
        expected.add("transport")
        from unilabos.center_protocol.transport import validate_transport_context

        try:
            context = validate_transport_context(payload["transport"])
            if context["logistics_station_uuid"] != payload.get("station_uuid") or context != validate_transport_context(payload.get("input", {}).get("center_transport")):
                raise ValueError("搬运许可与物流工站或输入不匹配")
            # 搬运调用可使用本站的独立物料，但承运货物仍由交接保护，不能同时作为消费输入。
            if context["handoff_uuid"] in payload["handoff_uuids"]:
                raise ValueError("搬运调用不能消费本次承运交接的收货结果")
            for reference in payload["container_refs"]:
                if not isinstance(reference, Mapping):
                    raise ValueError("搬运调用的库存容器引用无效")
                if reference.get("container_uuid") in context["container_uuids"]:
                    raise ValueError("搬运调用不能消费本次承运容器")
            for reference in payload["material_refs"]:
                if not isinstance(reference, Mapping) or reference.get("station_uuid") != payload.get("station_uuid"):
                    raise ValueError("搬运调用的消费物料必须属于执行工站库存")
        except (ValueError, AttributeError) as error:
            raise TaskPackageError(str(error)) from error
    if "transport_binding" in payload:
        expected.add("transport_binding")
        from .transport_preparation import validate_binding
        try:
            binding = validate_binding(payload["transport_binding"])
            if "transport" not in payload or binding["vehicle_id"] != payload["transport"]["vehicle_id"]:
                raise ValueError("车辆预占绑定与中心运输许可不一致")
        except ValueError as error:
            raise TaskPackageError(str(error)) from error
    if "transport" not in payload and isinstance(payload.get("input"), Mapping) and "center_transport" in payload["input"]:
        raise TaskPackageError("任务输入含搬运许可但缺少中心冻结的 transport 字段")
    if set(body) != {"payload", "payload_digest"} or set(payload) != expected:
        raise TaskPackageError("任务包字段缺失或包含未知字段")
    for key in ("station_invocation_uuid", "global_task_uuid", "contract_uuid",
                "global_workflow_revision_uuid", "station_uuid", "workflow_uuid"):
        try:
            if str(UUID(payload[key])) != payload[key]:
                raise ValueError("UUID 不是规范形式")
        except (ValueError, TypeError, AttributeError) as error:
            raise TaskPackageError(f"任务包 {key} 不是有效 UUID") from error
    if payload["protocol_version"] != "1.0" or payload["adapter_version"] != "1.0":
        raise TaskPackageError("任务包协议或适配器版本不兼容")
    version = payload["publication_version"]
    if type(version) is not int or not 1 <= version <= 2**53 - 1:
        raise TaskPackageError("任务包发布版本无效")
    if not isinstance(payload["input"], Mapping):
        raise TaskPackageError("任务包 input 必须为对象")
    key = payload["invocation_key"]
    try:
        revision, node = key.split(":")
        if str(UUID(node)) != node:
            raise ValueError("调用节点 UUID 不是规范形式")
        if revision != payload["global_workflow_revision_uuid"]:
            raise ValueError("修订不匹配")
    except (ValueError, TypeError, AttributeError) as error:
        raise TaskPackageError("任务包调用键与修订不匹配") from error
    for name in ("revision_fingerprint", "contract_digest", "projection_digest"):
        if not is_digest(payload[name]):
            raise TaskPackageError(f"任务包 {name} 摘要无效")
    supplied = body.get("payload_digest")
    try:
        if not is_digest(supplied) or digest(payload) != supplied:
            raise TaskPackageError("任务包摘要不匹配，拒绝领取")
    except DigestError as error:
        raise TaskPackageError("任务包无法规范化") from error
    raw = deepcopy(dict(payload))
    return {"payload": raw, "payload_digest": supplied}
