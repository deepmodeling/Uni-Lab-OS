"""设备执行读取原生 Job 和已签发 Permit，不能自行生成任务身份或扩大资源范围。"""

from __future__ import annotations

import json

from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchFence,
    validate_physical_settlement_credentials,
)
from unilabos.app.scheduler.inventory.station_resource import (
    AliquotReceipt,
    MaterialAliquotCommand,
    MaterialTransferCommand,
)
from unilabos.workflow.execution_claim import get_execution_claim

from .inventory.execution_effects import CanonicalSettlement


def _value(value):
    if isinstance(value, dict) and "uuid" in value:
        return {"uuid": value["uuid"]}
    if isinstance(value, list):
        return [_value(item) for item in value]
    return value


def _resource_parameter_names(node):
    """从冻结动作资源合同及 ResourceSlot 标记确定分组，不信任请求自行分类。"""
    contract = node.get("action_resource_contract") or {}
    names = set(contract.get("required_device_params", ()))
    names.update(item["param"] for item in contract.get("resource_params", ()))
    fields = {
        "transfer": (
            "material_param",
            "source_owner_param",
            "source_site_uuid_param",
            "source_site_name_param",
            "target_owner_param",
            "target_site_uuid_param",
            "target_site_name_param",
        ),
        "transfer_step": ("material_param", "owner_param", "site_param"),
        "aliquot": ("source_material_param",),
    }
    for section, keys in fields.items():
        declaration = contract.get(section) or {}
        names.update(declaration[key] for key in keys if declaration.get(key))
    names.update((contract.get("transfer_step") or {}).get("carrier_params", ()))
    names.update((contract.get("aliquot") or {}).get("target_material_params", ()))
    schema = node.get("param_schema") or {}
    goal = schema.get("properties", {}).get("goal", {})

    def has_material_reference(value, seen=()):
        if isinstance(value, list):
            return any(has_material_reference(item, seen) for item in value)
        if not isinstance(value, dict):
            return False
        if value.get("x-unilabos-material-lock") is True:
            return True
        reference = value.get("$ref")
        if (
            isinstance(reference, str)
            and reference.startswith("#/")
            and reference not in seen
        ):
            resolved = goal
            for part in reference[2:].split("/"):
                resolved = resolved.get(part.replace("~1", "/").replace("~0", "~"), {})
            if has_material_reference(resolved, (*seen, reference)):
                return True
        return any(has_material_reference(item, seen) for item in value.values())

    names.update(
        key
        for key, prop in goal.get("properties", {}).items()
        if has_material_reference(prop)
    )
    return names


def _resource_reference(value):
    if isinstance(value, str):
        return True  # 冻结合同中的库位名称允许空串。
    if isinstance(value, list):
        return all(_resource_reference(item) for item in value)
    return (
        isinstance(value, dict)
        and set(value) == {"uuid"}
        and isinstance(value["uuid"], str)
        and bool(value["uuid"])
    )


class ExecutionAuthority:
    def __init__(self, workflow, inventory_service, bindings):
        self.workflow, self.inventory, self.bindings = (
            workflow,
            inventory_service,
            bindings,
        )
        self.settlement = CanonicalSettlement(inventory_service)

    def authorize(self, request):
        identity = request["identity"]
        job = self.workflow.get_job(identity["node_job_uuid"])
        task = self.workflow.get_task(identity["task_uuid"])
        binding = self.bindings[request["device_type"]]
        node = next(
            n
            for n in task["execution_plan"]["nodes"]
            if n["uuid"] == job["workflow_node_uuid"]
        )
        if (
            job["workflow_task_uuid"] != identity["task_uuid"]
            or node.get("material_uuid") != binding["uuid"]
            or node.get("kind") != "device_action"
            or node.get("action_name") != binding["actions"][request["operation"]]
            or job["status"] != "running"
            or not job["current_attempt"]
        ):
            raise ValueError("native_job_binding_mismatch")
        parameters, resources = request["parameters"], request["resources"]
        # 两类输入不得互相覆盖。执行模型消费 parameters，不能用 resources 遮蔽它。
        if set(parameters) & set(resources) or set(parameters) | set(resources) != set(
            job["param"]
        ):
            raise ValueError("native_job_parameter_mismatch")
        expected_resources = _resource_parameter_names(node) & set(job["param"])
        if set(resources) != expected_resources:
            raise ValueError("native_job_parameter_mismatch")
        for key, value in parameters.items():
            if json.dumps(value, sort_keys=True, allow_nan=False) != json.dumps(
                job["param"][key], sort_keys=True, allow_nan=False
            ):
                raise ValueError("native_job_parameter_mismatch")
        for key, value in resources.items():
            # 资源 UUID 与库位名称是引用；不接受夹带额外执行字段的资源对象。
            if not _resource_reference(value):
                raise ValueError("native_job_parameter_mismatch")
            if _value(value) != _value(job["param"][key]):
                raise ValueError("native_job_parameter_mismatch")
        with self.workflow.read() as connection:
            claim = get_execution_claim(connection, job_uuid=job["uuid"])
        if claim is None or claim["attempt"] != job["attempt"]:
            raise ValueError("native_job_claim_missing")
        # 原生驱动开始回执已把 Permit 从 reserved 推进到 running。这里复验
        # 正在执行的完整凭据；不能再次调用仅允许派发前 reserved 的校验器。
        with self.inventory.store.transaction() as connection:
            state = validate_physical_settlement_credentials(
                connection,
                effect_uuid=job["dispatch_effect_uuid"],
                claim_uuid=claim["claim_uuid"],
                job_uuid=job["uuid"],
                attempt=job["attempt"],
                parameter_hash=job["dispatch_parameter_hash"],
                expected_change_set=job["expected_change_set"],
                fences={
                    item["lock_key"]: item["fencing_token"] for item in claim["fences"]
                },
            )
            persisted = connection.execute(
                "SELECT task_uuid,resource_keys,committed_at,released_at FROM station_execution_claim WHERE claim_uuid=?",
                (claim["claim_uuid"],),
            ).fetchone()
            if (
                state != "running"
                or persisted["task_uuid"] != identity["task_uuid"]
                or sorted(json.loads(persisted["resource_keys"]))
                != sorted(claim["resource_keys"])
                or persisted["committed_at"] is None
                or persisted["released_at"] is not None
            ):
                raise ValueError("native_running_permit_mismatch")
        return job, claim

    def grant(self, request):
        """冻结执行许可；物理操作完成后必须提交同一 attempt/claim/fence。"""
        job, claim = self.authorize(request)
        return {
            "attempt": job["attempt"],
            "claim_uuid": claim["claim_uuid"],
            "effect_uuid": job["dispatch_effect_uuid"],
            "parameter_hash": job["dispatch_parameter_hash"],
            "fences": claim["fences"],
        }

    def settle(self, request, proof, *, grant=None):
        job, claim = self.authorize(request)
        if grant is None or grant != self.grant(request):
            raise ValueError("execution_grant_changed")
        if proof.get("identity") != request["identity"]:
            raise ValueError("physical_proof_identity_mismatch")
        common = dict(
            effect_uuid=job["dispatch_effect_uuid"],
            claim_uuid=claim["claim_uuid"],
            job_uuid=job["uuid"],
            attempt=job["attempt"],
            parameter_hash=job["dispatch_parameter_hash"],
            expected_change_set=job["expected_change_set"],
            fences=tuple(DispatchFence(**item) for item in claim["fences"]),
        )
        expected = job["expected_change_set"]
        if expected["kind"] == "material_transfer":
            target = self.inventory.store.query_one(
                "SELECT material_uuid,name FROM site WHERE uuid=? AND deleted_at IS NULL",
                (expected["target_site_uuid"],),
            )
            if not target:
                raise ValueError("native_target_site_missing")
            # 审计 actor 属于历史冻结意图；职责搬迁不能改变它的字节与摘要。
            command = MaterialTransferCommand(
                material_uuid=expected["material_uuid"],
                target_owner_material_uuid=target["material_uuid"],
                target_site_uuid=expected["target_site_uuid"],
                target_site_name=target["name"],
                actor="twin.physical_settlement",
                **common,
            )
        elif expected["kind"] == "material_content_aliquot":
            receipts = proof.get("material_aliquot_receipts", [])
            command = MaterialAliquotCommand(
                source_material_uuid=expected["source_material_uuid"],
                receipts=tuple(AliquotReceipt(**item) for item in receipts),
                **common,
            )
        else:
            raise ValueError("native_effect_contract_missing")
        return self.settlement.commit(command, proof)
