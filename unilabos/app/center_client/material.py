"""真实库存版本 → 稳定中心谱系 → 工作流 outcome 的可恢复桥。"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from unilabos.app.center_client.client import CenterClientError, SessionFencedError
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.app.scheduler.inventory.material_exports import InventoryMaterialAuthority
from unilabos.center_protocol.digest import digest
from unilabos.workflow.store import StoreConflict

if TYPE_CHECKING:
    from unilabos.app.center_client.runtime import CenterRuntime
    from unilabos.app.scheduler.inventory.store import InventoryStore
    from unilabos.workflow.store import WorkflowStore

logger = logging.getLogger(__name__)


def configure_materials(runtime: CenterRuntime, inventory_store: InventoryStore, workflow_store: WorkflowStore,
                        *, handoff_authority: Any, origins: Callable[[dict[str, Any]], dict[str, dict[str, Any]]] | None = None) -> MaterialBridge:
    """在 start 前显式装配；分装/合并提案由可信本站逻辑提供，缺省仅新物料或原样保留。"""
    def build() -> MaterialBridge:
        authority = InventoryMaterialAuthority(inventory_store, station_uuid=runtime.store.station_uuid,
            instance_uuid=runtime.store.instance_uuid, event_epoch_uuid=runtime.store.event_epoch_uuid)
        return MaterialBridge(runtime.client, workflow_store, authority, handoff_authority=handoff_authority, origins=origins)
    return runtime.install_material_bridge(build)


class MaterialBridge:
    def __init__(self, client: Any, workflow_store: WorkflowStore, authority: InventoryMaterialAuthority,
                 *, handoff_authority: Any = None, origins: Callable[[dict[str, Any]], dict[str, dict[str, Any]]] | None = None):
        self.client, self.workflow, self.authority = client, workflow_store, authority
        self.handoffs = handoff_authority
        if handoff_authority is not None:
            handoff_authority.source_context_resolver = workflow_store.center_material_source_context
        self.origins = origins
        self.errors: dict[str, str] = {}
        self._source_cursor = ""
        self._intent_cursor = ""
        self._boundary_cursor = ""

    def attention_state(self) -> tuple[Any, ...]:
        """只读取诊断修订和执行状态，不加载物料来源、结果或设备日志。"""
        from unilabos.persistence.inspection import table_exists
        with self.authority.store.read_connection() as c:
            failures = tuple(tuple(row) for row in c.execute(
                "SELECT invocation_uuid,local_task_uuid,retry_json FROM station_material_export "
                "WHERE retry_json != '{}' ORDER BY invocation_uuid").fetchall())
        with self.workflow.read() as c:
            if not table_exists(c, "center_invocation_inbox"):
                return (failures, ())
            failed = [row[0] for row in failures]
            condition = "e.pending_reason IN ('material_output_invalid','output_not_exportable')"
            if failed:
                condition += " OR i.invocation_uuid IN (" + ",".join("?" for _ in failed) + ")"
            rows = c.execute("""SELECT i.invocation_uuid,i.local_task_uuid,t.status,e.pending_reason,
                json_extract(e.cancel_settlement_json,'$.resources_released') AS released,
                json_extract(e.cancel_settlement_json,'$.release_receipt') IS NOT NULL AS has_release_receipt,
                json_extract(e.recorded_cancellation_json,'$.completed') AS historical_completed
                FROM center_invocation_inbox i JOIN workflow_task t ON t.uuid=i.local_task_uuid
                LEFT JOIN center_outcome_evidence e ON e.invocation_uuid=i.invocation_uuid
                WHERE """ + condition + " ORDER BY i.invocation_uuid", failed).fetchall()
            contexts = tuple(tuple(row) for row in rows)
        return (failures, contexts)

    def export_attention_items(self) -> list[dict[str, Any]]:
        """持久补报失败是任务级事项；绝不伪造 Job 或改写执行状态。"""
        from datetime import datetime, timezone
        contexts = {c["invocation_uuid"]: c for c in self.workflow.center_material_attention_contexts()}
        settlement_reader = getattr(self.workflow, "center_material_settlement", None)
        if callable(settlement_reader):
            contexts = {identity: context for identity, context in contexts.items()
                        if not self._settlement_released(settlement_reader(identity))}
        historical_reader = getattr(self.workflow, 'center_recorded_cancellation', None)
        if callable(historical_reader):
            from unilabos.app.center_client.recorded_cancellation import reconciliation_completed
            contexts = {identity: context for identity, context in contexts.items()
                        if not reconciliation_completed(historical_reader(identity))}
        items = []
        for failure in self.authority.export_failures():
            context = contexts.get(failure["invocation_uuid"])
            if context is None or context["local_task_uuid"] != failure["local_task_uuid"]:
                continue
            payload = context["package"]["payload"]
            if (context["instance_uuid"] != self.authority.instance_uuid
                    or payload["station_uuid"] != self.authority.station_uuid
                    or failure["source"]["owner_instance_uuid"] != self.authority.instance_uuid
                    or failure["source"]["event_epoch_uuid"] != self.authority.event_epoch_uuid
                    or failure["source"]["task_payload_digest"] != context["package"]["payload_digest"]):
                continue
            revision = int(digest(failure).split(":")[-1][:12], 16) + 1
            opened = (datetime.fromtimestamp(failure.get("opened_at_ms", failure["observed_at_ms"]) / 1000, timezone.utc).isoformat()
                      if failure.get("observed_at_ms") else context["finished_at"] or context["created_at"])
            items.append({"decision_kind": "material_export", "decision_id": failure["invocation_uuid"] + ":" + failure["phase"],
                "workflow_task_uuid": failure["local_task_uuid"], "invocation_uuid": failure["invocation_uuid"],
                "phase": failure["phase"], "revision": revision, "status": failure["status"],
                "opened_at": opened, "options": [{"id": "resume_export"}],
                "meta_data": {**{k: v for k, v in failure.items() if k != "source"},
                    "materials": failure["source"]["outputs"], "execution_status": context["task_status"]}})
        for identity, context in contexts.items():
            if context["pending_reason"] not in {"material_output_invalid", "output_not_exportable"}:
                continue
            payload = context["package"]["payload"]
            if context["instance_uuid"] != self.authority.instance_uuid or payload["station_uuid"] != self.authority.station_uuid:
                continue
            items.append({"decision_kind": "material_export", "decision_id": identity + ":output",
                "workflow_task_uuid": context["local_task_uuid"], "invocation_uuid": identity,
                "phase": "output", "revision": 1, "status": "blocked",
                "opened_at": context["finished_at"] or context["created_at"], "options": [],
                "meta_data": {"last_error": context["pending_reason"], "execution_status": context["task_status"],
                    "requires_verified_settlement": True}})
        return items

    @staticmethod
    def _settlement_released(state: dict[str, Any] | None) -> bool:
        return bool(state and state.get("resources_released") is True and state.get("release_receipt") is not None)

    def resume_failure(self, invocation_uuid: str, phase: str, *, expected_revision: int) -> dict[str, Any]:
        """核对原调用、owner、版本及恢复保护，只恢复原补报预算。"""
        item = next((i for i in self.export_attention_items()
                     if i["invocation_uuid"] == invocation_uuid and i["phase"] == phase), None)
        if item is None or phase == "output":
            raise ValueError("没有可恢复的原物料补报阶段")
        if item["revision"] != expected_revision:
            raise ValueError("物料补报版本已变化，请重新读取")
        if self._cancellation_settlement_pending(invocation_uuid):
            raise ValueError("原调用已进入取消结算或历史核实，请继续原结算，不能恢复普通物料补报")
        if self.workflow.invocation_held(invocation_uuid) or self.workflow.execution_held(item["workflow_task_uuid"]):
            raise ValueError("原调用或执行处于恢复保护，须先完成核实")
        self.authority.resume_export(invocation_uuid, phase)
        return next(i for i in self.export_attention_items() if i["decision_id"] == item["decision_id"])

    def pump(self) -> None:
        """单条错误隔离；崩溃只重试映射/证据，不重新执行 Task 或 Job。"""
        sources = self.workflow.list_center_material_exports(after_invocation_uuid=self._source_cursor)
        self._source_cursor = sources[-1]["invocation_uuid"] if sources else ""
        for source in sources:
            identity = source["invocation_uuid"]
            if self._cancellation_settlement_pending(identity):
                continue
            if self.workflow.invocation_held(identity) or self.workflow.execution_held(source["local_task_uuid"]):
                continue
            if not self.authority.can_attempt_export(identity, "prepare"):
                continue
            try:
                self.authority.begin_export(source)
                from unilabos.app.scheduler.inventory.outbound import verify_output_protection
                verify_output_protection(self.authority.store, source)
                self._register_produced_outputs(source)
                self.authority.prepare(source, origins=self.origins(source) if self.origins else None)
            except SessionFencedError:
                raise
            except (CenterClientError, CenterTransportError, ValueError, StoreConflict) as error:
                self._failed(identity, error, phase="prepare", local_task_uuid=source["local_task_uuid"])
        intents = self.authority.pending_intents(after_invocation_uuid=self._intent_cursor)
        self._intent_cursor = intents[-1]["invocation_uuid"] if intents else ""
        for intent in intents:
            identity = intent["invocation_uuid"]
            if self._cancellation_settlement_pending(identity):
                continue
            if self.workflow.invocation_held(identity) or self.workflow.execution_held(intent["local_task_uuid"]):
                continue
            if not self.authority.can_attempt_export(identity, "evidence"):
                continue
            try:
                from unilabos.app.scheduler.inventory.outbound import verify_output_protection
                verify_output_protection(self.authority.store, {"invocation_uuid":identity,
                    "local_task_uuid":intent["local_task_uuid"],"outputs":intent["source"]["outputs"]}, verify_versions=False)
                if intent["evidence"] is None:
                    response = intent["response"]
                    if response is None:
                        if intent["request"]["items"]:
                            response = self.client.allocate_material_lineages(identity, intent["request"], self.authority.event_epoch_uuid)
                        else:
                            # 合法空 many 输出无需分配身份；仍持久化空映射以恢复跨库提交。
                            response = {"station_invocation_uuid": identity, "owner_instance_uuid": self.authority.instance_uuid,
                                "task_payload_digest": intent["request"]["task_payload_digest"],
                                "request_digest": digest(intent["request"]), "items": []}
                    receipt = self.authority.remember_response(identity, response)
                    verify_output_protection(self.authority.store, {"invocation_uuid":identity,
                        "local_task_uuid":intent["local_task_uuid"],"outputs":intent["source"]["outputs"]})
                    refs = [item["material_ref"] for item in receipt["items"]]
                    if refs and self.handoffs is None:
                        raise ValueError("物料输出尚未配置容器绑定权威")
                    containers = self.handoffs.export_output_containers(refs) if refs else []
                    for entry in containers:
                        entry["center_snapshot"] = self.client.container(entry["container_uuid"], self.authority.event_epoch_uuid)
                    evidence = self.authority.record_boundary(identity, containers, handoff_authority=self.handoffs)
                else:
                    evidence = intent["evidence"]
                self.workflow.record_center_outcome_evidence(invocation_uuid=identity, task_uuid=intent["local_task_uuid"],
                    owner_instance_uuid=self.authority.instance_uuid, task_payload_digest=intent["request"]["task_payload_digest"], evidence=evidence)
                self.authority.acknowledge(identity)
                self.errors.pop(identity, None)
            except SessionFencedError:
                raise
            except (CenterClientError, CenterTransportError, ValueError, StoreConflict) as error:
                self._failed(identity, error, phase="evidence", local_task_uuid=intent["local_task_uuid"])
        self._publish_boundaries()

    def _register_produced_outputs(self, source: dict[str, Any]) -> None:
        """运行时产物沿用持久容器登记协议；断网恢复重放原申请，不重新产样。"""
        from unilabos.app.center_client.container_registration import ContainerRegistrationBridge

        if self.handoffs is None:
            return
        registration = ContainerRegistrationBridge(self.client, self.handoffs)
        for material in self.authority.produced_registration_facts(source["outputs"]):
            existing = registration.registration(material["local_material_uuid"])
            if existing is not None and existing["status"] == "registered":
                continue
            registration.ensure(
                local_carrier_material_uuid=material["local_material_uuid"],
                barcode_namespace="production:" + self.authority.station_uuid,
                barcode=material["barcode"], evidence_refs=material["evidence_refs"],
            )

    def _cancellation_settlement_pending(self, identity: str) -> bool:
        return any(callable(lookup) and lookup(identity) is not None for lookup in (
            getattr(self.workflow, 'center_material_settlement', None),
            getattr(self.workflow, 'center_recorded_cancellation', None),
        ))

    def _publish_boundaries(self) -> None:
        boundaries = self.authority.pending_boundaries(after_invocation_uuid=self._boundary_cursor)
        self._boundary_cursor = boundaries[-1]["invocation_uuid"] if boundaries else ""
        for boundary in boundaries:
            identity = boundary["invocation_uuid"]
            if self._cancellation_settlement_pending(identity):
                continue
            if self.workflow.invocation_held(identity) or self.workflow.execution_held(boundary["local_task_uuid"]):
                continue
            if not self.authority.can_attempt_export(identity, "boundary"):
                continue
            try:
                receipt = self.client.publish_material_boundary(identity, boundary["request"], self.authority.event_epoch_uuid)
                self.authority.acknowledge_boundary(identity, receipt)
                self.errors.pop(identity, None)
            except SessionFencedError:
                raise
            except (CenterClientError, CenterTransportError, ValueError, StoreConflict) as error:
                self._failed(identity, error, phase="boundary", local_task_uuid=boundary["local_task_uuid"])

    def _failed(self, invocation: str, error: Exception, *, phase: str, local_task_uuid: str) -> None:
        reason = str(error)[:500]
        failure = self.authority.record_failure(invocation, reason, phase=phase, local_task_uuid=local_task_uuid,
            retryable=isinstance(error, CenterTransportError) and error.retryable,
            retry_after_seconds=getattr(error, "retry_after_seconds", None),
            error_type=type(error).__name__, error_code=getattr(error, "code", None))
        self.errors[invocation] = failure["last_error"]
        logger.warning("物料证据补报失败 invocation=%s phase=%s status=%s attempts=%s next_attempt_ms=%s error=%s reason=%s",
            invocation, phase, failure["status"], failure["attempts"], failure["next_attempt_ms"], type(error).__name__, failure["last_error"])
