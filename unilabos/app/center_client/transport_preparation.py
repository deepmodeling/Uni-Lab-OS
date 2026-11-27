"""独立车辆准备命令与已提交库存事实的持久补报。"""

from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from unilabos.app.scheduler.inventory.transport_allocation import TransportAllocations
from unilabos.center_protocol.transport_preparation import uuid, validate_request


class TransportPreparationBridge:
    """库存预占先提交，共享信封随后可重建；命令确认不构成执行许可。"""

    def __init__(self, runtime: Any, inventory_store: Any, *, routes=None):
        self.routes = routes
        self.runtime = runtime
        self.inventory_store = inventory_store
        self.allocations = TransportAllocations(inventory_store)
        if runtime.service._task_scheduler_bridge is not None:
            runtime.service._task_scheduler_bridge.transport_dispatch_guard = self.dispatch_guard

    def _wake_dispatch(self):
        """占有状态提交后唤醒真实调度器，不等待另一个无关作业完成。"""
        wake = getattr(self.runtime.service._task_scheduler_bridge, "reschedule", None)
        if callable(wake):
            wake()

    def dispatch_guard(self, binding):
        """每个设备动作前复验原路线；配置变化保留原车并阻止新物理动作。"""
        row = self.inventory_store.query_one(
            "SELECT state,binding_json FROM transport_allocation WHERE allocation_uuid=?", (binding["allocation_uuid"],)
        )
        if row is None or row["state"] not in {"committed", "adopted"} or json.loads(row["binding_json"]) != binding:
            return False
        try:
            if self.routes is None:
                raise ValueError("未安装接驳配置")
            self.routes.verify(binding)
        except (ValueError, OSError):
            self.allocations.quarantine(binding)
            return False
        return True

    def _fact(self, request, fact, binding=None, decision_uuid=None, waiting_reason=None):
        key = f"transport-preparation:{request['request_uuid']}:{request['generation']}:{fact}"
        event_uuid = str(uuid5(NAMESPACE_URL, key))
        if fact == "waiting" and waiting_reason:
            prior = self.runtime.store.latest_preparation_event(request["request_uuid"])
            event_uuid = (
                prior["event_uuid"]
                if prior and prior["fact"] == fact and prior.get("waiting_reason") == waiting_reason
                else str(uuid4())
            )
        self.runtime.store.preparation_event(
            event_uuid=event_uuid,
            request=request,
            fact=fact,
            binding=binding,
            decision_uuid=decision_uuid,
            waiting_reason=waiting_reason,
        )
        self.runtime.wake()

    def handle(self, command):
        kind, payload = command["type"], command["payload"]
        if kind == "transport.prepare":
            request = validate_request(payload)
            if request["logistics_station_uuid"] != self.runtime.store.station_uuid:
                raise ValueError("车辆准备请求不属于当前物流工站")
            if self.routes is None:
                self._fact(request, "waiting", waiting_reason="route_unconfigured")
                return False
            try:
                route = self.routes.resolve(request)
            except (ValueError, OSError):
                self._fact(request, "waiting", waiting_reason="route_unavailable")
                return False
            candidates = self.runtime.service.transport_preparation_candidates(request)
            result = self.allocations.reserve(request, candidates, route_binding=route)
            fact = (
                "waiting"
                if result.get("state") == "waiting"
                else "closed"
                if result.get("state") == "closed"
                else "reserved"
            )
            self._fact(
                request,
                fact,
                result if fact == "reserved" else None,
                waiting_reason=("fleet_busy" if candidates else "no_compatible_vehicle") if fact == "waiting" else None,
            )
            return fact != "waiting"
        if kind == "transport.preparation.cancel":
            if set(payload) != {"request_uuid", "generation", "station_invocation_uuid", "condition_digest"}:
                raise ValueError("车辆准备取消字段无效")
            self.allocations.cancel(
                request_uuid=payload["request_uuid"],
                generation=payload["generation"],
                invocation_uuid=payload["station_invocation_uuid"],
            )
            self._fact(payload, "closed")
            self._wake_dispatch()
            return True
        if kind == "transport.release":
            self._settle_local_tasks()
            if set(payload) != {
                "decision_uuid",
                "allocation_uuid",
                "generation",
                "allocation_fence",
                "authorization_uuid",
                "close_reason",
                "evidence_digest",
            }:
                raise ValueError("车辆释放决定字段无效")
            row = self.inventory_store.query_one(
                "SELECT p.request_json FROM transport_preparation p JOIN transport_allocation a "
                "ON a.request_uuid=p.request_uuid AND a.generation=p.generation WHERE a.allocation_uuid=?",
                (payload["allocation_uuid"],),
            )
            if row is None:
                raise ValueError("释放决定没有原车辆准备记录")
            self.allocations.release(payload)
            self._fact(json.loads(row["request_json"]), "released", decision_uuid=payload["decision_uuid"])
            self._wake_dispatch()
            return True
        return False

    def pump(self):
        """在相同序列中可靠补发，ACK 丢失只重放原始信封。"""
        from unilabos.app.center_client.transport import CenterRequestHeldError

        self._settle_local_tasks()
        for row in self.inventory_store.query_all(
            "SELECT p.request_json FROM transport_preparation p JOIN transport_allocation a "
            "ON a.request_uuid=p.request_uuid AND a.generation=p.generation WHERE a.state='needs_review'"
        ):
            self._fact(json.loads(row["request_json"]), "rejected")
        for event in self.runtime.store.preparation_events():
            try:
                response = self.runtime.client.report_preparation(event)
            except CenterRequestHeldError:
                continue
            self.runtime.store.acknowledge_preparation(event, response)

    def _settle_local_tasks(self):
        for row in self.inventory_store.query_all("SELECT task_uuid FROM transport_allocation WHERE state='adopted'"):
            task = self.runtime.service.get_workflow_task(row["task_uuid"])
            if task["status"] in {"succeeded", "failed", "canceled", "timeout"}:
                self.allocations.hold(row["task_uuid"], uncertain=task["status"] != "succeeded")

    def commit(self, package):
        binding = package.raw.get("transport_binding")
        if binding is not None:
            if self.routes is None:
                raise ValueError("当前物流站未安装接驳映射")
            try:
                self.routes.verify(binding)
            except ValueError:
                self.allocations.quarantine(binding)
                raise
            self.allocations.commit(binding)

    def reconcile(self, handoff_uuid, *, allocation_uuid, physical_safe, evidence_refs):
        """先证明原任务停止且没有未决设备命令，再登记操作者真实核实。"""
        row = self.inventory_store.query_one(
            "SELECT a.*,p.request_json FROM transport_allocation a JOIN transport_preparation p ON p.request_uuid=a.request_uuid AND p.generation=a.generation WHERE a.allocation_uuid=?",
            (allocation_uuid,),
        )
        if row is None or json.loads(row["request_json"])["handoff_uuid"] != handoff_uuid:
            raise ValueError("车辆占用不属于原交接")
        request = json.loads(row["request_json"])
        package = self.runtime.client.handoff_package(handoff_uuid, None, self.runtime.store.event_epoch_uuid)
        payload = package["payload"]
        if payload["attention"] or payload["status"] not in {"received", "reconciled", "canceled"}:
            raise ValueError("中心交接尚未完成核实，保留原车辆占有")
        if not row["task_uuid"]:
            original = self.runtime.store.package(request["station_invocation_uuid"])
            if original is not None:
                from unilabos.app.center_client.runtime import submission

                if original.raw.get("transport_binding") != json.loads(row["binding_json"]):
                    raise ValueError("原调用冻结的车辆绑定与待核实占用不一致")
                actual = self.runtime.service.find_station_workflow_submission(**submission(original))
                if actual is not None:
                    frozen = (
                        actual["workflow_snapshot"]["workflow"].get("meta_data", {}).get("center_transport_binding")
                    )
                    if (
                        self.runtime.store.recorded_task_uuid(request["station_invocation_uuid"]) != actual["uuid"]
                        or frozen != original.raw["transport_binding"]
                    ):
                        raise ValueError("真实任务与原调用或车辆绑定不一致")
                    self.allocations.quarantine(frozen)
                    self.allocations.restore_task_reference(frozen, actual["uuid"])
                    row = self.inventory_store.query_one(
                        "SELECT * FROM transport_allocation WHERE allocation_uuid=?", (allocation_uuid,)
                    )
        if row["task_uuid"]:
            if self.runtime.service.get_workflow_task(row["task_uuid"])["status"] not in {
                "succeeded",
                "failed",
                "canceled",
                "timeout",
            }:
                raise ValueError("原搬运任务尚未停止")
            scheduler = self.runtime.service._task_scheduler_bridge
            unresolved = getattr(scheduler, "active_or_uncertain_job_ids", None)
            own_jobs = {j["uuid"] for j in self.runtime.service.list_workflow_node_jobs(row["task_uuid"])}
            if unresolved is None or set(unresolved()) & own_jobs:
                raise ValueError("须先在工站任务异常页核实未决设备命令")
        elif not self.runtime.store.uncreated_terminal(request["station_invocation_uuid"]):
            snapshot = self.runtime.client.reconciliation_state(
                request["station_invocation_uuid"], self.runtime.store.event_epoch_uuid
            )
            self.runtime.store.fence_transport_without_task(request, allocation_uuid, snapshot)
        self.allocations.quarantine(json.loads(row["binding_json"]))
        return {
            "evidence_digest": self.allocations.record_reconciliation(
                allocation_uuid, physical_safe=physical_safe, evidence_refs=evidence_refs
            )
        }

    def adopt(self, package, task_uuid):
        binding = package.raw.get("transport_binding")
        if binding is not None:
            uuid(task_uuid)
            self.allocations.adopt(binding, task_uuid=task_uuid)
            self._wake_dispatch()
