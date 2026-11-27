"""工作流库存命令的证据校验；所有数量写入仍由库存命令事务完成。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from unilabos.app.scheduler.inventory.commands import execute_command
from unilabos.app.scheduler.inventory.domain import CommandRejected, InventoryError

_PREFIX = "unilabos.workflow.native_commands:InventoryCommands."
_RESULT_FIELDS = ("previous_count", "measured_count", "changed", "version")


class InventoryWorkflowCommandError(InventoryError):
    """保留库存事务拒绝的具体原因，供前端和复核使用。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _input_job(store, event: Mapping[str, Any], key: str, task_uuid: str):
    """参数必须由同任务成功作业提供，不能用字面量冒充实测证据。"""
    inputs = dict(event.get("input_jobs") or {})
    identity = inputs.get(f"goal.{key}") or inputs.get(key)
    if not identity:
        raise CommandRejected(f"{key} 必须引用成功的上游作业结果")
    job = store.get_job(str(identity))
    if job["workflow_task_uuid"] != task_uuid or job["status"] != "succeeded":
        raise CommandRejected("库存命令的上游证据不是本任务已成功作业")
    return job


def execute_inventory_command(
    store, inventory, event: Mapping[str, Any], *, task_uuid: str
) -> dict[str, Any]:
    """冻结测量前版本并检查成功证据；重复提交由库存 command_id 幂等处理。"""
    name = event.get("command_name")
    if name not in {
        _PREFIX + "read_count",
        _PREFIX + "reconcile_count",
        _PREFIX + "inspect_carrier_transfer",
        _PREFIX + "locate",
        _PREFIX + "mark_used",
        _PREFIX + "site_occupant",
        _PREFIX + "record_produced",
    }:
        raise CommandRejected(f"OS 本地命令未注册：{name}")
    if inventory is None:
        raise CommandRejected("OS 库存权威未装配")
    if name == _PREFIX + "record_produced":
        return _record_produced(store, inventory, event, task_uuid=task_uuid)
    if name == _PREFIX + "inspect_carrier_transfer":
        from unilabos.app.scheduler.inventory.carrier_snapshot import (
            inspect_carrier_transfer,
        )

        return inspect_carrier_transfer(
            inventory, task_uuid=task_uuid, **dict(event.get("arguments") or {})
        )
    if name == _PREFIX + "locate":
        from unilabos.app.scheduler.inventory.material_usage import locate_material

        return locate_material(inventory, task_uuid=task_uuid, **dict(event.get("arguments") or {}))
    if name == _PREFIX + "site_occupant":
        from unilabos.app.scheduler.inventory.material_usage import site_occupant

        return site_occupant(inventory, task_uuid=task_uuid, **dict(event.get("arguments") or {}))
    if name == _PREFIX + "mark_used":
        from unilabos.app.scheduler.inventory.material_usage import mark_material_used

        return mark_material_used(
            inventory, task_uuid=task_uuid, job_uuid=str(event["job_id"]),
            **dict(event.get("arguments") or {}),
        )
    replayed = committed_inventory_command_result(
        store, inventory, str(event["job_id"]), task_uuid=task_uuid
    )
    if replayed is not None:
        return replayed
    args = dict(event.get("arguments") or {})
    lot = inventory.store.get_lot(str(args["lot_id"]))
    if lot is None or lot["warehouse_zone_id"] != args["mount_uuid"]:
        raise CommandRejected("计数库存不存在或不属于测量容器")
    if name == _PREFIX + "read_count":
        count = lot["quantity_total"]
        if int(count) != count:
            raise CommandRejected("计数命令不能用于非整数库存")
        return {
            "lot_id": args["lot_id"],
            "mount_uuid": args["mount_uuid"],
            "count": int(count),
            "version": lot["version"],
        }

    evidence = _input_job(store, event, "measured_count", task_uuid)
    snapshot = _input_job(store, event, "expected_version", task_uuid)
    if (
        evidence["executor_kind"] != "device_action"
        or snapshot["executor_kind"] != "workflow_command"
    ):
        raise CommandRejected("盘点必须引用设备测量和测量前库存快照")
    snapshot_value = snapshot.get("return_info") or {}
    snapshot_value = snapshot_value.get("return_value", snapshot_value)
    if (
        any(snapshot_value.get(key) != args[key] for key in ("lot_id", "mount_uuid"))
        or snapshot_value.get("version") != args["expected_version"]
    ):
        raise CommandRejected("盘点版本不属于本次测量容器的库存快照")
    observed_at = datetime.fromisoformat(
        str(evidence["finished_at"]).replace("Z", "+00:00")
    )
    snapshot_at = datetime.fromisoformat(
        str(snapshot["finished_at"]).replace("Z", "+00:00")
    )
    started_at = datetime.fromisoformat(
        str(evidence["started_at"]).replace("Z", "+00:00")
    )
    if (
        started_at < snapshot_at
        or not 0 <= (datetime.now(timezone.utc) - observed_at).total_seconds() <= 60
    ):
        raise CommandRejected("测量证据已过期或早于库存快照，须重新测量")
    result = execute_command(
        inventory,
        {
            "command_id": str(event["job_id"]),
            "type": "inventory.reconcile_count",
            "expected_version": args.pop("expected_version"),
            "actor": "workflow",
            "payload": {**args, "evidence_job_uuid": evidence["uuid"]},
        },
    )
    if result["status"] != "completed":
        raise InventoryWorkflowCommandError(
            str(result.get("error_code") or "command_rejected"),
            str(result.get("error") or "库存盘点失败"),
        )
    return {key: result["result"][key] for key in _RESULT_FIELDS}


def _record_produced(store, inventory, event: Mapping[str, Any], *, task_uuid: str) -> dict[str, Any]:
    """沿用 MaterialIO 原子创建与幂等效果；只接受本任务的成功设备产样证据。"""
    from uuid import UUID

    from unilabos.app.scheduler.inventory.material_usage import material_identity

    args = dict(event.get("arguments") or {})
    evidence = _input_job(store, event, "evidence", task_uuid)
    for field in ("barcode", "simulation"):
        if _input_job(store, event, field, task_uuid)["uuid"] != evidence["uuid"]:
            raise CommandRejected("产物条码、证据与模拟标记必须来自同一个动作")
    if evidence["executor_kind"] != "device_action":
        raise CommandRejected("产物登记必须引用成功的设备动作")
    if not isinstance(args.get("simulation"), bool) or not isinstance(args.get("evidence"), str) or not args["evidence"].strip():
        raise CommandRejected("产样证据无效")
    barcode = args.get("barcode")
    if not isinstance(barcode, str) or not 1 <= len(barcode.encode()) <= 128 or barcode != barcode.strip():
        raise CommandRejected("产物条码无效")
    owner = material_identity(args["owner"])
    site = str(UUID(args["site"]))
    template = str(UUID(args["template_uuid"]))
    observed_args = evidence.get("param") or {}
    if observed_args.get("owner") != args["owner"] or observed_args.get("site") != site:
        raise CommandRejected("产样动作的所属载体或库位与登记位置不一致")
    result = inventory.material_io.load(
        template_uuid=template, mount_material_uuid=owner, site_uuid=site,
        workflow_id=task_uuid, node_id=str(event["job_id"]), attempt=1, barcode=barcode,
        _production={"evidence_job_uuid": evidence["uuid"], "evidence_refs": [args["evidence"]],
                     "simulation": args["simulation"]},
    )
    return {"material": result["material"]}


def committed_inventory_command_result(
    store, inventory, job_uuid: str, *, task_uuid: str
) -> dict[str, Any] | None:
    """跨库崩溃后只重投已提交的数量事实，不重做测量或库存写入。"""
    if inventory is None:
        return None
    produced = inventory.material_io.committed_production(task_uuid=task_uuid, job_uuid=job_uuid)
    if produced is not None:
        evidence = store.get_job(produced["evidence_job_uuid"])
        if evidence["workflow_task_uuid"] != task_uuid or evidence["status"] != "succeeded":
            raise CommandRejected("产样登记的持久证据归属错误")
        return {"material": produced["material"]}
    saved = inventory.store.get_processed_command(job_uuid)
    if saved is None or saved["status"] != "completed":
        return None
    result = json.loads(saved["result_json"])
    if not all(key in result for key in (*_RESULT_FIELDS, "evidence_job_uuid")):
        raise CommandRejected("本地盘点命令的持久结果合同不一致")
    evidence = store.get_job(result["evidence_job_uuid"])
    if evidence["workflow_task_uuid"] != task_uuid or evidence["status"] != "succeeded":
        raise CommandRejected("持久盘点结果的测量证据归属错误")
    return {key: result[key] for key in _RESULT_FIELDS}
