"""可显式启用的中心轮询者：持久通知、公共工作流入口及事实补报。"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any, Mapping
from uuid import UUID

from unilabos.app.center_client.client import CenterClient, CenterClientError, SessionFencedError, TaskPackage
from unilabos.app.center_client.config import CenterClientConfig
from unilabos.app.center_client.digest import digest, is_digest
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.transport import CenterRequestHeldError, CenterTransportError
from unilabos.app.scheduler.inventory.material_inputs import (
    MaterialInputError,
    MaterialInputPending,
    StationMaterialInputAuthority,
)
from unilabos.center_protocol.commands import validate_cancellation
from unilabos.center_protocol.material_input import MaterialClaimConfirmationRequired

logger = logging.getLogger(__name__)
MAX_SEQUENCE = 2**53 - 1
UNCLAIMABLE_CODES = frozenset({
    "INVALID_STATE_TRANSITION", "WORKFLOW_REVISION_NOT_APPROVED", "WORKFLOW_REVISION_UNAVAILABLE",
    "INVOCATION_OWNER_CONFLICT", "PAYLOAD_DIGEST_MISMATCH", "TASK_CANCELED",
    "RECOVERY_RECONCILIATION_REQUIRED", "RECOVERY_EXECUTION_HELD",
})


def _uuid(value: Any) -> None:
    try:
        if str(UUID(value)) != value:
            raise ValueError("非规范 UUID")
    except (ValueError, TypeError, AttributeError) as error:
        raise CenterClientError("命令 UUID 无效") from error


def validate_command_page(body: Mapping[str, Any], station_uuid: str, cursor: int) -> list[dict[str, Any]]:
    """完整验证一页后才允许持久化或 ACK，不能信任服务端游标来跳号。"""
    if (set(body) != {"commands", "has_more", "next_sequence"}
            or not isinstance(body["commands"], list) or len(body["commands"]) > 100
            or type(body["has_more"]) is not bool or type(body["next_sequence"]) is not int):
        raise CenterClientError("命令分页结构无效")
    expected = cursor + 1
    identifiers: set[str] = set()
    for envelope in body["commands"]:
        if not isinstance(envelope, dict) or set(envelope) != {
            "command", "command_digest", "disposition", "disposition_revision"
        }:
            raise CenterClientError("命令信封结构无效")
        command = envelope["command"]
        if not isinstance(command, dict) or set(command) != {
            "protocol_version", "type", "command_uuid", "command_sequence", "station_uuid", "payload"
        }:
            raise CenterClientError("命令正文结构无效")
        _uuid(command["command_uuid"])
        _uuid(command["station_uuid"])
        if (command["command_uuid"] in identifiers
                or command["station_uuid"] != station_uuid or command["protocol_version"] != "1.0"
                or type(command["command_sequence"]) is not int
                or command["command_sequence"] != expected or expected > MAX_SEQUENCE
                or not isinstance(command["type"], str) or not isinstance(command["payload"], dict)
                or envelope["disposition"] not in {"active", "canceled", "recovery_held"}
                or type(envelope["disposition_revision"]) is not int
                or not 0 <= envelope["disposition_revision"] <= MAX_SEQUENCE
                or not is_digest(envelope["command_digest"])
                or digest(command) != envelope["command_digest"]):
            raise CenterClientError("命令身份、序列、摘要或处置版本无效")
        identifiers.add(command["command_uuid"])
        expected += 1
    if body["next_sequence"] != expected - 1 or (body["has_more"] and not body["commands"]):
        raise CenterClientError("命令分页游标与实际连续记录不匹配")
    return body["commands"]


def submission(package: TaskPackage) -> dict[str, Any]:
    """唯一公共提交参数；恢复时必须保留同一请求指纹。"""
    return {
        "backend_task_uuid": package.global_task_uuid,
        "invocation_key": package.raw["invocation_key"],
        "workflow_name": None,
        "workflow_id": package.raw["workflow_uuid"],
        "revision_fingerprint": package.raw["revision_fingerprint"],
        "input_value": dict(package.input),
    }


class CenterRuntime:
    """一次轮询可独立测试；停止先等待网络调用结束，再关闭本地存储。"""

    def __init__(self, config: CenterClientConfig, client: CenterClient,
                 store: DurableCenterStore, workflow_service: Any, *, handoffs: Any = None) -> None:
        self.config, self.client, self.store, self.service = config, client, store, workflow_service
        client.install_request_holds(store)
        self.handoffs = handoffs
        self._install_material_authorities()
        self.materials: Any = None
        self.transport_preparations: Any = None
        self.handoff_operations: Any = None
        self._wake = threading.Event()
        self._retry_wake = threading.Event()
        self._notifications: Any = None
        self._bridge_attention_state: tuple[Any, ...] | None = None
        self._native_attention_state: tuple[Any, ...] | None = None
        self._native_attention_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_lock = threading.RLock()
        self._evidence_cursor = 0
        self._command_cursor = 0
        self._ack_cursor = 0
        self._recovery_cursor = ""
        self._local_recovery_failed: set[str] = set()
        self._local_recovery_cache: dict[str, dict[str, Any]] = {}
        self._capacity_reasons: tuple[str, ...] = ()
        from unilabos.app.center_client.reconciliation import ReconciliationRecovery

        self.reconciliations = ReconciliationRecovery(client, store, workflow_service)
        old_session = store.session()
        if old_session is not None:
            client.restore_evidence_session(old_session)

    def _finish_local_recovery(self, state: dict[str, Any]) -> None:
        identity = state["invocation_uuid"]
        package = self.store.package(identity)
        failed = False
        if package is not None and self.store.claim_confirmed(identity):
            failed = self.store.reject_submission(package, state["command_uuid"], code="admission_retry_exhausted",
                                                  message="工站未创建任务，本地恢复已达到总尝试次数上限")
        self._local_recovery_cache[identity] = self.store.finish_local_recovery(state, failed=failed)
        logger.error("单调用本地恢复已停止 invocation=%s attempts=%s disposition=%s", identity, state["attempts"],
                     "admission_failed" if failed else "needs_review")

    def _local_recovery_allowed(self, identity: str, command_uuid: str | None = None) -> bool:
        state = self._local_recovery_cache.get(identity)
        if state is None:
            return identity not in self._local_recovery_failed
        if state["status"] == "exhausted":
            self._finish_local_recovery(state)
            state = self._local_recovery_cache[identity]
        if state["status"] in {"needs_review", "failed"}:
            if command_uuid is not None:
                self.store.finish_command(command_uuid, error="LOCAL_RECOVERY:" + json.dumps(state))
            return False
        return identity not in self._local_recovery_failed and int(time.time() * 1000) >= state["next_attempt_at_ms"]

    def _local_recovery_failure(self, identity: str, error: Exception, command_uuid: str | None = None) -> None:
        self._local_recovery_failed.add(identity)
        state = self.store.record_local_failure(identity, command_uuid=command_uuid,
            limit=self.config.local_recovery_max_attempts, max_backoff_seconds=self.config.max_backoff_seconds,
            code=getattr(error, "code", type(error).__name__))
        self._local_recovery_cache[identity] = state
        logger.warning("单调用本地恢复失败 invocation=%s attempt=%s/%s code=%s", identity,
                       state["attempts"], state["limit"], state["last_error_code"])
        if state["status"] == "exhausted":
            self._finish_local_recovery(state)

    def _recover(self) -> None:
        packages = self.store.packages(after_invocation_uuid=self._recovery_cursor)
        if not packages and self._recovery_cursor:
            packages = self.store.packages()
        if not packages:
            self._recovery_cursor = ""
        for package in packages:
            self._recovery_cursor = package.station_invocation_uuid
            if self.store.uncreated_terminal(package.station_invocation_uuid) or self.store.invocation_held(package.station_invocation_uuid):
                continue
            if not self._local_recovery_allowed(package.station_invocation_uuid):
                continue
            try:
                task = self.service.recover_center_workflow_invocation(package.station_invocation_uuid)
            except Exception as error:
                from unilabos.workflow.service import WorkflowError
                if not isinstance(error, WorkflowError):
                    raise
                self._local_recovery_failure(package.station_invocation_uuid, error)
            else:
                if task is not None:
                    self.store.resolve_local_recovery(package.station_invocation_uuid)
                    self._local_recovery_cache.pop(package.station_invocation_uuid, None)

    def _flush(self) -> None:
        if self.transport_preparations is not None:
            try:
                self.transport_preparations.pump()
            except SessionFencedError:
                raise
            except (CenterClientError, CenterTransportError, ValueError) as error:
                logger.warning("车辆准备补报尚未完成，保留持久事实并继续其它事件：%s", type(error).__name__)
        self.store.capture_transport_pickups()
        for _invocation, event in self.store.events():
            if self._stop.is_set():
                break
            try:
                application = self.client.report(event)
            except CenterRequestHeldError:
                logger.error("事件请求已暂停重传，原信封及序号保留等待核实")
                continue
            except CenterTransportError:
                if event["event_type"] != "admission_failed":
                    raise
                logger.warning("受理失败尚未获中心确认，保留原事件等待补报 invocation=%s", event["station_invocation_uuid"])
                continue
            if application not in {"applied", "duplicate", "conflict"}:
                raise CenterClientError("事件未获精确成功确认，保留持久正文")
            self.store.acknowledge_event(event["event_uuid"], application)
            if application == "conflict":
                logger.error("中心已保存冲突证据，原事件已隔离等待人工核实")

    def _pump_handoffs(self) -> None:
        if self.handoffs is None:
            return
        try:
            self.handoffs.pump()
        except SessionFencedError:
            raise
        except (CenterClientError, CenterTransportError, ValueError) as error:
            # 独立任务不依赖交接配置；保留原 intent/信封/回执，不能丢弃或重建。
            logger.warning("交接补报尚未完成，持久事实保留待恢复：%s", type(error).__name__)

    def _pump_materials(self) -> None:
        if self.materials is None:
            return
        try:
            self.materials.pump()
        except SessionFencedError:
            raise
        except (CenterClientError, CenterTransportError, ValueError) as error:
            logger.warning("物料证据补报尚未完成，持久事实保留待恢复：%s", type(error).__name__)

    def _sync_catalog(self, *, force: bool = False) -> None:
        from unilabos.app.center_client.catalog import projected_snapshot

        exporter = getattr(self.service, "export_published_workflow_catalog", None)
        if exporter is None:
            if force:
                raise CenterClientError("工站尚未提供完整目录导出，不能确认重同步命令")
            return
        pending = self.store.pending_catalog_upload()
        if pending is not None:
            snapshot = {key: [entry for chunk in pending["chunks"] for entry in chunk[key]]
                        for key in ("contracts", "tombstones")}
        else:
            exported = exporter()
            contracts, tombstones = [], list(exported["tombstones"])
            for contract in exported["contracts"]:
                reason = None
                if contract.get("transport_allocation") is not None:
                    manager = self.transport_preparations
                    if manager is None or manager.routes is None or not manager.routes.installed():
                        reason = "物流车辆预占或接驳映射当前未安装"
                if contract.get("handoff_operations") is not None:
                    bridge = self.handoffs
                    if (self.handoff_operations is None or bridge is None
                            or not getattr(bridge, "operation_driven", False) or bridge.incoming_plans is None):
                        reason = "工站交接操作或接收策略当前未安装"
                if reason:
                    tombstones.append({"workflow_uuid": contract["workflow_uuid"], "reason": reason})
                else:
                    contracts.append(contract)
            snapshot = projected_snapshot({"contracts": contracts, "tombstones": tombstones})
        receipt = self.client.current_catalog(self.store.event_epoch_uuid)
        upload = self.store.catalog_upload(snapshot, receipt, force=force)
        if upload is not None:
            committed = self.client.upload_catalog(upload, self.store.event_epoch_uuid)
            self.store.acknowledge_catalog(committed)
            if self._notifications is not None:
                self._notifications.notify_catalog_changed(committed)

    def _process(self, envelope: dict[str, Any]) -> None:
        self.store.require_execution()
        command = envelope["command"]
        command_uuid = command["command_uuid"]
        if envelope["disposition"] == "recovery_held" or self.store.command_held(envelope):
            self.store.finish_command(command_uuid, error="RECOVERY_EXECUTION_HELD")
            return
        if envelope["disposition"] == "canceled":
            if command["type"] == "task.available":
                self.store.reject_unclaimed_material_input(command["payload"].get("station_invocation_uuid", ""))
            self.store.finish_command(command_uuid)
            return
        if command["type"] in {"transport.prepare", "transport.preparation.cancel", "transport.release"}:
            if self.transport_preparations is not None and self.transport_preparations.handle(command):
                self.store.finish_command(command_uuid)
            return
        if command["type"] in {"handoff.target_prepare", "handoff.source_release", "handoff.target_receive", "handoff.target_renew"}:
            try:
                if self.handoff_operations is not None and self.handoff_operations.handle(command):
                    self.store.finish_command(command_uuid)
            except SessionFencedError:
                raise
            except (CenterClientError, CenterTransportError, ValueError) as error:
                logger.warning("交接操作等待配置、物理事实或重试，保留原请求：%s %s", command_uuid, type(error).__name__)
            return
        if command["type"] in {"handoff.available", "handoff.changed"}:
            # 本地配置尚未启用时保留待处理命令，不能把可靠 ACK 当作业务执行完成。
            try:
                if self.handoffs is not None and self.handoffs.handle_command(envelope):
                    self.store.finish_command(command_uuid)
            except SessionFencedError:
                raise
            except (CenterClientError, CenterTransportError, ValueError) as error:
                logger.warning("交接通知待核实或重试，保留收件箱：%s %s", command_uuid, type(error).__name__)
            return
        if command["type"] == "task.cancel":
            self._cancel(command)
            return
        if command["type"] != "task.available":
            self.store.finish_command(command_uuid, error="当前运行时只支持 task.available；需要人工处理")
            logger.error("中心命令类型尚不支持，已保留收件箱记录")
            return
        payload = command["payload"]
        if set(payload) != {"station_invocation_uuid", "global_task_uuid", "task_package_path", "payload_digest"}:
            raise CenterClientError("任务通知字段无效")
        _uuid(payload["station_invocation_uuid"])
        _uuid(payload["global_task_uuid"])
        invocation = payload["station_invocation_uuid"]
        if (payload["task_package_path"] != f"/api/v1/station-invocations/{invocation}/task-package"
                or not is_digest(payload["payload_digest"])):
            raise CenterClientError("任务通知地址或摘要无效")
        if self.store.uncreated_terminal(invocation):
            self.store.finish_command(command_uuid)
            return
        package = self.client.task_package(invocation, self.store.event_epoch_uuid)
        if package.global_task_uuid != payload["global_task_uuid"] or package.payload_digest != payload["payload_digest"]:
            raise CenterClientError("冻结任务包与通知不一致")
        if package.raw["artifact_refs"]:
            raise CenterClientError("当前运行时尚不支持携带附件引用的任务")
        package = self.store.remember_package(package)
        if package.raw.get('transport_binding') is not None:
            if self.transport_preparations is None:
                raise CenterClientError('当前物流工站未装配车辆预占权威')
            self.transport_preparations.commit(package)
        issue = self.store.material_input_issue(invocation)
        if issue is not None:
            if issue["receipt"] is not None:
                self.store.finish_command(command_uuid, error="MATERIAL_INPUT_REVIEW_REQUIRED")
            return
        task = self.service.find_station_workflow_submission(**submission(package))
        if task is None and not self.store.claim_confirmed(invocation) and self.capacity_status()["new_claim_paused"]:
            # 不创建新 claim/库存占用；已有 Task 和已确认领取的原调用仍可恢复。
            return
        try:
            claim_recovered = self._prepare_material_input(package, task)
        except MaterialInputPending:
            logger.info("目标交接投影尚未同步，保留原通知等待")
            return
        except MaterialInputError as error:
            self.store.remember_material_input_issue(package, str(error))
            logger.warning("目标物料输入校验失败，已持久记录等待中心核实")
            return
        if not claim_recovered and (task is None or not (self.store.claim_confirmed(invocation)
                                or self.store.reconciled_existing_task(invocation, task["uuid"]))):
            # 即使磁盘保留旧领取意图，新增本地执行仍必须由当前有效控制会话确认。
            self.client.claim(invocation, package.payload_digest, self.store.event_epoch_uuid)
            self.store.save_session(self.client.session)
            self.store.confirm_claim(invocation)
        if task is None:
            try:
                self.service.submit_station_workflow(
                    **submission(package), center_invocation_uuid=invocation,
                )
            except MaterialInputPending:
                return
            except MaterialInputError as error:
                self.store.remember_material_input_issue(package, str(error))
                return
            except Exception as error:
                from unilabos.workflow.service import WorkflowError
                if not isinstance(error, WorkflowError) or error.code not in {
                    "invalid_input", "not_found", "workflow_revision_conflict", "inventory_admission_failed",
                    "workflow_executor_unavailable", "preflight_failed", "material_shortage_unhandled", "invalid_control_region",
                }:
                    raise
                from unilabos.app.center_client.admission import public_reason
                reason = public_reason(error.code, error.message)
                if self.store.reject_submission(package, command_uuid, code=error.code, message=reason):
                    logger.warning("中心调用受理失败 invocation=%s code=%s reason=%s", invocation, error.code, reason)
                    return
                # 创建已在另一个写事务提交：恢复原 Task，绝不伪造受理失败。
                self.service.recover_center_workflow_invocation(invocation)
        else:
            self.service.recover_center_workflow_invocation(invocation)
        if self.transport_preparations is not None and package.raw.get('transport_binding') is not None:
            existing = self.service.find_station_workflow_submission(**submission(package))
            if existing is None:
                raise CenterClientError('已领取搬运调用缺少真实本地 Task')
            self.transport_preparations.adopt(package,existing['uuid'])
        self.store.finish_command(command_uuid)

    def _cancel(self, command: dict[str, Any]) -> None:
        """取消原调用的现存 Task；可靠收件确认不代替设备终态。"""
        try:
            payload = validate_cancellation(command["payload"])
        except ValueError as error:
            raise CenterClientError(str(error)) from error
        package = self.store.package(payload["station_invocation_uuid"])
        if package is None:
            # 领取成功、原任务尚未落地的窗口保留通知，不能凭取消命令新建执行。
            return
        if package.global_task_uuid != payload["global_task_uuid"]:
            raise CenterClientError("取消通知与原任务身份不匹配")
        task = self.service.find_station_workflow_submission(**submission(package))
        if task is None:
            if self.store.claim_confirmed(package.station_invocation_uuid):
                # 原领取已确认但本地 Task 尚未提交；可信取消以未建任务的受理终态收敛。
                # reject_submission 与真实 Task 创建共用写事务，不能覆盖并发建成的任务。
                self.store.reject_submission(package, command["command_uuid"],
                    code="canceled_before_creation", message="中心已在创建本地任务前取消调用")
            else:
                snapshot = self.client.reconciliation_state(package.station_invocation_uuid, self.store.event_epoch_uuid)
                self.store.reject_canceled_before_creation(package, command["command_uuid"], snapshot)
            return
        if task["status"] not in {"succeeded", "failed", "canceled", "timeout"}:
            # 容量暂停时可能未拉取新通知；历史 ACK/补报会话不授予本地控制权。
            self.client.pull_commands(self.store.cursor(), self.store.event_epoch_uuid, limit=1)
            self.store.save_session(self.client.session)
            result = self.service.command_workflow_task(
                task["uuid"], command_type="cancel", target_node_uuid=None,
                idempotency_key=command["command_uuid"],
                description=payload["reason"].strip(),
                meta_data={"center_invocation_uuid": package.station_invocation_uuid},
            )
            if result["status"] != "succeeded":
                logger.warning("本地取消尚未成功受理，保留中心通知待核实：%s", command["command_uuid"])
                return
        self.service.recover_center_workflow_invocation(package.station_invocation_uuid)
        self.store.finish_command(command["command_uuid"])

    def _prepare_material_input(self, package: TaskPackage, task: dict[str, Any] | None) -> bool:
        """准备输入；返回本轮是否已通过当前会话核实远端原 claim。"""
        from unilabos.center_protocol.material_source import requires_material_input
        body = {"payload": dict(package.raw), "payload_digest": package.payload_digest}
        if not requires_material_input(package.raw):
            # 旧包的来源仍在原子建任务计划中验证；领取后缺绑定以受理失败结束，
            # 不能向没有物料引用的调用发送无法确认的输入异常。
            return False
        if self.handoffs is None:
            raise MaterialInputPending("目标站尚未配置真实库存交接权威")
        authority = StationMaterialInputAuthority(self.handoffs.authority)
        proof = self.store.material_input(package.station_invocation_uuid)
        if proof is not None:
            terminal = task is not None and task["status"] in {"succeeded", "failed", "canceled", "timeout"}
            authority.verify(body, proof["receipt"], allow_released=terminal and proof["state"] == "released")
            if task is not None and task["uuid"] != proof["receipt"]["local_task_uuid"]:
                raise MaterialInputError("现存 Task 与原库存占用固定身份不同")
            if task is None and proof["state"] not in {"prepared", "claimed"}:
                raise MaterialInputError("已经结束的占用不能创建新 Task")
        elif task is not None:
            raise MaterialInputError("现存物料 Task 的库存占用证明缺失，禁止补造")
        claim_recovered = False
        if task is None:
            try:
                try:
                    inputs = self.service.center_material_input_items(body)
                except MaterialClaimConfirmationRequired:
                    if proof is None or self.store.claim_confirmed(package.station_invocation_uuid):
                        raise
                    # 只读核实远端 owner 后才重放；未领取调用不能借历史合同首次领取。
                    authority.verify(body, proof["receipt"], require_current=True)
                    identity = package.station_invocation_uuid
                    snapshot = self.client.reconciliation_state(identity, self.store.event_epoch_uuid)
                    if (snapshot["state"]["owner_instance_uuid"] != self.config.instance_uuid
                            or snapshot["payload_digest"] != package.payload_digest):
                        raise MaterialInputError("远端原领取与本地冻结输入不匹配")
                    if not self.client.claim(identity, package.payload_digest, self.store.event_epoch_uuid):
                        raise CenterClientError("历史合同恢复必须确认原领取重放")
                    self.store.confirm_claim(identity)
                    claim_recovered = True
                    inputs = self.service.center_material_input_items(body)
                receipt = authority.prepare(body, input_materials=inputs)
            except MaterialInputError:
                raise
            except ValueError as error:
                raise MaterialInputError(str(error)) from error
            self.store.remember_material_input(package, receipt)
        return claim_recovered

    def _pump_material_input_issues(self) -> None:
        for issue in self.store.pending_material_input_issues():
            identity = issue["invocation_uuid"]
            if self.store.invocation_held(identity):
                continue
            try:
                receipt = self.client.report_material_input_issue(identity, issue["request"], self.store.event_epoch_uuid)
                self.store.acknowledge_material_input_issue(identity, receipt)
            except SessionFencedError:
                raise
            except (CenterClientError, CenterTransportError, ValueError) as error:
                logger.warning("物料输入异常补报待重试：%s %s", identity, type(error).__name__)

    def _release_material_inputs(self) -> None:
        if self.handoffs is None:
            return
        for entry in self.store.releasable_material_inputs():
            identity = entry["receipt"]["station_invocation_uuid"]
            if self.store.invocation_held(identity):
                continue
            try:
                authority = StationMaterialInputAuthority(self.handoffs.authority)
                authority.release(entry["package"], entry["receipt"])
                self.store.finish_material_input_release(entry["receipt"])
            except ValueError as error:
                logger.warning("物料输入占用释放待核实：%s %s", identity, type(error).__name__)

    def _recover_evidence_only(self) -> None:
        """只使用原会话和原信封，不调用工作流、物料桥、交接桥或目录同步。"""
        from unilabos.app.center_client.event_history import EventHistoryRecovery, EventHistoryStore

        history = EventHistoryStore(self.store.path, station_uuid=self.config.station_uuid,
                                    instance_uuid=self.config.instance_uuid)
        try:
            recovery = EventHistoryRecovery(self.client, history, retain_missing=True)
            sequences = history.sequences(after_sequence=self._evidence_cursor)
            if not sequences:
                self._evidence_cursor = 0
                return
            for sequence in sequences:
                if self._stop.is_set():
                    return
                self._evidence_cursor = sequence
                recovery.recover(sequence)
        finally:
            history.close()

    def poll_once(self) -> None:
        with self._poll_lock:
            self._local_recovery_failed.clear()
            if self.store.recovery_status()["mode"] == "evidence_only":
                self._recover_evidence_only()
                return
            if self._bridge_attention_state is None:
                self._bridge_attention_state = self._read_bridge_attention_state()
            self._local_recovery_cache = {s["invocation_uuid"]: s for s in self.store.local_recovery_states()}
            try:
                # 已落地任务的历史证据补报不依赖重新取得控制权。
                self._recover()
                self._pump_materials()
                self._release_material_inputs()
                self._flush()
                self._pump_handoffs()
                if self._stop.is_set():
                    return
                cursor = self.store.cursor()
                if not self.capacity_status()["new_claim_paused"]:
                    body = self.client.pull_commands(cursor, self.store.event_epoch_uuid)
                    for envelope in validate_command_page(body, self.config.station_uuid, cursor):
                        self.store.receive(envelope)
                acknowledgements = self.store.commands(unacknowledged=True, after_sequence=self._ack_cursor)
                if not acknowledgements and self._ack_cursor:
                    acknowledgements = self.store.commands(unacknowledged=True)
                if not acknowledgements:
                    self._ack_cursor = 0
                for envelope in acknowledgements:
                    if self._stop.is_set():
                        return
                    command = envelope["command"]
                    self._ack_cursor = command["command_sequence"]
                    try:
                        self.client.ack_command(command["command_uuid"], command["command_sequence"],
                                                envelope["command_digest"], self.store.event_epoch_uuid)
                    except CenterRequestHeldError:
                        logger.error("命令确认请求已暂停重传，保留原通知并继续其他命令")
                        continue
                    self.store.acknowledge_command(command["command_uuid"])
                catalog_requests: list[str] = []
                commands = self.store.commands(after_sequence=self._command_cursor, acknowledged_only=True)
                if not commands and self._command_cursor:
                    commands = self.store.commands(acknowledged_only=True)
                if not commands:
                    self._command_cursor = 0
                for envelope in commands:
                    if self._stop.is_set():
                        break
                    command = envelope["command"]
                    self._command_cursor = command["command_sequence"]
                    if self.store.command_held(envelope):
                        self.store.finish_command(command["command_uuid"], error="RECOVERY_EXECUTION_HELD")
                        continue
                    if command["type"] == "catalog.sync_requested" and envelope["disposition"] == "active":
                        payload = command["payload"]
                        if (set(payload) != {"reason"} or not isinstance(payload["reason"], str)
                                or not payload["reason"].strip()
                                or len(payload["reason"].encode("utf-8")) > 500):
                            self.store.finish_command(command["command_uuid"], error="INVALID_CATALOG_SYNC_REQUEST")
                            logger.error("目录重同步请求正文无效，已保留待人工核实")
                        else:
                            # 目录网络操作在任务处理及事件补报后执行，断网不能挡住
                            # 同一批中后续已可领取的独立调用；成功才标记命令已处理。
                            catalog_requests.append(command["command_uuid"])
                        continue
                    if command["type"] == "task.available" and not self._local_recovery_allowed(
                            command["payload"].get("station_invocation_uuid", ""), command["command_uuid"]):
                        continue
                    try:
                        self._process(envelope)
                    except CenterRequestHeldError:
                        logger.error("通知对应请求已暂停重传，保留原收件箱等待核实")
                        continue
                    except CenterTransportError as error:
                        if error.code in {"DEPENDENCY_NOT_READY", "RESULT_AMENDMENT_REVIEW_REQUIRED"}:
                            # ready 之后上游仍可能出现待核实事实。保留原调用与收件箱，
                            # 后续继续领取同一身份；本批其他独立分支仍应得到处理。
                            logger.info("中心依赖尚未就绪，保留原通知等待核实")
                            continue
                        if error.retryable or error.code not in UNCLAIMABLE_CODES:
                            raise
                        if error.code in {"TASK_CANCELED", "INVALID_STATE_TRANSITION", "INVOCATION_OWNER_CONFLICT"}:
                            self.store.reject_unclaimed_material_input(command["payload"].get("station_invocation_uuid", ""))
                        # ACK 后取消或撤销也必须隔离；不能让一条旧命令阻塞其他独立调用。
                        self.store.finish_command(envelope["command"]["command_uuid"], error=error.code)
                        logger.warning("中心拒绝执行，命令已保留待人工核实：%s", error.code)
                    except SessionFencedError:
                        raise
                    except CenterClientError:
                        self.store.finish_command(envelope["command"]["command_uuid"], error="INVALID_COMMAND_OR_TASK_PACKAGE")
                        logger.error("中心通知或任务包不能安全执行，已保留待人工核实")
                    except Exception as error:
                        from unilabos.workflow.service import WorkflowError

                        if isinstance(error, WorkflowError) and command["type"] == "task.available":
                            self._local_recovery_failure(command["payload"].get("station_invocation_uuid", ""),
                                                         error, command["command_uuid"])
                            continue
                        if not isinstance(error, WorkflowError) or error.code not in {
                            "workflow_revision_conflict", "invalid_input", "not_found",
                        }:
                            raise
                        self.store.finish_command(envelope["command"]["command_uuid"], error=error.code)
                        logger.error("本地冻结合同或输入无法执行，已保留待人工核实：%s", error.code)
                self._pump_materials()
                self._release_material_inputs()
                self._flush()
                self._pump_material_input_issues()
                self._pump_handoffs()
                if not self._stop.is_set():
                    self._sync_catalog(force=bool(catalog_requests))
                    for command_uuid in catalog_requests:
                        self.store.finish_command(command_uuid)
            finally:
                self.store.save_session(self.client.session)
                self._notify_bridge_attention_changes()

    def _notify_native_attention_changes(self) -> None:
        """在提交线程过滤纯本地事项变化，WSS 线程只接收轻量唤醒。"""
        reader = getattr(self.service, "center_attention_state", None)
        if not callable(reader):
            self.notify_attention_changed()
            return
        # 原生入口均在工作流事务提交后回调；此处只按通知锁→存储读锁取锁，
        # 不取得轮询锁或调度锁。读取和基线更新必须串行，避免迟到旧读覆盖新状态。
        with self._native_attention_lock:
            state = reader()
            if state == self._native_attention_state:
                return
            self._native_attention_state = state
        self.notify_attention_changed()

    def _read_bridge_attention_state(self) -> tuple[Any, ...]:
        """只读取库存桥的小状态字段，不组装完整事项或物料正文。"""
        return tuple(reader() if callable(reader := getattr(bridge, "attention_state", None)) else None
                     for bridge in (self.materials, self.handoffs))

    def _notify_bridge_attention_changes(self) -> None:
        """轮询只比较桥的小状态；无变化时不能触发中心周期拉取。"""
        state = self._read_bridge_attention_state()
        previous = self._bridge_attention_state
        self._bridge_attention_state = state
        if previous is not None and state != previous:
            self.notify_attention_changed()

    def center_attention_snapshot(self) -> dict[str, Any]:
        """原生事项与持久物料责任合并；不修改 Task/Job 执行投影。"""
        from copy import deepcopy
        provider = getattr(self.service, "center_attention_snapshot", None)
        snapshot = deepcopy(provider()) if callable(provider) else {
            "manual_confirmation_count": 0, "exception_count": 0,
            "items": {"manual_confirmations": [], "exceptions": []}}
        if self.materials is not None:
            snapshot["items"]["exceptions"].extend(self.materials.export_attention_items())
        if self.handoffs is not None:
            from unilabos.app.scheduler.inventory.handoff_attention import attention_handoffs
            snapshot["items"]["exceptions"].extend(attention_handoffs(self.handoffs.authority))
        snapshot["exception_count"] = len(snapshot["items"]["exceptions"])
        return snapshot

    def notify_attention_changed(self) -> None:
        if self._notifications is not None:
            self._notifications.notify_attention_changed()

    def wake(self) -> None:
        self._wake.set()

    def capacity_status(self) -> dict[str, Any]:
        from unilabos.app.center_client.capacity import snapshot

        status = snapshot(self.store, self.config.capacity)
        reasons = tuple(status["pause_reasons"])
        if reasons != self._capacity_reasons:
            if reasons:
                logger.warning("新领取因容量暂停，已有任务与证据继续保留：%s", ",".join(reasons))
            else:
                logger.info("容量恢复，新领取可继续")
            self._capacity_reasons = reasons
        return status

    def diagnostics(self) -> dict[str, Any]:
        """按需运维聚合；全历史 Task/Job 查询不放进每次新 claim 的压力检查。"""
        return {"capacity": self.capacity_status(), "workload": self.store.workload_diagnostics(),
                "local_recovery": self.store.local_recovery_states(),
                "connection": self._notifications.statistics() if self._notifications is not None else
                {"available": False, "reason": "RECEIVER_NOT_RUNNING"}}

    def _install_material_authorities(self) -> None:
        """构造与生产延后装配共用能力接线；读取证明不会主动建立网络会话。"""
        if self.store.recovery_status()["mode"] == "evidence_only" or self.handoffs is None:
            return
        def source_session():
            self.store.require_execution()
            return self.client.session
        authority = self.handoffs.authority if self.handoffs is not None else None
        self.service.install_center_material_source_authority(authority, source_session)
        if authority is not None:
            self.service.install_center_material_input_authority(StationMaterialInputAuthority(authority))

    def install_handoff_bridge(self, factory: Callable[[], Any]) -> Any:
        """在启动前原子装配一次交接桥；生命周期检查由运行时统一负责。"""
        with self._poll_lock:
            if self._thread is not None or self.handoffs is not None:
                raise RuntimeError("交接桥必须在中心运行时启动前且仅配置一次")
            bridge = factory()
            if bridge is None:
                raise ValueError("交接桥装配不能返回空值")
            self.handoffs = bridge
            self._install_material_authorities()
            return bridge

    def install_material_bridge(self, factory: Callable[[], Any]) -> Any:
        """启动前装配物料证据桥；不以导出失败中断独立调用。"""
        with self._poll_lock:
            if self._thread is not None or self.materials is not None:
                raise RuntimeError("物料桥必须在中心运行时启动前且仅配置一次")
            bridge = factory()
            if bridge is None:
                raise ValueError("物料桥装配不能返回空值")
            self.materials = bridge
            return bridge

    def start(self) -> None:
        with self._poll_lock:
            if self._thread is not None:
                raise RuntimeError("中心运行时已经启动")
            logger.info(
                "中心恢复身份：station_uuid=%s instance_uuid=%s process_uuid=%s event_epoch_uuid=%s",
                self.config.station_uuid, self.config.instance_uuid,
                self.client.process_uuid, self.store.event_epoch_uuid,
            )
            self._thread = threading.Thread(target=self._run, name="center-runtime", daemon=True)
            self._thread.start()
            if self.config.websocket_enabled and self.store.recovery_status()["mode"] != "evidence_only":
                from unilabos.app.center_client.websocket import NotificationReceiver

                self._notifications = NotificationReceiver(
                    self.config,
                    self.client,
                    self.store,
                    self._wake.set,
                    attention_provider=self.center_attention_snapshot,
                )
                notifier = getattr(self.service, "set_attention_notifier", None)
                if callable(notifier):
                    reader = getattr(self.service, "center_attention_state", None)
                    if callable(reader):
                        with self._native_attention_lock:
                            self._native_attention_state = reader()
                    notifier(self._notify_native_attention_changes)
                self._notifications.start()

    def public_address_synced(self, value: str | None) -> bool:
        receiver = self._notifications
        return receiver is not None and receiver.public_address_synced(value)

    def reload_credentials(self, config: CenterClientConfig, *, recover_control: bool = False) -> None:
        """保留工作流与持久收件箱；恢复接入换证后重新申请控制会话。"""
        with self._poll_lock:
            if self._notifications is not None:
                self._notifications.stop()
            self.client.reload_credentials(config, recover_control=recover_control)
            self.config = config
            if self._notifications is not None:
                from unilabos.app.center_client.websocket import NotificationReceiver
                self._notifications = NotificationReceiver(config, self.client, self.store, self._wake.set,
                                                           attention_provider=self.center_attention_snapshot)
                self._notifications.start()
            self._retry_wake.set()
            self._wake.set()

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            self._retry_wake.clear()
            try:
                self.poll_once()
            except Exception as error:
                # 不把凭证、输入或远端响应正文写入日志。
                logger.warning("中心轮询未完成，持久事实保留待恢复：%s", type(error).__name__)
                pause = min(delay, self.config.max_backoff_seconds) * random.uniform(0.875, 1.125)
                if isinstance(error, CenterTransportError) and error.retry_after_seconds is not None:
                    pause = max(pause, error.retry_after_seconds)
                deadline = time.monotonic() + pause
                # 换证成功可打断旧凭据的本地退避；普通通知不影响退避，仍遵守 Retry-After。
                reset_backoff = False
                while not self._stop.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if self._retry_wake.wait(min(remaining, 3600.0)):
                        self._retry_wake.clear()
                        if not (isinstance(error, CenterTransportError) and error.retry_after_seconds):
                            reset_backoff = True
                            break
                delay = 1.0 if reset_backoff else min(delay * 2, self.config.max_backoff_seconds)
            else:
                delay = 1.0
                self._wake.wait(self.config.poll_seconds)
                self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._retry_wake.set()
        self._wake.set()
        if self._notifications is not None:
            self._notifications.stop()
        if self._thread is not None:
            self._thread.join(timeout=self.config.timeout_seconds * 3 + 1)
            if self._thread.is_alive():
                raise RuntimeError("中心轮询尚未停止，拒绝关闭工作流存储")

    def close(self) -> None:
        self.stop()
        with self._poll_lock:
            self.store.close()


def configured_runtime(database_path: Any, workflow_service: Any) -> CenterRuntime | None:
    """默认禁用；显式启用时才加载凭证和打开持久中心表。"""
    import os

    if os.environ.get("CENTER_ENABLED", "0") != "1":
        return None
    config = CenterClientConfig.from_env()
    store = DurableCenterStore(database_path, station_uuid=config.station_uuid, instance_uuid=config.instance_uuid)
    try:
        return CenterRuntime(config, CenterClient(config), store, workflow_service)
    except BaseException:
        store.close()
        raise
