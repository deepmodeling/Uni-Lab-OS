"""工作流物料转移作业在工站库存中的幂等物理结算。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from unilabos.app.scheduler.inventory.dispatch_admission import DispatchFence
from unilabos.app.scheduler.inventory.station_resource import (
    MaterialTransferCommand,
    StationResourceError,
    StationResourceInventory,
)
from unilabos.registry.action_resource_contract import TRANSFER_CONTRACT_FIELDS
from unilabos.utils.tracing import span
from unilabos.workflow.store import StoreConflict


class InventoryFactConflict(StoreConflict):
    """物理结果已知，但不能用旧预期覆盖人工库存事实。"""

    def __init__(self, error: StationResourceError) -> None:
        super().__init__(error.message)
        self.details = {"code": error.code, "message": error.message,
                        "resources": [dict(item) for item in error.resources]}


class MaterialTransferSettlement:
    """把成功的物料转移 Job 收敛为本地父级与库位事实。"""

    def __init__(self, inventory: StationResourceInventory | None) -> None:
        """绑定工站调度进程拥有的库存权威。

        参数：``inventory`` 是可选库存服务；无物料转移节点的工作流允许为空。
        返回无。异常：构造不访问数据库；真正需要转移结算但库存未装配时由
        ``settle_success`` 关闭式失败。
        """

        self._inventory = inventory

    def settle_success(
        self,
        *,
        job: Mapping[str, Any],
        execution_plan: Mapping[str, Any] | None = None,
        execution_claim: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """在作业成功终态落库前提交物料与库位变化。

        参数：``job`` 是含最终实际参数的持久工作流节点作业；``execution_plan``
        提供任务创建时冻结的 AST 动作资源合同。返回：普通设备作业为 ``None``；
        声明转运语义的作业返回库存权威物料快照。异常：库存未装配、计划、
        Job/Material/库位身份或实际参数不完整时抛 ``StoreConflict``；库存冲突
        原样传播。使用稳定 Job UUID 作为因果身份，跨库崩溃后可安全重放。
        """

        transfer = _transfer_contract(job, execution_plan)
        if transfer is None:
            return None
        if self._inventory is None:
            raise StoreConflict("物料转移作业未装配工站库存权威")
        job_uuid = _required_text(job.get("uuid"), field="job.uuid")
        if not isinstance(execution_claim, Mapping):
            raise StoreConflict(f"物料转移作业缺少库存 Claim：{job_uuid}")
        claim_uuid = _required_text(
            execution_claim.get("claim_uuid"),
            field="execution_claim.claim_uuid",
        )
        effect_uuid = _required_text(
            job.get("dispatch_effect_uuid"),
            field="job.dispatch_effect_uuid",
        )
        parameter_hash = _required_text(
            job.get("dispatch_parameter_hash"),
            field="job.dispatch_parameter_hash",
        )
        try:
            attempt = int(job.get("attempt"))
        except (TypeError, ValueError) as error:
            raise StoreConflict("物料转移作业 attempt 无效") from error
        if attempt <= 0 or int(execution_claim.get("attempt") or 0) != attempt:
            raise StoreConflict("物料转移作业 Claim attempt 与 Job 不一致")
        expected_change_set = job.get("expected_change_set")
        if not isinstance(expected_change_set, Mapping):
            raise StoreConflict("物料转移作业缺少冻结 ChangeSet")
        raw_fences = execution_claim.get("fences")
        if not isinstance(raw_fences, list) or not raw_fences:
            raise StoreConflict("物料转移作业缺少库存 Fence")
        try:
            fences = tuple(
                DispatchFence(
                    lock_key=_required_text(
                        item.get("lock_key"),
                        field="execution_claim.fence.lock_key",
                    ),
                    fencing_token=int(item.get("fencing_token")),
                )
                for item in raw_fences
                if isinstance(item, Mapping)
            )
        except (TypeError, ValueError) as error:
            raise StoreConflict("物料转移作业 Fence 损坏") from error
        if len(fences) != len(raw_fences) or any(
            fence.fencing_token <= 0 for fence in fences
        ):
            raise StoreConflict("物料转移作业 Fence 损坏")
        param = job.get("param")
        if not isinstance(param, Mapping):
            raise StoreConflict(f"物料转移作业实际参数不是对象：{job_uuid}")
        material_param = transfer["material_param"]
        owner_param = transfer["target_owner_param"]
        material_uuid = _resource_uuid(param.get(material_param), field=material_param)
        parent_uuid = _resource_uuid(
            param.get(owner_param),
            field=owner_param,
        )
        site_name_param = transfer["target_site_name_param"]
        site_uuid_param = transfer["target_site_uuid_param"]
        site_name = str(
            (param.get(site_name_param) if site_name_param else "") or ""
        ).strip()
        site_uuid = str(
            (param.get(site_uuid_param) if site_uuid_param else "") or ""
        ).strip()
        if not site_uuid:
            site_uuid = str(
                expected_change_set.get("target_site_uuid") or ""
            ).strip()
        if not site_name and not site_uuid:
            raise StoreConflict("物料转移结算缺少目标库位名称或稳定 UUID")
        try:
            with span(
                "inventory.material_transfer.settle",
                kind="client",
                attributes={
                    "workflow.job.uuid": job_uuid,
                    "workflow.task.uuid": str(
                        job.get("workflow_task_uuid") or ""
                    ),
                    "inventory.claim.uuid": claim_uuid,
                    "material.uuid": material_uuid,
                    "inventory.target.owner.uuid": parent_uuid,
                    "inventory.target.site.uuid": site_uuid,
                    "inventory.target.site.name": site_name,
                    "workflow.job.attempt": attempt,
                },
            ):
                return dict(
                    self._inventory.settle_material_transfer(
                        MaterialTransferCommand(
                            material_uuid=material_uuid,
                            target_owner_material_uuid=parent_uuid,
                            target_site_uuid=site_uuid,
                            target_site_name=site_name,
                            actor="station_scheduler.material_transfer",
                            causation_id=(
                                f"workflow-node-job:{job_uuid}:material-transfer"
                            ),
                            effect_uuid=effect_uuid,
                            claim_uuid=claim_uuid,
                            job_uuid=job_uuid,
                            attempt=attempt,
                            parameter_hash=parameter_hash,
                            expected_change_set=dict(expected_change_set),
                            fences=fences,
                        )
                    )
                )
        except StationResourceError as error:
            if error.code == "inventory_fact_changed":
                raise InventoryFactConflict(error) from error
            raise StoreConflict(error.message) from error


def _transfer_contract(
    job: Mapping[str, Any],
    execution_plan: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """从冻结计划读取作业唯一合法的转运参数映射。

    参数：``job`` 提供节点和执行种类；``execution_plan`` 是父任务的不可变计划。
    返回：八个转运参数/角色字段组成的稳定映射；非转运动作返回 ``None``。异常：
    计划节点或合同形状损坏时抛 ``StoreConflict``。
    """

    node_uuid = str(job.get("workflow_node_uuid") or "").strip()
    plan_nodes = (
        execution_plan.get("nodes") if isinstance(execution_plan, Mapping) else None
    )
    selected = None
    if isinstance(plan_nodes, list):
        selected = next(
            (
                node
                for node in plan_nodes
                if isinstance(node, Mapping)
                and str(node.get("uuid") or "") == node_uuid
            ),
            None,
        )
    resource_contract = (
        selected.get("action_resource_contract")
        if isinstance(selected, Mapping)
        else None
    )
    transfer = (
        resource_contract.get("transfer")
        if isinstance(resource_contract, Mapping)
        else None
    )
    if transfer is None:
        action_name = (
            str(selected.get("action_name") or "").strip()
            if isinstance(selected, Mapping)
            else ""
        )
        if action_name == "transfer_resource":
            return {
                "material_param": "resource",
                "source_owner_param": "",
                "source_site_uuid_param": "",
                "source_site_name_param": "",
                "target_owner_param": "mount_resource",
                "target_site_uuid_param": "site_uuid",
                "target_site_name_param": "site",
                "gripper_site_role": "",
            }
        if str(job.get("executor_kind") or "") == "material_transfer":
            raise StoreConflict(f"物料转移作业缺少冻结资源合同：{node_uuid}")
        return None
    required = set(TRANSFER_CONTRACT_FIELDS)
    optional = {"motion_resource_roles", "tool_resource_roles"}
    if (
        not isinstance(transfer, Mapping)
        or not required <= set(transfer)
        or bool(set(transfer) - required - optional)
        or any(not isinstance(transfer[field], str) for field in required)
        or any(
            field in transfer
            and (
                not isinstance(transfer[field], list)
                or any(
                    not isinstance(role, str) or not role
                    for role in transfer[field]
                )
            )
            for field in optional
        )
    ):
        raise StoreConflict(f"物料转移作业冻结合同损坏：{node_uuid}")
    return {field: str(transfer[field]) for field in required}


def _resource_uuid(value: Any, *, field: str) -> str:
    """从冻结动作参数读取一个明确物料 UUID。

    参数：``value`` 是 ResourceSlot 形状对象；``field`` 是诊断字段名。返回：
    去空白后的 UUID 文本。异常：对象或 UUID 字段缺失时抛 ``StoreConflict``；
    UUID 格式已经在工作流输入与动作合同门禁中验证，此处不重新解释。
    """

    if not isinstance(value, Mapping):
        raise StoreConflict(f"物料转移作业缺少 {field} 对象")
    return _required_text(value.get("uuid"), field=f"{field}.uuid")


def _required_text(value: Any, *, field: str) -> str:
    """读取非空稳定身份文本。

    参数：待检查值和字段名。返回：规范文本。异常：值不是非空字符串时抛
    ``StoreConflict``，阻止库存结算使用名称猜测身份。
    """

    normalized = str(value or "").strip()
    if not normalized:
        raise StoreConflict(f"物料转移结算缺少 {field}")
    return normalized


__all__ = ["MaterialTransferSettlement"]
