"""复用库存预留保存产物保护，任务收尾后原子移交既有交接保护。"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

from unilabos.app.scheduler.inventory.dispatch_admission import assert_resource_keys_unclaimed
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError, StationHandoffAuthority, _json, _uuid
from unilabos.app.scheduler.resource_lock import device_lock_key, material_lock_key

PREFIX = "center-outbound:"


def outbound_blocker(c, lock_keys, *, ignore_invocation_uuid=""):
    from unilabos.app.scheduler.resource_lock import conflicting_resource_lock_keys

    for row in c.execute(
        "SELECT * FROM inventory_reservation WHERE status='active' AND node_id LIKE ? AND workflow_id<>?",
        (PREFIX + "%", ignore_invocation_uuid),
    ).fetchall():
        for local in json.loads(row["amounts_json"]).get("instances", []):
            if conflicting_resource_lock_keys(set(lock_keys), {material_lock_key(local), device_lock_key(local)}):
                return row
    return None


def _output_carriers(c, outputs):
    carriers = {}
    for output in outputs:
        local = _uuid(output["local_material_uuid"])
        row = StationHandoffAuthority._material(c, local)
        if row["resource_template_uuid"] != output["resource_template_uuid"]:
            from unilabos.center_protocol.material import MaterialAllocationError

            raise MaterialAllocationError("实际库存模板与冻结输出端口不匹配")
        cursor, seen, carrier = local, set(), local
        while cursor:
            if cursor in seen:
                raise HandoffInventoryError("invalid_material_tree", "实际物料树包含循环")
            seen.add(cursor)
            binding = c.execute(
                "SELECT * FROM container_station_binding WHERE carrier_material_uuid=?", (cursor,)
            ).fetchone()
            if binding is not None:
                carrier = cursor
                break
            cursor = StationHandoffAuthority._material(c, cursor)["parent_uuid"]
        carriers[carrier] = StationHandoffAuthority._subtree(c, carrier)
    return carriers


def protect_outputs(store, *, invocation_uuid: str, task_uuid: str, outputs: list[dict[str, Any]]) -> None:
    """必须先有实际载体独占准入；预检不调用此写操作，保留完整子树及原始任务证据。"""
    invocation, task = _uuid(invocation_uuid), _uuid(task_uuid)
    with store.transaction() as c:
        carriers = _output_carriers(c, outputs)
        for carrier, materials in carriers.items():
            node = PREFIX + carrier
            old = c.execute(
                "SELECT * FROM inventory_reservation WHERE workflow_id=? AND node_id=? AND attempt=1",
                (invocation, node),
            ).fetchone()
            snapshots = {local: StationHandoffAuthority._material(c, local)["aggregate_version"] for local in materials}
            evidence = {
                "instances": sorted(materials),
                "outbound_task_uuid": task,
                "carrier_material_uuid": carrier,
                "revisions": snapshots,
            }
            if old is not None:
                if old["status"] != "active" or json.loads(old["amounts_json"]) != evidence:
                    raise HandoffInventoryError("outbound_changed", "已保护产物的实际版本或容器结构改变，必须核实")
                continue
            own = c.execute(
                "SELECT 1 FROM inventory_material_source_binding WHERE workflow_id=? AND material_uuid=? AND custody_policy='task_exclusive' AND status='active'",
                (task, carrier),
            ).fetchone()
            if own is None:
                raise HandoffInventoryError("outbound_exclusive_required", "实际出站载体必须已取得本任务的独占来源准入")
            for local in materials:
                material = StationHandoffAuthority._material(c, local)
                if material["inventory_status"] not in {"warehouse", "reserved"} or material["disposition"] != "active":
                    raise HandoffInventoryError("source_unavailable", "实际容器及内容不是可用库存")
                if c.execute(
                    "SELECT 1 FROM inventory_material_source_binding WHERE material_uuid=? AND status='active' AND (workflow_id<>? OR custody_policy='shared_source')",
                    (local, task),
                ).fetchone():
                    raise HandoffInventoryError("task_claimed", "完整载体或内容仍被其他任务或共享来源占用")
                if c.execute(
                    "SELECT 1 FROM inventory_reservation r,json_each(r.amounts_json,'$.instances') j WHERE r.status='active' AND r.workflow_id NOT IN (?,?) AND j.value=?",
                    (task, invocation, local),
                ).fetchone():
                    raise HandoffInventoryError("task_reserved", "完整载体或内容仍被其他任务预留")
            keys = [key for local in materials for key in (material_lock_key(local), device_lock_key(local))]
            assert_resource_keys_unclaimed(c, lock_keys=keys, material_input_task_uuid=task)
            c.execute(
                "INSERT INTO inventory_reservation(reservation_id,workflow_id,node_id,attempt,status,amounts_json,created_at,version) VALUES (?,?,?,1,'active',?,0,1)",
                (str(uuid4()), invocation, node, _json(evidence)),
            )


def transfer_to_handoff(c, *, carrier_uuid, materials, source_material_refs=None, source_kind=None) -> None:
    """正式冻结原子移交已核实的产物保护，后续错误使移交一起回滚。"""
    _handoff_source(c, carrier_uuid=carrier_uuid, materials=materials,
        source_material_refs=source_material_refs, source_kind=source_kind, release=True)


def validate_handoff_source(c, *, carrier_uuid, materials, source_material_refs, source_kind) -> set[str]:
    """预检复用正式产物证据检查；只返回可接管的原调用，不释放保护。"""
    return _handoff_source(c, carrier_uuid=carrier_uuid, materials=materials,
        source_material_refs=source_material_refs, source_kind=source_kind, release=False)


def _handoff_source(
    c,
    *,
    carrier_uuid: str,
    materials: set[str],
    source_material_refs: list[dict[str, Any]] | None = None,
    source_kind: Literal["node_output", "station_material"] | None = None,
    release: bool,
) -> set[str]:
    """仅在真实交接冻结同一库存事务中撤销本站产物预留，后续错误使移交整体回滚。"""
    rows = c.execute(
        "SELECT * FROM inventory_reservation WHERE status='active' AND node_id=?", (PREFIX + carrier_uuid,)
    ).fetchall()
    if source_kind == "node_output" and not rows:
        raise HandoffInventoryError("outbound_exclusive_required", "工作流出站载体缺少原 Task 移交的真实产物独占保护")
    if len(rows) > 1:
        raise HandoffInventoryError("outbound_conflict", "载体存在多个未完成的出站责任")
    for row in rows:
        facts = json.loads(row["amounts_json"])
        # 本站库存可以没有上游产物；已有产物保护仍须核对正式导出后才能原子接管。
        if source_kind is not None:
            from unilabos.persistence.inspection import table_exists

            export = (
                c.execute(
                    "SELECT * FROM station_material_export WHERE invocation_uuid=?", (row["workflow_id"],)
                ).fetchone()
                if table_exists(c, "station_material_export")
                else None
            )
            if (
                export is None
                or not export["completed"]
                or not export["evidence_json"]
                or not export["containers_json"]
            ):
                raise HandoffInventoryError("outbound_export_pending", "工作流原产物结果及容器边界尚未正式补报")
            entries = json.loads(export["containers_json"])
            binding = c.execute(
                "SELECT container_uuid FROM container_station_binding WHERE carrier_material_uuid=?", (carrier_uuid,)
            ).fetchone()
            boundary = next(
                (
                    entry
                    for entry in entries
                    if binding is not None and entry["container_uuid"] == binding["container_uuid"]
                ),
                None,
            )
            if boundary is None or sorted(
                boundary["source_material_refs"], key=lambda ref: ref["local_material_uuid"]
            ) != sorted(source_material_refs or [], key=lambda ref: ref["local_material_uuid"]):
                raise HandoffInventoryError("outbound_export_conflict", "工作流交接的谱系引用与原产物正式边界不一致")
        if set(facts["instances"]) != materials or any(
            StationHandoffAuthority._material(c, local)["aggregate_version"] != revision
            for local, revision in facts["revisions"].items()
        ):
            raise HandoffInventoryError("outbound_changed", "任务结束后实际容器、内容或库存版本已改变")
        if release:
            c.execute(
                "UPDATE inventory_reservation SET status='released',version=version+1 WHERE reservation_id=?",
                (row["reservation_id"],),
            )
    return {row["workflow_id"] for row in rows}


def advance_released_revision(
    c, *, task_uuid: str, material_uuid: str, before_revision: int, after_revision: int
) -> None:
    """只承认当前任务释放预留导致的单次行政修订；物理变化不能借此刷新保护。"""
    for row in c.execute(
        "SELECT * FROM inventory_reservation WHERE status='active' AND node_id LIKE ?", (PREFIX + "%",)
    ).fetchall():
        facts = json.loads(row["amounts_json"])
        if facts.get("outbound_task_uuid") != task_uuid or material_uuid not in facts["revisions"]:
            continue
        if facts["revisions"][material_uuid] != before_revision or after_revision != before_revision + 1:
            raise HandoffInventoryError("outbound_changed", "释放任务预留前产物版本已漂移，保留原保护")
        facts["revisions"][material_uuid] = after_revision
        c.execute(
            "UPDATE inventory_reservation SET amounts_json=?,version=version+1 WHERE reservation_id=?",
            (_json(facts), row["reservation_id"]),
        )


def admit_produced_outputs(inventory, *, task_uuid: str, outputs: list[dict[str, Any]]) -> None:
    """冻结声明的真实产物出现后走既有 MaterialSource 准入；共享源永不自动升级。"""
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest

    with inventory.store.read_connection() as c:
        carriers = _output_carriers(c, outputs)
        requests = []
        for carrier in carriers:
            active = c.execute(
                "SELECT workflow_id,custody_policy FROM inventory_material_source_binding WHERE material_uuid=? AND status='active'",
                (carrier,),
            ).fetchall()
            if any(row["custody_policy"] == "shared_source" for row in active):
                raise HandoffInventoryError("shared_source_outbound", "共享来源本身不能直接出站，必须先生成独立样品")
            if any(row["workflow_id"] != task_uuid for row in active):
                raise HandoffInventoryError("task_claimed", "实际载体已由其他任务占用")
            if active:
                continue
            # 保护已经建立或移交后重放不重新取得旧 Task 锁。
            protected = c.execute(
                "SELECT 1 FROM inventory_reservation WHERE node_id=? AND status='active'", (PREFIX + carrier,)
            ).fetchone()
            if protected:
                continue
            template = StationHandoffAuthority._material(c, carrier)["resource_template_uuid"]
            requests.append(
                MaterialSourceAdmissionRequest(
                    node_id="center-output:" + carrier,
                    resource_template_uuid=template,
                    custody_policy="task_exclusive",
                    requirement=MaterialRequirement(template_id=template, instance_uuid=carrier),
                )
            )
    if requests:
        inventory.admit_material_sources(task_uuid, requests, actor="center_output", causation_id=task_uuid)


def transfer_to_material_input(c, *, references: list[dict[str, Any]], container_refs: list[dict[str, Any]]) -> None:
    """已核实本站历史输入后，在新输入占用事务内交接已完成的原产物保护。"""
    refs = {ref["local_material_uuid"]: ref for ref in references}
    containers = {ref["container_uuid"] for ref in container_refs}
    rows = c.execute(
        "SELECT * FROM inventory_reservation WHERE status='active' AND node_id LIKE ?", (PREFIX + "%",)
    ).fetchall()
    for row in rows:
        facts = json.loads(row["amounts_json"])
        if not set(facts["instances"]) & set(refs):
            continue
        binding = c.execute(
            "SELECT container_uuid FROM container_station_binding WHERE carrier_material_uuid=?",
            (facts["carrier_material_uuid"],),
        ).fetchone()
        if binding is None or binding["container_uuid"] not in containers:
            raise HandoffInventoryError("outbound_input_conflict", "历史物料输入没有覆盖原保护载体")
        export = c.execute(
            "SELECT * FROM station_material_export WHERE invocation_uuid=?", (row["workflow_id"],)
        ).fetchone()
        if (
            export is None
            or not export["completed"]
            or not export["response_json"]
            or not export["evidence_json"]
            or not c.execute(
                "SELECT 1 FROM station_material_boundary_receipt WHERE invocation_uuid=?", (row["workflow_id"],)
            ).fetchone()
        ):
            raise HandoffInventoryError("outbound_export_pending", "原物料补报或边界确认尚未完成，不能转交新的任务输入")
        previous = {
            item["material_ref"]["local_material_uuid"]: item["material_ref"]
            for item in json.loads(export["response_json"])["items"]
        }
        for local in set(facts["instances"]) & set(refs):
            old, new = previous.get(local), refs[local]
            if (
                old is None
                or old["station_uuid"] != new["station_uuid"]
                or old["material_lineage_uuid"] != new["material_lineage_uuid"]
                or old["local_revision"] != new["local_revision"]
            ):
                raise HandoffInventoryError("outbound_input_conflict", "历史输入身份、谱系或版本与原回执不一致")
        materials = StationHandoffAuthority._subtree(c, facts["carrier_material_uuid"])
        if set(facts["instances"]) != materials or any(
            StationHandoffAuthority._material(c, local)["aggregate_version"] != revision
            for local, revision in facts["revisions"].items()
        ):
            raise HandoffInventoryError("outbound_changed", "原产物保护的完整树或库存版本已改变")
        c.execute(
            "UPDATE inventory_reservation SET status='released',version=version+1 WHERE reservation_id=?",
            (row["reservation_id"],),
        )


def verify_output_protection(store, source: dict[str, Any], *, verify_versions: bool = True) -> None:
    """结果补报只承认已建立的真实产物保护，不能在补报时补造旧 Task 准入。"""
    with store.read_connection() as c:
        for carrier, materials in _output_carriers(c, source["outputs"]).items():
            row = c.execute(
                "SELECT * FROM inventory_reservation WHERE workflow_id=? AND node_id=? AND status='active'",
                (source["invocation_uuid"], PREFIX + carrier),
            ).fetchone()
            if row is None:
                raise HandoffInventoryError(
                    "outbound_exclusive_required", "产物尚无原 Task 移交的真实独占保护，普通 UUID 不能代替来源准入"
                )
            facts = json.loads(row["amounts_json"])
            if (
                facts.get("outbound_task_uuid") != source["local_task_uuid"]
                or set(facts["instances"]) != materials
                or (
                    verify_versions
                    and any(
                        StationHandoffAuthority._material(c, local)["aggregate_version"] != revision
                        for local, revision in facts["revisions"].items()
                    )
                )
            ):
                raise HandoffInventoryError("outbound_changed", "原 Task 产物保护、容器树或库存版本与真实输出不一致")


def admit_dynamic_job_outputs(
    inventory, *, task: dict[str, Any], job: dict[str, Any], return_info: dict[str, Any]
) -> None:
    """生产作业返回新建身份时准入；冻结输入中的普通 UUID 及旧库存都不能冒充新产物。"""
    from unilabos.app.scheduler.param_resolver import json_get_exists
    from unilabos.workflow.material_ports import _path, output_material_items, ports_from_graph

    plan = task.get("execution_plan") or {}
    graph = task.get("workflow_snapshot") or {}
    metadata = graph.get("workflow", {}).get("meta_data", {})
    if not metadata.get("center_material_ports", metadata.get("unilab", {}).get("material_ports", [])):
        return
    ports = ports_from_graph(graph)
    if not ports or job.get("executor_kind") in {
        "material_source",
        "workflow_input",
        "workflow_output",
        "local_control",
    }:
        return
    outputs = {}
    node_uuid = job["workflow_node_uuid"]
    for node in plan.get("nodes", []):
        if node.get("kind") != "workflow_output":
            continue
        for name, binding in node.get("output_bindings", {}).items():
            if binding.get("kind") != "node_output" or binding.get("workflow_node_uuid") != node_uuid:
                continue
            handle = next(
                (
                    item
                    for item in plan.get("handles", [])
                    if item.get("node_uuid") == node_uuid
                    and item.get("template_handle_uuid") == binding.get("source_handle_uuid")
                ),
                None,
            )
            if handle is None:
                continue
            exists, value = json_get_exists(
                return_info.get("return_value", return_info), str(handle.get("data_key") or "")
            )
            if exists:
                outputs[name] = value
    selected = [port for port in ports if port["direction"] == "output" and _path(port["value_path"])[0] in outputs]
    if not selected:
        return
    items = output_material_items(selected, outputs)

    def strings(value):
        if isinstance(value, str):
            return {value}
        if isinstance(value, dict):
            return set().union(*(strings(item) for item in value.values())) if value else set()
        if isinstance(value, list):
            return set().union(*(strings(item) for item in value)) if value else set()
        return set()

    supplied = strings(task.get("input") or {})
    produced = []
    for item in items:
        local = item["local_material_uuid"]
        if local in supplied:
            continue
        # 仅库存权威在生产/创建结算时保存的精确任务、Job、物料事实可授予新来源。
        # 前端内容编辑同样能携带任务字段，因此不能把它的通用事件作为生产凭据。
        receipt = inventory.store.query_one(
            "SELECT entry_uuid FROM inventory_ledger WHERE workflow_task_uuid=? AND workflow_node_job_uuid=? "
            "AND material_uuid=? AND ((op_type='material.load' AND aggregate_type='material' AND quantity_delta=1) "
            "OR (op_type='current_substance.aliquot_receive' AND aggregate_type='current_substance' "
            "AND actor='scheduler' AND reason='physical_settlement' AND causation_id<>'')) "
            "AND entry_uuid IS NOT NULL AND entry_uuid<>'' LIMIT 1",
            (task["uuid"], job["uuid"], local),
        )
        if receipt is None and job.get("executor_kind") == "workflow_command":
            # 原生命令的原子创建效果本身也是准确的 Task/Job 生产凭据；恢复仅消费
            # 已提交效果，不重新运行设备、不把其他命令的普通 UUID 当作产物。
            production = inventory.material_io.committed_production(task_uuid=task["uuid"], job_uuid=job["uuid"])
            if production is not None and production["material"].get("uuid") == local:
                receipt = production
        if receipt is not None:
            produced.append(item)
    if produced:
        admit_produced_outputs(inventory, task_uuid=task["uuid"], outputs=produced)


def settle_canceled_outbound(
    c, *, invocation_uuid: str, task_uuid: str, evidence_refs: list[str],
    reservation_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """中心取消结算确认后核实本站在场产物；不能解除未知位置或其他占用。"""
    from unilabos.app.scheduler.inventory.handoff import _evidence
    from unilabos.app.scheduler.inventory.material_inputs import material_input_blocker
    from unilabos.app.scheduler.resource_lock import conflicting_resource_lock_keys

    _evidence(evidence_refs)
    invocation, task = _uuid(invocation_uuid), _uuid(task_uuid)
    result = []
    rows = c.execute(
        "SELECT * FROM inventory_reservation WHERE workflow_id=? AND node_id LIKE ? AND status='active'",
        (
            invocation,
            PREFIX + "%",
        ),
    ).fetchall()
    if reservation_ids is not None:
        # 单件交接只能释放已核实的原 reservation，不能顺带清除同调用的其他产物。
        rows = [row for row in rows if row["reservation_id"] in reservation_ids]
        if {row["reservation_id"] for row in rows} != set(reservation_ids):
            raise HandoffInventoryError("outbound_changed", "原交接产物保护已经改变")
    for row in rows:
        facts = json.loads(row["amounts_json"])
        carrier = facts["carrier_material_uuid"]
        materials = StationHandoffAuthority._subtree(c, carrier)
        if facts.get("outbound_task_uuid") != task or materials != set(facts["instances"]):
            raise HandoffInventoryError("outbound_changed", "取消产物责任、完整容器与原保护不一致")
        for local, revision in facts["revisions"].items():
            material = StationHandoffAuthority._material(c, local)
            if (
                material["aggregate_version"] != revision
                or material["inventory_status"] != "warehouse"
                or material["disposition"] != "active"
            ):
                raise HandoffInventoryError("outbound_unsettled", "取消产物版本或实际库存状态尚未核实")
            if c.execute(
                "SELECT 1 FROM inventory_material_source_binding WHERE material_uuid=? AND status='active'", (local,)
            ).fetchone():
                raise HandoffInventoryError("outbound_claimed", "取消产物仍有未释放的任务来源占用")
            if c.execute(
                "SELECT 1 FROM inventory_reservation r,json_each(r.amounts_json,'$.instances') j WHERE r.status='active' AND r.reservation_id<>? AND j.value=?",
                (row["reservation_id"], local),
            ).fetchone():
                raise HandoffInventoryError("outbound_claimed", "取消产物仍有其他库存预留")
        keys = {key for local in materials for key in (material_lock_key(local), device_lock_key(local))}
        if material_input_blocker(c, tuple(keys)) is not None:
            raise HandoffInventoryError("outbound_claimed", "取消产物仍有未结算的输入占用")
        # 执行动作、入口预留及既有交接保护统一校验；自己的原产物预留单独排除。
        for lease in c.execute(
            "SELECT lock_key FROM station_execution_lock_lease WHERE state IN ('prepared','reserved','running','uncertain')"
        ).fetchall():
            if conflicting_resource_lock_keys(keys, {lease["lock_key"]}):
                raise HandoffInventoryError("outbound_claimed", "取消产物仍有执行中或未知动作")
        for hold in c.execute("SELECT lock_key FROM station_handoff_resource WHERE active=1").fetchall():
            if conflicting_resource_lock_keys(keys, {hold["lock_key"]}):
                raise HandoffInventoryError("outbound_claimed", "取消产物已经由活动交接接管")
        for hold in c.execute("SELECT lock_key FROM station_ingress_reservation_resource WHERE active=1").fetchall():
            if conflicting_resource_lock_keys(keys, {hold["lock_key"]}):
                raise HandoffInventoryError("outbound_claimed", "取消产物仍有入口保护")
        c.execute(
            "UPDATE inventory_reservation SET status='released',version=version+1 WHERE reservation_id=?",
            (row["reservation_id"],),
        )
        result.append(
            {
                "reservation_id": row["reservation_id"],
                "carrier_material_uuid": carrier,
                "material_uuids": sorted(materials),
                "revisions": facts["revisions"],
                "evidence_refs": list(evidence_refs),
            }
        )
    return result
