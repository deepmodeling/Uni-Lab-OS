"""库存交接 intent 到中心公共事件账本的可恢复桥；不跨库伪造原子提交。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from unilabos.app.scheduler.inventory.handoff import StationHandoffAuthority
from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.handoff import validate_handoff_package

if TYPE_CHECKING:
    from unilabos.app.center_client.runtime import CenterRuntime
    from unilabos.app.scheduler.inventory.store import InventoryStore


def configure_handoffs(
    runtime: CenterRuntime,
    inventory_store: InventoryStore,
    *,
    lab_uuid: str,
    incoming_plans: Callable[[dict[str, Any]], Mapping[str, Mapping[str, Any]] | None] | None = None,
) -> HandoffBridge:
    """在 runtime.start() 前显式装配；模板、库位和本地操作权限由本站部署提供。

    库存连接仍由调用方持有，必须先停止 runtime 再关闭。未配置接收计划时，
    目标通知保持待处理；此入口不会注册 HTTP 路由或推断实际物料映射。
    """
    def build() -> HandoffBridge:
        authority = StationHandoffAuthority(
            inventory_store,
            station_uuid=runtime.store.station_uuid,
            instance_uuid=runtime.store.instance_uuid,
            event_epoch_uuid=runtime.store.event_epoch_uuid,
            lab_uuid=lab_uuid,
        )
        return HandoffBridge(runtime.client, runtime.store, authority, incoming_plans=incoming_plans)

    return runtime.install_handoff_bridge(build)


class HandoffBridge:
    """本地接收策略显式注入；不配置策略时仅保存通知，绝不猜测模板或库位。"""

    def __init__(
        self,
        client: Any,
        store: Any,
        authority: StationHandoffAuthority,
        *,
        incoming_plans: Callable[[dict[str, Any]], Mapping[str, Mapping[str, Any]] | None] | None = None,
    ):
        if (store.station_uuid, store.instance_uuid, store.event_epoch_uuid) != (
            authority.station_uuid,
            authority.instance_uuid,
            authority.event_epoch_uuid,
        ):
            raise ValueError("库存交接与中心事件账本身份或纪元不一致")
        self.client, self.store, self.authority = client, store, authority
        self.incoming_plans = incoming_plans
        self._intent_cursor = ""

    def authorize_transfer_plan(self, request: Mapping[str, Any], token: str) -> None:
        """先认证请求来源，再提供本站库存证明；不把公开会话 UUID 当凭据。"""
        from unilabos.center_protocol.digest import digest
        if not token or len(token) > 4096:
            raise ValueError("缺少搬运检查授权")
        self.client.authorize_transfer_plan(token, digest(request), self.store.event_epoch_uuid)

    def validate_transfer_plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """仅用当前持久控制身份检查库存，不把请求体当作会话或库存证明。"""
        from dataclasses import asdict

        from unilabos.app.scheduler.inventory.placement import validate_inventory_placement
        current = self.store.session()
        if current is None:
            raise ValueError("工站没有当前中心控制会话，不能提供可信搬运检查")
        return validate_inventory_placement(self.authority, request, session=asdict(current))

    def attention_state(self) -> tuple[Any, ...]:
        """读取交接责任的修订及本地状态，完整证明仍由详情查询按需组装。"""
        queries = (
            "SELECT handoff_uuid,revision,role,state FROM station_handoff ORDER BY handoff_uuid",
            "SELECT handoff_item_uuid,state,reservation_uuid FROM station_handoff_item ORDER BY handoff_item_uuid",
            "SELECT handoff_item_uuid,lock_key,active FROM station_handoff_resource ORDER BY handoff_item_uuid,lock_key",
            "SELECT r.uuid,r.state,r.site_active FROM station_ingress_reservation r "
                "JOIN station_handoff_item i ON i.reservation_uuid=r.uuid ORDER BY r.uuid",
            "SELECT event_uuid,application FROM station_handoff_outbox ORDER BY event_uuid",
            "SELECT event_uuid,reason FROM station_handoff_recovery_pending ORDER BY event_uuid",
        )
        with self.authority.store.read_connection() as c:
            return tuple(tuple(tuple(row) for row in c.execute(query).fetchall()) for query in queries)

    def pump(self) -> None:
        """先分号并在两库保存原信封，再补报或重放精确回执，最后结算库存。"""
        from unilabos.app.center_client.transport import CenterRequestHeldError

        intents = self.authority.pending_intents(after_event_uuid=self._intent_cursor)
        if not intents and self._intent_cursor:
            intents = self.authority.pending_intents()
        if not intents:
            self._intent_cursor = ""
        for intent in intents:
            self._intent_cursor = intent["event_uuid"]
            if self.store.event_request_held(intent["event_uuid"]):
                continue
            if self.store.event_held(intent["event_uuid"]):
                self.authority.retain_recovery_intent(intent["event_uuid"], "原事件已保管待核实，禁止推断业务应用")
                continue
            if intent["event_epoch_uuid"] != self.store.event_epoch_uuid and self.store.handoff_ack(intent["event_uuid"]) is None:
                self.authority.retain_recovery_intent(intent["event_uuid"], "原纪元缺少精确回执，禁止重新分号或新执行")
                continue
            event = self.store.remember_handoff_intent(intent)
            # 发送前保存原序号；工作流数据库若回退，不得用新序号补发旧物理事实。
            self.authority.remember_envelope(intent["event_uuid"], event)
            response = self.store.handoff_ack(event["event_uuid"])
            if response is None:
                try:
                    response = self.client.report_handoff(event)
                except CenterRequestHeldError:
                    continue
                self.store.acknowledge_handoff_event(event["event_uuid"], response)
            payload = response["handoff"]
            package = {"payload": payload, "payload_digest": digest(payload)}
            if response["ack"]["application"] == "conflict":
                self.authority.quarantine_center_snapshot(package)
            else:
                self.authority.apply_center_snapshot(package)
            self.authority.acknowledge_intent(event["event_uuid"], response)

    def handle_command(self, envelope: Mapping[str, Any]) -> bool:
        """拉取通知固定修订；返回 False 表示缺本地配置，命令必须留待处理。"""
        command = envelope["command"]
        if command["type"] not in {"handoff.available", "handoff.changed"}:
            return False
        notification = command["payload"]
        if set(notification) != {"handoff_uuid", "handoff_revision", "handoff_package_path", "payload_digest"}:
            raise ValueError("交接通知字段无效")
        identity = notification["handoff_uuid"]
        if not isinstance(identity, str) or str(UUID(identity)) != identity:
            raise ValueError("交接通知 UUID 不规范")
        revision = notification["handoff_revision"]
        if (
            type(revision) is not int
            or not 1 <= revision <= 2**53 - 1
            or notification["handoff_package_path"] != f"/api/v1/handoffs/{identity}/packages/{revision}"
        ):
            raise ValueError("交接通知路径或修订无效")
        package = self.client.handoff_package(identity, revision, self.store.event_epoch_uuid)
        package = validate_handoff_package(
            package,
            station_uuid=self.store.station_uuid,
            instance_uuid=self.store.instance_uuid,
            expected_revision=revision,
            expected_digest=notification["payload_digest"],
        )
        payload = package["payload"]
        if self.authority.has_handoff(identity):
            self.authority.apply_center_snapshot(package)
            return True
        if payload["status"] in {"canceling", "canceled"}:
            # 未曾建立保护也要持久确认无保护，并留下阻止旧包复活的取消墓碑。
            self.authority.apply_center_snapshot(package)
            return self.authority.has_handoff(identity)
        # 源放行必须通过下面的显式人工操作入口，不从中心通知推断物理可交付。
        if payload["source_station_uuid"] == self.store.station_uuid:
            return True
        if payload["status"] not in {"requested", "prepared"} or payload["permit"] is not None or payload["attention"]:
            return True
        if getattr(self, "operation_driven", False):
            return True
        plans = self.incoming_plans(payload) if self.incoming_plans is not None else None
        if plans is None:
            from unilabos.app.scheduler.inventory.placement import automatic_incoming_plans
            plans = automatic_incoming_plans(self.authority, payload)
        if plans is None:
            return False
        details = {
            item["container_uuid"]: self.client.container(
                item["container_uuid"], self.store.event_epoch_uuid, handoff_uuid=identity
            )
            for item in payload["items"]
        }
        self.authority.prepare_incoming(package, container_details=details, plans=plans)
        return True

    def refresh_snapshot(self, handoff_uuid: str) -> dict[str, Any]:
        """重取中心当前包并幂等应用，为旧终态补齐本站应用证明，不推断新的物理事实。"""
        from unilabos.app.scheduler.inventory.handoff_attention import handoff_details

        if not self.authority.has_handoff(handoff_uuid):
            raise ValueError("本站没有该交接的持久记录，不能刷新")
        package = self.client.handoff_package(handoff_uuid, None, self.store.event_epoch_uuid)
        self.authority.apply_center_snapshot(package)
        return handoff_details(self.authority, handoff_uuid)[0]

    def freeze_source(self, handoff_uuid: str, *, physical_ready: bool, evidence_refs: list[str]) -> dict[str, Any]:
        """显式源端操作先拉取最新可信包，再建立本站冻结。"""
        package = self.client.handoff_package(handoff_uuid, None, self.store.event_epoch_uuid)
        return self.authority.freeze_source(package, physical_ready=physical_ready, evidence_refs=evidence_refs)

    def verify_source_presence(self, handoff_uuid: str, *, handoff_item_uuid: str,
                               barcode_namespace: str, barcode: str, physical_present: bool,
                               evidence_refs: list[str]) -> dict[str, Any]:
        """显式源端复核先拉取最新中心包；只记证明，等待中心逐件结算。"""
        package = self.client.handoff_package(handoff_uuid, None, self.store.event_epoch_uuid)
        return self.authority.verify_source_presence(
            package, handoff_item_uuid=handoff_item_uuid, barcode_namespace=barcode_namespace,
            barcode=barcode, physical_present=physical_present, evidence_refs=evidence_refs,
        )

    def verify_source_departure(self, handoff_uuid: str, *, handoff_item_uuid: str,
                                physical_absent: bool, evidence_refs: list[str]) -> dict[str, Any]:
        """目标已收货但源未正常冻结时，核实真实离站并持久隔离原身份。"""
        package = self.client.handoff_package(handoff_uuid, None, self.store.event_epoch_uuid)
        return self.authority.verify_source_departure(package, handoff_item_uuid=handoff_item_uuid,
                                                      physical_absent=physical_absent, evidence_refs=evidence_refs)

    def verify_movement(self, handoff_uuid: str, *, handoff_item_uuid: str,
                        physical_present: bool, barcode_namespace: str, barcode: str,
                        evidence_refs: list[str], arrival_plan: dict[str, Any] | None = None) -> dict[str, Any]:
        """新代次核实使用最新可信包和权威容器版本，客户端不能自带中心证明。"""
        package = self.client.handoff_package(handoff_uuid, None, self.store.event_epoch_uuid)
        item = next((i for i in package["payload"]["items"] if i["handoff_item_uuid"] == handoff_item_uuid), None)
        if item is None:
            raise ValueError("新移动核实明细不属于该交接")
        details = self.client.container(item["container_uuid"], self.store.event_epoch_uuid, handoff_uuid=handoff_uuid)
        return self.authority.verify_movement(package, handoff_item_uuid=handoff_item_uuid,
            physical_present=physical_present, barcode_namespace=barcode_namespace, barcode=barcode,
            evidence_refs=evidence_refs, container_details=details, arrival_plan=arrival_plan)
