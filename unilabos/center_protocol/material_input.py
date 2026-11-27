"""工站两库间的物料输入占用回执；不是中心签发的执行许可。"""

from typing import Any
from uuid import UUID

from unilabos.center_protocol.digest import digest, is_digest


class MaterialClaimConfirmationRequired(ValueError):
    """历史合同只能在核实远端原领取后恢复，不能据本地预留授予新执行。"""


def validate_input_receipt(receipt: Any, package: dict[str, Any]) -> dict[str, Any]:
    if (not isinstance(receipt, dict) or set(receipt) != {
        "station_invocation_uuid", "task_payload_digest", "local_task_uuid", "resources_digest",
    } or receipt["station_invocation_uuid"] != package["payload"]["station_invocation_uuid"]
            or receipt["task_payload_digest"] != package["payload_digest"]
            or not is_digest(receipt["resources_digest"])):
        raise ValueError("库存输入占用回执与原冻结任务包不匹配")
    try:
        identity = receipt["local_task_uuid"]
        if not isinstance(identity, str) or str(UUID(identity)) != identity:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("库存输入占用没有规范的固定任务身份") from None
    return dict(receipt)


def validate_issue_request(request: Any) -> dict[str, Any]:
    if (not isinstance(request, dict) or set(request) != {"task_payload_digest", "reason"}
            or not is_digest(request["task_payload_digest"])
            or not isinstance(request["reason"], str) or not request["reason"].strip()
            or len(request["reason"].encode("utf-8")) > 500):
        raise ValueError("物料输入异常须包含原任务包摘要和 1 至 500 字节原因")
    return dict(request)


def validate_issue_receipt(receipt: Any, *, invocation_uuid: str, instance_uuid: str,
                           request: dict[str, Any]) -> dict[str, Any]:
    validate_issue_request(request)
    if (not isinstance(receipt, dict) or set(receipt) != {
        "station_invocation_uuid", "reporter_instance_uuid", "request_digest",
    } or receipt["station_invocation_uuid"] != invocation_uuid
            or receipt["reporter_instance_uuid"] != instance_uuid
            or receipt["request_digest"] != digest(request)):
        raise ValueError("物料输入异常回执与原调用、实例或正文不匹配")
    return dict(receipt)
