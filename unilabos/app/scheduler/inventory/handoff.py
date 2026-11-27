"""手工跨站交接的本站库存权威；实际库存、保护、receipt 和 intent 同事务。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from unilabos.app.scheduler.inventory.dispatch_admission import assert_resource_keys_unclaimed
from unilabos.app.scheduler.inventory.ingress import StationIngressAuthority
from unilabos.app.scheduler.inventory.store import InventoryStore, clear_site_occupancy
from unilabos.app.scheduler.resource_lock import device_lock_key, material_lock_key
from unilabos.center_protocol.digest import digest


class HandoffInventoryError(ValueError):
    """库存交接不能安全执行，保留原保护供核实。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _uuid(value: str) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise HandoffInventoryError("invalid_identity", "交接身份必须为规范 UUID")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _evidence(values: Sequence[str]) -> list[str]:
    if (
        not isinstance(values, (tuple, list))
        or not 1 <= len(values) <= 32
        or any(not isinstance(value, str) or not value.strip() or len(value.encode()) > 2048 for value in values)
    ):
        raise HandoffInventoryError("invalid_evidence", "必须提供 1 至 32 条非空物理证据引用")
    return list(values)


class StationHandoffAuthority:
    """持有本站库存事务；不联网，不把中心投影当成本地物理事实。"""

    def __init__(
        self,
        store: InventoryStore,
        *,
        station_uuid: str,
        instance_uuid: str,
        event_epoch_uuid: str,
        lab_uuid: str,
        now_ms: Callable[[], int] | None = None,
        source_context_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
    ):
        self.store = store
        self.station_uuid, self.instance_uuid = _uuid(station_uuid), _uuid(instance_uuid)
        self.event_epoch_uuid, self.lab_uuid = _uuid(event_epoch_uuid), _uuid(lab_uuid)
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self.source_context_resolver = source_context_resolver
        self.ingress = StationIngressAuthority(
            store,
            edge_id=self.station_uuid,
            lab_id=self.lab_uuid,
            now=lambda: datetime.fromtimestamp(self._now_ms() / 1000, timezone.utc),
        )
        with store.transaction() as c:
            identity = _json([self.station_uuid, self.instance_uuid, self.event_epoch_uuid, self.lab_uuid])
            row = c.execute("SELECT identity_json FROM station_handoff_identity WHERE singleton=1").fetchone()
            if row is not None and row[0] != identity:
                raise HandoffInventoryError("identity_changed", "库存交接身份或纪元改变，必须显式核实恢复")
            c.execute(
                "INSERT OR IGNORE INTO station_handoff_identity(singleton,identity_json) VALUES (1,?)", (identity,)
            )

    def _package(self, package: Mapping[str, Any], role: str | None = None) -> dict[str, Any]:
        from unilabos.center_protocol.handoff import validate_handoff_package

        body = validate_handoff_package(dict(package), station_uuid=self.station_uuid, instance_uuid=self.instance_uuid)
        payload = body["payload"]
        if payload["lab_uuid"] != self.lab_uuid:
            raise HandoffInventoryError("wrong_lab", "交接不属于本站实验室")
        if role and payload[f"{role}_station_uuid"] != self.station_uuid:
            raise HandoffInventoryError("wrong_role", "本站不是交接的指定角色")
        return payload

    def bind_container(
        self, *, container_uuid: str, local_carrier_material_uuid: str, barcode_namespace: str, barcode: str
    ) -> dict[str, Any]:
        """由本站登记明确的物理容器映射，不能按外站 UUID 覆写本地主键。"""
        container_uuid, local_carrier_material_uuid = _uuid(container_uuid), _uuid(local_carrier_material_uuid)
        if not barcode_namespace.strip() or not barcode.strip():
            raise HandoffInventoryError("invalid_barcode", "容器需要非空条码及命名空间")
        with self.store.transaction() as c:
            material = self._material(c, local_carrier_material_uuid)
            if material["inventory_status"] != "warehouse" or material["disposition"] != "active":
                raise HandoffInventoryError("source_unavailable", "实际载体不在本站可用库存中")
            if material["barcode"] != barcode:
                raise HandoffInventoryError("barcode_mismatch", "容器条码必须匹配本站载体")
            row = c.execute(
                "SELECT * FROM container_station_binding WHERE container_uuid=?", (container_uuid,)
            ).fetchone()
            wanted = dict(
                container_uuid=container_uuid,
                station_uuid=self.station_uuid,
                carrier_material_uuid=local_carrier_material_uuid,
                barcode_namespace=barcode_namespace,
                barcode=barcode,
            )
            if row is not None:
                if {key: row[key] for key in wanted} != wanted:
                    raise HandoffInventoryError("binding_conflict", "容器已绑定其他本站载体或条码")
                return wanted
            if c.execute("SELECT 1 FROM container_station_binding WHERE carrier_material_uuid=? OR (barcode_namespace=? AND barcode=?)",
                         (local_carrier_material_uuid, barcode_namespace, barcode)).fetchone():
                raise HandoffInventoryError("binding_conflict", "载体或条码已绑定其他容器")
            c.execute("INSERT INTO container_station_binding(container_uuid,station_uuid,carrier_material_uuid,barcode_namespace,barcode) VALUES (?,?,?,?,?)", tuple(wanted.values()))
            return wanted

    def export_source_snapshot(
        self, *, container_uuid: str, source_material_refs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """在创建中心交接前导出实际树、容量与内容；不分配或猜测谱系身份。"""
        from unilabos.app.scheduler.inventory.handoff_adapter import export_snapshot

        with self.store.transaction() as c:
            binding = c.execute(
                "SELECT carrier_material_uuid FROM container_station_binding WHERE container_uuid=?",
                (_uuid(container_uuid),),
            ).fetchone()
            if binding is None:
                raise HandoffInventoryError("container_not_bound", "容器尚未绑定本站载体")
            return export_snapshot(self, c, binding[0], source_material_refs)

    def export_output_containers(self, material_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按显式绑定导出完整物料树；未绑定、版本漂移或半棵树不能创建交接快照。"""
        with self.store.transaction() as c:
            return self._export_output_containers(c, material_refs)

    def verify_output_containers(self, connection, material_refs: list[dict[str, Any]], containers: list[dict[str, Any]]) -> None:
        """在调用方库存写事务内复核完整树，关闭网络读取后到证据落盘前的变更窗口。"""
        if not connection.in_transaction:
            raise HandoffInventoryError("transaction_required", "输出容器核验必须与结果证据同库存事务")
        expected = [{k: v for k, v in item.items() if k != "center_snapshot"} for item in containers]
        if digest(self._export_output_containers(connection, material_refs)) != digest(expected):
            raise HandoffInventoryError("source_snapshot_changed", "容器树、库位或内容在导出后已经改变")

    def _export_output_containers(self, c, material_refs):
        from unilabos.app.scheduler.inventory.handoff_adapter import export_snapshot

        groups: dict[str, dict[str, Any]] = {}
        for ref in material_refs:
            material = self._material(c, _uuid(ref["local_material_uuid"]))
            if ref["station_uuid"] != self.station_uuid or material["aggregate_version"] != ref["local_revision"]:
                raise HandoffInventoryError("material_revision_changed", "物料版本已改变，不能以旧结果生成新快照")
            ancestors, cursor = set(), material["uuid"]
            bound = None
            while cursor:
                if cursor in ancestors:
                    raise HandoffInventoryError("invalid_material_tree", "物料树包含循环")
                ancestors.add(cursor)
                binding = c.execute("SELECT * FROM container_station_binding WHERE carrier_material_uuid=?", (cursor,)).fetchone()
                if binding is not None:
                    bound = dict(binding)
                    break
                cursor = self._material(c, cursor)["parent_uuid"]
            if bound is None:
                raise HandoffInventoryError("container_not_bound", "输出物料尚无明确的物理容器绑定")
            if bound.get("registration_request_json") and not bound.get("registration_response_json"):
                raise HandoffInventoryError("container_registration_pending", "中心容器登记尚未完成")
            if bound["station_uuid"] != self.station_uuid:
                raise HandoffInventoryError("binding_conflict", "输出容器绑定不属于本站")
            entry = groups.setdefault(bound["container_uuid"], {**bound, "source_material_refs": []})
            entry["source_material_refs"].append({k: v for k, v in ref.items() if k != "handoff_item_uuid"})
        result = []
        for identity in sorted(groups):
            entry = groups[identity]
            snapshot = export_snapshot(self, c, entry["carrier_material_uuid"], entry["source_material_refs"],
                                       template_evidence=getattr(self, "include_template_evidence", False))
            result.append({"container_uuid": identity, "barcode_namespace": entry["barcode_namespace"],
                "barcode": entry["barcode"], "source_material_refs": entry["source_material_refs"], "source_snapshot": snapshot})
        return result

    def freeze_source(
        self, package: Mapping[str, Any], *, physical_ready: bool, evidence_refs: Sequence[str],
        include_transport_endpoints: bool = False,
    ) -> dict[str, Any]:
        """操作者确认物理可交付后，校验版本及任务占用并原子冻结整批源库存。"""
        payload, evidence = self._package(package, "source"), _evidence(evidence_refs)
        if payload["prepared"] is None or payload["status"] not in {"prepared", "released"}:
            raise HandoffInventoryError("target_not_prepared", "必须先收到目标已保护入口的可信 prepared 事实")
        if physical_ready is not True:
            raise HandoffInventoryError("physical_confirmation_required", "源物料必须由本站确认物理可交付")
        plan_digest = digest({"physical_ready": True, "evidence_refs": evidence})
        if include_transport_endpoints:
            plan_digest = digest({"physical_ready": True, "evidence_refs": evidence, "include_transport_endpoints": True})
        with self.store.transaction() as c:
            old = self._existing(c, payload, plan_digest)
            if old is not None:
                return self._intent(c, payload["handoff_uuid"], "released")
            self._new(c, payload, "source", plan_digest)
            for item in payload["items"]:
                self._freeze_source_item(c, payload, item)
            proof = c.execute(
                "SELECT proof_uuid FROM station_handoff WHERE handoff_uuid=?", (payload["handoff_uuid"],)
            ).fetchone()[0]
            from .handoff_endpoint import endpoint_snapshot

            endpoint = ({"endpoint_snapshot": endpoint_snapshot(c, payload, role="source")}
                        if include_transport_endpoints else {})
            return self._emit(
                c,
                payload,
                "released",
                {
                    "kind": "released",
                    **endpoint,
                    "expected_revision": payload["revision"],
                    "release_uuid": proof,
                    "evidence_refs": evidence,
                },
            )

    def _freeze_source_item(self, c, payload, item):
        """共享物料身份、完整快照和任务占用验证；物理确认语义由调用用例提供。"""
        binding = c.execute(
            "SELECT * FROM container_station_binding WHERE container_uuid=?", (item["container_uuid"],)
        ).fetchone()
        if binding is None:
            raise HandoffInventoryError("container_not_bound", "源容器尚未绑定本站载体")
        if binding["registration_request_json"] and not binding["registration_response_json"]:
            raise HandoffInventoryError("container_registration_pending", "中心容器登记尚未完成")
        carrier = self._material(c, binding["carrier_material_uuid"])
        from unilabos.app.scheduler.inventory.placement import verify_source_placement
        verify_source_placement(self, c, payload, item, carrier)
        materials = self._subtree(c, carrier["uuid"])
        referenced = {ref["local_material_uuid"] for ref in item["source_material_refs"]}
        if materials - {carrier["uuid"]} - referenced:
            raise HandoffInventoryError("unmapped_contents", "源载体含未登记谱系的子物料，不能隐式丢弃其身份")
        for ref in item["source_material_refs"]:
            row = self._material(c, ref["local_material_uuid"])
            if (
                ref["station_uuid"] != self.station_uuid
                or row["uuid"] not in materials
                or row["aggregate_version"] != ref["local_revision"]
            ):
                raise HandoffInventoryError("source_revision_mismatch", "源物料身份、版本或容器归属已经变化")
        from unilabos.app.scheduler.inventory.handoff_adapter import validate_source

        adapted = validate_source(self, c, item, carrier["uuid"])
        from unilabos.app.scheduler.inventory.outbound import transfer_to_handoff
        transfer_to_handoff(c, carrier_uuid=carrier["uuid"], materials=materials,
            source_material_refs=item["source_material_refs"],
            source_kind=payload.get("transfer_source_kind"))
        self._insert_item(c, payload, item, carrier["uuid"])
        from unilabos.app.scheduler.inventory.handoff_pickup import remember_source_location

        remember_source_location(c, item, carrier)
        for ref in item["source_material_refs"]:
            self._bind_material(c, item, ref, ref["local_material_uuid"], ref["local_revision"])
        for identity in materials:
            row = self._material(c, identity)
            if not adapted and c.execute(
                "SELECT 1 FROM site WHERE material_uuid=? AND deleted_at IS NULL", (identity,)
            ).fetchone():
                raise HandoffInventoryError(
                    "site_adapter_required", "源物料含自身库位，必须先配置显式跨站结构适配"
                )
            if not adapted and c.execute(
                "SELECT 1 FROM container_content WHERE material_uuid=? AND deleted_at IS NULL",
                (identity,),
            ).fetchone():
                raise HandoffInventoryError(
                    "content_adapter_required", "源物料含试剂或样品内容，必须先配置显式跨站内容适配"
                )
            if row["inventory_status"] != "warehouse" or row["disposition"] != "active":
                raise HandoffInventoryError("source_unavailable", "源载体及其内容必须全部处于可用库存状态")
            if c.execute(
                "SELECT 1 FROM inventory_material_source_binding WHERE material_uuid=? AND status='active'",
                (identity,),
            ).fetchone():
                raise HandoffInventoryError("task_claimed", "源物料仍由本地任务占用")
            if c.execute(
                "SELECT 1 FROM inventory_reservation,json_each(amounts_json,'$.instances') AS i WHERE status='active' AND i.value=?",
                (identity,),
            ).fetchone():
                raise HandoffInventoryError("task_reserved", "源物料仍由本地任务预留")
            self._protect_material(c, payload, item, row, "handoff_outgoing")

    def prepare_incoming(
        self,
        package: Mapping[str, Any],
        *,
        container_details: Mapping[str, Mapping[str, Any]],
        plans: Mapping[str, Mapping[str, Any]],
        include_transport_endpoints: bool = False,
    ) -> dict[str, Any]:
        """使用明确的本站模板/入口配置预建真实 incoming 物料，再保护入口并写 prepared。"""
        payload = self._package(package, "target")
        from unilabos.app.scheduler.inventory.placement import exact_incoming_plans
        plans = exact_incoming_plans(payload, plans)
        plan_digest = digest(dict(plans))
        if include_transport_endpoints:
            plan_digest = digest({"plans": dict(plans), "include_transport_endpoints": True})
        with self.store.transaction() as c:
            old = self._existing(c, payload, plan_digest)
            if old is not None:
                return self._intent(c, payload["handoff_uuid"], "prepared")
            self._new(c, payload, "target", plan_digest)
            expiry, reservations, placements = [], [], []
            for item in payload["items"]:
                identity = item["handoff_item_uuid"]
                if identity not in plans or item["container_uuid"] not in container_details:
                    raise HandoffInventoryError("local_plan_required", "每件容器必须有本站模板、入口和可信条码详情")
                plan = dict(plans[identity])
                if set(plan) - {"adapter"} != {
                    "carrier_template_uuid",
                    "carrier_lineage_uuid",
                    "lineage_template_uuids",
                    "candidate_site_uuids",
                    "ttl_seconds",
                }:
                    raise HandoffInventoryError("invalid_local_plan", "本站接收配置字段不完整或包含未知字段")
                adapted = "adapter" in plan
                source_materials = {}
                if adapted:
                    from unilabos.app.scheduler.inventory.handoff_adapter import validate_snapshot

                    source_materials, _ = validate_snapshot(item["source_snapshot"], item["source_material_refs"], plan)
                elif item["source_snapshot"].get("adapter") is not None:
                    raise HandoffInventoryError("local_plan_required", "版本化库存快照需要显式适配配置")
                details = dict(container_details[item["container_uuid"]])
                if (
                    details.get("container_uuid") != item["container_uuid"]
                    or details.get("lab_uuid") != self.lab_uuid
                    or details.get("current_station_uuid") != payload["source_station_uuid"]
                    or details.get("active_handoff_item_uuid") != identity
                    or details.get("state_version") != item["reserved_state_version"]
                    or details.get("attention")
                    or details.get("custody_status") != "reserved"
                ):
                    raise HandoffInventoryError("container_changed", "中心容器占用、归属或版本已变化")
                refs = item["source_material_refs"]
                lineages = {ref["material_lineage_uuid"] for ref in refs}
                if set(plan["lineage_template_uuids"]) != lineages or (
                    plan["carrier_lineage_uuid"] is not None and plan["carrier_lineage_uuid"] not in lineages
                ):
                    raise HandoffInventoryError("lineage_plan_mismatch", "接收模板配置必须精确覆盖所有谱系")
                bound = c.execute(
                    "SELECT * FROM container_station_binding WHERE container_uuid=?", (item["container_uuid"],)
                ).fetchone()
                if bound is None:
                    carrier = source_materials.get(item["source_snapshot"].get("carrier_material_uuid"), {})
                    carrier_uuid = self._create_incoming(
                        c, plan["carrier_template_uuid"], barcode=details["barcode"],
                        adapted=adapted, source_name=carrier.get("name"),
                    )
                    c.execute(
                        "INSERT INTO container_station_binding(container_uuid,station_uuid,carrier_material_uuid,barcode_namespace,barcode) VALUES (?,?,?,?,?)",
                        (
                            item["container_uuid"],
                            self.station_uuid,
                            carrier_uuid,
                            details["barcode_namespace"],
                            details["barcode"],
                        ),
                    )
                else:
                    if (
                        bound["barcode_namespace"] != details["barcode_namespace"]
                        or bound["barcode"] != details["barcode"]
                    ):
                        raise HandoffInventoryError("barcode_changed", "容器映射的条码不能改变")
                    carrier_uuid = bound["carrier_material_uuid"]
                    carrier = self._material(c, carrier_uuid)
                    self._revive_incoming(c, carrier, plan["carrier_template_uuid"])
                self._insert_item(c, payload, item, carrier_uuid)
                all_materials = {carrier_uuid}
                local_by_source = {}
                for ref in refs:
                    lineage = ref["material_lineage_uuid"]
                    template = plan["lineage_template_uuids"][lineage]
                    if lineage == plan["carrier_lineage_uuid"]:
                        if template != plan["carrier_template_uuid"]:
                            raise HandoffInventoryError("template_mismatch", "载体谱系与载体模板不一致")
                        local = carrier_uuid
                    else:
                        source = source_materials.get(ref["local_material_uuid"], {})
                        barcode = source.get("barcode", "")
                        historical = c.execute(
                            "SELECT m.* FROM material m WHERE m.barcode=? AND m.deleted_at IS NULL", (barcode,)
                        ).fetchone() if barcode else None
                        if historical is not None:
                            known = c.execute(
                                "SELECT 1 FROM material_station_binding b JOIN station_handoff_item i USING(handoff_item_uuid) WHERE b.local_material_uuid=? AND b.material_lineage_uuid=? AND i.container_uuid=?",
                                (historical["uuid"], lineage, item["container_uuid"]),
                            ).fetchone()
                            if known is None:
                                raise HandoffInventoryError("barcode_conflict", "子物料条码已属于另一本地物料或谱系")
                            self._revive_incoming(c, historical, template)
                            local = historical["uuid"]
                        else:
                            local = self._create_incoming(
                                c, template, parent_uuid=carrier_uuid, barcode=barcode,
                                adapted=adapted, source_name=source.get("name"),
                            )
                    all_materials.add(local)
                    local_by_source[ref["local_material_uuid"]] = local
                    self._bind_material(c, item, ref, local, self._material(c, local)["aggregate_version"])
                if adapted:
                    from unilabos.app.scheduler.inventory.handoff_adapter import prepare_structure

                    prepare_structure(self, c, item, plan, local_by_source)
                reservation = self.ingress.reserve_in_transaction(
                    c,
                    idempotency_key="center-handoff:" + identity,
                    carrier_material_uuid=carrier_uuid,
                    candidate_site_uuids=plan["candidate_site_uuids"],
                    ttl_seconds=plan["ttl_seconds"],
                )
                self._validate_site_template(c, reservation["site_uuid"], plan["carrier_template_uuid"])
                reservation = self.ingress.protect_prepared_in_transaction(c, reservation["uuid"])
                if item.get("placement_plan") is not None:
                    selected = item["placement_plan"]["selection"]
                    placements.append({"selection_uuid": selected["selection_uuid"],
                        "handoff_item_uuid": identity, "container_uuid": item["container_uuid"],
                        "target_site_uuid": reservation["site_uuid"],
                        "reservation_uuid": reservation["uuid"], "reservation_revision": reservation["revision"]})
                c.execute(
                    "UPDATE station_handoff_item SET reservation_uuid=? WHERE handoff_item_uuid=?",
                    (reservation["uuid"], identity),
                )
                for local in all_materials:
                    self._protect_material(
                        c, payload, item, self._material(c, local), "incoming", reservation_uuid=reservation["uuid"]
                    )
                expiry.append(
                    int(datetime.fromisoformat(reservation["expires_at"].replace("Z", "+00:00")).timestamp() * 1000)
                )
                reservations.append("inventory:ingress:" + reservation["uuid"])
            proof = c.execute(
                "SELECT proof_uuid FROM station_handoff WHERE handoff_uuid=?", (payload["handoff_uuid"],)
            ).fetchone()[0]
            from .handoff_endpoint import endpoint_snapshot

            endpoint = ({"endpoint_snapshot": endpoint_snapshot(c, payload, role="target")}
                        if include_transport_endpoints else {})
            return self._emit(
                c,
                payload,
                "prepared",
                {
                    "kind": "prepared",
                    **({"placement_reservations": {"selection_digest": payload["items"][0]["placement_plan"]["selection_digest"], "items": placements}} if placements else {}),
                    **endpoint,
                    "expected_revision": payload["revision"],
                    "protection_uuid": proof,
                    "reservation_until_ms": min(expiry),
                    "evidence_refs": ["inventory:handoff:" + payload["handoff_uuid"]],
                },
            )

    def renew_incoming(self, package, *, ttl_seconds):
        """仅在原保护连续有效且尚未发许可时续期，不释放再占用原位置。"""
        from datetime import timedelta

        from .handoff_endpoint import endpoint_snapshot
        payload = self._package(package, "target")
        if (type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400 or payload["permit"] is not None
                or payload["attention"] or payload["status"] not in {"prepared", "released"}):
            raise HandoffInventoryError("renewal_held", "当前交接不允许续期原接收保护")
        with self.store.transaction() as c:
            prior = c.execute("SELECT body_json FROM station_handoff_outbox WHERE handoff_uuid=? AND purpose=?",
                              (payload["handoff_uuid"], "renewal-" + str(payload["revision"]))).fetchone()
            if prior is not None:
                return json.loads(prior[0])
            row = c.execute("SELECT proof_uuid FROM station_handoff WHERE handoff_uuid=? AND role='target'", (payload["handoff_uuid"],)).fetchone()
            if row is None or row[0] != payload["prepared"]["protection_uuid"]:
                raise HandoffInventoryError("renewal_held", "原接收保护身份不匹配")
            now = self.ingress._utc_now()
            deadline = now + timedelta(seconds=ttl_seconds)
            for item in c.execute("SELECT reservation_uuid,state FROM station_handoff_item WHERE handoff_uuid=?", (payload["handoff_uuid"],)).fetchall():
                reservation = self.ingress._row(c,item["reservation_uuid"])
                self.ingress._assert_active_resources(c,reservation)
                if item["state"] != "protected" or reservation["state"] != "reserved" or reservation["expires_at"] <= self.ingress._format(now):
                    raise HandoffInventoryError("renewal_held", "原位置保护不连续，不能用续期重新取得位置")
                if reservation["expires_at"] >= self.ingress._format(deadline):
                    raise HandoffInventoryError("renewal_held", "续期不能缩短或复写原保护期限")
                c.execute("UPDATE station_ingress_reservation SET expires_at=?,update_time=?,revision=revision+1 WHERE uuid=?",
                          (self.ingress._format(deadline),self.ingress._format(now),item["reservation_uuid"]))
            endpoint = endpoint_snapshot(c, payload, role="target")
            endpoints = {item["container_uuid"]: item for item in endpoint["items"]}
            placements = []
            for item in payload["items"]:
                if item.get("placement_plan") is not None:
                    selected = item["placement_plan"]["selection"]
                    point = endpoints[item["container_uuid"]]
                    reservation = point["reservation"]
                    placements.append({
                        "selection_uuid": selected["selection_uuid"],
                        "handoff_item_uuid": item["handoff_item_uuid"],
                        "container_uuid": item["container_uuid"],
                        "target_site_uuid": point["site_uuid"],
                        "reservation_uuid": reservation["reservation_uuid"],
                        "reservation_revision": reservation["revision"],
                    })
            return self._emit(c,payload,"renewal-"+str(payload["revision"]),{
                "kind":"reservation_renewed","expected_revision":payload["revision"],"protection_uuid":row[0],
                "reservation_until_ms":int(deadline.timestamp()*1000),"endpoint_snapshot":endpoint,
                **({"placement_reservations": {
                    "selection_digest": payload["items"][0]["placement_plan"]["selection_digest"],
                    "items": placements,
                }} if placements else {}),
                "evidence_refs":["inventory:continuous-ingress-protection"]})

    def receive(
        self,
        handoff_uuid: str,
        handoff_item_uuid: str,
        *,
        barcode_namespace: str,
        barcode: str,
        evidence_refs: Sequence[str],
    ) -> dict[str, Any]:
        """实际扫码和落位时调用；收到中心 ACK 前维持隔离，重复调用只返回原 receipt。"""
        evidence = _evidence(evidence_refs)
        request_digest = digest({"barcode_namespace": barcode_namespace, "barcode": barcode, "evidence_refs": evidence})
        with self.store.transaction() as c:
            handoff, item = self._rows(c, _uuid(handoff_uuid), _uuid(handoff_item_uuid))
            if handoff["role"] != "target":
                raise HandoffInventoryError("wrong_role", "只有目标工站可以接收")
            if item["receipt_json"]:
                if item["receive_request_digest"] != request_digest:
                    raise HandoffInventoryError("receipt_conflict", "同一件收货证据不同，需核实而不能覆盖")
                return json.loads(item["receipt_json"])
            if item["state"] != "protected":
                raise HandoffInventoryError("item_not_protected", "此明细已取消或结算，实际到货需核实隔离")
            binding = c.execute(
                "SELECT * FROM container_station_binding WHERE container_uuid=?", (item["container_uuid"],)
            ).fetchone()
            if (barcode_namespace, barcode) != (binding["barcode_namespace"], binding["barcode"]):
                raise HandoffInventoryError("scan_mismatch", "实际扫描的条码与交接容器不一致")
            payload = json.loads(handoff["package_json"])
            reservation = self.ingress.receive_in_transaction(
                c, item["reservation_uuid"], handoff_uuid=handoff_uuid, physical_receipt=True
            )
            refs = []
            for mapping in c.execute(
                "SELECT * FROM material_station_binding WHERE handoff_item_uuid=? ORDER BY material_lineage_uuid",
                (handoff_item_uuid,),
            ).fetchall():
                local = mapping["local_material_uuid"]
                c.execute(
                    "UPDATE material SET inventory_status='handoff_received',aggregate_version=aggregate_version+1 WHERE uuid=?",
                    (local,),
                )
                row = self._material(c, local)
                refs.append(
                    {
                        "station_uuid": self.station_uuid,
                        "local_material_uuid": local,
                        "local_revision": row["aggregate_version"],
                        "material_lineage_uuid": mapping["material_lineage_uuid"],
                    }
                )
            if item["carrier_material_uuid"] not in {ref["local_material_uuid"] for ref in refs}:
                c.execute(
                    "UPDATE material SET inventory_status='handoff_received',aggregate_version=aggregate_version+1 WHERE uuid=?",
                    (item["carrier_material_uuid"],),
                )
            receipt = {
                "handoff_item_uuid": handoff_item_uuid,
                "target_material_refs": refs,
                "target_snapshot": {
                    "carrier_material_uuid": item["carrier_material_uuid"],
                    "container_uuid": item["container_uuid"],
                    "site_uuid": reservation["site_uuid"],
                    "received_at": reservation["received_at"],
                    "materials": self._material_snapshots(c, handoff_item_uuid),
                },
            }
            if next(i for i in payload["items"] if i["handoff_item_uuid"] == handoff_item_uuid)["source_snapshot"].get("adapter"):
                from unilabos.app.scheduler.inventory.handoff_adapter import export_snapshot

                receipt["target_snapshot"]["inventory_snapshot"] = export_snapshot(
                    self, c, item["carrier_material_uuid"], refs
                )
            receipt["receipt_digest"] = digest(receipt)
            c.execute(
                "UPDATE station_handoff_item SET state='received',receipt_json=?,receive_request_digest=? WHERE handoff_item_uuid=?",
                (_json(receipt), request_digest, handoff_item_uuid),
            )
            self._emit(
                c,
                payload,
                "received:" + handoff_item_uuid,
                {
                    "kind": "received",
                    "observed_revision": payload["revision"],
                    "items": [receipt],
                    "evidence_refs": evidence,
                },
            )
            return receipt

    def verify_source_departure(
        self, package: Mapping[str, Any], *, handoff_item_uuid: str,
        physical_absent: bool, evidence_refs: Sequence[str],
    ) -> dict[str, Any]:
        """未正常冻结却已目标收货：源端核实实物离开，隔离原身份后持久证明，等待结算。"""
        payload, evidence = self._package(package, "source"), _evidence(evidence_refs)
        identity = _uuid(handoff_item_uuid)
        if physical_absent is not True:
            raise HandoffInventoryError("physical_confirmation_required", "必须明确核实实物已离开源端")
        with self.store.transaction() as c:
            row = c.execute("SELECT * FROM station_handoff WHERE handoff_uuid=?", (payload["handoff_uuid"],)).fetchone()
            if row is not None:
                self._assert_same_items(json.loads(row["package_json"]), payload)
                if payload["revision"] < row["revision"]:
                    raise HandoffInventoryError("stale_package", "源离站复核需要最新中心包")
                old = c.execute("SELECT body_json FROM station_handoff_outbox WHERE handoff_uuid=? AND purpose=?",
                                (payload["handoff_uuid"], "source_departure_verified:" + identity)).fetchone()
                if old is not None:
                    intent = json.loads(old[0])
                    if intent["payload"]["evidence_refs"] != evidence:
                        raise HandoffInventoryError("verification_conflict", "原源离站复核证明不可覆盖")
                    return intent
            item = next((i for i in payload["items"] if i["handoff_item_uuid"] == identity), None)
            if item is None or item["receipt"] is None or not payload["attention"]:
                raise HandoffInventoryError("verification_not_pending", "源离站复核必须有原目标实际收货且仍待核实")
            if row is None:
                self._record_new(c, payload, "source", digest({"source_departure_verification": True}))
            local = c.execute("SELECT * FROM station_handoff_item WHERE handoff_item_uuid=?", (identity,)).fetchone()
            if local is None:
                self._freeze_source_item(c, payload, item)
            elif local["state"] != "protected":
                raise HandoffInventoryError("verification_not_pending", "源原明细已经结算，不可再次证明离站")
            else:
                for ref in item["source_material_refs"]:
                    if self._material(c, ref["local_material_uuid"])["aggregate_version"] != ref["local_revision"]:
                        raise HandoffInventoryError("source_revision_mismatch", "源离站复核原修订已变化")
                from unilabos.app.scheduler.inventory.handoff_adapter import validate_source

                validate_source(self, c, item, local["carrier_material_uuid"])
            proof = {"handoff_item_uuid": identity, "verification_uuid": str(uuid4()),
                     "source_snapshot_digest": digest(item["source_snapshot"]), "evidence_refs": evidence}
            proof["verification_digest"] = digest(proof)
            return self._emit(c, payload, "source_departure_verified:" + identity,
                              {"kind": "source_departure_verified", "observed_revision": payload["revision"],
                               "items": [proof], "evidence_refs": evidence})

    def verify_source_presence(
        self, package: Mapping[str, Any], *, handoff_item_uuid: str,
        barcode_namespace: str, barcode: str, physical_present: bool, evidence_refs: Sequence[str],
    ) -> dict[str, Any]:
        """异常期间显式扫码复核源端实物；保存原证明并保持冻结，绝不自行取消运输。"""
        payload, evidence = self._package(package, "source"), _evidence(evidence_refs)
        if physical_present is not True:
            raise HandoffInventoryError("physical_confirmation_required", "必须明确核实实物在源端")
        identity = _uuid(handoff_item_uuid)
        with self.store.transaction() as c:
            item = next((i for i in payload["items"] if i["handoff_item_uuid"] == identity), None)
            if item is None:
                raise HandoffInventoryError("item_missing", "源复核明细不属于原交接")
            row = c.execute("SELECT * FROM station_handoff WHERE handoff_uuid=?", (payload["handoff_uuid"],)).fetchone()
            if row is not None:
                self._assert_same_items(json.loads(row["package_json"]), payload)
                if payload["revision"] < row["revision"]:
                    raise HandoffInventoryError("stale_package", "源复核需要最新中心包")
            binding = c.execute("SELECT * FROM container_station_binding WHERE container_uuid=?", (item["container_uuid"],)).fetchone()
            if binding is None:
                raise HandoffInventoryError("container_not_bound", "源容器尚未绑定原本站载体")
            if binding["barcode_namespace"] != barcode_namespace or binding["barcode"] != barcode:
                raise HandoffInventoryError("barcode_mismatch", "源复核条码与原容器不符")
            purpose = "source_verified:" + identity
            old = c.execute("SELECT body_json FROM station_handoff_outbox WHERE handoff_uuid=? AND purpose=?", (payload["handoff_uuid"], purpose)).fetchone()
            if old is not None:
                intent = json.loads(old[0])
                if intent["payload"]["evidence_refs"] != evidence:
                    raise HandoffInventoryError("verification_conflict", "原源复核证明不可覆盖")
                return intent
            if not payload["attention"] and payload["status"] != "canceling":
                raise HandoffInventoryError("verification_not_pending", "只允许核实仍冻结的异常原明细")
            if item["receipt"] is not None:
                raise HandoffInventoryError("return_handoff_required", "已确认目标收货必须另建退运交接")
            if row is None:
                self._record_new(c, payload, "source", digest({"source_presence_verification": True}))
            local = c.execute("SELECT * FROM station_handoff_item WHERE handoff_item_uuid=?", (identity,)).fetchone()
            if local is None:
                # 部分早到时，未离站件也必须能独立核实并建立真实保护，不能伪造整批released。
                self._freeze_source_item(c, payload, item)
                _, local = self._rows(c, payload["handoff_uuid"], identity)
            if local["state"] != "protected":
                raise HandoffInventoryError("verification_not_pending", "原明细已结算，不可重建保护")
            carrier = self._material(c, local["carrier_material_uuid"])
            materials = self._subtree(c, carrier["uuid"])
            for ref in item["source_material_refs"]:
                material = self._material(c, ref["local_material_uuid"])
                if (material["uuid"] not in materials or material["aggregate_version"] != ref["local_revision"]
                        or material["inventory_status"] not in {"handoff_outgoing", "handoff_quarantined"}):
                    raise HandoffInventoryError("source_revision_mismatch", "源复核原身份、修订或冻结已改变")
            from unilabos.app.scheduler.inventory.handoff_adapter import validate_source

            validate_source(self, c, item, carrier["uuid"])
            proof = {"handoff_item_uuid": identity, "verification_uuid": str(uuid4()),
                     "source_snapshot_digest": digest(item["source_snapshot"]), "evidence_refs": evidence}
            proof["verification_digest"] = digest(proof)
            return self._emit(c, payload, purpose, {"kind": "source_verified", "observed_revision": payload["revision"],
                                                   "items": [proof], "evidence_refs": evidence})

    def verify_movement(
        self, package: Mapping[str, Any], *, handoff_item_uuid: str,
        physical_present: bool, barcode_namespace: str, barcode: str,
        evidence_refs: Sequence[str], container_details: Mapping[str, Any],
        arrival_plan: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按新核实代次扫码当前库存；新到站需显式本站适配计划。"""
        from unilabos.app.scheduler.inventory.handoff_movement import verify_movement

        return verify_movement(self, package, handoff_item_uuid=handoff_item_uuid,
                               physical_present=physical_present, barcode_namespace=barcode_namespace,
                               barcode=barcode, evidence_refs=evidence_refs, container_details=container_details,
                               arrival_plan=arrival_plan)

    def apply_center_snapshot(self, package: Mapping[str, Any]) -> None:
        """只使用已验证的中心包结算本地保护；许可后取消或 attention 保持隔离。"""
        payload = self._package(package)
        try:
            self._apply_center_snapshot(package, quarantine_only=False)
        except HandoffInventoryError as error:
            self._record_snapshot_application(payload, applied=False, reason=str(error))
            raise
        self._record_snapshot_application(payload, applied=True, reason=None)

    def _record_snapshot_application(self, payload, *, applied: bool, reason: str | None):
        # 普通过程包仍由原证明驱动；终结结论必须另外报告本站实际应用结果。
        if payload["status"] not in {"received", "canceled", "reconciled"}:
            return
        with self.store.transaction() as c:
            row = c.execute("SELECT revision FROM station_handoff WHERE handoff_uuid=?", (payload["handoff_uuid"],)).fetchone()
            if row is None or row["revision"] > payload["revision"]:
                return
            package_digest = digest(payload)
            reason = reason.encode("utf-8")[:1000].decode("utf-8", errors="ignore") if reason else None
            self._emit(c, payload, f"snapshot-applied:{payload['revision']}:{int(applied)}:{digest(reason)}", {
                "kind": "snapshot_applied", "observed_revision": payload["revision"],
                "package_digest": package_digest, "applied": applied, "failure_reason": reason,
                "evidence_refs": [f"inventory-handoff:{payload['handoff_uuid']}:{payload['revision']}"]})

    def quarantine_center_snapshot(self, package: Mapping[str, Any]) -> None:
        """冲突 ACK 只可增加隔离，不可成为释放库存或入口的授权。"""
        self._apply_center_snapshot(package, quarantine_only=True)

    def _apply_center_snapshot(self, package: Mapping[str, Any], *, quarantine_only: bool) -> None:
        payload = self._package(package)
        from unilabos.app.scheduler.inventory.handoff_outbound_cancel import (
            cancellation_contexts,
            settle_outbound_cancellation,
        )

        # 不持库存锁读取原工作流的不可变调用；写事务内仍逐项重核库存和导出事实。
        contexts = cancellation_contexts(self, payload) if not quarantine_only else {}
        with self.store.transaction() as c:
            row = c.execute("SELECT * FROM station_handoff WHERE handoff_uuid=?", (payload["handoff_uuid"],)).fetchone()
            if row is None:
                source_settled = (
                    payload["target_station_uuid"] == self.station_uuid
                    and payload["status"] == "canceled" and not payload["attention"]
                    and all((item.get("settlement") or {}).get("decision") == "source_present"
                            for item in payload["items"])
                )
                if not quarantine_only and (self._can_cancel(payload) or source_settled):
                    settle_outbound_cancellation(self, c, payload, contexts)
                    # 检查无保护和保存墓碑共用库存写锁，迟到的 requested 包不能重新建保护。
                    role = "source" if payload["source_station_uuid"] == self.station_uuid else "target"
                    c.execute(
                        "INSERT INTO station_handoff VALUES (?,?,?,?,?,?,?,'canceled_unprepared',?,?)",
                        (
                            payload["handoff_uuid"], role, self.station_uuid, self.instance_uuid,
                            payload["revision"], _json(payload), str(uuid4()),
                            digest({"canceled_unprepared": True}), self._now_ms(),
                        ),
                    )
                    # 核实结算已是终结事实；未建立 incoming 的目标仅保存墓碑，不伪造取消证明。
                    if not source_settled:
                        self._emit_cancel_ack(c, payload)
                return
            self._assert_same_items(json.loads(row["package_json"]), payload)
            if payload["revision"] < row["revision"]:
                return
            old_pickup = json.loads(row["package_json"]).get("pickup")
            if old_pickup is not None and digest(old_pickup) != digest(payload.get("pickup")):
                raise HandoffInventoryError("pickup_conflict", "原取货证明不可删除或改写")
            if payload["revision"] == row["revision"] and digest(payload) != digest(json.loads(row["package_json"])):
                raise HandoffInventoryError("package_conflict", "同一中心交接修订的内容不同")
            c.execute(
                "UPDATE station_handoff SET revision=?,package_json=? WHERE handoff_uuid=?",
                (payload["revision"], _json(payload), payload["handoff_uuid"]),
            )
            if row["state"] == "canceled_unprepared" and not any(i.get("movement_review") for i in payload["items"]):
                # 旧墓碑可能早于产物收尾修复；原包按完整证据补结算一次，已有证明不再触碰库存。
                if not quarantine_only:
                    settle_outbound_cancellation(self, c, payload, contexts)
                return
            if payload["attention"] or quarantine_only:
                # 只有本件明确核实才可解隔离；另一件仍待核实不妨碍已证明件结算。
                for center_item in payload["items"]:
                    settlement = center_item.get("settlement")
                    if not quarantine_only and settlement and settlement["decision"] != "keep_quarantined":
                        continue
                    c.execute("UPDATE station_handoff_resource SET active=1 WHERE handoff_item_uuid=?",
                              (center_item["handoff_item_uuid"],))
                    c.execute(
                        "UPDATE material SET inventory_status='handoff_quarantined' WHERE inventory_status='warehouse' AND uuid IN (SELECT material_uuid FROM station_handoff_resource WHERE handoff_item_uuid=?)",
                        (center_item["handoff_item_uuid"],),
                    )
            if quarantine_only:
                return
            from unilabos.app.scheduler.inventory.handoff_pickup import apply_pickup

            apply_pickup(self, c, payload, row["role"])
            for center_item in payload["items"]:
                if center_item.get("settlement"):
                    self._apply_settlement(c, payload, row["role"], center_item)
            if self._can_cancel(payload):
                if c.execute(
                    "SELECT 1 FROM station_handoff_item WHERE handoff_uuid=? AND receipt_json IS NOT NULL",
                    (payload["handoff_uuid"],),
                ).fetchone():
                    return
                for item in c.execute(
                    "SELECT * FROM station_handoff_item WHERE handoff_uuid=?", (payload["handoff_uuid"],)
                ).fetchall():
                    if item["state"] == "canceled":
                        continue
                    if row["role"] == "target":
                        self.ingress.cancel_in_transaction(
                            c,
                            item["reservation_uuid"],
                            reason="center-pre-permit-cancel",
                            handoff_uuid=payload["handoff_uuid"],
                        )
                    self._release(c, item, state="canceled", role=row["role"])
                self._emit_cancel_ack(c, payload)
            elif not payload["attention"]:
                for center_item in payload["items"]:
                    if center_item.get("movement_review") or center_item["receipt"] is None:
                        continue
                    _, item = self._rows(c, payload["handoff_uuid"], center_item["handoff_item_uuid"])
                    if item["state"] == "settled":
                        continue
                    if item["state"] == "canceled":
                        raise HandoffInventoryError("late_receipt", "已撤销明细出现迟到收货，需核实")
                    if row["role"] == "target" and (
                        item["receipt_json"] is None
                        or digest(json.loads(item["receipt_json"])) != digest(center_item["receipt"])
                    ):
                        raise HandoffInventoryError("receipt_mismatch", "中心收货与本站持久 receipt 不一致")
                    self._release(c, item, state="settled", role=row["role"])

    def _apply_settlement(self, c, payload, role, center_item):
        settlement = center_item["settlement"]
        if settlement["decision"] == "keep_quarantined":
            return
        if center_item.get("movement_review"):
            from unilabos.app.scheduler.inventory.handoff_movement import apply_settlement

            apply_settlement(self, c, payload, role, center_item)
            return
        prior = c.execute("SELECT settlement_json FROM station_handoff_settlement WHERE handoff_item_uuid=?",
                          (center_item["handoff_item_uuid"],)).fetchone()
        if prior is not None:
            recorded = json.loads(prior[0])
            if recorded == settlement:
                # 首次结算已经提交。正常本地任务/后续运输可合法推进当前库存；旧证明只作幂等回放。
                return
            if any(recorded[key] != settlement[key] for key in ("decision", "actual_holder_station_uuid", "proof_digest")):
                raise HandoffInventoryError("settlement_conflict", "原结算方向或证明不可替换")
        _, item = self._rows(c, payload["handoff_uuid"], center_item["handoff_item_uuid"])
        state = "canceled" if settlement["decision"] == "source_present" else "settled"
        if item["state"] in {"settled", "canceled"} and item["state"] != state:
            raise HandoffInventoryError("settlement_conflict", "核实结论不能覆盖已结算的物理方向")
        if state == "settled" and role == "source":
            row = c.execute("SELECT proof_uuid FROM station_handoff WHERE handoff_uuid=?", (payload["handoff_uuid"],)).fetchone()
            released = payload["released"]
            if released is None or released["release_uuid"] != row["proof_uuid"]:
                proof = c.execute("SELECT body_json FROM station_handoff_outbox WHERE handoff_uuid=? AND purpose=?",
                                  (payload["handoff_uuid"], "source_departure_verified:" + item["handoff_item_uuid"])).fetchone()
                if proof is None or json.loads(proof[0])["payload"]["items"][0] != center_item.get("source_departure_verification"):
                    raise HandoffInventoryError("departure_verification_mismatch", "中心目标结算缺少本站冻结或离站复核证明")
        if state == "settled" and role == "target":
            if item["receipt_json"] is None or digest(json.loads(item["receipt_json"])) != digest(center_item["receipt"]):
                raise HandoffInventoryError("receipt_mismatch", "核实收货与本站持久 receipt 不一致")
            for ref in center_item["receipt"]["target_material_refs"]:
                if self._material(c, ref["local_material_uuid"])["aggregate_version"] != ref["local_revision"]:
                    raise HandoffInventoryError("receipt_revision_mismatch", "核实收货引用的本站修订已变化")
        if state == "canceled":
            if item["receipt_json"] is not None:
                raise HandoffInventoryError("return_handoff_required", "本站已收货，不可按回源取消擦除实际收货")
            if role == "source":
                proof = c.execute("SELECT body_json FROM station_handoff_outbox WHERE handoff_uuid=? AND purpose=?",
                                  (payload["handoff_uuid"], "source_verified:" + item["handoff_item_uuid"])).fetchone()
                if proof is None or json.loads(proof[0])["payload"]["items"][0] != center_item["source_verification"]:
                    raise HandoffInventoryError("verification_mismatch", "中心回源结算缺少本站原复核证明")
                for ref in center_item["source_material_refs"]:
                    if self._material(c, ref["local_material_uuid"])["aggregate_version"] != ref["local_revision"]:
                        raise HandoffInventoryError("source_revision_mismatch", "复核后源物料修订已变化")
            else:
                self.ingress.cancel_in_transaction(c, item["reservation_uuid"],
                                                   reason="center-verified-source-present", handoff_uuid=payload["handoff_uuid"])
        self._release(c, item, state=state, role=role)
        c.execute("INSERT INTO station_handoff_settlement_history VALUES (?,?,?) ON CONFLICT DO NOTHING",
                  (item["handoff_item_uuid"], settlement["handoff_revision"], _json(settlement)))
        c.execute(
            "INSERT INTO station_handoff_settlement(handoff_item_uuid,settlement_json) VALUES (?,?) "
            "ON CONFLICT(handoff_item_uuid) DO UPDATE SET settlement_json=excluded.settlement_json",
            (item["handoff_item_uuid"], _json(settlement)),
        )

    def has_handoff(self, handoff_uuid: str) -> bool:
        """只检查是否已经持久建立本站保护，不推断实物位置。"""
        return (
            self.store.query_one("SELECT 1 FROM station_handoff WHERE handoff_uuid=?", (_uuid(handoff_uuid),))
            is not None
        )

    def pending_intents(self, *, after_event_uuid: str = "") -> list[dict[str, Any]]:
        """按库存提交顺序返回未确认边界；序号由共享工作流事件账本分配。"""
        return [
            json.loads(row["body_json"])
            for row in self.store.query_all(
                """SELECT body_json FROM station_handoff_outbox WHERE application IS NULL
                   AND rowid>COALESCE((SELECT rowid FROM station_handoff_outbox WHERE event_uuid=?),0)
                   AND NOT EXISTS (SELECT 1 FROM station_handoff_recovery_pending p WHERE p.event_uuid=station_handoff_outbox.event_uuid)
                   ORDER BY rowid LIMIT 100""", (after_event_uuid,),
            )
        ]

    def retain_recovery_intent(self, event_uuid: str, reason: str) -> None:
        """保留旧库存责任与原信封，不作为成功 ACK，也不阻塞新纪元补报。"""
        with self.store.transaction() as c:
            if c.execute("SELECT 1 FROM station_handoff_outbox WHERE event_uuid=?", (_uuid(event_uuid),)).fetchone() is None:
                raise HandoffInventoryError("intent_not_found", "库存交接 intent 不存在")
            c.execute("INSERT INTO station_handoff_recovery_pending VALUES (?,?) ON CONFLICT(event_uuid) DO UPDATE SET reason=excluded.reason",
                      (event_uuid, reason))

    def recovery_pending_intents(self) -> list[dict[str, Any]]:
        """独立列出待核实责任；原 outbox/application 与库存保护仍保留。"""
        return self.store.query_all("SELECT event_uuid,reason FROM station_handoff_recovery_pending ORDER BY event_uuid")

    def remember_envelope(self, event_uuid: str, envelope: Mapping[str, Any]) -> None:
        """保存首次分号的原信封，阻止工作流库回退后改号补报。"""
        from unilabos.center_protocol.handoff import INTENT_FIELDS, validate_handoff_event

        envelope = validate_handoff_event(dict(envelope))
        with self.store.transaction() as c:
            row = c.execute(
                "SELECT envelope_json,body_json FROM station_handoff_outbox WHERE event_uuid=?", (_uuid(event_uuid),)
            ).fetchone()
            if row is None:
                raise HandoffInventoryError("intent_not_found", "库存交接 intent 不存在")
            if digest({key: envelope[key] for key in INTENT_FIELDS}) != digest(json.loads(row["body_json"])):
                raise HandoffInventoryError("intent_changed", "分号过程改变了原库存 intent")
            if row[0] is not None and digest(json.loads(row[0])) != digest(dict(envelope)):
                raise HandoffInventoryError(
                    "event_ledger_rolled_back", "共享事件账本改变了已冻结交接信封，必须核实恢复"
                )
            c.execute(
                "UPDATE station_handoff_outbox SET envelope_json=? WHERE event_uuid=?",
                (_json(dict(envelope)), event_uuid),
            )

    def acknowledge_intent(self, event_uuid: str, response: Mapping[str, Any]) -> None:
        """精确核对原信封 ACK 并保存完整结论；冲突不能释放库存保护。"""
        with self.store.transaction() as c:
            row = c.execute("SELECT * FROM station_handoff_outbox WHERE event_uuid=?", (_uuid(event_uuid),)).fetchone()
            if row is None or row["envelope_json"] is None:
                raise HandoffInventoryError("intent_not_numbered", "库存 intent 尚无已冻结事件信封")
            envelope = json.loads(row["envelope_json"])
            from unilabos.center_protocol.handoff import validate_handoff_ack

            response = validate_handoff_ack(dict(response), envelope)
            ack = dict(response["ack"])
            # handoff 与 invocation 使用相同精确 ACK 身份字段。
            for field in ("event_uuid", "event_epoch_uuid", "event_sequence", "event_digest"):
                if ack.get(field) != envelope[field]:
                    raise HandoffInventoryError("ack_mismatch", "中心 ACK 与原库存边界事件不匹配")
            if ack.get("application") not in {"applied", "duplicate", "conflict"}:
                raise HandoffInventoryError("ack_mismatch", "中心 ACK 处理结果无效")
            if row["response_json"] is not None and digest(json.loads(row["response_json"])) != digest(dict(response)):
                raise HandoffInventoryError("ack_changed", "中心返回的原事件回执发生变化")
            c.execute(
                "UPDATE station_handoff_outbox SET application=?,response_json=? WHERE event_uuid=?",
                (ack["application"], _json(dict(response)), event_uuid),
            )

    def _new(self, c, payload, role, plan_digest):
        if (
            payload["permit"] is not None
            or payload["attention"]
            or payload["status"] not in {"requested", "prepared", "released", "ready"}
        ):
            raise HandoffInventoryError("handoff_not_preparable", "仅无许可、无异常的未发运交接可以建立保护")
        self._record_new(c, payload, role, plan_digest)

    def _record_new(self, c, payload, role, plan_digest):
        c.execute(
            "INSERT INTO station_handoff VALUES (?,?,?,?,?,?,?,'protected',?,?)",
            (
                payload["handoff_uuid"],
                role,
                self.station_uuid,
                self.instance_uuid,
                payload["revision"],
                _json(payload),
                str(uuid4()),
                plan_digest,
                self._now_ms(),
            ),
        )

    def _existing(self, c, payload, plan_digest):
        row = c.execute("SELECT * FROM station_handoff WHERE handoff_uuid=?", (payload["handoff_uuid"],)).fetchone()
        if row is not None:
            self._assert_same_items(json.loads(row["package_json"]), payload)
            if row["state"] == "canceled_unprepared":
                raise HandoffInventoryError("handoff_canceled", "交接已持久确认未建立保护并取消，不能使用旧包重建")
            if row["plan_digest"] != plan_digest:
                raise HandoffInventoryError("plan_conflict", "同一交接不能更换本站接收或放行计划")
        return row

    @staticmethod
    def _can_cancel(payload):
        return (
            payload["status"] in {"canceling", "canceled"}
            and payload["permit"] is None
            and payload.get("pickup") is None
            and not payload["attention"]
            and not any(item["receipt"] or item.get("settlement") or item.get("movement_review")
                        for item in payload["items"])
        )

    def _emit_cancel_ack(self, c, payload):
        return self._emit(
            c, payload, "cancel_ack",
            {
                "kind": "cancel_ack", "observed_revision": payload["revision"],
                "protection_released": True,
                "evidence_refs": ["inventory:handoff:" + payload["handoff_uuid"]],
            },
        )

    @staticmethod
    def _assert_same_items(old, new):
        fixed = (
            "handoff_uuid",
            "lab_uuid",
            "source_station_uuid",
            "target_station_uuid",
            "source_instance_uuid",
            "target_instance_uuid",
            "global_task_uuid",
            "transfer_definition_uuid",
            "transfer_source_kind",
            "chain_uuid",
            "leg_no",
            "previous_handoff_uuid",
        )

        def items(payload):
            return [
                {k: v for k, v in item.items() if k not in {"receipt", "current_state_version", "source_verification", "source_departure_verification", "settlement", "movement_review", "covered_transport_digests"}}
                for item in payload["items"]
            ]

        if any(old.get(k) != new.get(k) for k in fixed) or digest(items(old)) != digest(items(new)):
            raise HandoffInventoryError("handoff_identity_changed", "交接固定身份或明细快照发生变化")

    @staticmethod
    def _material(c, identity):
        row = c.execute("SELECT * FROM material WHERE uuid=? AND deleted_at IS NULL", (identity,)).fetchone()
        if row is None:
            raise HandoffInventoryError("material_missing", "本站物料不存在或已删除")
        return row

    @staticmethod
    def _subtree(c, carrier):
        return {
            row[0]
            for row in c.execute(
                "WITH RECURSIVE edges(parent_uuid,uuid) AS (SELECT parent_uuid,uuid FROM material WHERE deleted_at IS NULL UNION SELECT material_uuid,occupied_material_uuid FROM site WHERE deleted_at IS NULL AND occupied_material_uuid IS NOT NULL), children(uuid) AS (SELECT ? UNION SELECT m.uuid FROM edges m JOIN children p ON m.parent_uuid=p.uuid) SELECT uuid FROM children",
                (carrier,),
            ).fetchall()
        }

    @staticmethod
    def _insert_item(c, payload, item, carrier):
        c.execute(
            "INSERT INTO station_handoff_item(handoff_item_uuid,handoff_uuid,container_uuid,carrier_material_uuid,state) VALUES (?,?,?,?,'protected')",
            (item["handoff_item_uuid"], payload["handoff_uuid"], item["container_uuid"], carrier),
        )

    @staticmethod
    def _bind_material(c, item, ref, local, revision):
        c.execute(
            "INSERT INTO material_station_binding VALUES (?,?,?,?,?)",
            (item["handoff_item_uuid"], ref["material_lineage_uuid"], local, _json(ref), revision),
        )

    def _revive_incoming(self, c, material, template_uuid):
        if material["inventory_status"] not in {"handoff_departed", "incoming_canceled"} or material["resource_template_uuid"] != template_uuid:
            raise HandoffInventoryError("carrier_already_present", "本站已有物料仍占有库存或模板不匹配")
        # 只能复用已结算或已取消身份；其他交接的有效保护不得被释放。
        c.execute(
            "UPDATE station_handoff_resource SET active=0 WHERE material_uuid=? AND handoff_item_uuid IN (SELECT handoff_item_uuid FROM station_handoff_item WHERE state IN ('settled','canceled'))",
            (material["uuid"],),
        )
        if c.execute("SELECT 1 FROM station_handoff_resource WHERE material_uuid=? AND active=1", (material["uuid"],)).fetchone():
            raise HandoffInventoryError("carrier_protected", "已有物料仍属于未结算交接")
        c.execute(
            "UPDATE material SET inventory_status='incoming',disposition='active',aggregate_version=aggregate_version+1 WHERE uuid=?", (material["uuid"],),
        )

    def _create_incoming(self, c, template_uuid, *, barcode="", parent_uuid=None, adapted=False, source_name=None):
        template = c.execute(
            "SELECT * FROM resource_template WHERE uuid=? AND deleted_at IS NULL", (_uuid(template_uuid),)
        ).fetchone()
        if template is None or template["resource_type"] == "device":
            raise HandoffInventoryError("template_missing", "incoming 需要本站物料模板，不能自动导入外站模板或设备")
        if not adapted and json.loads(template["available_sites"] or "[]"):
            raise HandoffInventoryError(
                "nested_template_unsupported", "含默认子物料的模板需先配置显式接收适配，不能猜测跨站内容"
            )
        identity = str(uuid4())
        now = datetime.fromtimestamp(self._now_ms() / 1000, timezone.utc)
        name = "incoming-" + identity
        if source_name:
            base_name = f"{source_name}_incoming_{now.astimezone():%Y%m%d%S}"
            # 子物料和载体离站后都可能成为根物料，按全站未删除物料去重。
            used_suffixes = {
                row[0]
                for row in c.execute(
                    "SELECT substr(name, ?) FROM material WHERE deleted_at IS NULL "
                    "AND lower(substr(name, 1, ?)) = lower(?)",
                    (len(base_name) + 1, len(base_name), base_name),
                )
            }
            name = base_name
            if "" in used_suffixes:
                sequence = 1
                while f"_{sequence:02d}" in used_suffixes:
                    sequence += 1
                name = f"{base_name}_{sequence:02d}"
        c.execute(
            "INSERT INTO material(uuid,create_time,update_time,resource_template_uuid,parent_uuid,class,type,barcode,name,inventory_status,legacy_template_id,aggregate_version) VALUES (?,?,?,?,?,?,?,?,?,'incoming',?,1)",
            (
                identity,
                now.isoformat(),
                now.isoformat(),
                template_uuid,
                parent_uuid,
                template["resource_type"],
                template["resource_type"],
                barcode,
                name,
                template_uuid,
            ),
        )
        return identity

    def _protect_material(self, c, payload, item, material, status, *, reservation_uuid=""):
        keys = (material_lock_key(material["uuid"]), device_lock_key(material["uuid"]))
        assert_resource_keys_unclaimed(c, lock_keys=keys, ignore_ingress_reservation_uuid=reservation_uuid)
        for key in keys:
            c.execute(
                "INSERT INTO station_handoff_resource VALUES (?,?,?,?,?,1)",
                (
                    payload["handoff_uuid"],
                    item["handoff_item_uuid"],
                    key,
                    material["uuid"],
                    material["inventory_status"],
                ),
            )
        c.execute("UPDATE material SET inventory_status=? WHERE uuid=?", (status, material["uuid"]))

    @staticmethod
    def _validate_site_template(c, site_uuid, template_uuid):
        site = c.execute(
            "SELECT allowed_resource_template_uuids FROM site WHERE uuid=? AND deleted_at IS NULL", (site_uuid,)
        ).fetchone()
        allowed = json.loads(site[0]) if site is not None else None
        if allowed is None or (allowed and template_uuid not in allowed):
            raise HandoffInventoryError("ingress_template_mismatch", "所选入口不支持本站载体模板")

    @staticmethod
    def _rows(c, handoff, item):
        h = c.execute("SELECT * FROM station_handoff WHERE handoff_uuid=?", (handoff,)).fetchone()
        i = c.execute(
            "SELECT * FROM station_handoff_item WHERE handoff_uuid=? AND handoff_item_uuid=?", (handoff, item)
        ).fetchone()
        if h is None or i is None:
            raise HandoffInventoryError("handoff_not_found", "本站尚未建立该交接明细")
        return h, i

    def _material_snapshots(self, c, item):
        return [
            {
                "local_material_uuid": r["uuid"],
                "local_revision": r["aggregate_version"],
                "resource_template_uuid": r["resource_template_uuid"],
                "config": json.loads(r["config"]),
                "data": json.loads(r["data"]),
            }
            for r in c.execute(
                "SELECT m.* FROM material m JOIN material_station_binding b ON b.local_material_uuid=m.uuid WHERE b.handoff_item_uuid=? ORDER BY b.material_lineage_uuid",
                (item,),
            ).fetchall()
        ]

    def _release(self, c, item, *, state, role):
        for resource in c.execute(
            "SELECT DISTINCT material_uuid,original_status FROM station_handoff_resource WHERE handoff_item_uuid=? AND active=1",
            (item["handoff_item_uuid"],),
        ).fetchall():
            if state == "canceled":
                status = resource["original_status"] if role == "source" else "incoming_canceled"
            else:
                status = "handoff_departed" if role == "source" else "warehouse"
            if (state == "settled" and role == "source") or (state == "canceled" and role == "target"):
                clear_site_occupancy(c, material_uuid=resource["material_uuid"])
                # 离站/取消内容是历史事实；软删除释放本站活动样品编码且不修改冻结快照。
                c.execute(
                    "UPDATE container_content SET deleted_at=?,update_time=? WHERE material_uuid=? AND deleted_at IS NULL",
                    (datetime.fromtimestamp(self._now_ms() / 1000, timezone.utc).isoformat(),
                     datetime.fromtimestamp(self._now_ms() / 1000, timezone.utc).isoformat(), resource["material_uuid"]),
                )
                # 离站或取消 incoming 后的 parent 已是历史位置，不可混进复用载体的当前树。
                # 谱系映射、冻结包和 receipt 仍保留原身份供追溯。
                c.execute(
                    "UPDATE material SET parent_uuid=NULL WHERE uuid=?",
                    (resource["material_uuid"],),
                )
            c.execute("UPDATE material SET inventory_status=? WHERE uuid=?", (status, resource["material_uuid"]))
        c.execute(
            "UPDATE station_handoff_resource SET active=0 WHERE handoff_item_uuid=?", (item["handoff_item_uuid"],)
        )
        c.execute(
            "UPDATE station_handoff_item SET state=? WHERE handoff_item_uuid=?", (state, item["handoff_item_uuid"])
        )
        # 已取消的 incoming 身份保留为墓碑；普通 Job 不可按其 UUID 直接派发。
        if (state == "canceled" and role == "target") or (state == "settled" and role == "source"):
            c.execute(
                "UPDATE station_handoff_resource SET active=1 WHERE handoff_item_uuid=?", (item["handoff_item_uuid"],)
            )

    def _emit(self, c, payload, purpose, event_payload):
        existing = c.execute(
            "SELECT body_json FROM station_handoff_outbox WHERE handoff_uuid=? AND purpose=?",
            (payload["handoff_uuid"], purpose),
        ).fetchone()
        if existing is not None:
            return json.loads(existing[0])
        body = {
            "event_uuid": str(uuid4()),
            "event_epoch_uuid": self.event_epoch_uuid,
            "station_uuid": self.station_uuid,
            "instance_uuid": self.instance_uuid,
            "handoff_uuid": payload["handoff_uuid"],
            "occurred_at_ms": self._now_ms(),
            "payload": event_payload,
        }
        from unilabos.center_protocol.handoff import validate_handoff_intent

        body = validate_handoff_intent(body)
        c.execute(
            "INSERT INTO station_handoff_outbox(event_uuid,handoff_uuid,purpose,body_json) VALUES (?,?,?,?)",
            (body["event_uuid"], payload["handoff_uuid"], purpose, _json(body)),
        )
        InventoryStore.tx_append_inventory_event(
            c,
            entry_uuid=body["event_uuid"],
            edge_id=self.station_uuid,
            lab_id=self.lab_uuid,
            occurred_at=body["occurred_at_ms"],
            aggregate_type="station_handoff",
            aggregate_id=payload["handoff_uuid"],
            aggregate_version=payload["revision"],
            event_type="station_handoff." + event_payload["kind"],
            payload=body,
        )
        return body

    @staticmethod
    def _intent(c, handoff, purpose):
        row = c.execute(
            "SELECT body_json FROM station_handoff_outbox WHERE handoff_uuid=? AND purpose=?", (handoff, purpose)
        ).fetchone()
        if row is None:
            raise HandoffInventoryError("intent_missing", "库存保护存在但边界 intent 缺失，禁止重新生成")
        return json.loads(row[0])
