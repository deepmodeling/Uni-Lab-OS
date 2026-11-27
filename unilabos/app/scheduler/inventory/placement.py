"""逐件搬运的真实库存检查；预检只读，正式准备仍由原交接事务负责。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from unilabos.app.scheduler.inventory.dispatch_admission import assert_resource_keys_unclaimed
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError
from unilabos.app.scheduler.resource_lock import device_lock_key, site_lock_key
from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.placement import (
    _object,
    _uuid,
    _version,
    validate_transfer_destinations,
)


def _request(value: Any) -> dict[str, Any]:
    # 页面不能再提交现有库存作为来源；真实来源由上游成功输出确定。
    if not isinstance(value, Mapping):
        raise ValueError("搬运选择请求必须为对象")
    if "selection" in value:
        raise ValueError("库存直选已移除，请使用上游工作流物料输出")
    result = _object(value, {"schema_version", "role", "source_station_uuid", "target_station_uuid", "transfer_definition_uuid", "destinations"})
    if type(result["schema_version"]) is not int or result["schema_version"] != 1 or result["role"] != "target":
        raise ValueError("搬运检查角色或版本无效")
    for key in ("source_station_uuid", "target_station_uuid", "transfer_definition_uuid"):
        _uuid(result[key])
    if result["source_station_uuid"] == result["target_station_uuid"]:
        raise ValueError("跨站搬运来源与目标不能相同")
    result["destinations"] = validate_transfer_destinations(result["destinations"])
    return result


def validate_inventory_placement(authority, request: Mapping[str, Any], *, session: Mapping[str, Any]) -> dict[str, Any]:
    body = _request(request)
    if (body[body["role"] + "_station_uuid"] != authority.station_uuid
            or session.get("station_uuid") != authority.station_uuid
            or session.get("instance_uuid") != authority.instance_uuid):
        raise ValueError("搬运检查不属于本站当前控制身份")
    for key in ("station_uuid", "instance_uuid", "process_uuid", "session_uuid"):
        _uuid(session.get(key))
    _version(session.get("session_epoch"))
    selected = body["destinations"]
    checks, items = [], []
    with authority.store.read_connection() as c:
        for item in selected["items"]:
            try:
                facts = _target(authority, c, item)
                items.append({"selection_uuid": item["selection_uuid"], **facts})
            except (ValueError, HandoffInventoryError) as error:
                checks.append({"selection_uuid": item["selection_uuid"], "role": body["role"],
                    "code": "TRANSFER_TARGET_UNAVAILABLE",
                    "blocking": True, "message": str(error)})
    return {"schema_version": 1, "capabilities": ["transfer_placement.v1"],
        **{key: session[key] for key in ("station_uuid", "instance_uuid", "process_uuid", "session_uuid", "session_epoch")},
        "role": body["role"], "transfer_definition_uuid": body["transfer_definition_uuid"],
        "input_digest": digest(body),
        "selection_digest": digest({"transfer_definition_uuid": body["transfer_definition_uuid"], "destinations": selected}),
        "can_run": not checks, "checks": checks, "items": items}


def _target(authority, c, item):
    site = c.execute("SELECT * FROM site WHERE uuid=? AND deleted_at IS NULL", (item["target_site_uuid"],)).fetchone()
    template = c.execute("SELECT * FROM resource_template WHERE uuid=? AND deleted_at IS NULL", (item["target_resource_template_uuid"],)).fetchone()
    if site is None or template is None or template["resource_type"] == "device":
        raise ValueError("目标库位或物料模板不存在")
    authority._validate_site_template(c, site["uuid"], template["uuid"])
    if site["occupied_material_uuid"] is not None or c.execute("SELECT 1 FROM station_ingress_reservation WHERE site_uuid=? AND site_active=1", (site["uuid"],)).fetchone():
        raise ValueError("指定目标库位已被占用或预留")
    assert_resource_keys_unclaimed(c, lock_keys=[site_lock_key(site["material_uuid"], site["uuid"]), device_lock_key(site["material_uuid"])], allow_ingress_device_sharing=True)
    return {"target_site_uuid": site["uuid"], "target_resource_template_uuid": template["uuid"], "site_owner_uuid": site["material_uuid"]}


def exact_incoming_plans(payload: Mapping[str, Any], configured: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """沿用本站模板配置，只将每件的候选入口收窄到原指定位置。"""
    from copy import deepcopy

    from unilabos.center_protocol.placement import validate_item_placement
    plans = deepcopy(dict(configured))
    for item in payload["items"]:
        placement = item.get("placement_plan")
        if placement is None:
            continue
        selected = validate_item_placement(placement)["selection"]
        identity = item["handoff_item_uuid"]
        if identity not in plans or plans[identity].get("carrier_template_uuid") != selected["target_resource_template_uuid"]:
            raise HandoffInventoryError("placement_template_mismatch", "本站接收模板与提前选择不一致")
        plans[identity]["candidate_site_uuids"] = [selected["target_site_uuid"]]
    return plans


def verify_source_placement(authority, connection, payload, item, carrier) -> None:
    placement = item.get("placement_plan")
    if placement is None:
        return
    selected = placement["selection"]
    sites=connection.execute("SELECT * FROM site WHERE occupied_material_uuid=? AND deleted_at IS NULL",(carrier["uuid"],)).fetchall()
    if len(sites)>1:
        raise HandoffInventoryError("placement_source_changed","来源载体同时占用多个库位")
    if selected.get("source_site_uuid") is not None:
        if len(sites)!=1 or sites[0]["uuid"]!=selected["source_site_uuid"]:
            raise HandoffInventoryError("placement_source_changed","实际来源位与原选择不符")
    elif payload.get("transfer_source_kind") != "node_output":
        raise HandoffInventoryError("placement_source_changed","库存选择必须具有真实来源位")
    if carrier["uuid"]!=selected["source_material_uuid"] or carrier["aggregate_version"]!=selected["source_material_revision"]:
        raise HandoffInventoryError("placement_source_changed","来源物料或版本与原选择不符")
    if sites:
        site=sites[0]
        assert_resource_keys_unclaimed(connection,lock_keys=[site_lock_key(site["material_uuid"],site["uuid"]),device_lock_key(site["material_uuid"])],ignore_handoff_uuid=payload["handoff_uuid"],allow_ingress_device_sharing=True)


def automatic_incoming_plans(authority, payload: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    """精确计划可自动准备已明确的根模板；复杂子树仍需本站显式模板适配。"""
    if not all(item.get("placement_plan") is not None for item in payload["items"]):
        return None
    plans = {}
    for item in payload["items"]:
        selected = item["placement_plan"]["selection"]
        refs = item["source_material_refs"]
        if len(refs) != 1 or refs[0]["local_material_uuid"] != selected["source_material_uuid"]:
            return None
        plan = {"carrier_template_uuid": selected["target_resource_template_uuid"],
            "carrier_lineage_uuid": refs[0]["material_lineage_uuid"],
            "lineage_template_uuids": {refs[0]["material_lineage_uuid"]: selected["target_resource_template_uuid"]},
            "candidate_site_uuids": [selected["target_site_uuid"]], "ttl_seconds": 86400}
        snapshot = item["source_snapshot"]
        if snapshot.get("adapter") is not None:
            if snapshot.get("adapter") != "unilab.inventory.v1" or len(snapshot.get("materials", [])) != 1:
                return None
            node = snapshot["materials"][0]
            content = node.get("content")
            if content is not None and content.get("kind") != "sample":
                # 化学目录需显式匹配，不按名称复制未知试剂身份。
                return None
            plan["adapter"] = {"kind": "unilab.inventory.v1", "site_names": {selected["source_material_uuid"]: {site["name"]:site["name"] for site in node["sites"]}}, "reagent_info_uuids": {}}
        plans[item["handoff_item_uuid"]] = plan
    return plans
