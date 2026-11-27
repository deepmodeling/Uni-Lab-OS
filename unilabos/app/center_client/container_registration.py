"""出站选择时保存原登记申请，联网后仅确认同一个物理载体。"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError, _evidence, _json, _uuid
from unilabos.center_protocol.handoff import validate_container


class ContainerRegistrationBridge:
    def __init__(self, client: Any, authority: Any):
        self.client, self.authority = client, authority

    def registration(self, local_carrier_material_uuid: str) -> dict[str, Any] | None:
        row = self.authority.store.query_one(
            "SELECT * FROM container_station_binding WHERE carrier_material_uuid=?",
            (_uuid(local_carrier_material_uuid),),
        )
        if row is None:
            return None
        return {
            "container_uuid": row["container_uuid"],
            "local_carrier_material_uuid": row["carrier_material_uuid"],
            "status": (
                "pending"
                if row["registration_request_json"] and not row["registration_response_json"]
                else "registered"
            ),
            "last_error": row["registration_last_error"],
        }

    def ensure(
        self, *, local_carrier_material_uuid: str, barcode_namespace: str, barcode: str, evidence_refs: list[str]
    ) -> dict[str, Any]:
        evidence = _evidence(evidence_refs)
        if any(
            not isinstance(value, str) or value != value.strip() or not 1 <= len(value.encode()) <= 128
            for value in (barcode_namespace, barcode)
        ):
            raise HandoffInventoryError("invalid_barcode", "条码及命名空间必须为1至128字节且无首尾空白")
        local = _uuid(local_carrier_material_uuid)
        # 身份及请求在同一个库存事务落盘，网络超时和进程重启不生成第二份 c。
        with self.authority.store.transaction() as c:
            material = self.authority._material(c, local)
            if (
                material["barcode"] != barcode
                or material["inventory_status"] != "warehouse"
                or material["disposition"] != "active"
            ):
                raise HandoffInventoryError("source_unavailable", "实际载体、条码或库存状态不符合登记要求")
            row = c.execute(
                "SELECT * FROM container_station_binding WHERE carrier_material_uuid=?", (local,)
            ).fetchone()
            if row is None:
                identity = str(uuid4())
                request = dict(
                    container_uuid=identity,
                    barcode_namespace=barcode_namespace,
                    barcode=barcode,
                    evidence_refs=evidence,
                )
                if c.execute(
                    "SELECT 1 FROM container_station_binding WHERE barcode_namespace=? AND barcode=?",
                    (barcode_namespace, barcode),
                ).fetchone():
                    raise HandoffInventoryError("binding_conflict", "此条码已经绑定其他载体")
                c.execute(
                    """INSERT INTO container_station_binding(container_uuid,station_uuid,carrier_material_uuid,
                    barcode_namespace,barcode,registration_request_json) VALUES (?,?,?,?,?,?)""",
                    (identity, self.authority.station_uuid, local, barcode_namespace, barcode, _json(request)),
                )
            else:
                if (row["station_uuid"], row["barcode_namespace"], row["barcode"]) != (
                    self.authority.station_uuid,
                    barcode_namespace,
                    barcode,
                ):
                    raise HandoffInventoryError("binding_conflict", "已有绑定的工站或条码与本次选择不一致")
                identity = row["container_uuid"]
                request = json.loads(row["registration_request_json"]) if row["registration_request_json"] else None
                if row["registration_response_json"]:
                    request = None
        try:
            if request is not None:
                self.client.register_container(request, self.authority.event_epoch_uuid)
            # 幂等登记回执可能早于之后的运输；必须重新查询当前占有，不能信旧回执。
            state = validate_container(self.client.container(identity, self.authority.event_epoch_uuid), identity)
            if (
                state["lab_uuid"] != self.authority.lab_uuid
                or state["current_station_uuid"] != self.authority.station_uuid
                or state["barcode_namespace"] != barcode_namespace
                or state["barcode"] != barcode
                or state["custody_status"] != "available"
                or state["active_handoff_item_uuid"] is not None
                or state["attention"]
            ):
                raise HandoffInventoryError("container_unavailable", "中心容器当前归属、条码或交接状态不允许本站出站")
            with self.authority.store.transaction() as c:
                material = self.authority._material(c, local)
                if (
                    material["barcode"] != barcode
                    or material["inventory_status"] != "warehouse"
                    or material["disposition"] != "active"
                ):
                    raise HandoffInventoryError("source_changed", "登记期间实际载体状态已改变，需要重新核实")
                c.execute(
                    "UPDATE container_station_binding SET registration_response_json=?,registration_last_error=NULL WHERE container_uuid=?",
                    (_json(state), identity),
                )
        except Exception as error:
            with self.authority.store.transaction() as c:
                c.execute(
                    "UPDATE container_station_binding SET registration_last_error=? WHERE container_uuid=?",
                    (str(error)[:500], identity),
                )
            raise
        return self.registration(local)

    def attest_material_inputs(self, local_carrier_material_uuid: str) -> dict[str, Any]:
        """将当前完整树及已知谱系作为本站历史输入事实提交，不取得库存占用。"""
        from unilabos.app.scheduler.inventory.handoff_adapter import export_snapshot
        from unilabos.persistence.inspection import table_exists

        local = _uuid(local_carrier_material_uuid)
        with self.authority.store.read_connection() as c:
            binding = c.execute(
                "SELECT * FROM container_station_binding WHERE carrier_material_uuid=?", (local,)
            ).fetchone()
            if binding is None or binding["station_uuid"] != self.authority.station_uuid:
                raise HandoffInventoryError("container_not_bound", "历史输入载体尚未绑定本站容器")
            if binding["registration_request_json"] and not binding["registration_response_json"]:
                raise HandoffInventoryError("container_registration_pending", "历史输入容器登记尚未确认")
            identity = binding["container_uuid"]
        state = validate_container(self.client.container(identity, self.authority.event_epoch_uuid), identity)
        if (
            state["current_station_uuid"] != self.authority.station_uuid
            or state["lab_uuid"] != self.authority.lab_uuid
            or state["custody_status"] != "available"
            or state["attention"]
            or state["active_handoff_item_uuid"] is not None
            or state["barcode_namespace"] != binding["barcode_namespace"]
            or state["barcode"] != binding["barcode"]
        ):
            raise HandoffInventoryError("container_unavailable", "历史输入容器当前归属或交接状态不符合本站证明要求")
        with self.authority.store.read_connection() as c:
            refs = []
            for material_uuid in sorted(self.authority._subtree(c, local)):
                material = self.authority._material(c, material_uuid)
                if material["inventory_status"] != "warehouse" or material["disposition"] != "active":
                    raise HandoffInventoryError("source_unavailable", "历史输入实际载体及内容必须在本站可用库存中")
                known = {
                    row["material_lineage_uuid"]
                    for row in c.execute(
                        "SELECT material_lineage_uuid FROM material_station_binding WHERE local_material_uuid=?",
                        (material_uuid,),
                    ).fetchall()
                }
                if table_exists(c, "station_material_export"):
                    known.update(
                        row["material_lineage_uuid"]
                        for row in c.execute(
                            """SELECT item.material_lineage_uuid FROM station_material_export_item item
                        JOIN station_material_export e USING(invocation_uuid) WHERE item.local_material_uuid=?
                        AND item.material_lineage_uuid IS NOT NULL AND e.completed=1 AND e.response_json IS NOT NULL""",
                            (material_uuid,),
                        ).fetchall()
                    )
                if len(known) != 1:
                    raise HandoffInventoryError(
                        "lineage_unknown", "历史输入完整树中的物料缺少唯一已验证谱系，不能申请新身份代替"
                    )
                refs.append(
                    dict(
                        station_uuid=self.authority.station_uuid,
                        local_material_uuid=material_uuid,
                        local_revision=material["aggregate_version"],
                        material_lineage_uuid=next(iter(known)),
                    )
                )
            snapshot = export_snapshot(self.authority, c, local, refs, template_evidence=True)
        request = {
            "container_state_version": state["state_version"],
            "material_refs": refs,
            "inventory_snapshot": snapshot,
        }
        return self.client.attest_material_inputs(identity, request, self.authority.event_epoch_uuid)
