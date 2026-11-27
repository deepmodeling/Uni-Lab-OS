"""由操作者显式调用的核实流程；准备、发送、修复事实均不触发物理执行。"""

from __future__ import annotations

from typing import Any

from unilabos.app.center_client.client import CenterClient, CenterClientError
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.center_protocol.reconciliation import validate_request


class ReconciliationRecovery:
    """复用当前运行进程的 service/store/client；后台轮询不会自动发起人工确认。"""

    def __init__(self, client: CenterClient, store: DurableCenterStore, workflow_service: Any) -> None:
        self.client, self.store, self.service = client, store, workflow_service

    def inspect(self, invocation_uuid: str) -> dict[str, Any]:
        return self.client.reconciliation_state(invocation_uuid, self.store.event_epoch_uuid)

    def _body(self, snapshot: dict[str, Any], *, reason: str, evidence_refs: list[str],
              resolution: dict[str, Any]) -> dict[str, Any]:
        return validate_request({
            "expected_state_revision": snapshot["state_revision"],
            "expected_result_revision": snapshot["result_revision"],
            "expected_evidence_digest": snapshot["evidence_digest"],
            "reason": reason, "evidence_refs": evidence_refs, "resolution": resolution,
        })

    def prepare_resume(self, invocation_uuid: str, *, reason: str, evidence_refs: list[str]) -> str:
        snapshot = self.inspect(invocation_uuid)
        state = snapshot["state"]
        if (state["owner_instance_uuid"] != self.store.instance_uuid or state["local_task_uuid"] is None
                or state["execution_status"] in {"succeeded", "failed", "canceled"}):
            raise CenterClientError("恢复只能核实原 owner 已绑定且非终态的 Task")
        package = self.client.task_package(invocation_uuid, self.store.event_epoch_uuid)
        raw = {"payload": dict(package.raw), "payload_digest": package.payload_digest}
        if (snapshot["global_task_uuid"] != package.global_task_uuid
                or snapshot["workflow_uuid"] != package.raw["workflow_uuid"]
                or snapshot["payload_digest"] != package.payload_digest):
            raise CenterClientError("核实快照与冻结任务包不匹配")
        self.service.inspect_existing_center_task(raw, local_task_uuid=state["local_task_uuid"])
        body = self._body(snapshot, reason=reason, evidence_refs=evidence_refs, resolution={
            "kind": "resume_original_task", "local_task_uuid": state["local_task_uuid"],
            "payload_digest": package.payload_digest, "event_epoch_uuid": self.store.event_epoch_uuid,
        })
        return self.store.remember_reconciliation(invocation_uuid, body, package=raw)

    def prepare_evidence(self, invocation_uuid: str, *, reason: str, evidence_refs: list[str]) -> str:
        return self.store.remember_reconciliation(invocation_uuid, self._body(
            self.inspect(invocation_uuid), reason=reason, evidence_refs=evidence_refs,
            resolution={"kind": "record_evidence"},
        ))

    def prepare_result_confirmation(self, invocation_uuid: str, *, local_task_uuid: str,
                                    result_event_uuid: str, result_digest: str,
                                    reason: str, evidence_refs: list[str]) -> str:
        return self.store.remember_reconciliation(invocation_uuid, self._body(
            self.inspect(invocation_uuid), reason=reason, evidence_refs=evidence_refs,
            resolution={"kind": "confirm_recorded_result", "local_task_uuid": local_task_uuid,
                        "result_event_uuid": result_event_uuid, "result_digest": result_digest},
        ))

    def send(self, request_uuid: str) -> dict[str, Any]:
        """精确重试持久工作单；ACK 丢失后即使 Task 已终态也只补真实结果。"""
        request = self.store.reconciliation(request_uuid)
        body = request["body"]
        resume = body["resolution"]["kind"] == "resume_original_task"
        if resume:
            if (request["event_epoch_uuid"] != self.store.event_epoch_uuid
                    or request["instance_uuid"] != self.store.instance_uuid
                    or request["station_uuid"] != self.store.station_uuid):
                raise CenterClientError("旧恢复工作单不能用于新的账本纪元或实例")
            self.service.inspect_existing_center_task(
                request["package"], local_task_uuid=request["local_task_uuid"],
                allow_terminal=bool(request["attempted"]),
            )
        receipt = request["receipt"]
        if receipt is None:
            self.store.mark_reconciliation_attempted(request_uuid)
            receipt = self.client.reconcile(
                request["invocation_uuid"], body, idempotency_key=request_uuid,
                event_epoch_uuid=self.store.event_epoch_uuid,
            )
            self.store.save_reconciliation_receipt(request_uuid, receipt)
        if resume:
            self.service.apply_center_reconciliation(request_uuid)
        return receipt
