"""派发前冻结库存来源；库存库提交应答丢失后仍按同一 attempt 恢复。"""
from __future__ import annotations

from typing import Any, Callable

from unilabos.protocol.materials import InventoryRequirement
from unilabos.protocol.utils.json_codec import decode_json_bytes, encode_json
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims
from unilabos.server.services.runtime.workflow.errors import StoreConflict


def freeze_inventory(workflow: Any, epoch: str, job_uuid: str,
                     resolve: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    with workflow.transaction() as conn:
        ExecutionClaims._require_epoch(conn, epoch)
        row = ExecutionClaims._job(conn, job_uuid)
        if (row["current_job_uuid"] != job_uuid or row["status"] != "pending"
                or row["task_status"] not in {"pending", "running"}):
            raise StoreConflict("过期或已结束的尝试不能预留库存")
        data = decode_json_bytes(row["control_data"].encode("utf-8"))
        if "inventory_request" not in data:
            data["inventory_request"] = [InventoryRequirement.model_validate(item).model_dump(
                mode="json", exclude_none=False) for item in resolve()]
            conn.execute("UPDATE workflow_node_job SET control_data=? WHERE uuid=?",
                         (encode_json(data).decode("utf-8"), job_uuid))
        return data["inventory_request"]


def inventory_wait(workflow: Any, epoch: str, job_uuid: str, reason: str | None, *, advance: bool = False) -> None:
    with workflow.transaction() as conn:
        ExecutionClaims._require_epoch(conn, epoch)
        row = ExecutionClaims._job(conn, job_uuid)
        if row["status"] != "pending" or row["current_job_uuid"] != job_uuid:
            return
        data = decode_json_bytes(row["control_data"].encode("utf-8"))
        if data.get("inventory_wait") == reason and not advance:
            return
        if advance:
            data["inventory_generation"] = int(data.get("inventory_generation", 0)) + 1
        if reason:
            data["inventory_wait"] = reason
        else:
            data.pop("inventory_wait", None)
        conn.execute("UPDATE workflow_node_job SET control_data=? WHERE uuid=?",
                     (encode_json(data).decode("utf-8"), job_uuid))
