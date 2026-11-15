"""调度器本地执行的人工装入 / 取走 / 补量节点（``executor_kind="material_io"``）。

这一步不派发到 Host / edge、不占设备锁，由权威侧调度器自己收敛：

1. 起跑时解析参数里的引用，请物料权威试运行一次（``check_material_io``）得到执行计划与锁定集合
   （挂载物料，unload / refill 另加物料）；库位被占、被其他任务占用等临时条件让 attempt 不持有任何东西地排队；
2. 以 attempt 为 owner 取得这些物料锁（持久区间准入 + 执行 Claim，重启时先于任务恢复），期间其他任务要用
   同一库位 / 物料的动作按原有等待规则排队；持锁后再试运行一次，通过即把计划冻结在 attempt 的
   ``control_data.material_io``，并开一张 durable 人工确认单（复用 ``manual_confirm`` 的确认单、超时与取消）；
3. 批准后提交一条 ``material.io`` 库存命令（命令身份 = attempt），完成即节点成功、输出新物料 / 物料；
   被拒（例如确认期间库位被人工占了）节点失败并带物料错误码；人工拒绝 / 超时节点失败、库存不变；取消即取消。

冻结的计划让重启后的接管与第一次完全一致：同一锁定集合、同一张确认单、同一条库存命令（已写过就只重放结果）。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from unilabos.protocol.utils.json_codec import decode_json_bytes, encode_json
from unilabos.server.backend.scheduler.models import MaterialLockClaim, SchedulerResourceRequest
from unilabos.server.services.runtime.workflow.errors import StoreConflict
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims

_CONTROL_KEY = "material_io"


def frozen_material_io(workflow: Any, job_uuid: str) -> Optional[dict[str, Any]]:
    """attempt 已冻结的执行计划与确认设置；还没冻结时返回 None。"""

    getter = getattr(workflow, "get_workflow_node_job", None)
    if not callable(getter):
        return None
    value = ((getter(job_uuid) or {}).get("control_data") or {}).get(_CONTROL_KEY)
    return dict(value) if isinstance(value, Mapping) and isinstance(value.get("plan"), Mapping) else None


def freeze_material_io(workflow: Any, epoch: str, job_uuid: str, frozen: dict[str, Any]) -> dict[str, Any]:
    """把执行计划写进 attempt 的 ``control_data.material_io``；已冻结过就返回原来的那份。"""

    with workflow.transaction() as conn:
        ExecutionClaims._require_epoch(conn, epoch)
        row = ExecutionClaims._job(conn, job_uuid)
        if (row["current_job_uuid"] != job_uuid or row["status"] != "pending"
                or row["task_status"] not in {"pending", "running"}):
            raise StoreConflict("过期或已结束的尝试不能冻结人工物料计划")
        data = decode_json_bytes(row["control_data"].encode("utf-8"))
        if not isinstance(data.get(_CONTROL_KEY), dict):
            data[_CONTROL_KEY] = frozen
            conn.execute("UPDATE workflow_node_job SET control_data=? WHERE uuid=?",
                         (encode_json(data).decode("utf-8"), job_uuid))
        return dict(data[_CONTROL_KEY])


def material_io_request(plan: Mapping[str, Any], *, task_uuid: str, job_uuid: str) -> SchedulerResourceRequest:
    """锁定集合 → 资源申请：只有物料锁，没有设备动作锁；同一计划每次得到完全相同的申请（重启后幂等）。"""

    return SchedulerResourceRequest(
        request_uuid=f"resource:{job_uuid}",
        owner_uuid=job_uuid,
        task_uuid=task_uuid,
        interval_admission=True,
        material_lock_claims=[MaterialLockClaim(material_uuid=item) for item in sorted(plan["lock_material_uuids"])],
    )


def confirmation_description(plan: Mapping[str, Any]) -> str:
    """确认单上给操作员看的一句话。"""

    where = f"{plan.get('mount_name') or plan['mount_uuid']} 的库位 {plan.get('site_label') or plan['site_uuid']}"
    if plan["mode"] == "load":
        name = f"（命名 {plan['material_name']}）" if plan.get("material_name") else ""
        reagent = plan.get("reagent") or {}
        amount = f"，内含 {reagent['quantity']} {reagent['quantity_unit']}" if reagent else ""
        return f"人工装入：把一件 {plan.get('template_name') or plan['template_uuid']}{name}放到 {where}{amount}"
    material = plan.get("material_display_name") or plan.get("material_uuid")
    if plan["mode"] == "unload":
        how = "解除挂载，物料留在库存" if plan.get("disposition") == "detach" else (
            "软删除（连同子物料与内容物）" if plan.get("scope") == "material_with_contents" else "软删除")
        return f"人工取走：从 {where} 取走 {material}，{how}"
    reagent = plan.get("reagent") or {}
    return f"人工补量：给 {where} 上的 {material} 补 {reagent.get('quantity')} {reagent.get('quantity_unit')}"


def confirmation_param(frozen: Mapping[str, Any]) -> dict[str, Any]:
    """确认单参数：去掉任务身份的执行计划 + 确认设置（``assignee_user_ids`` 必须显式给出）。"""

    plan = {key: value for key, value in frozen["plan"].items() if key not in {"task_uuid", "job_uuid"}}
    return {**plan, **frozen["confirmation"]}


def rejection_error(response: Mapping[str, Any]) -> dict[str, Any]:
    """库存命令被拒 → 节点 ``error_info`` 条目，带物料错误码（如 ``conflict:site_occupied``）与业务码。"""

    return {
        "code": "material_io_rejected",
        "message": str(response.get("error") or "库存命令被拒"),
        "material_error_code": response.get("error_code"),
        "business_code": response.get("code"),
    }


__all__ = [
    "confirmation_description",
    "confirmation_param",
    "freeze_material_io",
    "frozen_material_io",
    "material_io_request",
    "rejection_error",
]
